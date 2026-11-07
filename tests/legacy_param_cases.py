"""S1 参数适配器：仅对获批的 case/turn/batch/action 显式启用。

旧解析器只属于评测与测试（``evals.f010_d.legacy_intent``）。生产代码不能
反向导入它；本模块只给参数化 A 回放和适配器单测使用。未列入
``legacy_case_map.json`` 的 key（包括 41–48 的缺参/错卷/同轮纠正反例）
一律原样返回，绝不自动补齐。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals.f010_d.legacy_params import enrich

MAP_PATH = Path(__file__).resolve().parents[1] / "evals" / "f010_d" / "legacy_case_map.json"


def load_allowed(path: Path = MAP_PATH) -> set[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return set(payload["allowed"])


@dataclass
class LegacyParameterAdapter:
    """按审批 key 逐动作启用的参数适配器；默认全部不启用。"""

    allowed: set[str] = field(default_factory=load_allowed)
    log_path: Path | None = None
    records: list[dict[str, Any]] = field(default_factory=list)

    def key(self, case_id: str, turn: int, batch: int, action: str) -> str:
        return f"{case_id}:{turn}:{batch}:{action}"

    def enabled(self, case_id: str, turn: int, batch: int, action: str) -> bool:
        return self.key(case_id, turn, batch, action) in self.allowed

    def enrich(
        self,
        case_id: str,
        turn: int,
        batch: int,
        action: str,
        parameters: dict[str, Any] | None,
        text: str,
        *,
        confirmed_course: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        before = dict(parameters or {})
        if not self.enabled(case_id, turn, batch, action):
            return before, {}
        after = enrich(
            action,
            before,
            text,
            confirmed_course=confirmed_course,
        )
        added = {key: value for key, value in after.items() if key not in before}
        if added:
            record = {
                "case": case_id,
                "turn": turn,
                "batch": batch,
                "action": action,
                "added": added,
            }
            self.records.append(record)
            if self.log_path is not None:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return after, added