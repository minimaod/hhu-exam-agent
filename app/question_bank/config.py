"""题目库的路径、模型身份与检索参数。

这些值同时被离线构建工具和查题服务使用；索引清单会记录其中会影响检索
结果的参数，便于判断一个 build 是否仍与当前代码/模型匹配。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BANK_ROOT = PROJECT_ROOT / "data" / "local" / "question-bank"
DEFAULT_MODEL_CACHE = DEFAULT_BANK_ROOT / "models"
DEFAULT_PAPERS_ROOT = PROJECT_ROOT / "data" / "papers"

SCHEMA_VERSION = 1
COURSE = "Python语言程序设计"

MODEL_ID = "BAAI/bge-small-zh-v1.5"
MODEL_REPO = "Qdrant/bge-small-zh-v1.5"
MODEL_DIM = 512
MODEL_FILES = (
    "config.json",
    "model_optimized.onnx",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
)
# 实测 fastembed 0.9.1 的该模型 query_embed 与 embed 完全一致，没有前缀。
QUERY_PREFIX = ""
NORMALIZE = "l2"
DISTANCE = "Cosine"
MAX_TOKENS = 512
SPECIAL_TOKENS = 2
CONTENT_BUDGET = MAX_TOKENS - SPECIAL_TOKENS

COLLECTION = "question_fragments"

BM25_K1 = 1.5
BM25_B = 0.75
RRF_K = 60
CANDIDATE_MULTIPLIER = 8
MIN_CANDIDATES = 20
MAX_CANDIDATES = 100
# 相关性检索的上限：排名结果再多也没有意义，多出来的只是噪声。
MAX_LIMIT = 20
# 整卷浏览的上限：当前 20 份卷单卷最多 61 题、单卷选择题最多 40 题，
# 64 保证“指定试卷就整卷列出”一次就能装下；超过这个数才分批，并明确
# 告诉学生还有多少道没显示。它只控制传输大小，不代表题库总量。
MAX_BROWSE_LIMIT = 64


@dataclass(frozen=True)
class RetrievalConfig:
    """影响排序/无匹配判定的参数。

    默认阈值只是起点，最终值由 `tools/question_bank/evaluate.py` 在调参集上
    选定并写进评估报告；独立验收集冻结后不再改动。
    """

    bm25_k1: float = BM25_K1
    bm25_b: float = BM25_B
    rrf_k: int = RRF_K
    candidate_multiplier: int = CANDIDATE_MULTIPLIER
    min_candidates: int = MIN_CANDIDATES
    max_candidates: int = MAX_CANDIDATES
    vector_threshold: float = 0.58
    idf_coverage_threshold: float = 0.30

    def candidate_count(self, limit: int) -> int:
        wanted = max(limit * self.candidate_multiplier, self.min_candidates)
        return min(wanted, self.max_candidates)

    def as_dict(self) -> dict[str, float | int]:
        return {
            "bm25_k1": self.bm25_k1,
            "bm25_b": self.bm25_b,
            "rrf_k": self.rrf_k,
            "candidate_multiplier": self.candidate_multiplier,
            "min_candidates": self.min_candidates,
            "max_candidates": self.max_candidates,
            "vector_threshold": self.vector_threshold,
            "idf_coverage_threshold": self.idf_coverage_threshold,
        }