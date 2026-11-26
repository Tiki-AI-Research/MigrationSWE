"""Recognize executed commands, rather than runtime words used as shell data.

This stdlib-only module is also projected into the public blackbox assets.
It does not execute candidate text. Unknown executable expansions are reported
as dynamic commands; expansions used only as arguments are not process sinks.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shlex

NODE_RUNTIMES = {"node", "nodejs", "npx", "ts-node", "tsx", "deno", "bun"}
SHELLS = {"sh", "bash", "dash", "zsh", "ksh"}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=")


def without_shell_comments(script):
    # shlex's default comment handling consumes the newline as well, merging
    # the next executable into the previous command's arguments.
    clean, quote, index = [], "", 0
    while index < len(script):
        char = script[index]
        if char == "\\" and quote != "'" and index + 1 < len(script):
            clean.extend(script[index:index + 2])
            index += 2
            continue
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or script[index - 1].isspace() or script[index - 1] in ";&|()"):
            while index < len(script) and script[index] != "\n":
                index += 1
            continue
        clean.append(char)
        index += 1
    return "".join(clean)


def shell_commands(script):
    """Split shell command positions while respecting quotes and comments."""
    lexer = shlex.shlex(without_shell_comments(script), posix=True, punctuation_chars=";&|()<>\n")
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    lexer.commenters = ""
    groups, command = [], []
    skip_redirect = False
    for token in lexer:
        if token and all(c in ";&|()\n" for c in token):
            if command:
                groups.append(command)
            command = []
        elif token and all(c in "<>" for c in token):
            skip_redirect = True
        elif skip_redirect:
            skip_redirect = False
        else:
            command.append(token)
    if command:
        groups.append(command)
    return groups


def _substitutions(script):
    """Yield executable substitutions; single-quoted prose stays inert."""
    quote, index = "", 0
    while index < len(script):
        char = script[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if char == "'" and quote != '"':
            quote = "" if quote == "'" else "'"
        elif char == '"' and quote != "'":
            quote = "" if quote == '"' else '"'
        elif quote != "'" and (script.startswith("$(", index) or char == "`"):
            tick = char == "`"
            start = index + (1 if tick else 2)
            depth, cursor = 1, start
            while cursor < len(script):
                if script[cursor] == "\\":
                    cursor += 2
                    continue
                if tick and script[cursor] == "`":
                    break
                if not tick:
                    depth += (script[cursor] == "(") - (script[cursor] == ")")
                    if depth == 0:
                        break
                cursor += 1
            yield script[start:cursor]
            index = cursor
        index += 1


def runtime_in_shell(script, tokens=NODE_RUNTIMES, variables=None, depth=0):
    if depth > 12:
        return "<dynamic-shell-command>"
    variables = dict(variables or {})
    script = without_shell_comments(script)
    for nested in _substitutions(script):
        hit = runtime_in_shell(nested, tokens, variables, depth + 1)
        if hit:
            return hit
    try:
        commands = shell_commands(script)
    except ValueError:
        return "<unparsed-shell-command>"
    for command in commands:
        while command and command[0] in {"if", "then", "elif", "else", "do", "!", "{", "}"}:
            command = command[1:]
        while command and ASSIGNMENT.match(command[0]):
            name, value = command.pop(0).split("=", 1)
            variables[name] = value
        if command and command[0] in {"export", "local", "readonly"}:
            for argument in command[1:]:
                if ASSIGNMENT.match(argument):
                    name, value = argument.split("=", 1)
                    variables[name] = value
            continue
        hit = runtime_in_argv(command, tokens, variables, depth + 1)
        if hit:
            return hit
    return None


def runtime_in_argv(argv, tokens=NODE_RUNTIMES, variables=None, depth=0):
    if not argv:
        return None
    if depth > 12:
        return "<dynamic-shell-command>"
    variables = variables or {}
    executable = str(argv[0])
    executable = re.sub(r"\$(?:\{(\w+)\}|(\w+))",
                        lambda m: variables.get(m[1] or m[2], m[0]), executable)
    if "$" in executable or "`" in executable:
        return "<environment-derived-command>"
    base = os.path.basename(executable).casefold().removesuffix(".exe")
    if base in {t.casefold() for t in tokens} or executable.endswith("dist/index.js"):
        return base
    if base == "command" and any(arg in {"-v", "-V"} for arg in argv[1:]):
        return None  # executable lookup does not execute the queried command
    if base == "timeout":
        rest = list(argv[1:])
        while rest and rest[0].startswith("-"):
            option = rest.pop(0)
            if option in {"-k", "--kill-after", "-s", "--signal"} and rest:
                rest.pop(0)
        return runtime_in_argv(rest[1:], tokens, variables, depth + 1)
    if base in {"exec", "command", "nohup", "sudo", "env", "nice", "setsid", "time", "stdbuf"}:
        rest = list(argv[1:])
        while rest and (rest[0].startswith("-") or ASSIGNMENT.match(rest[0])):
            option = rest.pop(0)
            if option in {"-u", "--unset", "-C", "--chdir", "-S", "--split-string", "-a", "-n", "-i", "-o", "-e"} and rest:
                value = rest.pop(0)
                if option in {"-S", "--split-string"}:
                    rest = shlex.split(value) + rest
        return runtime_in_argv(rest, tokens, variables, depth + 1)
    if base in SHELLS:
        for index, argument in enumerate(argv[1:], 1):
            if argument.startswith("-") and "c" in argument[1:] and index + 1 < len(argv):
                return runtime_in_shell(argv[index + 1], tokens, variables, depth + 1)
    if base == "eval":
        return runtime_in_shell(" ".join(argv[1:]), tokens, variables, depth + 1)
    if base in {"python", "python3"} and "-c" in argv:
        index = argv.index("-c") + 1
        if index < len(argv):
            try:
                tree = ast.parse(argv[index])
            except SyntaxError:
                return "<unparsed-interpreter-command>"
            for call in ast.walk(tree):
                if not isinstance(call, ast.Call) or not call.args:
                    continue
                name = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", "")
                if name not in {"run", "Popen", "call", "check_call", "check_output", "system", "popen", "execv", "execve", "execl"}:
                    continue
                try:
                    target = ast.literal_eval(call.args[0])
                except (ValueError, TypeError):
                    return "<dynamic-interpreter-command>"
                hit = runtime_in_argv(target, tokens, variables, depth + 1) if isinstance(target, list) else runtime_in_shell(str(target), tokens, variables, depth + 1)
                if hit:
                    return hit
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--argv", required=True)
    args = parser.parse_args()
    found = runtime_in_argv(json.loads(args.argv))
    if found:
        print("source runtime command: " + found)
    raise SystemExit(1 if found else 0)
