"""F007 多学科拓展：多课程共存、课程隔离、新题型检索与跨科知识点消歧测试。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from app.question_bank.bm25 import Bm25Document, Bm25Index
from app.question_bank.config import COLLECTION, MODEL_DIM, SCHEMA_VERSION
from app.question_bank.models import SearchRequest
from app.question_bank.paths import BankPaths, atomic_write_json, write_current
from app.question_bank.service import QuestionBankService
from app.question_bank.taxonomy import load_taxonomies
from app.question_bank.textbuild import build_retrieval_text, make_fragments
from app.question_bank.tokenization import tokenize
from app.question_bank.vector_store import VectorStore
from app.questions.cards import card_from_summary, compose_material
from app.questions.models import EvidenceBundle
from app.questions.runtime import QuestionRuntime
from tools.question_bank import db as QDB


class _FakeEmbedder:
    def fingerprint(self) -> dict[str, Any]:
        return {"id": "fake-bge", "dim": MODEL_DIM, "sha256": "fake-sha256"}

    def token_count_exact(self, text: str) -> int:
        return max(1, len(text) // 2)

    def _vec(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw = [float(digest[i % len(digest)]) / 255.0 + 0.01 for i in range(MODEL_DIM)]
        norm = sum(v * v for v in raw) ** 0.5
        return [v / norm for v in raw]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


def _build_multicourse_bank(root: Path) -> tuple[Path, Path]:
    paths = BankPaths(root)
    paths.taxonomy_dir.mkdir(parents=True, exist_ok=True)
    paths.stopwords_file.write_text("# stop\n的\n了\n", encoding="utf-8")

    # 两份课程词表同时包含别名“向量”，验证按课程精准消歧
    (paths.taxonomy_dir / "knowledge-points.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "course": "Python语言程序设计",
                "knowledge_points": [
                    {
                        "slug": "control-flow",
                        "name": "分支与循环",
                        "definition": "循环结构",
                        "aliases": ["循环"],
                        "patterns": ["for", "循环"],
                        "priority": 10,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (paths.taxonomy_dir / "knowledge-points-math.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "course": "高等数学",
                "knowledge_points": [
                    {
                        "slug": "math-limit",
                        "name": "极限与连续",
                        "definition": "函数极限与无穷小",
                        "aliases": ["极限", "洛必达"],
                        "patterns": ["极限", "lim"],
                        "priority": 10,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (paths.taxonomy_dir / "knowledge-points-linear-algebra.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "course": "几何与线性代数",
                "knowledge_points": [
                    {
                        "slug": "la-vector",
                        "name": "向量组与线性相关",
                        "definition": "线性相关性与秩",
                        "aliases": ["向量", "向量组"],
                        "patterns": ["向量", "线性无关"],
                        "priority": 10,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (paths.taxonomy_dir / "knowledge-points-physics.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "course": "大学物理",
                "knowledge_points": [
                    {
                        "slug": "phys-kinematics",
                        "name": "质点运动学",
                        "definition": "位矢、速度与加速度向量",
                        "aliases": ["向量", "质点运动"],
                        "patterns": ["质点", "加速度", "向量"],
                        "priority": 10,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    taxonomy = load_taxonomies(paths.taxonomy_files)
    embedder = _FakeEmbedder()

    papers = [
        {
            "paper_id": "py-2024-final",
            "title": "2024-2025 Python期末",
            "course": "Python语言程序设计",
            "exam_type": "final",
            "academic_year": "2024-2025",
            "pdf_rel_path": "py.pdf",
            "pdf_sha256": "aaa",
            "physical_pages": 4,
        },
        {
            "paper_id": "math-2025-final",
            "title": "2024-2025 高数BⅠ期末",
            "course": "高等数学",
            "exam_type": "final",
            "academic_year": "2024-2025",
            "pdf_rel_path": "math.pdf",
            "pdf_sha256": "bbb",
            "physical_pages": 8,
        },
        {
            "paper_id": "geo-2024-final",
            "title": "2023-2024 几何与线性代数期末",
            "course": "几何与线性代数",
            "exam_type": "final",
            "academic_year": "2023-2024",
            "pdf_rel_path": "geo.pdf",
            "pdf_sha256": "ccc",
            "physical_pages": 10,
        },
        {
            "paper_id": "phys-2021-final",
            "title": "2020-2021 大学物理II期末",
            "course": "大学物理",
            "exam_type": "final",
            "academic_year": "2020-2021",
            "pdf_rel_path": "phys.pdf",
            "pdf_sha256": "ddd",
            "physical_pages": 4,
        },
    ]

    records = [
        {
            "question_id": "py-2024-final-choice-01",
            "paper_id": "py-2024-final",
            "course": "Python语言程序设计",
            "section": {"code": "一", "title": "一、选择题"},
            "number_label": "1",
            "question_type": "choice",
            "statement": "以下 for 循环语句输出什么？",
            "options": [
                {"label": "A", "text": "1"},
                {"label": "B", "text": "2"},
                {"label": "C", "text": "3"},
                {"label": "D", "text": "4"},
            ],
            "pages": {"statement": [1], "answer": [4]},
            "answer": {"state": "original", "text": "A"},
        },
        {
            "question_id": "math-2025-final-solve-01",
            "paper_id": "math-2025-final",
            "course": "高等数学",
            "section": {"code": "三", "title": "三、计算题"},
            "number_label": "1",
            "question_type": "solve",
            "statement": r"计算极限 $\lim_{x \to 0} \frac{\sin x - x}{x^3}$。",
            "pages": {"statement": [2], "answer": [6]},
            "answer": {"state": "original", "text": r"$-\frac{1}{6}$"},
        },
        {
            "question_id": "geo-2024-final-proof-01",
            "paper_id": "geo-2024-final",
            "course": "几何与线性代数",
            "section": {"code": "五", "title": "五、证明题"},
            "number_label": "1",
            "question_type": "proof",
            "statement": r"证明向量组 $\alpha_1, \alpha_2, \alpha_3$ 线性无关。",
            "pages": {"statement": [4], "answer": [9]},
            "answer": {"state": "original", "text": "反证法略。"},
        },
        {
            "question_id": "phys-2021-final-judge-01",
            "paper_id": "phys-2021-final",
            "course": "大学物理",
            "section": {"code": "一", "title": "一、判断题"},
            "number_label": "1",
            "question_type": "judge",
            "statement": "质点作曲线运动时，其加速度向量总指向轨道凹侧。",
            "pages": {"statement": [1], "answer": []},
            "answer": {"state": "missing", "text": None},
        },
    ]

    build_id = "iv-aaaaaaaaaaaaaaaa"
    source_v = "sv-bbbbbbbbbbbbbbbb"
    build_dir = paths.build_dir(build_id)
    build_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(build_dir / "questions.sqlite3")
    QDB.create_schema(conn)
    for paper in papers:
        QDB.insert_paper(conn, paper)
    for idx, point in enumerate(taxonomy.points, start=1):
        conn.execute(
            "INSERT INTO knowledge_points(slug,name,definition,aliases_json,sort_order) VALUES(?,?,?,?,?)",
            (point.slug, point.name, point.definition, json.dumps(list(point.aliases), ensure_ascii=False), idx),
        )

    decisions: dict[str, Any] = {}
    flat: list[dict[str, Any]] = []
    for rec in records:
        matches = taxonomy.label(rec)
        label_rows = [
            {"slug": m.slug, "basis": m.basis, "rule_id": m.rule_id, "status": "verified"}
            for m in matches
        ]
        QDB.insert_question(conn, rec, labels=label_rows, asset_map={})
        text = build_retrieval_text(rec, [taxonomy.name(row["slug"]) for row in label_rows])
        frags = make_fragments(text, embedder.token_count_exact)
        frag_dicts = [
            {
                "fragment_id": f"{rec['question_id']}#{f.fragment_id}",
                "ordinal": f.ordinal,
                "text": f.text,
                "token_count": f.token_count,
            }
            for f in frags
        ]
        QDB.insert_fragments(conn, rec["question_id"], text, frag_dicts)
        for item in frag_dicts:
            flat.append({"fragment_id": item["fragment_id"], "question_id": rec["question_id"], "text": item["text"]})
        decisions[rec["question_id"]] = {
            "action": "approve",
            "content_sha256": QDB.content_sha256(rec),
            "labels_verified": [row["slug"] for row in label_rows],
            "visual": "sampled",
        }

    conn.commit()
    conn.close()

    bm25 = Bm25Index.build(
        [Bm25Document(item["fragment_id"], item["question_id"], tuple(tokenize(item["text"]))) for item in flat],
        k1=1.5,
        b=0.75,
    )
    (build_dir / "bm25").mkdir(parents=True, exist_ok=True)
    bm25.save(build_dir / "bm25" / "index.json")

    vectors = embedder.embed_documents([item["text"] for item in flat])
    by_id = {rec["question_id"]: rec for rec in records}
    vec_frags = [
        {
            "fragment_id": item["fragment_id"],
            "payload": {
                "fragment_id": item["fragment_id"],
                "question_id": item["question_id"],
                "course": by_id[item["question_id"]]["course"],
                "paper_id": by_id[item["question_id"]]["paper_id"],
                "exam_type": "final",
                "question_type": by_id[item["question_id"]]["question_type"],
                "searchable": True,
                "kp_slugs": [m.slug for m in taxonomy.label(by_id[item["question_id"]])],
            },
        }
        for item in flat
    ]
    store = VectorStore(build_dir / "qdrant", COLLECTION, MODEL_DIM).open(require_collection=False)
    store.create()
    store.upsert(vec_frags, vectors)
    store.close()

    atomic_write_json(
        build_dir / "build.json",
        {
            "schema_version": SCHEMA_VERSION,
            "build_id": build_id,
            "index_version": build_id,
            "source_version": source_v,
            "course": "Python语言程序设计",
            "courses": sorted({p["course"] for p in papers}),
            "kp_courses": dict(taxonomy.course_by_slug),
            "collection": COLLECTION,
            "model": embedder.fingerprint(),
        },
    )
    atomic_write_json(paths.reviews_file, {"schema_version": 1, "decisions": decisions})
    write_current(
        paths,
        build_id=build_id,
        source_version=source_v,
        index_version=build_id,
        previous_build_id=None,
        activated_at="2026-11-18T10:00:00+08:00",
    )

    catalog_path = root / "catalog.json"
    catalog_path.write_text(
        json.dumps(
            [
                {"id": p["paper_id"], "course": p["course"]} for p in papers
            ]
            + [{"id": "prob-2024-final", "course": "概率论与数理统计"}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return root, catalog_path


def test_multicourse_capabilities_isolation_and_new_types(tmp_path: Path) -> None:
    root, catalog_path = _build_multicourse_bank(tmp_path / "qb")
    embedder = _FakeEmbedder()
    with QuestionBankService(root, embedder=embedder, catalog_path=catalog_path) as svc:
        # 四科均 ready 且数量互相独立
        for course, expected_type in (
            ("Python", "choice"),
            ("高数", "solve"),
            ("线代", "proof"),
            ("大物", "judge"),
        ):
            cap = svc.capability(course)
            assert cap.status == "ready"
            assert cap.approved_count == 1
            assert cap.paper_count == 1
            assert cap.by_type == {expected_type: 1}

        # 未建库课程仍为 catalog_only
        prob = svc.capability("概率论")
        assert prob.status == "catalog_only"
        assert prob.paper_count == 1

        # 总状态汇总全部四门课
        st = svc.status()
        assert st.available is True
        assert st.question_count == 4
        assert st.searchable_count == 4

        # 跨科同名别名“向量”严格按课程消歧，不串台
        la_res = svc.search(SearchRequest(course="线代", knowledge_points=["向量"]))
        assert la_res.status == "ok"
        assert [item.question_id for item in la_res.items] == ["geo-2024-final-proof-01"]

        phys_res = svc.search(SearchRequest(course="大物", knowledge_points=["向量"]))
        assert phys_res.status == "ok"
        assert [item.question_id for item in phys_res.items] == ["phys-2021-final-judge-01"]

        # 新题型精准过滤
        math_solve = svc.search(SearchRequest(course="高等数学", question_type="solve"))
        assert math_solve.status == "ok"
        assert math_solve.items[0].question_type == "solve"

        math_proof = svc.search(SearchRequest(course="高等数学", question_type="proof"))
        assert math_proof.status == "no_match"

        # Runtime 与讲解材料不再写死 Python
        runtime = QuestionRuntime(svc, bank_root=root)
        rt_cap = runtime.capability("高数")
        assert rt_cap.status == "ready"
        assert "高等数学 题库可用" in rt_cap.message

        card = card_from_summary(
            math_solve.items[0],
            conversation_id="conv-1",
            build_id="iv-aaaaaaaaaaaaaaaa",
        )
        assert card.source.course == "高等数学"
        material = compose_material(card, EvidenceBundle(status="missing_answer"))
        assert "高等数学" in material
        assert "Python语言程序设计" not in material
        assert "解答题" in material


def test_live_multicourse_http_selection_answer_asset_and_source(client, study_db_path: Path) -> None:
    """用隔离学习库联调真实多学科题库：选物理含图题 / 高数解答题 → 看答案 → 读题图与原页。"""

    from uuid import uuid4
    import pytest
    from app.question_bank.paths import read_current

    if read_current(BankPaths()) is None:
        pytest.skip("多学科题库尚未构建")

    with QuestionBankService() as svc:
        if svc.capability("大学物理").status != "ready":
            pytest.skip("大学物理尚未入库")
        runtime = QuestionRuntime(svc)
        previous = client.app.state.question_runtime
        client.app.state.question_runtime = runtime
        try:
            from tests.test_question_api import add_search_turn, create_conversation

            conv = create_conversation(client)
            # 1) 大学物理：带题图 + 原答案
            phys_res = runtime.search(
                SearchRequest(
                    course="大学物理",
                    paper_id="phys-2020-2021-s1-final-ii",
                    question_type="fill",
                    limit=10,
                ),
                conversation_id=conv["id"],
            )
            fig_card = next(c for c in phys_res.cards if c.assets)
            add_search_turn(study_db_path, conv["id"], fig_card)
            asset_resp = client.get(fig_card.assets[0].url)
            assert asset_resp.status_code == 200
            assert asset_resp.content.startswith(b"\x89PNG")

            # 2) 几何与线性代数：proof 题型写入 v11 question_selections 并看答案 + 原页
            geo_res = runtime.search(
                SearchRequest(
                    course="几何与线性代数",
                    paper_id="geo-2022-2023-s2-final",
                    question_type="proof",
                    limit=5,
                ),
                conversation_id=conv["id"],
            )
            proof_card = geo_res.cards[0]
            assert proof_card.question_type == "proof"
            source_turn = add_search_turn(study_db_path, conv["id"], proof_card)
            r1 = client.post(
                f"/api/conversations/{conv['id']}/turns",
                json={
                    "request_id": str(uuid4()),
                    "kind": "select_question",
                    "question_id": proof_card.question_id,
                    "question_source_turn": source_turn,
                },
            )
            assert r1.status_code == 200
            endpoint = f"/api/conversations/{conv['id']}/questions/{proof_card.question_id}"
            assert client.get(endpoint + f"/answer?build={proof_card.build_id}").status_code == 409

            r2 = client.post(
                f"/api/conversations/{conv['id']}/turns",
                json={
                    "request_id": str(uuid4()),
                    "kind": "view_question_answer",
                    "question_id": proof_card.question_id,
                    "question_source_turn": source_turn,
                },
            )
            assert r2.status_code == 200
            ans_resp = client.get(endpoint + f"/answer?build={proof_card.build_id}")
            assert ans_resp.status_code == 200
            assert ans_resp.json()["answer_detail"]["state"] == "original"
            src_resp = client.get(
                endpoint + f"/source?build={proof_card.build_id}&kind=statement&page={proof_card.pages.statement[0]}"
            )
            assert src_resp.status_code == 200 and src_resp.content.startswith(b"%PDF-")
        finally:
            client.app.state.question_runtime = previous

