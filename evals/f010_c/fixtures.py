"""F010 C 专项评测夹具：真实服务栈 + 真实题库副本 + 脚本替身。

与 A/B 批共用离线题库副本与守卫，但运行目录、用例、判据独立；只读写
``tmp/f010-c``，不读取或改写 A/B 的评测文件与运行记录。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals.f010_a import fixtures as _a
from evals.f010_b.fixtures import BEnv, CaseService, install_offline_config

PROJECT_ROOT = Path(__file__).resolve().parents[2]
C_EVAL_DIR = Path(__file__).resolve().parent
RUN_ROOT = PROJECT_ROOT / "tmp" / "f010-c"

OfflineGuard = _a.OfflineGuard
PY_COURSE = "Python语言程序设计"
PAPER = "py-2024-2025-s1-final"


def prepare_run_dir(run_id: str) -> Path:
    if not run_id or "/" in run_id or "\\" in run_id:
        raise SystemExit("run-id 必须是单段目录名")
    target = RUN_ROOT / run_id
    if target.exists():
        raise SystemExit(f"运行目录已存在，拒绝覆盖历史证据：{target}")
    target.mkdir(parents=True)
    return target


def bootstrap(run_id: str) -> BEnv:
    run_dir = prepare_run_dir(run_id)
    _a.RUN_ROOT = RUN_ROOT
    bank = _a.prepare_bank(run_dir)
    papers = _a.prepare_papers(run_dir)
    os.environ["HHU_QB_ROOT"] = str(bank)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    temp = run_dir / "system-temp"
    temp.mkdir(exist_ok=True)
    os.environ["TEMP"] = str(temp)
    os.environ["TMP"] = str(temp)
    install_offline_config()
    guard = OfflineGuard()
    guard.install()
    guard.self_test()
    return BEnv(run_dir=run_dir, bank=bank, papers=papers, guard=guard)


__all__ = [
    "BEnv",
    "CaseService",
    "PY_COURSE",
    "PAPER",
    "bootstrap",
    "prepare_run_dir",
]