#!/usr/bin/env python3
"""Side-agnostic F2P probes for BATCH-F2P-0001.

Usage:
    probe.py <repo-dir> <arkade-binary> [source|target]

The same predicate is evaluated for both sides.  The optional side argument
only decides the exit status: source must observe FAIL for every observation
and target must observe PASS for every observation.
"""

import json
import os
import re
import subprocess
import sys
import tempfile

IN_SCOPE = [
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
MSG_TOOL_HINT = 'no such app. kubectl is available as a tool, run "arkade get kubectl" to get it'
MSG_UNKNOWN_APP = 'no such app: does-not-exist, run "arkade install --help" for a list of apps'

REPO = os.path.abspath(sys.argv[1])
BIN = os.path.abspath(sys.argv[2])
SIDE = sys.argv[3] if len(sys.argv) > 3 else None
HOME = tempfile.mkdtemp(prefix="arkade-f2p-")
ENV = os.environ.copy()
ENV["HOME"] = HOME
ENV["NO_COLOR"] = "1"


def run(args, timeout=90, stdin=None):
    proc = subprocess.run(
        [BIN] + list(args),
        input=stdin,
        capture_output=True,
        text=True,
        env=ENV,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def read(rel):
    with open(os.path.join(REPO, rel), "r", encoding="utf-8") as handle:
        return handle.read()


def static_absent(tokens):
    hits = []
    for rel in IN_SCOPE:
        text = read(rel)
        for token in tokens:
            if token in text:
                hits.append("%s:%s" % (rel, token))
    return hits


def kong_linked():
    proc = subprocess.run(
        ["go", "version", "-m", BIN],
        capture_output=True,
        text=True,
        cwd=REPO,
        env=ENV,
    )
    return bool(KONG_MODULE_RE.search(proc.stdout))


def all_in(text, needles):
    return all(needle in text for needle in needles)


def help_rendered(text):
    """Is help available at all?

    The instruction fixes help *availability*, not its wording, so the exact
    prose and layout of the help page are never asserted here.  Each check
    still pins the concrete commands, aliases and flags it cares about.
    """
    return bool(text.strip())


def message_ok(actual, expected):
    """Keep the complete app error and tool hint, allowing stderr usage/context."""
    return any(line.endswith(expected) for line in actual.splitlines())


def normalized_error(stderr):
    """Lower-cased, whitespace-collapsed stderr for semantic error matching.

    The instruction fixes the *error channel and exit semantics*, never the
    exact wording: Cobra writes ``unknown flag: --nope`` while Kong writes
    ``unknown flag --nope`` (and quotes positional tokens).  Comparing
    normalized text keeps the semantic assertion (the invocation was rejected
    and the offending token was named) without pinning one framework's prose.
    """
    return re.sub(r"\s+", " ", (stderr or "")).strip().lower()


def reports_unknown_flag(stderr, flag):
    """Did the CLI reject ``flag`` and say so on stderr?"""
    message = normalized_error(stderr)
    return "unknown flag" in message and flag.lower() in message


def reports_unknown_command(stderr, token):
    """Did the CLI reject the unknown positional ``token`` on stderr?"""
    message = normalized_error(stderr)
    return token.lower() in message and (
        "unknown command" in message or "unexpected argument" in message
    )


def completion_covers(text, needles):
    """Are the command names/aliases present anywhere in a completion script?"""
    return all(needle in text for needle in needles)


def shows_flag_default(text, flag, default):
    """Does a help page document ``flag`` together with its default value?

    The instruction pins flag names, defaults and accepted values, but not the
    help layout: Cobra renders ``--parallel int ... (default 4)`` whereas Kong
    renders ``--parallel=4``.  Both state the same contract, so the assertion
    only requires the flag and its default on the same help line.
    """
    pattern = re.compile(r"--" + re.escape(flag) + r"[^\n]*\b" + re.escape(str(default)) + r"\b")
    return bool(pattern.search(text or ""))


def check_f2p_001():
    rc1, out1, err1 = run(["install", "kubectl"])
    rc2, out2, err2 = run(["install", "does-not-exist"])
    ok = (
        rc1 != 0
        and out1.strip() == ""
        and message_ok(err1.strip(), MSG_TOOL_HINT)
        and rc2 != 0
        and out2.strip() == ""
        and message_ok(err2.strip(), MSG_UNKNOWN_APP)
        and kong_linked()
    )
    return ok, "kubectl=%r/%r does-not-exist=%r/%r kong=%s" % (
        rc1, err1.strip(), rc2, err2.strip(), kong_linked())


def check_f2p_002():
    rc, out, err = run(["--help"])
    release_hidden = rc == 0 and "Create a GitHub release" not in out + err
    rc_r, out_r, _ = run(["r", "--help"])
    rc_rel, out_rel, _ = run(["rel", "--help"])
    aliases = (
        rc_r == 0
        and rc_rel == 0
        and "Create a GitHub release" in out_r
        and "Create a GitHub release" in out_rel
    )
    return release_hidden and aliases and help_rendered(out) and kong_linked(), \
        "root_rc=%d hidden=%s r=%d rel=%d help_rendered=%s" % (
            rc, release_hidden, rc_r, rc_rel, help_rendered(out))


def check_f2p_003():
    rc, out, err = run([])
    ok = (
        rc == 0
        and "Version:" in out
        and all_in(out, ["install", "get", "completion"])
        and help_rendered(out)
        and kong_linked()
    )
    return ok, "rc=%d version=%s tokens=%s help_rendered=%s" % (
        rc, "Version:" in out,
        all_in(out, ["install", "get", "completion"]), help_rendered(out))


def check_f2p_004():
    hits = static_absent(["github.com/spf13/cobra"])
    # Kong owns the migrated command tree: proven by the module metadata baked
    # into the built binary, not by *which* file contains the import.  The
    # instruction does not prescribe a file layout, so requiring the import to
    # appear in a pinned file list would fail a legal migration that mounts the
    # Kong root in a file of its own.
    ok = not hits and kong_linked()
    return ok, "cobra_hits=%s kong_linked=%s" % (hits, kong_linked())


def check_f2p_005():
    rc1, out1, _ = run(["get", "--help"])
    rc2, out2, _ = run(["get", "--format", "list"])
    ok = (
        rc1 == 0
        and help_rendered(out1)
        and rc2 == 0
        and "kubectl" in out2
        and "helm" in out2
        and kong_linked()
    )
    return ok, "help_rc=%d help_rendered=%s list_rc=%d tools=%s" % (
        rc1, help_rendered(out1), rc2, all_in(out2, ["kubectl", "helm"]))


def check_f2p_006():
    """App aliases must still dispatch to the app they name.

    ``nginx-ingress``, ``gatekeeper``, ``binfmt`` and ``nfs-client-provisioner``
    are registered command aliases in the baseline, so the alias and the
    canonical spelling must render the same app page (the app list is unordered
    and help layout is not part of the contract, so pages are compared as sorted
    line sets).  ``sealed-secret`` is a catalogue key rather than a command
    alias, so only its continued presence in ``install --print-table`` is
    asserted; requiring the alias help page to change would be a new behaviour
    the instruction does not ask for.
    """
    aliases = {
        "nginx-ingress": "ingress-nginx",
        "gatekeeper": "opa-gatekeeper",
        "binfmt": "qemu-static",
        "nfs-client-provisioner": "nfs-subdir-external-provisioner",
    }
    results = {}
    for alias, canonical in aliases.items():
        rc_alias, out_alias, _ = run(["install", alias, "--help"])
        rc_canonical, out_canonical, _ = run(["install", canonical, "--help"])
        results[alias] = (
            rc_alias == 0
            and rc_canonical == 0
            and bool(out_alias.strip())
            and sorted(set(out_alias.splitlines())) == sorted(set(out_canonical.splitlines())),
            rc_alias,
        )
    rc_table, out_table, _ = run(["install", "--print-table"])
    results["sealed-secret"] = (rc_table == 0 and "sealed-secret" in out_table, rc_table)
    _, install_help, _ = run(["install", "--help"])
    ok = all(ok for ok, _ in results.values()) and help_rendered(install_help) and kong_linked()
    return ok, "aliases=%s install_help=%s" % (results, help_rendered(install_help))


def check_f2p_008():
    rc_app, out_app, _ = run(["install", "argocd", "--help"])
    rc_install, out_install, _ = run(["install", "--help"])
    # --print-table is local to install even when it is explicitly false.
    # Use the read-only info child to check both sides of the child token.
    scoped = True
    for flag in ("--print-table", "--print-table=false"):
        for args in (["install", flag, "info", "argocd"],
                     ["install", "info", "argocd", flag]):
            rc, out, err = run(args)
            scoped = scoped and rc != 0 and not out.strip() and bool(err.strip())
    ok = (
        rc_app == 0
        and all_in(out_app, ["--kubeconfig", "--wait"])
        and rc_install == 0
        and "--print-table" in out_install
        and help_rendered(out_install)
        and scoped
        and kong_linked()
    )
    return ok, "app=%d flags=%s install=%d print_table=%s help_rendered=%s local_scope=%s" % (
        rc_app, all_in(out_app, ["--kubeconfig", "--wait"]), rc_install,
        "--print-table" in out_install, help_rendered(out_install), scoped)


def check_f2p_009():
    legacy = [
        ["chart", "verify", "--help"],
        ["docker", "upgrade", "--help"],
        ["gha", "bump", "--help"],
        ["oci", "publish", "--help"],
        ["oci", "login", "--help"],
        ["fstail", "--help"],
        ["system", "install", "go", "--help"],
    ]
    results = {}
    for args in legacy:
        rc, out, err = run(args)
        results[" ".join(args[:-1])] = (rc == 0 and ("Usage:" in out or "Flags:" in out), rc)
    rc_chart, out_chart, _ = run(["chart", "--help"])
    ok = (
        all(ok for ok, _ in results.values())
        and rc_chart == 0
        and help_rendered(out_chart)
        and kong_linked()
    )
    return ok, "legacy=%s chart_rc=%d chart_help_rendered=%s" % (
        results, rc_chart, help_rendered(out_chart))


def check_f2p_010():
    rc, out, _ = run(["d"])
    rc_help, out_help, _ = run(["d", "--help"])
    ok = (
        rc == 0
        and all_in(out, ["kubectl", "helm"])
        and rc_help == 0
        and help_rendered(out_help)
        and "get" in out_help
        and kong_linked()
    )
    return ok, "rc=%d tools=%s d_help_rc=%d help_rendered=%s" % (
        rc, all_in(out, ["kubectl", "helm"]), rc_help, help_rendered(out_help))


def check_f2p_012():
    rc, out, err = run(["--help"])
    hidden = rc == 0 and "Create a GitHub release" not in out + err
    aliases = {}
    for name in ("release", "rel", "r"):
        arc, aout, _ = run([name, "--help"])
        aliases[name] = (arc == 0 and "Create a GitHub release" in aout, arc)
    ok = hidden and all(ok for ok, _ in aliases.values()) and help_rendered(out) and kong_linked()
    return ok, "hidden=%s aliases=%s help_rendered=%s" % (
        hidden, aliases, help_rendered(out))


def check_f2p_013():
    rc_reg, out_reg, _ = run(["s", "i", "registry", "--help"])
    rc_go, out_go, _ = run(["system", "install", "go", "--help"])
    registry_flags = ["--bind-addr", "--docker-password", "--remote-url", "--storage", "--tls", "--type"]
    go_flags = ["--path", "--progress", "--version"]
    # ``registry`` is an individual ``system install`` tool command, which the
    # instruction allows to stay on Cobra.  Judge it by behaviour only (exit
    # status and the preserved flags), never by the Kong help marker.
    registry_help = rc_reg == 0 and ("Usage:" in out_reg or "Flags:" in out_reg)
    ok = (
        registry_help
        and all_in(out_reg, registry_flags)
        and rc_go == 0
        and all_in(out_go, go_flags)
        and kong_linked()
    )
    return ok, "registry=%d help=%s flags=%s go=%d go_flags=%s" % (
        rc_reg, registry_help, all_in(out_reg, registry_flags),
        rc_go, all_in(out_go, go_flags))


def check_f2p_014():
    rc_b, out_b, err_b = run(["completion", "bash"])
    rc_z, out_z, err_z = run(["completion", "zsh"])
    rc_f, out_f, _ = run(["completion", "fish"])
    rc_p, out_p, _ = run(["completion", "powershell"])
    rc_t, out_t, err_t = run(["completion", "tcsh"])
    # The instruction requires every command, alias and subcommand of the
    # migrated tree to appear in the generated scripts; the script layout and
    # whether flags are completed at all are explicitly exempt from
    # byte-for-byte matching, so only commands and aliases are asserted.
    bash_ok = rc_b == 0 and completion_covers(out_b, ["arkade", "install", "download", "delete"])
    zsh_ok = rc_z == 0 and completion_covers(out_z, ["arkade", "install", "download", "delete"])
    fish_ok = rc_f == 0 and completion_covers(out_f, ["complete", "arkade", "install"])
    psh_ok = rc_p == 0 and completion_covers(out_p, ["arkade", "install"])
    ok = (
        bash_ok
        and zsh_ok
        and fish_ok
        and psh_ok
        and rc_t != 0
        and "not supported" in normalized_error(err_t)
        and kong_linked()
    )
    return ok, "bash=%d/%s zsh=%d/%s fish=%d/%s pwsh=%d/%s tcsh=%d/%r" % (
        rc_b, bash_ok, rc_z, zsh_ok, rc_f, fish_ok, rc_p, psh_ok, rc_t, err_t.strip())


def check_f2p_017():
    rc_cmd, out_cmd, err_cmd = run(["not-a-command"])
    rc_flag, out_flag, err_flag = run(["get", "--nope"])
    rc_json, out_json, err_json = run(["version", "--json"])
    try:
        payload = json.loads(out_json)
    except Exception:
        payload = {}
    ok = (
        rc_cmd != 0
        and out_cmd.strip() == ""
        and reports_unknown_command(err_cmd, "not-a-command")
        and rc_flag != 0
        and out_flag.strip() == ""
        and reports_unknown_flag(err_flag, "--nope")
        and rc_json == 0
        and err_json.strip() == ""
        and all_in(payload, ["version", "commit", "build_date"])
        and kong_linked()
    )
    return ok, "cmd=%d/%r flag=%d/%r json=%d/%s" % (
        rc_cmd, err_cmd.strip(), rc_flag, err_flag.strip(), rc_json, sorted(payload))


def check_f2p_018():
    bridge_tokens = ['"os/exec"', "exec.Command(", "net/rpc", "plugin.Open", "//go:linkname"]
    bridge_hits = static_absent(bridge_tokens)
    cobra_hits = static_absent(["github.com/spf13/cobra"])
    rc_list, out_list, _ = run(["get", "--format", "list"])
    _, out_help, _ = run(["--help"])
    wrapper_exposed = "__default" in out_help or "\n  cobra" in out_help or "\n  legacy" in out_help
    ok = (
        not bridge_hits
        and not cobra_hits
        and not wrapper_exposed
        and rc_list == 0
        and all_in(out_list, ["kubectl", "helm"])
        and help_rendered(out_help)
        and kong_linked()
    )
    return ok, "bridge=%s cobra=%s wrapper=%s list=%d" % (
        bridge_hits, cobra_hits, wrapper_exposed, rc_list)


def check_f2p_020():
    rc_help, out_help, _ = run(["get", "--help"])
    rc_flag, out_flag, err_flag = run(["get", "helm", "--kubeconfig=/tmp/k"])
    ok = (
        rc_help == 0
        and "--kubeconfig" not in out_help
        and "--wait" not in out_help
        and help_rendered(out_help)
        and rc_flag != 0
        and out_flag.strip() == ""
        and reports_unknown_flag(err_flag, "--kubeconfig")
        and kong_linked()
    )
    return ok, "help=%d scoped=%s flag=%d/%r" % (
        rc_help, ("--kubeconfig" not in out_help and "--wait" not in out_help),
        rc_flag, err_flag.strip())


def check_f2p_007():
    hits = static_absent(["github.com/spf13/cobra"])
    # See check_f2p_004: the Kong link is judged from the built binary, and the
    # capability itself is exercised through the CLI below.
    rc, out, _ = run(["get", "--format", "list"])
    ok = not hits and rc == 0 and all_in(out, ["kubectl", "helm"]) and kong_linked()
    return ok, "cobra_hits=%s list=%d kong_linked=%s" % (hits, rc, kong_linked())


def check_f2p_011():
    rc, out, _ = run(["d"])
    rc_help, out_help, _ = run(["d", "--help"])
    ok = (
        rc == 0
        and all_in(out, ["kubectl", "helm"])
        and rc_help == 0
        and "arkade get" in out_help
        and help_rendered(out_help)
        and kong_linked()
    )
    return ok, "rc=%d tools=%s help=%d get_node=%s help_rendered=%s" % (
        rc, all_in(out, ["kubectl", "helm"]), rc_help, "arkade get" in out_help,
        help_rendered(out_help))


def check_f2p_015():
    rc_get, out_get, _ = run(["get", "--help"])
    rc_install, out_install, _ = run(["install", "--help"])
    rc_version, out_version, _ = run(["version", "--help"])
    rc_search, out_search, _ = run(["search", "--help"])
    rc_uninstall, out_uninstall, _ = run(["uninstall", "--help"])
    rc_release, out_release, _ = run(["release", "--help"])
    get_flags = ["--progress", "--format", "-o", "--path", "--version", "-v",
                 "--arch", "--os", "--quiet", "--verify", "--parallel", "-p"]
    ok = (
        rc_get == 0 and all_in(out_get, get_flags) and "4" in out_get
        and help_rendered(out_get)
        and rc_install == 0
        and all_in(out_install, ["--kubeconfig", "--wait", "--print-table"])
        and help_rendered(out_install)
        and rc_version == 0 and all_in(out_version, ["--json", "-j"]) and help_rendered(out_version)
        and rc_search == 0 and "--format" in out_search and "table" in out_search and help_rendered(out_search)
        and rc_uninstall == 0 and all_in(out_uninstall, ["--kubeconfig", "--wait"]) and help_rendered(out_uninstall)
        and rc_release == 0
        and all_in(out_release, ["--dry-run", "--major", "--minor", "--patch",
                                 "--prerelease", "--latest", "--token"])
        and kong_linked()
    )
    return ok, "get=%d install=%d version=%d search=%d uninstall=%d release=%d help_rendered=%s" % (
        rc_get, rc_install, rc_version, rc_search, rc_uninstall, rc_release,
        [help_rendered(x) for x in (out_get, out_install, out_version, out_search, out_uninstall)])


def check_f2p_016():
    rc_app, out_app, _ = run(["install", "argocd", "--help"])
    rc_install, out_install, _ = run(["install", "--help"])
    ok = (
        rc_app == 0
        and all_in(out_app, ["--kubeconfig", "--wait"])
        and rc_install == 0
        and "--print-table" in out_install
        and help_rendered(out_install)
        and kong_linked()
    )
    return ok, "app=%d flags=%s install=%d print_table=%s help_rendered=%s" % (
        rc_app, all_in(out_app, ["--kubeconfig", "--wait"]), rc_install,
        "--print-table" in out_install, help_rendered(out_install))


def check_f2p_019():
    commands = [
        ["--help"],
        ["install", "--help"],
        ["get", "--help"],
        ["version", "--help"],
        ["search", "--help"],
        ["completion", "--help"],
        ["release", "--help"],
        ["chart", "--help"],
        ["docker", "--help"],
        ["fstail", "--help"],
        ["gha", "--help"],
        ["system", "--help"],
        ["oci", "--help"],
        ["system", "install", "go", "--help"],
        ["chart", "verify", "--help"],
    ]
    results = {}
    for args in commands:
        rc, out, err = run(args)
        results[" ".join(args[:-1]) or "root"] = (rc == 0 and ("Usage:" in out or "Flags:" in out), rc)
    rc_root, out_root, _ = run(["--help"])
    ok = all(ok for ok, _ in results.values()) and help_rendered(out_root) and kong_linked()
    return ok, "commands=%s root_help=%s" % (results, help_rendered(out_root))


def check_f2p_023():
    return check_f2p_013()


def check_f2p_024():
    rc_root, out_root, _ = run(["--help"])
    legacy = [
        ["chart", "verify", "--help"],
        ["oci", "publish", "--help"],
        ["system", "install", "go", "--help"],
    ]
    results = {}
    for args in legacy:
        rc, out, _ = run(args)
        results[" ".join(args[:-1])] = (rc == 0 and ("Usage:" in out or "Flags:" in out), rc)
    rc_d, out_d, _ = run(["d"])
    ok = (
        rc_root == 0
        and help_rendered(out_root)
        and all(ok for ok, _ in results.values())
        and rc_d == 0
        and all_in(out_d, ["kubectl", "helm"])
        and kong_linked()
    )
    return ok, "root_rc=%d help_rendered=%s legacy=%s d=%d" % (
        rc_root, help_rendered(out_root), results, rc_d)


def check_f2p_021():
    rc, out, err = run(["--help"])
    hidden = rc == 0 and "Create a GitHub release" not in out + err and help_rendered(out)
    alias_ok = True
    for name in ("r", "rel"):
        arc, aout, _ = run([name, "--help"])
        alias_ok = alias_ok and arc == 0 and "Create a GitHub release" in aout
    return hidden and alias_ok and kong_linked(), \
        "root_rc=%d hidden=%s help_rendered=%s aliases=%s" % (
            rc, hidden, help_rendered(out), alias_ok)


def check_f2p_022():
    rc_cmd, out_cmd, err_cmd = run(["not-a-command"])
    rc_flag, out_flag, err_flag = run(["get", "--nope"])
    rc_json, out_json, _ = run(["version", "--json"])
    try:
        payload = json.loads(out_json)
    except Exception:
        payload = {}
    rc_list, out_list, _ = run(["get", "--format", "list"])
    ok = (
        rc_cmd != 0
        and out_cmd.strip() == ""
        and reports_unknown_command(err_cmd, "not-a-command")
        and rc_flag != 0
        and out_flag.strip() == ""
        and reports_unknown_flag(err_flag, "--nope")
        and rc_json == 0
        and all_in(payload, ["version", "commit", "build_date"])
        and rc_list == 0
        and all_in(out_list, ["kubectl", "helm"])
        and kong_linked()
    )
    return ok, "cmd=%d flag=%d json=%d list=%d" % (rc_cmd, rc_flag, rc_json, rc_list)


CHECKS = {
    "F2P-001": check_f2p_001,
    "F2P-002": check_f2p_002,
    "F2P-003": check_f2p_003,
    "F2P-004": check_f2p_004,
    "F2P-005": check_f2p_005,
    "F2P-006": check_f2p_006,
    "F2P-008": check_f2p_008,
    "F2P-009": check_f2p_009,
    "F2P-010": check_f2p_010,
    "F2P-012": check_f2p_012,
    "F2P-013": check_f2p_013,
    "F2P-014": check_f2p_014,
    "F2P-017": check_f2p_017,
    "F2P-018": check_f2p_018,
    "F2P-020": check_f2p_020,
    "F2P-007": check_f2p_007,
    "F2P-011": check_f2p_011,
    "F2P-015": check_f2p_015,
    "F2P-016": check_f2p_016,
    "F2P-019": check_f2p_019,
    "F2P-023": check_f2p_023,
    "F2P-024": check_f2p_024,
    "F2P-021": check_f2p_021,
    "F2P-022": check_f2p_022,
}


def main():
    results = {}
    for obs_id, check in CHECKS.items():
        try:
            ok, detail = check()
        except Exception as exc:  # infrastructure failure is not a semantic result
            results[obs_id] = {"status": "INFRA_ERROR", "detail": repr(exc)}
            continue
        results[obs_id] = {"status": "PASS" if ok else "FAIL", "detail": detail}

    print(json.dumps(results, indent=2, sort_keys=True))
    if SIDE:
        expected = "FAIL" if SIDE == "source" else "PASS"
        mismatched = [key for key, value in results.items() if value["status"] != expected]
        return 1 if mismatched else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
