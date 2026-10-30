"""P0：验证嵌入模型能否在本机下载、编码，并记录维度/最大长度/分词器行为。

目的不是建索引，而是把"模型与缓存位置、实际维度、token 上限、耗时"变成
可复核的数字。写入位置由 `--cache` / `--out` 显式给出，缺省落在
`data/local/question-bank/models` 与 `data/local/qb-evidence/p0`。
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from tools.question_bank import common as C

MODEL_NAME = "BAAI/bge-small-zh-v1.5"

SAMPLES = [
    "下列选项中，使用 bool() 函数测试，值不是 False 的是（ ）。",
    "def f(x):\n    return x * 2\n\nprint(f(3))",
    "已知列表 a=[3,1,2]，执行 a.sort() 后 a 的值为（ ）。",
    "填空：Python 源程序文件其后缀通常为 ______。",
]


def _versions() -> dict[str, str]:
    import importlib.metadata as metadata

    payload: dict[str, str] = {"python": sys.version.split()[0], "platform": platform.platform()}
    for name in ("fastembed", "onnxruntime", "tokenizers", "numpy", "qdrant-client"):
        try:
            payload[name] = metadata.version(name)
        except Exception as exc:  # pragma: no cover - 仅记录
            payload[name] = f"unavailable: {exc}"
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P0 嵌入模型探针")
    parser.add_argument("--model", default=MODEL_NAME)
    parser.add_argument("--cache", default=str(C.MODEL_CACHE_ROOT))
    parser.add_argument("--out", default=str(C.EVIDENCE_ROOT / "p0" / "model_probe.json"))
    args = parser.parse_args(argv)

    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    logger = C.get_logger("qb.model_probe", Path(args.out).with_suffix(".log"))
    logger.info("模型=%s 缓存=%s", args.model, C.relative(cache))

    from fastembed import TextEmbedding

    started = time.perf_counter()
    model = TextEmbedding(model_name=args.model, cache_dir=str(cache))
    load_seconds = time.perf_counter() - started
    logger.info("模型加载完成，用时 %.2fs", load_seconds)

    vectors = list(model.embed(SAMPLES))
    dims = {len(vector) for vector in vectors}

    batch_started = time.perf_counter()
    list(model.embed(SAMPLES * 8))
    batch_seconds = time.perf_counter() - batch_started

    tokenizer_report: dict[str, Any] = {}
    max_length = None
    tokenizer = getattr(getattr(model, "model", None), "tokenizer", None)
    if tokenizer is not None:
        truncation = getattr(tokenizer, "truncation", None)
        if isinstance(truncation, dict):
            max_length = truncation.get("max_length")
        tokenizer_report["tokenizer_class"] = type(tokenizer).__name__
        tokenizer_report["truncation"] = truncation
        encoded = tokenizer.encode(SAMPLES[0])
        tokenizer_report["sample_token_ids"] = len(encoded.ids)
        tokenizer_report["sample_tokens"] = encoded.tokens[:24]
    else:
        tokenizer_report["error"] = "未找到 tokenizer，无法按 token 计长"

    # 超长输入是否被截断：先量"未截断"的真实 token 数，再量编码器实际喂给
    # 模型的数量。直接读 token_count 会得到被截到 512 的数字，看不出原文多长。
    truncation_probe: dict[str, Any] = {}
    if tokenizer is not None:
        long_text = "Python 列表推导式与字典推导式" * 300
        tokenizer.no_truncation()
        untruncated = len(tokenizer.encode(long_text).ids)
        tokenizer.enable_truncation(max_length=max_length or 512)
        truncated = len(tokenizer.encode(long_text).ids)
        truncation_probe = {
            "input_chars": len(long_text),
            "untruncated_tokens": untruncated,
            "truncated_tokens": truncated,
            "dropped_tokens": max(untruncated - truncated, 0),
            "capped_at_max_length": bool(max_length and truncated == max_length),
            "max_length": max_length,
        }

    sample_tokens = [
        {"chars": len(text), "tokens": int(model.token_count(text))} for text in SAMPLES
    ]

    sizes = sorted(path.stat().st_size for path in cache.rglob("*") if path.is_file())
    payload = {
        "tool": "model_probe.py@0.1",
        "generated_at": C.now_iso(),
        "model": args.model,
        "cache_dir": C.relative(cache),
        "versions": _versions(),
        "load_seconds": round(load_seconds, 2),
        "embedding_dim": sorted(dims),
        "max_length": max_length,
        "special_tokens": 2,
        "content_budget_tokens": (max_length - 2) if max_length else None,
        "tokenizer": tokenizer_report,
        "truncation_probe": truncation_probe,
        "sample_token_counts": sample_tokens,
        "timing": {
            "single_batch_items": len(SAMPLES),
            "single_batch_ms_per_item": round(
                (batch_seconds / (len(SAMPLES) * 8)) * 1000, 2
            ),
            "batch_items": len(SAMPLES) * 8,
            "batch_total_seconds": round(batch_seconds, 3),
        },
        "cache": {
            "files": int(len(sizes)),
            "bytes": int(sum(sizes)),
            "largest_file_bytes": int(sizes[-1]) if sizes else 0,
        },
    }
    C.write_json(Path(args.out), payload)
    logger.info("写入 %s", C.relative(Path(args.out)))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())