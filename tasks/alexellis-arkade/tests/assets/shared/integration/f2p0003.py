#!/usr/bin/env python3
"""Runtime probes for the observations owned by BATCH-F2P-0003.

The batch dependency snapshot does not contain an executable probe for these
observations, so the integration harness carries the equivalent predicates.
Every predicate requires Kong 1.8.1 to be on the real runtime path *and* the
observable CLI contract to hold.  The source (Cobra) checkout therefore fails
these target postconditions while the reference target passes them.
"""

import base64
import json
import os
import re
import subprocess
import tempfile


MIGRATED = [
    "main.go",
    "cmd/get.go",
    "cmd/install.go",
    "cmd/info.go",
    "cmd/update.go",
    "cmd/uninstall.go",
    "cmd/version.go",
    "cmd/search.go",
    "cmd/completion.go",
    "cmd/release.go",
    "cmd/chart/chart.go",
    "cmd/system/system.go",
    "cmd/system/install.go",
    "cmd/oci/oci.go",
    "cmd/oci/install.go",
    "cmd/apps/argocd_app.go",
]

KONG_MODULE_RE = re.compile(r"github\.com/alecthomas/kong\s+v1\.8\.1")

# ``cmd/system/registry.go`` is an individual ``system install`` tool command
# the instruction allows to keep Cobra, so it is exempt from the Cobra-free
# requirement while remaining part of the migrated scope.
COBRA_FREE = [path for path in MIGRATED if path != "cmd/system/registry.go"]

TOP_LEVEL_HELP = [
    (),
    ("install",),
    ("version",),
    ("info",),
    ("update",),
    ("get",),
    ("uninstall",),
    ("completion",),
    ("release",),
    ("chart",),
    ("docker",),
    ("fstail",),
    ("gha",),
    ("system",),
    ("oci",),
    ("search",),
]


def combined(result):
    return (result.stdout or "") + (result.stderr or "")


def has(result, *needles):
    text = combined(result)
    return all(needle in text for needle in needles)


class Probe:
    def __init__(self, repo, binary):
        self.repo = os.path.abspath(repo)
        self.binary = os.path.abspath(binary)
        self.tmp = tempfile.mkdtemp(prefix="arkade-f2p0003-")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home, exist_ok=True)
        self.env = os.environ.copy()
        self.env["HOME"] = self.home
        self.env["NO_COLOR"] = "1"
        self._kong = None

    def read(self, relative):
        try:
            with open(os.path.join(self.repo, relative), encoding="utf-8") as handle:
                return handle.read()
        except (FileNotFoundError, OSError):
            return ""

    def run(self, args, stdin=None, env=None, timeout=120):
        return subprocess.run(
            [self.binary] + list(args),
            cwd=self.repo,
            env=env or self.env,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )

    def help_ok(self, *command):
        result = self.run(tuple(command) + ("--help",))
        return result.returncode == 0 and self.help_rendered(combined(result))

    @staticmethod
    def error_text(result):
        """Semantic view of an error: lower-cased, whitespace-collapsed stderr.

        The instruction pins the error channel, the rejection and the exit
        status, never one framework's exact prose.
        """
        return re.sub(r"\s+", " ", (result.stderr or "")).strip().lower()

    @staticmethod
    def completion_covers(text, needles):
        return all(needle in text for needle in needles)

    @staticmethod
    def shows_flag_default(text, flag, default):
        """Does help document ``flag`` with its default on the same line?

        Flag names, defaults and accepted values are part of the contract, but
        the help layout is not: Cobra renders ``--parallel int ... (default 4)``
        while Kong renders ``--parallel=4``.  Both state the same default.
        """
        pattern = re.compile(
            r"--" + re.escape(flag) + r"[^\n]*\b" + re.escape(str(default)) + r"\b"
        )
        return bool(pattern.search(text or ""))

    @staticmethod
    def help_rendered(text):
        """Is help available at all?

        The instruction fixes help availability, not its wording or layout, so
        only the presence of help output is asserted; concrete commands, flags
        and aliases are pinned by the individual checks.
        """
        return bool(text.strip())

    def kong_on_path(self):
        if self._kong is not None:
            return self._kong
        ok = "github.com/alecthomas/kong v1.8.1" in self.read("go.mod")
        if ok:
            ok = all(
                "github.com/spf13/cobra" not in self.read(relative)
                for relative in COBRA_FREE
            )
        if ok:
            process = subprocess.run(
                ["go", "version", "-m", self.binary],
                cwd=self.repo,
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            ok = bool(KONG_MODULE_RE.search(process.stdout or ""))
        self._kong = ok
        return ok

    def check_completion_contract(self):
        bash = self.run(["completion", "bash"])
        zsh = self.run(["completion", "zsh"])
        fish = self.run(["completion", "fish"])
        powershell = self.run(["completion", "powershell"])
        tcsh = self.run(["completion", "tcsh"])
        # The instruction requires completion for bash, zsh, fish and
        # powershell from the migrated tree, including aliases and
        # subcommands.  Script layout and internal helper names are explicitly
        # exempt from byte-for-byte matching, so only the covered command names
        # and the unsupported-shell error are asserted.
        ok = (
            self.kong_on_path()
            and bash.returncode == 0
            and self.completion_covers(bash.stdout, ["arkade", "install", "download", "delete"])
            and zsh.returncode == 0
            and self.completion_covers(zsh.stdout, ["arkade", "install", "download", "delete"])
            and fish.returncode == 0
            and self.completion_covers(fish.stdout, ["complete", "arkade", "install"])
            and powershell.returncode == 0
            and self.completion_covers(powershell.stdout, ["arkade", "install"])
            and tcsh.returncode != 0
            and "not supported" in self.error_text(tcsh)
        )
        return ok, "bash=%d zsh=%d fish=%d pwsh=%d tcsh=%d kong=%s" % (
            bash.returncode,
            zsh.returncode,
            fish.returncode,
            powershell.returncode,
            tcsh.returncode,
            self.kong_on_path(),
        )

    def check_hidden_oci_path(self):
        parse = self.run(["oci", "install", "--path=/tmp/x"])
        help_result = self.run(["oci", "install", "--help"])
        ok = (
            self.kong_on_path()
            and parse.returncode != 0
            and "please provide an image name" in combined(parse)
            and "unknown flag" not in combined(parse)
            and "--path" not in help_result.stdout
        )
        return ok, "parse=%d hidden=%s" % (
            parse.returncode,
            "--path" not in help_result.stdout,
        )

    def check_get_validation(self):
        parallel = self.run(["get", "--parallel", "0", "helm"])
        arch = self.run(["get", "helm", "--arch", "notanarch"])
        operating_system = self.run(["get", "helm", "--os", "notanos"])
        unknown = self.run(["get", "no-such-tool"])
        ok = (
            self.kong_on_path()
            and parallel.returncode != 0
            and arch.returncode != 0
            and "not supported" in combined(arch)
            and operating_system.returncode != 0
            and "not supported" in combined(operating_system)
            and unknown.returncode != 0
        )
        return ok, "parallel=%d arch=%d os=%d unknown=%d" % (
            parallel.returncode,
            arch.returncode,
            operating_system.returncode,
            unknown.returncode,
        )

    def check_signal_cleanup(self):
        source = self.read("cmd/get.go")
        markers = [
            "signal.Notify(signalChan, os.Interrupt, syscall.SIGTERM)",
            "leaveAltScreen()",
            "[?25h",
            "[?1049l",
            "os.Exit(2)",
        ]
        listing = self.run(["get", "--format", "list"])
        ok = (
            self.kong_on_path()
            and all(marker in source for marker in markers)
            and self.help_ok("get")
            and listing.returncode == 0
        )
        return ok, "markers=%s list=%d" % (
            all(marker in source for marker in markers),
            listing.returncode,
        )

    def check_subcommand_flags(self):
        matrix = [
            (("s", "i", "registry"), ["--bind-addr", "--docker-password", "--remote-url", "--storage", "--tls", "--type"]),
            (("oci", "install"), ["--arch", "--os", "--version"]),
            (("chart", "upgrade"), []),
            (("docker", "gen"), []),
            (("gha", "bump"), []),
            (("fstail",), []),
        ]
        ok = self.kong_on_path()
        details = []
        for command, flags in matrix:
            result = self.run(tuple(command) + ("--help",))
            command_ok = result.returncode == 0 and all(
                flag in combined(result) for flag in flags
            )
            details.append("%s=%s" % (" ".join(command), command_ok))
            ok = ok and command_ok
        systems = [command for command in (("chart",), ("docker",), ("gha",), ("system",), ("oci",), ("fstail",)) if not self.help_ok(*command)]
        ok = ok and not systems
        return ok, " ".join(details) + " kong=%s" % self.kong_on_path()

    def check_help_availability(self):
        ok = self.kong_on_path() and all(self.help_ok(*command) for command in TOP_LEVEL_HELP)
        root = self.run(["--help"])
        ok = ok and root.returncode == 0 and self.help_rendered(combined(root))
        help_command, help_detail = self.check_help_command()
        ok = ok and help_command
        return ok, "root=%d all_help=%s kong=%s help_command=%s" % (
            root.returncode,
            all(self.help_ok(*command) for command in TOP_LEVEL_HELP),
            self.kong_on_path(),
            help_detail,
        )

    def check_help_command(self):
        # Cobra registers `help [command]` automatically. It is an accepted
        # public command covered by instruction.md's preservation requirement,
        # even though Kong only creates a --help flag by default. Check actual
        # dispatch to root, aliases and nested native/legacy commands; matching
        # a help heading alone could accept a root-help fallback for every topic.
        matrix = [
            ((), ["install", "get", "completion"]),
            (("get",), ["--format", "--version", "--parallel"]),
            (("g",), ["--format", "--version", "--parallel"]),
            (("install",), ["--kubeconfig", "--wait"]),
            (("install", "argocd"), ["--kubeconfig", "--wait"]),
            (("i", "nginx-ingress"), ["--kubeconfig", "--wait"]),
            (("s", "i", "go"), ["--path", "--version"]),
            (("o", "extract"), ["--arch", "--os", "--version"]),
            (("c", "verify"), ["--file"]),
            (("rel",), ["--dry-run", "--major", "--token"]),
        ]
        results = {}
        for command, tokens in matrix:
            result = self.run(("help",) + command)
            results[" ".join(command) or "<root>"] = (
                result.returncode == 0
                and bool(result.stdout.strip())
                and all(token in result.stdout for token in tokens)
            )
        return all(results.values()), str(results)

    def check_alias_dispatch(self):
        invocations = [
            ("g", "--format", "list"),
            ("d", "--format", "list"),
            ("download", "--format", "list"),
            ("v",),
            ("i", "--print-table"),
            ("f", "argocd"),
            ("u", "--help"),
            ("delete", "--help"),
            ("rel", "--help"),
            ("r", "--help"),
            ("c", "--help"),
            ("s", "--help"),
            ("o", "--help"),
            ("ft", "--help"),
        ]
        results = {}
        for args in invocations:
            results[" ".join(args)] = self.run(args).returncode
        d_alias = self.run(["d", "--format", "list"])
        ok = (
            self.kong_on_path()
            and all(code == 0 for code in results.values())
            and "helm" in d_alias.stdout
            and "kubectl" in d_alias.stdout
        )
        return ok, "aliases=%s kong=%s" % (results, self.kong_on_path())

    def check_release_flags(self):
        help_result = self.run(["release", "--help"])
        major_minor = self.run(["release", "--major", "--minor", "--dry-run"])
        major_patch = self.run(["release", "--major", "--patch", "--dry-run"])
        flags = ["--dry-run", "--major", "--minor", "--patch", "--prerelease", "--latest", "--token"]
        ok = (
            self.kong_on_path()
            and help_result.returncode == 0
            and all(flag in combined(help_result) for flag in flags)
            and major_minor.returncode != 0
            and major_patch.returncode != 0
            and ("mutually exclusive" in combined(major_minor)
                 or "can't be used together" in combined(major_minor))
        )
        return ok, "help=%d major_minor=%d major_patch=%d" % (
            help_result.returncode,
            major_minor.returncode,
            major_patch.returncode,
        )

    def check_oci_install_surface(self):
        help_result = self.run(["oci", "install", "--help"])
        install_alias = self.run(["oci", "i", "--help"])
        extract_alias = self.run(["oci", "extract", "--help"])
        parse = self.run(["oci", "install", "--path=/tmp/x"])
        ok = (
            self.kong_on_path()
            and help_result.returncode == 0
            and has(help_result, "--arch", "--os", "--version")
            and install_alias.returncode == 0
            and extract_alias.returncode == 0
            and parse.returncode != 0
            and "please provide an image name" in combined(parse)
        )
        return ok, "install=%d i=%d extract=%d path=%d" % (
            help_result.returncode,
            install_alias.returncode,
            extract_alias.returncode,
            parse.returncode,
        )

    def check_release_semver(self):
        default = self.run(["release", "v1.2.3", "t", "--dry-run"])
        explicit = self.run(["release", "v1.2.3", "t", "--prerelease=false", "--dry-run"])
        bare = self.run(["release", "1.2.3", "t", "--dry-run"])
        ok = (
            self.kong_on_path()
            and default.returncode == 0
            and "Tag:        v1.2.3" in default.stdout
            and "Prerelease: true" in default.stdout
            and explicit.returncode == 0
            and "Prerelease: false" in explicit.stdout
            and bare.returncode == 0
            and "Tag:        1.2.3" in bare.stdout
        )
        return ok, "default=%d explicit=%d bare=%d" % (
            default.returncode,
            explicit.returncode,
            bare.returncode,
        )

    def check_get_download_handler(self):
        listing = self.run(["get", "--format", "list"])
        default = self.run(["get"])
        table = self.run(["get", "--format", "table"])
        # The handler is judged by what ``get`` does, never by the function name
        # the implementation happens to choose for its command constructor.
        ok = (
            self.kong_on_path()
            and listing.returncode == 0
            and "helm" in listing.stdout
            and "kubectl" in listing.stdout
            and default.returncode == 0
            and "kubectl" in default.stdout
            and table.returncode == 0
            and "kubectl" in table.stdout
        )
        return ok, "list=%d default=%d table=%d" % (
            listing.returncode,
            default.returncode,
            table.returncode,
        )

    def check_public_contract(self):
        listing = self.run(["get", "--format", "list"])
        table = self.run(["install", "--print-table"])
        version = self.run(["version", "--json"])
        # Hidden implementation branches must not turn previously unknown
        # root command tokens into successful public invocations.
        unknown_invocations = [(name,) for name in (
            "default", "_default", "__default", "__root", "overview",
        )]
        # A default child with positional arguments can also swallow unknown
        # nested commands, or an unknown app followed by a second argument.
        # Check those independently of the root's hidden default name.
        unknown_invocations += [tuple(group) + ("no-such-command",) for group in (
            ("chart",), ("docker",), ("gha",), ("system",),
            ("system", "install"), ("oci",),
        )]
        unknown_invocations += [
            ("install", "no-such-app", "extra"),
            ("i", "no-such-app", "extra"),
        ]
        rejected = [self.run(args) for args in unknown_invocations]
        try:
            payload = json.loads(version.stdout)
        except (TypeError, ValueError):
            payload = None
        ok = (
            self.kong_on_path()
            and listing.returncode == 0
            and has(listing, "helm", "kubectl")
            and table.returncode == 0
            and "|" in table.stdout
            and "There are" in table.stdout
            and version.returncode == 0
            and isinstance(payload, dict)
            and set(payload) == {"version", "commit", "build_date"}
            and all(result.returncode != 0 and result.stdout == ""
                    and bool(result.stderr.strip()) for result in rejected)
        )
        return ok, "list=%d table=%d version=%d unknown_rejected=%d/%d" % (
            listing.returncode,
            table.returncode,
            version.returncode,
            sum(result.returncode != 0 and result.stdout == ""
                and bool(result.stderr.strip()) for result in rejected),
            len(rejected),
        )

    def check_chart_surface(self):
        results = {}
        for command in (("chart", "verify"), ("chart", "upgrade"), ("chart", "bump")):
            result = self.run(tuple(command) + ("--help",))
            results[" ".join(command)] = result.returncode
        chart_help = self.run(["chart", "--help"])
        ok = (
            self.kong_on_path()
            and all(code == 0 for code in results.values())
            and chart_help.returncode == 0
            and self.help_rendered(combined(chart_help))
        )
        return ok, "charts=%s kong_marker=%s" % (
            results,
            self.help_rendered(combined(chart_help)),
        )

    def check_system_flags(self):
        registry = self.run(["s", "i", "registry", "--help"])
        go = self.run(["system", "install", "go", "--help"])
        registry_flags = ["--bind-addr", "--docker-password", "--remote-url", "--storage", "--tls", "--type"]
        go_flags = ["--path", "--progress", "--version"]
        ok = (
            self.kong_on_path()
            and registry.returncode == 0
            and all(flag in combined(registry) for flag in registry_flags)
            and go.returncode == 0
            and all(flag in combined(go) for flag in go_flags)
        )
        return ok, "registry=%d go=%d" % (registry.returncode, go.returncode)

    def check_get_validation_tests(self):
        base_ok, _ = self.check_get_validation()
        test_source = self.read("cmd/get_test.go")
        ok = (
            base_ok
            and "Test_GetCommandWithInvalidArch" in test_source
            and "not supported" in test_source
        )
        return ok, "validation=%s test_assertion=%s" % (
            base_ok,
            "Test_GetCommandWithInvalidArch" in test_source,
        )

    def check_oci_aliases(self):
        results = {}
        for alias in ("i", "extract", "push", "export"):
            results[alias] = self.run(["oci", alias, "--help"]).returncode
        oci_help = self.run(["oci", "--help"])
        ok = (
            self.kong_on_path()
            and all(code == 0 for code in results.values())
            and oci_help.returncode == 0
            and has(oci_help, "install", "publish")
        )
        return ok, "aliases=%s" % results

    def check_get_download_behavior(self):
        default = self.run(["get"])
        listing = self.run(["get", "--format", "list"])
        download_source = self.read("pkg/get/download.go")
        ok = (
            self.kong_on_path()
            and default.returncode == 0
            and "kubectl" in default.stdout
            and listing.returncode == 0
            and has(listing, "helm", "kubectl")
            and "func Download" in download_source
            and "DownloadFileP" in download_source
        )
        return ok, "default=%d list=%d handler=%s" % (
            default.returncode,
            listing.returncode,
            "func Download" in download_source and "DownloadFileP" in download_source,
        )

    def check_get_formats(self):
        default = self.run(["get"])
        table = self.run(["get", "--format", "table"])
        markdown = self.run(["get", "--format", "markdown"])
        listing = self.run(["get", "--format", "list"])
        shorthand = self.run(["get", "-o=list"])
        ok = (
            self.kong_on_path()
            and default.returncode == 0
            and "TOOL" in default.stdout
            and "|" in default.stdout
            and "kubectl" in default.stdout
            and table.returncode == 0
            and "|" in table.stdout
            and "kubectl" in table.stdout
            and markdown.returncode == 0
            and "|" in markdown.stdout
            and "https://" in markdown.stdout
            and "kubectl" in markdown.stdout
            and listing.returncode == 0
            and "helm" in listing.stdout
            and "kubectl" in listing.stdout
            and "|" not in listing.stdout
            and shorthand.returncode == 0
            and shorthand.stderr == ""
            and shorthand.stdout == listing.stdout
        )
        return ok, "default=%d table=%d markdown=%d list=%d shorthand=%d" % (
            default.returncode,
            table.returncode,
            markdown.returncode,
            listing.returncode,
            shorthand.returncode,
        )

    def check_get_flags(self):
        help_result = self.run(["get", "--help"])
        listing = self.run(["get", "--format", "list"])
        boolean_values = self.run([
            "get", "--format", "list", "--progress=f", "--verify=t", "--quiet=F",
        ])
        # Both tools exist, so URL selection is local and parallel validation
        # returns before any download. Flags must work between positional tools.
        interspersed = self.run(["get", "helm", "--parallel", "0", "kubectl"])
        flags = [
            "--progress",
            "--format",
            "-o",
            "--path",
            "--version",
            "-v",
            "--arch",
            "--os",
            "--quiet",
            "--verify",
            "--parallel",
            "-p",
        ]
        ok = (
            self.kong_on_path()
            and help_result.returncode == 0
            and all(flag in combined(help_result) for flag in flags)
            and self.shows_flag_default(combined(help_result), "parallel", 4)
            and listing.returncode == boolean_values.returncode == 0
            and boolean_values.stderr == ""
            and boolean_values.stdout == listing.stdout
            and interspersed.returncode != 0
            and interspersed.stdout == ""
            and "--parallel must be at least 1" in interspersed.stderr
        )
        return ok, "help=%d flags=%s bool_values=%d interspersed=%d" % (
            help_result.returncode,
            all(flag in combined(help_result) for flag in flags),
            boolean_values.returncode,
            interspersed.returncode,
        )

    def check_oci_login(self):
        stdin_dir = os.path.join(self.tmp, "docker-stdin")
        os.makedirs(stdin_dir, exist_ok=True)
        stdin_env = dict(self.env)
        stdin_env["DOCKER_CONFIG"] = stdin_dir
        stdin_result = self.run(
            ["oci", "login", "reg.example.com", "-u", "alice", "--password-stdin"],
            stdin="s3cret\n",
            env=stdin_env,
        )
        auth = None
        config_path = os.path.join(stdin_dir, "config.json")
        try:
            with open(config_path, encoding="utf-8") as handle:
                auth = json.load(handle)["auths"]["reg.example.com"]["auth"]
        except (FileNotFoundError, KeyError, TypeError, ValueError):
            auth = None
        expected = base64.b64encode(b"alice:s3cret").decode("ascii")

        flag_dir = os.path.join(self.tmp, "docker-flag")
        os.makedirs(flag_dir, exist_ok=True)
        flag_env = dict(self.env)
        flag_env["DOCKER_CONFIG"] = flag_dir
        flag_result = self.run(
            ["oci", "login", "reg.example.com", "-u", "alice", "-p", "direct"],
            env=flag_env,
        )
        flag_auth = None
        try:
            with open(os.path.join(flag_dir, "config.json"), encoding="utf-8") as handle:
                flag_auth = json.load(handle)["auths"]["reg.example.com"]["auth"]
        except (FileNotFoundError, KeyError, TypeError, ValueError):
            flag_auth = None

        missing_dir = os.path.join(self.tmp, "docker-missing")
        os.makedirs(missing_dir, exist_ok=True)
        missing_env = dict(self.env)
        missing_env["DOCKER_CONFIG"] = missing_dir
        missing = self.run(["oci", "login", "reg.example.com"], env=missing_env)

        ok = (
            self.kong_on_path()
            and stdin_result.returncode == 0
            and auth == expected
            and flag_result.returncode == 0
            and flag_auth == base64.b64encode(b"alice:direct").decode("ascii")
            and missing.returncode != 0
            and "username and password required" in combined(missing)
        )
        return ok, "stdin=%d stdin_config=%s flag=%d missing=%d" % (
            stdin_result.returncode,
            auth == expected,
            flag_result.returncode,
            missing.returncode,
        )

    def check_get_format_tests(self):
        formats_ok, _ = self.check_get_formats()
        test_source = self.read("cmd/get_test.go")
        ok = (
            formats_ok
            and "Test_GetCommandWithInvalidArch" in test_source
            and "not supported" in test_source
        )
        return ok, "formats=%s test_assertion=%s" % (
            formats_ok,
            "Test_GetCommandWithInvalidArch" in test_source,
        )

    def check_get_version_syntax(self):
        conflict = self.run(["get", "helm@v1.2.3", "--version", "v1.2.3"])
        help_result = self.run(["get", "--help"])
        listing = self.run(["get", "--format", "list"])
        ok = (
            self.kong_on_path()
            and conflict.returncode != 0
            and "cannot specify" in combined(conflict)
            and help_result.returncode == 0
            and "--version" in combined(help_result)
            and listing.returncode == 0
            and has(listing, "helm", "kubectl")
        )
        return ok, "conflict=%d help=%d list=%d" % (
            conflict.returncode,
            help_result.returncode,
            listing.returncode,
        )

    def check_get_public_contract(self):
        listing = self.run(["get", "--format", "list"])
        version = self.run(["version", "--json"])
        try:
            payload = json.loads(version.stdout)
        except (TypeError, ValueError):
            payload = None
        hidden = self.check_hidden_oci_path()[0]
        ok = (
            self.kong_on_path()
            and listing.returncode == 0
            and has(listing, "helm", "kubectl")
            and version.returncode == 0
            and isinstance(payload, dict)
            and set(payload) == {"version", "commit", "build_date"}
            and hidden
        )
        return ok, "list=%d version=%d oci=%s" % (
            listing.returncode,
            version.returncode,
            hidden,
        )

    def checks(self):
        return {
            "F2P-049": self.check_hidden_oci_path,
            "F2P-050": self.check_get_validation,
            "F2P-051": self.check_signal_cleanup,
            "F2P-052": self.check_completion_contract,
            "F2P-053": self.check_subcommand_flags,
            "F2P-054": self.check_help_availability,
            "F2P-055": self.check_alias_dispatch,
            "F2P-056": self.check_release_flags,
            "F2P-057": self.check_oci_install_surface,
            "F2P-058": self.check_hidden_oci_path,
            "F2P-059": self.check_release_semver,
            "F2P-060": self.check_get_download_handler,
            "F2P-061": self.check_public_contract,
            "F2P-062": self.check_chart_surface,
            "F2P-063": self.check_system_flags,
            "F2P-064": self.check_get_validation_tests,
            "F2P-065": self.check_oci_aliases,
            "F2P-066": self.check_get_download_behavior,
            "F2P-067": self.check_get_formats,
            "F2P-068": self.check_get_flags,
            "F2P-069": self.check_oci_login,
            "F2P-070": self.check_get_format_tests,
            "F2P-071": self.check_get_version_syntax,
            "F2P-072": self.check_get_public_contract,
        }


def evaluate(repo, binary):
    probe = Probe(repo, binary)
    results = {}
    for observation_id, check in sorted(probe.checks().items()):
        try:
            passed, detail = check()
            results[observation_id] = {
                "status": "PASS" if passed else "FAIL",
                "detail": detail,
            }
        except Exception as exc:  # execution failure is not a semantic result
            results[observation_id] = {
                "status": "INFRA_ERROR",
                "detail": repr(exc),
            }
    return results
