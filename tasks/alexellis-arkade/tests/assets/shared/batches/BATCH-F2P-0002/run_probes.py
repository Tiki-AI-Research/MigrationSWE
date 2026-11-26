#!/usr/bin/env python3
"""Run the BATCH-F2P-0002 target probes against a repository checkout.

The same predicate is used for the source and target sides. A side passes only
when the frozen target technology (Kong 1.8.1) is on the real runtime path and
the observable command contract holds. This deliberately makes the Cobra-based
source checkout fail every F2P observation instead of grading behaviour alone.
"""

import argparse
import json
import os
import re
import subprocess
import sys


KONG_MODULE_RE = re.compile(r"github\.com/alecthomas/kong\s+v1\.8\.1")

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

# ``cmd/system/registry.go`` is an individual ``system install`` tool command
# that the instruction allows to keep Cobra, so it stays inside the permitted
# scope but is exempt from the Cobra-free requirement.
COBRA_FREE = [path for path in MIGRATED if path != "cmd/system/registry.go"]

ALLOWED_EXACT = set(MIGRATED) | {
    "cmd/system/registry.go",
    "cmd/kong.go",
    "cmd/apps_catalog.go",
    "cmd/kong_test.go",
    "cmd/get_test.go",
    "go.mod",
    "go.sum",
    "vendor/modules.txt",
}
ALLOWED_PREFIXES = ("vendor/", "internal/", "fixtures/", "shared/", "fragments/")


class Probe:
    def __init__(self, repo, binary):
        self.repo = os.path.abspath(repo)
        self.binary = binary

    def read(self, relative):
        try:
            with open(os.path.join(self.repo, relative), encoding="utf-8") as handle:
                return handle.read()
        except FileNotFoundError:
            return ""

    def run(self, *args):
        completed = subprocess.run(
            [self.binary, *args], capture_output=True, text=True, timeout=120
        )
        return completed.returncode, completed.stdout, completed.stderr

    def kong_technology(self):
        """Is Kong 1.8.1 the parser actually linked into the binary?

        Judged from the declared dependency, the module metadata baked into the
        built binary and the absence of Cobra in the migrated production files.
        The reference implementation's file names, struct names and call sites
        are implementation details, so they are never asserted.
        """
        if "github.com/alecthomas/kong v1.8.1" not in self.read("go.mod"):
            return False
        process = subprocess.run(
            ["go", "version", "-m", self.binary],
            capture_output=True,
            text=True,
        )
        if not KONG_MODULE_RE.search(process.stdout or ""):
            return False
        return all("github.com/spf13/cobra" not in self.read(path) for path in COBRA_FREE)

    def scope_policy(self):
        status = subprocess.run(
            ["git", "-C", self.repo, "status", "--porcelain"],
            capture_output=True,
            text=True,
        )
        if status.returncode != 0:
            return False
        changed = [line[3:].strip() for line in status.stdout.splitlines() if line.strip()]
        if not changed:
            return False
        for path in changed:
            if path in ALLOWED_EXACT:
                continue
            if path.startswith(ALLOWED_PREFIXES):
                continue
            if path.startswith("cmd/") and path.endswith("_test.go"):
                continue
            if path in {"pkg/get/tools.go", "pkg/helm/helm.go"}:
                return False
            return False
        return self.kong_technology()

    def root_no_subcommand(self):
        rc, out, _ = self.run()
        return (
            rc == 0
            and "Version:" in out
            and "install" in out
            and "get" in out
            and "completion" in out
        )

    def release_help(self):
        return all(
            (lambda r: r[0] == 0 and "Create a GitHub release" in r[1])(self.run(*args))
            for args in (
                ("release", "--help"),
                ("rel", "--help"),
                ("r", "--help"),
            )
        )

    def install_alias_help(self, name, canonical):
        rc, flags, out = self._help_flags("install", name)
        canonical_rc, canonical_flags, canonical_out = self._help_flags("install", canonical)
        return (rc == 0 and canonical_rc == 0 and bool(out.strip())
                and bool(canonical_out.strip()) and bool(flags)
                and flags == canonical_flags)

    def install_print_table(self):
        rc, out, _ = self.run("install", "--print-table")
        return (
            rc == 0
            and "|" in out
            and re.search(r"There are \d+ apps", out) is not None
        )

    def _help_flags(self, *args):
        rc, out, _ = self.run(*(tuple(args) + ("--help",)))
        return rc, set(re.findall(r"--[a-z0-9][a-z0-9-]*", out)), out

    def oci_alias_help(self, alias, canonical):
        """Is ``oci <alias>`` dispatched to ``oci <canonical>``?

        Reachable help plus the canonical flag surface proves the alias resolves
        to the same command.  The help description wording is not part of the
        contract, so it is never compared.
        """
        rc, alias_flags, alias_out = self._help_flags("oci", alias)
        canonical_rc, canonical_flags, _ = self._help_flags("oci", canonical)
        return (
            rc == 0
            and canonical_rc == 0
            and "Usage:" in alias_out
            and bool(alias_flags)
            and alias_flags == canonical_flags
        )

    def oci_install_help(self, alias):
        return self.oci_alias_help(alias, "install")

    def oci_publish_help(self, alias):
        return self.oci_alias_help(alias, "publish")

    def error_and_success_streams(self):
        bad_command = self.run("not-a-command")
        bad_flag = self.run("get", "--nope")
        good_json = self.run("version", "--json")
        return (
            bad_command[0] != 0
            and bad_command[1] == ""
            and bad_command[2].strip() != ""
            and bad_flag[0] != 0
            and bad_flag[1] == ""
            and bad_flag[2].strip() != ""
            and good_json[0] == 0
            and good_json[2] == ""
            and all(key in good_json[1] for key in ('"version"', '"commit"', '"build_date"'))
        )

    def install_unknown_app_errors(self):
        kubectl = self.run("install", "kubectl")
        unknown = self.run("install", "does-not-exist")
        return (
            kubectl[0] != 0
            and 'no such app. kubectl is available as a tool, run "arkade get kubectl" to get it'
            in kubectl[2]
            and unknown[0] != 0
            and 'no such app: does-not-exist, run "arkade install --help" for a list of apps'
            in unknown[2]
        )

    def get_flags(self):
        rc, out, _ = self.run("get", "--help")
        return rc == 0 and all(
            flag in out
            for flag in (
                "--progress",
                "--format",
                "--path",
                "--version",
                "--arch",
                "--os",
                "--quiet",
                "--verify",
                "--parallel",
            )
        )

    def get_flag_scope(self):
        help_rc, help_out, _ = self.run("get", "--help")
        leaked = self.run("get", "helm", "--kubeconfig=/tmp/k")
        return (
            help_rc == 0
            and "--kubeconfig" not in help_out
            and "--wait" not in help_out
            and leaked[0] != 0
        )

    def install_print_table_scope(self):
        return (
            self.install_print_table()
            and self.run("install", "argocd", "--print-table")[0] != 0
            and self.run("get", "--print-table")[0] != 0
        )

    def hidden_oci_path_flag(self):
        parse = self.run("oci", "install", "--path=/tmp/x")
        help_result = self.run("oci", "install", "--help")
        return (
            parse[0] != 0
            and "please provide an image name" in parse[2]
            and "unknown flag" not in parse[2]
            and "--path" not in help_result[1]
        )

    def presence_aware_release_bool(self):
        default = self.run("release", "v1.2.3", "t", "--dry-run")
        explicit = self.run("release", "v1.2.3", "t", "--prerelease=false", "--dry-run")
        return (
            default[0] == 0
            and "Prerelease: true" in default[1]
            and explicit[0] == 0
            and "Prerelease: false" in explicit[1]
        )

    def hidden_release_aliases(self):
        root = self.run("--help")
        return (
            root[0] == 0
            and "Create a GitHub release" not in root[1]
            and self.release_help()
        )

    def help_available(self):
        invocations = (
            ("--help",),
            ("install", "--help"),
            ("get", "--help"),
            ("release", "--help"),
            ("oci", "--help"),
            ("system", "--help"),
        )
        return all(self.run(*args)[0] == 0 for args in invocations)

    def dispatch_help_matrix(self):
        invocations = (
            ("install", "--help"),
            ("get", "--help"),
            ("release", "--help"),
            ("chart", "--help"),
            ("docker", "--help"),
            ("system", "--help"),
            ("oci", "--help"),
            ("fstail", "--help"),
        )
        return all(self.run(*args)[0] == 0 for args in invocations)

    def get_alias_d(self):
        rc, out, _ = self.run("d")
        return rc == 0 and "kubectl" in out and "helm" in out

    def top_level_aliases(self):
        invocations = (
            ("d",),
            ("g", "--help"),
            ("i", "--help"),
            ("rel", "--help"),
            ("chart", "--help"),
            ("docker", "--help"),
            ("ft", "--help"),
            ("system", "--help"),
            ("oci", "--help"),
        )
        return all(self.run(*args)[0] == 0 for args in invocations)

    def install_alias_helps(self):
        return all(
            self.install_alias_help(alias, canonical)
            for alias, canonical in (
                ("nginx-ingress", "ingress-nginx"),
                ("gatekeeper", "opa-gatekeeper"),
                ("binfmt", "qemu-static"),
                # sealed-secret is a catalogue key, not a baseline command alias.
                ("sealed-secrets", "sealed-secrets"),
                ("nfs-client-provisioner", "nfs-subdir-external-provisioner"),
            )
        )

    def system_registry_flags(self):
        rc, out, _ = self.run("s", "i", "registry", "--help")
        return rc == 0 and all(
            flag in out
            for flag in (
                "--bind-addr",
                "--docker-password",
                "--remote-url",
                "--storage",
                "--tls",
                "--type",
            )
        )

    def legacy_system_go_flags(self):
        rc, out, _ = self.run("system", "install", "go", "--help")
        return rc == 0 and "--version" in out and "--path" in out and "--progress" in out

    def legacy_command_help(self):
        invocations = (
            ("chart", "verify", "--help"),
            ("docker", "upgrade", "--help"),
            ("gha", "bump", "--help"),
            ("oci", "publish", "--help"),
            ("oci", "login", "--help"),
            ("fstail", "--help"),
            ("system", "install", "go", "--help"),
        )
        return all(self.run(*args)[0] == 0 for args in invocations)

    def oci_aliases(self):
        return (
            self.oci_install_help("i")
            and self.oci_install_help("extract")
            and self.oci_publish_help("push")
            and self.oci_publish_help("export")
        )


def checks(probe):
    return {
        "F2P-032": lambda: probe.scope_policy()
        and probe.legacy_command_help()
        and probe.kong_technology(),
        "F2P-037": lambda: probe.hidden_oci_path_flag() and probe.kong_technology(),
        "F2P-038": lambda: probe.oci_aliases() and probe.kong_technology(),
        "F2P-040": lambda: probe.presence_aware_release_bool()
        and probe.kong_technology(),
        "F2P-041": lambda: probe.install_print_table_scope() and probe.kong_technology(),
        "F2P-026": lambda: probe.hidden_release_aliases() and probe.kong_technology(),
        "F2P-030": lambda: probe.install_unknown_app_errors()
        and probe.kong_technology(),
        "F2P-035": lambda: probe.help_available() and probe.kong_technology(),
        "F2P-036": lambda: probe.root_no_subcommand()
        and probe.error_and_success_streams()
        and probe.kong_technology(),
        "F2P-039": lambda: probe.top_level_aliases()
        and probe.install_unknown_app_errors()
        and probe.kong_technology(),
        "F2P-042": lambda: probe.get_flags()
        and probe.get_flag_scope()
        and probe.kong_technology(),
        "F2P-043": lambda: probe.install_print_table_scope() and probe.kong_technology(),
        "F2P-044": lambda: probe.install_alias_helps() and probe.kong_technology(),
        "F2P-046": lambda: probe.get_flags()
        and probe.get_flag_scope()
        and probe.kong_technology(),
        "F2P-047": lambda: probe.system_registry_flags()
        and probe.legacy_system_go_flags()
        and probe.kong_technology(),
        "F2P-048": lambda: probe.install_print_table()
        and probe.install_alias_helps()
        and probe.kong_technology(),
        "F2P-045": lambda: probe.scope_policy() and probe.kong_technology(),
        "F2P-025": lambda: probe.root_no_subcommand() and probe.kong_technology(),
        "F2P-027": lambda: probe.root_no_subcommand()
        and probe.get_flags()
        and probe.get_flag_scope()
        and probe.install_print_table_scope()
        and probe.kong_technology(),
        "F2P-028": lambda: probe.get_alias_d() and probe.kong_technology(),
        "F2P-029": lambda: probe.legacy_command_help() and probe.kong_technology(),
        "F2P-031": lambda: probe.root_no_subcommand()
        and probe.dispatch_help_matrix()
        and probe.kong_technology(),
        "F2P-033": lambda: probe.error_and_success_streams() and probe.kong_technology(),
        "F2P-034": lambda: probe.hidden_release_aliases() and probe.kong_technology(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--side", choices=("source", "target"), required=True)
    args = parser.parse_args()

    probe = Probe(args.repo, args.binary)
    results = {}
    for observation_id, check in sorted(checks(probe).items()):
        passed = check()
        results[observation_id] = "PASS" if passed else "FAIL"

    print(
        json.dumps(
            {
                "batch_id": "BATCH-F2P-0002",
                "side": args.side,
                "observations": results,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if any(value == "PASS" for value in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
