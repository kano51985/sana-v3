from __future__ import annotations

import argparse
import json

from sana.modules.orchestration.evaluation import evaluate_cases, load_cases


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic Sana search evals")
    parser.add_argument("--fixtures", required=True)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    report = evaluate_cases(load_cases(args.fixtures))
    print(
        json.dumps(
            report.to_dict(),
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            sort_keys=True,
        )
    )
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
