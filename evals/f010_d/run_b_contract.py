"""B 新契约对照：只为 B05 的已限定选择题范围显式传参，冻结 B 不变。"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    from evals.f010_b import fixtures, run

    original = fixtures.CaseService.turn
    changes = []

    async def explicit_turn(self, text, *, decisions=(), **kwargs):
        if self.case_id == "B05" and text == "第一题给我讲讲":
            decisions = copy.deepcopy(decisions)
            for decision in decisions:
                for action in decision.actions:
                    if action.action == "select_question":
                        before = dict(action.parameters)
                        assert before == {"number_label": "1"}, before
                        action.parameters["question_type"] = "choice"
                        changes.append({"case": "B05", "text": text, "before": before,
                            "after": dict(action.parameters), "reason": "前轮学生已明确要求新卷选择题；同号选择要求模型显式传递题型"})
        return await original(self, text, decisions=decisions, **kwargs)

    fixtures.CaseService.turn = explicit_turn
    try:
        code = run.main(["--run-id", args.run_id])
    finally:
        fixtures.CaseService.turn = original
    assert len(changes) == 1, changes
    output = Path(__file__).resolve().parents[2] / "tmp/f010-b" / args.run_id
    (output / "contract-parameters.json").write_text(json.dumps(changes, ensure_ascii=False, indent=2), encoding="utf-8")
    result = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    return code or (0 if result["all_passed"] else 1)


if __name__ == "__main__":
    raise SystemExit(main())
