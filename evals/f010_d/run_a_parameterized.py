"""A 批参数化启动器：不改 A 冻结文件，在评测进程内挂接旧参数适配。

用法::

    python -m evals.f010_d.run_a_parameterized \
        --run-id 20261106-S1-A-param \
        --case-map evals/f010_d/legacy_case_map.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evals.f010_d.legacy_params import enrich  # noqa: E402


def _load_map(path: Path) -> set[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {str(item) for item in payload.get("allowed", [])}


def _install(case_map: Path, trace_path: Path) -> None:
    from evals.f010_a import fixtures

    allowed = _load_map(case_map)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace = trace_path.open("a", encoding="utf-8")

    class LegacyScriptedToolModel(fixtures.ScriptedToolModel):
        async def ainvoke(self, messages: list[Any], **kwargs: Any) -> Any:
            message = await super().ainvoke(messages, **kwargs)
            case_id = fixtures.ACTIVE.case_id
            turn = fixtures.ACTIVE.turn_index
            batch = fixtures.ACTIVE.batch_index - 1
            text = fixtures.ACTIVE.turn.utterance if fixtures.ACTIVE.turn_index < len(fixtures.ACTIVE.turns) else ""
            for call in message.tool_calls:
                name = str(call.get("name") or "")
                key = f"{case_id}:{turn}:{batch}:{name}"
                if key not in allowed:
                    continue
                before = dict(call.get("args") or {})
                after = enrich(
                    name,
                    before,
                    text,
                    confirmed_course=fixtures.ACTIVE.turn.confirmed_course
                    if getattr(fixtures.ACTIVE.turn, "confirmed_course", None)
                    else None,
                )
                if after != before:
                    call["args"] = after
                    trace.write(
                        json.dumps(
                            {
                                "case": case_id,
                                "turn": turn,
                                "batch": batch,
                                "tool": name,
                                "before": before,
                                "after": after,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    trace.flush()
            return message

    fixtures.ScriptedToolModel = LegacyScriptedToolModel


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--case-map",
        default=str(Path(__file__).with_name("legacy_case_map.json")),
    )
    parser.add_argument("--only", default="")
    parser.add_argument("--replay-f008", action="store_true")
    parser.add_argument("--self-check-detector", action="store_true")
    parser.add_argument("--skip-cases", action="store_true")
    args = parser.parse_args(argv)

    trace_path = (
        PROJECT_ROOT
        / "tmp"
        / "f010-d"
        / args.run_id
        / "a-adapter.jsonl"
    )
    _install(Path(args.case_map), trace_path)

    from evals.f010_a import run

    forwarded = ["--run-id", args.run_id]
    if args.only:
        forwarded += ["--only", args.only]
    if args.replay_f008:
        forwarded.append("--replay-f008")
    if args.self_check_detector:
        forwarded.append("--self-check-detector")
    if args.skip_cases:
        forwarded.append("--skip-cases")
    return run.main(forwarded)


if __name__ == "__main__":
    raise SystemExit(main())