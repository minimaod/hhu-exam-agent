"""F010 D：A 冻结集的显式参数对照启动器。

不修改 ``evals/f010_a`` 的冻结文件；仅在进程中把 A runner 的用例路径指向
``explicit_a_cases.json``，复用 A 的真实题库、目录、记忆种子与 check 函数。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--only", default="")
    args = parser.parse_args(argv)

    from evals.f010_a import run as a_run

    a_run.CASES_PATH = ROOT / "evals/f010_d/explicit_a_cases.json"
    child_args = ["--run-id", args.run_id]
    if args.only:
        child_args.extend(["--only", args.only])
    code = a_run.main(child_args)
    if code != 0:
        return code

    from evals.f010_d.explicit_contract_checks import verify

    verify_code, report = verify(args.run_id, args.only)
    output = ROOT / "tmp" / "f010-a" / args.run_id / "explicit-contract.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"explicit_contract": report["ok"], "path": str(output)}, ensure_ascii=False))
    return verify_code


if __name__ == "__main__":
    raise SystemExit(main())