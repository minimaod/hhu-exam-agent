"""完整 48 例的静态参数迁移对照；不挂接原话解析器、不改 A 判分。"""
from __future__ import annotations

import argparse
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    from evals.f010_a import run

    run.CASES_PATH = Path(__file__).with_name("contract_a_cases.json")
    result = run.main(["--run-id", args.run_id])
    from evals.f010_d.explicit_contract_checks import verify
    import json

    code, report = verify(args.run_id, "")
    output = run.RUN_ROOT / args.run_id / "explicit-contract.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return result or code


if __name__ == "__main__":
    raise SystemExit(main())
