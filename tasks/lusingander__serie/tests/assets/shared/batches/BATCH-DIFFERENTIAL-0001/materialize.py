#!/usr/bin/env python3
"""Materialize a deterministic Git fixture repository from the shared corpus.

Every case in ``fixtures/batches/BATCH-DIFFERENTIAL-0001/corpus.json`` is built
from a small, explicit list of steps.  The same materializer is used before the
source probe and before the target probe so both observe byte-identical input.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

DEFAULT_DATE = "2024-01-01T01:02:03+00:00"
IDENTITIES = {
    "GIT_AUTHOR_NAME": "Diff Author",
    "GIT_AUTHOR_EMAIL": "diff-author@example.com",
    "GIT_COMMITTER_NAME": "Diff Committer",
    "GIT_COMMITTER_EMAIL": "diff-committer@example.com",
}


class MaterializeError(RuntimeError):
    pass


def _env(repo: str, date: str) -> dict:
    env = dict(os.environ)
    env.update(IDENTITIES)
    env["GIT_AUTHOR_DATE"] = date
    env["GIT_COMMITTER_DATE"] = date
    env["GIT_CONFIG_NOSYSTEM"] = "true"
    env["GIT_MERGE_AUTOEDIT"] = "no"
    env["HOME"] = repo
    return env


def _git(repo: str, args: list[str], date: str = DEFAULT_DATE) -> str:
    command = ["git", *args]
    completed = subprocess.run(
        command,
        cwd=repo,
        env=_env(repo, date),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if completed.returncode != 0:
        raise MaterializeError(f"{' '.join(command)} failed:\n{completed.stdout}")
    return completed.stdout.strip()


def _write(repo: str, path: str, content: str) -> None:
    destination = os.path.join(repo, path)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with open(destination, "w", encoding="utf-8") as handle:
        handle.write(content)


def apply_steps(repo: str, steps: list[dict]) -> None:
    for step in steps:
        op = step.get("op")
        date = step.get("date", DEFAULT_DATE)
        if op == "write":
            _write(repo, step["path"], step.get("content", ""))
        elif op == "git":
            _git(repo, list(step["args"]), date)
        elif op == "commit":
            _git(repo, ["add", "-A"], date)
            _git(repo, ["commit", "--allow-empty", "-m", step["message"]], date)
        elif op == "merge":
            args = ["merge", "--no-ff", "--no-log", "-m", "merge", "--no-edit"]
            args.extend(step.get("branches", []))
            _git(repo, args, date)
        elif op == "tag":
            args = ["tag"]
            if step.get("annotated"):
                args.extend(["-a", step["name"], "-m", step.get("message", step["name"])])
            else:
                args.append(step["name"])
            _git(repo, args, date)
        elif op == "branch":
            _git(repo, ["branch", step["name"]], date)
        elif op == "checkout":
            _git(repo, ["checkout", step["ref"]], date)
        elif op == "update-ref":
            _git(repo, ["update-ref", step["ref"], step["target"]], date)
        else:
            raise MaterializeError(f"unknown step op {op!r}")
        if not os.path.isdir(os.path.join(repo, ".git")):
            raise MaterializeError(f"step {step!r} left no Git repository at {repo}")


def find_case(corpus: dict, case_id: str) -> dict:
    for case in corpus.get("cases", []):
        if case.get("id") == case_id:
            return case
    raise MaterializeError(f"case {case_id!r} not found")


def materialize(corpus_path: str, case_id: str, destination: str) -> str:
    with open(corpus_path, "r", encoding="utf-8") as handle:
        corpus = json.load(handle)
    case = find_case(corpus, case_id)
    if os.path.exists(destination):
        shutil.rmtree(destination)
    os.makedirs(destination)
    apply_steps(destination, case.get("setup", []))
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus")
    parser.add_argument("case_id")
    parser.add_argument("destination")
    args = parser.parse_args(argv)
    destination = materialize(args.corpus, args.case_id, args.destination)
    print(destination)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MaterializeError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
