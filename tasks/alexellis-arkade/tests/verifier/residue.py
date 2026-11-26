"""Language-neutral evaluation of declarative residue rules."""

from __future__ import annotations

import base64
import binascii
from collections import Counter
import json
import os
import re
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Iterable, Mapping, Sequence


_EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "target",
    "dist",
    "build",
    "coverage",
}
_RULE_KINDS = {"literal", "dependency", "import", "file"}

# Only these out-of-scope leaf paths may reach the Cobra compatibility adapter.
# Pin both the command path and constructor so an in-scope constructor cannot
# be hidden behind a permitted key.
_LEGACY_CONSTRUCTORS = {
    "chart verify": "chart.MakeVerify", "chart upgrade": "chart.MakeUpgrade",
    "chart bump": "chart.MakeBump", "docker upgrade": "docker.MakeUpgrade",
    "docker gen": "docker.MakeGen", "fstail": "fstail.MakeFstail",
    "gha bump": "gha.MakeBump", "oci publish": "oci.MakeOciPublish",
    "oci login": "oci.MakeOciLogin",
    "system install go": "system.MakeInstallGo",
    "system install firecracker": "system.MakeInstallFirecracker",
    "system install prometheus": "system.MakeInstallPrometheus",
    "system install cni": "system.MakeInstallCNI",
    "system install containerd": "system.MakeInstallContainerd",
    "system install actions-runner": "system.MakeInstallActionsRunner",
    "system install node": "system.MakeInstallNode",
    "system install tc-redirect-tap": "system.MakeInstallTCRedirectTap",
    "system install gitlab-runner": "system.MakeInstallGitLabRunner",
    "system install buildkitd": "system.MakeInstallBuildkitd",
    "system install pwsh": "system.MakeInstallPowershell",
    "system install caddy": "system.MakeInstallCaddyServer",
    "system install zvol-snapshotter": "system.MakeInstallZvolSnapshotter",
    "system install node_exporter": "system.MakeInstallNodeExporter",
}
_LEGACY_LEAVES = {
    ("cmd/kong.go", "Fstail"), ("cmd/kong.go", "Upgrade"),
    ("cmd/kong.go", "Gen"), ("cmd/kong.go", "Bump"),
    ("cmd/chart/chart.go", "Verify"), ("cmd/chart/chart.go", "Upgrade"),
    ("cmd/chart/chart.go", "Bump"), ("cmd/oci/oci.go", "Publish"),
    ("cmd/oci/oci.go", "Login"),
    *(("cmd/system/install.go", field) for field in (
        "Go", "Firecracker", "Prometheus", "CNI", "Containerd",
        "ActionsRunner", "Node", "TCRedirectTap", "GitLabRunner",
        "Buildkitd", "Powershell", "Caddy", "ZvolSnapshotter", "NodeExporter",
    )),
}
_GO_TOKENS = re.compile(
    r'(?s)//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|`[^`]*`|'
    r"'(?:\\.|[^'\\])*'|[A-Za-z_]\w*|\S"
)
_PROCESS_SINKS = {
    "syscall": {
        "Exec",
        "Syscall", "Syscall6", "RawSyscall", "RawSyscall6",
    },
    "golang.org/x/sys/unix": {
        "Exec", "Execve",
        "Syscall", "Syscall6", "RawSyscall", "RawSyscall6",
    },
    "plugin": {"Open"},
}
# Packages whose mere import already proves an executable bridge.  ``syscall``,
# ``os`` and ``os/exec`` are deliberately absent: the migrated commands legitimately
# inspect signals (``signal.Notify(ch, syscall.SIGTERM)``) and run fixed
# subcommands (``git``, ``gh``, Helm).  ``os/exec`` is judged per call instead:
# only a process target that comes from the environment/loopback, or that is
# named like a bridge, is a delegation to a source runtime.
_EXECUTION_IMPORTS = {"C", "plugin"}
_EXECUTION_IMPORT_PREFIXES = ("net/rpc",)
# Position of the executable-name argument for the process sinks that are legal
# with a fixed command and deceptive with a runtime-supplied one.
_CONDITIONAL_PROCESS_SINKS = {
    "os/exec": {"Command": 0, "CommandContext": 1},
    "os": {"StartProcess": 0},
    "syscall": {"ForkExec": 0, "StartProcess": 0},
    "golang.org/x/sys/unix": {"ForkExec": 0, "StartProcess": 0},
}
# The only executables a migrated command may start with a *fixed* target: the
# CLI shells out to these tools by design (``git``/``gh`` for releases, Helm for
# chart work, kubectl for cluster work).  A fixed target is not a blanket
# exemption: a subprocess that names a toolchain or interpreter can run a legacy
# implementation the candidate shipped inside its own tree, which is exactly the
# source-runtime fallback the migration must not keep.  So a fixed target stays
# legal only while none of its arguments points back into the repository.
_FIXED_PROCESS_TOOLS = {
    "git", "gh", "helm", "kubectl", "docker", "tar", "unzip", "curl", "wget",
}
# A string literal that points back into the candidate tree: a relative path, a
# path under a tree that holds packaged fixtures/third-party code, or a Go
# source file.  Such an argument hands the process a source implementation
# instead of an external tool.
_REPO_PATH_LITERAL = re.compile(
    r"^\.{1,2}/|(?:^|/)(?:testdata|third_party|fixtures|legacy|old|internal/konglegacy)/|\.go$"
)
# ``net/http`` is the ordinary transport for the target implementation (remote
# chart/asset downloads), so importing or calling it is not by itself a bridge.
# Only a request whose target is runtime-supplied (environment/loopback) or that
# is routed through an explicitly named bridge endpoint is a delegation to a
# source runtime.
_HTTP_CLIENT_PACKAGES = {"net/http"}
_HTTP_REQUEST_METHODS = {
    "Get", "Head", "Post", "PostForm",
    "NewRequest", "NewRequestWithContext",
}
# Position of the URL argument for each request constructor/method.
_HTTP_URL_ARGUMENT = {
    "Get": 0, "Head": 0, "Post": 0, "PostForm": 0,
    "NewRequest": 1, "NewRequestWithContext": 1,
}
# ``net`` is the ordinary transport for a migrated CLI as well (the commands
# resolve and reach remote hosts), so importing it is not a bridge.  A *dial*
# whose address comes from the environment/loopback, or is named like a bridge,
# is a raw TCP/RPC delegation to a source runtime and is judged per call site.
_NETWORK_PACKAGES = {"net", "net/rpc"}
_NETWORK_DIAL_ARGUMENT = {
    "Dial": 1, "DialTimeout": 1, "DialIP": 1, "DialTCP": 1,
    "DialUDP": 1, "DialUnix": 1,
}
# Address position for the dial methods of a ``net.Dialer`` value: the receiver
# is the dialer, so ``Dial`` starts at the network argument and
# ``DialContext`` at the context one.
_DIALER_METHOD_ARGUMENT = {"Dial": 1, "DialContext": 2}
_RPC_DIAL_ARGUMENT = {"Dial": 1, "DialHTTP": 1, "DialHTTPPath": 1}
_ENV_READERS = {"Getenv", "LookupEnv"}
_LOOPBACK_ENDPOINT = re.compile(
    r"(?i)(?:127\.\d{1,3}\.\d{1,3}\.\d{1,3}|0\.0\.0\.0|localhost|\[::1\]|::1)"
)
# A URL handed to the HTTP client whose *name* advertises a bridge is treated as
# a delegation even when its origin is not statically resolvable.
_BRIDGE_ENDPOINT_NAME = re.compile(
    r"(?i)(?:bridge|legacy|fallback|source_?runtime|old_?runtime|loopback)"
)
# Tokens that describe an unmigrated Cobra CLI rather than a runtime bridge back
# into the source implementation.  They fail the migration (scope/residue) but
# are not cheating.
_ORDINARY_BRIDGE_TOKENS = (
    "in-scope Kong passthrough command",
    "in-scope Cobra import",
    "Cobra import outside the dispatch scope",
    "cobra usage outside the dispatch scope",
    "Cobra constructor outside the legacy allowlist",
    "unlisted legacy Cobra constructor",
    "Cobra command constructed outside every approved constructor",
    "Cobra constructor literal outside every approved constructor",
    "Cobra constructor declared as a method",
)
# Raw syscall primitives are also the legitimate route for ioctl/terminal work,
# so the syscall number decides instead of the call itself.
_RAW_SYSCALL_METHODS = {"Syscall", "Syscall6", "RawSyscall", "RawSyscall6"}
_EXECUTION_SYSCALLS = {
    "SYS_EXECVE", "SYS_EXECVEAT", "SYS_EXEC", "SYS_FORK", "SYS_VFORK", "SYS_CLONE", "SYS_CLONE3",
}
# Directories that never hold migrated production source: dependencies, test
# fixtures and build/cache output.  Production trees are ``cmd/`` and
# ``internal/``; anything outside them is scanned or reported, never trusted.
_SOURCE_SKIP_PARTS = _EXCLUDED_PARTS | {"vendor", "testdata"}
# The residue enumeration deliberately does **not** inherit the ``vendor`` /
# ``testdata`` prune above.  Those trees are not compiled into the migrated
# binary, but a whole implementation parked in one of them is still residue: the
# migration is only complete when the legacy source is gone, not "gone from the
# build path".  Pruning them let a candidate hide ``cmd/info.go.bak`` as
# ``cmd/testdata/info_legacy.go.bak`` and still score a perfect scope ratio.
# Only VCS/cache/build output stays out of the moved-content scan, and a Go
# source found there is reported as an uncertainty instead of being dropped.
_RESIDUE_SKIP_PARTS = frozenset(_EXCLUDED_PARTS)
# A candidate source is a text file; anything this large is a binary blob and is
# never read as Go.
_MAX_GO_SOURCE_BYTES = 4 * 1024 * 1024
# Trees that define command wiring and therefore get the strict dispatch rules.
_DISPATCH_PREFIXES = ("cmd/", "internal/")
# The bridge scan covers the candidate's whole production surface, not only the
# command wiring: a source-runtime fallback hidden in a library package is the
# same delegation as one in ``cmd/``, and it is the natural place to put it when
# the command wiring is being watched.  Kept separate from the dispatch prefixes
# so widening the scan does not silently change the command-surface rules.
_SOURCE_SCAN_PREFIXES = ("cmd/", "internal/", "pkg/")
_MIGRATED_PRODUCTION = {
    "main.go", "cmd/get.go", "cmd/install.go", "cmd/info.go",
    "cmd/update.go", "cmd/uninstall.go", "cmd/version.go", "cmd/search.go",
    "cmd/completion.go", "cmd/release.go", "cmd/chart/chart.go",
    "cmd/system/system.go", "cmd/system/install.go", "cmd/system/registry.go",
    "cmd/oci/oci.go", "cmd/oci/install.go", "cmd/apps/argocd_app.go",
}
# The pinned legacy adapter is identified by *role*, not by file name: the
# candidate chooses where to mount it (``instruction.md`` does not prescribe a
# layout), so the boundary is enforced as "exactly one file holds the adapter"
# instead of "the adapter must be ``cmd/kong.go``".  ``_legacy_adapter_files``
# derives that file set from the code; a migration that merely renames the
# reference's adapter file must not be reported as a source fallback.
def _legacy_adapter_files(codes: Mapping[str, Sequence[str]]) -> set[str]:
    """Files that hold the pinned legacy adapter, identified by role.

    The adapter is the file that registers ``konglegacy.SetExecutor`` and/or
    references the ``legacyConstructors`` map; those are the two things that
    make the pinned dispatch reachable.  Importing ``internal/konglegacy`` is
    part of the same role (a file that only imports it to mount commands does
    not register anything).
    """
    files: set[str] = set()
    for relative, code in codes.items():
        if "legacyConstructors" in code:
            files.add(relative)
        for index, token in enumerate(code):
            if token == "SetExecutor" and not _go_is_declaration(code, index):
                files.add(relative)
    return files


def _uses_legacy_package(code: Sequence[str]) -> bool:
    """Does this file use the pinned adapter package in an approved role?

    Mounting the permitted out-of-scope groups needs three things from
    ``internal/konglegacy``: the ``Leaf`` field type on an adapter struct, the
    ``Default`` marker on a group root, and (in the adapter file itself) the
    executor/constructor-map wiring.  Anything else that pulls the package in is
    a new escape and stays a finding.
    """
    if "legacyConstructors" in code:
        return True
    for index, token in enumerate(code):
        if token != "konglegacy" or index + 1 >= len(code) or code[index + 1] != ".":
            continue
        following = code[index + 2] if index + 2 < len(code) else ""
        if following in {"Leaf", "Default", "PrintParentUsage", "SetExecutor"}:
            return True
    return False
# ``instruction.md`` permits every ``system install`` tool command to keep its
# Cobra constructor.  ``registry`` is the one tool command whose migration is
# optional: a candidate may migrate it to Kong or keep it on the pinned
# compatibility adapter.  Both are correct, so the file stays in
# ``_MIGRATED_PRODUCTION`` (it must exist) while Cobra usage in it is optional
# rather than fatal.
_OPTIONAL_COBRA_SOURCES = {"cmd/system/registry.go"}
_OPTIONAL_COBRA_CONSTRUCTORS = frozenset({"MakeInstallRegistry"})
# The dispatch/Leaf inventory may grow by the optional registry tool command,
# but nothing else: every other field and constructor stays pinned.
_OPTIONAL_LEGACY_LEAVES = {("cmd/system/install.go", "Registry")}
_OPTIONAL_LEGACY_CONSTRUCTORS = {"system install registry": "system.MakeInstallRegistry"}

# The only Cobra usage that may survive the migration: the out-of-scope command
# groups that ``instruction.md`` allows to stay on Cobra.  Each entry pins the
# file *and* the ``*cobra.Command`` constructors it may declare, so a new helper
# cannot inherit an exemption by living in one of these packages.  Every entry
# must be disjoint from ``_MIGRATED_PRODUCTION``; bridge sinks are checked in
# these files exactly like everywhere else.
_COBRA_COMPAT_SOURCES = {
    "cmd/apps/cassandra_app.go":            frozenset({"MakeInstallCassandra"}),
    "cmd/apps/certmanager_app.go":          frozenset({"MakeInstallCertManager"}),
    "cmd/apps/chart_app.go":                frozenset({"MakeInstallChart"}),
    "cmd/apps/cockroachdb_app.go":          frozenset({"MakeInstallCockroachdb"}),
    "cmd/apps/confluentkafka.go":           frozenset({"MakeInstallConfluentPlatformKafka"}),
    "cmd/apps/consul_app.go":               frozenset({"MakeInstallConsul"}),
    "cmd/apps/cronconnector_app.go":        frozenset({"MakeInstallCronConnector"}),
    "cmd/apps/crossplane_app.go":           frozenset({"MakeInstallCrossplane"}),
    "cmd/apps/falco.go":                    frozenset({"MakeInstallFalco"}),
    "cmd/apps/gitea_app.go":                frozenset({"MakeInstallGitea"}),
    "cmd/apps/gitlab_app.go":               frozenset({"MakeInstallGitLab"}),
    "cmd/apps/grafana_app.go":              frozenset({"MakeInstallGrafana"}),
    "cmd/apps/influxdb_app.go":             frozenset({"MakeInstallinfluxdb"}),
    "cmd/apps/inletsoperator_app.go":       frozenset({"MakeInstallInletsOperator"}),
    "cmd/apps/istio_app.go":                frozenset({"MakeInstallIstio"}),
    "cmd/apps/jenkins_app.go":              frozenset({"MakeInstallJenkins"}),
    "cmd/apps/kafkaconnector_app.go":       frozenset({"MakeInstallKafkaConnector"}),
    "cmd/apps/kong_ingress_app.go":         frozenset({"MakeInstallKongIngress"}),
    "cmd/apps/kube_image_prefetch_app.go":  frozenset({"MakeInstallKubeImagePrefetch"}),
    "cmd/apps/kube_state_metrics.go":       frozenset({"MakeInstallKubeStateMetrics"}),
    "cmd/apps/kubernetes_dashboard_app.go": frozenset({"MakeInstallKubernetesDashboard"}),
    "cmd/apps/kuma_app.go":                 frozenset({"MakeInstallKuma"}),
    "cmd/apps/kyverno_app.go":              frozenset({"MakeInstallKyverno"}),
    "cmd/apps/linkerd_app.go":              frozenset({"MakeInstallLinkerd"}),
    "cmd/apps/loki_app.go":                 frozenset({"MakeInstallLoki"}),
    "cmd/apps/metallb_app.go":              frozenset({"MakeInstallMetalLB"}),
    "cmd/apps/metricsserver_app.go":        frozenset({"MakeInstallMetricsServer"}),
    "cmd/apps/minio_app.go":                frozenset({"MakeInstallMinio"}),
    "cmd/apps/mongodb_app.go":              frozenset({"MakeInstallMongoDB"}),
    "cmd/apps/mqttconnector_app.go":        frozenset({"MakeInstallMQTTConnector"}),
    "cmd/apps/nats_connector.go":           frozenset({"MakeInstallNATSConnector"}),
    "cmd/apps/nfs_app.go":                  frozenset({"MakeInstallNfsProvisioner"}),
    "cmd/apps/nginx_app.go":                frozenset({"MakeInstallNginx"}),
    "cmd/apps/of_loki.go":                  frozenset({"MakeInstallOpenFaaSLoki"}),
    "cmd/apps/opa_gatekeeper.go":           frozenset({"MakeInstallOPAGateKeeper"}),
    "cmd/apps/openfaas_app.go":             frozenset({"MakeInstallOpenFaaS"}),
    "cmd/apps/openfaas_ce_app.go":          frozenset({"MakeInstallOpenFaaSCE"}),
    "cmd/apps/openfaas_ingress_app.go":     frozenset({"MakeInstallOpenFaaSIngress"}),
    "cmd/apps/portainer_app.go":            frozenset({"MakeInstallPortainer"}),
    "cmd/apps/postgres_app.go":             frozenset({"MakeInstallPostgresql"}),
    "cmd/apps/prometheus_app.go":           frozenset({"MakeInstallPrometheus"}),
    "cmd/apps/qemu-static.go":              frozenset({"MakeInstallQemuStatic"}),
    "cmd/apps/rabbitmq_app.go":             frozenset({"MakeInstallRabbitmq"}),
    "cmd/apps/redis_app.go":                frozenset({"MakeInstallRedis"}),
    "cmd/apps/registry_app.go":             frozenset({"MakeInstallRegistry"}),
    "cmd/apps/registry_creds_app.go":       frozenset({"MakeInstallRegistryCredsOperator"}),
    "cmd/apps/registry_ingress_app.go":     frozenset({"MakeInstallRegistryIngress"}),
    "cmd/apps/sealed_secret_app.go":        frozenset({"MakeInstallSealedSecrets"}),
    "cmd/apps/tekton_app.go":               frozenset({"MakeInstallTekton"}),
    "cmd/apps/traefik2_app.go":             frozenset({"MakeInstallTraefik2"}),
    "cmd/apps/vault_app.go":                frozenset({"MakeInstallVault"}),
    "cmd/apps/waypoint_app.go":             frozenset({"MakeInstallWaypoint"}),
    "cmd/apps_catalog.go":                  frozenset({}),
    "cmd/chart/bump.go":                    frozenset({"MakeBump"}),
    "cmd/chart/upgrade.go":                 frozenset({"MakeUpgrade"}),
    "cmd/chart/verify.go":                  frozenset({"MakeVerify"}),
    "cmd/docker/docker.go":                 frozenset({"MakeDocker"}),
    "cmd/docker/gen.go":                    frozenset({"MakeGen"}),
    "cmd/docker/upgrade.go":                frozenset({"MakeUpgrade"}),
    "cmd/fstail/fstail.go":                 frozenset({"MakeFstail"}),
    "cmd/gha/gha.go":                       frozenset({"MakeBump", "MakeGHA"}),
    "cmd/gha/upgrade.go":                   frozenset({"MakeUpgrade"}),
    "cmd/oci/login.go":                     frozenset({"MakeOciLogin"}),
    "cmd/oci/publish.go":                   frozenset({"MakeOciPublish"}),
    "cmd/system/actions_runner.go":         frozenset({"MakeInstallActionsRunner"}),
    "cmd/system/buildkitd.go":              frozenset({"MakeInstallBuildkitd"}),
    "cmd/system/caddy.go":                  frozenset({"MakeInstallCaddyServer"}),
    "cmd/system/cni.go":                    frozenset({"MakeInstallCNI"}),
    "cmd/system/containerd.go":             frozenset({"MakeInstallContainerd"}),
    "cmd/system/firecracker.go":            frozenset({"MakeInstallFirecracker"}),
    "cmd/system/gitlab_runner.go":          frozenset({"MakeInstallGitLabRunner"}),
    "cmd/system/go.go":                     frozenset({"MakeInstallGo"}),
    "cmd/system/node.go":                   frozenset({"MakeInstallNode"}),
    "cmd/system/nodeexporter.go":           frozenset({"MakeInstallNodeExporter"}),
    "cmd/system/powershell.go":             frozenset({"MakeInstallPowershell"}),
    "cmd/system/prometheus.go":             frozenset({"MakeInstallPrometheus"}),
    "cmd/system/tc_redirect_tap.go":        frozenset({"MakeInstallTCRedirectTap"}),
    "cmd/system/zvol_snapshotter.go":       frozenset({"MakeInstallZvolSnapshotter"}),
}

def _constructs_cobra_command(code: Sequence[str], imports: Mapping[str, str]) -> bool:
    """Does this file build a Cobra command of its own?

    ``&cobra.Command{...}`` (or the same literal through an import alias) is a
    command definition.  A file that only receives ``*cobra.Command`` values and
    mounts them is an adapter, and mounting the permitted out-of-scope groups is
    exactly what ``instruction.md`` allows.  The qualifier is resolved through the
    import table, so ``&kong.Command{}`` (Kong aliases ``Command`` to ``Node``) is
    not mistaken for a Cobra command.
    """
    # Resolve the actual imported type. A local adapter named Command may hold
    # an allowed Cobra leaf without being a Cobra command itself. Conversely,
    # new(cobra.Command) and aliases of cobra.Command are real constructions.
    command_types, _ = _cobra_command_types(code, imports)
    if imports.get(".") == "github.com/spf13/cobra":
        command_types.add(("Command",))
    return bool(_command_constructions(code, command_types))


def _is_cobra_passthrough(
    code: Sequence[str], text: str, imports: Mapping[str, str]
) -> bool:
    """Is this file a pass-through adapter for permitted out-of-scope commands?

    ``instruction.md`` lets the out-of-scope command groups stay on Cobra and only
    requires them to hang off the Kong root, so a helper that receives those
    commands and mounts them is legal. Storing a received command or factory
    does not construct a command surface. Constructors and executable bridge
    sinks remain checked separately, including inside adapter files.
    """
    if _constructs_cobra_command(code, imports):
        return False
    # The exception covers adapters for the permitted Cobra leaves. It does
    # not cover code which also constructs/parses the migrated Kong root.
    # Resolve the import alias and inspect code tokens so comments, filenames
    # and unrelated packages named kong cannot affect this decision.
    for alias, package in imports.items():
        if package != "github.com/alecthomas/kong":
            continue
        for name in ("New", "Parse"):
            needle = [name, "("] if alias == "." else [alias, ".", name, "("]
            if any(list(code[index:index + len(needle)]) == needle
                   for index in range(len(code))):
                return False
    return True


def _go_matching_brace(text: str, opening: int) -> int:
    """Index of the brace that closes ``text[opening]`` (``-1`` if unbalanced)."""
    depth = 0
    for index in range(opening, len(text)):
        character = text[index]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _declares_cobra_command_field(text: str) -> bool:
    """Does any declared struct hold a Cobra command value?"""
    for match in re.finditer(r"\btype\s+[A-Za-z_]\w*\s+struct\s*\{", text):
        brace = text.find("{", match.end() - 1)
        if brace < 0:
            continue
        end = _go_matching_brace(text, brace)
        if end <= brace:
            continue
        body = text[brace + 1:end]
        if re.search(r"\*\s*(?:[A-Za-z_]\w*\.)?Command\b", body):
            return True
    return False


def _cobra_compat_packages() -> dict[str, frozenset[str]]:
    """Allowed Cobra constructors per *package directory*, for out-of-scope packages.

    ``instruction.md`` fixes *which commands* may stay on Cobra; it does not
    prescribe the file layout of those commands, so a candidate may move
    ``MakeBump`` into ``cmd/gha/bump.go`` and keep the command reachable.  Only
    packages that contain **no** in-scope production file get this cross-file
    tolerance: a directory that also holds migrated code stays pinned per file,
    so an allowed constructor name cannot be claimed by an unrelated file.
    """
    migrated_dirs = {
        path.rsplit("/", 1)[0] if "/" in path else "" for path in _MIGRATED_PRODUCTION
    }
    packages: dict[str, set[str]] = {}
    for relative, names in _COBRA_COMPAT_SOURCES.items():
        directory = relative.rsplit("/", 1)[0] if "/" in relative else ""
        packages.setdefault(directory, set()).update(names)
    return {
        directory: frozenset(names)
        for directory, names in packages.items()
        if directory and directory not in migrated_dirs
    }


_COBRA_COMPAT_BY_PACKAGE = _cobra_compat_packages()
# Variable-valued constructors are a distinct declaration form from a pinned
# top-level function.  The reference declares none, so nothing is accepted as a
# ``var Name = func() *cobra.Command``; the empty set keeps that decision
# explicit instead of inferring it from the name alone.
_APPROVED_VAR_CONSTRUCTORS: frozenset[str] = frozenset()


# The package clause is the only clause a Go file may open with, so a file whose
# first *token* is ``package <ident>`` is Go source whatever it is named.
_GO_PACKAGE_CLAUSE = re.compile(r"package[ \t]+[A-Za-z_]\w*")


def _first_go_token(text: str) -> int | None:
    """Index of the first Go token, skipping whitespace and comments.

    Comments are trivia in Go: ``/* ... */`` (including one that spans several
    lines) and ``// ...`` both sit legally before the package clause and carry
    no tokens of their own.  Skipping them here keeps the content judgement
    aligned with ``_GO_TOKENS``, which drops the same trivia before the other
    rules see the file.  A byte order mark is legal as the first byte of a Go
    source file, so it is trivia too.  Returns ``None`` when the file holds
    nothing but trivia (or an unterminated block comment).
    """
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character.isspace() or (index == 0 and character == "\ufeff"):
            index += 1
            continue
        if text.startswith("//", index):
            newline = text.find("\n", index)
            if newline < 0:
                return None
            index = newline + 1
            continue
        if text.startswith("/*", index):
            closing = text.find("*/", index + 2)
            if closing < 0:
                return None
            index = closing + 2
            continue
        return index
    return None


# A source file's identity is its content, not its name.  A whole migrated
# implementation copied aside as ``cmd/info.go.bak`` (or ``.orig``, no suffix, a
# ``.txt`` snapshot) used to be invisible to a ``*.go`` glob, so the residue scan
# now accepts any file that opens with the package clause.  Kept deliberately
# strict -- the clause has to be the *first token*, not merely present -- so
# prose that only *contains* code (a markdown note with fenced snippets) is never
# tokenised as a source, while a leading block comment cannot hide one either.
def _is_go_source_file(path: Path) -> bool:
    """Is this file Go source, judged by content?"""
    try:
        if path.stat().st_size > _MAX_GO_SOURCE_BYTES:
            return False
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    start = _first_go_token(text)
    if start is None:
        return False
    return _GO_PACKAGE_CLAUSE.match(text, start) is not None


def _go_sources(root: Path, uncertain: list[str]) -> list[tuple[str, Path]]:
    """Enumerate the production Go source the bridge scan must inspect.

    The surface is ``*.go`` at the repository root plus ``cmd/**``,
    ``internal/**`` and ``pkg/**``.  ``_test.go`` files are out of scope, and so
    is VCS/cache/build output; a source that is unreadable, symlinked or hidden
    there is reported instead of being dropped.  Dependencies (``vendor``) and
    fixtures (``testdata``) stay out of the *build* surface, but a full copy of
    an implementation under either name is still residue, so the content pass
    below reads them rather than trusting the directory name.
    """
    collected: list[Path] = []
    try:
        collected.extend(sorted(root.glob("*.go")))
    except OSError:
        uncertain.append("repository root is not enumerable")
    for prefix in _SOURCE_SCAN_PREFIXES:
        base = root / prefix.rstrip("/")
        if base.is_symlink() or (base.exists() and not base.is_dir()):
            uncertain.append(f"production tree is not a directory: {prefix}")
            continue
        if not base.is_dir():
            continue
        try:
            collected.extend(sorted(base.rglob("*.go")))
            for entry in sorted(base.rglob("*")):
                if entry.is_symlink() and (entry.is_dir() or not entry.exists()):
                    uncertain.append(f"symlinked production tree needs review: {entry.relative_to(root).as_posix()}")
        except OSError:
            uncertain.append(f"production tree is not enumerable: {prefix}")
    # Content pass: a copy of a source under any name (``.bak``/``.orig``/no
    # suffix/``.txt``) is the same artefact, so it is collected wherever it sits
    # outside the cache/build trees -- ``vendor``/``testdata`` included, because
    # a name is not a guarantee and a hidden copy is exactly the evasion this
    # pass exists to catch.  ``*.go`` files stay on the named surface above, so
    # widening this does not silently change which plain sources are scanned.
    try:
        for path in sorted(root.rglob("*")):
            if path.suffix == ".go" or path.is_symlink() or not path.is_file():
                continue
            try:
                parts = path.relative_to(root).parts
            except ValueError:
                continue
            if set(parts) & _RESIDUE_SKIP_PARTS:
                continue
            if _is_go_source_file(path):
                collected.append(path)
    except OSError:
        uncertain.append("repository root is not enumerable")
    found: dict[str, Path] = {}
    for path in collected:
        if path.name.endswith("_test.go"):
            continue
        try:
            relative = path.relative_to(root)
        except ValueError:
            uncertain.append(f"production source escapes the candidate root: {path.name}")
            continue
        skipped = [part for part in relative.parts if part in _RESIDUE_SKIP_PARTS]
        if skipped:
            if any(part in _EXCLUDED_PARTS for part in skipped):
                uncertain.append(f"production source in a cache or build directory: {relative.as_posix()}")
            continue
        if path.is_symlink():
            uncertain.append(f"symlinked production source needs review: {relative.as_posix()}")
            continue
        if not path.is_file():
            uncertain.append(f"unreadable production source: {relative.as_posix()}")
            continue
        found[relative.as_posix()] = path
    return [(relative, found[relative]) for relative in sorted(found)]


def _go_imports(code: Sequence[str]) -> tuple[dict[str, str], list[str]]:
    """Parse the import declarations of one file: alias -> package path.

    Returns the resolved imports plus syntax issues (an unclosed block), so both
    the dispatch scan and the independent production-scope pass read imports the
    same way instead of each owning a slightly different parser.
    """
    imports: dict[str, str] = {}
    issues: list[str] = []
    for index, token in enumerate(code):
        if token != "import" or index + 1 >= len(code):
            continue
        start = index + 1
        end = start + 1
        if code[start] == "(":
            while end < len(code) and code[end] != ")":
                end += 1
            if end == len(code):
                issues.append("unclosed import block")
        else:
            end = min(start + 2, len(code))
        for pos in range(start, end):
            item = code[pos]
            if not (item.startswith('"') and item.endswith('"')):
                continue
            package = item[1:-1]
            previous = code[pos - 1] if pos > start else ""
            alias = previous if re.fullmatch(r"[A-Za-z_]\w*", previous) or previous == "." else package.rsplit("/", 1)[-1]
            imports[alias] = package
    return imports, issues


def _production_scope_sources(
    root: Path, uncertain: list[str], scanned: set[str]
) -> list[tuple[str, Path]]:
    """Enumerate build sources outside the dispatch trees, never silently.

    ``cmd/`` and ``internal/`` already get the full dispatch analysis; every
    other tree the Go build compiles (``pkg/**`` above all) is enumerated here so
    a package outside the dispatch scope cannot re-open the Cobra fallback or the
    legacy executor unseen.  Dependencies, fixtures and VCS/cache directories are
    pruned, but anything hiding Go sources there is reported instead of trusted.
    ``scanned`` holds the paths the dispatch pass already produced.
    """
    found: dict[str, Path] = {}
    if not root.is_dir():
        uncertain.append("repository root is not a directory")
        return []
    try:
        for current, directories, files in os.walk(root):
            base = Path(current)
            top = base == root
            keep: list[str] = []
            for directory in sorted(directories):
                child = base / directory
                relative = child.relative_to(root).as_posix()
                if top and f"{directory}/" in _DISPATCH_PREFIXES:
                    # ``cmd/`` and ``internal/`` get the full dispatch analysis,
                    # including their own cache/symlink reporting; walking them
                    # again here would report the same condition twice.
                    continue
                if directory in _SOURCE_SKIP_PARTS:
                    if (
                        directory not in {".git", "vendor", "testdata"}
                        and next(child.rglob("*.go"), None) is not None
                    ):
                        uncertain.append(
                            f"production source in a cache or build directory: {relative}"
                        )
                    continue
                if child.is_symlink():
                    uncertain.append(f"symlinked production tree needs review: {relative}")
                    continue
                keep.append(directory)
            directories[:] = keep
            for name in sorted(files):
                if not name.endswith(".go") or name.endswith("_test.go"):
                    continue
                path = base / name
                relative = path.relative_to(root).as_posix()
                if relative in scanned:
                    continue
                if path.is_symlink():
                    uncertain.append(f"symlinked production source needs review: {relative}")
                    continue
                if not path.is_file():
                    uncertain.append(f"unreadable production source: {relative}")
                    continue
                found[relative] = path
    except OSError:
        uncertain.append("repository tree is not enumerable")
    return [(relative, found[relative]) for relative in sorted(found)]


def _outside_dispatch_issues(
    relative: str, code: Sequence[str]
) -> tuple[list[str], list[str]]:
    """Hits/uncertainties for a production file outside ``cmd/`` and ``internal/``.

    No tree outside the dispatch scope may hold command wiring, so the bar here
    is deliberately absolute rather than shape-aware: any Cobra import or use,
    any legacy executor import or use, and any reference to the frozen
    constructor map is a hit.  The reference tree has none of these in ``pkg/**``,
    so nothing legitimate is excluded.
    """
    hits: list[str] = []
    uncertain: list[str] = []
    imports, issues = _go_imports(code)
    for issue in issues:
        uncertain.append(f"{issue}: {relative}")
    for package in imports.values():
        if package == "github.com/spf13/cobra":
            hits.append(f"Cobra import outside the dispatch scope: {relative}")
        if package == "internal/konglegacy" or package.endswith("/internal/konglegacy"):
            hits.append(f"legacy executor import outside the dispatch scope: {relative}")
    for index, token in enumerate(code):
        if token in {"cobra", "konglegacy"}:
            following = code[index + 1] if index + 1 < len(code) else ""
            if following == ".":
                hits.append(f"{token} usage outside the dispatch scope: {relative}")
        elif token == "SetExecutor":
            hits.append(f"legacy executor declared or registered outside the dispatch scope: {relative}")
        elif token == "legacyConstructors":
            hits.append(f"legacy constructor map referenced outside the dispatch scope: {relative}")
    return list(dict.fromkeys(hits)), uncertain


def _go_balanced(code: list[str], index: int) -> int | None:
    """Index of the bracket closing the group opened at ``index``, if any."""
    depth = 0
    for position in range(index, len(code)):
        token = code[position]
        if token in {"(", "[", "{"}:
            depth += 1
        elif token in {")", "]", "}"}:
            depth -= 1
            if depth == 0:
                return position
    return None


def _go_open(code: Sequence[str], index: int) -> int | None:
    """Index of the bracket opening the group closed at ``index``, if any."""
    pairs = {")": "(", "]": "[", "}": "{"}
    opener = pairs.get(code[index])
    if opener is None:
        return None
    depth = 0
    for position in range(index, -1, -1):
        token = code[position]
        if token in {")", "]", "}"}:
            depth += 1
        elif token in {"(", "[", "{"}:
            depth -= 1
            if depth == 0:
                return position if token == opener else None
    return None


def _go_brace_depths(code: Sequence[str]) -> list[int]:
    """Brace nesting depth *before* each token; package scope is zero."""
    depths: list[int] = []
    depth = 0
    for token in code:
        if token == "}":
            depth = max(depth - 1, 0)
        depths.append(depth)
        if token == "{":
            depth += 1
    return depths


def _go_is_declaration(code: Sequence[str], index: int) -> bool:
    """Whether ``code[index]`` is the name declared by a ``func`` signature.

    Distinguishes a declaration (``func SetExecutor``, ``func (r *T) Name``)
    from a call or value reference (``konglegacy.SetExecutor``), so moving the
    legacy executor boundary itself is not mistaken for a second registration.
    """
    cursor = index - 1
    if cursor >= 0 and code[cursor] == "func":
        return True
    if cursor < 0 or code[cursor] != ")":
        return False
    opening = _go_open(code, cursor)
    if opening is None:
        return False
    if opening >= 1 and code[opening - 1] == "func":
        return True
    if (
        opening >= 2
        and _GO_IDENTIFIER.fullmatch(code[opening - 1])
        and code[opening - 2] == ")"
    ):
        receiver = _go_open(code, opening - 2)
        return receiver is not None and receiver >= 1 and code[receiver - 1] == "func"
    return False


_GO_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
# Keywords are not declaration names, so a ``type`` spec scan must not mistake
# them for the identifier that introduces an alias or a defined type.
_GO_KEYWORDS = {
    "break", "case", "chan", "const", "continue", "default", "defer", "else",
    "fallthrough", "for", "func", "go", "goto", "if", "import", "interface",
    "map", "package", "range", "return", "select", "struct", "switch", "type",
    "var",
}
# Tokens that start a new declaration: seeing one while walking a result list
# means the previous ``func`` was a type or a body-less declaration.
_GO_DECLARATION_STARTS = {"func", "var", "type", "const", "import", "package"}
# Tokens that cannot continue a Go result list, so a body-less ``func`` type
# (``func() *cobra.Command`` in a field, parameter or map value) stops here
# instead of swallowing the next unrelated function body.
_GO_RESULT_TERMINATORS = {")", "]", "}", ",", ";", "=", ":"}
_GO_VALUE_PRECEDERS = {"=", ",", "(", ":", "return", "[", "&", "{", "||", "&&", "?"}


def _go_split_top_level(code: Sequence[str], separator: str) -> list[list[str]]:
    """Split ``code`` on ``separator`` tokens that sit outside any bracket."""
    parts: list[list[str]] = []
    current: list[str] = []
    depth = 0
    for token in code:
        if token in {"(", "[", "{"}:
            depth += 1
        elif token in {")", "]", "}"}:
            depth -= 1
        if token == separator and depth <= 0:
            parts.append(current)
            current = []
            continue
        current.append(token)
    parts.append(current)
    return [part for part in parts if part]


def _go_strip_result_name(code: Sequence[str]) -> list[str]:
    """Drop the optional declared name(s) from one result-type clause."""
    tokens = list(code)
    while len(tokens) >= 2 and _GO_IDENTIFIER.fullmatch(tokens[0]) and _GO_IDENTIFIER.fullmatch(tokens[1]):
        tokens = tokens[1:]
    if len(tokens) >= 2 and _GO_IDENTIFIER.fullmatch(tokens[0]) and tokens[1] in {"*", "[", "("}:
        tokens = tokens[1:]
    return tokens


def _is_command_type(code: Sequence[str], command_types: set[tuple[str, ...]]) -> bool:
    """Whether a result/type clause resolves to the Cobra command type."""
    tokens = list(code)
    while tokens and tokens[0] == "&":
        tokens = tokens[1:]
    while tokens and tokens[0] == "*":
        tokens = tokens[1:]
    if tokens and tokens[0] == "(" and _go_balanced(tokens, 0) == len(tokens) - 1:
        tokens = tokens[1:-1]
        while tokens and tokens[0] == "*":
            tokens = tokens[1:]
    return tuple(tokens) in command_types


def _returns_command(results: Sequence[str], command_types: set[tuple[str, ...]]) -> bool:
    """Whether a result list declares a Cobra command result (named or not)."""
    tokens = list(results)
    if not tokens:
        return False
    if tokens[0] == "(":
        if _go_balanced(tokens, 0) != len(tokens) - 1:
            return False
        return any(
            _is_command_type(_go_strip_result_name(part), command_types)
            for part in _go_split_top_level(tokens[1:-1], ",")
        )
    return _is_command_type(_go_strip_result_name(tokens), command_types)


def _go_source_tokens(text: str) -> tuple[list[str], set[int]]:
    """Tokenize ``text`` and record where each source line starts.

    Comments and a leading byte order mark are dropped (every rule is token
    based, and a BOM before ``package`` is legal Go), and the index of the first
    token on each new source line is kept so ``type`` specs can be told apart
    without changing the token stream the other rules consume.
    """
    code: list[str] = []
    breaks: set[int] = set()
    previous_end = 0
    for match in _GO_TOKENS.finditer(text):
        token = match.group()
        if token.startswith(("//", "/*")) or token == "\ufeff":
            previous_end = match.end()
            continue
        if code and "\n" in text[previous_end:match.start()]:
            breaks.add(len(code))
        code.append(token)
        previous_end = match.end()
    return code, breaks


def _command_type_span(code: Sequence[str], start: int, end: int) -> tuple[int, int]:
    """Widen a command spelling to the pointer/parenthesised type around it."""
    while True:
        widened = False
        if start >= 1 and code[start - 1] == "*":
            start -= 1
            widened = True
        elif (
            start >= 2
            and code[start - 1] == "("
            and end < len(code)
            and code[end] == ")"
            and not _GO_IDENTIFIER.fullmatch(code[start - 2])
            and code[start - 2] not in {")", "]", "."}
        ):
            # ``type X = (cobra.Command)`` is a parenthesised type; ``new(cobra.Command)``
            # and ``f(cobra.Command)`` are calls and keep their own shape.
            start -= 1
            end += 1
            widened = True
        if not widened:
            return start, end


def _command_type_occurrences(
    code: Sequence[str], command_types: set[tuple[str, ...]]
) -> list[tuple[int, int]]:
    """Every span of ``code`` that spells a Cobra command type."""
    occurrences: list[tuple[int, int]] = []
    if not command_types:
        return occurrences
    index = 0
    while index < len(code):
        width = next(
            (len(candidate) for candidate in command_types if tuple(code[index:index + len(candidate)]) == candidate),
            0,
        )
        if not width:
            index += 1
            continue
        start, end = _command_type_span(code, index, index + width)
        occurrences.append((start, end))
        index = end
    return occurrences


def _type_spec_spans(
    code: Sequence[str], start: int, end: int, breaks: set[int]
) -> list[tuple[int, int, str | None]]:
    """Split one ``type`` spec list into per-spec ``(rhs_start, end, name)``.

    ``start``/``end`` bound the spec list of a ``type ( … )`` group; specs are
    separated by a semicolon or a line break at the group's own depth.  Each
    spec's optional name (and generic parameter list) is skipped, so the right
    hand side of ``A = cobra.Command`` starts at ``=`` and its name is ``A``.
    """
    spans: list[tuple[int, int, str | None]] = []
    cursor = start
    while cursor < end:
        segment_end = end
        depth = 0
        probe = cursor
        while probe < end:
            token = code[probe]
            if token in {"(", "[", "{"}:
                depth += 1
            elif token in {")", "]", "}"}:
                depth -= 1
            elif depth == 0 and (token == ";" or (probe > cursor and probe in breaks)):
                segment_end = probe
                break
            probe += 1
        name = None
        position = cursor
        if (
            position < segment_end
            and _GO_IDENTIFIER.fullmatch(code[position])
            and code[position] not in _GO_KEYWORDS
        ):
            name = code[position]
            position += 1
        if position < segment_end and code[position] == "[":
            parameters = _go_balanced(list(code), position)
            if parameters is not None and parameters < segment_end:
                position = parameters + 1
        if position < segment_end:
            spans.append((position, segment_end, name))
        cursor = segment_end + 1 if code[segment_end:segment_end + 1] == [";"] else segment_end
    return spans


def _type_declaration_spans(
    code: Sequence[str], breaks: set[int]
) -> list[tuple[int, int, str | None]]:
    """Spans of the type expression declared by each ``type`` spec.

    A single ``type Name = …`` ends at the next source line, at a closing bracket
    at the spec's own depth or at a semicolon; a ``type ( … )`` group is split
    into per-spec spans.  The declared name is kept so a resolved alias can be
    named when the right-hand side is decorated.
    """
    spans: list[tuple[int, int, str | None]] = []
    index = 0
    while index < len(code):
        if code[index] != "type":
            index += 1
            continue
        cursor = index + 1
        if cursor < len(code) and code[cursor] == "(":
            end = _go_balanced(code, cursor)
            if end is None:
                spans.extend(_type_spec_spans(code, cursor + 1, len(code), breaks))
                break
            spans.extend(_type_spec_spans(code, cursor + 1, end, breaks))
            index = end + 1
            continue
        name = None
        if (
            cursor < len(code)
            and _GO_IDENTIFIER.fullmatch(code[cursor])
            and code[cursor] not in _GO_KEYWORDS
        ):
            name = code[cursor]
            cursor += 1
        if cursor < len(code) and code[cursor] == "[":
            parameters = _go_balanced(list(code), cursor)
            if parameters is None:
                break
            cursor = parameters + 1
        end = len(code)
        depth = 0
        probe = cursor
        while probe < len(code):
            token = code[probe]
            if token in {"(", "[", "{"}:
                depth += 1
            elif token in {")", "]", "}"}:
                if depth == 0:
                    end = probe
                    break
                depth -= 1
            elif depth == 0 and (token == ";" or (probe > cursor and probe in breaks)):
                end = probe
                break
            probe += 1
        spans.append((cursor, end, name))
        index = max(end, index + 1)
    return spans


def _declared_command_types(
    code: Sequence[str],
    command_types: set[tuple[str, ...]],
    breaks: set[int],
) -> list[tuple[str | None, bool, bool]]:
    """Cobra command types declared by a ``type`` spec.

    Returns ``(name, alias, resolved)`` per declaration: whether the spec aliases
    (``=``) or defines a new type, and whether the right-hand side is exactly one
    of the known spellings, so the declared name can be tracked transitively.
    Occurrences inside a ``struct { … }`` body (field types) and inside a
    ``func`` type are ordinary type *uses* and are not reported.
    """
    spans = _type_declaration_spans(code, breaks)
    if not spans:
        return []
    structures = _go_struct_bodies(code)
    declared: list[tuple[str | None, bool, bool]] = []
    for start, end in _command_type_occurrences(code, command_types):
        span = next((item for item in spans if item[0] <= start < item[1]), None)
        if span is None:
            continue
        if any(open < start < close for open, close in structures):
            continue
        origin, _, name = span
        prefix = list(code[origin:start])
        if "func" in prefix:
            # ``type Installer func() *cobra.Command`` is a function type, which is
            # the approved shape; it is not a renamed command type.
            continue
        alias = "=" in prefix
        stripped = list(code[start:end])
        while stripped and stripped[0] == "*":
            stripped = stripped[1:]
        if len(stripped) >= 2 and stripped[0] == "(" and _go_balanced(stripped, 0) == len(stripped) - 1:
            stripped = stripped[1:-1]
            while stripped and stripped[0] == "*":
                stripped = stripped[1:]
        declared.append((name, alias, tuple(stripped) in command_types))
    return declared


def _cobra_command_types(
    code: Sequence[str],
    imports: Mapping[str, str],
    breaks: set[int] | frozenset[int] = frozenset(),
) -> tuple[set[tuple[str, ...]], list[str]]:
    """Local spellings of the Cobra command type, plus analysis warnings.

    Covers the canonical import, an aliased import and every ``type`` spec whose
    right-hand side is a Cobra command: a plain alias, a pointer alias, a
    parenthesised alias, a chained alias and a defined type.  The reference
    declares no such type, so each declaration is reported for review instead of
    being silently trusted; the name is still added to the spelling set, so a
    constructor that returns it is enumerated and hits the allowlist.
    """
    aliases = {
        alias
        for alias, package in imports.items()
        if package == "github.com/spf13/cobra" and alias not in {".", "_"}
    }
    types: set[tuple[str, ...]] = {(alias, ".", "Command") for alias in aliases}
    issues: list[str] = []
    if "." in {alias for alias, package in imports.items() if package == "github.com/spf13/cobra"}:
        issues.append("dot-imported Cobra package needs review")
    seen: set[tuple[str, ...]] = set()
    while types != seen:
        seen = set(types)
        for name, alias, resolved in _declared_command_types(code, types, breaks):
            if name is None:
                issues.append("unnamed Cobra command type declaration needs review")
                continue
            kind = "Cobra command type alias" if alias else "declared Cobra command type"
            issues.append(f"{kind} needs review: {name}")
            if resolved:
                types.add((name,))
    return types, list(dict.fromkeys(issues))


def _go_signature(code: Sequence[str], index: int) -> dict[str, object] | None:
    """Parse the ``func`` at ``index``: name, receiver, results and body span."""
    cursor = index + 1
    receiver = False
    if cursor < len(code) and code[cursor] == "(":
        end = _go_balanced(code, cursor)
        if end is None:
            return None
        following = end + 1
        if (
            following + 1 < len(code)
            and _GO_IDENTIFIER.fullmatch(code[following])
            and code[following + 1] == "("
        ):
            cursor = following  # method receiver
            receiver = True
    name: str | None = None
    if cursor < len(code) and _GO_IDENTIFIER.fullmatch(code[cursor]):
        name = code[cursor]
        cursor += 1
    if cursor < len(code) and code[cursor] == "[":
        end = _go_balanced(code, cursor)
        if end is None:
            return None
        cursor = end + 1
    if cursor >= len(code) or code[cursor] != "(":
        return None
    end = _go_balanced(code, cursor)
    if end is None:
        return None
    cursor = end + 1
    results_start = cursor
    body: int | None = None
    while cursor < len(code) and code[cursor] != "{":
        token = code[cursor]
        if token in {"(", "["}:
            nested = _go_balanced(code, cursor)
            if nested is None:
                return None
            cursor = nested + 1
            continue
        if token in _GO_DECLARATION_STARTS or token in _GO_RESULT_TERMINATORS:
            break
        cursor += 1
    if cursor < len(code) and code[cursor] == "{":
        body = cursor
    return {
        "name": name,
        "receiver": receiver,
        "results": list(code[results_start:cursor]),
        "body": body,
        "close": _go_balanced(code, body) if body is not None else None,
    }


def _go_functions(
    code: Sequence[str], command_types: set[tuple[str, ...]]
) -> list[dict[str, object]]:
    """Every function declaration/literal that returns a Cobra command.

    Each entry keeps its *declaration identity*: whether it is a method
    (``receiver``) and whether it sits at package scope (``top_level``).  The
    allowlist pins top-level, receiver-less constructors, so a method that
    happens to reuse an approved name must never inherit that identity.
    """
    functions: list[dict[str, object]] = []
    depths = _go_brace_depths(code)
    index = 0
    while index < len(code):
        if code[index] != "func":
            index += 1
            continue
        if index and code[index - 1] == "]":
            # ``map[K]func() *cobra.Command{...}`` (and slices): the trailing
            # brace belongs to the composite literal, not to a function body.
            index += 1
            continue
        signature = _go_signature(code, index)
        if signature is None:
            index += 1
            continue
        name = signature["name"]
        if name is None and (index == 0 or code[index - 1] not in _GO_VALUE_PRECEDERS):
            # A body-less function type such as ``Installer func() *cobra.Command``
            # is a type position, not a constructor.
            index += 1
            continue
        if signature["body"] is None or signature["close"] is None:
            index += 1
            continue
        if _returns_command(signature["results"], command_types):
            functions.append(
                {
                    "index": index,
                    "name": name,
                    "receiver": bool(signature["receiver"]),
                    "top_level": depths[index] == 0,
                    "body": signature["body"],
                    "close": signature["close"],
                    "returns": True,
                }
            )
        index += 1
    return functions


def _var_command_hooks(code: Sequence[str], functions: Sequence[Mapping[str, object]]) -> set[str]:
    """Package values declared as ``var Name = func() *cobra.Command {...}``."""
    by_index = {entry["index"]: entry for entry in functions}
    hooks: set[str] = set()
    index = 0
    while index < len(code):
        if code[index] != "var":
            index += 1
            continue
        cursor = index + 1
        names: list[str] = []
        while cursor < len(code) and _GO_IDENTIFIER.fullmatch(code[cursor]):
            names.append(code[cursor])
            cursor += 1
            if cursor < len(code) and code[cursor] == ",":
                cursor += 1
                continue
            break
        if names and cursor < len(code) and code[cursor] == "=":
            cursor += 1
        if names and cursor < len(code) and code[cursor] == "func" and cursor in by_index:
            entry = by_index[cursor]
            if not entry["receiver"] and entry["top_level"]:
                hooks.update(names)
        index += 1
    return hooks


def _command_container_literal(
    code: Sequence[str], index: int, width: int
) -> int | None:
    """The literal opened by a container whose element type is a command.

    Handles ``[]T``, ``[]*T``, ``[N]T`` and ``map[K]T`` (and their pointer
    forms): Go writes the element type once at the container, so the elements
    that follow are elided ``{…}`` values with no command spelling of their own.
    An index/selector expression such as ``commands[i]`` never matches because
    the bracket group is not a type, and a signature result such as
    ``func Build() []cobra.Command {`` is excluded by the value-preceder guard:
    its ``{`` opens a body, not a composite literal.
    """
    lead = index - 1 if index >= 1 and code[index - 1] == "*" else index
    if lead < 1 or code[lead - 1] != "]":
        return None
    opening = _go_open(code, lead - 1)
    if opening is None:
        return None
    if opening >= 1 and code[opening - 1] == "map":
        container_start = opening - 1
    elif all(
        token == "." or re.fullmatch(r"\d+", token)
        for token in code[opening + 1:lead - 1]
    ):
        container_start = opening
    else:
        return None
    if container_start:
        preceder = code[container_start - 1]
        if preceder not in _GO_VALUE_PRECEDERS and preceder != "]":
            return None
    after = index + width
    if after >= len(code) or code[after] != "{":
        return None
    return after


def _elided_elements(code: Sequence[str], opening: int) -> list[int]:
    """Indexes of the ``{`` tokens that elide their element type in a literal."""
    close = _go_balanced(list(code), opening)
    sites: list[int] = []
    if close is None:
        return sites
    depth = 0
    previous = "{"
    for position in range(opening + 1, close):
        token = code[position]
        if token in {"(", "[", "{"}:
            if token == "{" and depth == 0 and previous in {"{", ",", ":"}:
                sites.append(position)
            depth += 1
        elif token in {")", "]", "}"}:
            depth -= 1
        previous = token
    return sites


def _command_constructions(
    code: Sequence[str], command_types: set[tuple[str, ...]]
) -> list[int]:
    """Token indexes where a Cobra command value is constructed.

    Includes both the explicit forms (``&cobra.Command{…}``, ``new(cobra.Command)``)
    and the elements of a container literal whose element type is a command,
    where the element type is written once at the container and each element is
    an elided ``{…}``.
    """
    sites: list[int] = []
    # A return type immediately precedes a function body brace too. It is not
    # a command literal; real constructions in the body still get scanned.
    signatures = []
    for position, token in enumerate(code):
        if token == "func":
            signature = _go_signature(code, position)
            if signature is not None and signature["body"] is not None:
                signatures.append((position, int(signature["body"])))
    index = 0
    while index < len(code):
        width = next(
            (len(candidate) for candidate in command_types if tuple(code[index:index + len(candidate)]) == candidate),
            0,
        )
        if not width:
            index += 1
            continue
        after = code[index + width] if index + width < len(code) else ""
        before = code[index - 1] if index else ""
        if after == "{" and before in _GO_VALUE_PRECEDERS and not any(
            start < index < end == index + width for start, end in signatures
        ):
            sites.append(index)
        elif after == ")" and before == "(" and index >= 2 and code[index - 2] == "new":
            sites.append(index)
        else:
            container = _command_container_literal(code, index, width)
            if container is not None:
                sites.extend(_elided_elements(code, container))
        index += width
    return sites


def _legacy_scaffolding(
    relative: str, code: Sequence[str], imports: Mapping[str, str], breaks: set[int]
) -> tuple[set[int], set[str]]:
    """Recognise empty parents used only to mount permitted Cobra leaves.

    Kong has already selected the leaf. Reconstructing its old parent path for
    Cobra's usage/flag inheritance does not implement a migrated command.
    Approval depends on the literal's fields and attached children, never on
    the helper's name. Parents with handlers or new flags are still residue.
    """
    directory = relative.rsplit("/", 1)[0]
    groups = {
        "cmd/chart": {"chart"}, "cmd/docker": {"docker"},
        "cmd/gha": {"gha"}, "cmd/fstail": set(),
        "cmd/oci": {"oci"}, "cmd/system": {"system", "install"},
    }
    catalog = relative == "cmd/apps_catalog.go"
    if directory not in groups and not catalog:
        return set(), set()
    allowed_uses = {"arkade"} | ({"install"} if catalog else groups[directory])
    factories = set().union(*(
        names for path, names in _COBRA_COMPAT_SOURCES.items()
        if path.rsplit("/", 1)[0] == directory
    ))
    # Group constructors themselves are not leaves.
    factories -= {"MakeDocker", "MakeGHA"}
    if directory == "cmd/system":
        factories.update(_OPTIONAL_COBRA_CONSTRUCTORS)
    types, _ = _cobra_command_types(code, imports)
    sites = _command_constructions(code, types)
    if not sites:
        return set(), set()
    depths = _go_brace_depths(code)
    functions = []
    for index, token in enumerate(code):
        if token != "func" or depths[index] != 0:
            continue
        entry = _go_signature(code, index)
        if entry and entry["name"] and not entry["receiver"] and entry["close"] is not None:
            functions.append(entry)
    approved_sites: set[int] = set()
    approved_functions: set[str] = set()
    # A parent builder may be called by another parent builder in this file.
    for _ in range(len(functions) + 1):
        changed = False
        for entry in functions:
            opening, closing = int(entry["body"]), int(entry["close"])
            local_sites = [site for site in sites if opening < site < closing]
            if not local_sites:
                continue
            parents = {}
            valid = True
            for site in local_sites:
                width = next((len(t) for t in types if tuple(code[site:site + len(t)]) == t), 0)
                brace = site + width
                end = _go_balanced(code, brace) if code[brace:brace + 1] == ["{"] else None
                # Restrict the exemption to a named local composite literal.
                assignment = site - 1 if code[site - 1] == "&" else site
                variable = assignment - 3 if code[assignment - 2:assignment] == [":", "="] else assignment - 2
                if (end is None or code[assignment - 1] != "="
                        or not _GO_IDENTIFIER.fullmatch(code[variable])):
                    valid = False
                    break
                fields = _go_split_top_level(code[brace + 1:end], ",")
                metadata = {"Use", "Short", "Long", "Example", "Aliases", "SilenceUsage", "SilenceErrors", "Hidden"}
                if any(len(f) < 3 or f[1] != ":" or f[0] not in metadata for f in fields):
                    valid = False
                    break
                use = next((f[2:] for f in fields if f[0] == "Use"), [])
                if len(use) != 1 or _string_literal(use[0]) not in allowed_uses:
                    valid = False
                    break
                parents[code[variable]] = site
            if not valid:
                continue
            body = list(code[opening + 1:closing])
            safe = set(parents)
            assignments = _go_assignments(body, {point - opening - 1 for point in breaks if opening < point < closing})
            for name, expressions in assignments.items():
                if expressions and all(
                    (len(expr) == 3 and expr[0] in factories | approved_functions and expr[1:] == ["(", ")"])
                    or (catalog and len(expr) == 5 and expr[1:] == [".", "Installer", "(", ")"])
                    for expr in expressions
                ):
                    safe.add(name)
            if any(len(assignments.get(name, [])) != 1 for name in parents):
                continue
            attached = set()
            for index, token in enumerate(body):
                if token not in parents or body[index + 1:index + 2] != ["."]:
                    continue
                method = body[index + 2] if index + 2 < len(body) else ""
                if method in {"Run", "RunE", "PreRun", "PreRunE", "PersistentPreRun", "PersistentPreRunE", "PostRun", "PostRunE"}:
                    # The only permitted handler on an empty parent is usage.
                    start = index + 4
                    signature = _go_signature(body, start) if body[index + 3:index + 4] == ["="] and body[start:start + 1] == ["func"] else None
                    if not signature or signature["body"] is None:
                        valid = False
                        break
                    content = body[int(signature["body"]) + 1:int(signature["close"])]
                    if method != "RunE" or len(content) != 6 or content[0] != "return" or content[2:] != [".", "Usage", "(", ")"]:
                        valid = False
                        break
                elif method in {"Flags", "PersistentFlags"}:
                    # The app catalogue exception also preserves install's
                    # existing inherited kubeconfig/wait declarations.
                    tail = body[index + 3:index + 11]
                    if not catalog or tail[:3] != ["(", ")", "."] or tail[3] not in {"String", "Bool"} or _string_literal(tail[5]) not in {"kubeconfig", "wait"}:
                        valid = False
                        break
                elif method == "AddCommand":
                    end = _go_balanced(body, index + 3)
                    if end is None:
                        valid = False
                        break
                    args = _go_split_top_level(body[index + 4:end], ",")
                    if not args or any(not (
                        (len(arg) == 1 and arg[0] in safe)
                        or (len(arg) == 3 and arg[0] in factories | approved_functions and arg[1:] == ["(", ")"])
                        or (catalog and len(arg) == 5 and arg[1:] == [".", "Installer", "(", ")"])
                    ) for arg in args):
                        valid = False
                        break
                    attached.add(token)
                elif method not in {"SetOut", "SetErr", "SetArgs", "Execute", "Usage"}:
                    valid = False
                    break
            if not valid or attached != set(parents):
                continue
            approved_sites.update(local_sites)
            name = str(entry["name"])
            if _returns_command(entry["results"], types) and name not in approved_functions:
                approved_functions.add(name)
                changed = True
        if not changed:
            break
    return approved_sites, approved_functions


def _enclosing_function(functions: Sequence[Mapping[str, object]], index: int) -> Mapping[str, object] | None:
    """The innermost declared Cobra constructor whose body contains ``index``."""
    owner: Mapping[str, object] | None = None
    for entry in functions:
        body, close = entry["body"], entry["close"]
        if body is None or close is None:
            continue
        if body < index < close and (owner is None or body > owner["body"]):
            owner = entry
    return owner


def _approved_identity(entry: Mapping[str, object], approved: Iterable[str]) -> bool:
    """Whether ``entry`` *is* one pinned constructor, by declaration identity.

    A pinned name is only satisfied by a package-level, receiver-less function.
    A method that reuses the name is a different declaration and never inherits
    the approval, and variable-valued constructors are only accepted for the
    explicitly allowed names.
    """
    if entry["receiver"] or not entry["top_level"]:
        return False
    name = entry["name"]
    if name in approved:
        return True
    return name in _APPROVED_VAR_CONSTRUCTORS


def _inside_approved(
    functions: Sequence[Mapping[str, object]], index: int, approved: Iterable[str]
) -> bool:
    """Whether ``index`` sits inside the body of one approved constructor.

    Nested closures count as part of their enclosing constructor: they are only
    reachable through the constructor the allowlist already pinned.  A method
    body is never an approved body, even when the method reuses a pinned name.
    """
    names = set(approved)
    for entry in functions:
        body, close = entry["body"], entry["close"]
        if body is None or close is None:
            continue
        if body < index < close and _approved_identity(entry, names):
            return True
    return False


def _go_struct_bodies(code: Sequence[str]) -> list[tuple[int, int]]:
    """Spans of every ``struct { … }`` body, as ``(open, close)`` indexes."""
    bodies: list[tuple[int, int]] = []
    for index, token in enumerate(code):
        if token != "struct":
            continue
        cursor = index + 1
        if cursor < len(code) and code[cursor] == "{":
            end = _go_balanced(code, cursor)
            if end is not None:
                bodies.append((cursor, end))
    return bodies


def _in_scope_passthrough_issues(code: Sequence[str]) -> list[str]:
    """Argo CD is a migrated command, not an opaque legacy leaf.

    Inspect Kong command tags, independent of the file and target type names.
    Passing this command's arguments through the root leaves its flags outside
    the Kong grammar. Other app installers are explicitly allowed passthroughs.
    """
    issues: list[str] = []
    for opening, closing in _go_struct_bodies(code):
        start = opening + 1
        depth = 0
        for index in range(start, closing):
            token = code[index]
            if token in {"{", "(", "["}:
                depth += 1
            elif token in {"}", ")", "]"}:
                depth -= 1
            if depth or not token.startswith(("`", '"')):
                continue
            try:
                tag = token[1:-1] if token.startswith("`") else json.loads(token)
            except ValueError:
                continue
            if not re.search(r'\bcmd:"', tag):
                start = index + 1
                continue
            name = re.search(r'\bname:"([^"\\]*)"', tag)
            field = code[start] if start < index else ""
            command = name.group(1) if name else field.lower()
            if command == "argocd" and re.search(r'\bpassthrough:"', tag):
                issues.append("in-scope Kong passthrough command: argocd")
            start = index + 1
    return issues


def _leaf_references(code: Sequence[str]) -> tuple[list[str], list[str]]:
    """Enumerate every token-level ``konglegacy.Leaf`` type reference.

    Returns the field names that are declared in the approved form (one or more
    plain names, comma separated, then ``konglegacy.Leaf`` directly inside a
    ``struct`` body) plus one hit per reference in any other shape: a pointer, a
    slice/map value, an embedded field, a variable, a type alias, a parameter or
    a result.  Those forms are what would let an unpinned command path reach the
    legacy dispatcher, so they are decided violations rather than
    "unclassifiable".

    Go allows ``Sneaky, Fstail konglegacy.Leaf``, so the whole name list is
    recorded: taking only the token adjacent to the type would let an extra
    command path inherit the frozen name in front of it.
    """
    fields: list[str] = []
    hits: list[str] = []
    bodies = _go_struct_bodies(code)
    for index in range(len(code) - 2):
        if list(code[index:index + 3]) != ["konglegacy", ".", "Leaf"]:
            continue
        in_struct = any(start < index < close for start, close in bodies)
        names = _leaf_field_names(code, index) if in_struct else []
        if not names:
            hits.append("legacy Leaf is not declared as an approved struct field")
            continue
        fields.extend(names)
    return fields, hits


def _leaf_field_names(code: Sequence[str], index: int) -> list[str]:
    """The comma-separated field names declared immediately before ``index``."""
    cursor = index - 1
    if cursor < 0 or not _GO_IDENTIFIER.fullmatch(code[cursor]):
        return []
    names = [code[cursor]]
    while cursor >= 2 and code[cursor - 1] == "," and _GO_IDENTIFIER.fullmatch(code[cursor - 2]):
        names.append(code[cursor - 2])
        cursor -= 2
    names.reverse()
    return names


def _legacy_scope_issues(codes: Mapping[str, Sequence[str]]) -> list[tuple[str, str]]:
    """Cross-file hits for the legacy executor and constructor map.

    The bridge boundary is only trustworthy while the whole *repository* has a
    single ``konglegacy.SetExecutor`` registration and a single
    ``legacyConstructors`` declaration/initialisation/lookup, and while both live
    in the *same* file.  A second holder, or any reference to the constructor
    map in a file other than the one that registers the executor, re-opens the
    same escape and is a hit.

    Which file that is comes from the code (see ``_legacy_adapter_files``):
    ``instruction.md`` does not prescribe a layout, so pinning ``cmd/kong.go``
    by name would fail a migration that merely renames the adapter file.
    """
    hits: list[tuple[str, str]] = []
    # ``SetExecutor``/``legacyConstructors`` are what make the pinned dispatch
    # reachable, so the file that holds either of them *is* the adapter.  Two
    # holders means a second registration/reference, which is the escape this
    # check exists to catch.
    holders = _legacy_adapter_files(codes)
    if len(holders) > 1:
        for relative in sorted(holders):
            hits.append((relative, "legacy adapter is held by more than one file"))
    for relative, code in codes.items():
        for index, token in enumerate(code):
            if token == "SetExecutor" and _go_is_declaration(code, index):
                if not relative.startswith("internal/konglegacy/"):
                    hits.append(
                        (relative, "legacy executor is declared outside internal/konglegacy")
                    )
    return hits


def _legacy_dispatch_issues(code: Sequence[str]) -> tuple[list[str], list[str]]:
    """Hits/uncertainties for the frozen ``legacyConstructors`` boundary.

    The map is only trusted while it has exactly one declaration, one literal
    initialisation and one ``legacyConstructors[path]`` lookup.  A second
    assignment, an index write, an alias capture or any other escape can
    redirect an in-scope path back onto a Cobra leaf after the entries were
    checked, so each of those fails closed.
    """
    hits: list[str] = []
    uncertain: list[str] = []
    declarations = 0
    initialisations = 0
    lookups = 0
    escapes = 0
    for index, token in enumerate(code):
        if token != "legacyConstructors":
            continue
        previous = code[index - 1] if index else ""
        following = code[index + 1] if index + 1 < len(code) else ""
        if previous == "var":
            declarations += 1
            continue
        if following == "=":
            if index + 2 < len(code) and code[index + 2] == "map":
                initialisations += 1
            else:
                hits.append("legacy constructor map is reassigned from a non-literal value")
            continue
        if following == "[":
            end = _go_balanced(code, index + 1)
            if end is None:
                uncertain.append("legacy constructor map index is not statically resolvable")
                continue
            key = list(code[index + 2:end])
            after = code[end + 1] if end + 1 < len(code) else ""
            if after in {"=", ":="}:
                hits.append("legacy constructor map is mutated through an index write")
            elif key != ["path"]:
                hits.append("legacy constructor map is indexed by an unexpected key")
            else:
                lookups += 1
            continue
        escapes += 1
    if escapes:
        hits.append("legacy constructor map escapes its approved declaration, initialisation and lookup")
    if declarations != 1:
        uncertain.append("legacy constructor declaration is not unique")
    if initialisations != 1:
        hits.append("legacy constructor map is initialised more than once")
    if lookups != 1:
        uncertain.append("legacy constructor lookup is not unique")
    return hits, uncertain


def _legacy_executor_issues(code: Sequence[str]) -> tuple[list[str], list[str]]:
    """Hits/uncertainties for the ``konglegacy.SetExecutor`` registration."""
    calls = [
        index
        for index, token in enumerate(code)
        if token == "SetExecutor" and not _go_is_declaration(code, index)
    ]
    if len(calls) > 1:
        return ["legacy executor is registered more than once"], []
    if not calls:
        return [], ["legacy executor dispatch is not statically resolvable"]
    index = calls[0]
    if index < 2 or list(code[index - 2:index]) != ["konglegacy", "."] or code[index + 1] != "(":
        return [], ["legacy executor registration is not a direct konglegacy.SetExecutor call"]
    end = _go_balanced(code, index + 1)
    arguments = list(code[index + 2:end]) if end is not None else []
    if arguments != ["executeLegacy"]:
        return [], ["legacy executor is not the canonical executeLegacy dispatcher"]
    return [], []


def _raw_syscall_sink(code: list[str], index: int) -> tuple[bool, bool]:
    """Classify a raw syscall call as ``(exec_family, statically_decidable)``."""
    position = index + 4
    if position >= len(code):
        return False, False
    if re.fullmatch(r"SYS_[A-Z0-9_]+", code[position]):
        return code[position] in _EXECUTION_SYSCALLS, True
    if (
        position + 2 < len(code)
        and code[position + 1] == "."
        and re.fullmatch(r"SYS_[A-Z0-9_]+", code[position + 2])
    ):
        return code[position + 2] in _EXECUTION_SYSCALLS, True
    return False, False


def _go_call_arguments(code: Sequence[str], opening: int) -> list[list[str]] | None:
    """Split the argument list of the call opened at ``opening``."""
    closing = _go_balanced(list(code), opening)
    if closing is None:
        return None
    arguments: list[list[str]] = []
    current: list[str] = []
    depth = 0
    for position in range(opening + 1, closing):
        token = code[position]
        if token in {"(", "[", "{"}:
            depth += 1
        elif token in {")", "]", "}"}:
            depth -= 1
        if token == "," and depth == 0:
            arguments.append(current)
            current = []
        else:
            current.append(token)
    arguments.append(current)
    return arguments


def _go_assignments(code: Sequence[str], breaks: set[int]) -> dict[str, list[list[str]]]:
    """Map each assigned identifier to the right-hand side token slices."""
    assignments: dict[str, list[list[str]]] = {}
    for index in range(1, len(code) - 1):
        operator = code[index]
        if operator == ":":
            if code[index + 1] != "=":
                continue
            name = code[index - 1]
            start = index + 2
        elif operator == "=":
            # ``==``, ``!=``, ``<=``, ``>=`` and plain ``=`` tokenise one byte at
            # a time, so a neighbouring ``=``/``!``/``<``/``>`` is a comparison.
            if code[index - 1] in {"=", "!", "<", ">", ":"} or code[index + 1] == "=":
                continue
            name = code[index - 1]
            start = index + 1
        else:
            continue
        if not re.fullmatch(r"[A-Za-z_]\w*", name) or start >= len(code):
            continue
        # ``raw, err := decode(...)`` binds every name on the left, so the whole
        # comma-separated target list is recorded; otherwise the value stored in
        # ``raw`` would never be resolved from the expression that produced it.
        targets = [name]
        back = index - 2
        while operator == ":" and back >= 1 and code[back] == ",":
            candidate = code[back - 1]
            if re.fullmatch(r"[A-Za-z_]\w*", candidate):
                targets.insert(0, candidate)
                back -= 2
            else:
                break
        end = start
        depth = 0
        while end < len(code):
            token = code[end]
            if token in {"(", "[", "{"}:
                depth += 1
            elif token in {")", "]", "}"}:
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and token == ";":
                break
            elif depth == 0 and end in breaks and end > start and code[end - 1] not in {
                "=", ":", ",", ".", "+", "-", "*", "/", "&", "|", "!", "<", ">",
            }:
                break
            end += 1
        expression = list(code[start:end])
        for target in targets:
            assignments.setdefault(target, []).append(expression)
    return assignments


def _string_literal_body(token: str) -> str:
    """The contents of a Go string literal token, without its quotes."""
    if len(token) >= 2 and token[0] in {'"', "`"} and token[-1] == token[0]:
        return token[1:-1]
    return token


def _fold_string_literals(tokens: Sequence[str]) -> list[str]:
    """Fold adjacent string literals so a split constant reads as one value.

    ``"http://127.0.0." + "1:8080/version"`` is a single constant URL at
    runtime; matching each fragment separately would miss the loopback target.
    Only literal-to-literal ``+`` chains are folded, so a value that depends on
    a runtime operand keeps its fragments and is judged by the normal taint
    rules.
    """
    folded: list[str] = []
    index = 0
    total = len(tokens)
    while index < total:
        token = tokens[index]
        if not token.startswith(('"', "`")):
            index += 1
            continue
        parts = [_string_literal_body(token)]
        cursor = index
        while (
            cursor + 2 < total
            and tokens[cursor + 1] == "+"
            and tokens[cursor + 2].startswith(('"', "`"))
        ):
            parts.append(_string_literal_body(tokens[cursor + 2]))
            cursor += 2
        folded.append("".join(parts))
        index = cursor + 1
    return folded


_DECODERS = {"DecodeString": ("base64", "hex"), "MustDecodeString": ("base64",)}


def _decoded_string_constants(tokens: Sequence[str]) -> list[str]:
    """String constants produced by decoding a literal.

    ``base64.StdEncoding.DecodeString("aHR0cDovLzEyNy4wLjAuMTo4MDgw")`` produces a
    loopback URL at run time; the value is decidable because both the encoding and
    the payload are constants, so it is folded here and then judged by the same
    endpoint rule instead of being treated as an opaque call.
    """
    decoded: list[str] = []
    for index, token in enumerate(tokens):
        if token not in _DECODERS or index + 1 >= len(tokens) or tokens[index + 1] != "(":
            continue
        package = ""
        for back in range(index - 1, max(index - 6, -1), -1):
            if re.fullmatch(r"[A-Za-z_]\w*", tokens[back]):
                package = tokens[back]
                break
        schemes = _DECODERS[token]
        # The receiver decides the encoding: ``hex.DecodeString`` must not be fed
        # to the base64 decoder just because both are called ``DecodeString``.
        if package in schemes:
            scheme = package
        else:
            scheme = next(
                (candidate for candidate in schemes if candidate in tokens[max(index - 6, 0):index]),
                "",
            )
        if not scheme:
            continue
        # The payload may be written as adjacent literals; fold it the same way
        # the endpoint rules do, so a split constant is decoded rather than read
        # as an opaque call with fragments.
        payloads: list[str] = []
        closing = index + 2
        while closing < len(tokens) and tokens[closing] not in {")", ";"}:
            closing += 1
        expression = list(tokens[index + 2:closing])
        if "+" in expression:
            # ``_fold_string_literals`` already returns the literal *bodies*.
            payloads.extend(_fold_string_literals(expression))
        else:
            for forward in range(index + 2, min(index + 6, len(tokens))):
                literal = _string_literal(tokens[forward])
                if literal is not None:
                    payloads.append(literal)
                    break
        for literal in payloads:
            try:
                if scheme == "base64":
                    raw = literal.encode("utf-8", "ignore")
                    padded = raw + b"=" * (-len(raw) % 4)
                    value = base64.b64decode(padded, validate=False).decode("utf-8", "ignore")
                else:
                    value = bytes.fromhex(literal).decode("utf-8", "ignore")
            except (binascii.Error, ValueError, UnicodeDecodeError):
                continue
            decoded.append(value)
            break
    return [item for item in decoded if item]


def _has_loopback_endpoint(tokens: Sequence[str]) -> bool:
    """Does any string constant in ``tokens`` name a loopback endpoint?

    Constant concatenation and constant decoding are folded first so a split or
    encoded URL still matches: the question is which value the expression can
    produce, not which spelling it used.
    """
    candidates = [
        token for token in tokens if token.startswith(('"', "`"))
    ] + _fold_string_literals(tokens) + _decoded_string_constants(tokens)
    return any(_LOOPBACK_ENDPOINT.search(item) for item in candidates)


_STDLIB_ENVIRONMENT_PACKAGES = {"os", "syscall"}


_ASSIGNMENT_TAINT_CACHE: dict[int, tuple[object, frozenset[str], set[str], set[str]]] = {}


def _assignment_taint(
    assignments: Mapping[str, Sequence[Sequence[str]]],
    stdlib_aliases: set[str] | frozenset[str],
) -> tuple[set[str], set[str]]:
    """Names whose value comes from the environment / a loopback endpoint.

    Computed once per assignment table and cached: every sink question then costs
    a set intersection instead of a recursive walk, and the closure runs until it
    stops changing so a chain of any length is followed.
    """
    aliases = frozenset(stdlib_aliases)
    key = id(assignments)
    cached = _ASSIGNMENT_TAINT_CACHE.get(key)
    if cached is not None and cached[0] is assignments and cached[1] == aliases:
        return cached[2], cached[3]
    environment: set[str] = set()
    loopback: set[str] = set()
    # Reverse edges make the closure linear: ``depends_on[name]`` lists the names
    # whose expression mentions ``name``, so seeding a name only re-examines its
    # dependents instead of the whole table on every round.
    depends_on: dict[str, set[str]] = {}
    seeds_environment: set[str] = set()
    seeds_loopback: set[str] = set()
    for name, expressions in assignments.items():
        for expression in expressions:
            identifiers = {
                token for token in expression if re.fullmatch(r"[A-Za-z_]\w*", token)
            }
            for identifier in identifiers:
                depends_on.setdefault(identifier, set()).add(name)
            if any(token in _ENV_READERS for token in expression) and identifiers & aliases:
                seeds_environment.add(name)
            if _has_loopback_endpoint(expression):
                seeds_loopback.add(name)

    def close(seeds: set[str], sink: set[str]) -> None:
        queue = list(seeds)
        while queue:
            current = queue.pop()
            if current in sink:
                continue
            sink.add(current)
            queue.extend(depends_on.get(current, ()))

    close(seeds_environment, environment)
    close(seeds_loopback, loopback)
    _ASSIGNMENT_TAINT_CACHE[key] = (assignments, aliases, environment, loopback)
    return environment, loopback


def _reads_loopback(
    tokens: Sequence[str],
    assignments: Mapping[str, Sequence[Sequence[str]]],
) -> bool:
    """Does this expression evaluate to a loopback endpoint (through variables)?"""
    if _has_loopback_endpoint(tokens):
        return True
    identifiers = {token for token in tokens if re.fullmatch(r"[A-Za-z_]\w*", token)}
    _environment, loopback = _assignment_taint(assignments, frozenset())
    return bool(identifiers & loopback)


def _stdlib_aliases(imports: Mapping[str, str]) -> set[str]:
    """Import aliases of the packages that read the environment."""
    return _package_aliases(imports, _STDLIB_ENVIRONMENT_PACKAGES)


def _package_aliases(imports: Mapping[str, str], packages: set[str]) -> set[str]:
    """Import aliases bound to ``packages`` (``import o "os"`` -> ``o``)."""
    return {alias for alias, package in imports.items() if package in packages}


_RETURNING_CACHE: dict[int, tuple[object, frozenset[str], set[str], set[str]]] = {}


def _returning_functions(
    code: Sequence[str],
    imports: Mapping[str, str],
    assignments: Mapping[str, Sequence[Sequence[str]]],
) -> tuple[set[str], set[str]]:
    """Functions whose ``return`` value is environment / loopback derived.

    ``func b() string { return a() }`` forwards ``a``'s taint and chains can be
    arbitrarily long, so the set is closed transitively rather than following a
    fixed number of hops.  Computed once per file: every sink question then costs
    a set lookup.
    """
    key = id(code)
    aliases = frozenset(_stdlib_aliases(imports))
    cached = _RETURNING_CACHE.get(key)
    if cached is not None and cached[0] is code and cached[1] == aliases:
        return cached[2], cached[3]

    returns: dict[str, list[list[str]]] = {}
    for span in _go_function_spans(code):
        name = str(span["name"] or "")
        if not name:
            continue
        body = code[int(span["start"]):int(span["end"]) + 1]
        for index, token in enumerate(body):
            if token != "return":
                continue
            expression: list[str] = []
            depth = 0
            for item in body[index + 1:]:
                if item in {"(", "[", "{"}:
                    depth += 1
                elif item in {")", "]", "}"}:
                    if depth == 0:
                        break
                    depth -= 1
                elif item in {";"} and depth == 0:
                    break
                expression.append(item)
            returns.setdefault(name, []).append(expression)

    depends_on: dict[str, set[str]] = {}
    seeds_environment: set[str] = set()
    seeds_loopback: set[str] = set()
    for name, expressions in returns.items():
        for expression in expressions:
            identifiers = {
                token for token in expression if re.fullmatch(r"[A-Za-z_]\w*", token)
            }
            for identifier in identifiers:
                depends_on.setdefault(identifier, set()).add(name)
            if _reads_environment(expression, assignments, aliases):
                seeds_environment.add(name)
            if _reads_loopback(expression, assignments):
                seeds_loopback.add(name)

    def close(seeds: set[str]) -> set[str]:
        sink: set[str] = set()
        queue = list(seeds)
        while queue:
            current = queue.pop()
            if current in sink:
                continue
            sink.add(current)
            queue.extend(depends_on.get(current, ()))
        return sink

    environment = close(seeds_environment)
    loopback = close(seeds_loopback)
    _RETURNING_CACHE[key] = (code, aliases, environment, loopback)
    return environment, loopback


def _environment_returning_functions(
    code: Sequence[str],
    imports: Mapping[str, str],
    assignments: Mapping[str, Sequence[Sequence[str]]],
) -> set[str]:
    """Functions whose ``return`` expression carries an environment value."""
    return _returning_functions(code, imports, assignments)[0]


def _loopback_returning_functions(
    code: Sequence[str],
    imports: Mapping[str, str],
    assignments: Mapping[str, Sequence[Sequence[str]]],
) -> set[str]:
    """Functions whose ``return`` expression carries a loopback endpoint."""
    return _returning_functions(code, imports, assignments)[1]


def _bridge_derived_names(
    assignments: Mapping[str, Sequence[Sequence[str]]],
    stdlib_aliases: set[str] | frozenset[str] = frozenset(),
    environment_returning: set[str] | frozenset[str] = frozenset(),
    loopback_returning: set[str] | frozenset[str] = frozenset(),
) -> tuple[set[str], set[str]]:
    """Identifiers whose value comes from the environment or a loopback URL.

    The environment reader is recognised by the *package* it belongs to, so an
    aliased import (``import o "os"``) taints exactly like the plain spelling;
    matching the literal name ``os`` would make the taint depend on how the
    import is written.
    """
    readers = _ENV_READERS
    aliases = set(stdlib_aliases)
    environment: set[str] = set()
    loopback: set[str] = set()
    for _ in range(len(assignments) + 1):
        changed = False
        for name, expressions in assignments.items():
            for expression in expressions:
                identifiers = {token for token in expression if re.fullmatch(r"[A-Za-z_]\w*", token)}
                if name not in environment and (
                    any(token in readers for token in expression)
                    and bool(identifiers & aliases)
                    or identifiers & environment
                    or bool(identifiers & set(environment_returning))
                ):
                    environment.add(name)
                    changed = True
                if name not in loopback and (
                    _reads_loopback(expression, assignments)
                    or identifiers & loopback
                    or bool(identifiers & set(loopback_returning))
                ):
                    loopback.add(name)
                    changed = True
        if not changed:
            break
    return environment, loopback


def _go_parameter_names(tokens: Sequence[str]) -> list[str]:
    """Parameter names declared in a Go function parameter list.

    ``(endpoint string)``, ``(a, b string)`` and ``(c *Client, u string)`` all
    resolve; unnamed parameters (a bare type) are not reported as names.
    """
    groups: list[list[str]] = []
    current: list[str] = []
    depth = 0
    for token in tokens:
        if token in {"(", "[", "{"}:
            depth += 1
        elif token in {")", "]", "}"}:
            depth -= 1
        if token == "," and depth == 0:
            groups.append(current)
            current = []
        else:
            current.append(token)
    groups.append(current)
    names: list[str] = []
    builtin_types = {
        "string", "bool", "byte", "rune", "error", "any",
        "int", "int8", "int16", "int32", "int64",
        "uint", "uint8", "uint16", "uint32", "uint64", "uintptr",
        "float32", "float64", "complex64", "complex128",
        "map", "chan", "func", "interface", "struct",
    }
    for group in groups:
        if not group:
            continue
        if not re.fullmatch(r"[A-Za-z_]\w*", group[0]):
            continue
        if group[0] in builtin_types:
            continue
        names.append(group[0])
    return names


def _go_function_spans(code: Sequence[str]) -> list[dict[str, object]]:
    """Locate every function or method declaration and its parameter names."""
    spans: list[dict[str, object]] = []
    for index, token in enumerate(code):
        if token != "func":
            continue
        cursor = index + 1
        receiver = False
        if cursor < len(code) and code[cursor] == "(":
            end = _go_balanced(list(code), cursor)
            if end is None:
                continue
            receiver = True
            cursor = end + 1
        if cursor >= len(code) or not re.fullmatch(r"[A-Za-z_]\w*", code[cursor]):
            continue
        name = code[cursor]
        cursor += 1
        if cursor < len(code) and code[cursor] == "[":
            end = _go_balanced(list(code), cursor)
            if end is None:
                continue
            cursor = end + 1
        if cursor >= len(code) or code[cursor] != "(":
            continue
        end = _go_balanced(list(code), cursor)
        if end is None:
            continue
        params = _go_parameter_names(code[cursor + 1:end])
        cursor = end + 1
        while cursor < len(code) and code[cursor] not in {"{", ";"}:
            if code[cursor] in {"(", "["}:
                close = _go_balanced(list(code), cursor)
                if close is None:
                    break
                cursor = close + 1
                continue
            cursor += 1
        if cursor >= len(code) or code[cursor] != "{":
            continue
        close = _go_balanced(list(code), cursor)
        if close is None:
            continue
        spans.append(
            {
                "name": name,
                "params": params,
                "receiver": receiver,
                "start": cursor,
                "end": close,
            }
        )
    return spans


def _enclosing_span(spans: Sequence[Mapping[str, object]], index: int) -> Mapping[str, object] | None:
    """The innermost function span containing ``index``."""
    match: Mapping[str, object] | None = None
    for span in spans:
        if int(span["start"]) <= index <= int(span["end"]):
            if match is None or int(span["start"]) > int(match["start"]):
                match = span
    return match


def _value_is_injected(tokens: Sequence[str], derived: set[str]) -> bool:
    """Does a value expression come from the environment, loopback or a bridge?"""
    identifiers = {token for token in tokens if re.fullmatch(r"[A-Za-z_]\w*", token)}
    return (
        any(token in _ENV_READERS for token in tokens)
        or _has_loopback_endpoint(tokens)
        or bool(identifiers & derived)
        or any(_BRIDGE_ENDPOINT_NAME.search(token) for token in identifiers)
    )


def _injected_endpoint(
    target: Sequence[str],
    *,
    environment: set[str],
    loopback: set[str],
    derived: set[str],
    span: Mapping[str, object] | None = None,
) -> bool:
    """Is an endpoint expression delegated to the surrounding environment?"""
    if _has_fixed_remote_endpoint(target):
        # The host is pinned in the source; a variable path segment does not turn
        # a documented download URL into a runtime-supplied endpoint.
        return False
    identifiers = {item for item in target if re.fullmatch(r"[A-Za-z_]\w*", item)}
    injected = (
        any(item in _ENV_READERS for item in target)
        or _has_loopback_endpoint(target)
        or bool(identifiers & derived)
        or bool(identifiers & environment)
        or bool(identifiers & loopback)
    )
    if not injected:
        injected = any(_BRIDGE_ENDPOINT_NAME.search(item) for item in identifiers)
    if not injected:
        # A wrapper whose own name advertises a bridge and forwards one of its
        # parameters into the endpoint is delegating even when the value arrived
        # as an argument from the caller.
        if span is not None and _BRIDGE_ENDPOINT_NAME.search(str(span["name"])):
            parameters = {str(name) for name in span["params"]}
            injected = bool(identifiers & parameters)
    return injected


def _string_literal(token: str) -> str | None:
    """The unquoted value of a Go string literal token, if it is one."""
    if len(token) >= 2 and token.startswith('"') and token.endswith('"'):
        try:
            return bytes(token[1:-1], "utf-8").decode("unicode_escape")
        except UnicodeDecodeError:
            return token[1:-1]
    if len(token) >= 2 and token.startswith("`") and token.endswith("`"):
        return token[1:-1]
    return None


def _seeded_parameter_spans(
    code: Sequence[str],
    breaks: set[int],
    imports: Mapping[str, str],
    spans: Sequence[Mapping[str, object]],
    derived: set[str],
) -> list[tuple[int, set[str]]]:
    """Functions whose parameter receives a runtime-supplied value from a caller.

    ``postRuntime(endpoint)`` performs the request while ``endpoint`` was read
    from the environment in ``enableSourceHTTP``.  Judging the callee without
    its call sites would see a plain parameter; the delegation only exists in
    the caller/callee pair.  So for every call to a function declared in this
    file, an argument that is runtime-supplied taints the matching parameter,
    and the function body is re-examined with those parameters seeded.

    Returns ``(span start, seeded parameter names)`` pairs.
    """
    assignments = _go_assignments(code, breaks)
    by_name: dict[str, Mapping[str, object]] = {}
    for span in spans:
        name = str(span["name"])
        if name and re.fullmatch(r"[A-Za-z_]\w*", name) and name not in by_name:
            by_name[name] = span
    seeds: dict[int, set[str]] = {}
    for index in range(len(code) - 1):
        token = code[index]
        if token not in by_name or token in imports:
            continue
        if code[index + 1] != "(":
            continue
        arguments = _go_call_arguments(code, index + 1)
        if not arguments:
            continue
        span = by_name[token]
        params = [str(name) for name in span["params"]]
        for offset, argument in enumerate(arguments):
            if offset >= len(params):
                break
            injected = _injected_endpoint(
                argument,
                environment=derived,
                loopback=derived,
                derived=derived,
                span=None,
            ) or _reads_environment(
                argument, assignments, _stdlib_aliases(imports)
            )
            if injected:
                seeds.setdefault(int(span["start"]), set()).add(params[offset])
    return sorted(seeds.items())


_FIXED_REMOTE_ENDPOINT = re.compile(r"(?i)^(?:https?|ftps?|wss?)://[^/\s]+")


def _has_fixed_remote_endpoint(tokens: Sequence[str]) -> bool:
    """Does the expression pin a concrete remote host?

    A request to ``https://nodejs.org/download/%s/%s`` keeps its host fixed even
    when a path segment comes from a variable, so it is ordinary networking, not
    a delegation to whatever endpoint the environment supplies.  Only the *host*
    decides: a loopback literal is still a bridge, and a fully variable target
    has no fixed host at all.

    A concatenation is one value built from its pieces, so the pieces are not
    values on their own: ``http.Get("http://127.0.0." + "1:8080/version")`` must be
    read as ``http://127.0.0.1:8080/version`` (a loopback bridge) rather than
    judging the truncated fragment ``"http://127.0.0."`` as a pinned host.
    """
    has_concatenation = any(token == "+" for token in tokens)
    candidates = (
        _fold_string_literals(tokens)
        if has_concatenation
        else [token for token in tokens if token.startswith(('"', "`"))]
    )
    for item in candidates:
        text = item.strip('"`')
        if _LOOPBACK_ENDPOINT.search(text):
            continue
        if _FIXED_REMOTE_ENDPOINT.match(text):
            return True
    return False


def _reads_environment(
    tokens: Sequence[str],
    assignments: Mapping[str, Sequence[Sequence[str]]],
    stdlib_aliases: set[str] | frozenset[str],
) -> bool:
    """Does this expression read the environment (directly or through a variable)?

    The transitive part is pre-computed once per assignment table, so asking the
    question costs a set lookup instead of re-walking the graph for every
    expression (a recursive walk over a large table re-visits the same
    sub-expressions exponentially).
    """
    identifiers = {token for token in tokens if re.fullmatch(r"[A-Za-z_]\w*", token)}
    if any(token in _ENV_READERS for token in tokens) and identifiers & set(stdlib_aliases):
        return True
    environment, _loopback = _assignment_taint(assignments, stdlib_aliases)
    return bool(identifiers & environment)


def _runtime_supplied_arguments(
    arguments: Sequence[Sequence[str]],
    *,
    position: int,
    environment: set[str],
    loopback: set[str],
    span: Mapping[str, object] | None,
    assignments: Mapping[str, Sequence[Sequence[str]]],
) -> bool:
    """Does any argument of a process call come from the source runtime?

    The executable sits at ``position``; the remaining arguments are the ones a
    documented external tool would receive.  A runtime-supplied endpoint among
    them (``curl -sSf "$ARKADE_SOURCE_HTTP"``) hands the command's execution to
    whatever the environment names, which is the same delegation as a
    runtime-supplied executable.
    """
    for offset, argument in enumerate(arguments):
        # ``position`` is the executable; it is runtime-supplied in exactly the
        # same sense as the rest of the call, so it is checked here too.
        if position < 0:
            continue
        if _injected_endpoint(
            argument,
            environment=environment,
            loopback=loopback,
            derived=environment | loopback,
            span=span,
        ):
            return True
        for token in argument:
            if not re.fullmatch(r"[A-Za-z_]\w*", token):
                continue
            for expression in assignments.get(token, ()):
                if _injected_endpoint(
                    expression,
                    environment=environment,
                    loopback=loopback,
                    derived=environment | loopback,
                    span=span,
                ):
                    return True
    return False


def _expression_values(
    tokens: Sequence[str],
    assignments: Mapping[str, Sequence[Sequence[str]]],
    depth: int = 2,
) -> list[str]:
    """The string values an expression can evaluate to.

    Values are resolved by *meaning*, not by spelling: constant concatenation is
    folded (``"g" + "o"`` -> ``go``), a slice literal contributes its elements
    (``[]string{"run", "./testdata/legacyruntime"}``), and a variable is replaced
    by the right-hand sides it was assigned, so an executable name or an argument
    assembled in one statement is judged the same as one spelled inline.
    """
    folded = _fold_string_literals(tokens)
    if folded:
        return folded
    if depth <= 0:
        return []
    values: list[str] = []
    for token in tokens:
        if not re.fullmatch(r"[A-Za-z_]\w*", token):
            continue
        for expression in assignments.get(token, ()):  # type: ignore[union-attr]
            values.extend(_expression_values(expression, assignments, depth - 1))
    return values


def _go_composite_literal_fields(code: Sequence[str]) -> dict[str, list[str]]:
    """Field expressions of a struct literal body (``Name: expr`` pairs).

    ``code`` is the token slice between the braces.  A field whose value spans
    several tokens (``Args: append([]string{path}, args...)``) keeps all of them,
    because the question that follows is about the *value*, not the spelling.
    """
    fields: dict[str, list[str]] = {}
    for part in _go_split_top_level(list(code), ","):
        if ":" not in part:
            continue
        colon = part.index(":")
        name = part[colon - 1]
        if not re.fullmatch(r"[A-Za-z_]\w*", name):
            continue
        fields.setdefault(name, list(part[colon + 1:]))
    return fields


def _exec_cmd_literal_tokens(
    code: Sequence[str],
    imports: Mapping[str, str],
    environment: set[str],
    loopback: set[str],
    spans: Sequence[Mapping[str, object]],
    assignments: Mapping[str, Sequence[Sequence[str]]],
) -> list[str]:
    """Process sinks built as an ``exec.Cmd`` value instead of a constructor call.

    ``exec.Command("git", args...)`` and ``command := &exec.Cmd{Path: p, Args: a};
    command.Run()`` are the same delegation when ``p``/``a`` come from the
    environment: the observable fact is the *value* that reaches the process
    launcher, not which spelling produced it.  The executable is the ``Path``
    field and the arguments are the ``Args`` field, so the existing sink rules
    apply unchanged -- a literal assigns a fixed executable, and a runtime value
    is the source fallback the migration must not keep.
    """
    tokens: list[str] = []
    for index in range(len(code) - 3):
        alias = code[index]
        if imports.get(alias) != "os/exec":
            continue
        if code[index + 1] != "." or code[index + 2] != "Cmd" or code[index + 3] != "{":
            continue
        closing = _go_balanced(list(code), index + 3)
        if closing is None or closing <= index + 3:
            continue
        fields = _go_composite_literal_fields(code[index + 4:closing])
        executable = fields.get("Path")
        arguments = fields.get("Args")
        if executable is None and arguments is None:
            continue
        span = _enclosing_span(spans, index)
        runtime_arguments = _runtime_supplied_arguments(
            [executable or [], arguments or []],
            position=0,
            environment=environment,
            loopback=loopback,
            span=span,
            assignments=assignments,
        )
        if runtime_arguments or (
            executable is not None
            and _fixed_process_runs_source(executable, [arguments] if arguments else [], assignments)
        ):
            tokens.append("os/exec.Cmd")
    return tokens


def _fixed_process_runs_source(
    executable: Sequence[str],
    arguments: Sequence[Sequence[str]],
    assignments: Mapping[str, Sequence[Sequence[str]]] | None = None,
) -> bool:
    """Does a *fixed* process target hand the process source from this tree?

    ``exec.Command("git", "status")`` is the documented way to keep the CLI's own
    subcommand execution and stays legal.  The same sink becomes a delegation to
    the source runtime when the target is a toolchain/interpreter or when any
    argument names a path inside the candidate repository (``go run
    ./testdata/legacycobra``), because then the process executes code the
    candidate shipped instead of an external tool the migration depends on.

    Both sides are resolved to the values they can produce, so a name assembled
    from constants (``"g" + "o"``) or an argument list stored in a slice
    (``[]string{"run", "./testdata/legacyruntime"}``) is judged exactly like the
    inline literal spelling.
    """
    targets = _expression_values(executable, assignments or {})
    if not targets:
        return False
    for target in targets:
        name = target.replace("\\", "/").rsplit("/", 1)[-1]
        if name not in _FIXED_PROCESS_TOOLS:
            # A path to a toolchain or interpreter (``/usr/local/go/bin/go``,
            # ``sh``, ``go``) is not one of the CLI's documented external tools:
            # what runs is whatever its arguments name.
            return True
    for argument in arguments:
        for text in _expression_values(argument, assignments or {}):
            if _REPO_PATH_LITERAL.search(text):
                return True
    return False


def _http_bridge_tokens(
    code: Sequence[str],
    breaks: set[int],
    imports: Mapping[str, str],
    seeds: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    """Hits for ``net/http`` requests aimed at a runtime-injected endpoint.

    The import alone is legitimate: the migrated commands download remote chart
    and asset URLs over the same package.  A hit requires the request target to
    (a) come from the environment/loopback, or (b) be named like a bridge, which
    is the shape of a delegation back to the source runtime.  ``seeds`` carries
    identifiers tainted by an interprocedural parameter flow, and a wrapper
    whose name advertises a bridge is judged by the parameters it forwards, so
    the endpoint does not have to be spelled in the same function that reads the
    environment.
    """
    aliases = {alias for alias, package in imports.items() if package in _HTTP_CLIENT_PACKAGES}
    if not aliases:
        return []
    assignments = _go_assignments(code, breaks)
    environment, loopback = _bridge_derived_names(
        assignments,
        _stdlib_aliases(imports),
        _environment_returning_functions(code, imports, assignments),
        _loopback_returning_functions(code, imports, assignments),
    )
    derived = environment | loopback | set(seeds)
    if seeds:
        # A parameter tainted by the caller is a value the wrapper forwards, so
        # identifiers derived from it inside this body are injected too.
        derived |= _tainted_identifiers(list(code), set(seeds))
    tokens: list[str] = []
    spans = _go_function_spans(code)
    for index, token in enumerate(code):
        if token not in aliases or index + 1 >= len(code) or code[index + 1] != ".":
            continue
        cursor = index + 1
        while (
            cursor + 2 < len(code)
            and code[cursor] == "."
            and re.fullmatch(r"[A-Za-z_]\w*", code[cursor + 1])
            and code[cursor + 2] == "."
        ):
            cursor += 2
        if cursor + 2 >= len(code) or code[cursor] != ".":
            continue
        method = code[cursor + 1]
        if method not in _HTTP_URL_ARGUMENT or code[cursor + 2] != "(":
            continue
        arguments = _go_call_arguments(code, cursor + 2)
        if arguments is None:
            continue
        position = _HTTP_URL_ARGUMENT[method]
        if position >= len(arguments):
            continue
        target = arguments[position]
        if _injected_endpoint(
            target,
            environment=environment,
            loopback=loopback,
            derived=derived,
            span=_enclosing_span(spans, cursor),
        ):
            tokens.append(
                f"HTTP bridge to a runtime-supplied endpoint via {code[index]}.{method}"
            )
    return tokens


def _network_bridge_tokens(
    code: Sequence[str],
    breaks: set[int],
    imports: Mapping[str, str],
    seeds: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    """Hits for raw socket/RPC dials aimed at a runtime-injected endpoint.

    ``net`` is imported legitimately by the migrated commands, and a dial to a
    fixed remote host is ordinary networking.  A dial whose address is read from
    the environment, points at loopback, or is named like a bridge hands the
    command's execution to whatever the environment supplies, which is exactly
    the "old runtime still running out of band" fallback the migration must not
    keep.  Both the package-level helpers and the ``net.Dialer`` method form are
    inspected.
    """
    aliases = {alias for alias, package in imports.items() if package in _NETWORK_PACKAGES}
    net_alias = {alias for alias, package in imports.items() if package == "net"}
    if not aliases:
        return []
    assignments = _go_assignments(code, breaks)
    environment, loopback = _bridge_derived_names(
        assignments,
        _stdlib_aliases(imports),
        _environment_returning_functions(code, imports, assignments),
        _loopback_returning_functions(code, imports, assignments),
    )
    derived = environment | loopback | set(seeds)
    if seeds:
        derived |= _tainted_identifiers(list(code), set(seeds))
    dialers = _dialer_value_names(code, breaks, aliases)
    tokens: list[str] = []
    spans = _go_function_spans(code)
    for index, token in enumerate(code):
        if index + 3 >= len(code) or code[index + 1] != ".":
            continue
        method = code[index + 2]
        if code[index + 3] != "(":
            continue
        position: int | None = None
        label = ""
        if token in aliases and imports.get(token) == "net" and method in _NETWORK_DIAL_ARGUMENT:
            position = _NETWORK_DIAL_ARGUMENT[method]
            label = f"{token}.{method}"
        elif token in aliases and imports.get(token) == "net/rpc" and method in _RPC_DIAL_ARGUMENT:
            position = _RPC_DIAL_ARGUMENT[method]
            label = f"{token}.{method}"
        elif token in dialers and method in _DIALER_METHOD_ARGUMENT:
            position = _DIALER_METHOD_ARGUMENT[method]
            label = f"{token}.{method}"
        elif token == ")" and method in _DIALER_METHOD_ARGUMENT and _composite_dialer(code, index, net_alias):
            position = _DIALER_METHOD_ARGUMENT[method]
            label = f"net.Dialer{{...}}.{method}"
        if position is None:
            continue
        arguments = _go_call_arguments(code, index + 3)
        if arguments is None or position >= len(arguments):
            continue
        if _injected_endpoint(
            arguments[position],
            environment=environment,
            loopback=loopback,
            derived=derived,
            span=_enclosing_span(spans, index),
        ):
            tokens.append(f"raw socket/RPC bridge to a runtime-supplied endpoint via {label}")
    return tokens


def _dialer_value_names(
    code: Sequence[str], breaks: set[int], net_aliases: set[str]
) -> set[str]:
    """Identifiers holding a ``net.Dialer`` (or ``*net.Dialer``) value."""
    names: set[str] = set()
    for name, expressions in _go_assignments(code, breaks).items():
        for expression in expressions:
            if "Dialer" not in expression:
                continue
            if any(item in net_aliases for item in expression) or "Dialer" in expression:
                names.add(name)
    for index in range(len(code) - 4):
        if code[index] not in {"var"}:
            continue
        name = code[index + 1]
        window = code[index + 2:index + 6]
        if re.fullmatch(r"[A-Za-z_]\w*", name) and "Dialer" in window and any(item in net_aliases for item in window):
            names.add(name)
    return names


def _composite_dialer(code: Sequence[str], closing: int, net_aliases: set[str]) -> bool:
    """Is the parenthesised expression ending at ``closing`` a ``net.Dialer``?"""
    depth = 0
    cursor = closing
    while cursor >= 0:
        if code[cursor] == ")":
            depth += 1
        elif code[cursor] == "(":
            depth -= 1
            if depth == 0:
                break
        cursor -= 1
    if cursor < 0:
        return False
    window = code[cursor:closing + 1]
    return "Dialer" in window and any(item in net_aliases for item in window)


def _vendor_sources(root: Path, uncertain: list[str]) -> list[tuple[str, Path]]:
    """Enumerate the vendored Go sources the runtime scan must inspect.

    Dependencies shipped under ``vendor/`` are compiled into the candidate, so a
    bridge hidden in a vendored helper executes exactly like one in ``cmd/``.  The
    tree is enumerated separately from the dispatch surface because Cobra and
    dispatch rules do not apply inside dependencies; only sink analysis does.
    """
    base = root / "vendor"
    if not base.exists():
        return []
    if base.is_symlink() or not base.is_dir():
        uncertain.append("vendor tree is not a directory")
        return []
    found: dict[str, Path] = {}
    try:
        for path in sorted(base.rglob("*.go")):
            if path.name.endswith("_test.go"):
                continue
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                uncertain.append("vendored source escapes the candidate root")
                continue
            if path.is_symlink():
                uncertain.append(f"symlinked vendored source needs review: {relative}")
                continue
            if not path.is_file():
                uncertain.append(f"unreadable vendored source: {relative}")
                continue
            found[relative] = path
    except OSError:
        uncertain.append("vendor tree is not enumerable")
    return [(relative, found[relative]) for relative in sorted(found)]


def _vendor_package(relative: str) -> str:
    """Import path of the package a ``vendor/...`` file belongs to."""
    parent = relative.rsplit("/", 1)[0] if "/" in relative else relative
    return parent[len("vendor/"):] if parent.startswith("vendor/") else parent


def _span_assignments(tokens: Sequence[str], breaks: set[int]) -> dict[str, list[list[str]]]:
    """Assignments inside one function body (indices are body-relative)."""
    return _go_assignments(tokens, breaks)


def _tainted_identifiers(
    tokens: Sequence[str], seeds: set[str]
) -> set[str]:
    """Close ``seeds`` over the assignments inside one function body."""
    assignments = _span_assignments(tokens, set())
    tainted = set(seeds)
    for _ in range(len(assignments) + 1):
        changed = False
        for name, expressions in assignments.items():
            if name in tainted:
                continue
            for expression in expressions:
                identifiers = {
                    token for token in expression if re.fullmatch(r"[A-Za-z_]\w*", token)
                }
                if (
                    any(token in _ENV_READERS for token in expression)
                    or identifiers & tainted
                ):
                    tainted.add(name)
                    changed = True
                    break
        if not changed:
            break
    return tainted


def _vendor_bridge_scan(
    root: Path,
    production: Mapping[str, Sequence[str]],
    uncertain: list[str],
    coverage: dict[str, object] | None = None,
) -> list[dict[str, str]]:
    """Source-to-sink analysis of the vendored runtime surface.

    A vendored helper is only a hit when a value that came from the environment,
    a loopback endpoint or a bridge-named wrapper reaches an HTTP request target
    or a process sink.  Vendor trees contain a great deal of legitimate
    ``net/http`` and ``os/exec`` code, so blanket sink matching is deliberately
    not used: the value has to be injected, either by name (the wrapper
    advertises a bridge and forwards its parameter) or through a call the
    candidate makes with a tainted argument.
    """
    files = _vendor_sources(root, uncertain)
    if coverage is not None:
        coverage["vendor_files_scanned"] = len(files)
    parsed: dict[str, dict[str, object]] = {}
    for relative, path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            uncertain.append(f"unreadable vendored source: {relative}")
            continue
        code, breaks = _go_source_tokens(text)
        if not code or code[0] != "package":
            uncertain.append(f"vendored source has no resolvable package clause: {relative}")
            continue
        imports, issues = _go_imports(code)
        for issue in issues:
            uncertain.append(f"{issue}: {relative}")
        if not (
            _HTTP_CLIENT_PACKAGES & set(imports.values())
            or _EXECUTION_IMPORTS & set(imports.values())
            or _NETWORK_PACKAGES & set(imports.values())
            or set(imports.values()) & set(_CONDITIONAL_PROCESS_SINKS)
            or set(imports.values()) & set(_PROCESS_SINKS)
        ):
            continue
        parsed[relative] = {
            "code": code,
            "breaks": breaks,
            "imports": imports,
            "package": _vendor_package(relative),
            "spans": _go_function_spans(code),
        }
    index: dict[tuple[str, str], list[dict[str, object]]] = {}
    for entry in parsed.values():
        for span in entry["spans"]:
            index.setdefault((str(entry["package"]), str(span["name"])), []).append(span)

    def taint_parameter(key: tuple[str, str], position: int) -> bool:
        params = [str(name) for name in index[key][0]["params"]]
        if position >= len(params):
            return False
        values = taint.setdefault(key, set())
        if params[position] in values:
            return False
        values.add(params[position])
        return True

    taint: dict[tuple[str, str], set[str]] = {}
    # Seed the taint set from the candidate's own calls into vendored helpers:
    # an argument that came from the environment (or is itself bridge named)
    # taints the matching parameter of the callee.
    for relative, code in production.items():
        vendored_imports = _go_imports(code)[0]
        vendored_assignments = _go_assignments(code, set())
        environment, loopback = _bridge_derived_names(
            vendored_assignments,
            _stdlib_aliases(vendored_imports),
            _environment_returning_functions(code, vendored_imports, vendored_assignments),
            _loopback_returning_functions(code, vendored_imports, vendored_assignments),
        )
        derived = environment | loopback
        # ``derived`` is empty when the environment value is passed inline
        # (``tools.Kubectl(os.Getenv("BIN"))``), so the argument itself is
        # judged by ``_value_is_injected`` rather than only by prior assignment.
        imports, _ = _go_imports(code)
        for position in range(len(code) - 3):
            if (
                code[position + 1] != "."
                or code[position + 3] != "("
                or not re.fullmatch(r"[A-Za-z_]\w*", code[position])
                or not re.fullmatch(r"[A-Za-z_]\w*", code[position + 2])
            ):
                continue
            package = imports.get(code[position])
            if package is None:
                continue
            key = (package, code[position + 2])
            if key not in index:
                continue
            arguments = _go_call_arguments(code, position + 3)
            if arguments is None:
                continue
            for offset, argument in enumerate(arguments):
                if _value_is_injected(argument, derived):
                    taint_parameter(key, offset)
    # Propagate inside the vendored packages, so a wrapper that forwards its
    # parameter to a second helper is caught as well.
    for _ in range(4):
        changed = False
        for entry in parsed.values():
            code = entry["code"]
            imports = entry["imports"]
            package = str(entry["package"])
            for span in entry["spans"]:
                key = (package, str(span["name"]))
                seeds = {str(name) for name in span["params"]} & taint.get(key, set())
                if not seeds:
                    continue
                body = code[int(span["start"]):int(span["end"]) + 1]
                tainted = _tainted_identifiers(body, seeds)
                for position in range(len(body) - 1):
                    token = body[position]
                    if not re.fullmatch(r"[A-Za-z_]\w*", token) or token in _GO_KEYWORDS:
                        continue
                    if body[position + 1] == "." and position + 3 < len(body) and body[position + 3] == "(":
                        # Qualified call into another vendored package.
                        function = body[position + 2]
                        if not re.fullmatch(r"[A-Za-z_]\w*", function):
                            continue
                        imported = imports.get(token)
                        if imported is None:
                            continue
                        target = (imported, function)
                        open_paren = position + 3
                    elif body[position + 1] == "(":
                        # Same-package helper: ``Forward(target)``.
                        function = token
                        target = (package, function)
                        open_paren = position + 1
                    else:
                        continue
                    if target not in index or target[1] in _GO_KEYWORDS:
                        continue
                    arguments = _go_call_arguments(body, open_paren)
                    if arguments is None:
                        continue
                    for offset, argument in enumerate(arguments):
                        identifiers = {
                            item for item in argument if re.fullmatch(r"[A-Za-z_]\w*", item)
                        }
                        if identifiers & tainted and taint_parameter(target, offset):
                            changed = True
        if not changed:
            break

    hits: list[dict[str, str]] = []
    http_sinks = 0
    network_sinks = 0
    process_sinks = 0
    for relative, entry in parsed.items():
        code = entry["code"]
        imports = entry["imports"]
        package = str(entry["package"])
        for span in entry["spans"]:
            key = (package, str(span["name"]))
            seeds = {str(name) for name in span["params"]} & taint.get(key, set())
            if not seeds:
                continue
            body = code[int(span["start"]):int(span["end"]) + 1]
            for token in _http_bridge_tokens(body, set(), imports, seeds):
                http_sinks += 1
                hits.append({
                    "path": relative,
                    "token": f"{token} inside vendored {span['name']}",
                    "kind": "go-bridge",
                    "rule_id": "STRUCTURED-GO-BRIDGE",
                })
            for token in _network_bridge_tokens(body, set(), imports, seeds):
                network_sinks += 1
                hits.append({
                    "path": relative,
                    "token": f"{token} inside vendored {span['name']}",
                    "kind": "go-bridge",
                    "rule_id": "STRUCTURED-GO-BRIDGE",
                })
            for position in range(len(body) - 3):
                if body[position + 1] != "." or body[position + 3] != "(":
                    continue
                method = body[position + 2]
                imported = imports.get(body[position])
                if imported is None:
                    continue
                conditional = _CONDITIONAL_PROCESS_SINKS.get(imported)
                if conditional is not None and method in conditional:
                    # A fixed command is legitimate even inside a dependency;
                    # only an injected executable name is a bridge.
                    arguments = _go_call_arguments(body, position + 3)
                    if not arguments:
                        continue
                    argument = conditional[method]
                    if argument >= len(arguments):
                        continue
                    identifiers = {
                        item for item in arguments[argument] if re.fullmatch(r"[A-Za-z_]\w*", item)
                    }
                    if identifiers & seeds:
                        process_sinks += 1
                        hits.append({
                            "path": relative,
                            "token": f"subprocess bridge to a runtime-supplied command inside vendored {span['name']}",
                            "kind": "go-bridge",
                            "rule_id": "STRUCTURED-GO-BRIDGE",
                        })
                    continue
                if method not in _PROCESS_SINKS.get(imported, ()):
                    continue
                arguments = _go_call_arguments(body, position + 3)
                if not arguments:
                    continue
                identifiers = {
                    item for item in arguments[0] if re.fullmatch(r"[A-Za-z_]\w*", item)
                }
                if identifiers & seeds:
                    process_sinks += 1
                    hits.append({
                        "path": relative,
                        "token": f"subprocess bridge to a runtime-supplied command inside vendored {span['name']}",
                        "kind": "go-bridge",
                        "rule_id": "STRUCTURED-GO-BRIDGE",
                    })
    if coverage is not None:
        coverage["vendor_http_sinks"] = http_sinks
        coverage["vendor_network_sinks"] = network_sinks
        coverage["vendor_process_sinks"] = process_sinks
    return hits


def _reflect_callable_extraction(code: Sequence[str]) -> bool:
    """Can this file obtain a callable through reflection?

    ``.Method(name)``/``.Method(i)`` selects a method value and ``.(func...)``
    asserts a function type out of an interface.  Either one lets a
    ``reflect.Value`` be invoked without a ``.Call``/``.MethodByName`` call site
    (those are already reported as a hit), so they are the shapes that make a
    constructed ``reflect.Value`` worth reviewing.
    """
    for index in range(len(code) - 2):
        if code[index] == "." and code[index + 1] == "Method" and code[index + 2] == "(":
            return True
        if code[index] == "." and code[index + 1] == "(" and code[index + 2] == "func":
            return True
    return False


def _legacy_cobra_factory_call(
    code: Sequence[str],
    index: int,
    imports: Mapping[str, str],
    spans: Sequence[Mapping[str, object]],
) -> bool:
    """Is this reflection limited to constructing one permitted Cobra leaf?

    An out-of-scope command adapter may receive a zero-argument constructor as
    ``any`` and invoke it through ``reflect.Value``.  That is equivalent to a
    typed ``func() *cobra.Command`` call, not dynamic loading or command
    dispatch.  Require the narrow zero-argument shape and a Cobra-command type
    assertion in the same receiver method; arbitrary reflected calls remain
    deceptive findings.
    """
    if code[index + 1:index + 3] != ["Call", "("]:
        return False
    arguments = _go_call_arguments(code, index + 2)
    if arguments != [["nil"]]:
        return False
    owner = _enclosing_span(spans, index)
    if owner is None or owner.get("receiver") is not True:
        return False
    body = code[int(owner["start"]):int(owner["end"]) + 1]
    cobra_aliases = {
        alias for alias, package in imports.items() if package == _COBRA_PACKAGE
    }
    return any(
        body[offset] in cobra_aliases
        and body[offset + 1:offset + 3] == [".", "Command"]
        for offset in range(len(body) - 2)
    )


def _catalog_metadata_factory_call(
    relative: str,
    code: Sequence[str],
    index: int,
    spans: Sequence[Mapping[str, object]],
) -> bool:
    """Is this a zero-argument constructor read by AppDescription only?"""
    if relative != "cmd/apps_catalog.go" or code[index + 1:index + 3] != ["Call", "("]:
        return False
    if _go_call_arguments(code, index + 2) != [["nil"]]:
        return False
    owner = _enclosing_span(spans, index)
    return owner is not None and owner.get("name") == "AppDescription"


def _go_bridge_severity(token: str) -> str:
    """Classify a bridge hit as ordinary residue or deceptive delegation."""
    return "ordinary" if str(token).startswith(_ORDINARY_BRIDGE_TOKENS) else "deceptive"


def scan_go_bridges(
    root: Path, coverage: dict[str, object] | None = None
) -> tuple[list[dict[str, str]], list[str]]:
    """Inspect candidate production sinks and the frozen legacy dispatch boundary.

    Every production Go file is scanned; the only accepted Cobra usage is the
    pinned out-of-scope allowlist.  Vendored dependencies are compiled into the
    candidate, so their sinks are analysed through the same source-to-sink
    parameter flow.  Unknown or dynamically rewritten dispatch is *not* a clean
    scan: callers must treat the second return value as an inconclusive
    required gate.  ``coverage`` is filled with the positive scan report that a
    PASS has to carry: which trees, sink classes and call sites were examined.
    """
    hits: list[dict[str, str]] = []
    uncertain: list[str] = []
    sources = _go_sources(root, uncertain)
    if coverage is not None:
        coverage["production_files_scanned"] = len(sources)
        coverage["sink_classes"] = [
            "ffi", "subprocess", "rpc-http", "rpc-socket", "dynamic-load", "legacy-dispatch",
        ]
    leaves: list[tuple[str, str]] = []
    declared_by_package: dict[str, set[str]] = {}
    codes: dict[str, list[str]] = {}
    seen: set[str] = set()
    reflect_ctor_files: list[str] = []
    for relative, path in sources:
        seen.add(relative)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            uncertain.append(f"unreadable production file: {relative}")
            continue
        def hit(token: str) -> None:
            hits.append({"path": relative, "token": token, "kind": "go-bridge", "rule_id": "STRUCTURED-GO-BRIDGE"})

        code, breaks = _go_source_tokens(text)
        codes[relative] = code
        for token in _in_scope_passthrough_issues(code):
            hit(token)
        leaf_fields, leaf_hits = _leaf_references(code)
        for field in leaf_fields:
            leaves.append((relative, field))
        for token in leaf_hits:
            hit(token)
        if re.search(r"(?m)^// Code generated .* DO NOT EDIT\.$", text):
            uncertain.append(f"generated production source needs review: {relative}")
            continue
        if re.search(r"(?m)^[ \t]*//(?:go:build|[ \t]*\+build)\b", text):
            uncertain.append(f"build-constrained production source needs review: {relative}")
            continue
        if re.search(r"(?m)^\s*//go:linkname\b", text):
            hit("go:linkname")
        if not code or code[0] != "package":
            uncertain.append(f"production source has no resolvable package clause: {relative}")
            continue
        imports, import_issues = _go_imports(code)
        cobra_here = "github.com/spf13/cobra" in imports.values()
        scaffold_sites, scaffold_functions = _legacy_scaffolding(relative, code, imports, breaks)
        optional_cobra = relative in _OPTIONAL_COBRA_SOURCES
        for issue in import_issues:
            uncertain.append(f"{issue}: {relative}")
        for token in _http_bridge_tokens(code, breaks, imports):
            hit(token)
        for token in _network_bridge_tokens(code, breaks, imports):
            hit(token)
        for alias, package in imports.items():
            if package in _EXECUTION_IMPORTS or package.startswith(_EXECUTION_IMPORT_PREFIXES):
                hit(f"import {package}")
            if package == "unsafe":
                uncertain.append(f"unsafe pointer usage needs review: {relative}")
            if package == "github.com/spf13/cobra" and not optional_cobra:
                allowed_here = relative in _COBRA_COMPAT_SOURCES or (
                    relative.rsplit("/", 1)[0] if "/" in relative else ""
                ) in _COBRA_COMPAT_BY_PACKAGE
                # A pass-through adapter receives the permitted out-of-scope
                # commands and mounts them below the Kong root; `instruction.md`
                # permits the exceptions to stay on Cobra and does not prescribe a
                # file layout for mounting them.  A file that *defines* a command
                # surface -- builds a command literal, or stores a Cobra command in
                # a type the in-scope CLI carries around -- is in-scope usage.
                if (not allowed_here and not _is_cobra_passthrough(code, text, imports)
                        and (not scaffold_sites or set(_command_constructions(
                            code, _cobra_command_types(code, imports)[0]
                        )) != scaffold_sites)):
                    hit("in-scope Cobra import")
            if package.endswith("/internal/konglegacy"):
                # The pinned adapter is mounted from whichever file the
                # candidate chooses; what matters is the *role* the package is
                # used in, not the file's name.
                if not (leaf_fields or _uses_legacy_package(code)):
                    hit("unexpected in-scope legacy import")
                if alias != "konglegacy":
                    uncertain.append(f"aliased legacy import needs review: {relative}")
        aliases: set[str] = set()
        conditional_aliases: dict[str, tuple[str, str, int]] = {}
        assignments = _go_assignments(code, breaks)
        environment, loopback = _bridge_derived_names(
            assignments,
            _stdlib_aliases(imports),
            _environment_returning_functions(code, imports, assignments),
            _loopback_returning_functions(code, imports, assignments),
        )
        spans = _go_function_spans(code)
        # A process sink can also be built as a value: ``&exec.Cmd{Path: p, Args: a}``
        # followed by ``Run()`` is the same launch as ``exec.Command(p, a...)``.
        for token in _exec_cmd_literal_tokens(
            code, imports, environment, loopback, spans, assignments
        ):
            hit(token)
        # An endpoint can be tainted by the caller rather than read locally:
        # ``enableSourceHTTP`` evaluates ``o.Getenv(...)`` and forwards it to
        # ``postRuntime(endpoint)``, which performs the request.  The parameter
        # inherits the caller's taint, so each function is scanned again with
        # the parameters its call sites feed from the environment.  This reuses
        # the same sink analysis; only the seed set is new.
        for start, seeds in _seeded_parameter_spans(
            code, breaks, imports, spans, environment | loopback
        ):
            span = next(
                (item for item in spans if int(item["start"]) == start), None
            )
            if span is None:
                continue
            body = code[start:int(span["end"]) + 1]
            for token in _http_bridge_tokens(body, set(), imports, seeds):
                hit(f"{token} inside {span['name']}")
            for token in _network_bridge_tokens(body, set(), imports, seeds):
                hit(f"{token} inside {span['name']}")
        for index in range(len(code) - 3):
            package = imports.get(code[index])
            method = code[index + 2]
            if code[index + 1] != ".":
                continue
            conditional = _CONDITIONAL_PROCESS_SINKS.get(package or "")
            if conditional is not None and method in conditional:
                # A process sink is only a bridge when its executable comes from
                # the environment/bridge surface; ``exec.Command("git", ...)`` is
                # the documented way to keep this CLI's own subcommands.
                if code[index + 3] != "(":
                    if index >= 2 and code[index - 1] == "=":
                        var = code[index - 3] if index >= 3 and code[index - 2] == ":" else code[index - 2]
                        if re.fullmatch(r"[A-Za-z_]\w*", var):
                            conditional_aliases[var] = (str(package), method, conditional[method])
                    continue
                arguments = _go_call_arguments(code, index + 3)
                position = conditional[method]
                if arguments is None:
                    uncertain.append(f"unresolvable subprocess sink needs review: {relative}")
                    continue
                if position >= len(arguments):
                    continue
                span = _enclosing_span(spans, index)
                # A delegation can hide on either side of the call: the
                # executable may be runtime-supplied, or a documented external
                # tool may be handed a runtime endpoint (``curl <url-from-env>``).
                # Both are the same question -- "does any part of this call come
                # from the source runtime?" -- so every argument is examined.
                runtime_arguments = _runtime_supplied_arguments(
                    arguments,
                    position=position,
                    environment=environment,
                    loopback=loopback,
                    span=span,
                    assignments=assignments,
                )
                if runtime_arguments or _fixed_process_runs_source(
                    arguments[position],
                    [argument for offset, argument in enumerate(arguments) if offset != position],
                    assignments,
                ):
                    hit(f"{package}.{method}")
                continue
            if method not in _PROCESS_SINKS.get(package, ()):
                continue
            if method in _RAW_SYSCALL_METHODS:
                if code[index + 3] != "(":
                    uncertain.append(f"forwarded raw syscall needs review: {relative}")
                else:
                    is_exec, decidable = _raw_syscall_sink(code, index)
                    if is_exec:
                        hit(f"{package}.{method}")
                    elif not decidable:
                        uncertain.append(f"raw syscall dispatch needs review: {relative}")
            elif code[index + 3] == "(":
                hit(f"{package}.{method}")
            else:
                # A sink used as a value (alias, argument, field) is still a
                # bridge the migration must not introduce.
                hit(f"forwarded {package}.{method}")
            if index >= 2 and code[index - 1] == "=":
                var = code[index - 3] if index >= 3 and code[index - 2] == ":" else code[index - 2]
                if re.fullmatch(r"[A-Za-z_]\w*", var):
                    aliases.add(var)
        if any(
            alias == "."
            and (
                package in _PROCESS_SINKS
                or package in _EXECUTION_IMPORTS
                or package.startswith(_EXECUTION_IMPORT_PREFIXES)
            )
            for alias, package in imports.items()
        ):
            uncertain.append(f"dot-imported execution sink needs review: {relative}")
        for index in range(len(code) - 1):
            if code[index] in aliases and code[index + 1] == "(":
                hit("indirect process/plugin sink")
            if code[index] in conditional_aliases and code[index + 1] == "(":
                alias_package, alias_method, alias_position = conditional_aliases[code[index]]
                arguments = _go_call_arguments(code, index + 1)
                if arguments is None:
                    uncertain.append(f"unresolvable subprocess alias needs review: {relative}")
                    continue
                if alias_position < len(arguments) and (
                    _injected_endpoint(
                        arguments[alias_position],
                        environment=environment,
                        loopback=loopback,
                        derived=environment | loopback,
                        span=_enclosing_span(spans, index),
                    )
                    or _fixed_process_runs_source(
                        arguments[alias_position],
                        [
                            argument for offset, argument in enumerate(arguments)
                            if offset != alias_position
                        ],
                    )
                ):
                    hit(f"{alias_package}.{alias_method}")
        if "reflect" in imports.values():
            dispatches = {"Call", "CallSlice", "MethodByName", "MakeFunc"}
            dispatch_sites = [
                index
                for index in range(len(code) - 2)
                if code[index] == "."
                and code[index + 1] in dispatches
                and code[index + 2] == "("
            ]
            # ``cmd/apps_catalog.go`` is explicitly inside the Cobra exception
            # boundary.  Its AppDescription helper may invoke a legacy app
            # constructor only to read catalog metadata; that neither dispatches
            # the app nor delegates parsing to Cobra.  Reflection anywhere else,
            # including another function in this same file, remains a finding.
            dispatch_sites = [
                index
                for index in dispatch_sites
                if not _catalog_metadata_factory_call(relative, code, index, spans)
                and not _legacy_cobra_factory_call(code, index, imports, spans)
            ]
            if dispatch_sites:
                hit("reflective dispatch")
            elif any(
                code[index] == "reflect"
                and code[index + 1] == "."
                and code[index + 2] in {"ValueOf", "New", "Zero", "MakeFunc", "MakeSlice", "MakeMap", "MakeChan"}
                and code[index + 3] == "("
                for index in range(len(code) - 3)
            ):
                # Constructing a ``reflect.Value`` is only a review item when the
                # scanned production code can also *obtain a callable* from
                # reflection (``.Method(``) or assert one out of an interface
                # (``.(func``): only then can the value be invoked without one of
                # the dispatch calls above (which remain a hit).  Pure type or
                # struct construction (``reflect.StructOf``/``New``/
                # ``ValueOf(...).Elem()``) cannot invoke anything by itself -- the
                # same reason a bare type descriptor is not a review item -- so it
                # is recorded and decided once the whole production surface is
                # known (see the post-loop check).
                reflect_ctor_files.append(relative)
        expected = _COBRA_COMPAT_SOURCES.get(relative)
        package_scope = False
        if expected is None:
            directory = relative.rsplit("/", 1)[0] if "/" in relative else ""
            allowed_here = _COBRA_COMPAT_BY_PACKAGE.get(directory)
            if allowed_here is not None:
                # The command list may legitimately be split across files of the
                # same package, so an allowed constructor is accepted wherever it
                # lives inside that package.
                expected = allowed_here
                package_scope = True
        if expected is None and optional_cobra:
            expected = _OPTIONAL_COBRA_CONSTRUCTORS
        command_types, type_issues = _cobra_command_types(code, imports, breaks)
        for issue in type_issues:
            uncertain.append(f"{issue}: {relative}")
        functions = _go_functions(code, command_types)
        methods = {
            entry["name"]
            for entry in functions
            if entry["name"] and (entry["receiver"] or not entry["top_level"])
        }
        declared = {
            entry["name"]
            for entry in functions
            if entry["name"] and not entry["receiver"] and entry["top_level"]
        }
        declared |= _var_command_hooks(code, functions)
        if cobra_here:
            declared_by_package.setdefault(
                relative.rsplit("/", 1)[0] if "/" in relative else "", set()
            ).update(declared)
        if scaffold_functions:
            expected = frozenset(expected or ()) | scaffold_functions
        for name in sorted(methods):
            hit(f"Cobra constructor declared as a method, not a top-level function: {name}")
        if expected is None:
            for name in sorted(declared):
                hit(f"Cobra constructor outside the legacy allowlist: {name}")
        else:
            for name in sorted(declared - expected):
                hit(f"unlisted legacy Cobra constructor: {name}")
            # A pinned constructor that is no longer declared is *not* a defect:
            # the command may have been moved to a sibling file, or the whole
            # command group may have been migrated to Kong with only its
            # out-of-scope leaves kept on Cobra.  What is still enforced is that
            # no Cobra command surface appears outside the allowlist (below) and
            # that the pinned legacy adapter is internally consistent.
            for index in _command_constructions(code, command_types):
                if index in scaffold_sites or _inside_approved(functions, index, expected):
                    continue
                owner = _enclosing_function(functions, index)
                where = f": {owner['name']}" if owner is not None and owner["name"] else ""
                hit(f"Cobra command constructed outside every approved constructor{where}")
            for entry in functions:
                if entry["name"] is not None:
                    continue
                position = int(entry["index"])
                if _inside_approved(functions, position, expected):
                    continue
                owner = _enclosing_function(functions, position)
                where = f": {owner['name']}" if owner is not None and owner["name"] else ""
                hit(f"Cobra constructor literal outside every approved constructor{where}")
    if reflect_ctor_files and any(
        _reflect_callable_extraction(code) for code in codes.values()
    ):
        # The production surface both builds ``reflect.Value``s and knows how to
        # obtain a callable through reflection; the combination is what has to be
        # reviewed, and it may be split across files, so the decision is made with
        # the whole surface in hand.
        for relative in reflect_ctor_files:
            uncertain.append(f"reflection in production requires review: {relative}")
    for relative in sorted(_MIGRATED_PRODUCTION - seen):
        uncertain.append(f"migrated production file is missing: {relative}")
    for relative in sorted(set(_COBRA_COMPAT_SOURCES) & _MIGRATED_PRODUCTION):
        uncertain.append(f"legacy Cobra allowlist overlaps migrated production: {relative}")
    for relative, path in _production_scope_sources(root, uncertain, seen):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            uncertain.append(f"unreadable production file: {relative}")
            continue
        code, breaks = _go_source_tokens(text)
        if re.search(r"(?m)^// Code generated .* DO NOT EDIT\.$", text):
            uncertain.append(f"generated production source needs review: {relative}")
            continue
        if re.search(r"(?m)^[ \t]*//(?:go:build|[ \t]*\+build)\b", text):
            uncertain.append(f"build-constrained production source needs review: {relative}")
            continue
        if not code or code[0] != "package":
            uncertain.append(f"production source has no resolvable package clause: {relative}")
            continue
        scope_hits, scope_uncertain = _outside_dispatch_issues(relative, code)
        for token in scope_hits:
            hits.append({"path": relative, "token": token, "kind": "go-bridge", "rule_id": "STRUCTURED-GO-BRIDGE"})
        uncertain.extend(scope_uncertain)
        scope_imports, _ = _go_imports(code)
        for token in _http_bridge_tokens(code, breaks, scope_imports):
            hits.append({"path": relative, "token": token, "kind": "go-bridge", "rule_id": "STRUCTURED-GO-BRIDGE"})
        for token in _network_bridge_tokens(code, breaks, scope_imports):
            hits.append({"path": relative, "token": token, "kind": "go-bridge", "rule_id": "STRUCTURED-GO-BRIDGE"})
    # A pinned constructor may live in any file of its package, and a command
    # group that was migrated to Kong legitimately stops declaring its old Cobra
    # group constructor while keeping only the out-of-scope leaves, so a missing
    # pin is not treated as a finding.  `declared_by_package` is kept for
    # diagnostics only.
    # The leaf inventory is a *multiset of field names*, not a set of
    # (file, field) pairs: the candidate decides which file declares a leaf, so
    # keying on the reference's file names would fail a renamed adapter.  The
    # counts still pin the inventory exactly (an extra leaf of a pinned name is
    # a finding, and a missing pinned leaf is reported below).
    approved_leaf_fields = Counter(field for _, field in _LEGACY_LEAVES | _OPTIONAL_LEGACY_LEAVES)
    leaf_counts: dict[str, int] = {}
    leaf_paths: dict[str, str] = {}
    for path, field in leaves:
        leaf_counts[field] = leaf_counts.get(field, 0) + 1
        leaf_paths.setdefault(field, path)
    for field in sorted(leaf_counts):
        path = leaf_paths[field]
        if leaf_counts[field] > approved_leaf_fields.get(field, 0):
            hits.append({"path": path, "token": f"unexpected legacy Leaf {field}", "kind": "go-bridge", "rule_id": "LEGACY-DISPATCH"})
    scope_hits = _legacy_scope_issues(codes)
    for path, token in scope_hits:
        hits.append({"path": path, "token": token, "kind": "go-bridge", "rule_id": "LEGACY-DISPATCH"})
    # The adapter file is the one that holds the pinned wiring; the candidate
    # chooses its path (see ``_legacy_adapter_files``).
    adapter_files = sorted(_legacy_adapter_files(codes))
    kong_file = root / adapter_files[0] if len(adapter_files) == 1 else None
    legacy_file = root / "internal/konglegacy/legacy.go"
    # ``instruction.md`` only requires the out-of-scope command groups to stay
    # mounted under the Kong root; it does not prescribe a file layout for that
    # mounting.  The inventory checks below describe one specific adapter
    # (``cmd/kong.go`` + ``internal/konglegacy``), so they only apply when the
    # candidate actually uses it.  A candidate that keeps Cobra for the
    # permitted exceptions through its own passthrough helper has no pinned
    # inventory to compare against: its Cobra usage is already pinned per file
    # by ``_COBRA_COMPAT_SOURCES``/``_OPTIONAL_COBRA_SOURCES`` and its sinks are
    # scanned exactly like everywhere else, so inventing an inventory would
    # turn a legal design into an inconclusive scan.
    pinned_adapter = bool(leaf_counts) or any(
        "internal/konglegacy" in package
        for code in codes.values()
        for package in _go_imports(code)[0].values()
    ) or any("legacyConstructors" in code for code in codes.values())
    if pinned_adapter:
        required_leaf_fields = Counter(field for _, field in _LEGACY_LEAVES)
        if any(leaf_counts.get(field, 0) < count for field, count in required_leaf_fields.items()):
            uncertain.append("legacy Leaf inventory differs from approved command paths")
        allowed_constructors = dict(_LEGACY_CONSTRUCTORS)
        allowed_constructors.update(_OPTIONAL_LEGACY_CONSTRUCTORS)
        if kong_file is None or not kong_file.is_file() or not legacy_file.is_file():
            uncertain.append("legacy dispatch source is missing")
        else:
            adapter_path = str(adapter_files[0])
            kong = kong_file.read_text(encoding="utf-8", errors="replace")
            legacy = legacy_file.read_text(encoding="utf-8", errors="replace")
            match = re.search(r"legacyConstructors\s*=\s*map\[string\]func\(\)\s*\*cobra\.Command\s*\{(.*?)\n\s*\}", kong, re.S)
            if match is None:
                uncertain.append("legacy constructor map is not statically resolvable")
            else:
                body = re.sub(r"//[^\n]*", "", match.group(1))
                entries = re.findall(r'"([^"\n]+)"\s*:\s*(\w+\.\w+)\s*,?', body)
                missing_entries = set(_LEGACY_CONSTRUCTORS) - {key for key, _ in entries}
                extra_entries = {key for key, _ in entries} - set(allowed_constructors)
                if (
                    missing_entries
                    or extra_entries
                    or len(entries) != len(set(key for key, _ in entries))
                    or body.count(":") != len(entries)
                ):
                    uncertain.append("legacy constructor map contains dynamic or missing entries")
                for key, constructor in entries:
                    if allowed_constructors.get(key) != constructor:
                        hits.append({"path": str(adapter_files[0]) if len(adapter_files) == 1 else "legacy-adapter", "token": f"unexpected legacy dispatch: {key} -> {constructor}", "kind": "go-bridge", "rule_id": "LEGACY-DISPATCH"})
            if not re.search(r"konglegacy\.SetExecutor\(executeLegacy\)", kong) or not re.search(r"legacyConstructors\[path\]", kong):
                uncertain.append("legacy executor dispatch is not statically resolvable")
            if len(re.findall(r"\blegacyConstructors\s*\[", kong)) != 1:
                uncertain.append("legacy constructor map has dynamic accesses")
            kong_tokens = [match.group() for match in _GO_TOKENS.finditer(kong)]
            kong_code = [token for token in kong_tokens if not token.startswith(("//", "/*"))]
            dispatch_hits, dispatch_uncertain = _legacy_dispatch_issues(kong_code)
            for token in dispatch_hits:
                hits.append({"path": adapter_path, "token": token, "kind": "go-bridge", "rule_id": "LEGACY-DISPATCH"})
            uncertain.extend(dispatch_uncertain)
            executor_hits, executor_uncertain = _legacy_executor_issues(kong_code)
            for token in executor_hits:
                hits.append({"path": adapter_path, "token": token, "kind": "go-bridge", "rule_id": "LEGACY-DISPATCH"})
            uncertain.extend(executor_uncertain)
            if not re.search(r"return\s+executor\(CanonicalPath\(ctx\.Selected\(\)\),\s*l\.Args\)", legacy):
                uncertain.append("legacy canonical path dispatch is not statically resolvable")
    hits.extend(_vendor_bridge_scan(root, codes, uncertain, coverage))
    if coverage is not None:
        coverage["http_sinks_examined"] = sum(
            1 for code in codes.values() if "net/http" in _go_imports(code)[0].values()
        )
        coverage["deceptive_hits"] = sum(
            1 for row in hits if _go_bridge_severity(str(row.get("token", ""))) != "ordinary"
        )
        coverage["ordinary_hits"] = sum(
            1 for row in hits if _go_bridge_severity(str(row.get("token", ""))) == "ordinary"
        )
        coverage["uncertainties"] = list(uncertain)
    for row in hits:
        row["severity"] = _go_bridge_severity(str(row.get("token", "")))
    return hits, uncertain


def _safe_relative(value: str) -> bool:
    path = Path(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and "\\" not in value


def _eligible(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return (
        path.is_file()
        and not path.is_symlink()
        and not any(part in _EXCLUDED_PARTS for part in relative.parts)
    )


def _paths(root: Path, patterns: Sequence[str]) -> Iterable[Path]:
    if not patterns:
        return ()
    collected: list[Path] = []
    for pattern in patterns:
        if not _safe_relative(pattern):
            continue
        path = root / pattern
        if any(token in pattern for token in "*?["):
            collected.extend(root.glob(pattern))
        elif path.is_dir() and not path.is_symlink():
            collected.extend(path.rglob("*"))
        else:
            collected.append(path)
    candidates = collected
    unique = {path.resolve(): path for path in candidates if _eligible(root, path)}
    return (unique[key] for key in sorted(unique, key=lambda item: item.as_posix()))


def _target_matches(value: str, target: str) -> bool:
    actual = value.casefold().strip()
    expected = target.casefold().strip()
    return actual == expected or fnmatchcase(actual, expected)


_COBRA_PACKAGE = "github.com/spf13/cobra"
# The first ``ident.ident`` spelling in a target rule.  ``rootCmd.Execute()``
# names a value receiver, ``cobra.Command{`` names a package qualifier; the same
# dotted shape, so the two are told apart by looking the leading identifier up
# in the file's imports first and in its declarations second.
_GO_DOTTED = re.compile(r"(?<![A-Za-z0-9_.])([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)")


def _go_receiver_qualifiers(code: Sequence[str], name: str) -> set[str]:
    """Package qualifiers of the types a value is declared with in this file.

    Covers ``rootCmd := &cobra.Command{}``, ``var rootCmd = &kong.Command{}``,
    ``var rootCmd *cobra.Command`` and ``rootCmd := new(cobra.Command)``.  Only
    direct declarations count: a value assigned from a call (``rootCmd :=
    makeRoot()``) has no visible type here and is reported as unresolved.
    """
    qualifiers: set[str] = set()
    for index, token in enumerate(code):
        if token != name:
            continue
        cursor = index + 1
        if cursor + 1 < len(code) and code[cursor] == ":" and code[cursor + 1] == "=":
            cursor += 2
        elif cursor < len(code) and code[cursor] == "=":
            cursor += 1
        elif (
            index >= 1
            and code[index - 1] == "var"
            and cursor < len(code)
            and code[cursor] not in {"=", "."}
        ):
            pass
        else:
            continue
        while cursor < len(code) and code[cursor] in {"&", "*"}:
            cursor += 1
        if (
            cursor + 2 < len(code)
            and code[cursor] == "new"
            and code[cursor + 1] == "("
        ):
            cursor += 2
        if (
            cursor + 2 < len(code)
            and _GO_IDENTIFIER.fullmatch(code[cursor])
            and code[cursor + 1] == "."
            and _GO_IDENTIFIER.fullmatch(code[cursor + 2])
        ):
            qualifiers.add(code[cursor])
    return qualifiers


def _go_literal_confirmed(text: str, target: str) -> bool:
    """Corroborate a Go literal rule with the file's actual Cobra semantics.

    A text hit alone cannot tell ``rootCmd.Execute()`` on a Cobra command from
    the identical call on a Kong command: both spellings are legal after a
    migration that keeps the method names.  The rule therefore only stands when
    the file really carries Cobra semantics:

    * the file imports ``github.com/spf13/cobra``;
    * a target whose leading identifier is imported must resolve to Cobra (so
      ``kong.Command{`` never counts as ``cobra.Command{``);
    * a target naming a value receiver must have that value declared with a
      Cobra type in the same file.

    Unresolvable receivers keep the historical substring verdict, so a shape
    that cannot be analysed is never silently excused.
    """
    code, _ = _go_source_tokens(text)
    imports, _ = _go_imports(code)
    cobra_aliases = {
        alias for alias, package in imports.items() if package == _COBRA_PACKAGE
    }
    if not cobra_aliases:
        return False
    match = _GO_DOTTED.search(target)
    if match is None:
        return True
    qualifier = match.group(1)
    if qualifier in imports:
        return imports[qualifier] == _COBRA_PACKAGE
    declared = _go_receiver_qualifiers(code, qualifier)
    if not declared:
        return True
    return bool(declared & cobra_aliases)


def _literal_contains(path: Path, target: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    if target.casefold() not in text.casefold():
        return False
    if path.suffix != ".go":
        return True
    return _go_literal_confirmed(text, target)


def _dependency_contains(path: Path, target: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, Mapping):
        for field in (
            "dependencies",
            "devDependencies",
            "peerDependencies",
            "optionalDependencies",
            "require",
            "require-dev",
        ):
            entries = payload.get(field)
            if isinstance(entries, Mapping) and any(
                _target_matches(str(name), target) for name in entries
            ):
                return True
    dependency_lines = (
        re.compile(r"^\s*([A-Za-z0-9@._+/-]+)\s*(?:[<>=~!].*)?$"),
        re.compile(r"^\s*(?:gem|implementation|api|compileOnly|runtimeOnly)\s*[( ]\s*['\"]([^'\"]+)"),
        re.compile(r"^\s*([A-Za-z0-9_.+-]+)\s*=\s*['\"]?[0-9*^~<>=]"),
    )
    for line in text.splitlines():
        for pattern in dependency_lines:
            match = pattern.search(line)
            if match and _target_matches(match.group(1), target):
                return True
    return False


def _import_contains(path: Path, target: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    candidates: list[str] = []
    patterns = (
        re.compile(r"\bfrom\s+([A-Za-z0-9_./@+-]+)\s+import\b"),
        re.compile(r"\bimport\s+([A-Za-z0-9_./@+-]+)"),
        re.compile(r"\bfrom\s*['\"]([^'\"]+)['\"]"),
        re.compile(r"\brequire\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
        re.compile(r"\brequire\s+['\"]([^'\"]+)['\"]"),
        re.compile(r"\b(?:use|using)\s+([A-Za-z0-9_.\\]+)"),
    )
    for pattern in patterns:
        candidates.extend(match.group(1) for match in pattern.finditer(text))
    return any(
        _target_matches(candidate, target)
        or _target_matches(re.split(r"[./\\]", candidate, maxsplit=1)[0], target)
        for candidate in candidates
    )


def _matches(root: Path, path: Path, *, kind: str, target: str) -> bool:
    if kind == "literal":
        return _literal_contains(path, target)
    if kind == "dependency":
        return _dependency_contains(path, target)
    if kind == "import":
        return _import_contains(path, target)
    relative = path.relative_to(root).as_posix()
    return fnmatchcase(relative.casefold(), target.casefold()) or fnmatchcase(
        path.name.casefold(), target.casefold()
    )


def scan_residue_rules(
    root: Path, rules: object
) -> list[dict[str, str]]:
    if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
        return []
    hits: list[dict[str, str]] = []
    for row in rules:
        if not isinstance(row, Mapping):
            continue
        identifier = row.get("id")
        kind = row.get("kind")
        target = row.get("target")
        patterns = row.get("paths", [])
        if (
            not isinstance(identifier, str)
            or not identifier
            or not isinstance(kind, str)
            or not isinstance(target, str)
            or not target
            or not isinstance(patterns, Sequence)
            or isinstance(patterns, (str, bytes))
            or not patterns
            or any(
                not isinstance(item, str) or not _safe_relative(item)
                for item in patterns
            )
        ):
            raise ValueError(f"invalid residue rule: {identifier or '<unknown>'}")
        if kind not in _RULE_KINDS:
            continue
        for path in _paths(root, tuple(patterns)):
            matched = _matches(root, path, kind=str(kind), target=target)
            if matched:
                hits.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "token": target,
                        "kind": str(kind),
                        "rule_id": identifier,
                    }
                )
    return hits


def classify_residue_rules(
    root: Path, rules: object
) -> list[dict[str, object]]:
    """Classify residue rules without treating unsupported syntax as a failure."""

    if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
        return []
    diagnostics: list[dict[str, object]] = []
    for row in rules:
        if not isinstance(row, Mapping):
            continue
        identifier = row.get("id")
        kind = row.get("kind")
        if kind not in _RULE_KINDS:
            diagnostics.append(
                {
                    "rule_id": str(identifier or "<unknown>"),
                    "kind": str(kind or ""),
                    "status": "UNSUPPORTED",
                    "hits": [],
                    "reason": "residue rule syntax is not supported by the generic scanner",
                }
            )
            continue
        hits = scan_residue_rules(root, [row])
        diagnostics.append(
            {
                "rule_id": str(identifier),
                "kind": str(kind),
                "status": "FAIL" if hits else "PASS",
                "hits": hits,
            }
        )
    return diagnostics


__all__ = ["classify_residue_rules", "scan_go_bridges", "scan_residue_rules"]
