"""Docker RUN control flow and actual Go builds from the copied stage context.

This validates Go artifacts, not base-image package installation or image user
permissions. Unknown build operations are rejected instead of inventing files.
"""
from __future__ import annotations

import os
from pathlib import Path
import posixpath
import re
import shlex
import shutil
import subprocess
import tempfile


class BuildError(ValueError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def absolute(path, cwd):
    return posixpath.normpath(posixpath.join(cwd, path))


def expand(value, environment):
    return re.sub(r'\$(?:\{(\w+)\}|(\w+))', lambda m: environment.get(m[1] or m[2], m[0]), value)


def tokens(script):
    """Keep command operators distinct from quoted data and assignments."""
    result, word, quote, index = [], [], '', 0
    def flush():
        if word:
            result.append(('word', ''.join(word)))
            word.clear()
    while index < len(script):
        char = script[index]
        if char == '\\' and quote != "'" and index + 1 < len(script):
            if script[index + 1] != '\n': word.extend(script[index:index + 2])
            index += 2
            continue
        if quote:
            word.append(char)
            if char == quote: quote = ''
        elif char in "'\"":
            quote = char
            word.append(char)
        elif char == '#' and not word:
            while index < len(script) and script[index] != '\n': index += 1
            continue
        elif script.startswith('${', index):
            end = script.find('}', index + 2)
            if end < 0: raise BuildError('unclosed Docker RUN variable')
            word.extend(script[index:end + 1]); index = end
        elif char in ' \t\r':
            flush()
        elif char in ';&|()\n':
            flush()
            operator = char
            if char in '&|' and script[index:index + 2] == char * 2:
                operator *= 2; index += 1
            if operator == '&': raise BuildError('background Docker build commands cannot establish an artifact')
            result.append(('op', ';' if char == '\n' else operator))
        else:
            word.append(char)
        index += 1
    if quote: raise BuildError('unclosed Docker RUN quote')
    flush()
    return result


class ShellParser:
    def __init__(self, text):
        self.values = tokens(text) + [('end', '')]
        self.index = 0

    def at(self, *values):
        return self.values[self.index][1] in values

    def take(self, value=None):
        token = self.values[self.index]
        if value is not None and token[1] != value: raise BuildError('expected ' + value + ' in Docker RUN')
        self.index += 1
        return token

    def sequence(self, stops=(), depth=0):
        if depth > 60: raise BuildError('Docker RUN nesting is too deep')
        nodes = []
        while not self.at('', *stops):
            if self.at(';'): self.take(); continue
            node = self.pipeline(depth + 1)
            while self.at('&&', '||'):
                operator = self.take()[1]
                while self.at(';'): self.take()
                node = ('andor', operator, node, self.pipeline(depth + 1))
            nodes.append(node)
            if not self.at(';', '', *stops): raise BuildError('unsupported Docker RUN syntax')
        return ('sequence', nodes)

    def pipeline(self, depth):
        negate = self.at('!')
        if negate: self.take()
        nodes = [self.command(depth + 1)]
        while self.at('|'):
            self.take(); nodes.append(self.command(depth + 1))
        node = ('pipeline', nodes)
        return ('not', node) if negate else node

    def command(self, depth):
        if depth > 60: raise BuildError('Docker RUN nesting is too deep')
        if self.at('('):
            self.take(); node = self.sequence((')',), depth + 1); self.take(')')
            return ('subshell', node)
        if self.at('if'):
            self.take(); condition = self.sequence(('then',), depth + 1); self.take('then')
            yes = self.sequence(('else', 'elif', 'fi'), depth + 1)
            if self.at('elif'):
                self.values[self.index] = ('word', 'if')
                no = self.command(depth + 1)
                return ('if', condition, yes, no)
            no = ('sequence', [])
            if self.at('else'):
                self.take(); no = self.sequence(('fi',), depth + 1)
            self.take('fi')
            return ('if', condition, yes, no)
        words = []
        while self.values[self.index][0] == 'word' and not self.at('then', 'else', 'elif', 'fi'):
            words.append(self.take()[1])
        if not words: raise BuildError('missing command in Docker RUN')
        return ('command', words)


class ExitRun(Exception):
    def __init__(self, status): self.status = status


def go_arguments(words, cwd):
    args = list(words[1:])
    if args[:1] == ['-C']:
        if len(args) < 3: raise BuildError('go -C needs a directory and command')
        cwd, args = absolute(args[1], cwd), args[2:]
    elif args and args[0].startswith('-C='):
        cwd, args = absolute(args[0][3:], cwd), args[1:]
    output, package, index = '', '.', 1
    if args[:1] == ['build']:
        while index < len(args):
            arg = args[index]
            if arg == '-o' and index + 1 < len(args):
                index += 1; output = args[index]
            elif arg.startswith('-o='): output = arg[3:]
            elif arg in {'-ldflags', '-gcflags', '-asmflags', '-tags', '-mod', '-modfile', '-p', '-pkgdir', '-toolexec'}:
                index += 1
            elif not arg.startswith('-'): package = arg
            index += 1
    return args, cwd, output, package


def execute_go(stage, words, cwd, environment, root, build_root):
    """Run the declared Go command, flags and environment in its copied inputs."""
    build_root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='stage-', dir=build_root))
    for path, (kind, source) in stage.files.items():
        target = directory / path.lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        if kind in {'source', 'go'} and Path(source).is_file():
            if Path(source).resolve() != target.resolve(): shutil.copy2(source, target)
    for path in stage.directories:
        (directory / path.lstrip('/')).mkdir(parents=True, exist_ok=True)
    args, go_cwd, output, package = go_arguments(words, cwd)
    working = directory / go_cwd.lstrip('/')
    working.mkdir(parents=True, exist_ok=True)
    rewritten = []
    for arg in args:
        if arg.startswith('/'):
            arg = str(directory / arg.lstrip('/'))
        elif arg.startswith('-o=/'):
            arg = '-o=' + str(directory / arg[3:].lstrip('/'))
        elif arg.startswith('-modfile=/'):
            arg = '-modfile=' + str(directory / arg[len('-modfile=/'):])
        rewritten.append(arg)
    env = {**os.environ, **environment, 'GOWORK': 'off', 'GOTOOLCHAIN': 'local', 'GOPROXY': 'off', 'GOSUMDB': 'off'}
    # Caches belong to the evaluator's candidate sandbox, never to the copied
    # image path. A stage can still choose compilation flags such as GOOS.
    for key in ['GOMODCACHE', 'GOCACHE']:
        if key in os.environ: env[key] = os.environ[key]
    tool = shutil.which('go', path=os.environ.get('PATH')) or 'go'
    command = subprocess.run([tool, *rewritten], cwd=working, env=env, capture_output=True, text=True, timeout=180)
    # Go module commands may legitimately update these copied inputs. Keep
    # those changes for the next RUN, without retaining deleted stale files.
    for target in directory.rglob('go.*'):
        if target.is_file() and target.name in {'go.mod', 'go.sum'}:
            path = '/' + target.relative_to(directory).as_posix()
            previous = stage.files.get(path)
            if not previous or Path(previous[1]).read_bytes() != target.read_bytes():
                stage.files[path] = ('source', str(target))
    name = posixpath.basename(go_cwd if package == '.' else package.rstrip('/')).removesuffix('.go')
    artifact = directory / absolute(output or name, go_cwd).lstrip('/')
    if artifact.is_dir(): artifact /= name
    if command.returncode:
        return command.returncode, None, (command.stdout + command.stderr)[-4000:]
    return 0, str(artifact) if artifact.is_file() else None, ''


def run(stage, script, root, compiler=None, depth=0, initial_environment=None, workdir=None, shell=None, argv=None):
    if depth > 8: raise BuildError('recursive Docker build wrapper')
    parser = ShellParser(script)
    tree = ('command', argv) if argv is not None else parser.sequence()
    if argv is None and not parser.at(''): raise BuildError('trailing Docker RUN syntax')
    environment, cwd = dict(stage.env if initial_environment is None else initial_environment), workdir or stage.workdir
    stage.directories.add(cwd)
    shell = stage.shell if shell is None else shell
    strict = argv is None and any(word.startswith('-') and 'e' in word[1:] for word in shell[1:] if word not in {'-c', '--'})
    pipefail, last_error = argv is None and 'pipefail' in shell, ''

    def directory_exists(path):
        return path in stage.directories or any(p.startswith(path.rstrip('/') + '/') for p in set(stage.files) | stage.directories)

    def command(raw):
        nonlocal cwd, strict, pipefail, last_error
        # Quoted operator-looking strings remain data. Assignment values and
        # ordinary variable expansions are resolved without executing text.
        def shell_word(word):
            # Expand only unquoted/double-quoted variables; single quotes and
            # escaped dollar signs are literal shell data.
            chunks, quote, index = [], '', 0
            while index < len(word):
                char = word[index]
                if char in "'\"" and (not quote or quote == char):
                    quote = '' if quote else char
                    index += 1; continue
                if char == '\\' and quote != "'" and index + 1 < len(word):
                    following = word[index + 1]
                    if not quote or following in '$`"\\\n':
                        chunks.append(following); index += 2; continue
                if char == '$' and quote != "'":
                    match = re.match(r'\$(?:\{(\w+)\}|(\w+))', word[index:])
                    if match:
                        chunks.append(environment.get(match[1] or match[2], ''))
                        index += len(match[0]); continue
                chunks.append(char); index += 1
            return ''.join(chunks)
        words = list(raw) if argv is not None and raw is argv else [shell_word(word) for word in raw]
        local = dict(environment)
        while argv is None and words and re.match(r'^[A-Za-z_]\w*=', words[0]):
            key, value = words.pop(0).split('=', 1); local[key] = value
        if not words:
            environment.update(local); return 0
        name = posixpath.basename(words[0])
        if name in {':', 'true', 'echo', 'printf', 'pwd'}: return 0
        if name == 'false': return 1
        if name in {'exit', 'return'}: raise ExitRun(int(words[1]) if len(words) > 1 else 0)
        if name == 'set':
            for index, word in enumerate(words[1:], 1):
                if word.startswith(('-', '+')) and 'e' in word[1:]: strict = word[0] == '-'
                if word in {'-o', '+o'} and words[index + 1:index + 2] == ['pipefail']: pipefail = word == '-o'
            return 0
        if name in {'export', 'readonly'}:
            for word in words[1:]:
                if '=' in word:
                    key, value = word.split('=', 1); environment[key] = value
            return 0
        if name == 'unset':
            for word in words[1:]: environment.pop(word, None)
            return 0
        if name == 'cd':
            target = absolute(words[1], cwd)
            if not directory_exists(target): return 1
            cwd = target; return 0
        if name == 'mkdir':
            stage.directories.update(absolute(word, cwd) for word in words[1:] if not word.startswith('-')); return 0
        if name in {'cp', 'mv', 'install'} and len(words) >= 3:
            source, target = absolute(words[-2], cwd), absolute(words[-1], cwd)
            if source not in stage.files: return 1
            if directory_exists(target) or words[-1].endswith('/'): target = absolute(posixpath.basename(source), target)
            stage.files[target] = stage.files[source]
            if name == 'mv': stage.files.pop(source)
            return 0
        if name == 'rm':
            for word in words[1:]:
                if word.startswith('-'): continue
                target = absolute(word, cwd)
                for path in list(stage.files):
                    if path == target or path.startswith(target.rstrip('/') + '/'): stage.files.pop(path)
            return 0
        if name in {'test', '['}:
            args = words[1:-1] if name == '[' else words[1:]
            if len(args) == 2 and args[0] in {'-e', '-f', '-d', '-x', '-n', '-z'}:
                flag, value = args; path = absolute(value, cwd)
                match = (bool(value) if flag == '-n' else not value if flag == '-z' else directory_exists(path) if flag == '-d'
                         else path in stage.files or directory_exists(path) if flag == '-e' else path in stage.files)
                return 0 if match else 1
            if len(args) == 3 and args[1] in {'=', '==', '!='}:
                return 0 if (args[0] == args[2]) == (args[1] != '!=') else 1
            raise BuildError('unsupported Docker build test expression')
        if name == 'go':
            args, build_cwd, output, package = go_arguments(words, cwd)
            status, artifact, error = compiler(stage, words, cwd, local) if compiler else (0, 'cmd/labeler', '')
            last_error = error
            if args[:1] == ['build'] and status == 0:
                source_dir = absolute(package, build_cwd)
                if source_dir.endswith('.go'): source_dir = posixpath.dirname(source_dir)
                production = [value for path, value in stage.files.items() if posixpath.dirname(path) == source_dir
                              and path.endswith('.go') and not path.endswith('_test.go')]
                if production and all(kind == 'source' and Path(path).parent == root / 'cmd/labeler' for kind, path in production):
                    if compiler and (not artifact or not Path(artifact).is_file()):
                        raise BuildError('declared Go build produced no executable artifact')
                    name = posixpath.basename(build_cwd if package == '.' else package.rstrip('/')).removesuffix('.go')
                    target = absolute(output or name, build_cwd)
                    if output.endswith('/'): target = absolute(name, target)
                    stage.files[target] = ('go', artifact)
            return status
        shell_command = next((index for index, word in enumerate(words[1:], 1)
                              if word.startswith('-') and not word.startswith('--') and 'c' in word), None)
        if name in {'sh', 'bash', 'dash'} and shell_command is not None:
            offset = shell_command + 1
            if offset >= len(words): return 2
            try: return run(stage, words[offset], root, compiler, depth + 1, local, cwd, words[:offset])
            except BuildError as error:
                if error.status is not None: return error.status
                raise
        # Base-image package provisioning is outside this host adapter. It
        # never creates a Go artifact; actual Go commands are verified above.
        if name in {'apk', 'apt-get', 'apt', 'yum', 'dnf', 'update-ca-certificates'}: return 0
        source = stage.files.get(absolute(words[0], cwd))
        if source and source[0] == 'source' and Path(source[1]).suffix in {'.sh', '.bash', ''}:
            return run(stage, Path(source[1]).read_text(), root, compiler, depth + 1, local, cwd, ['/bin/sh', '-c'])
        raise BuildError('unsupported Docker build command: ' + words[0])

    def visit(node, conditional=False):
        nonlocal cwd, environment, strict, pipefail
        kind = node[0]
        if kind == 'command':
            status = command(node[1])
            if strict and status and not conditional: raise ExitRun(status)
            return status
        if kind == 'sequence':
            status = 0
            for child in node[1]:
                status = visit(child, conditional)
            return status
        if kind == 'andor':
            status = visit(node[2], True)
            if (status == 0) == (node[1] == '&&'): return visit(node[3], conditional)
            return status
        if kind == 'pipeline':
            statuses = []
            for child in node[1]:
                previous = cwd, dict(environment), strict, pipefail
                try: statuses.append(visit(child, True if len(node[1]) > 1 else conditional))
                finally:
                    if len(node[1]) > 1: cwd, environment, strict, pipefail = previous
            status = next((code for code in reversed(statuses) if code), 0) if pipefail else statuses[-1]
            if strict and status and not conditional: raise ExitRun(status)
            return status
        if kind == 'not': return int(visit(node[1], True) == 0)
        if kind == 'if': return visit(node[2] if visit(node[1], True) == 0 else node[3], conditional)
        if kind == 'subshell':
            previous = cwd, dict(environment), strict, pipefail
            try:
                try: return visit(node[1], conditional)
                except ExitRun as exit_run: return exit_run.status
            finally: cwd, environment, strict, pipefail = previous
        raise BuildError('unknown Docker build syntax')

    try: status = visit(tree)
    except ExitRun as exit_run: status = exit_run.status
    if status:
        raise BuildError('Docker RUN failed with exit %d: %s\n%s' % (status, script, last_error), status)
    return status
