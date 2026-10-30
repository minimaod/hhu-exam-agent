"""P0：验证 Qdrant 本地模式的创建、关闭、重开与重复打开行为。

只在一个独立的测试目录里跑，不碰正式题库索引。要回答的问题：
- 本地模式能否在指定目录创建 collection 并写入/检索点；
- `close()` 之后同一进程能否重新打开并读到数据；
- 同一路径能否同时被两个 client 打开（并发占用行为）；
- 关闭后目录里留下哪些文件（便于估计体积与备份策略）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from tools.question_bank import common as C

COLLECTION = "probe_points"
DIM = 8
POINTS = [
    (1, [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]),
    (2, [0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1]),
    (3, [0.1, 0.1, 0.1, 0.1, 0.9, 0.9, 0.9, 0.9]),
]


def _dir_size(path: Path) -> tuple[int, int]:
    files = [item for item in path.rglob("*") if item.is_file()]
    return len(files), sum(item.stat().st_size for item in files)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P0 Qdrant 本地模式生命周期探针")
    parser.add_argument(
        "--path",
        default=str(C.EVIDENCE_ROOT / "p0" / "qdrant-probe"),
        help="独立的探针目录（每次运行会先清空）",
    )
    parser.add_argument("--out", default=str(C.EVIDENCE_ROOT / "p0" / "storage_probe.json"))
    args = parser.parse_args(argv)

    path = Path(args.path).resolve()
    # 删除边界：只允许清理"证据目录下、名字以 -probe 结尾"的独立目录，
    # 防止参数写错时把正式题库或项目目录删掉。
    evidence_root = C.EVIDENCE_ROOT.resolve()
    allowed = (
        evidence_root == path.parent or evidence_root in path.parents
    ) and path.name.endswith("-probe")
    if not allowed:
        print(
            f"拒绝清理 {path}：探针目录必须是 {evidence_root} 之下、以 -probe 结尾的目录。",
            file=sys.stderr,
        )
        return 2
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    logger = C.get_logger("qb.storage_probe", Path(args.out).with_suffix(".log"))
    logger.info("探针目录 %s", C.relative(path))

    from qdrant_client import QdrantClient, models

    report: dict[str, Any] = {
        "tool": "storage_probe.py@0.2",
        "generated_at": C.now_iso(),
        "path": C.relative(path),
        "collection": COLLECTION,
        "steps": [],
    }

    def record(step: str, ok: bool, **extra: Any) -> None:
        report["steps"].append({"step": step, "ok": ok, **extra})
        logger.info("%s -> %s %s", step, "ok" if ok else "FAILED", extra or "")

    # 1) 创建并写入
    client = QdrantClient(path=str(path))
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config=models.VectorParams(size=DIM, distance=models.Distance.COSINE),
    )
    client.upsert(
        collection_name=COLLECTION,
        points=[
            models.PointStruct(id=index, vector=vector, payload={"tag": f"p{index}"})
            for index, vector in POINTS
        ],
    )
    hits = client.query_points(
        collection_name=COLLECTION, query=POINTS[0][1], limit=3
    ).points
    record(
        "create_upsert_search",
        len(hits) == 3,
        top_id=hits[0].id,
        count=client.count(COLLECTION).count,
    )

    # 2) 关闭后同进程重开
    client.close()
    client_again = QdrantClient(path=str(path))
    count_after_reopen = client_again.count(COLLECTION).count
    record("reopen_after_close", count_after_reopen == 3, count=count_after_reopen)

    # 3) 同路径并发打开第二个 client：本地模式的预期是"独占锁"，
    #    这里把两种结果都记录下来，只有"能开但不一致"才算异常。
    try:
        second = QdrantClient(path=str(path))
        try:
            second_count = second.count(COLLECTION).count
            record(
                "concurrent_second_client",
                second_count == 3,
                count=second_count,
                outcome="shared_access",
                note="两个 client 可同时打开（非独占）",
            )
        finally:
            second.close()
    except Exception as exc:  # pragma: no cover - 取决于 qdrant 版本
        record(
            "concurrent_second_client",
            True,
            outcome="exclusive_lock",
            error=f"{type(exc).__name__}: {exc}",
            note="同路径被独占；验证/导入进程不能与已运行的索引进程同时打开同一目录。",
        )

    # 4) 再关再开一次，确认数据仍在
    client_again.close()
    started = time.perf_counter()
    third = QdrantClient(path=str(path))
    reopen_seconds = time.perf_counter() - started
    collections = [item.name for item in third.get_collections().collections]
    third_count = third.count(COLLECTION).count
    record(
        "second_reopen",
        COLLECTION in collections and third_count == 3,
        collections=collections,
        count=third_count,
        reopen_seconds=round(reopen_seconds, 3),
    )
    third.close()

    files, size = _dir_size(path)
    report["storage"] = {"files": files, "bytes": size}
    report["all_ok"] = all(step["ok"] for step in report["steps"])
    C.write_json(Path(args.out), report)
    logger.info("写入 %s", C.relative(Path(args.out)))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())