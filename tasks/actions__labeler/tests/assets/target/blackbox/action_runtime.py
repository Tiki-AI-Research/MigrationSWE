"""Exercise the declared action wrapper without requiring Docker-in-Docker.

Composite scripts really execute from a caller workspace with per-step command
files. Docker build stages compile Go with their copied inputs, declared flags
and environment. The resulting artifact and repository wrappers execute on the
verifier host with the image PATH mapped to materialized files. Base-image
packages, system libraries and user permissions are not reproduced. This is
not a Docker image build or a complete
GitHub runner. Unsupported metadata/recipes fail explicitly, never silently
fall back to executing cmd/labeler directly.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass, field
import fnmatch
import json
import os
from pathlib import Path
import posixpath
import re
import shlex
import subprocess
import tempfile

import yaml

from action_expressions import evaluate as expression, string as scalar, truthy
from docker_build import run as trace_build_commands, execute_go
from process_commands import NODE_RUNTIMES, runtime_in_argv, runtime_in_shell


class RuntimeContractError(ValueError):
    pass


def load_action(root):
    action = yaml.safe_load((Path(root) / "action.yml").read_text())
    if not isinstance(action, dict) or not isinstance(action.get("runs"), dict):
        raise RuntimeContractError("action.yml must declare runs")
    return action


def render(value, context):
    return re.sub(r"\$\{\{\s*(.*?)\s*\}\}", lambda m: scalar(expression(m[1], context)), scalar(value))


def validate_metadata(action):
    runs = action["runs"]
    if runs.get("using") == "docker":
        if not scalar(runs.get("image")).strip():
            raise RuntimeContractError("Docker action must declare its image")
    elif runs.get("using") == "composite":
        if not runs.get("steps"):
            raise RuntimeContractError("composite action must declare executable steps")
    else:
        raise RuntimeContractError("action runtime must execute the Go action via docker or composite")


@dataclass
class Stage:
    files: dict = field(default_factory=dict)  # container path -> (source|go, repository path)
    env: dict = field(default_factory=lambda: {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'})
    workdir: str = "/"
    entry: list = field(default_factory=list)
    command: list = field(default_factory=list)
    directories: set = field(default_factory=lambda: {'/'})
    shell: list = field(default_factory=lambda: ['/bin/sh', '-c'])


def absolute(path, cwd):
    return posixpath.normpath(posixpath.join(cwd, path))


def expand(text, environment):
    return re.sub(r"\$(?:\{(\w+)\}|(\w+))", lambda m: environment.get(m[1] or m[2], m[0]), text)


def repository_files(root):
    ignored = {".git", ".gomodcache", ".gocache", "node_modules", "blackbox", "target", "__pycache__"}
    patterns = (root / ".dockerignore").read_text().splitlines() if (root / ".dockerignore").exists() else []
    result = {}
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ignored]
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            excluded = False
            for pattern in patterns:
                pattern = pattern.strip()
                if not pattern or pattern.startswith("#"):
                    continue
                negate = pattern.startswith("!")
                pattern = pattern.lstrip("!").strip("/")
                if any(fnmatch.fnmatchcase(prefix, pattern) for prefix in
                       [relative] + ["/".join(relative.split("/")[:i]) for i in range(1, len(relative.split("/")))]):
                    excluded = not negate
            if not excluded and path.is_file():
                result[relative] = ("source", str(path))
    return result


def docker_plan(root, action, compiler=None):
    """Trace artifact provenance through arbitrary stage names and binary names."""
    image = scalar(action["runs"]["image"])
    recipe = root / image
    if image.startswith("docker://") or not recipe.is_file():
        raise RuntimeContractError("cannot inspect Docker action image recipe: " + image)
    context = repository_files(root)
    stages, names, stage = [], {}, None
    logical = re.sub(r"\\\s*\n", " ", recipe.read_text())
    for raw in logical.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        instruction, _, body = raw.strip().partition(" ")
        instruction = instruction.upper()
        if instruction == "FROM":
            parts = shlex.split(body)
            parts = [p for p in parts if not p.startswith("--platform=")]
            parent = names.get(parts[0])
            stage = Stage(dict(parent.files), dict(parent.env), parent.workdir,
                          list(parent.entry), list(parent.command), set(parent.directories), list(parent.shell)) if parent else Stage()
            stages.append(stage)
            names[str(len(stages) - 1)] = stage
            if len(parts) > 2 and parts[-2].upper() == "AS":
                names[parts[-1]] = stage
            continue
        if stage is None:
            if instruction == "ARG":
                continue
            raise RuntimeContractError("Docker recipe instruction before FROM")
        if instruction in {"ARG", "ENV"}:
            parts = shlex.split(body)
            if instruction == "ENV" and parts and "=" not in parts[0]:
                parts = [parts[0] + "=" + " ".join(parts[1:])]
            for part in parts:
                if "=" in part:
                    key, value = part.split("=", 1)
                    stage.env[key] = expand(value, stage.env)
        elif instruction == "WORKDIR":
            stage.workdir = absolute(expand(body, stage.env), stage.workdir)
            stage.directories.add(stage.workdir)
        elif instruction in {"COPY", "ADD"}:
            origin = context
            while body.startswith("--"):
                flag, _, body = body.partition(" ")
                if flag.startswith("--from="):
                    source = names.get(flag.split("=", 1)[1])
                    if source is None:
                        raise RuntimeContractError("unresolved Docker COPY stage: " + flag)
                    origin = source.files
            parts = json.loads(body) if body.startswith("[") else shlex.split(body)
            parts = [expand(part, stage.env) for part in parts]
            destination = absolute(parts[-1], stage.workdir)
            for source in parts[:-1]:
                source = posixpath.normpath(source).lstrip("/") if origin is context else absolute(source, "/")
                matches = [(p, value) for p, value in origin.items()
                           if p == source or p.startswith(source.rstrip("/") + "/") or source == "."
                           or fnmatch.fnmatchcase(p, source)]
                directory = source == "." or any(p.startswith(source.rstrip("/") + "/") for p, _ in matches)
                for path, value in matches:
                    suffix = path if source == "." else path[len(source):].lstrip("/")
                    target = posixpath.join(destination, suffix) if directory else (
                        posixpath.join(destination, posixpath.basename(path)) if parts[-1].endswith("/") or len(parts) > 2 else destination)
                    stage.files[posixpath.normpath(target)] = value
        elif instruction == "RUN":
            if body.startswith('['):
                arguments = json.loads(body)
                if not arguments or not all(isinstance(arg, str) for arg in arguments):
                    raise RuntimeContractError('Docker exec RUN requires a nonempty string array')
                trace_build_commands(stage, '', root, compiler, argv=arguments)
            else:
                trace_build_commands(stage, body, root, compiler)
        elif instruction == 'SHELL':
            stage.shell = json.loads(body)
            if not stage.shell or posixpath.basename(stage.shell[0]) not in {'sh', 'bash', 'dash'}:
                raise RuntimeContractError('unsupported Docker build shell')
        elif instruction in {"ENTRYPOINT", "CMD"}:
            command = json.loads(body) if body.startswith("[") else ["sh", "-c", body]
            if instruction == "ENTRYPOINT":
                stage.entry, stage.command = command, []
            else:
                stage.command = command
    if stage is None:
        raise RuntimeContractError("empty Docker recipe")
    if action["runs"].get("entrypoint"):
        stage.entry = [scalar(action["runs"]["entrypoint"])]
    argv = stage.entry + list(action["runs"].get("args", stage.command))
    if not argv:
        raise RuntimeContractError("Docker final stage declares no executable entrypoint")
    hit = runtime_in_argv(argv)
    if hit in NODE_RUNTIMES or hit == "index.js":
        raise RuntimeContractError("Docker entrypoint invokes source runtime: " + hit)
    if not any(kind == "go" for kind, _ in stage.files.values()):
        raise RuntimeContractError("Docker final stage has no artifact built from cmd/labeler")
    return stage, argv



class ActionRuntime:
    def __init__(self, root, binary, parse_outputs):
        self._build_directory = None
        self._docker_plan = None
        self.root = Path(root).resolve()
        self.action = load_action(self.root)
        self.binary, self.parse_outputs = binary, parse_outputs
        validate_metadata(self.action)

    def close(self):
        if self._build_directory is not None:
            self._build_directory.cleanup()
            self._build_directory = None

    def __del__(self):
        self.close()

    def run(self, environment, workspace):
        try:
            with tempfile.TemporaryDirectory(prefix="labeler-wrapper-") as temporary:
                inputs = {name: environment.get("INPUT_" + name.upper(), scalar(spec.get("default")))
                          for name, spec in self.action.get("inputs", {}).items()}
                env = dict(environment)
                env.update(GITHUB_ACTION_PATH=str(self.root), GITHUB_WORKSPACE=str(workspace), RUNNER_TEMP=temporary)
                context = {"inputs": inputs, "github": {"action_path": str(self.root), "workspace": str(workspace), "token": inputs.get("repo-token", "")},
                           "runner": {"os": "Linux", "temp": temporary}, "steps": {}, "env": env}
                if self.action["runs"]["using"] == "composite":
                    return self.composite(env, str(workspace), context, Path(temporary))
                return self.docker(env, str(workspace), context, Path(temporary))
        except (RuntimeContractError, OSError, ValueError, subprocess.TimeoutExpired) as error:
            return 1, "action wrapper: " + str(error), {}

    def composite(self, env, workspace, context, temporary):
        # Composite inputs are contexts, not automatically exported INPUT_ vars.
        env = {key: value for key, value in env.items() if not key.startswith("INPUT_")}
        context["env"] = env
        logs = []
        for index, step in enumerate(self.action["runs"]["steps"]):
            condition = step.get("if")
            if condition is not None and not truthy(expression(
                    re.sub(r"^\s*\$\{\{|\}\}\s*$", "", scalar(condition)).strip(), context)):
                continue
            if "uses" in step:
                if scalar(step["uses"]).startswith("actions/setup-go@"):
                    continue  # verifier already provides the required Go toolchain
                raise RuntimeContractError("offline wrapper cannot resolve action step: " + scalar(step["uses"]))
            if "run" not in step or "shell" not in step:
                raise RuntimeContractError("composite run step must declare run and shell")
            output = temporary / ("output-" + str(index))
            output.touch()
            environment_file = temporary / ("env-" + str(index))
            path_file = temporary / ("path-" + str(index))
            environment_file.touch()
            path_file.touch()
            step_env = dict(env)
            step_env.update({key: render(value, context) for key, value in step.get("env", {}).items()})
            step_env["GITHUB_OUTPUT"] = str(output)
            step_env["GITHUB_ENV"] = str(environment_file)
            step_env["GITHUB_PATH"] = str(path_file)
            script = temporary / ("step-" + str(index) + ".sh")
            text = render(step["run"], context)
            hit = runtime_in_shell(text)
            if hit in NODE_RUNTIMES:
                raise RuntimeContractError("composite executes source runtime: " + hit)
            script.write_text(text)
            shell = render(step["shell"], context)
            if shell == "bash":
                argv = ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", str(script)]
            elif shell == "sh":
                argv = ["sh", "-e", str(script)]
            else:
                argv = shlex.split(shell.replace("{0}", str(script)))
            cwd = render(step.get("working-directory", workspace), context)
            proc = subprocess.run(argv, cwd=cwd, env=step_env, capture_output=True, text=True, timeout=120)
            logs.append(proc.stdout + proc.stderr)
            values = self.parse_outputs(output.read_text())
            if step.get("id"):
                context["steps"][step["id"]] = {"outputs": values}
            if proc.returncode:
                return proc.returncode, "\n".join(logs), {}
            env.update(self.parse_outputs(environment_file.read_text()))
            additions = path_file.read_text().splitlines()
            if additions:
                env["PATH"] = os.pathsep.join(reversed(additions)) + os.pathsep + env.get("PATH", "")
        outputs = {name: render(spec.get("value", ""), context) for name, spec in self.action.get("outputs", {}).items()}
        return 0, "\n".join(logs), outputs

    def docker(self, env, workspace, context, temporary):
        if self._docker_plan is None:
            self._build_directory = tempfile.TemporaryDirectory(prefix='labeler-docker-build-')
            def compiler(stage, words, cwd, environment):
                return execute_go(stage, words, cwd, environment, self.root, Path(self._build_directory.name))
            self._docker_plan = docker_plan(self.root, self.action, compiler)
        stage, declared_argv = self._docker_plan
        argv = list(declared_argv)
        paths = {}
        for container_path, (kind, source) in stage.files.items():
            target = temporary / 'image' / container_path.lstrip('/')
            target.parent.mkdir(parents=True, exist_ok=True)
            if kind == "go":
                # This is the artifact built with the Docker stage's copied
                # context, flags and environment, never the unrelated CLI build.
                target.symlink_to(source)
            else:
                target.write_bytes(Path(source).read_bytes())
                target.chmod(0o755 if os.access(source, os.X_OK) else 0o644)
            paths[container_path] = str(target)
        # Repository scripts keep their chosen names; map container paths to
        # their materialized counterparts rather than prescribing an entry name.
        def mapped(text):
            if paths:
                pattern = "|".join(re.escape(path) for path in sorted(paths, key=len, reverse=True))
                text = re.sub(pattern, lambda match: paths[match[0]], text)
            return text.replace("/github/workspace", workspace)
        for container_path, (kind, source) in stage.files.items():
            if kind == "source" and (Path(source).suffix in {".sh", ".bash"} or Path(source).read_bytes().startswith(b"#!")):
                script = Path(paths[container_path])
                text = script.read_text()
                hit = runtime_in_shell(text)
                if hit in NODE_RUNTIMES:
                    raise RuntimeContractError("Docker wrapper executes source runtime: " + hit)
                script.write_text(mapped(text))
        image_path = expand(stage.env.get('PATH', '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'), env)
        argv = [mapped(render(arg, context)) for arg in argv]
        # Materialized image directories let the OS resolve both a bare
        # entrypoint and bare commands in scripts using the image PATH order.
        image_directories = [str(temporary / 'image' / absolute(directory or '/github/workspace', '/').lstrip('/'))
                             for directory in image_path.split(':')]
        env['PATH'] = os.pathsep.join(image_directories + [env.get('PATH', '/usr/bin:/bin')])
        env.update({key: mapped(expand(value, env)) for key, value in stage.env.items() if key != 'PATH'})
        env.update({key: render(value, context) for key, value in self.action["runs"].get("env", {}).items()})
        proc = subprocess.run(argv, cwd=workspace, env=env, capture_output=True, text=True, timeout=120)
        output = Path(env["GITHUB_OUTPUT"])
        return proc.returncode, proc.stdout + proc.stderr, self.parse_outputs(output.read_text())


def verify_entrypoint(root):
    # Import the real API fixture and exercise an actual caller-local relative
    # configuration, not an absolute path that hides wrapper cwd mistakes.
    import labeler_blackbox as blackbox
    blackbox.REPO = str(Path(root).resolve())
    runtime = ActionRuntime(root, blackbox.build_binary(), blackbox.parse_outputs)
    state = blackbox.State()
    server = blackbox.start_server(state)
    state.api_url = "http://127.0.0.1:%d" % server.server_address[1]
    state.content = "wrong-remote-label: [{head-branch: main}]\n"
    try:
        verify_runtime_inputs(runtime, blackbox, state)
    finally:
        runtime.close()
        server.shutdown()
        server.server_close()


def verify_runtime_inputs(runtime, blackbox, state):
    """Exercise the public wrapper with both default and non-default inputs."""
    def check(name, failures):
        if failures:
            raise RuntimeContractError('declared action input contract (%s): %s' % (name, '\n'.join(failures)))

    def local_configuration(**options):
        code, log, outputs = blackbox.run_action(runtime.run, state, blackbox.BASIC,
                                                changed=["docs/a.md"], relative_config=True, **options)
        if code or outputs.get("new-labels") != "documentation" or outputs.get("all-labels") != "documentation":
            raise RuntimeContractError("declared action did not expose expected labels: exit=%s outputs=%s\n%s" % (code, outputs, log[-3000:]))
        if not state.added or any("/contents/" in path for _, path in state.requests):
            raise RuntimeContractError("declared action did not use the caller-local configuration and label API")

    local_configuration()
    blackbox._fresh(state)
    local_configuration(local_config_path='.github/custom label rules.yml', token='wrapper-contract-token')
    def authorized(header):
        scheme, _, credentials = header.partition(' ')
        if scheme.lower() in {'token', 'bearer'}: return credentials == 'wrapper-contract-token'
        if scheme.lower() == 'basic':
            try: return base64.b64decode(credentials).decode().rsplit(':', 1)[-1] == 'wrapper-contract-token'
            except (ValueError, UnicodeError): return False
        return False
    if not state.authorizations or not all(map(authorized, state.authorizations)):
        raise RuntimeContractError('declared action did not forward repo-token to the GitHub API')
    check('sync-labels and outputs', blackbox._outputs(runtime.run, state, (False, True)))
    check('dot', blackbox._runtime_inputs(runtime.run, state))
    for pr_input in ['347\n 619 \n\n', '']:
        blackbox._fresh(state, pr_number=347)
        failures = []
        blackbox._expect(runtime.run, state, failures, blackbox.BASIC, 'documentation', pr_input=pr_input,
                         settings={'pr_number': 347})
        numbers = [347, 619] if pr_input else [347]
        fetched = {int(path.split('?')[0].rsplit('/', 1)[-1]) for method, path in state.requests
                   if method == 'GET' and re.search(r'/pulls/\d+(?:\?|$)', path)}
        if fetched != set(numbers): failures.append('requested PRs %r, fetched %r' % (numbers, sorted(fetched)))
        check('pr-number', failures)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--metadata-only", action="store_true")
    args = parser.parse_args()
    try:
        root = Path(args.repo).resolve()
        action = load_action(root)
        validate_metadata(action)
        if action["runs"]["using"] == "docker":
            docker_plan(root, action)
        if not args.metadata_only:
            verify_entrypoint(root)
    except (RuntimeContractError, OSError, ValueError, RuntimeError) as error:
        print(error)
        raise SystemExit(1)
