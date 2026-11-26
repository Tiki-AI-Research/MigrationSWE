"""Inspect the action's final launcher without executing candidate code.

Docker stages retain file identities through COPY, and shell wrappers are
followed by exact executable paths. Unknown launchers do not count as evidence
that Go runs. The same inspector supplies source-runtime evidence to hardgate.
Only runtime entry chains are scanned: unused scripts and build-time tools do
not imply that migrated production responsibilities use Node.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import posixpath
from pathlib import Path
import re
import shlex

if __package__:
    from .shell_syntax import Word, ShellSyntaxError, match_pattern, lex as shell_words, parse as shell_program, programs as shell_programs
else:
    from shell_syntax import Word, ShellSyntaxError, match_pattern, lex as shell_words, parse as shell_program, programs as shell_programs


NODE = re.compile(r"^(?:node(?:js|\d+)?|ts-node|tsx|deno|bun|npm|npx|pnpm|yarn)(?:\.exe|\.cmd)?$", re.I)
SPECIAL_BUILTINS = frozenset({'break', ':', 'continue', '.', 'eval', 'exec', 'exit', 'export',
                              'readonly', 'return', 'set', 'shift', 'times', 'trap', 'unset'})


class Expansion(str):
    """An expanded word and the fields produced by unquoted expansions."""


class Unknown(Expansion):
    """A symbolic shell value, distinct from literal text containing '$'."""


class Unset(str):
    """A variable explicitly removed by the launcher, rather than unknown env."""


class ParameterLiteral(Expansion):
    """Literal text inside a double-quoted parameter operand."""


def command_query(words):
    """Whether command's options request a lookup instead of execution."""
    if not words or words[0] != "command":
        return False
    for word in words[1:]:
        if word == "--" or not word.startswith("-") or word == "-":
            break
        if set(word[1:]) <= set("pvV") and set(word[1:]) & set("vV"):
            return True
    return False


def shell_fields(fragments, ifs=' \t\n'):
    """Split only unquoted expansion characters, retaining quoted empties."""
    characters = []
    for value, split in fragments:
        characters.extend((char, split) for char in value)
        if not value and not split:
            characters.append((None, False))
    fields, current, present, index = [], '', False, 0
    whitespace = set(ifs) & set(' \t\n')
    while index < len(characters):
        char, split = characters[index]
        if char is None:
            present = True
            index += 1
        elif not split or char not in ifs:
            current += char
            present = True
            index += 1
        else:
            nonwhite = char not in whitespace
            index += 1
            if not nonwhite:
                while index < len(characters) and characters[index][1] and characters[index][0] in whitespace:
                    index += 1
                if index < len(characters) and characters[index][1] and characters[index][0] in ifs:
                    nonwhite = True
                    index += 1
            while index < len(characters) and characters[index][1] and characters[index][0] in whitespace:
                index += 1
            if present or nonwhite:
                fields.append(current)
            current, present = '', False
    if present:
        fields.append(current)
    return fields


def printf_output(fmt, arguments):
    """Evaluate supported POSIX printf forms without running a shell.

    The format is reused until arguments are consumed. An unsupported format
    stays unknown; it must never produce a fabricated executable path.
    """
    def escapes(value, *, argument=False):
        output, index = [], 0
        while index < len(value):
            char = value[index]
            index += 1
            if char != "\\":
                output.append(char)
                continue
            if index == len(value):
                raise ValueError('Incomplete printf escape')
            char = value[index]
            index += 1
            if char == 'c' and argument:
                return ''.join(output), True
            if char in "abfnrtv\\":
                output.append(dict(zip("abfnrtv\\", "\a\b\f\n\r\t\v\\"))[char])
            elif char in "01234567":
                # %b uses \0ddd; the format uses \ddd.
                start = index if argument and char == '0' else index - 1
                match = re.match(r'[0-7]{0,3}', value[start:])
                digits = match[0]
                index = start + len(digits)
                escaped = chr(int(digits or '0', 8) % 256)
                output.append('%%' if not argument and escaped == '%' else escaped)
            else:
                raise ValueError('Unsupported printf escape')
        return ''.join(output), False

    try:
        fmt, _ = escapes(fmt)
        result, consumed = [], 0
        while True:
            before, cursor = consumed, 0
            while cursor < len(fmt):
                if fmt[cursor] != '%':
                    result.append(fmt[cursor])
                    cursor += 1
                    continue
                match = re.match(r'%(?:%|([-+ #0]*)([0-9]*)(?:\.([0-9]+))?([sbcdioxXu]))', fmt[cursor:])
                if not match:
                    return None
                cursor += len(match[0])
                if match[0] == '%%':
                    result.append('%')
                    continue
                flags, width, precision, kind = match.groups()
                if max(int(width or '0'), int(precision or '0')) > 4096:
                    return None
                arg = arguments[consumed] if consumed < len(arguments) else ''
                consumed += 1
                if kind in {'s', 'b', 'c'}:
                    if flags.strip('-') or kind != 's' and (width or precision):
                        return None
                    if (width or precision) and not arg.isascii():
                        return None
                    value, stop = escapes(arg, argument=True) if kind == 'b' else ((arg[:1] or '\0') if kind == 'c' else arg, False)
                    if kind == 's' and precision is not None:
                        value = value[:int(precision)]
                    if width:
                        value = value.ljust(int(width)) if '-' in flags else value.rjust(int(width))
                    result.append(value)
                    if stop:
                        return ''.join(result)
                else:
                    # Reject rather than invent values for shell-specific
                    # numeric syntax or unsigned overflow.
                    if not re.fullmatch(r'[+-]?[0-9]*', arg):
                        return None
                    number = int(arg or '0', 8 if len(arg.lstrip('+-')) > 1 and arg.lstrip('+-').startswith('0') else 10)
                    if number < 0 and kind in 'ouxXu':
                        return None
                    digits = format(abs(number), kind if kind in 'oxX' else 'd')
                    if precision is not None:
                        digits = '' if int(precision) == 0 and number == 0 else digits.zfill(int(precision))
                    sign = '-' if number < 0 else '+' if '+' in flags and kind in 'di' else ' ' if ' ' in flags and kind in 'di' else ''
                    prefix = ('0' if not digits.startswith('0') else '') if '#' in flags and kind == 'o' else '0' + kind if '#' in flags and kind in 'xX' and number else ''
                    padding = max(0, int(width or '0') - len(sign + prefix + digits))
                    if '0' in flags and '-' not in flags and precision is None:
                        value = sign + prefix + '0' * padding + digits
                    else:
                        value = sign + prefix + digits
                        value = value + ' ' * padding if '-' in flags else ' ' * padding + value
                    result.append(value)
            if consumed >= len(arguments) or consumed == before:
                return ''.join(result)
            if len(result) > 65536:
                return None
    except (ValueError, OverflowError):
        return None


def scalar(value):
    value = value.strip()
    if value.startswith('"'):
        try:
            return json.loads(value)
        except ValueError:
            pass
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value.split(" #", 1)[0].strip()


def shell_commands(text):
    result, row = [], []
    try:
        words = shell_words(text)
    except ValueError:
        return []
    for word in words:
        if not isinstance(word, Word) and word in {";", "&", "|", "&&", "||", "\n"}:
            if row:
                result.append(row)
                row = []
        else:
            row.append(word)
    if row:
        result.append(row)
    return result


def expand(word, variables):
    def sub(match):
        name = match[1] or match[3]
        fallback = match[2] or ""
        return variables.get(name, fallback if match[2] is not None else "$" + name)
    return re.sub(r"\$\{(\w+)(?::-([^}]*))?\}|\$(\w+)", sub, word)


def path_at(value, cwd):
    return posixpath.normpath(value if value.startswith("/") else posixpath.join(cwd, value))


@dataclass
class Stage:
    cwd: str = "/"
    # (image path, kind, repository-relative source). Directories map a prefix.
    files: dict[str, tuple[str, str]] = field(default_factory=dict)
    entry: str | None = None
    cmd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    directories: set[str] = field(default_factory=lambda: {"/"})
    sh_dialect: str = 'sh'

    def lookup(self, value):
        value = path_at(value, self.cwd)
        if value in self.files:
            return self.files[value]
        for prefix in sorted(self.files, key=len, reverse=True):
            kind, source = self.files[prefix]
            if kind == "directory" and value.startswith(prefix.rstrip("/") + "/"):
                return "source", posixpath.join(source, value[len(prefix):].lstrip("/"))
        return None


def local_file(root, relative):
    candidate = root / relative
    try:
        resolved = candidate.resolve()
        if resolved.is_relative_to(root.resolve()) and resolved.is_file():
            return resolved
    except (OSError, RuntimeError):
        pass
    return None


def go_command(words, *, allow_assignments=True):
    words = list(words)
    while words and (allow_assignments and re.match(r"^\w+=", words[0]) or words[0] == "env"):
        words.pop(0)
    if not words or posixpath.basename(words[0]) != "go":
        return None
    i = 1
    while i < len(words) and words[i].startswith("-"):
        i += 2 if words[i] == "-C" else 1
    return (words[i], words[i + 1:]) if i < len(words) else None


def build_outputs(words, cwd, root):
    go = go_command(words)
    if not go or go[0] != "build":
        return []
    if "-C" in words:
        index = words.index("-C")
        if index + 1 < len(words):
            cwd = path_at(words[index + 1], cwd)
    output, packages = None, []
    args, index = go[1], 0
    while index < len(args):
        arg = args[index]
        if arg in {"-o", "-ldflags", "-gcflags", "-asmflags", "-tags", "-modfile", "-p"} and index + 1 < len(args):
            if arg == "-o":
                output = args[index + 1]
            index += 2
            continue
        if arg.startswith("-o="):
            output = arg[3:]
        elif not arg.startswith("-"):
            packages.append(arg)
        index += 1
    if not output or output.endswith("/"):
        if len(packages) > 1 or any("..." in package or package.endswith(".a") for package in packages):
            return []
        package = packages[0] if packages else "."
        if package.endswith(".go"):
            name = posixpath.basename(package)[:-3]
        elif package not in {".", "./"}:
            name = posixpath.basename(package.rstrip("/"))
        else:
            module = local_file(root, "go.mod")
            match = re.search(r"(?m)^module\s+(\S+)", module.read_text()) if module else None
            name = match[1].rstrip("/").split("/")[-1] if match else posixpath.basename(cwd)
        output = posixpath.join(output, name) if output else name
    return [path_at(output, cwd)]


def go_package(words, stage, root):
    """Resolve the package of a Go executable through Docker COPY mappings.

    The output's package identity follows the binary into later stages. This
    lets the behavior fixture select the real action among multiple commands,
    including a root package built with ``go build -o /out/action .``.
    """
    go = go_command(words)
    if not go or go[0] not in {"build", "run"}:
        return ""
    cwd = stage.cwd
    if "-C" in words:
        index = words.index("-C")
        if index + 1 < len(words):
            cwd = path_at(words[index + 1], cwd)
    packages, index = [], 0
    args = go[1]
    value_flags = {"-o", "-ldflags", "-gcflags", "-asmflags", "-tags", "-modfile",
                   "-p", "-overlay", "-mod", "-coverpkg", "-covermode"}
    while index < len(args):
        arg = args[index]
        if arg in value_flags:
            index += 2
            continue
        if not arg.startswith("-"):
            packages.append(arg)
            if go[0] == "run" and not arg.endswith(".go"):
                break
        index += 1
    if not packages:
        packages = ["."]
    if all(package.endswith(".go") for package in packages):
        packages = list({posixpath.dirname(package) or "." for package in packages})
    if len(packages) != 1 or "..." in packages[0]:
        return ""
    package = packages[0]
    if "$" in package:
        return ""
    if package.startswith((".", "/")):
        target = path_at(package, cwd)
        found = stage.lookup(target)
        if found and found[0] in {"source", "directory"}:
            return posixpath.normpath(found[1])
        # COPY main.go ./ and COPY internal ./internal need not create a
        # directory-prefix mapping. The copied module still anchors relative
        # package paths in this stage.
        parent = target
        while True:
            module = stage.lookup(posixpath.join(parent, "go.mod"))
            if module and module[0] == "source":
                return posixpath.normpath(posixpath.join(posixpath.dirname(module[1]),
                                                        posixpath.relpath(target, parent)))
            if parent == "/":
                break
            parent = posixpath.dirname(parent)
    else:
        module = local_file(root, "go.mod")
        match = re.search(r"(?m)^module\s+(\S+)", module.read_text()) if module else None
        if match and (package == match[1] or package.startswith(match[1] + "/")):
            return package[len(match[1]):].lstrip("/") or "."
    return ""


def recipe(root, text):
    stages, names, current = [], {}, None
    logical = re.sub(r"\\\r?\n\s*", " ", text)
    for line in logical.splitlines():
        match = re.match(r"^\s*([A-Za-z]+)\s+(.*?)\s*$", line)
        if not match:
            continue
        kind, value = match[1].upper(), match[2]
        if kind == "FROM":
            parts = shlex.split(value)
            source = next((p for p in parts if not p.startswith("--")), "")
            parent = names.get(source)
            current = Stage(parent.cwd, dict(parent.files), parent.entry, parent.cmd, dict(parent.env),
                            set(parent.directories), parent.sh_dialect) if parent else Stage()
            if not parent:
                image = source.rsplit('/', 1)[-1].lower()
                if 'alpine' in image or image.startswith('busybox'):
                    current.sh_dialect = 'ash'
                elif image.startswith(('debian', 'ubuntu')):
                    current.sh_dialect = 'dash'
            names[str(len(stages))] = current
            if len(parts) >= 3 and parts[-2].upper() == "AS":
                names[parts[-1]] = current
            stages.append(current)
        elif current is None:
            continue
        elif kind == "WORKDIR":
            current.cwd = path_at(expand(value, current.env), current.cwd)
            current.directories.add(current.cwd)
        elif kind == "ENV":
            env_words = shlex.split(value)
            if env_words and "=" not in env_words[0] and len(env_words) > 1:
                current.env[env_words[0]] = expand(" ".join(env_words[1:]), current.env)
            else:
                for word in env_words:
                    if "=" in word:
                        name, val = word.split("=", 1)
                        current.env[name] = expand(val, current.env)
        elif kind == "RUN":
            cwd = current.cwd
            for words in shell_commands(value):
                words = [expand(w, current.env) for w in words]
                if words[:1] == ["cd"] and len(words) == 2:
                    cwd = path_at(words[1], cwd)
                for output in build_outputs(words, cwd, root):
                    build_stage = Stage(cwd, dict(current.files))
                    current.files[output] = ("go", go_package(words, build_stage, root))
        elif kind in {"COPY", "ADD"}:
            source_stage = None
            while value.startswith("--"):
                flag, _, value = value.partition(" ")
                if flag.startswith("--from="):
                    source_stage = names.get(flag[7:])
            try:
                args = json.loads(value) if value.startswith("[") else shlex.split(value)
            except ValueError:
                continue
            if len(args) < 2:
                continue
            destination = expand(args[-1], current.env)
            for source in args[:-1]:
                source = expand(source, current.env)
                source_path = path_at(source, "/") if source_stage else source
                file = None if source_stage else local_file(root, source)
                if source_stage:
                    identity = source_stage.lookup(source_path)
                    is_directory = (source_path in source_stage.directories
                                    or identity is not None and identity[0] == "directory"
                                    or any(path.startswith(source_path.rstrip("/") + "/") for path in source_stage.files))
                else:
                    folder = (root / source).resolve()
                    is_directory = folder.is_relative_to(root.resolve()) and folder.is_dir()
                dest = path_at(destination, current.cwd)
                if not is_directory and (destination.endswith("/") or dest in current.directories):
                    dest = posixpath.join(dest, posixpath.basename(source.rstrip("/")))
                if is_directory:
                    current.directories.add(dest)
                if source_stage:
                    found = source_stage.lookup(source_path)
                    if found:
                        current.files[dest] = found
                    for path, identity in list(source_stage.files.items()):
                        if path.startswith(source_path.rstrip("/") + "/"):
                            current.files[posixpath.join(dest, path[len(source_path):].lstrip("/"))] = identity
                elif file:
                    current.files[dest] = ("source", source)
                elif is_directory:
                    current.files[dest] = ("directory", source)
                    for target in list(current.files):
                        if target.startswith(dest.rstrip("/") + "/"):
                            relative = posixpath.join(source, target[len(dest):].lstrip("/"))
                            if local_file(root, relative):
                                current.files[target] = ("source", relative)
        elif kind == "ENTRYPOINT":
            current.entry = value
            current.cmd = None
        elif kind == "CMD":
            current.cmd = value
    return stages[-1] if stages else Stage()


def argv(value):
    if not value:
        return []
    try:
        return json.loads(value) if value.lstrip().startswith("[") else ["/bin/sh", "-c", value]
    except ValueError:
        return []


def inspect_action(root, action_text):
    root = Path(root)
    result = {"go": False, "go_packages": [], "source_runtime_hits": [], "unresolved": []}
    block = re.search(r"(?ms)^runs:\s*\n(.*?)(?=^\S|\Z)", action_text)
    if not block:
        result["unresolved"].append("Action has no runs entry")
        return result
    block = block[1]
    using = re.search(r"(?m)^\s*using:\s*(.+)$", block)
    using = scalar(using[1]) if using else ""
    visited = set()
    incomplete = False

    def process_arguments(variables, name, arguments):
        # A new interpreter receives its own argv. Subshells and command
        # substitutions use process_child instead and retain the caller's
        # positional parameters, as do sourced scripts without new arguments.
        child = {key: value for key, value in variables.items() if key != '@' and not key.isdigit()}
        child.update({'0': name, '@': arguments,
                      **{str(index): value for index, value in enumerate(arguments, 1)}})
        return child

    def shell_options(words, initial=None, *, invocation=False, dialect=None, startup=None, changed=None):
        options = dict(initial or {'errexit': False, 'pipefail': False, 'posix': False, 'noexec': False, 'nounset': False, 'allexport': False, 'privileged': False,
                                   'xtrace': False, 'verbose': False, 'noglob': False})
        index, command_string = 0, False
        while index < len(words) and words[index] not in {'--', '-'} and words[index].startswith(('-', '+')):
            option = words[index]
            index += 1
            if isinstance(option, Unknown):
                raise ValueError('Unknown shell option')
            enabled = option[0] == '-'
            if invocation and option in {'--noprofile', '--norc'}:
                if startup is not None:
                    startup[option[2:]] = True
                continue
            if invocation and dialect == 'bash' and option in {'--posix', '--login'}:
                if option == '--posix':
                    options['posix'] = True
                elif startup is not None:
                    startup['login'] = True
                continue
            for offset, flag in enumerate(option[1:]):
                if flag == 'o':
                    if offset != len(option) - 2:
                        raise ValueError('Unsupported shell option query')
                    if index >= len(words):
                        if invocation:
                            raise ValueError('Missing shell option name')
                        # set -o / +o prints options without changing them.
                        return options, index, command_string
                    name = words[index]
                    index += 1
                    if name not in options or isinstance(name, Unknown):
                        raise ValueError('Unsupported shell option ' + name)
                    if name == 'privileged' and dialect == 'ash':
                        raise ValueError('Unsupported shell option privileged')
                    if name == 'posix' and dialect is not None and dialect != 'bash':
                        raise ValueError('Unsupported shell option posix')
                    options[name] = enabled
                    if changed is not None:
                        changed.add(name)
                elif flag in {'a', 'e', 'n', 'u', 'p', 'x', 'v', 'f'}:
                    if flag == 'p' and dialect not in {'bash', 'dash'}:
                        raise ValueError('Unsupported shell option p')
                    name = {'a': 'allexport', 'e': 'errexit', 'n': 'noexec', 'u': 'nounset', 'p': 'privileged',
                            'x': 'xtrace', 'v': 'verbose', 'f': 'noglob'}[flag]
                    options[name] = enabled
                    if changed is not None:
                        changed.add(name)
                elif invocation and dialect == 'bash' and flag == 'l' and startup is not None:
                    startup['login'] = enabled
                elif invocation and flag == 'c' and enabled:
                    command_string = True
                elif flag not in 'auxvf':
                    raise ValueError('Unsupported shell option ' + flag)
        return options, index, command_string

    def exec_arguments(words, dialect):
        """Parse the calling shell's exec builtin without losing argv[0]."""
        words, clear, name, login = list(words), False, None, False
        if dialect == 'dash':
            return words, clear, name, login
        while words and words[0].startswith('-') and words[0] != '-':
            option = words.pop(0)
            if isinstance(option, Unknown):
                raise ValueError('Unknown exec option')
            if option == '--':
                break
            index = 1
            while index < len(option):
                flag = option[index]
                index += 1
                if flag == 'a' and dialect in {'bash', 'ash'}:
                    if index < len(option):
                        name = option[index:]
                    elif words:
                        name = words.pop(0)
                    else:
                        raise ValueError('Missing exec argv[0]')
                    index = len(option)
                elif dialect == 'bash' and flag in {'c', 'l'}:
                    clear = clear or flag == 'c'
                    login = login or flag == 'l'
                else:
                    raise ValueError('Unsupported exec option ' + flag)
        return words, clear, name, login

    def environment_arguments(words, variables, closed, stage):
        """Apply env options before assignments, without changing the caller."""
        words, variables = list(words), dict(variables)
        stage = Stage(stage.cwd, dict(stage.files), env=dict(stage.env),
                      directories=set(stage.directories), sh_dialect=stage.sh_dialect)
        while words and words[0].startswith('-'):
            option = words.pop(0)
            if isinstance(option, Unknown):
                raise ValueError('Unknown env option')
            if option == '--':
                break
            if option in {'-', '-i', '--ignore-environment'}:
                variables.clear()
                closed = True
            elif option in {'-u', '--unset', '-C', '--chdir', '-S', '--split-string'}:
                if not words or isinstance(words[0], Unknown):
                    raise ValueError('Missing or unknown env option operand')
                operand = words.pop(0)
                if option in {'-u', '--unset'}:
                    variables[operand] = Unset('')
                elif option in {'-C', '--chdir'}:
                    stage.cwd = path_at(operand, stage.cwd)
                else:
                    words = shlex.split(operand) + words
            elif option.startswith(('--unset=', '-u')):
                name = option.split('=', 1)[1] if option.startswith('--unset=') else option[2:]
                variables[name] = Unset('')
            elif option.startswith(('--chdir=', '-C')):
                operand = option.split('=', 1)[1] if option.startswith('--chdir=') else option[2:]
                stage.cwd = path_at(operand, stage.cwd)
            elif option.startswith(('--split-string=', '-S')):
                operand = option.split('=', 1)[1] if option.startswith('--split-string=') else option[2:]
                words = shlex.split(operand) + words
            else:
                raise ValueError('Unsupported env option ' + option)
        while words and re.match(r'^[A-Za-z_]\w*=', words[0]):
            word = words.pop(0)
            name, value = word.split('=', 1)
            variables[name] = Unknown(value) if isinstance(word, Unknown) else value
        return words, variables, closed, stage

    def executable(words, stage, variables, source, depth=0, script_mode=False, script_shell=None,
                   fallback_shell='sh', script_options=None, fallback_options=None, status_out=None,
                   environment_closed=False, lookup_path=None, script_startup=None):
        nonlocal incomplete
        if depth > 64:
            incomplete = True
            result["unresolved"].append("Launcher nesting limit reached")
            return False
        if not words:
            return False
        # Shell words are expanded by the interpreter before dispatch. JSON
        # Docker argv and values passed to exec/env are already literal argv.
        words = list(words)
        argv0 = None
        while words and posixpath.basename(words[0]) in {"exec", "command", "env"}:
            if command_query(words):
                return False
            wrapper = posixpath.basename(words.pop(0))
            if wrapper == 'env':
                # env executes through execvp: its ENOEXEC fallback is /bin/sh,
                # independent of the shell which launched env.
                fallback_shell = 'sh'
                fallback_options = None
                argv0 = None
                try:
                    words, variables, environment_closed, stage = environment_arguments(words, variables, environment_closed, stage)
                    lookup_path = variables.get('PATH', Unset('') if environment_closed else None)
                except ValueError as error:
                    incomplete = True
                    result['unresolved'].append(str(error))
                    return False
                continue
            if wrapper == 'exec':
                selected_dialect = stage.sh_dialect if fallback_shell == 'sh' else fallback_shell
                try:
                    words, clear, argv0, login = exec_arguments(words, selected_dialect)
                except ValueError as error:
                    result['unresolved'].append(str(error))
                    if status_out is not None:
                        status_out[:] = [(False, '2')]
                    return False
                if clear:
                    variables, environment_closed = {}, True
                if login and words:
                    argv0 = '-' + (argv0 if argv0 is not None else words[0])
            else:
                while words and words[0] in {'-p', '--'}:
                    if words.pop(0) == '--':
                        break
        if not words:
            return False
        command = posixpath.basename(words[0])
        if command in {".", "source"} and len(words) > 1:
            script = words[1:]
            if "/" not in script[0]:
                script[0] = "./" + script[0]
            return executable(script, stage, variables, source, depth + 1, script_mode=True,
                              fallback_shell=fallback_shell, status_out=status_out, environment_closed=environment_closed)
        if NODE.fullmatch(command):
            result["source_runtime_hits"].append({"path": source, "sink": "action-entry", "token": command,
                                                  "evidence": shlex.join(words)})
            return False
        go = go_command(words, allow_assignments=False)
        if go and go[0] == "run":
            package = go_package(words, stage, root)
            if package and package not in result["go_packages"]:
                result["go_packages"].append(package)
            return True
        if command in {"sh", "bash", "dash", "ash"}:
            startup = {'name': argv0 if argv0 is not None else words[0]}
            try:
                selected_dialect = stage.sh_dialect if command == 'sh' else command
                options, index, command_string = shell_options(words[1:], invocation=True, dialect=selected_dialect, startup=startup)
            except ValueError as error:
                incomplete = True
                result['unresolved'].append(str(error))
                return False
            if (command == 'dash' or command == 'sh' and stage.sh_dialect == 'dash') and options['pipefail']:
                if status_out is not None:
                    status_out[:] = [(False, '2')]
                return False
            i = index + 1
            if i < len(words) and words[i] == '--':
                i += 1
            if command_string:
                child = process_arguments(variables, words[i + 1] if len(words) > i + 1 else startup['name'], words[i + 2:])
                return shell(words[i] if len(words) > i else "", stage, child, source, depth + 1,
                             dialect=command, options=options, status_out=status_out, command_string=True,
                             environment_closed=environment_closed, startup=startup)
            args = words[i:]
            if args and "/" not in args[0]:
                args[0] = "./" + args[0]
            return executable(args, stage, variables, source, depth + 1, script_mode=True,
                              script_shell=command, script_options=options, status_out=status_out,
                              environment_closed=environment_closed, script_startup=startup) if args else False
        if "/" in words[0]:
            found = stage.lookup(words[0])
        else:
            search = lookup_path if lookup_path is not None else variables.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
            if isinstance(search, Unset):
                search = '/usr/local/bin:/bin:/usr/bin' if stage.sh_dialect == 'ash' else '/bin:/usr/bin'
            found = next((stage.lookup(posixpath.join(folder, words[0])) for folder in search.split(":")
                          if stage.lookup(posixpath.join(folder, words[0]))), None)
        if found and found[0] == "go":
            if found[1] and found[1] not in result["go_packages"]:
                result["go_packages"].append(found[1])
            return True
        if not found and command.startswith('-') and '/' not in words[0]:
            if status_out is not None:
                status_out[:] = [(False, '127')]
            return False
        if found and found[0] == "source":
            file = local_file(root, found[1])
            if file and (script_mode or file.suffix in {".sh", ".bash"} or file.read_bytes().startswith(b"#!")):
                key = (str(file), tuple(words[1:]))
                if key in visited:
                    incomplete = True
                    result["unresolved"].append("Recursive script launcher")
                    return False
                visited.add(key)
                child = process_arguments(variables, words[0], words[1:])
                try:
                    content = file.read_text()
                    first = content.splitlines()[0] if content else ""
                    interpreter_shell = script_shell or fallback_shell
                    options = script_options if script_shell else fallback_options
                    startup = script_startup or ({'name': 'bash'} if fallback_shell == 'bash' else {'fresh': False})
                    if first.startswith("#!") and script_shell is None:
                        try:
                            interpreter = shlex.split(first[2:])
                        except ValueError as error:
                            incomplete = True
                            result['unresolved'].append(str(error))
                            return False
                        selected = list(interpreter)
                        if selected and posixpath.basename(selected[0]) == "env":
                            try:
                                selected, variables, environment_closed, stage = environment_arguments(
                                    selected[1:], variables, environment_closed, stage)
                            except ValueError as error:
                                incomplete = True
                                result['unresolved'].append(str(error))
                                return False
                            child = process_arguments(variables, words[0], words[1:])
                        if selected and NODE.fullmatch(posixpath.basename(selected[0])):
                            return executable(interpreter + [words[0]] + words[1:], stage, child,
                                              str(file.relative_to(root)), depth + 1, status_out=status_out,
                                              environment_closed=environment_closed)
                        if selected and posixpath.basename(selected[0]) in {'sh', 'bash', 'dash', 'ash'}:
                            interpreter_shell = posixpath.basename(selected[0])
                            startup = {'name': selected[0]}
                            try:
                                options = shell_options(selected[1:], invocation=True, dialect=interpreter_shell, startup=startup)[0]
                            except ValueError as error:
                                incomplete = True
                                result['unresolved'].append(str(error))
                                return False
                    return shell(content, stage, child, str(file.relative_to(root)), depth + 1,
                                 dialect=interpreter_shell, options=options, status_out=status_out,
                                 environment_closed=environment_closed, startup=startup)
                finally:
                    visited.remove(key)
        if not found and command == 'time' and words[0] in {'time', '/bin/time', '/usr/bin/time'}:
            # Here time is an external executable, including a coprocess's
            # simple command. It cannot invoke caller functions or builtins.
            arguments = words[1:]
            if arguments[:1] == ['-p']:
                arguments = arguments[1:]
            if arguments[:1] == ['--']:
                arguments = arguments[1:]
            elif arguments and arguments[0].startswith('-'):
                incomplete = True
                result['unresolved'].append('Unsupported external time option')
                return False
            if not arguments or arguments[0] == ':':
                if status_out is not None:
                    status_out[:] = [(False, '125' if not arguments else '127')]
                return False
            return executable(arguments, stage, variables, source, depth + 1,
                              fallback_shell='sh', status_out=status_out,
                              environment_closed=environment_closed, lookup_path=lookup_path)
        if status_out is not None and command in {'true', 'false', ':'}:
            status_out[:] = [(command != 'false', '1' if command == 'false' else '0')]
        if command not in {"echo", "printf", "set", "export", "cd", "chmod", "mkdir", "true", "false", "test", "[", "trap", "pwd", "dirname", "mktemp", ":"}:
            result["unresolved"].append(shlex.join(words))
        return False

    def shell(text, stage, variables, source, depth=0, *, dialect='sh', options=None, status_out=None, command_string=False,
              environment_closed=False, startup=None):
        def clone(state):
            current = state["stage"]
            return dict(state, vars=dict(state["vars"]), functions=dict(state["functions"]),
                        options=dict(state['options']),
                        readonly=set(state['readonly']),
                        exported=set(state['exported']),
                        stage=Stage(current.cwd, dict(current.files), env=dict(current.env),
                                    directories=set(current.directories), sh_dialect=current.sh_dialect),
                        output=None if state["output"] is None else list(state["output"]),
                        locals=[dict(frame) for frame in state["locals"]])

        def variable(state, name):
            return state['vars'].get(name, Unset('') if state['environment_closed'] else None)

        def assigned(state, name):
            if state['options']['allexport']:
                state['exported'].add(name)

        def refresh_shellopts(state):
            if dialect == 'bash':
                enabled = {'braceexpand', 'hashall', 'interactive-comments'}
                enabled.update(name for name, value in state['options'].items() if value)
                state['vars']['SHELLOPTS'] = ':'.join(sorted(enabled))

        def environment_value(state, name):
            value = state['vars'][name]
            if name in state['exported'] and not isinstance(value, Unset):
                return value
            if dialect == 'bash':
                # An unexported/unset local binding hides the shell value,
                # while Bash's process environment retains an exported outer
                # binding. ash/dash keep the local export flag on unset.
                for frame in reversed(state['locals']):
                    previous, exported = frame.get(name, (None, False))
                    if exported and previous is not None and not isinstance(previous, Unset):
                        return previous
            return Unset('')

        def set_status(state, status, raw=None):
            state['status'], state['raw_status'] = status, raw

        def process_status(state):
            # Shell functions retain their return value internally; a process
            # boundary exposes its low eight bits. Do not conflate the two.
            raw = state.get('raw_status')
            return int(raw[-8:]) % 256 == 0 if raw is not None and not isinstance(raw, Unknown) else state['status']

        def status_value(state, *, process=False):
            raw = state.get('raw_status')
            if isinstance(raw, Unknown):
                return raw
            if raw is not None:
                return str(int(raw[-8:]) % 256) if process else raw.lstrip('0') or '0'
            return '0' if state['status'] is True else '1' if state['status'] is False else Unknown('$?')

        def unknown(state, reason):
            nonlocal incomplete
            # A later Go command in a parent interpreter cannot establish
            # that an unanalysed child avoided the source runtime.
            incomplete = True
            result["unresolved"].append(reason)
            state.update(go=False, done="unsupported", status=None, raw_status=None, output=None)
            return state

        def parse_program(value, state):
            try:
                return shell_program(value, dialect=dialect)
            except (ValueError, RecursionError) as error:
                unknown(state, str(error))
                return None

        def read_file(file, states, level, origin):
            read_command = False
            try:
                for program in shell_programs(file.read_text(), dialect=dialect):
                    read_command = True
                    states = evaluate(program, states, level, origin)
                    if all(state['done'] for state in states):
                        break
                if not read_command:
                    # Sourcing an entirely empty file succeeds, but an empty
                    # logical input line never changes the preceding status.
                    for state in states:
                        if not state['done']:
                            set_status(state, True)
            except ShellSyntaxError:
                for state in states:
                    if not state['done']:
                        set_status(state, False, '2')
                        if (state['options']['posix'] and not state.get('startup_file') or state['options']['errexit']
                                and not state['ignore_errexit'] and not state.get('startup_errexit_inhibited')):
                            state.update(done='exit', exit_reason='syntax')
            except (OSError, UnicodeError, ValueError, RecursionError) as error:
                for state in states:
                    if not state['done']:
                        unknown(state, str(error))
            return states

        def child_result(parent, child, *, restore_scope=True):
            combined = clone(parent)
            combined["stage"].files.update(child["stage"].files)
            combined["stage"].directories.update(child["stage"].directories)
            combined.update(go=parent["go"] or child["go"], output=child["output"], done=None)
            set_status(combined, process_status(child), status_value(child, process=True))
            if parent["done"] == "unsupported" or child["done"] == "unsupported":
                combined.update(go=False, done="unsupported")
            if not restore_scope:
                combined["vars"], combined["functions"] = child["vars"], child["functions"]
                combined['exported'] = set(child['exported'])
            return combined

        def process_child(state, *, substitution=False):
            child = clone(state)
            # Caught traps are reset in subshell environments. Bash also
            # clears -e in command substitutions unless POSIX mode is set.
            # BusyBox ash clears it too; dash retains it. Resolve /bin/sh
            # from known Docker base images without changing quote semantics.
            child.update(exit_trap=None, chld_trap=None, pending_chld=0, deferred_chld=0, in_chld_trap=False,
                         done=None, expansion_status='1' if dialect == 'bash' else '2',
                         readonly_status='1' if dialect == 'bash' and substitution else '2')
            clears_errexit = dialect in {'bash', 'ash'} or dialect == 'sh' and stage.sh_dialect == 'ash'
            if substitution and clears_errexit and not child['options']['posix']:
                child['options']['errexit'] = False
            # Bash clears enclosing loops in explicit subshells/pipelines,
            # but retains them in command substitutions. ash/dash retain both.
            if dialect == 'bash' and not substitution:
                child['loop_depth'] = 0
            return child

        def drain_chld(states, level, origin):
            """Bash dispatches queued CHLD traps after a foreground command.

            Caught handlers are reset in child processes, and children started
            by the handler wait until the next foreground child completes.
            Keep one snapshot for the children of a pipeline, even if the
            first invocation replaces or clears the registered handler.
            """
            finished = []
            for state in states:
                count, handler = state['pending_chld'], state['chld_trap']
                state['pending_chld'] = 0
                if not count or handler is None or state['in_chld_trap'] or state['done'] in {'exec', 'unsupported'}:
                    if handler is None:
                        state['deferred_chld'] = 0
                    finished.append(state)
                    continue
                count += state['deferred_chld']
                state['deferred_chld'] = 0
                if count > 128:
                    finished.append(unknown(state, 'CHLD trap dispatch limit reached'))
                    continue
                saved = state['done'], state['status'], state.get('raw_status'), state.get('exit_reason')
                state.update(done=None, in_chld_trap=True)
                outcomes = [state]
                for _ in range(count):
                    next_outcomes = []
                    for current in outcomes:
                        if current['done']:
                            next_outcomes.append(current)
                            continue
                        program = parse_program(handler, current)
                        next_outcomes.extend(evaluate(program, [current], level + 1, origin) if program else [current])
                    outcomes = next_outcomes
                for outcome in outcomes:
                    outcome.update(in_chld_trap=False, pending_chld=0)
                    if outcome['done'] is None:
                        outcome['done'], outcome['exit_reason'] = saved[0], saved[3]
                        set_status(outcome, saved[1], saved[2])
                    elif outcome['done'] == 'exit' and dialect == 'bash':
                        # Bash retains the foreground status even for an
                        # explicit exit from its CHLD handler.
                        set_status(outcome, saved[1], saved[2])
                    finished.append(outcome)
            return finished

        def queue_chld(state, count=1):
            if state['chld_trap'] is None:
                return
            if state['in_chld_trap'] and dialect != 'bash':
                # ash/dash immediately redispatch a still-caught CHLD when
                # its handler creates a child. A finite continuation cannot
                # be inferred; preserve actual child hits and stop analysis.
                unknown(state, 'Recursive CHLD dispatch outside Bash')
            else:
                state['deferred_chld' if state['in_chld_trap'] else 'pending_chld'] += count if dialect == 'bash' else 1

        def expansion_error(state, status=None):
            state.update(done='exit', exit_reason='expansion')
            set_status(state, False, status or state['expansion_status'])

        def finish_process(states, level, origin):
            finished = []
            for state in states:
                handler = state['exit_trap']
                if handler is None or state['options']['noexec'] or state['done'] in {'exec', 'unsupported'}:
                    finished.append(state)
                    continue
                saved = state['done'], state['status'], state.get('raw_status')
                # A handler runs once, in the exiting interpreter's scope.
                # Its ordinary last status does not replace the exit status;
                # an explicit exit or exec in the handler does.
                state.update(exit_trap=None, done=None)
                program = parse_program(handler, state)
                outcomes = evaluate(program, [state], level + 1, origin) if program else [state]
                for outcome in outcomes:
                    if outcome['done'] is None:
                        outcome['done'] = saved[0]
                        set_status(outcome, saved[1], saved[2])
                    finished.append(outcome)
            return finished

        def expand_word(word, state, level, origin, pattern=False, parameter=False):
            if not isinstance(word, Word):
                return word
            value, uncertain, field_groups, has_vector = [], False, [[]], False
            bash_at_operand = False
            quoted_bash_at_operand = False
            quoted_operand = parameter and dialect == 'bash' and getattr(word, 'parameter_quoted', False)
            for part_index, (kind, part) in enumerate(word.parts):
                if state['done']:
                    break
                offset = len(value)
                part_uncertain = False
                vector_groups = None
                part_has_vector = False
                if kind == "literal":
                    value.append(part)
                elif kind == "param":
                    length = part.startswith('#') and len(part) > 1
                    match = re.fullmatch(r"(\w+|[@*?#$!-])(?:(:?[+\-=?])([\s\S]*))?", part[1:] if length else part)
                    if not match:
                        if state['options']['nounset']:
                            unknown(state, 'Unsupported parameter expansion with nounset')
                            break
                        value.append("$" + part)
                        uncertain = True
                        field_groups[-1].append((Unknown('$' + part), part_index not in word.protected))
                        continue
                    name, operator, default = match.groups()
                    if name in state.get('coprocess_variables', set()):
                        unknown(state, 'Asynchronous coprocess variable ' + name)
                        break
                    current = variable(state, name)
                    if name == '#':
                        current = str(len(state['vars'].get('@', [])))
                    elif name == '@':
                        current = state['vars'].get('@', [])
                    elif name == '?':
                        current = status_value(state)
                    elif name == '*':
                        separator = state['vars'].get('IFS', ' \t\n')
                        if isinstance(separator, Unset):
                            separator = ' \t\n'
                        arguments = state['vars'].get('@', [])
                        joined = separator[:1].join(arguments)
                        current = Unknown(joined) if isinstance(separator, Unknown) or any(
                            isinstance(argument, Unknown) for argument in arguments) else joined
                    elif name == '-' and dialect == 'bash':
                        flags = {'a': 'allexport', 'e': 'errexit', 'f': 'noglob', 'n': 'noexec',
                                 'p': 'privileged', 'u': 'nounset', 'v': 'verbose', 'x': 'xtrace'}
                        current = ''.join(flag for flag in 'aefhnpuvx' if flag == 'h' or state['options'][flags[flag]])
                        current += 'Bc' if command_string else 'B'
                    elif name in {'$', '-'}:
                        current = Unknown('$' + name)
                    elif name == '!' or name.isdigit() and current is None:
                        current = Unset('')
                    if current is None and state['options']['nounset'] and not operator:
                        unknown(state, 'Unknown environment variable with nounset: ' + name)
                        break
                    if isinstance(current, Unset) and not operator and state['options']['nounset']:
                        expansion_error(state)
                        break
                    if length:
                        if operator or isinstance(current, Unknown):
                            unknown(state, 'Unknown shell parameter length')
                            break
                        current = str(len(state['vars'].get('@', []))) if name in {'@', '*'} else str(len(current)) if current is not None else Unknown('$' + part)
                    present = current is not None and not isinstance(current, Unset) and (bool(current) or not operator or not operator.startswith(":"))
                    alternate = operator is not None and ((operator.endswith("+") and present) or (not operator.endswith("+") and not present))
                    if alternate:
                        try:
                            quoted = part_index in word.protected
                            default_word = shell_words(default or "", parameter=True, double_quoted=quoted, dialect=dialect)[0]
                            expanded = expand_word(default_word, state, level, origin, parameter=True)
                            if state['done']:
                                break
                            part_uncertain = isinstance(expanded, Unknown)
                            vector_groups = [list(group) for group in expanded.field_groups]
                            part_has_vector = expanded.has_vector
                            bash_at_operand = bash_at_operand or expanded.bash_at_operand and not quoted
                            quoted_bash_at_operand = quoted_bash_at_operand or quoted_operand and expanded.quoted_bash_at_operand
                            if quoted:
                                # A quoted parameter default retains an
                                # empty field even when its $@ is empty.
                                vector_groups[0].insert(0, ('', False))
                        except (ValueError, RecursionError):
                            unknown(state, "Unsupported parameter expansion")
                            expanded = "$" + part
                        if state['done']:
                            break
                        if operator.endswith("="):
                            if name in state['readonly']:
                                # The operand has already expanded, including
                                # any real command substitutions. The failed
                                # assignment now aborts this interpreter.
                                status = state['readonly_status']
                                if dialect == 'bash':
                                    if state['options']['errexit']:
                                        status = '1'
                                    elif state['options']['posix']:
                                        status = state['expansion_status']
                                expansion_error(state, status)
                                break
                            state["vars"][name] = expanded
                            assigned(state, name)
                        elif operator.endswith('?'):
                            expansion_error(state)
                            break
                        value.append(expanded)
                    elif operator and operator.endswith("+"):
                        value.append("")
                    else:
                        quoted_bash_at_operand = quoted_bash_at_operand or quoted_operand and name == '@'
                        part_uncertain = current is None or isinstance(current, Unknown) or isinstance(current, list) and any(isinstance(v, Unknown) for v in current)
                        value.append(" ".join(current) if isinstance(current, list) else str(current) if current is not None else "$" + name)
                        separator = state['vars'].get('IFS', ' \t\n')
                        if isinstance(separator, Unset):
                            separator = ' \t\n'
                        # Bash joins unquoted positional parameters inside a
                        # parameter operand before IFS splitting (except when
                        # IFS is empty). Explicitly quoted vectors stay intact.
                        scalar_operand = dialect == 'bash' and parameter and part_index not in word.protected and bool(separator)
                        if scalar_operand and name == '@':
                            bash_at_operand = True
                            vector_groups = [[]]
                            for argument_index, argument in enumerate(current):
                                if argument_index:
                                    vector_groups[0].append((' ', True))
                                # With a non-space first IFS character Bash
                                # protects argument contents, including nulls;
                                # only the inserted separators can split.
                                vector_groups[0].append((argument, separator.startswith(' ')))
                        elif (name == '@' or name == '*' and part_index not in word.protected) and not scalar_operand:
                            vector_groups = [[(argument, part_index not in word.protected)]
                                             for argument in state['vars'].get('@', [])]
                            part_has_vector = True
                elif kind == "sub":
                    child = process_child(state, substitution=True)
                    child.update(go=False, done=None, output=[])
                    program = parse_program(part, child)
                    outcomes = evaluate(program, [child], level + 1, origin) if program else [child]
                    outcomes = finish_process(outcomes, level + 1, origin)
                    queue_chld(state)
                    statuses = [process_status(outcome) for outcome in outcomes]
                    raw_statuses = [status_value(outcome, process=True) for outcome in outcomes]
                    state['substitution_status'] = (True, statuses[0] if statuses and all(
                        status == statuses[0] for status in statuses) else None,
                        raw_statuses[0] if raw_statuses and all(raw == raw_statuses[0] for raw in raw_statuses) else Unknown('$?'))
                    paths = set().union(*(outcome["stage"].files for outcome in outcomes))
                    for path in paths:
                        identities = [outcome["stage"].files.get(path) for outcome in outcomes]
                        state["stage"].files[path] = identities[0] if identities[0] is not None and all(identity == identities[0] for identity in identities) else ("unknown", "")
                    state["go"] = state["go"] or all(outcome["go"] for outcome in outcomes)
                    outputs = [None if outcome["output"] is None else
                               (Unknown if any(isinstance(item, Unknown) for item in outcome["output"]) else str)("".join(outcome["output"]).rstrip("\n")) for outcome in outcomes]
                    if outcomes and all(outcome["done"] != "unsupported" for outcome in outcomes) and outputs[0] is not None and all(out == outputs[0] for out in outputs):
                        value.append(outputs[0])
                        part_uncertain = isinstance(outputs[0], Unknown)
                    else:
                        value.append("$unresolved_substitution")
                        part_uncertain = True
                        if any(outcome["done"] == "unsupported" for outcome in outcomes):
                            unknown(state, "Unsupported command substitution")
                else:
                    # Arithmetic itself does not execute commands, but nested
                    # $(...) / backtick expressions do, before arithmetic.
                    try:
                        arithmetic = []
                        for item in shell_words(part, dialect=dialect):
                            arithmetic.append(expand_word(item, state, level + 1, origin))
                            if state['done']:
                                break
                        if state['options']['nounset'] and dialect == 'bash' and not state['done']:
                            if any(isinstance(item, Unknown) for item in arithmetic):
                                unknown(state, 'Unknown arithmetic with nounset')
                            else:
                                for name in re.findall(r'\b[A-Za-z_]\w*\b', ' '.join(arithmetic)):
                                    current = variable(state, name)
                                    if isinstance(current, Unset):
                                        expansion_error(state)
                                        break
                                    if current is None or isinstance(current, Unknown) or not re.fullmatch(r'[+-]?[0-9]+', current):
                                        unknown(state, 'Unknown arithmetic variable with nounset')
                                        break
                    except (ValueError, RecursionError):
                        unknown(state, "Unsupported shell arithmetic")
                    value.append("$arithmetic")
                    part_uncertain = True
                fragment = "".join(value[offset:])
                split = (kind != 'literal' or parameter) and part_index not in word.protected
                if vector_groups is not None:
                    # Preserve fragment quoting as well as positional field
                    # boundaries through nested parameter defaults.
                    has_vector = has_vector or part_has_vector
                    for group_index, group in enumerate(vector_groups):
                        if group_index:
                            field_groups.append([])
                        field_groups[-1].extend(group)
                else:
                    field_groups[-1].append((Unknown(fragment) if part_uncertain else
                                            ParameterLiteral(fragment) if quoted_operand and kind == 'literal' else fragment, split))
                uncertain = uncertain or part_uncertain
                if pattern and part_index in word.protected:
                    protected = ''.join('\\' + char for char in fragment)
                    ash = dialect == 'ash' or dialect == 'sh' and stage.sh_dialect == 'ash'
                    if (ash and part_index in word.quoted_parts and fragment.startswith('^')
                            and ''.join(value[:offset]) == '['):
                        # BusyBox ash retains quote protection for ! here,
                        # but a quoted ^ still negates a bracket expression.
                        protected = '^' + protected[2:]
                    value[offset:] = [protected]
            expanded = (Unknown if uncertain else Expansion)("".join(value))
            ifs = state['vars'].get('IFS', ' \t\n')
            if isinstance(ifs, Unset):
                ifs = ' \t\n'
            if isinstance(ifs, Unknown):
                unknown(state, 'Unknown shell field separator')
            if quoted_bash_at_operand:
                # Bash splits non-whitespace IFS delimiters in the literal
                # portions of a quoted default containing $@. Argument values
                # retain their quote protection, as does literal whitespace.
                field_groups = [[piece for fragment, split in group for piece in
                                 ([(char, split or char in ifs and char not in ' \t\n') for char in fragment]
                                  if isinstance(fragment, ParameterLiteral) and fragment else [(fragment, split)])]
                                for group in field_groups]
            expanded.fields = [(Unknown if any(isinstance(fragment, Unknown) for fragment, _ in group) else Expansion)(item)
                               for group in field_groups for item in shell_fields(group, ifs)]
            if bash_at_operand:
                # Bash first expands/splits the operand word, then rejoins
                # those fields with spaces. Keep each field's contents
                # protected while allowing the inserted spaces to split.
                fragments = []
                for field_index, item in enumerate(expanded.fields):
                    if field_index:
                        fragments.append((' ', True))
                    fragments.append((item, False))
                field_groups = [fragments]
                expanded.fields = [(Unknown if any(isinstance(fragment, Unknown) for fragment, _ in fragments) else Expansion)(item)
                                   for item in shell_fields(fragments, ifs)]
            expanded.field_groups, expanded.has_vector = field_groups, has_vector
            expanded.bash_at_operand = bash_at_operand
            expanded.quoted_bash_at_operand = quoted_bash_at_operand
            return expanded

        def word_fields(word, state, level, origin):
            expanded = expand_word(word, state, level, origin)
            return expanded.fields if isinstance(expanded, Expansion) else [expanded]

        def declaration_target(word, builtin):
            name, separator, value = word.partition('=')
            append = dialect == 'bash' and bool(separator) and name.endswith('+')
            if append:
                name = name[:-1]
            subscript = None
            if dialect == 'bash' and builtin == 'local':
                match = re.fullmatch(r'([A-Za-z_][A-Za-z0-9_]*)\[(0|[1-9][0-9]*)\]', name)
                if match:
                    name, subscript = match.groups()
            return name, separator, value, append, subscript

        def assignment_name(word, *, declaration=None):
            if not isinstance(word, Word):
                return None
            prefix = ''
            for index, (kind, value) in enumerate(word.parts):
                if kind != 'literal' or index in word.protected:
                    return None
                prefix += value
                if '=' in prefix:
                    name = (declaration_target(prefix, declaration)[0]
                            if declaration else prefix.split('=', 1)[0])
                    return name if re.fullmatch(r'[A-Za-z_]\w*', name) else None
            return None

        def readonly_error(state, *, fatal=False, special=True, process_fatal=False):
            bash = dialect == 'bash'
            set_status(state, False, '1' if bash else '2')
            if not bash and special or fatal:
                active_errexit = (state['options']['errexit'] and not state['ignore_errexit']
                                  and not state.get('startup_errexit_inhibited'))
                state['done'] = ('startup_error' if bash and (state.get('startup_file') or state.get('source_file'))
                                 and not active_errexit and not process_fatal else 'exit')
                state['exit_reason'] = 'readonly'
            return state

        def trace_prefix(state, level, origin):
            # xtrace can run command substitutions in PS4. They are real
            # runtime evidence, while trace output is not command output.
            if dialect != 'bash' or not state['options']['xtrace'] or state.get('in_xtrace'):
                return
            prefix = variable(state, 'PS4')
            if isinstance(prefix, Unset):
                return
            if prefix is None or isinstance(prefix, Unknown):
                unknown(state, 'Unknown xtrace prefix')
                return
            saved = {key: state.get(key) for key in ['status', 'raw_status', 'substitution_status', 'output', 'done', 'exit_reason']}
            state['in_xtrace'] = True
            try:
                word = shell_words(prefix, dialect=dialect, expand_only=True)[0]
                expand_word(word, state, level, origin)
            except (ValueError, RecursionError) as error:
                unknown(state, str(error))
            finally:
                state['in_xtrace'] = False
            if state['done'] != 'unsupported':
                # A failed prompt expansion reports an error but does not
                # abort the command whose trace Bash was printing.
                state.update(saved)

        def resolve_command(row, state, *, lookup_functions=True):
            """Use the same command/function selection for dispatch and scope."""
            row = list(row)
            while (row and row[0] == 'command' and not command_query(row)
                   and (not lookup_functions or 'command' not in state['functions'])):
                row = row[1:]
                while row and row[0] in {'-p', '--'}:
                    if row.pop(0) == '--':
                        break
                lookup_functions = False
            name = row[0] if row else ''
            special_first = name in SPECIAL_BUILTINS and (dialect != 'bash' or state['options']['posix'])
            function = lookup_functions and not special_first and name in state['functions']
            return row, lookup_functions, function

        def command(raw, state, level, origin):
            # Arguments expand in the caller's environment. Assignment words
            # then expand from left to right, seeing earlier assignments.
            raw = list(raw)
            state['substitution_status'] = (False, None, None)
            assignments = []
            while raw and assignment_name(raw[0]) is not None:
                assignments.append((assignment_name(raw[0]), raw.pop(0)))
            row, origins = [], []
            # Bash recognises unquoted declaration words lexically, including
            # same-named functions; POSIX mode also recognises bare `command`
            # prefixes. ash/dash recognise the expanded declaration command.
            declaration_words = raw
            if dialect == 'bash' and state['options']['posix']:
                while (declaration_words and declaration_words[0] == 'command'
                       and not declaration_words[0].quoted and not declaration_words[0].protected):
                    declaration_words = declaration_words[1:]
            declarations = bool(declaration_words) and str(declaration_words[0]) in {'export', 'local', 'readonly'}
            if dialect == 'bash':
                declarations = declarations and not declaration_words[0].quoted and not declaration_words[0].protected
            for word in raw:
                if dialect != 'bash':
                    selected, _, function = resolve_command(row, state)
                    declarations = bool(selected) and not function and selected[0] in {'export', 'local', 'readonly'}
                declaration = declarations and assignment_name(word, declaration=str(declaration_words[0])) is not None
                values = [expand_word(word, state, level, origin)] if declaration else word_fields(word, state, level, origin)
                row.extend(values)
                origins.extend([word] * len(values))
                if state['done']:
                    return drain_chld([state], level, origin)
            if state["done"]:
                return [state]
            # Redirection targets are expanded above, including substitutions;
            # they are not executable arguments.
            cleaned, index = [], 0
            while index < len(row):
                if not isinstance(origins[index], Word) and row[index] in {">", "<", ">>", ">&", "<&", "<<", "<<-"}:
                    if cleaned and cleaned[-1].isdigit():
                        cleaned.pop()
                    index += 2
                else:
                    cleaned.append(row[index])
                    index += 1
            row = cleaned
            saved = {name: state["vars"].get(name) for name, _ in assignments}
            saved_exports = set(state['exported'])
            saved_readonly = set(state['readonly'])
            for name, word in assignments:
                expanded = expand_word(word, state, level, origin)
                if state['done']:
                    return drain_chld([state], level, origin)
                if not row:
                    trace_prefix(state, level, origin)
                    if state['done']:
                        return [state]
                if name in state['readonly']:
                    # Assignment values expand before the readonly error.
                    # Bash aborts this file for a bare assignment; a failed
                    # command environment instead returns failure to its list.
                    if dialect != 'bash':
                        return [unknown(state, 'Assignment to readonly shell variable')]
                    if row and len(assignments) > 1:
                        # Partial command environments have builtin-specific
                        # rollback rules. Keep this previously unsupported
                        # case explicit instead of leaking temporary values.
                        return [unknown(state, 'Unsupported partial readonly command environment')]
                    if not row or state['options']['posix']:
                        return drain_chld([readonly_error(state, fatal=True)], level, origin)
                    continue
                value = expanded.split('=', 1)[1]
                state["vars"][name] = Unknown(value) if isinstance(expanded, Unknown) else value
                assigned(state, name)
                if row:
                    trace_prefix(state, level, origin)
                    if state['done']:
                        return [state]
            if state["done"]:
                return [state]
            if not row:
                substituted, status, raw_status = state['substitution_status']
                set_status(state, status if substituted else True, raw_status if substituted else None)
                return drain_chld([state], level, origin)
            # Prefix assignments form this command's environment, including
            # external commands called from a temporarily bound function.
            if assignments:
                # Bash traces the command with the caller's variable values;
                # its assignment traces above use each temporary binding.
                temporary = {name: state['vars'].get(name) for name in saved}
                for name, value in saved.items():
                    if value is None:
                        state['vars'].pop(name, None)
                    else:
                        state['vars'][name] = value
                    if name in saved_exports:
                        state['exported'].add(name)
                    else:
                        state['exported'].discard(name)
                trace_prefix(state, level, origin)
                for name, previous in list(saved.items()):
                    value = state['vars'].get(name)
                    # Prompt assignments affect the caller and supersede a
                    # same-named temporary environment binding in Bash.
                    if type(value) is not type(previous) or value != previous:
                        temporary[name] = value
                    saved[name] = value
                    if name in state['exported']:
                        saved_exports.add(name)
                    else:
                        saved_exports.discard(name)
                for name, value in temporary.items():
                    if value is None:
                        state['vars'].pop(name, None)
                    else:
                        state['vars'][name] = value
            else:
                trace_prefix(state, level, origin)
            if state['done']:
                return [state]
            state['exported'].update(saved)
            prefix_posix = state['options']['posix']
            selected, lookup_functions, function = resolve_command(row, state)
            builtin = selected[0] if selected else ''
            special = lookup_functions and not function and builtin in SPECIAL_BUILTINS
            declarations = selected[1:] if not function and builtin in {'export', 'readonly', 'local'} else []
            declared = {declaration_target(word, builtin)[0] for word in declarations if not word.startswith('-')}
            remove_export = builtin == 'export' and any('n' in word[1:] for word in declarations
                                                        if word.startswith('-') and word != '--')
            local_frame = dict(state['locals'][-1]) if not function and builtin == 'local' and state['locals'] else None
            if local_frame is not None:
                for name, value in saved.items():
                    if name in declared:
                        state['locals'][-1].setdefault(name, (value, name in saved_exports))
            outcomes = dispatch(row, state, level, origin, prefix_assignments=saved)
            for outcome in outcomes:
                for name, value in saved.items():
                    persist = special and (dialect != 'bash' or prefix_posix)
                    promoted = (dialect == 'bash' and name in declared and
                                (builtin == 'readonly' or builtin == 'export' and not remove_export
                                 or builtin == 'local' and local_frame is not None))
                    unset_temporary = dialect == 'bash' and isinstance(outcome['vars'].get(name), Unset)
                    if unset_temporary or not persist and not promoted:
                        if value is None:
                            outcome["vars"].pop(name, None)
                        else:
                            outcome["vars"][name] = value
                        if name in saved_exports:
                            outcome['exported'].add(name)
                        else:
                            outcome['exported'].discard(name)
                        if name not in saved_readonly:
                            outcome['readonly'].discard(name)
                    elif dialect == 'bash' and name in saved_exports:
                        # Unexporting a temporary binding does not erase an
                        # already exported outer variable's environment slot.
                        outcome['exported'].add(name)
                    if local_frame is not None and name in declared and name not in local_frame:
                        outcome['locals'][-1][name] = (value, name in saved_exports)
            return drain_chld(outcomes, level, origin)

        def dispatch(row, state, level, origin, *, lookup_functions=True, prefix_assignments=()):
            # command can select another builtin, including eval. Unwrap it
            # without recursion or another round of argument expansion.
            row, lookup_functions, function = resolve_command(row, state, lookup_functions=lookup_functions)
            if not lookup_functions and row and row[0].startswith('-'):
                return [unknown(state, 'Unsupported command option')]
            if not row:
                set_status(state, True)
                return [state]
            if isinstance(row[0], Unknown) and not NODE.fullmatch(posixpath.basename(row[0])):
                return [unknown(state, 'Unknown shell executable')]
            if function:
                if level > 24:
                    return [unknown(state, "Shell function recursion limit reached")]
                body, definition = state["functions"][row[0]]
                child = clone(state)
                child["locals"].append({})
                # Bash/dash functions cannot transfer control to a caller's
                # loop. BusyBox ash deliberately retains that loop context.
                selected_dialect = stage.sh_dialect if dialect == 'sh' else dialect
                if selected_dialect != 'ash':
                    child['loop_depth'] = 0
                saved_readonly = set(state['readonly'])
                saved = {key: value for key, value in state["vars"].items() if key == "@" or key.isdigit()}
                child["vars"] = {key: value for key, value in child["vars"].items() if key != "@" and (not key.isdigit() or key == "0")}
                child["vars"].update({"@": row[1:], **{str(n): val for n, val in enumerate(row[1:], 1)}})
                outcomes = evaluate(body, [child], level + 1, definition)
                for outcome in outcomes:
                    outcome['loop_depth'] = state['loop_depth']
                    for name, (value, exported) in outcome["locals"].pop().items():
                        if value is None:
                            outcome["vars"].pop(name, None)
                        else:
                            outcome["vars"][name] = value
                        if exported:
                            outcome['exported'].add(name)
                        else:
                            outcome['exported'].discard(name)
                        if name not in saved_readonly:
                            outcome['readonly'].discard(name)
                    outcome["vars"] = {key: value for key, value in outcome["vars"].items() if key != "@" and not key.isdigit()}
                    outcome["vars"].update(saved)
                    if outcome["done"] == "return":
                        outcome["done"] = None
                return outcomes
            if row[0] in {"exit", "return", "break", "continue"}:
                selected_dialect = stage.sh_dialect if dialect == 'sh' else dialect
                builtin, operands = row[0], row[1:]
                if selected_dialect == 'bash' and operands and operands[0] == '--help':
                    # All four builtins parse help before their context checks.
                    # In POSIX mode an untested special-builtin error is fatal.
                    state['output'] = None
                    set_status(state, False, '2')
                    if (state['options']['posix'] and lookup_functions
                            and not state.get('special_error_tested', False)):
                        state.update(done='exit', exit_reason=builtin)
                    return [state]
                if builtin in {'break', 'continue'}:
                    # Bash checks loop context before inspecting operands.
                    # Even invalid arguments are ignored outside a loop.
                    if not state['loop_depth'] and selected_dialect == 'bash':
                        set_status(state, True)
                        return [state]
                    if not state['loop_depth'] and selected_dialect not in {'dash', 'ash'}:
                        # A positive POSIX count is harmless outside loops in
                        # all supported shells. Extensions require a known
                        # interpreter because Bash, dash and ash disagree.
                        if operands:
                            operand = operands[0]
                            digits = operand.lstrip('0') or '0'
                            if (isinstance(operand, Unknown) or not re.fullmatch(r'[0-9]+', operand)
                                    or len(digits) > 10 or not 0 < int(digits) < (1 << 31)):
                                return [unknown(state, 'Unknown shell dialect for loop control operand')]
                        set_status(state, True)
                        return [state]
                    if selected_dialect == 'bash' and operands and operands[0] == '--':
                        operands = operands[1:]
                    if selected_dialect in {'dash', 'ash'}:
                        count = 1
                        if operands:
                            operand = operands[0]
                            if isinstance(operand, Unknown):
                                return [unknown(state, 'Unknown shell loop control operand')]
                            value = operand.strip(' \t\n\r\v\f') if selected_dialect == 'dash' else operand
                            pattern = r'[+]?[0-9]+' if selected_dialect == 'dash' else r'[0-9]+'
                            digits = value.lstrip('+0') or '0'
                            valid = bool(re.fullmatch(pattern, value)) and len(digits) <= 10
                            count = int(digits) if valid else 0
                            if not valid or not 0 < count < (1 << 31):
                                set_status(state, False, '2')
                                if lookup_functions:
                                    state.update(done='exit', exit_reason=builtin)
                                return [state]
                        if not state['loop_depth']:
                            set_status(state, True)
                            return [state]
                        if count != 1:
                            return [unknown(state, "Unsupported multi-level shell loop control")]
                    elif selected_dialect == 'bash' and len(operands) > 1:
                        # Invalid numbers take precedence over extra operands.
                        value = operands[0].strip(' \t\n\r\v\f')
                        digits = value.lstrip('+-').lstrip('0') or '0'
                        valid = (not isinstance(operands[0], Unknown)
                                 and bool(re.fullmatch(r'[+-]?[0-9]+', value)) and len(digits) <= 19)
                        if isinstance(operands[0], Unknown):
                            return [unknown(state, 'Unknown shell loop control operand')]
                        if valid:
                            count = int(('-' if value.startswith('-') else '') + digits)
                            valid = -(1 << 63) <= count < (1 << 63)
                        set_status(state, False, '1' if valid else '128')
                        state.update(done='exit', exit_reason=builtin)
                        return [state]
                    elif operands and (len(operands) != 1 or operands[0] != '1'):
                        return [unknown(state, "Unsupported multi-level shell loop control")]
                    state['done'] = builtin
                    set_status(state, True)
                    return [state]
                if selected_dialect == 'bash':
                    if (builtin == 'return' and state.get('startup_file')
                            and not state.get('source_file') and not state['locals']):
                        # BASH_ENV returns bypass numeric/arity validation and
                        # keep the status from before the return command.
                        state['done'] = 'return'
                        return [state]
                    if operands and operands[0] == '--':
                        operands = operands[1:]
                code = None
                if operands:
                    operand = operands[0]
                    if isinstance(operand, Unknown):
                        return [unknown(state, 'Unknown shell exit status operand')]
                    if selected_dialect not in {'bash', 'dash', 'ash'}:
                        # Plain POSIX status values agree across these shells;
                        # extensions need an identified interpreter, rather
                        # than a guessed status or a missing dictionary key.
                        digits = operand.lstrip('0') or '0'
                        if (len(operands) != 1 or not re.fullmatch(r'[0-9]+', operand)
                                or len(digits) > 3 or int(digits) > 255):
                            return [unknown(state, 'Unknown shell dialect for exit status operand')]
                    value = operand.strip(' \t\n\r\v\f') if selected_dialect != 'ash' else operand
                    pattern = {'bash': r'[+-]?[0-9]+', 'dash': r'[+]?[0-9]+',
                               'ash': r'[0-9]+'}.get(selected_dialect, r'[0-9]+')
                    valid = bool(re.fullmatch(pattern, value))
                    digits = value.lstrip('+-').lstrip('0') or '0'
                    # Bound conversion independently of Python's digit limit.
                    limit = 10 if selected_dialect == 'dash' else 19
                    if selected_dialect != 'ash':
                        valid = valid and len(digits) <= limit
                    if valid:
                        code = (int(('-' if value.startswith('-') else '') + digits)
                                if len(digits) <= limit else (1 << 63) - 1)
                        if selected_dialect == 'bash':
                            valid = -(1 << 63) <= code < (1 << 63)
                        elif selected_dialect == 'dash':
                            valid = code < (1 << 31)
                        else:
                            # BusyBox's decimal conversion saturates at LONG_MAX.
                            # exit then clamps a negative 32-bit result to zero;
                            # return retains the low byte even in that case.
                            code = min(code, (1 << 63) - 1)
                            if builtin == 'exit' and code % (1 << 32) >= (1 << 31):
                                code = 0
                    if not valid:
                        code = 2
                        if selected_dialect != 'bash':
                            set_status(state, False, '2')
                            if lookup_functions:
                                state.update(done='exit', exit_reason=builtin)
                            return [state]
                    elif selected_dialect == 'bash' and len(operands) > 1:
                        # Numeric parsing precedes arity checking, which in
                        # turn precedes return-context checking. This error
                        # terminates even a function used as an if condition.
                        set_status(state, False, '1')
                        state.update(done='exit', exit_reason=builtin)
                        return [state]
                    if selected_dialect in {'bash', 'ash'}:
                        code %= 256
                if (row[0] == 'return' and dialect == 'bash' and not state['locals']
                        and not state.get('source_file') and not state.get('startup_file')):
                    set_status(state, False, '2')
                    if state['options']['posix'] and lookup_functions and not state['ignore_errexit']:
                        state.update(done='exit', exit_reason='return')
                    return [state]
                state["done"] = row[0]
                if row[0] == 'exit':
                    state['exit_reason'] = 'exit'
                if code is not None:
                    set_status(state, code == 0, str(code))
                return [state]
            if row[0] in {"export", "local", "readonly"}:
                builtin = row.pop(0)
                if builtin == 'local' and not state['locals']:
                    set_status(state, False, '1')
                    return [state]
                remove_export = False
                export_local = readonly_local = False
                while row and row[0].startswith('-'):
                    option = row.pop(0)
                    if option == '--':
                        break
                    flags = set(option[1:])
                    if builtin == 'export' and flags and flags <= {'n', 'p'}:
                        if 'n' in flags and (dialect == 'dash' or dialect == 'sh' and stage.sh_dialect == 'dash'):
                            state['done'] = 'exit'
                            set_status(state, False, '2')
                            return [state]
                        remove_export = remove_export or 'n' in flags
                    elif builtin == 'local' and dialect == 'bash' and flags and flags <= {'x', 'r', 'p'}:
                        export_local = export_local or 'x' in flags
                        readonly_local = readonly_local or 'r' in flags
                    elif option != '-p':
                        return [unknown(state, 'Unsupported shell declaration option')]
                if not row:
                    state['output'] = None
                failed = False
                for word in row:
                    name, separator, value, append, subscript = declaration_target(word, builtin)
                    if isinstance(word, Unknown):
                        value = Unknown(value)
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                        failed = True
                        if dialect != 'bash':
                            set_status(state, False, '2')
                            if lookup_functions:
                                state.update(done='exit', exit_reason=builtin)
                            return [state]
                        continue
                    declared = builtin == 'local' and state['locals'] and name in state['locals'][-1]
                    if builtin == "local" and state["locals"]:
                        state["locals"][-1].setdefault(name, (state["vars"].get(name), name in state['exported']))
                    if separator:
                        trace_prefix(state, level, origin)
                        if state['done']:
                            return [state]
                        if name in state['readonly']:
                            if builtin == 'export':
                                if remove_export:
                                    state['exported'].discard(name)
                                else:
                                    state['exported'].add(name)
                            process_fatal = lookup_functions and dialect == 'bash' and state['options']['posix'] and builtin != 'local'
                            readonly_error(state, fatal=process_fatal, special=lookup_functions, process_fatal=process_fatal)
                            if state['done']:
                                return [state]
                            failed = True
                            continue
                        if append:
                            previous = (Unset('') if (name in prefix_assignments and builtin != 'local'
                                                      or builtin == 'local' and not declared)
                                        else variable(state, name))
                            if previous is None or isinstance(previous, Unknown) or isinstance(value, Unknown):
                                value = Unknown(str(previous or '') + value)
                            else:
                                value = str(previous) + value
                        if subscript not in {None, '0'}:
                            # A nonzero element does not describe the scalar
                            # (index-zero) expansion; retain that uncertainty.
                            value = Unknown(value)
                        state["vars"][name] = value
                        assigned(state, name)
                    elif builtin == "local":
                        if not declared and (dialect in {'bash', 'ash'} or dialect == 'sh' and stage.sh_dialect == 'ash'):
                            state['vars'][name] = Unset('')
                        else:
                            state["vars"].setdefault(name, Unset(''))
                    if builtin == 'readonly' or readonly_local:
                        state['readonly'].add(name)
                    if export_local:
                        state['exported'].add(name)
                    elif builtin == 'export':
                        if remove_export:
                            state['exported'].discard(name)
                        else:
                            state['exported'].add(name)
                set_status(state, not failed, ('1' if dialect == 'bash' else '2') if failed else None)
                return [state]
            if row[0] == "eval":
                if any(isinstance(word, Unknown) for word in row[1:]):
                    return [unknown(state, "Unknown eval program")]
                # eval is a special builtin: definitions, variables, cwd,
                # locals and control transfers all belong to this interpreter.
                program = parse_program(" ".join(row[1:]), state)
                # Bash's POSIX special-builtin errors ignore a condition on
                # eval itself, while conditions inside its program still
                # suppress those errors. Keep this separate from errexit.
                special_error_tested = state.get('special_error_tested', False)
                state['special_error_tested'] = False
                outcomes = evaluate(program, [state], level + 1, origin) if program else [state]
                for outcome in outcomes:
                    outcome['special_error_tested'] = special_error_tested
                return outcomes
            if row[0] == "unset":
                functions, index = False, 1
                while index < len(row) and row[index].startswith('-'):
                    option = row[index]
                    index += 1
                    if option == '--':
                        break
                    if option not in {'-v', '-f'}:
                        return [unknown(state, 'Unsupported unset option')]
                    functions = option == '-f'
                failed = False
                for name in row[index:]:
                    if not re.fullmatch(r'[A-Za-z_]\w*', name):
                        return [unknown(state, 'Unsupported unset operand')]
                    if functions:
                        state["functions"].pop(name, None)
                    else:
                        if name in state['readonly']:
                            readonly_error(state, special=lookup_functions)
                            if state['done']:
                                return [state]
                            failed = True
                            continue
                        state["vars"][name] = Unset('')
                        fixed = name in {'PATH', 'IFS', 'PS1', 'PS2', 'PS4'} or any(name in frame for frame in state['locals'])
                        if dialect == 'bash' or not fixed:
                            state['exported'].discard(name)
                set_status(state, not failed, ('1' if dialect == 'bash' else '2') if failed else None)
                return [state]
            if row[0] == 'set':
                try:
                    changed = set()
                    selected_dialect = stage.sh_dialect if dialect == 'sh' else dialect
                    options, index, _ = shell_options(row[1:], state['options'], dialect=selected_dialect, changed=changed)
                except ValueError as error:
                    return [unknown(state, str(error))]
                index += 1
                if index == len(row) and row[-1].endswith('o'):
                    state['output'] = None
                if (dialect == 'dash' or dialect == 'sh' and stage.sh_dialect == 'dash') and options['pipefail']:
                    set_status(state, False, '2')
                    state['done'] = 'exit'
                    return [state]
                state['options'] = options
                if 'errexit' in changed:
                    state.pop('startup_errexit_inhibited', None)
                refresh_shellopts(state)
                replace = index < len(row)
                if replace and row[index] in {'--', '-'}:
                    replace = row[index] == '--' or index + 1 < len(row)
                    index += 1
                if replace:
                    state['vars'] = {key: value for key, value in state['vars'].items()
                                     if key != '@' and (not key.isdigit() or key == '0')}
                    state['vars'].update({'@': row[index:], **{str(n): value for n, value in enumerate(row[index:], 1)}})
                set_status(state, True)
                return [state]
            if row[0] == 'trap':
                args = row[1:]
                if args[:1] == ['--']:
                    args = args[1:]
                if not args or args[0] == '-p' and dialect == 'bash' and all(arg in {'EXIT', '0', 'CHLD', 'SIGCHLD', '17'} for arg in args[1:]):
                    # A query prints declarations; it neither executes nor
                    # discards the registered handler. Its text is unknown
                    # if a caller later tries to evaluate that output.
                    state['output'] = None
                    set_status(state, True)
                    return [state]
                if args and args[0].isascii() and args[0].isdigit():
                    args.insert(0, '-')
                if len(args) < 2 or any(isinstance(arg, Unknown) for arg in args):
                    return [unknown(state, 'Unsupported trap query or arguments')]
                handler, signals = args[0], args[1:]
                if handler.startswith('-') and handler != '-':
                    return [unknown(state, 'Unsupported trap option')]
                ordinary = {'HUP', 'INT', 'QUIT', 'ILL', 'ABRT', 'FPE', 'KILL', 'SEGV', 'PIPE', 'ALRM',
                            'TERM', 'USR1', 'USR2', 'CHLD', 'CONT', 'STOP', 'TSTP', 'TTIN', 'TTOU', 'WINCH'}
                for signal in signals:
                    name = signal.removeprefix('SIG') if signal.removeprefix('SIG') in ordinary else signal
                    if name in {'EXIT', '0'}:
                        state['exit_trap'] = None if handler in {'', '-'} else handler
                    elif name in {'CHLD', '17'}:
                        state['chld_trap'] = None if handler in {'', '-'} else handler
                    elif name not in ordinary and not (name.isascii() and name.isdigit() and len(name) <= 2 and 0 < int(name) < 65):
                        return [unknown(state, 'Unsupported shell trap ' + signal)]
                set_status(state, True)
                return [state]
            if row[0] == 'shift':
                operand = row[1] if len(row) == 2 else '1'
                arguments = state['vars'].get('@', [])
                if (len(row) > 2 or isinstance(operand, Unknown) or not re.fullmatch(r'[0-9]+', operand)
                        or len(operand.lstrip('0')) > len(str(len(arguments)))):
                    return [unknown(state, 'Unsupported shift operand')]
                count = int(operand.lstrip('0') or '0')
                if count > len(arguments):
                    return [unknown(state, 'Shift exceeds positional parameters')]
                remaining = arguments[count:]
                state['vars'] = {key: value for key, value in state['vars'].items()
                                 if key != '@' and (not key.isdigit() or key == '0')}
                state['vars'].update({'@': remaining, **{str(n): value for n, value in enumerate(remaining, 1)}})
                set_status(state, True)
                return [state]
            if row[0] == "cd" and len(row) == 2:
                state["stage"].cwd = path_at(row[1], state["stage"].cwd)
                state["vars"]["PWD"] = state["stage"].cwd
                set_status(state, True)
                return [state]
            if row[0] in {".", "source"} and len(row) > 1:
                found = state["stage"].lookup(row[1])
                file = local_file(root, found[1]) if found and found[0] == "source" else None
                if file:
                    previous = state.get('source_file', 0)
                    state['source_file'] = previous + 1
                    if dialect == 'bash':
                        outcomes = read_file(file, [state], level + 1, str(file.relative_to(root)))
                    else:
                        body = parse_program(file.read_text(), state)
                        outcomes = evaluate(body, [state], level + 1, str(file.relative_to(root))) if body else [state]
                    for outcome in outcomes:
                        outcome['source_file'] = previous
                        if outcome['done'] == 'return' or dialect == 'bash' and outcome['done'] == 'startup_error':
                            outcome['done'] = None
                    return outcomes
            if row[0] == 'exec':
                selected_dialect = stage.sh_dialect if dialect == 'sh' else dialect
                try:
                    arguments, _, _, _ = exec_arguments(row[1:], selected_dialect)
                except ValueError:
                    set_status(state, False, '2')
                    if selected_dialect != 'bash':
                        state.update(done='exit', exit_reason='exit')
                    return [state]
                if not arguments:
                    # With no command exec only applies its redirections.
                    set_status(state, True)
                    return [state]
            go = go_command(row, allow_assignments=row[0] == 'env')
            outputs = build_outputs(row, state["stage"].cwd, root) if row[0] == 'env' or posixpath.basename(row[0]) == 'go' else []
            for output in outputs:
                state["stage"].files[output] = ("go", go_package(row, state["stage"], root))
            external = row[0] not in {'exec', 'echo', 'printf', 'pwd', 'true', 'false', ':', 'test', '[', 'command'}
            if external and state['chld_trap'] is not None:
                name = posixpath.basename(row[0])
                found = state['stage'].lookup(row[0])
                known = (name in {'go', 'bash', 'ash', 'dash', 'sh', 'env', 'chmod', 'mkdir', 'dirname', 'mktemp'}
                         or NODE.fullmatch(name) or found and (found[0] == 'go' or found[0] == 'source' and local_file(root, found[1])))
                if not known:
                    return [unknown(state, 'Unknown process boundary with CHLD trap')]
            child_status = []
            environment = {name: environment_value(state, name)
                           for name, value in state['vars'].items() if name != '@' and not name.isdigit()}
            state["go"] = executable(row, state["stage"], environment, origin, depth + level + 1,
                                     fallback_shell=dialect, fallback_options=state['options'],
                                     status_out=child_status, environment_closed=state['environment_closed'],
                                     lookup_path=variable(state, 'PATH')) or state["go"]
            if external:
                queue_chld(state)
                if state['done']:
                    return [state]
            set_status(state, True if (go and go[0] == "build") or row[0] in {"true", ":", "echo", "printf", "set", "export", "chmod", "mkdir", "pwd", "dirname", "mktemp"} else False if row[0] == "false" else None)
            if child_status:
                set_status(state, *child_status[0])
            if row[0] in {"[", "test"}:
                args = row[1:-1] if row[0] == "[" and row[-1] == "]" else row[1:]
                if not any(isinstance(word, Unknown) for word in args):
                    if len(args) == 1:
                        state["status"] = bool(args[0])
                    elif len(args) == 2 and args[0] in {"-n", "-z"}:
                        state["status"] = bool(args[1]) == (args[0] == "-n")
                    elif len(args) == 2 and args[0] in {"-f", "-x", "-d"}:
                        found = state["stage"].lookup(args[1])
                        state["status"] = (path_at(args[1], state["stage"].cwd) in state["stage"].directories if args[0] == "-d"
                                           else bool(found and (found[0] == "go" or found[0] == "source" and local_file(root, found[1]))))
                    elif len(args) == 3 and args[1] in {"=", "==", "!="}:
                        state["status"] = (args[0] == args[2]) == (args[1] != "!=")
            captured = None
            if command_query(row):
                # Querying a name is inactive. If its output is subsequently
                # invoked, preserve a symbolic executable identity so that a
                # lookup cannot disguise a later source-runtime invocation.
                index = 1
                verbose = False
                while index < len(row) and row[index].startswith('-'):
                    option = row[index]
                    index += 1
                    if option == '--':
                        break
                    verbose = verbose or 'V' in option
                names = row[index:]
                if names and not verbose:
                    captured = Unknown('\n'.join(names) + '\n')
                    if len(names) == 1 and state['stage'].lookup(names[0]) == ('go', ''):
                        captured = names[0] + '\n'
                        set_status(state, True)
            elif go and go[0] == "build":
                captured = ""
            elif row[0] == "pwd":
                captured = state["stage"].cwd + "\n"
            elif row[0] == "dirname" and len(row) == 2:
                captured = (posixpath.dirname(row[1]) or ".") + "\n"
            elif row[0] == "mktemp":
                template = next((word for word in reversed(row[1:]) if not word.startswith("-")), "/tmp/action.XXXXXX")
                captured = re.sub(r"X{3,}", "audit_tmp", template) + "\n"
            elif row[0] == "echo":
                captured = " ".join(row[1:]) + "\n"
            elif row[0] == "printf" and len(row) > 1:
                captured = printf_output(row[1], row[2:])
            if captured is not None and any(isinstance(arg, Unknown) for arg in row[1:]):
                captured = Unknown(captured)
            if captured is not None and state["output"] is not None:
                state["output"].append(captured)
            elif row[0] not in {"true", "false", ":", "set", "test", "[", "chmod", "mkdir", "trap"}:
                state["output"] = None
            if row[0] == "exec":
                state["done"] = "exec"
            return [state]

        def evaluate(node, states, level=0, origin=source, *, tested=False):
            if level > 40:
                return [unknown(state, "Shell execution nesting limit reached") for state in states]
            if tested:
                outcomes = []
                for state in states:
                    previous = state['ignore_errexit']
                    special_error_tested = state.get('special_error_tested', False)
                    state['ignore_errexit'] = True
                    state['special_error_tested'] = True
                    for outcome in evaluate(node, [state], level, origin):
                        outcome['ignore_errexit'] = previous
                        outcome['special_error_tested'] = special_error_tested
                        outcomes.append(outcome)
                return outcomes
            kind = node[0]
            if kind == "seq":
                if not node[1]:
                    for state in states:
                        if not state['done']:
                            set_status(state, True)
                for child in node[1]:
                    states = evaluate(child, states, level, origin)
                return states
            output = []
            for state in states:
                if state['options']['noexec'] and not state['done']:
                    state['done'] = 'exit'
                if state["done"]:
                    output.append(state)
                    continue
                if kind == "cmd":
                    output.extend(command(node[1], state, level, origin))
                elif kind == "function":
                    if dialect == 'bash' and state['options']['posix'] and node[1] in SPECIAL_BUILTINS:
                        # Bash permits these definitions before POSIX mode is
                        # enabled, but a new definition in that mode is fatal.
                        state.update(done='exit', exit_reason='syntax')
                        set_status(state, False, '2')
                    else:
                        state["functions"][node[1]] = (node[2], origin)
                        set_status(state, True)
                    output.append(state)
                elif kind == "group":
                    output.extend(evaluate(node[1], [state], level + 1, origin))
                elif kind == "subshell":
                    children = evaluate(node[1], [process_child(state)], level + 1, origin)
                    combined = [child_result(state, child) for child in finish_process(children, level + 1, origin)]
                    for parent in combined:
                        queue_chld(parent)
                    output.extend(drain_chld(combined, level, origin))
                elif kind == 'time':
                    # Bash's keyword times a pipeline in the current scope;
                    # timing diagnostics go to stderr, not captured stdout.
                    output.extend(evaluate(node[1], [state], level + 1, origin))
                elif kind == 'coproc':
                    children = evaluate(node[1], [process_child(state)], level + 1, origin)
                    for child in finish_process(children, level + 1, origin):
                        combined = child_result(state, child)
                        combined['output'] = state['output']
                        if combined['done'] != 'unsupported':
                            set_status(combined, True)
                        name = node[2]
                        combined['coprocess_variables'] = set(state.get('coprocess_variables', set())) | {name, name + '_PID', '!'}
                        # A coprocess exits asynchronously; without wait its
                        # CHLD handler may run on either side of later code.
                        if state['chld_trap'] is not None:
                            unknown(combined, 'Asynchronous coprocess CHLD timing')
                        output.append(combined)
                elif kind == "not":
                    for tested in evaluate(node[1], [state], level + 1, origin, tested=True):
                        if not tested['done'] and tested["status"] is not None:
                            set_status(tested, not tested["status"])
                        output.append(tested)
                elif kind == "pipe":
                    branches = [(clone(state), True, '0')]
                    for index, child in enumerate(node[1]):
                        next_branches = []
                        for parent, aggregate, aggregate_raw in branches:
                            child_state = process_child(parent)
                            child_state.update(done=None, vars=dict(state["vars"]), functions=dict(state["functions"]),
                                               status=state['status'], raw_status=state.get('raw_status'))
                            outcomes = evaluate(child, [child_state], level + 1, origin)
                            for outcome in finish_process(outcomes, level + 1, origin):
                                combined = child_result(parent, outcome)
                                status, raw = combined['status'], combined.get('raw_status')
                                next_status, next_raw = aggregate, aggregate_raw
                                if not state['options']['pipefail']:
                                    next_status, next_raw = status, raw
                                elif status is False:
                                    next_status, next_raw = False, raw
                                elif status is None:
                                    next_status = False if aggregate is False else None
                                    next_raw = Unknown('$?')
                                set_status(combined, next_status, next_raw)
                                next_branches.append((combined, next_status, next_raw))
                        branches = next_branches
                    combined = [branch for branch, _, _ in branches]
                    for parent in combined:
                        queue_chld(parent, len(node[1]))
                    output.extend(drain_chld(combined, level, origin))
                elif kind in {"&&", "||"}:
                    for tested in evaluate(node[1], [state], level + 1, origin, tested=True):
                        take = tested["status"]
                        if kind == "||" and take is not None:
                            take = not take
                        if take is not False:
                            output.extend(evaluate(node[2], [clone(tested)], level + 1, origin))
                        if take is not True:
                            output.append(tested)
                elif kind == "if":
                    for tested in evaluate(node[1], [state], level + 1, origin, tested=True):
                        if tested["done"]:
                            output.append(tested)
                            continue
                        if tested["status"] is not False:
                            output.extend(evaluate(node[2], [clone(tested)], level + 1, origin))
                        if tested["status"] is not True:
                            output.extend(evaluate(node[3], [clone(tested)], level + 1, origin))
                elif kind == "case":
                    trace_prefix(state, level, origin)
                    if state['done']:
                        output.append(state)
                        continue
                    subject = expand_word(node[1], state, level, origin)
                    pending = drain_chld([state], level, origin)
                    for patterns, body in node[2]:
                        matched = []
                        # Bash expands each alternative only if no earlier
                        # pattern matched, including its command substitutions.
                        for pattern in patterns:
                            next_pending = []
                            for current in pending:
                                if current['done']:
                                    output.append(current)
                                    continue
                                option = expand_word(pattern, current, level, origin, pattern=True)
                                uncertain = isinstance(subject, Unknown) or isinstance(option, Unknown)
                                matches = None
                                if not isinstance(option, Unknown) and option == '*':
                                    matches = True
                                elif not uncertain and not current['done']:
                                    selected_dialect = stage.sh_dialect if dialect == 'sh' else dialect
                                    # The supported ash/dash interpreters match
                                    # bytes, even with a UTF-8 environment.
                                    locales = ['C', 'C']
                                    if selected_dialect not in {'ash', 'dash'}:
                                        locales = []
                                        for category in ['LC_CTYPE', 'LC_COLLATE']:
                                            locale = next((current['vars'][name] for name in ['LC_ALL', category, 'LANG']
                                                           if current['vars'].get(name) and not isinstance(current['vars'][name], Unset)), 'C')
                                            locales.append(locale)
                                    try:
                                        if any(isinstance(locale, Unknown) for locale in locales):
                                            raise ValueError('Unknown shell pattern locale')
                                        matches = match_pattern(subject, option, ctype=locales[0], collate=locales[1],
                                                                caret_negates=selected_dialect != 'dash',
                                                                collating_symbols=selected_dialect != 'dash')
                                    except ValueError as error:
                                        unknown(current, str(error))
                                for tested in drain_chld([current], level, origin):
                                    if tested['done']:
                                        output.append(tested)
                                        continue
                                    if matches is not False:
                                        matched.append(clone(tested))
                                    if matches is not True:
                                        next_pending.append(tested)
                            pending = next_pending
                            if not pending:
                                break
                        output.extend(evaluate(body, matched, level + 1, origin))
                    for current in pending:
                        if not current['done']:
                            set_status(current, True)
                        output.append(current)
                elif kind == "for":
                    values = state["vars"].get("@", []) if node[2] is None else [value for word in node[2] for value in word_fields(word, state, level, origin)]
                    parents = drain_chld([state], level, origin)
                    if len(values) > 64:
                        output.append(unknown(state, "Shell loop limit reached"))
                    else:
                        loop_depth = state['loop_depth']
                        for parent in parents:
                            parent['loop_depth'] = loop_depth + 1
                        branches = [parent for parent in parents if not parent['done']]
                        finished = [parent for parent in parents if parent['done']]
                        if not values:
                            for branch in branches:
                                set_status(branch, True)
                        for value in values:
                            for branch in branches:
                                trace_prefix(branch, level, origin)
                                if branch['done']:
                                    continue
                                branch["vars"][node[1]] = value
                                assigned(branch, node[1])
                            current = evaluate(node[3], branches, level + 1, origin)
                            branches = []
                            for branch in current:
                                if branch["done"] == "break":
                                    branch["done"] = None
                                    finished.append(branch)
                                elif branch['done'] not in {None, 'continue'}:
                                    finished.append(branch)
                                else:
                                    if branch["done"] == "continue":
                                        branch["done"] = None
                                    branches.append(branch)
                        for branch in finished + branches:
                            branch['loop_depth'] = loop_depth
                            output.append(branch)
                elif kind in {"while", "until"}:
                    loop_depth, output_start = state['loop_depth'], len(output)
                    state['loop_depth'] += 1
                    def freeze(value):
                        if isinstance(value, Word):
                            return ('word', value.parts, value.quoted, value.protected)
                        if isinstance(value, dict):
                            return tuple(sorted((key, freeze(item)) for key, item in value.items()))
                        if isinstance(value, (list, tuple)):
                            return tuple(freeze(item) for item in value)
                        if isinstance(value, set):
                            return frozenset(freeze(item) for item in value)
                        return (type(value).__name__, value)

                    def loop_key(current):
                        stage = current['stage']
                        return freeze((current['vars'], current['functions'], current['readonly'], current['options'],
                                       current['exported'], current['environment_closed'],
                                       current['exit_trap'], current['chld_trap'], current['pending_chld'],
                                       current['deferred_chld'], current['in_chld_trap'], current['ignore_errexit'],
                                       current['locals'], current['status'], current.get('raw_status'), stage.cwd, stage.files,
                                       stage.directories, stage.env))

                    # The loop returns its last body status (zero if the body
                    # never ran), not the status that ended its condition.
                    pending = [(state, frozenset(), (True, None))]
                    for iteration in range(128):
                        next_pending = []
                        for current, seen, last_status in pending:
                            key = loop_key(current)
                            if key in seen:
                                output.append(unknown(current, "Nonterminating shell loop"))
                                continue
                            history = seen | {key}
                            for tested in evaluate(node[1], [current], level + 1, origin, tested=True):
                                take = tested["status"]
                                if kind == "until" and take is not None:
                                    take = not take
                                if tested['done'] == 'break':
                                    tested['done'] = None
                                    output.append(tested)
                                    continue
                                if tested['done'] == 'continue':
                                    tested['done'] = None
                                    next_pending.append((tested, history, last_status))
                                    continue
                                if tested["done"]:
                                    output.append(tested)
                                    continue
                                if take is not True:
                                    finished = clone(tested)
                                    set_status(finished, *last_status)
                                    output.append(finished)
                                if take is not False:
                                    for branch in evaluate(node[2], [tested], level + 1, origin):
                                        if branch["done"] == "break":
                                            branch["done"] = None
                                            output.append(branch)
                                        elif branch["done"] in {None, "continue"}:
                                            branch["done"] = None
                                            if take is None:
                                                output.append(branch)
                                            else:
                                                next_pending.append((branch, history, (branch['status'], branch.get('raw_status'))))
                                        else:
                                            output.append(branch)
                        pending = next_pending
                        if not pending:
                            break
                    for branch, _, _ in pending:
                        # A budget is not a proof of nontermination. Discard
                        # earlier Go credit and surface uncertainty; never
                        # invent execution/cheating evidence after this point.
                        output.append(unknown(branch, "Shell loop analysis limit reached"))
                    for branch in output[output_start:]:
                        branch['loop_depth'] = loop_depth
                else:
                    output.append(unknown(state, "Unsupported shell control flow"))
            if len(output) > 128:
                return [unknown(state, "Shell branch limit reached") for state in output[:128]]
            if kind in {'cmd', 'subshell', 'pipe'}:
                for state in output:
                    if (state['options']['errexit'] and not state['ignore_errexit']
                            and not state.get('startup_errexit_inhibited')
                            and state['status'] is False and state['done'] in {None, 'return'}
                            and not (state['done'] == 'return' and state.get('startup_file'))):
                        state['done'] = 'exit'
                        state['exit_reason'] = 'errexit'
            return output

        initial = {"stage": Stage(stage.cwd, dict(stage.files), env=dict(stage.env), directories=set(stage.directories)),
                   "vars": dict(variables), "functions": {}, "readonly": set(), "go": False, "done": None,
                   "status": True, "output": [], "locals": [], "options": dict(options or shell_options([])[0]),
                   "loop_depth": 0,
                   "ignore_errexit": False, "exit_trap": None, "chld_trap": None,
                   "pending_chld": 0, "deferred_chld": 0, "in_chld_trap": False,
                   "expansion_status": '127' if dialect == 'bash' and command_string else '1' if dialect == 'bash' else '2',
                   "readonly_status": '1' if dialect == 'bash' and command_string else '2'}
        initial['environment_closed'] = environment_closed
        initial['exported'] = {name for name, value in variables.items()
                               if name != '@' and not name.isdigit() and not isinstance(value, Unset)}
        initial['stage'].sh_dialect = stage.sh_dialect
        initial['vars']['IFS'] = ' \t\n'
        initial['vars']['PWD'] = stage.cwd
        initial['exported'].add('PWD')
        initial['vars']['OPTIND'] = '1'
        initial['vars']['PPID'] = Unknown('$PPID')
        if isinstance(variable(initial, 'PS4'), Unset) or variable(initial, 'PS4') is None:
            initial['vars']['PS4'] = '+ '
        if dialect == 'bash':
            initial['vars']['OPTERR'] = '1'
            # Noninteractive Bash discards inherited interactive prompts.
            for name in ['PS1', 'PS2']:
                initial['vars'][name] = Unset('')
                initial['exported'].discard(name)
            for name in ['UID', 'EUID', 'BASHPID', 'BASH_VERSION', 'BASHOPTS', 'SHELLOPTS', 'LINENO', 'SECONDS', 'RANDOM', 'SHLVL']:
                initial['vars'][name] = Unknown('$' + name)
            initial['readonly'].add('SHELLOPTS')
        elif dialect == 'ash' or dialect == 'sh' and stage.sh_dialect == 'ash':
            for name in ['LINENO', 'RANDOM', 'SHLVL']:
                initial['vars'][name] = Unknown('$' + name)
        if dialect in {'ash', 'dash'} or dialect == 'sh' and stage.sh_dialect in {'ash', 'dash'}:
            for name, default in [('PS1', Unknown('$PS1')), ('PS2', '> ')]:
                if isinstance(variable(initial, name), Unset) or variable(initial, name) is None:
                    initial['vars'][name] = default
        inherited_path = initial['vars'].get('PATH')
        if inherited_path is None and not environment_closed:
            # PATH is defined on shell startup. Keep its inherited value
            # symbolic while retaining the inspector's existing search-path
            # fallback for resolving bare executable names.
            initial['vars']['PATH'] = Unknown('/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin')
            initial['exported'].add('PATH')
        elif inherited_path is None or isinstance(inherited_path, Unset):
            ash = dialect == 'ash' or dialect == 'sh' and stage.sh_dialect == 'ash'
            initial['vars']['PATH'] = '/sbin:/usr/sbin:/bin:/usr/bin' if ash else '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
        startup = dict(startup or {})
        fresh = startup.get('fresh', True)
        invocation_name = startup.get('name', dialect)
        if isinstance(invocation_name, Unknown):
            unknown(initial, 'Unknown interpreter argv[0]')
        if dialect == 'bash' and fresh and not initial['options']['privileged']:
            inherited = variables.get('SHELLOPTS')
            if isinstance(inherited, Unknown):
                unknown(initial, 'Unknown inherited SHELLOPTS')
            elif inherited is not None and not isinstance(inherited, Unset):
                # Bash imports these *after* parsing command-line switches;
                # an inherited option can re-enable a command-line +u/+e/+n.
                passive = {'braceexpand', 'hashall', 'interactive-comments'}
                unsupported = {'emacs', 'errtrace', 'functrace', 'histexpand', 'history', 'ignoreeof',
                               'keyword', 'monitor', 'noclobber', 'nolog', 'notify',
                               'onecmd', 'physical', 'vi'}
                for name in inherited.split(':'):
                    if name in initial['options']:
                        initial['options'][name] = True
                    elif name in unsupported:
                        unknown(initial, 'Unsupported inherited shell option ' + name)
                    # Invalid names only print an error; Bash still executes.
                    elif name and name not in passive:
                        continue
        if dialect == 'bash' and posixpath.basename(invocation_name.lstrip('-')) == 'sh':
            initial['options']['posix'] = True
        refresh_shellopts(initial)
        if (dialect == 'dash' or dialect == 'sh' and stage.sh_dialect == 'dash') and initial['options']['pipefail']:
            if status_out is not None:
                status_out[:] = [(False, '2')]
            return False
        if initial['options']['noexec']:
            if status_out is not None:
                status_out[:] = [(True, '0')]
            return False
        states = [initial]
        if (dialect == 'bash' and fresh and not initial['done']
                and not initial['options']['privileged']):
            login = startup.get('login') or invocation_name.startswith('-')
            if login and not startup.get('noprofile'):
                unknown(initial, 'Unresolved Bash login profile')
            if not initial['options']['posix'] and not initial['done']:
                filename = variable(initial, 'BASH_ENV')
                if filename is not None and not isinstance(filename, Unset) and filename:
                    if isinstance(filename, Unknown):
                        unknown(initial, 'Unknown Bash startup filename')
                    else:
                        try:
                            word = shell_words(filename, dialect='bash', expand_only=True)[0]
                            filename = expand_word(word, initial, 0, source)
                        except (ValueError, RecursionError) as error:
                            unknown(initial, str(error))
                        if not initial['done']:
                            if isinstance(filename, Unknown):
                                unknown(initial, 'Unknown expanded Bash startup filename')
                            elif filename:
                                found = initial['stage'].lookup(filename)
                                file = local_file(root, found[1]) if found and found[0] == 'source' else None
                                if file:
                                    initial['startup_file'] = True
                                    # Invocation -e is initially inhibited in
                                    # BASH_ENV; an explicit set -e activates it.
                                    initial['startup_errexit_inhibited'] = True
                                    origin = str(file.relative_to(root))
                                    states = read_file(file, states, 0, origin)
                                    for state in states:
                                        state['startup_file'] = False
                                        state.pop('startup_errexit_inhibited', None)
                                        if state['done'] in {'return', 'startup_error'}:
                                            state['done'] = None
                                elif found and found[0] == 'source':
                                    try:
                                        if not (root / found[1]).resolve().is_relative_to(root.resolve()):
                                            unknown(initial, 'Bash startup file escapes source root')
                                    except (OSError, RuntimeError):
                                        unknown(initial, 'Unresolved Bash startup file ' + filename)
                                elif (found and found[0] != 'directory'
                                      or not found and not path_at(filename, initial['stage'].cwd).startswith('/action/')):
                                    unknown(initial, 'Unresolved Bash startup file ' + filename)
        outcomes = []
        for state in states:
            program = parse_program(text, state) if not state['done'] else None
            outcomes.extend(evaluate(program, [state]) if program else [state])
        states = outcomes
        states = finish_process(states, 0, source)
        if status_out is not None:
            statuses = [(process_status(state), status_value(state, process=True)) for state in states]
            status_out[:] = [statuses[0] if statuses and all(status == statuses[0] for status in statuses) else (None, Unknown('$?'))]
        return bool(states) and all(state["go"] and state["done"] != "unsupported" for state in states)

    if using == "docker":
        image = re.search(r"(?m)^\s*image:\s*(.+)$", block)
        image = scalar(image[1]) if image else ""
        file = local_file(root, image)
        if file is None:
            result["unresolved"].append("Action Docker build file missing")
            return result
        stage = recipe(root, file.read_text())
        words = (argv(stage.entry) + (argv(stage.cmd) if stage.entry.lstrip().startswith("[") else [])
                 if stage.entry and stage.entry != "[]" else argv(stage.cmd))
        result["go"] = executable(words, stage, dict(stage.env), image)
    elif using == "composite":
        lines = block.splitlines()
        step_markers = [(index, len(match[1])) for index, line in enumerate(lines)
                        if (match := re.match(r'^(\s*)-\s+\w+:', line))]
        for index, line in enumerate(lines):
            match = re.match(r"^(\s*)(?:-\s*)?run:\s*(.*)$", line)
            if not match:
                continue
            command = match[2]
            if command in {"|", ">", "|-", ">-"}:
                captured = []
                for following in lines[index + 1:]:
                    if following.strip() and len(following) - len(following.lstrip()) <= len(match[1]):
                        break
                    captured.append(following)
                command = "\n".join(captured)
            else:
                command = scalar(command)
            command = re.sub(r"\$\{\{\s*github\.action_path\s*\}\}", "/action", command)
            dialect = 'sh'
            selected_options = None
            preceding = [(start, indent) for start, indent in step_markers if start <= index and indent <= len(match[1])]
            if preceding:
                start, indent = preceding[-1]
                end = next((stop for stop, level in step_markers if stop > index and level == indent), len(lines))
                for step_line in lines[start:end]:
                    selector = re.match(r'^(\s*)(?:-\s*)?shell:\s*(.+)$', step_line)
                    if selector and len(selector[1]) <= indent + 2:
                        selection = scalar(selector[2]).split()
                        if selection:
                            dialect = posixpath.basename(selection[0])
                            try:
                                arguments = (['-e', '-o', 'pipefail'] if dialect == 'bash' else ['-e']) if len(selection) == 1 else selection[1:]
                                selected_options = shell_options(arguments, invocation=True, dialect=dialect)[0]
                            except ValueError as error:
                                incomplete = True
                                result['unresolved'].append(str(error))
            stage = Stage("/action", {"/action": ("directory", ".")}, directories={"/", "/action"})
            variables = {"GITHUB_ACTION_PATH": "/action", "PWD": "/action"}
            result["go"] = shell(command, stage, variables, "action.yml", dialect=dialect,
                                 options=selected_options) or result["go"]
    elif NODE.fullmatch(using):
        result["source_runtime_hits"].append({"path": "action.yml", "sink": "action-entry", "token": using, "evidence": using})
    if incomplete:
        result["go"] = False
    return result
