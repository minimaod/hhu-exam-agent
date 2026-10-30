"""查题服务：事实库 + BM25 + 向量 + RRF，以及课程能力查询。

P3 只提供进程内 Python 契约，不注册 HTTP 路由，也不接入 LangGraph。P4 在
应用启动时构造本服务、在关闭时调用 `close()`；本地 Qdrant 同一路径只能有
一个客户端，所以不能在一个进程里重复打开同一 build。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

from app.papers.courses import normalize_course
from app.question_bank.bm25 import Bm25Hit, Bm25Index
from app.question_bank.config import (
    CONTENT_BUDGET,
    COURSE,
    DEFAULT_BANK_ROOT,
    DEFAULT_PAPERS_ROOT,
    MAX_LIMIT,
    MODEL_ID,
    RetrievalConfig,
)
from app.question_bank.embedding import Embedder, FastEmbedder, model_fingerprint
from app.question_bank.errors import (
    Bm25IndexUnavailableError,
    EmbeddingUnavailableError,
    QuestionBankNotBuiltError,
    QuestionBankRequestError,
    QuestionBankUnavailableError,
    VectorIndexUnavailableError,
)
from app.question_bank.models import (
    AnswerDetail,
    AnswerSource,
    AnswerSummary,
    AssetRef,
    BankStatus,
    BuildInfo,
    CodeAnnotation,
    CodeBlock,
    CourseCapability,
    CourseRef,
    Degradation,
    FormulaRef,
    IndependentSolution,
    KnowledgePointRef,
    Option,
    PageRefs,
    PaperRef,
    QuestionDetail,
    QuestionSummary,
    ReviewSummary,
    SearchDiagnostics,
    SearchRequest,
    SearchResponse,
)
from app.question_bank.paths import BankLayoutError, BankPaths, read_current, relative
from app.question_bank.repository import QuestionRepository
from app.question_bank.textbuild import build_retrieval_text, make_fragments
from app.question_bank.tokenization import load_stopwords, query_terms
from app.question_bank.vector_store import VectorStore


class QuestionBankService:
    def __init__(
        self,
        bank_root: Path | None = None,
        *,
        embedder: Embedder | None = None,
        retrieval: RetrievalConfig | None = None,
        catalog_path: Path | None = None,
    ) -> None:
        self.paths = BankPaths(Path(bank_root) if bank_root is not None else DEFAULT_BANK_ROOT)
        self.config = retrieval or RetrievalConfig()
        self._catalog_path = catalog_path or (DEFAULT_PAPERS_ROOT / "catalog.json")
        self._injected_embedder = embedder
        self._embedder: Embedder | None = embedder
        self._embedder_error: str | None = None
        self._vector_error: str | None = None
        self._bm25_error: str | None = None
        self._repository: QuestionRepository | None = None
        self._bm25: Bm25Index | None = None
        self._vector: VectorStore | None = None
        self._reviews: dict[str, dict[str, Any]] = {}
        self._content_hashes: dict[str, str] = {}
        self._rejected: set[str] = set()
        self._approved: set[str] = set()
        self._hashes: dict[str, str] = {}
        self._course = COURSE
        self._not_built_reason: str | None = None
        self._unavailable_reason: str | None = None
        self._catalog_courses: set[str] = set()
        self._catalog_counts: dict[str, int] = {}
        self._cold = True
        self._stopwords = load_stopwords(self.paths.stopwords_file)
        self._load_catalog()
        self._open_bank()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def _open_bank(self) -> None:
        try:
            current = read_current(self.paths)
        except BankLayoutError as exc:
            self._unavailable_reason = str(exc)
            return
        if current is None:
            self._not_built_reason = "题目库尚未建立（current.json 不存在）"
            return
        build_id = str(current["build_id"])
        try:
            build_dir = self.paths.build_dir(build_id)
            self._repository = QuestionRepository(build_dir)
        except (BankLayoutError, QuestionBankUnavailableError) as exc:
            self._unavailable_reason = str(exc)
            return
        build = self._repository.build
        self._course = str(build.get("course") or COURSE)
        if (
            build.get("build_id") != build_id
            or build.get("index_version") != current["index_version"]
            or build.get("source_version") != current["source_version"]
        ):
            self._unavailable_reason = "current.json 与 build.json 的版本不一致"
            self._close_partial()
            return
        try:
            self._reviews = self._read_reviews()
            self._content_hashes = self._repository.question_hashes()
            for question_id, decision in self._reviews.items():
                if self._content_hashes.get(question_id) != decision.get("content_sha256"):
                    continue
                if decision.get("action") == "reject":
                    self._rejected.add(question_id)
                elif decision.get("action") == "approve":
                    self._approved.add(question_id)
        except QuestionBankUnavailableError as exc:
            self._unavailable_reason = str(exc)
            self._close_partial()
            return

        self._check_model_identity(build)
        try:
            self._bm25 = Bm25Index.load(build_dir / "bm25" / "index.json")
        except Bm25IndexUnavailableError as exc:
            self._bm25_error = str(exc)
        self._open_vector(build_dir)

    def _close_partial(self) -> None:
        if self._repository is not None:
            self._repository.close()
            self._repository = None

    def _open_vector(self, build_dir: Path) -> None:
        if self._vector_error:
            return
        path = build_dir / "qdrant"
        if not path.is_dir():
            self._vector_error = "向量索引目录不存在"
            return
        store = VectorStore(path, str(self._repository.build.get("collection") or "question_fragments"), int(self._repository.build["model"]["dim"]))
        try:
            store.open()
        except VectorIndexUnavailableError as exc:
            self._vector_error = str(exc)
            return
        self._vector = store

    def _check_model_identity(self, build: dict[str, Any]) -> None:
        expected = build.get("model") or {}
        try:
            if self._embedder is not None:
                actual = self._embedder.fingerprint()
            else:
                actual = model_fingerprint(self.paths.model_dir)
        except EmbeddingUnavailableError as exc:
            self._embedder_error = str(exc)
            self._vector_error = "向量模型不可用"
            return
        if actual.get("sha256") != expected.get("sha256"):
            self._embedder_error = (
                "索引模型身份与本地缓存不一致"
                f"（build={expected.get('id')}, local={actual.get('id')}）"
            )
            self._vector_error = "索引模型身份不兼容"

    def close(self) -> None:
        if self._vector is not None:
            self._vector.close()
            self._vector = None
        self._close_partial()

    def __enter__(self) -> "QuestionBankService":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @property
    def build_id(self) -> str | None:
        """当前已打开 build 的标识；未打开时为 None。"""

        if self._repository is None:
            return None
        return str(self._repository.build.get("build_id") or "") or None

    @property
    def source_version(self) -> str | None:
        if self._repository is None:
            return None
        return str(self._repository.build.get("source_version") or "") or None

    @property
    def approved_count(self) -> int:
        return len(self._approved)

    @property
    def approved_ids(self) -> frozenset[str]:
        return frozenset(self._approved)

    @property
    def repository(self) -> QuestionRepository | None:
        return self._repository

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def search(
        self,
        request: SearchRequest,
        *,
        force_mode: str | None = None,
    ) -> SearchResponse:
        started = time.perf_counter()
        cold = self._cold
        course_input = request.course
        canonical = normalize_course(course_input)
        if canonical is None:
            raise QuestionBankRequestError("course", "课程不能为空")
        known = canonical in self._catalog_courses
        course_ref = CourseRef(input=course_input, canonical=canonical, known=known)

        if self._not_built_reason or self._unavailable_reason:
            status = "bank_not_built" if self._not_built_reason else "bank_unavailable"
            message = self._not_built_reason or self._unavailable_reason
            self._cold = False
            return SearchResponse(
                status=status,
                mode="browse",
                course=course_ref,
                message=message,
                diagnostics=SearchDiagnostics(elapsed_ms=self._ms(started), cold_start=cold),
            )

        if canonical != self._course:
            self._cold = False
            reason = (
                f"“{canonical}”尚未建立题目库；当前仅 Python 真题可查题。"
                if known
                else f"未登记课程“{canonical}”。"
            )
            return SearchResponse(
                status="bank_not_built",
                mode="browse",
                course=course_ref,
                build=self._build_info(),
                message=reason,
                diagnostics=SearchDiagnostics(elapsed_ms=self._ms(started), cold_start=cold),
            )

        assert self._repository is not None
        filters = self._normalise_filters(request, canonical)
        if request.query:
            self._validate_query_length(request.query)
        excluded = self._rejected
        candidates = self._repository.list_question_ids(
            course=canonical,
            question_type=filters["question_type"],
            exam_type=filters["exam_type"],
            paper_id=filters["paper_id"],
            number_label=filters["number_label"],
            knowledge_points=filters["knowledge_point_slugs"],
            excluded=excluded,
        )
        # 默认只返回复核通过（reviews.json 中 approve 且内容哈希仍匹配）的题。
        # 1154 题当前全是 candidate，因此默认检索应为空，而不是把候选当正式结果。
        # 只有显式 preview_candidates=True 才预览候选，评估/自检也走这个显式开关。
        eligible_ids = (
            candidates
            if request.preview_candidates
            else [question_id for question_id in candidates if question_id in self._approved]
        )
        matched_total = len(eligible_ids)
        if not eligible_ids:
            self._cold = False
            if request.preview_candidates:
                message = "候选预览中没有符合条件的题目"
            elif self._approved:
                message = "没有符合筛选条件的已审核题目"
            else:
                message = (
                    "题库中的题目尚未审核，默认检索不返回；"
                    "如需预览候选，请显式开启 preview_candidates。"
                )
            return SearchResponse(
                status="no_match",
                mode="browse",
                course=course_ref,
                build=self._build_info(),
                filters=filters["echo"],
                preview=request.preview_candidates,
                matched_total=0,
                message=message,
                diagnostics=SearchDiagnostics(elapsed_ms=self._ms(started), cold_start=cold),
            )

        if not request.query:
            return self._browse(
                request, course_ref, filters, eligible_ids, excluded, started, cold
            )

        query = request.query
        terms = query_terms(query, self._stopwords)
        # 相关性检索最多返回 MAX_LIMIT 条：排名结果再多也只是噪声，且不分类卡
        # 那样有“总量/范围”的语义。整卷浏览的额度见 MAX_BROWSE_LIMIT。
        ranked_limit = min(request.limit, MAX_LIMIT)
        candidate_k = self.config.candidate_count(ranked_limit)
        mode = force_mode or "auto"
        if mode == "bm25":
            mode = "keyword_only"
        elif mode == "vector":
            mode = "vector_only"
        forced = force_mode is not None

        bm25_hits: list[Bm25Hit] = []
        bm25_ms = 0.0
        if mode in ("auto", "hybrid", "keyword_only") and self._bm25 is not None:
            begin = time.perf_counter()
            bm25_hits = self._bm25.search(terms, set(eligible_ids), candidate_k)
            bm25_ms = (time.perf_counter() - begin) * 1000
        elif mode in ("auto", "hybrid", "keyword_only") and self._bm25 is None:
            if mode == "keyword_only":
                return self._unavailable(
                    course_ref, "BM25 索引不可用：" + (self._bm25_error or "未知原因"), started, cold
                )

        vector_hits: list[dict[str, Any]] = []
        vector_ms = 0.0
        if mode in ("auto", "hybrid", "vector_only"):
            embedder = self._ensure_embedder()
            if embedder is None or self._vector is None:
                if mode == "vector_only":
                    reason = self._vector_error or self._embedder_error or "向量检索不可用"
                    return self._unavailable(course_ref, reason, started, cold)
            else:
                try:
                    begin = time.perf_counter()
                    vector = embedder.embed_query(query)
                    query_filter = VectorStore.build_filter(
                        course=canonical,
                        question_type=filters["question_type"],
                        exam_type=filters["exam_type"],
                        paper_id=filters["paper_id"],
                        knowledge_points=filters["knowledge_point_slugs"],
                        # 只有候选范围确实比过滤后的可检索集更窄时才需要把
                        # ID 列表塞进 Qdrant；预览/全部获批且无拒绝题时列表
                        # 等于候选本身，再传 1152 个 ID 只会拖慢本地过滤。
                        question_ids=(
                            eligible_ids
                            if filters["number_label"] is not None or self._rejected or len(eligible_ids) != len(candidates)
                            else None
                        ),
                    )
                    raw_hits = self._vector.search(vector, query_filter, candidate_k)
                    vector_ms = (time.perf_counter() - begin) * 1000
                    for item in raw_hits:
                        if str(item.get("question_id")) in excluded:
                            continue
                        vector_hits.append(item)
                except (EmbeddingUnavailableError, VectorIndexUnavailableError) as exc:
                    self._vector_error = str(exc)
                    if mode == "vector_only":
                        return self._unavailable(course_ref, str(exc), started, cold)
                    self._vector = None

        effective_mode = mode
        if effective_mode == "auto":
            effective_mode = "hybrid"
        if self._bm25 is None and (self._vector is None or self._embedder is None):
            reasons = [reason for reason in (self._bm25_error, self._vector_error, self._embedder_error) if reason]
            return self._unavailable(
                course_ref,
                "关键词与向量检索均不可用：" + ("；".join(reasons) or "索引缺失"),
                started,
                cold,
            )
        if effective_mode == "hybrid" and not vector_hits and (self._vector is None or self._embedder_error):
            effective_mode = "keyword_only"
        if effective_mode == "hybrid" and self._bm25 is None:
            effective_mode = "vector_only"

        fused = self._fuse(bm25_hits, vector_hits)
        gate = self._gate(effective_mode, bm25_hits, vector_hits)
        degraded = False
        degradation = None
        if effective_mode == "keyword_only" and not forced:
            degraded = True
            degradation = Degradation(
                code="vector_unavailable" if self._vector is None else "embedding_unavailable",
                message=self._embedder_error or self._vector_error or "向量检索不可用，已降级为关键词检索",
            )
        if effective_mode == "vector_only" and self._bm25 is None and not forced:
            degraded = True
            degradation = Degradation(
                code="bm25_unavailable", message=self._bm25_error or "BM25 索引不可用，已降级为向量检索"
            )

        if not fused or gate["no_match"]:
            self._cold = False
            return SearchResponse(
                status="no_match",
                mode=effective_mode,  # type: ignore[arg-type]
                degraded=degraded,
                degradation=degradation,
                course=course_ref,
                build=self._build_info(),
                filters=filters["echo"],
                matched_total=matched_total,
                returned=0,
                preview=request.preview_candidates,
                diagnostics=SearchDiagnostics(
                    elapsed_ms=self._ms(started),
                    bm25_ms=bm25_ms,
                    vector_ms=vector_ms,
                    bm25_candidates=len(bm25_hits),
                    vector_candidates=len(vector_hits),
                    gate=gate,
                    cold_start=cold,
                ),
            )

        summaries = self._repository.fetch_summaries([question_id for question_id, _ in fused])
        items: list[QuestionSummary] = []
        violations = 0
        for question_id, meta in fused:
            record = summaries.get(question_id)
            if record is None:
                violations += 1
                continue
            if not self._passes_filters(record, filters, preview=request.preview_candidates):
                violations += 1
                continue
            items.append(self._summary(record, matched_by=meta["matched_by"], rank=len(items) + 1))
            if len(items) >= ranked_limit:
                break

        self._cold = False
        status = "ok" if items else "no_match"
        return SearchResponse(
            status=status,
            mode=effective_mode,  # type: ignore[arg-type]
            degraded=degraded,
            degradation=degradation,
            course=course_ref,
            build=self._build_info(),
            filters=filters["echo"],
            matched_total=matched_total,
            returned=len(items),
            items=items,
            preview=request.preview_candidates,
            diagnostics=SearchDiagnostics(
                elapsed_ms=self._ms(started),
                bm25_ms=bm25_ms,
                vector_ms=vector_ms,
                bm25_candidates=len(bm25_hits),
                vector_candidates=len(vector_hits),
                filter_violations=violations,
                gate=gate,
                cold_start=cold,
            ),
        )

    def _browse(
        self,
        request: SearchRequest,
        course_ref: CourseRef,
        filters: dict[str, Any],
        eligible_ids: list[str],
        excluded: set[str],
        started: float,
        cold: bool,
    ) -> SearchResponse:
        assert self._repository is not None
        pool = [qid for qid in eligible_ids if qid not in excluded]
        total = len(pool)
        # 分页只是传输控制：位置按“排除已拒绝题之后的稳定题序”推进，
        # 不改变总数，也不让不同会话共享位置。
        start = min(max(request.offset, 0), total)
        ids = pool[start : start + request.limit]
        summaries = self._repository.fetch_summaries(ids)
        items = [
            self._summary(summaries[qid], matched_by="browse", rank=index + 1)
            for index, qid in enumerate(ids)
            if qid in summaries
        ]
        end = start + len(items)
        self._cold = False
        return SearchResponse(
            status="ok" if items else "no_match",
            mode="browse",
            course=course_ref,
            build=self._build_info(),
            filters=filters["echo"],
            matched_total=total,
            offset=start,
            returned=len(items),
            next_offset=end if end < total else None,
            items=items,
            preview=request.preview_candidates,
            diagnostics=SearchDiagnostics(elapsed_ms=self._ms(started), cold_start=cold),
        )

    # ------------------------------------------------------------------
    # 详情与能力
    # ------------------------------------------------------------------

    def detail(
        self,
        question_id: str,
        *,
        include_answer: bool = False,
        include_reference_code: bool = False,
        include_code_blocks: bool = False,
        include_annotations: bool = False,
    ) -> QuestionDetail | None:
        if self._repository is None:
            raise QuestionBankNotBuiltError(self._not_built_reason or self._unavailable_reason or "题目库不可用")
        record = self._repository.fetch_detail(question_id)
        if record is None:
            return None
        summary = self._summary(record)
        answer_detail = None
        if include_answer and record.get("answer_json"):
            payload = record["answer_json"]
            answer_detail = AnswerDetail(
                state=payload.get("state", record["answer_state"]),
                text=payload.get("text"),
                note=payload.get("note"),
                origin=payload.get("origin"),
                label=payload.get("label"),
                sources=[
                    AnswerSource(
                        kind=str(item["kind"]),
                        text=item.get("text"),
                        detail=item.get("detail"),
                        pairing_verified=item.get("pairing_verified"),
                        pages=[int(page) for page in item.get("pages") or []],
                    )
                    for item in record.get("answer_sources") or []
                ],
            )
        blocks = []
        if include_code_blocks:
            blocks = [
                CodeBlock(
                    block_id=item.get("block_id"),
                    method=item.get("method"),
                    method_label=item.get("method_label"),
                    column=item.get("column"),
                    kind=item.get("kind"),
                    name=item.get("name"),
                    text=str(item.get("text") or ""),
                    page=item.get("page"),
                    pages=[int(page) for page in item.get("pages") or []],
                    depends_on=list(item.get("depends_on") or []),
                )
                for item in record.get("code_blocks") or []
            ]
        annotations = []
        if include_annotations:
            annotations = [
                CodeAnnotation(role=item.get("role"), text=str(item.get("text") or ""), page=item.get("page"))
                for item in record.get("code_annotations") or []
            ]
        return QuestionDetail(
            **summary.model_dump(),
            answer_detail=answer_detail,
            independent_solutions=[
                IndependentSolution(author=item.get("author"), text=str(item.get("text") or ""), note=item.get("note"))
                for item in record.get("independent_solutions") or []
            ]
            if include_answer
            else [],
            reference_code=record.get("reference_code") if include_reference_code else None,
            reference_code_pages=(
                [int(page) for page in record.get("reference_code_pages") or []]
                if include_reference_code
                else []
            ),
            code_blocks=blocks,
            code_annotations=annotations,
            code_examples=[str(item.get("text") or "") for item in record.get("code_examples") or []],
        )

    def capability(self, course: str) -> CourseCapability:
        canonical = normalize_course(course)
        if canonical is None:
            raise QuestionBankRequestError("course", "课程不能为空")
        known = canonical in self._catalog_courses
        if canonical != self._course:
            if not known:
                return CourseCapability(
                    course=canonical, known=False, status="unknown", reason="未登记课程"
                )
            return CourseCapability(
                course=canonical,
                known=True,
                status="catalog_only",
                paper_count=self._catalog_counts.get(canonical, 0),
                reason="该课程只有试卷目录，题目库尚未建立",
            )
        if self._repository is None:
            return CourseCapability(
                course=canonical,
                known=True,
                status="error",
                reason=self._not_built_reason or self._unavailable_reason or "题目库不可用",
            )
        stats = self._repository.course_stats(canonical)
        approved = self._approved_count()
        if not stats["searchable_count"]:
            status = "error"
            reason = "没有可检索题目"
        elif not approved:
            status = "unreviewed"
            reason = (
                f"有 {stats['searchable_count']} 道可检索题，但均未审核；"
                "默认检索不返回，候选预览需显式开启"
            )
        else:
            status = "ready"
            reason = None
        return CourseCapability(
            course=canonical,
            known=True,
            status=status,
            question_count=stats["question_count"],
            searchable_count=stats["searchable_count"],
            approved_count=approved,
            by_type=stats["by_type"],
            answer_coverage={
                "original": stats["answered_count"],
                "missing": stats["question_count"] - stats["answered_count"],
            },
            verified_label_count=self._verified_label_count(),
            build_id=self._repository.build.get("build_id"),
            source_version=self._repository.build.get("source_version"),
            verified=True,
            reason=reason,
            paper_count=len(self._repository.papers()),
        )

    def status(self) -> BankStatus:
        if self._repository is None:
            return BankStatus(
                available=False,
                message=self._not_built_reason or self._unavailable_reason or "题目库不可用",
                degraded=True,
            )
        stats = self._repository.course_stats(self._course)
        degraded = self._vector is None or self._embedder_error is not None
        return BankStatus(
            available=True,
            build=self._build_info(),
            question_count=stats["question_count"],
            fragment_count=self._repository.fragment_count(),
            searchable_count=stats["searchable_count"],
            degraded=degraded,
            degradation=Degradation(
                code="vector_unavailable",
                message=self._embedder_error or self._vector_error or "向量检索不可用",
            )
            if degraded
            else None,
        )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _load_catalog(self) -> None:
        try:
            rows = json.loads(self._catalog_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(rows, list):
            return
        for row in rows:
            course = normalize_course(str(row.get("course") or ""))
            if not course:
                continue
            self._catalog_courses.add(course)
            self._catalog_counts[course] = self._catalog_counts.get(course, 0) + 1

    def _read_reviews(self) -> dict[str, dict[str, Any]]:
        path = self.paths.reviews_file
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise QuestionBankUnavailableError("reviews.json 无法读取") from exc
        decisions = payload.get("decisions") if isinstance(payload, dict) else None
        if not isinstance(decisions, dict):
            raise QuestionBankUnavailableError("reviews.json 缺少 decisions")
        return {str(key): value for key, value in decisions.items() if isinstance(value, dict)}

    def _ensure_embedder(self) -> Embedder | None:
        if self._embedder is not None:
            return self._embedder
        if self._embedder_error:
            return None
        try:
            self._embedder = FastEmbedder(self.paths.model_dir)
        except EmbeddingUnavailableError as exc:
            self._embedder_error = str(exc)
            return None
        if self._repository is not None:
            expected = (self._repository.build.get("model") or {}).get("sha256")
            if expected and self._embedder.fingerprint().get("sha256") != expected:
                self._embedder_error = "向量模型与索引清单不一致"
                return None
        return self._embedder

    def _validate_query_length(self, query: str) -> None:
        if self._embedder is None:
            try:
                self._embedder = FastEmbedder(self.paths.model_dir)
            except EmbeddingUnavailableError:
                if len(query) > 1000:
                    raise QuestionBankRequestError("query", "查询过长，请缩短到 1000 字以内")
                return
        count = self._embedder.token_count_exact(query)
        if count > CONTENT_BUDGET:
            raise QuestionBankRequestError(
                "query", f"查询长度为 {count} token，超过 {CONTENT_BUDGET} 的上限"
            )

    def _normalise_filters(self, request: SearchRequest, canonical: str) -> dict[str, Any]:
        assert self._repository is not None
        paper_id = request.paper_id
        if request.number_label is not None and paper_id is None:
            raise QuestionBankRequestError("paper_id", "按题号查找需要先确定试卷")
        if paper_id is not None:
            paper = self._repository.get_paper(paper_id)
            if paper is None:
                raise QuestionBankRequestError("paper_id", f"试卷 {paper_id} 不存在")
            if normalize_course(str(paper.get("course") or "")) != canonical:
                raise QuestionBankRequestError("paper_id", "该试卷不属于所选课程")
            if request.exam_type and paper.get("exam_type") != request.exam_type:
                raise QuestionBankRequestError("exam_type", "试卷考试类型与请求条件冲突")
        slugs: list[str] = []
        unknown: list[str] = []
        for name in request.knowledge_points:
            slug = self._normalise_kp(name)
            if slug is None:
                unknown.append(name)
            elif slug not in slugs:
                slugs.append(slug)
        if unknown:
            raise QuestionBankRequestError(
                "knowledge_points", "未知知识点：" + "、".join(unknown)
            )
        return {
            "question_type": request.question_type,
            "exam_type": request.exam_type,
            "paper_id": paper_id,
            "number_label": request.number_label,
            "knowledge_point_slugs": slugs,
            "echo": {
                "course": canonical,
                "question_type": request.question_type,
                "exam_type": request.exam_type,
                "paper_id": paper_id,
                "number_label": request.number_label,
                "knowledge_points": request.knowledge_points,
            },
        }

    def _normalise_kp(self, name: str) -> str | None:
        if self._repository is None:
            return None
        key = name.strip().casefold()
        for row in self._repository.all_knowledge_points():
            candidates = [row["slug"], row["name"], *row["aliases"]]
            if any(str(item).strip().casefold() == key for item in candidates):
                return str(row["slug"])
        return None

    def _passes_filters(
        self, record: dict[str, Any], filters: dict[str, Any], *, preview: bool = False
    ) -> bool:
        if record["course"] != self._course or not record["searchable"]:
            return False
        if not preview and record["question_id"] not in self._approved:
            return False
        if filters["question_type"] and record["question_type"] != filters["question_type"]:
            return False
        if filters["exam_type"] and record["exam_type"] != filters["exam_type"]:
            return False
        if filters["paper_id"] and record["paper_id"] != filters["paper_id"]:
            return False
        if filters.get("number_label") is not None and record["number_label"] != str(int(filters["number_label"])):
            return False
        wanted = set(filters["knowledge_point_slugs"])
        if wanted:
            present = {item["slug"] for item in record["knowledge_points"]}
            if not wanted <= present:
                return False
        if record["question_id"] in self._rejected:
            return False
        return True

    def _aggregate_vector_hits(
        self, vector_hits: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """同一父题的多个片段只保留最高分片段，再参与 RRF 排名。"""
        best: dict[str, dict[str, Any]] = {}
        for point in vector_hits:
            question_id = str(point.get("question_id") or "")
            if not question_id:
                continue
            score = float(point.get("score") or 0.0)
            previous = best.get(question_id)
            if previous is None or score > float(previous.get("score") or 0.0):
                best[question_id] = point
        return sorted(
            best.values(),
            key=lambda item: (-float(item.get("score") or 0.0), str(item.get("question_id"))),
        )

    def _fuse(
        self,
        bm25_hits: list[Bm25Hit],
        vector_hits: list[dict[str, Any]],
    ) -> list[tuple[str, dict[str, Any]]]:
        fused: dict[str, dict[str, Any]] = {}
        for rank, hit in enumerate(bm25_hits, start=1):
            entry = fused.setdefault(
                hit.question_id,
                {"score": 0.0, "bm25_rank": None, "vector_rank": None, "matched_by": "bm25"},
            )
            entry["score"] += 1.0 / (self.config.rrf_k + rank)
            entry["bm25_rank"] = rank
        for rank, point in enumerate(self._aggregate_vector_hits(vector_hits), start=1):
            question_id = str(point.get("question_id"))
            entry = fused.setdefault(
                question_id,
                {"score": 0.0, "bm25_rank": None, "vector_rank": None, "matched_by": "vector"},
            )
            entry["score"] += 1.0 / (self.config.rrf_k + rank)
            entry["vector_rank"] = rank
        for entry in fused.values():
            if entry["bm25_rank"] and entry["vector_rank"]:
                entry["matched_by"] = "both"
        ordered = sorted(
            fused.items(),
            key=lambda item: (
                -item[1]["score"],
                min(
                    rank
                    for rank in (item[1]["bm25_rank"], item[1]["vector_rank"])
                    if rank is not None
                ),
                item[0],
            ),
        )
        return ordered

    def _gate(
        self,
        mode: str,
        bm25_hits: list[Bm25Hit],
        vector_hits: list[dict[str, Any]],
    ) -> dict[str, Any]:
        best_cos = float(vector_hits[0]["score"]) if vector_hits else 0.0
        best_idf = max((hit.idf_coverage for hit in bm25_hits), default=0.0)
        no_match = False
        # 只要不是空查询的纯 browse，就必须做相关性判断。显式筛选只缩范围，
        # 不能把“量子计算”这类无关查询变成有结果；过滤后的错误结果比空结果更糟。
        applied = True
        if mode in ("bm25", "keyword_only"):
            no_match = not bm25_hits
        elif mode in ("vector", "vector_only"):
            no_match = not vector_hits or best_cos < self.config.vector_threshold
        else:
            no_match = (not bm25_hits and not vector_hits) or (
                best_cos < self.config.vector_threshold
                and best_idf < self.config.idf_coverage_threshold
            )
        return {
            "mode": mode,
            "best_vector_cosine": round(best_cos, 6),
            "best_idf_coverage": round(best_idf, 6),
            "vector_threshold": self.config.vector_threshold,
            "idf_coverage_threshold": self.config.idf_coverage_threshold,
            "gate_applied": applied,
            "no_match": no_match,
        }

    def _summary(
        self,
        record: dict[str, Any],
        *,
        matched_by: str = "bm25",
        rank: int = 0,
    ) -> QuestionSummary:
        review = self._effective_review(record)
        labels = []
        decision = self._reviews.get(record["question_id"]) or {}
        verified = set(decision.get("labels_verified") or []) if self._decision_matches(record) else set()
        for item in record["knowledge_points"]:
            status = "verified" if item["slug"] in verified else item["status"]
            labels.append(KnowledgePointRef(slug=item["slug"], name=item["name"], status=status))
        return QuestionSummary(
            question_id=record["question_id"],
            paper=PaperRef(
                paper_id=record["paper_id"],
                title=record["paper_title"],
                course=record["course"],
                exam_type=record["exam_type"],
                academic_year=record["academic_year"],
            ),
            question_type=record["question_type"],
            number_label=record["number_label"],
            statement=record["statement"],
            content_sha256=record.get("content_sha256") or "",
            options=[Option(**item) for item in record["options"]],
            code_lines=list(record["code_lines"]),
            blank_marks=list(record["blank_marks"]),
            formulas=[FormulaRef(**item) for item in record["formulas"]],
            assets=[AssetRef(**item) for item in record["assets"]],
            pages=PageRefs(statement=list(record["pages_statement"]), answer=list(record["pages_answer"])),
            knowledge_points=labels,
            answer=AnswerSummary(
                state=record["answer_state"],
                available=record["answer_available"],
                label=record["answer_label"],
            ),
            review=review,
            matched_by=matched_by,  # type: ignore[arg-type]
            rank=rank,
        )

    def _effective_review(self, record: dict[str, Any]) -> ReviewSummary:
        base = record["review_status"]
        stale = False
        decision = self._reviews.get(record["question_id"])
        status = base
        visual = record["visual_status"]
        if decision is not None:
            if self._decision_matches(record):
                action = decision.get("action")
                if action in ("approve", "reject"):
                    status = "approved" if action == "approve" else "rejected"
                visual = str(decision.get("visual") or visual)
            else:
                stale = True
        return ReviewSummary(
            status=status,  # type: ignore[arg-type]
            visual=visual,
            approved=status == "approved",
            stale=stale,
        )

    def _decision_matches(self, record: dict[str, Any]) -> bool:
        decision = self._reviews.get(record["question_id"])
        return bool(decision and decision.get("content_sha256") == record["content_sha256"])

    def _approved_count(self) -> int:
        count = 0
        for question_id, decision in self._reviews.items():
            if decision.get("action") == "approve" and self._content_hashes.get(question_id) == decision.get("content_sha256"):
                count += 1
        return count

    def _verified_label_count(self) -> int:
        if self._repository is None:
            return 0
        verified = {
            (str(row[0]), str(row[1]))
            for row in self._repository.conn.execute(
                "SELECT question_id,kp_slug FROM question_knowledge_points WHERE status='verified'"
            )
        }
        known = {row["slug"] for row in self._repository.all_knowledge_points()}
        for question_id, decision in self._reviews.items():
            if self._content_hashes.get(question_id) == decision.get("content_sha256"):
                verified.update(
                    (question_id, slug) for slug in decision.get("labels_verified") or []
                    if slug in known
                )
        return len(verified)

    def _build_info(self) -> BuildInfo | None:
        if self._repository is None:
            return None
        build = self._repository.build
        return BuildInfo(
            build_id=str(build.get("build_id")),
            source_version=str(build.get("source_version")),
            index_version=str(build.get("index_version")),
            created_at=build.get("created_at"),
        )

    def _unavailable(self, course: CourseRef, message: str, started: float, cold: bool) -> SearchResponse:
        self._cold = False
        return SearchResponse(
            status="bank_unavailable",
            mode="browse",
            course=course,
            build=self._build_info(),
            message=message,
            diagnostics=SearchDiagnostics(elapsed_ms=self._ms(started), cold_start=cold),
        )

    @staticmethod
    def _ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 2)


def describe_bank(bank_root: Path | None = None) -> dict[str, Any]:
    """给报告/CLI 用的只读摘要；不打开 Qdrant，避免占用锁。"""

    service = QuestionBankService(bank_root, embedder=None)
    try:
        status = service.status()
        capability = service.capability(COURSE) if status.available else None
        return {
            "status": status.model_dump(),
            "capability": capability.model_dump() if capability else None,
        }
    finally:
        service.close()
