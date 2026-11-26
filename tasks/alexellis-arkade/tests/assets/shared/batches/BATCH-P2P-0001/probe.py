#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
import tempfile

OBSERVATION_IDS = ["P2P-%03d" % i for i in range(1, 41)]


def combined(result):
    return (result.stdout + result.stderr).lower()


def has(result, *needles):
    text = combined(result)
    return all(needle.lower() in text for needle in needles)


class Probe:
    def __init__(self, repo):
        self.repo = os.path.abspath(repo)
        self.tmp = tempfile.mkdtemp(prefix="arkade-p2p-")
        self.binary = os.path.join(self.tmp, "arkade")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home, exist_ok=True)
        self.env = os.environ.copy()
        self.env.setdefault("CGO_ENABLED", "0")
        self.run_env = dict(self.env)
        self.run_env["HOME"] = self.home
        self._test_ok = None

    def run(self, args, timeout=120, env=None):
        return subprocess.run(
            args,
            cwd=self.repo,
            env=env or self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )

    def arkade(self, *args, timeout=60):
        return self.run([self.binary] + list(args), timeout=timeout, env=self.run_env)

    def build(self):
        if not os.path.isdir(self.repo):
            return False
        result = self.run(["go", "build", "-o", self.binary, "."], timeout=900)
        return result.returncode == 0 and os.path.isfile(self.binary)

    def tests_ok(self):
        if self._test_ok is None:
            result = self.run(["go", "test", "./...", "-count=1"], timeout=1200)
            self._test_ok = result.returncode == 0
        return self._test_ok

    def help_ok(self, *command):
        result = self.arkade(*(tuple(command) + ("--help",)))
        return result.returncode == 0 and bool(combined(result).strip())

    def checks(self):
        checks = {}

        command_helps = [
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
            ("chart", "verify"),
            ("chart", "upgrade"),
            ("chart", "bump"),
            ("docker", "upgrade"),
            ("docker", "gen"),
            ("gha", "bump"),
            ("system", "install"),
            ("system", "install", "go"),
            ("system", "install", "registry"),
            ("oci", "install"),
            ("oci", "publish"),
            ("oci", "login"),
            ("install", "argocd"),
            ("install", "openfaas"),
            ("install", "info"),
        ]
        checks["P2P-001"] = all(self.help_ok(*command) for command in command_helps)

        listing = self.arkade("get", "--format", "list")
        checks["P2P-002"] = (
            listing.returncode == 0
            and has(listing, "helm", "kubectl")
            and self.tests_ok()
        )

        unknown_command = self.arkade("not-a-command")
        unknown_flag = self.arkade("get", "--nope")
        parallel_zero = self.arkade("get", "--parallel", "0", "helm")
        version = self.arkade("version")
        listing = self.arkade("get", "--format", "list")
        checks["P2P-003"] = (
            unknown_command.returncode != 0
            and unknown_flag.returncode != 0
            and parallel_zero.returncode != 0
            and version.returncode == 0
            and listing.returncode == 0
        )

        release_help = self.arkade("release", "--help")
        release_conflict = self.arkade("release", "--major", "--minor", "--dry-run")
        checks["P2P-004"] = (
            release_help.returncode == 0
            and has(
                release_help,
                "--dry-run",
                "--major",
                "--minor",
                "--patch",
                "--prerelease",
                "--latest",
                "--token",
            )
            and release_conflict.returncode != 0
            and ("mutually exclusive" in combined(release_conflict)
                 or "can't be used together" in combined(release_conflict))
        )

        print_table = self.arkade("install", "--print-table")
        checks["P2P-005"] = (
            print_table.returncode == 0
            and has(print_table, "there are", "apps", "|")
        )

        dispatch_helps = [
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
        checks["P2P-006"] = (
            all(self.help_ok(*command) for command in dispatch_helps)
            and self.arkade("version").returncode == 0
            and self.arkade("get", "--format", "list").returncode == 0
            and self.arkade("install", "--print-table").returncode == 0
        )

        get_source = os.path.join(self.repo, "cmd", "get.go")
        get_text = ""
        if os.path.isfile(get_source):
            with open(get_source, encoding="utf-8", errors="replace") as handle:
                get_text = handle.read()
        checks["P2P-007"] = (
            "signal.Notify" in get_text
            and ("os.Interrupt" in get_text or "SIGTERM" in get_text)
            and self.tests_ok()
        )

        get_help = self.arkade("get", "--help")
        version_conflict = self.arkade("get", "helm@v1.2.3", "--version", "v1.2.3")
        checks["P2P-008"] = (
            get_help.returncode == 0
            and has(get_help, "--version")
            and version_conflict.returncode != 0
            and "cannot specify" in combined(version_conflict)
        )

        no_args = self.arkade()
        checks["P2P-009"] = (
            no_args.returncode == 0
            and has(no_args, "version:", "install", "get", "completion")
        )

        completion_ok = True
        for shell in ("bash", "zsh", "fish", "powershell"):
            result = self.arkade("completion", shell)
            completion_ok = completion_ok and result.returncode == 0 and bool(
                result.stdout.strip()
            )
        completion_help = self.arkade("completion")
        completion_bad = self.arkade("completion", "nonsense")
        checks["P2P-010"] = (
            completion_ok
            and completion_help.returncode == 0
            and completion_bad.returncode != 0
        )

        registry_help = self.arkade("system", "install", "registry", "--help")
        checks["P2P-011"] = (
            self.help_ok("system", "install")
            and self.help_ok("system", "install", "go")
            and registry_help.returncode == 0
            and has(
                registry_help,
                "--arch",
                "--bind-addr",
                "--docker-password",
                "--remote-url",
                "--storage",
                "--tls",
                "--type",
            )
        )

        deprecated_path = self.arkade("oci", "install", "--path=/tmp/x")
        checks["P2P-012"] = (
            deprecated_path.returncode != 0
            and "please provide an image name" in combined(deprecated_path)
            and "unknown flag" not in combined(deprecated_path)
        )

        invalid_gets = [
            self.arkade("get", "--parallel", "0", "helm"),
            self.arkade("get", "helm", "--arch", "notanarch"),
            self.arkade("get", "helm", "--os", "notanos"),
            self.arkade("get", "no-such-tool"),
        ]
        checks["P2P-013"] = all(result.returncode != 0 for result in invalid_gets)

        tool_listing = self.arkade("get", "--format", "list")
        app_table = self.arkade("install", "--print-table")
        checks["P2P-014"] = (
            tool_listing.returncode == 0
            and has(tool_listing, "helm", "kubectl")
            and app_table.returncode == 0
            and has(app_table, "argocd", "openfaas", "there are")
        )

        out_of_scope = [
            ("chart", "verify"),
            ("docker", "gen"),
            ("fstail",),
            ("gha", "bump"),
            ("oci", "publish"),
            ("oci", "login"),
            ("system", "install", "go"),
        ]
        checks["P2P-015"] = all(self.help_ok(*command) for command in out_of_scope)

        checks["P2P-016"] = (
            all(self.help_ok(*command) for command in out_of_scope)
            and self.help_ok("docker")
            and self.help_ok("system", "install")
            and self.help_ok("oci", "install")
        )

        checks["P2P-017"] = (
            all(self.help_ok(*command) for command in dispatch_helps)
            and self.arkade("get", "--format", "list").returncode == 0
            and self.arkade("install", "--print-table").returncode == 0
        )

        info_guidance = self.arkade("info")
        info_app = self.arkade("info", "argocd")
        info_unknown = self.arkade("info", "no-such-app")
        info_many = self.arkade("info", "a", "b")
        checks["P2P-018"] = (
            info_guidance.returncode == 0
            and "run arkade info app_name for more" in combined(info_guidance)
            and info_app.returncode == 0
            and "info for app: argocd" in combined(info_app)
            and info_unknown.returncode != 0
            and info_many.returncode != 0
        )

        handler_tests = self.run(
            [
                "go",
                "test",
                "./pkg/get",
                "./pkg/archive",
                "./pkg/helm",
                "./pkg/k8s",
                "-count=1",
            ],
            timeout=900,
        )
        checks["P2P-019"] = handler_tests.returncode == 0

        oci_help = self.arkade("oci", "install", "--help")
        oci_missing_image = self.arkade("oci", "install", "--path=/tmp/x")
        checks["P2P-020"] = (
            oci_help.returncode == 0
            and has(oci_help, "--arch", "--os", "--version")
            and oci_missing_image.returncode != 0
            and "please provide an image name" in combined(oci_missing_image)
        )

        checks["P2P-021"] = self.tests_ok()

        app_help = self.arkade("install", "argocd", "--help")
        app_help_two = self.arkade("install", "openfaas", "--help")
        checks["P2P-022"] = (
            app_table.returncode == 0
            and has(app_table, "argocd", "openfaas")
            and app_help.returncode == 0
            and app_help_two.returncode == 0
        )

        checks["P2P-023"] = (
            registry_help.returncode == 0
            and has(
                registry_help,
                "--arch",
                "--docker-password",
                "--remote-url",
                "--storage",
                "--tls",
                "--type",
            )
            and oci_help.returncode == 0
            and has(oci_help, "--arch", "--os")
        )

        bad_invocation = self.arkade("get", "--nope")
        good_listing = self.arkade("get", "--format", "list")
        checks["P2P-024"] = (
            bad_invocation.returncode != 0
            and not bad_invocation.stdout.strip()
            and bool(bad_invocation.stderr.strip())
            and good_listing.returncode == 0
            and bool(good_listing.stdout.strip())
            and not good_listing.stderr.strip()
        )

        alias_invocations = [
            ("g", "--format", "list"),
            ("d", "--format", "list"),
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
            ("download", "--format", "list"),
        ]
        checks["P2P-025"] = all(
            self.arkade(*args).returncode == 0 for args in alias_invocations
        )

        version_help = self.arkade("version", "--help")
        version_json = self.arkade("version", "-j")
        try:
            version_payload = json.loads(version_json.stdout)
        except (ValueError, TypeError):
            version_payload = None
        checks["P2P-026"] = (
            version_help.returncode == 0
            and has(version_help, "--json", "-j")
            and version_json.returncode == 0
            and isinstance(version_payload, dict)
            and set(version_payload) == {"version", "commit", "build_date"}
        )

        install_help = self.arkade("install", "--help")
        checks["P2P-027"] = (
            install_help.returncode == 0
            and has(install_help, "--kubeconfig", "--wait", "--print-table")
        )

        install_guidance = self.arkade("install")
        install_tool_hint = self.arkade("install", "kubectl")
        checks["P2P-028"] = (
            install_guidance.returncode == 0
            and install_tool_hint.returncode != 0
            and "no such app. kubectl is available as a tool" in combined(install_tool_hint)
            and self.help_ok("install", "argocd")
        )

        d_alias = self.arkade("d", "--format", "list")
        docker_help = self.arkade("docker", "--help")
        checks["P2P-029"] = (
            d_alias.returncode == 0
            and has(d_alias, "helm", "kubectl")
            and docker_help.returncode == 0
            and "docker" in combined(docker_help)
        )

        search_missing = self.arkade("search")
        search_results = self.arkade("search", "k8s")
        checks["P2P-030"] = (
            search_missing.returncode != 0
            and "search query" in combined(search_missing)
            and search_results.returncode == 0
            and "found" in combined(search_results)
        )

        get_flags = [
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
        checks["P2P-031"] = get_help.returncode == 0 and has(get_help, *get_flags)

        # P2P-032 guards the public ``get`` contract, not the reference
        # implementation's Go symbol names: the package tests must pass and the
        # command must keep listing, formatting and help behaviour.
        get_default = self.arkade("get")
        get_list = self.arkade("get", "--format", "list")
        checks["P2P-032"] = (
            self.tests_ok()
            and get_help.returncode == 0
            and has(get_help, "--version", "--format")
            and get_default.returncode == 0
            and has(get_default, "kubectl")
            and get_list.returncode == 0
            and has(get_list, "helm", "kubectl")
        )

        version_text = self.arkade("version")
        checks["P2P-033"] = (
            version_text.returncode == 0
            and has(version_text, "commit:", "version:", "build date:")
            and version_json.returncode == 0
            and isinstance(version_payload, dict)
            and set(version_payload) == {"version", "commit", "build_date"}
        )

        oci_login_help = self.arkade("oci", "login", "--help")
        checks["P2P-034"] = (
            oci_login_help.returncode == 0
            and "docker" in combined(oci_login_help)
            and (
                "credential" in combined(oci_login_help)
                or "password" in combined(oci_login_help)
            )
        )

        checks["P2P-035"] = (
            release_help.returncode == 0
            and has(
                release_help,
                "--dry-run",
                "--major",
                "--minor",
                "--patch",
                "--prerelease",
                "--latest",
                "--token",
            )
        )

        checks["P2P-036"] = (
            app_help.returncode == 0
            and has(app_help, "--kubeconfig", "--wait")
        )

        search_help = self.arkade("search", "--help")
        checks["P2P-037"] = (
            search_help.returncode == 0
            and has(search_help, "--format", "table")
        )

        get_default = self.arkade("get")
        get_table = self.arkade("get", "--format", "table")
        get_list = self.arkade("get", "--format", "list")
        checks["P2P-038"] = (
            get_default.returncode == 0
            and has(get_default, "kubectl")
            and get_table.returncode == 0
            and has(get_table, "kubectl")
            and get_list.returncode == 0
            and has(get_list, "helm")
        )

        uninstall_help = self.arkade("uninstall", "--help")
        checks["P2P-039"] = (
            uninstall_help.returncode == 0
            and has(uninstall_help, "--kubeconfig", "--wait", "kubeconfig")
        )

        flag_surfaces = [
            (
                ("get", "--help"),
                [
                    "--progress",
                    "--format",
                    "--path",
                    "--version",
                    "--arch",
                    "--os",
                    "--quiet",
                    "--verify",
                    "--parallel",
                ],
            ),
            (("install", "--help"), ["--kubeconfig", "--wait", "--print-table"]),
            (("uninstall", "--help"), ["--kubeconfig", "--wait"]),
            (("version", "--help"), ["--json"]),
            (("search", "--help"), ["--format"]),
            (
                ("release", "--help"),
                [
                    "--dry-run",
                    "--major",
                    "--minor",
                    "--patch",
                    "--prerelease",
                    "--latest",
                    "--token",
                ],
            ),
            (
                ("system", "install", "registry", "--help"),
                [
                    "--arch",
                    "--docker-password",
                    "--remote-url",
                    "--storage",
                    "--tls",
                    "--type",
                ],
            ),
        ]
        flag_ok = True
        for command, flags in flag_surfaces:
            result = self.arkade(*command)
            flag_ok = flag_ok and result.returncode == 0 and has(result, *flags)
        checks["P2P-040"] = flag_ok

        return checks

    def report(self):
        if not self.build():
            return {
                observation_id: {"result": "FAIL"}
                for observation_id in OBSERVATION_IDS
            }
        checks = self.checks()
        return {
            observation_id: {
                "result": "PASS" if checks.get(observation_id, False) else "FAIL"
            }
            for observation_id in OBSERVATION_IDS
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--side", required=True, choices=["source", "target"])
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    report = Probe(args.repo).report()
    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
