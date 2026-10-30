"""纯 Python BM25（片段级），先按可检索题目集合过滤，再算分。"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from app.question_bank.errors import Bm25IndexUnavailableError


@dataclass(frozen=True)
class Bm25Document:
    fragment_id: str
    question_id: str
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class Bm25Hit:
    question_id: str
    fragment_id: str
    score: float
    matched_terms: int
    idf_coverage: float


class Bm25Index:
    def __init__(self, payload: dict) -> None:
        self.k1 = float(payload["k1"])
        self.b = float(payload["b"])
        self.n = int(payload["n"])
        self.avgdl = float(payload["avgdl"])
        self.df: dict[str, int] = {str(k): int(v) for k, v in payload["df"].items()}
        self.docs: list[dict] = list(payload["docs"])

    @classmethod
    def build(
        cls,
        documents: Sequence[Bm25Document],
        *,
        k1: float,
        b: float,
    ) -> "Bm25Index":
        df: dict[str, int] = {}
        rows: list[dict] = []
        total_length = 0
        for document in documents:
            counts: dict[str, int] = {}
            for token in document.tokens:
                counts[token] = counts.get(token, 0) + 1
            for token in counts:
                df[token] = df.get(token, 0) + 1
            length = len(document.tokens)
            total_length += length
            rows.append(
                {
                    "fragment_id": document.fragment_id,
                    "question_id": document.question_id,
                    "length": length,
                    "tf": counts,
                }
            )
        n = len(rows)
        return cls(
            {
                "version": 1,
                "k1": k1,
                "b": b,
                "n": n,
                "avgdl": (total_length / n) if n else 0.0,
                "df": df,
                "docs": rows,
            }
        )

    @classmethod
    def load(cls, path: Path) -> "Bm25Index":
        if not path.exists():
            raise Bm25IndexUnavailableError(f"BM25 索引不存在：{path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise Bm25IndexUnavailableError("BM25 索引无法读取") from exc
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise Bm25IndexUnavailableError("BM25 索引版本不兼容")
        return cls(payload)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "k1": self.k1,
            "b": self.b,
            "n": self.n,
            "avgdl": self.avgdl,
            "df": self.df,
            "docs": self.docs,
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    def _idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        if self.n == 0:
            return 0.0
        return math.log(1.0 + (self.n - df + 0.5) / (df + 0.5))

    def search(
        self,
        query_tokens: Iterable[str],
        eligible_questions: set[str],
        limit: int,
    ) -> list[Bm25Hit]:
        terms = list(dict.fromkeys(query_tokens))
        if not terms or not eligible_questions:
            return []
        idf = {term: self._idf(term) for term in terms}
        idf_total = sum(idf.values())
        best: dict[str, Bm25Hit] = {}
        for doc in self.docs:
            question_id = str(doc["question_id"])
            if question_id not in eligible_questions:
                continue
            tf: dict[str, int] = doc["tf"]
            length = int(doc["length"])
            matched = 0
            matched_idf = 0.0
            score = 0.0
            for term in terms:
                count = tf.get(term, 0)
                if not count:
                    continue
                matched += 1
                matched_idf += idf[term]
                denominator = count + self.k1 * (1.0 - self.b + self.b * length / self.avgdl)
                score += idf[term] * count * (self.k1 + 1.0) / denominator
            if not matched:
                continue
            hit = Bm25Hit(
                question_id=question_id,
                fragment_id=str(doc["fragment_id"]),
                score=score,
                matched_terms=matched,
                idf_coverage=(matched_idf / idf_total) if idf_total else 0.0,
            )
            previous = best.get(question_id)
            if previous is None or (hit.score, hit.fragment_id) > (previous.score, previous.fragment_id):
                best[question_id] = hit
        return sorted(best.values(), key=lambda item: (-item.score, item.question_id))[:limit]