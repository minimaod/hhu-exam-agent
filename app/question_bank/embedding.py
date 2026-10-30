"""FastEmbed 本地封装：离线加载、精确 token 计数、模型指纹。

查询时绝不下载模型：先检查缓存文件是否齐全，再用 `local_files_only`
加载；缺失就抛 EmbeddingUnavailable，让上层明确降级为关键词检索。
"""

from __future__ import annotations

import hashlib
import json
import threading
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Protocol, Sequence

from app.question_bank.config import (
    MAX_TOKENS,
    MODEL_DIM,
    MODEL_FILES,
    MODEL_ID,
    MODEL_REPO,
    NORMALIZE,
    QUERY_PREFIX,
    SPECIAL_TOKENS,
)
from app.question_bank.errors import EmbeddingUnavailableError


class Embedder(Protocol):
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...

    def token_count_exact(self, text: str) -> int: ...

    def fingerprint(self) -> dict: ...


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_dir(cache_dir: Path) -> Path:
    base = cache_dir / f"models--{MODEL_REPO.replace('/', '--')}"
    snapshots = sorted(path for path in (base / "snapshots").glob("*") if path.is_dir())
    for snapshot in reversed(snapshots):
        if all((snapshot / name).is_file() for name in MODEL_FILES):
            return snapshot
    raise EmbeddingUnavailableError(f"模型缓存不完整：{base}")


def model_fingerprint(cache_dir: Path) -> dict:
    """只读计算模型身份；缺文件时抛 EmbeddingUnavailable。"""

    snapshot = _snapshot_dir(cache_dir)
    files = {name: _sha256_file(snapshot / name) for name in MODEL_FILES}
    material = json.dumps(
        {
            "id": MODEL_ID,
            "repo": MODEL_REPO,
            "files": files,
            "dim": MODEL_DIM,
            "normalize": NORMALIZE,
            "query_prefix": QUERY_PREFIX,
            "max_tokens": MAX_TOKENS,
            "special_tokens": SPECIAL_TOKENS,
            "fastembed": package_version("fastembed"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return {
        "id": MODEL_ID,
        "repo": MODEL_REPO,
        "files": files,
        "dim": MODEL_DIM,
        "normalize": NORMALIZE,
        "distance": "Cosine",
        "query_prefix": QUERY_PREFIX,
        "max_tokens": MAX_TOKENS,
        "special_tokens": SPECIAL_TOKENS,
        "fastembed": package_version("fastembed"),
        "sha256": hashlib.sha256(material.encode("utf-8")).hexdigest(),
    }


class FastEmbedder:
    """真实模型实现；构造时加载 ONNX 模型。"""

    def __init__(self, cache_dir: Path, *, local_files_only: bool = True) -> None:
        self._cache_dir = Path(cache_dir)
        self._fingerprint = model_fingerprint(self._cache_dir)
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - 环境缺依赖
            raise EmbeddingUnavailableError("未安装 fastembed，无法使用向量检索") from exc
        try:
            self._model = TextEmbedding(
                model_name=MODEL_ID,
                cache_dir=str(self._cache_dir),
                local_files_only=local_files_only,
            )
        except Exception as exc:  # fastembed 的异常类型不固定
            raise EmbeddingUnavailableError("向量模型加载失败；已停止自动下载") from exc
        self._tokenizer = self._model.model.tokenizer
        self._lock = threading.Lock()

    def fingerprint(self) -> dict:
        return dict(self._fingerprint)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            with self._lock:
                vectors = list(self._model.embed(list(texts), batch_size=32))
        except Exception as exc:  # pragma: no cover - 运行期模型故障
            raise EmbeddingUnavailableError("文档向量编码失败") from exc
        return [vector.tolist() for vector in vectors]

    def embed_query(self, text: str) -> list[float]:
        try:
            with self._lock:
                vectors = list(self._model.query_embed([text]))
        except Exception as exc:  # pragma: no cover
            raise EmbeddingUnavailableError("查询向量编码失败") from exc
        if not vectors:
            raise EmbeddingUnavailableError("查询向量为空")
        return vectors[0].tolist()

    def token_count_exact(self, text: str) -> int:
        """不受 512 截断影响地计数，用于发现真实超长输入。"""

        try:
            with self._lock:
                self._tokenizer.no_truncation()
                try:
                    encoded = self._tokenizer.encode(text, add_special_tokens=True)
                finally:
                    self._tokenizer.enable_truncation(max_length=MAX_TOKENS)
        except Exception as exc:  # pragma: no cover
            raise EmbeddingUnavailableError("tokenizer 计数失败") from exc
        return len(encoded.ids)