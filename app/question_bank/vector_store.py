"""Qdrant 本地模式封装。

本地目录同一时间只能被一个客户端持有；构建脚本必须在写入后 close 再改名。
服务侧遇到目录被占用或损坏时抛 VectorIndexUnavailable，由上层降级为
关键词检索，而不是报“没有题目”。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.question_bank.errors import VectorIndexUnavailableError


def _point_id(fragment_id: str) -> int:
    return int(hashlib.sha256(fragment_id.encode("utf-8")).hexdigest()[:15], 16)


class VectorStore:
    def __init__(self, path: Path, collection: str, dim: int) -> None:
        self.path = Path(path)
        self.collection = collection
        self.dim = dim
        self._client = None

    def open(self, *, require_collection: bool = True) -> "VectorStore":
        try:
            from qdrant_client import QdrantClient
        except ImportError as exc:  # pragma: no cover
            raise VectorIndexUnavailableError("未安装 qdrant-client") from exc
        try:
            self._client = QdrantClient(path=str(self.path))
            if require_collection and not self._client.collection_exists(self.collection):
                raise VectorIndexUnavailableError(f"向量集合不存在：{self.collection}")
        except VectorIndexUnavailableError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise VectorIndexUnavailableError("向量索引无法打开（可能被其他客户端占用）") from exc
        return self

    @property
    def client(self):
        if self._client is None:
            raise VectorIndexUnavailableError("向量索引尚未打开")
        return self._client

    def create(self, points: int = 0) -> None:
        from qdrant_client.models import Distance, VectorParams

        self.client.create_collection(
            collection_name=self.collection,
            vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE),
        )

    def count(self) -> int:
        return int(self.client.count(self.collection, exact=True).count)

    def payloads(self) -> list[dict[str, Any]]:
        """读取全部点 payload；供构建校验核对片段/父题对齐。"""
        results: list[dict[str, Any]] = []
        offset = None
        while True:
            points, offset = self.client.scroll(
                collection_name=self.collection,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in points:
                results.append(dict(point.payload or {}))
            if offset is None:
                break
        return results

    def upsert(
        self,
        fragments: Sequence[dict[str, Any]],
        vectors: Sequence[Sequence[float]],
        batch_size: int = 64,
    ) -> None:
        from qdrant_client.models import PointStruct

        if len(fragments) != len(vectors):
            raise ValueError("片段与向量数量不一致")
        points = [
            PointStruct(
                id=_point_id(str(fragment["fragment_id"])),
                vector=list(vector),
                payload=dict(fragment["payload"]),
            )
            for fragment, vector in zip(fragments, vectors)
        ]
        for start in range(0, len(points), batch_size):
            self.client.upsert(
                collection_name=self.collection,
                points=points[start : start + batch_size],
                wait=True,
            )

    @staticmethod
    def build_filter(
        *,
        course: str,
        question_type: str | None = None,
        exam_type: str | None = None,
        paper_id: str | None = None,
        knowledge_points: Iterable[str] = (),
        question_ids: Iterable[str] | None = None,
    ):
        from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

        must = [FieldCondition(key="course", match=MatchValue(value=course))]
        if question_type:
            must.append(FieldCondition(key="question_type", match=MatchValue(value=question_type)))
        if exam_type:
            must.append(FieldCondition(key="exam_type", match=MatchValue(value=exam_type)))
        if paper_id:
            must.append(FieldCondition(key="paper_id", match=MatchValue(value=paper_id)))
        for slug in knowledge_points:
            must.append(FieldCondition(key="kp_slugs", match=MatchValue(value=slug)))
        if question_ids is not None:
            # 必须在 Qdrant 取 top-k 之前限定审核范围：否则前 20 条可能全被
            # 未批准题占满，事后过滤会把真正获批的题漏掉。
            allowed = sorted({str(item) for item in question_ids})
            must.append(
                FieldCondition(
                    key="question_id",
                    match=MatchAny(any=allowed or ["__no_eligible_question__"]),
                )
            )
        return Filter(must=must)

    def search(self, vector: Sequence[float], query_filter, limit: int) -> list[dict[str, Any]]:
        try:
            response = self.client.query_points(
                collection_name=self.collection,
                query=list(vector),
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            )
        except Exception as exc:
            raise VectorIndexUnavailableError("向量检索失败") from exc
        results: list[dict[str, Any]] = []
        for point in response.points:
            payload = dict(point.payload or {})
            payload["score"] = float(point.score)
            results.append(payload)
        return results

    def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:  # pragma: no cover - 关闭失败不改变业务结果
                pass

    def __enter__(self) -> "VectorStore":
        return self.open()

    def __exit__(self, *exc_info) -> None:
        self.close()