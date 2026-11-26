from __future__ import annotations
import argparse, json, sys
sys.dont_write_bytecode = True
from pathlib import Path
from hardgate_engine import evaluate_hardgate

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    result = evaluate_hardgate(args.candidate_root, evidence)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return 0 if result["status"] == "PASS" else 1
if __name__ == "__main__": raise SystemExit(main())
