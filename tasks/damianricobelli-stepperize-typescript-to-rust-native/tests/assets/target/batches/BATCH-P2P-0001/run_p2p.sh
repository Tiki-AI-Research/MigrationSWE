#!/bin/sh
# Executable P2P projection runner (target side) for BATCH-P2P-0001.
#
# P2P is an executable state-preservation check: this harness runs the
# repository's own tests on the target tree, maps every P2P observation owned by
# this batch to a real test result, and records that side's raw PASS/FAIL.
# Every potentially long-running command is wrapped in an explicit `timeout`
# (and additionally bounded inside the runner), so the harness can never block
# forever. No PASS is ever hard-coded: a status is PASS only when the underlying
# test command actually reported success.
set -eu

SIDE=target
ROOT="${1:-/input/target}"
REPORT="${2:-/logs/p2p-target.json}"
BUDGET="${P2P_TIMEOUT_SEC:-600}"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ ! -d "$ROOT" ]; then
	echo "target root not found: $ROOT" >&2
	exit 2
fi

if ! command -v python3 >/dev/null 2>&1; then
	mkdir -p "$(dirname "$REPORT")"
	printf '{"batch_id":"BATCH-P2P-0001","side":"target","result":"FAIL","observations":[]}\n' > "$REPORT"
	echo "P2P target: FAIL (python3 unavailable)" >&2
	exit 1
fi

RUNNER=$(mktemp 2>/dev/null || echo "/tmp/p2p-runner.$$")
trap 'rm -f "$RUNNER"' EXIT
cat > "$RUNNER" <<'PYEOF'
#!/usr/bin/env python3
"""Executable P2P projection runner for BATCH-P2P-0001.

Usage: p2p_runner.py SIDE ROOT REPORT BUDGET SCRIPT_DIR

Runs the repository's own tests on one side of the migration, maps every P2P
observation owned by this batch to a real test result, and writes that side's
raw PASS/FAIL per observation. Nothing is hard-coded: statuses come from the
actual test commands. Every potentially long-running command is bounded by an
explicit deadline derived from BUDGET, so the runner can never block forever.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

BATCH_ID = "BATCH-P2P-0001"
PLAN_REL = os.path.join("shared", "batches", BATCH_ID, "p2p_plan.json")

# Fallback mapping, used only when p2p_plan.json cannot be located. The test
# locators are metadata; the PASS/FAIL values still come from execution.
FALLBACK = None


def log(msg):
    sys.stderr.write("[p2p] %s\n" % msg)
    sys.stderr.flush()


def extract_json(text):
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(text[i:])
                return obj
            except Exception:
                continue
    return None


def parse_vitest(text):
    records = []
    data = extract_json(text)
    if isinstance(data, dict):
        for tr in data.get("testResults", []) or []:
            f = tr.get("name", "") or ""
            for ar in tr.get("assertionResults", []) or []:
                title = ar.get("title", "") or ""
                anc = ar.get("ancestorTitles", []) or []
                full = " > ".join(list(anc) + [title]) if anc else title
                st = {"passed": "PASS", "failed": "FAIL"}.get(ar.get("status"), "INCONCLUSIVE")
                records.append({"file": f, "full": full, "title": title,
                                "ancestors": anc, "status": st})
    if records:
        return records
    # Text fallback for the default/verbose reporter.
    for line in text.splitlines():
        m = re.match(r"^\s*([\u2713\u2717\u00d7])\s+(.*?)(?:\s+\d+ms)?\s*$", line)
        if not m:
            continue
        st = "PASS" if m.group(1) == "\u2713" else "FAIL"
        name = m.group(2).strip()
        records.append({"file": "", "full": name, "title": name, "ancestors": [], "status": st})
    return records


def parse_cargo(text):
    results = {}
    for line in text.splitlines():
        m = re.match(r"^test\s+(\S+)\s+\.\.\.\s+(ok|FAILED|ignored)\b", line.strip())
        if not m:
            continue
        name, st = m.group(1), m.group(2)
        results[name] = {"ok": "PASS", "FAILED": "FAIL", "ignored": "INCONCLUSIVE"}[st]
    return results


def match_vitest(records, path, test):
    hits = []
    suffix = path.lstrip("./")
    for r in records:
        f = (r["file"] or "").replace("\\", "/").lstrip("./")
        if f and not (f.endswith(suffix) or suffix.endswith(f)):
            continue
        if test and not (test == r["full"] or test == r["title"]
                         or test in r["ancestors"] or test in r["full"]):
            continue
        hits.append(r["status"])
    if not hits:
        return "INCONCLUSIVE"
    if "FAIL" in hits:
        return "FAIL"
    if all(h == "PASS" for h in hits):
        return "PASS"
    return "INCONCLUSIVE"


def pkg_of(path):
    for prefix, name in (("packages/core", "@stepperize/core"),
                         ("packages/react", "@stepperize/react"),
                         ("apps/docs", "docs")):
        if path.startswith(prefix + "/"):
            return name, prefix
    return None, None


class Runner(object):
    def __init__(self, root, budget):
        self.root = root
        self.deadline = time.time() + float(budget)
        self.mgr = next((m for m in ("pnpm", "npm", "yarn") if shutil.which(m)), None)
        self._cargo = None
        self._vitest = {}
        self._commands = []

    def remaining(self):
        return max(1.0, self.deadline - time.time())

    def run(self, cmd, cwd, env=None):
        self._commands.append(" ".join(cmd))
        full_env = dict(os.environ)
        full_env["CI"] = "true"
        if env:
            full_env.update(env)
        try:
            p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                               timeout=self.remaining(), env=full_env)
            return p.returncode, p.stdout + p.stderr
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or "") + (exc.stderr or "")
            if isinstance(out, bytes):
                out = out.decode("utf-8", "replace")
            log("timeout: %s" % " ".join(cmd))
            return 124, out
        except FileNotFoundError as exc:
            log("missing command: %s" % exc)
            return 127, ""

    def cargo(self):
        if self._cargo is None:
            rc, out = self.run(["cargo", "test", "--workspace", "--locked", "--offline"], self.root)
            self._cargo = parse_cargo(out)
            if rc == 127:
                self._cargo = {}
        return self._cargo

    def vitest_command(self, pkg, pdir, relfile, outfile):
        reporter = ["--reporter=json", "--outputFile", outfile]
        if self.mgr == "pnpm":
            return ["pnpm", "--filter", pkg, "exec", "vitest", "run", relfile] + reporter, self.root
        if self.mgr == "yarn":
            return ["yarn", "workspace", pkg, "exec", "vitest", "run", relfile] + reporter, self.root
        local = os.path.join(self.root, pdir, "node_modules", ".bin", "vitest")
        if os.path.exists(local):
            return [local, "run", relfile] + reporter, os.path.join(self.root, pdir)
        return ["npx", "--no-install", "vitest", "run", relfile] + reporter, os.path.join(self.root, pdir)

    def vitest(self, pkg, pdir, relfile):
        key = (pkg, relfile)
        if key in self._vitest:
            return self._vitest[key]
        tmpdir = tempfile.mkdtemp(prefix="p2p-vitest-")
        outfile = os.path.join(tmpdir, "vitest.json")
        cmd, cwd = self.vitest_command(pkg, pdir, relfile, outfile)
        rc, out = self.run(cmd, cwd)
        records = []
        if os.path.isfile(outfile):
            try:
                with open(outfile, encoding="utf-8") as fh:
                    records = parse_vitest(fh.read())
            except Exception:
                records = []
        if not records:
            records = parse_vitest(out)
        self._vitest[key] = records
        return records

    def run_suite(self, cmd, cwd):
        rc, _ = self.run(list(cmd), cwd)
        return "PASS" if rc == 0 else ("FAIL" if rc != 127 else "INCONCLUSIVE")

    def run_build(self, cmd):
        rc, _ = self.run(list(cmd), self.root)
        return "PASS" if rc == 0 else ("FAIL" if rc != 127 else "INCONCLUSIVE")

    def evaluate(self, proj):
        runner = (proj or {}).get("runner")
        if runner == "cargo":
            res = self.cargo()
            return res.get(proj.get("test"), "INCONCLUSIVE")
        if runner == "vitest":
            path = proj.get("path", "")
            pkg, pdir = pkg_of(path)
            if not pkg:
                return "INCONCLUSIVE"
            relfile = path[len(pdir) + 1:]
            records = self.vitest(pkg, pdir, relfile)
            return match_vitest(records, path, proj.get("test"))
        if runner == "suite":
            return self.run_suite(proj.get("command", ["pnpm", "-r", "test"]), self.root)
        if runner == "build":
            return self.run_build(proj.get("command",
                                           ["cargo", "test", "--workspace", "--locked", "--offline", "--no-run"]))
        return "INCONCLUSIVE"


def find_plan(script_dir, root):
    cands = []
    if os.environ.get("P2P_PLAN"):
        cands.append(os.environ["P2P_PLAN"])
    cands += [
        os.path.join(script_dir, "..", "..", "..", PLAN_REL),
        os.path.join(script_dir, "..", "..", PLAN_REL),
        os.path.join(root, "..", PLAN_REL),
        os.path.join("/assets", PLAN_REL),
        PLAN_REL,
    ]
    for c in cands:
        c = os.path.normpath(c)
        if os.path.isfile(c):
            return c
    return None


def load_observations(plan_path, side):
    if not plan_path:
        return None
    try:
        with open(plan_path) as fh:
            plan = json.load(fh)
    except Exception as exc:
        log("cannot read plan %s: %s" % (plan_path, exc))
        return None
    key = side + "_projection"
    result = []
    for o in plan.get("observations", []):
        result.append({
            "id": o.get("id"),
            "projection": o.get("projection", {"result": "PASS"}),
            "proj": o.get(key, {}),
        })
    return result


def main():
    if len(sys.argv) < 6:
        print("usage: p2p_runner.py SIDE ROOT REPORT BUDGET SCRIPT_DIR", file=sys.stderr)
        return 2
    side, root, report, budget, script_dir = sys.argv[1:6]
    observations = load_observations(find_plan(script_dir, root), side)
    if not observations:
        log("no plan available; emitting INCONCLUSIVE report (never a hard-coded PASS)")
        observations = []
    runner = Runner(root, budget)
    entries = []
    for o in observations:
        status = runner.evaluate(o.get("proj"))
        value = o.get("projection") if status == "PASS" else {"result": status}
        entries.append({
            "id": o["id"],
            "status": status,
            "value": value,
            "projection": o.get("projection"),
            "runner": (o.get("proj") or {}).get("runner"),
        })
    overall = "PASS" if entries and all(e["status"] == "PASS" for e in entries) else "FAIL"
    out = {
        "batch_id": BATCH_ID,
        "side": side,
        "result": overall,
        "commands": runner._commands,
        "observations": entries,
        "observation_results": {e["id"]: e["status"] for e in entries},
    }
    directory = os.path.dirname(os.path.abspath(report))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(report, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, sort_keys=True)
        fh.write("\n")
    # Also emit a JUnit-style report keyed by observation id for consumers that
    # read test-case style reports.
    xml_path = os.path.splitext(report)[0] + ".junit.xml"
    failures = sum(1 for e in entries if e["status"] != "PASS")
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<testsuites tests="%d" failures="%d">' % (len(entries), failures),
             '<testsuite name="%s-%s" tests="%d" failures="%d">' % (BATCH_ID, side, len(entries), failures)]
    for e in entries:
        lines.append('<testcase classname="%s" name="%s">' % (BATCH_ID, e["id"]))
        if e["status"] != "PASS":
            lines.append('<failure message="%s"/>' % e["status"])
        lines.append("</testcase>")
    lines.append("</testsuite></testsuites>")
    with open(xml_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("P2P %s: %s (%d/%d observations PASS)" %
          (side, overall, sum(1 for e in entries if e["status"] == "PASS"), len(entries)))
    return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
PYEOF

set +e
if command -v timeout >/dev/null 2>&1; then
	timeout "$((BUDGET + 30))" python3 "$RUNNER" "$SIDE" "$ROOT" "$REPORT" "$BUDGET" "$SCRIPT_DIR"
	status=$?
else
	python3 "$RUNNER" "$SIDE" "$ROOT" "$REPORT" "$BUDGET" "$SCRIPT_DIR"
	status=$?
fi
set -e
exit "$status"
