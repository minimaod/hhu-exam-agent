"""F019 第三阶段：会话内资料复习、按需生成、增量重试、多文档澄清、删除边界与迁移测试。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth.dependencies import require_user
from app.auth.migrate import migrate_database
from app.conversations import schema
from app.conversations.graph import ActionPlan, ScheduleDecision
from app.main import app
from app.materials import storage as mat_storage
from tests.f012_support import TEST_CSRF_HEADERS, create_invite, register
from tests.test_materials_pdf_parser import _make_simple_pdf
from tests.tutoring_fakes import FakeScheduler


def _setup_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, TestClient, Path, Path]:
    db_path = tmp_path / "study.sqlite3"
    mat_root = tmp_path / "materials"
    att_root = tmp_path / "attachments"
    mat_root.mkdir(parents=True, exist_ok=True)
    att_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HHU_STUDY_DB", str(db_path))
    monkeypatch.setattr("app.conversations.repository.DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(mat_storage, "MATERIALS_ROOT", mat_root)
    monkeypatch.setattr("app.materials.service.MATERIALS_ROOT", mat_root)
    monkeypatch.setattr("app.materials.service.ATTACHMENTS_ROOT", att_root)
    app.dependency_overrides.pop(require_user, None)

    c_a = TestClient(app)
    c_b = TestClient(app)
    c_a.__enter__()
    c_b.__enter__()
    register(c_a, username="alice_p3", password="Password123!", invite_code=create_invite(db_path))
    register(c_b, username="bob_p3", password="Password123!", invite_code=create_invite(db_path))
    return c_a, c_b, db_path, mat_root


def _mock_exampass_transport(
    calls: list[str],
    *,
    fail_step: str | None = None,
) -> httpx.MockTransport:
    skeleton_json = json.dumps(
        {
            "title": "高斯定理与静电场复习",
            "course_hint": "大学物理",
            "chapters": [
                {
                    "id": "ch1",
                    "label": "静电场与高斯定理",
                    "summary": "真空中静电场通量等于包围电荷除以真空介电常数。",
                    "kcs": [
                        {
                            "id": "kc1.1",
                            "label": "高斯定理",
                            "type": "concept",
                            "importance": "key",
                            "deps": [],
                            "is_hub": True,
                            "source_refs": ["1"],
                        },
                        {
                            "id": "kc1.2",
                            "label": "电势梯度",
                            "type": "procedure",
                            "importance": "key",
                            "deps": ["kc1.1"],
                            "is_hub": False,
                            "source_refs": ["2"],
                        },
                    ],
                }
            ],
        },
        ensure_ascii=False,
    )
    notes_html = (
        "<h2>一、静电场 <span data-slides=\"1\"></span></h2>"
        "<h3>1.1 高斯定理<span data-slides=\"1\"></span></h3>"
        "<div class=\"tldr\"><strong>核心结论：</strong>通量只与闭合曲面内总电荷有关。</div>"
        "<div class=\"checkpoint\">自测：均匀带电球壳内部场强是多少？"
        "<details class=\"cp-answer\"><summary>参考答案</summary>0</details></div>"
        "<h3 data-kc=\"kc1.2\">电势梯度<span data-slides=\"2\"></span></h3><p>静电场为保守场。</p>"
        "<h3>附加讲解</h3><p>保留未匹配的正文。</p>"
    )
    quiz_json = json.dumps(
        {
            "questions": [
                {
                    "type": "choice",
                    "points": 5,
                    "kc_id": "kc1.1",
                    "question": "闭合高斯面内净电荷为零时，面上各点场强：",
                    "options": ["必处处为零", "不一定处处为零", "一定为无穷大", "只与面形状有关"],
                    "answer": 1,
                    "explanation": "通量为零不代表被积函数处处为零。",
                    "cognitive": "understand",
                    "difficulty": "basic",
                    "pitfall": "混淆电通量为零与场强处处为零。",
                },
                {
                    "type": "tf",
                    "points": 5,
                    "kc_id": "kc1.2",
                    "question": "静电场强沿闭合路径的环流恒为零。",
                    "answer": True,
                    "explanation": "静电场为保守场。",
                    "cognitive": "remember",
                    "difficulty": "basic",
                    "pitfall": "与感生涡旋电场混淆。",
                },
            ]
        },
        ensure_ascii=False,
    )
    review_json = json.dumps(
        {"coverage_warnings": ["覆盖第 1–2 页核心知识点。"], "patches": []},
        ensure_ascii=False,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        sys_msg = body["messages"][0]["content"]
        if "知识架构师" in sys_msg:
            step = "skeleton"
            content = skeleton_json
        elif "讲解设计师" in sys_msg:
            step = "notes"
            content = notes_html
        elif "独立复习笔记审校员" in sys_msg:
            step = "notes_review"
            assert "<untrusted_notes_draft>" in body["messages"][1]["content"]
            assert "<script" not in body["messages"][1]["content"]
            content = notes_html + "<p>笔记审校发布</p>"
        elif "命题专家" in sys_msg:
            step = "quiz"
            content = quiz_json
        else:
            step = "review"
            assert "课程命题审校专家" in sys_msg
            review_input = body["messages"][1]["content"]
            assert "笔记摘要" not in review_input
            assert "笔记审校发布" not in review_input
            content = review_json
        calls.append(step)
        if fail_step and step == fail_step:
            return httpx.Response(500, json={"error": {"message": f"{step} mock error"}})
        return httpx.Response(
            200,
            json={
                "id": f"mock-{len(calls)}",
                "object": "chat.completion",
                "created": 1700000000,
                "model": "deepseek-chat",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            },
        )

    return httpx.MockTransport(handler)


def test_workbench_distinguishes_same_title_by_page_and_reports_duplicate_graph_roots():
    from app.materials.workbench import build_kc_index, ensure_note_anchors

    skeleton = {"chapters": [
        {"label": "磁场计算", "kcs": [{"id": "m1", "label": "叠加法", "source_refs": ["1"]}]},
        {"label": "磁场计算", "kcs": [{"id": "m2", "label": "叠加法", "source_refs": ["4"], "deps": ["m1"]}]},
    ]}
    notes, warnings = ensure_note_anchors(
        '<h3>叠加法<span data-slides="4"></span></h3><p>有原页。</p><h3>叠加法</h3><p>无原页。</p>',
        skeleton,
    )
    assert notes.count("data-kc=") == 1 and 'data-kc="m2"' in notes
    assert any("未匹配小节" in warning for warning in warnings)
    root1 = {"label": "磁场计算", "children": [{"id": "n1", "kc_id": "m1"}]}
    root2 = {"label": "磁场计算", "children": [{"id": "n2", "kc_id": "m2"}, {"id": "n3", "kc_id": "m1"}]}
    index, warnings = build_kc_index(skeleton, notes, {"nodes": [root1, root2]}, [], 1, 4)
    assert index["m1"].graph_node_id == "n1" and index["m1"].dependents == ["m2"]
    assert index["m2"].note_anchor == "m2"
    assert any("根节点" in warning for warning in warnings)
    assert any("2 个节点" in warning for warning in warnings)


def test_v11_to_v12_migration_backfills_conversation_materials_and_output_statuses(
    tmp_path: Path,
) -> None:
    db = tmp_path / "study.sqlite3"
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        schema._create_v11(conn)
        conn.execute(
            "INSERT INTO users (id, username, username_normalized, password_hash, created_at) "
            "VALUES ('u1', 'alice', 'alice', 'h', '2026-11-18T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO conversations (id, owner_id, title, created_at, updated_at) "
            "VALUES ('c-dedicated', 'u1', '资料学习', '2026-11-18T00:00:00Z', '2026-11-18T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO personal_materials "
            "(id, owner_id, display_name, stored_name, file_sha256, size_bytes, page_count, dedicated_conversation_id, status, created_at, updated_at) "
            "VALUES ('m1', 'u1', 'old.pdf', 'u1/m1.pdf', ?, 1024, 5, 'c-dedicated', 'active', '2026-11-18T00:00:00Z', '2026-11-18T00:00:00Z')",
            ("a" * 64,),
        )
        conn.execute(
            """
            INSERT INTO material_artifacts (
                id, material_id, owner_id, file_sha256, start_page, end_page, rule_version,
                course_hint, title, is_sample, status, current_step, generation_token,
                skeleton_json, notes_html, graph_tree_json, coverage_warnings_json,
                conversation_id, error_code, error_message, created_at, updated_at
            ) VALUES (
                'a1', 'm1', 'u1', ?, 1, 5, 'exampass-v1',
                '', '旧版产物', 0, 'ready', 'completed', NULL,
                '{}', '<p>notes</p>', '{"nodes":[]}', '[]',
                'c-dedicated', NULL, NULL, '2026-11-18T00:00:00Z', '2026-11-18T00:00:00Z'
            )
            """,
            ("a" * 64,),
        )
        conn.execute(
            """
            INSERT INTO material_quiz_questions (
                id, artifact_id, owner_id, sort_order, kc_id, q_type, stem,
                options_json, answer_json, explanation, pitfall, cognitive, difficulty,
                points, source_pages_json, created_at
            ) VALUES (
                'q1', 'a1', 'u1', 1, 'kc1', 'tf', '高斯定理成立。',
                NULL, 'true', '解析', '', 'remember', 'basic', 5.0, '[1]', '2026-11-18T00:00:00Z'
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    report = migrate_database(db, backup_dir=tmp_path / "backups")
    assert report.version == schema.SCHEMA_VERSION

    verify = sqlite3.connect(db)
    try:
        link = verify.execute(
            "SELECT conversation_id, material_id FROM conversation_materials WHERE conversation_id = 'c-dedicated'"
        ).fetchone()
        assert link == ("c-dedicated", "m1")
        row = verify.execute(
            "SELECT notes_status, quiz_status, graph_status, requested_outputs_json FROM material_artifacts WHERE id = 'a1'"
        ).fetchone()
        assert row[:3] == ("ready", "ready", "ready")
        assert json.loads(row[3]) == ["notes", "quiz", "graph"]
    finally:
        verify.close()


def test_session_upload_zero_model_call_and_scanned_pdf_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c_a, c_b, _, _ = _setup_clients(tmp_path, monkeypatch)
    headers = TEST_CSRF_HEADERS
    try:
        # 禁止任何模型调用，验证会话内上传 PDF 零模型消耗
        def _forbid_http(*args, **kwargs):
            raise AssertionError("上传阶段不应触发模型客户端")

        monkeypatch.setattr(app.state, "model_http_client", None, raising=False)
        conv = c_a.post("/api/conversations", json={}, headers=headers).json()
        cid = conv["id"]

        # 1. 可读 PDF 上传到当前会话
        readable_pdf = _make_simple_pdf(
            [
                "Chapter 1 Electrostatics: Gauss law relates flux to enclosed electric charge inside a surface.",
                "Section 1.2 Electric potential is the line integral of electric field intensity across space.",
            ]
        )
        up_ok = c_a.post(
            f"/api/conversations/{cid}/materials/upload",
            files={"file": ("physics.pdf", readable_pdf, "application/pdf")},
            headers=headers,
        )
        assert up_ok.status_code == 201, up_ok.text
        payload_ok = up_ok.json()
        assert payload_ok["conversation_id"] == cid
        assert payload_ok["item"]["material"]["display_name"] == "physics.pdf"
        assert payload_ok["item"]["material"]["page_count"] == 2
        assert payload_ok["item"]["readability"] is None
        assert payload_ok["turn_request_id"] is not None

        # 2. 扫描件 / 空白 PDF 上传：不阻塞上传、保留原件预览，按需生成时才解析并阻止
        scanned_pdf = _make_simple_pdf(["", "   "])
        up_scan = c_a.post(
            f"/api/conversations/{cid}/materials/upload",
            files={"file": ("scan.pdf", scanned_pdf, "application/pdf")},
            headers=headers,
        )
        assert up_scan.status_code == 201, up_scan.text
        scan_payload = up_scan.json()
        scan_mid = scan_payload["item"]["material"]["id"]
        assert scan_payload["item"]["material"]["page_count"] == 2
        assert scan_payload["item"]["readability"] is None

        # 原件预览依然可用
        preview = c_a.get(f"/api/materials/pdfs/{scan_mid}/file")
        assert preview.status_code == 200
        assert preview.headers["content-type"] == "application/pdf"

        # 试图对扫描件按需生成会被拦截
        gen_scan = c_a.post(
            f"/api/conversations/{cid}/materials/{scan_mid}/generate",
            json={"outputs": ["notes"]},
            headers=headers,
        )
        assert gen_scan.status_code == 409
        assert gen_scan.json()["detail"]["missing_pages"] == [1, 2]
        assert gen_scan.json()["detail"]["can_generate_partial"] is False

        # 混合文档先检查扫描页，后续仍然可以独立检查有文字的页段。
        mixed_pdf = _make_simple_pdf(["", "Electrostatics " * 20])
        mixed = c_a.post(
            f"/api/conversations/{cid}/materials/upload",
            files={"file": ("mixed.pdf", mixed_pdf, "application/pdf")},
            headers=headers,
        )
        assert mixed.status_code == 201, mixed.text
        mixed_mid = mixed.json()["item"]["material"]["id"]
        first = c_a.post(
            f"/api/materials/pdfs/{mixed_mid}/readability",
            json={"start_page": 1, "end_page": 1}, headers=headers,
        )
        assert first.status_code == 200 and not first.json()["sufficient"]
        materials = c_a.get(f"/api/conversations/{cid}/materials").json()
        overview = next(x["readability"] for x in materials["items"] if x["material"]["id"] == mixed_mid)
        assert overview["checked_pages"] == [1]
        assert "已检查 1/2 页" in overview["summary"]
        second = c_a.post(
            f"/api/materials/pdfs/{mixed_mid}/readability",
            json={"start_page": 2, "end_page": 2}, headers=headers,
        )
        assert second.status_code == 200 and second.json()["sufficient"], second.text

        # 3. 账号 B 对账号 A 的会话与 PDF 完全隔离（404）
        assert c_b.get(f"/api/conversations/{cid}/materials").status_code == 404
        assert c_b.post(
            f"/api/conversations/{cid}/materials/upload",
            files={"file": ("x.pdf", readable_pdf, "application/pdf")},
            headers=headers,
        ).status_code == 404
        assert c_b.get(f"/api/materials/pdfs/{scan_mid}/file").status_code == 404
    finally:
        c_a.__exit__(None, None, None)
        c_b.__exit__(None, None, None)


def test_on_demand_generation_partial_failure_retry_and_in_session_study(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://mock.deepseek.local")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")

    c_a, c_b, _, _ = _setup_clients(tmp_path, monkeypatch)
    headers = TEST_CSRF_HEADERS
    prev_http = getattr(app.state, "model_http_client", None)
    try:
        conv = c_a.post("/api/conversations", json={}, headers=headers).json()
        cid = conv["id"]

        pdf_bytes = _make_simple_pdf(
            [
                "Gauss theorem in electrostatics explains closed surface flux equals net enclosed charge divided by epsilon zero.",
                "Electrostatic potential energy and electric field lines remain conservative in static conditions.",
            ]
        )
        up = c_a.post(
            f"/api/conversations/{cid}/materials/upload",
            files={"file": ("electrostatics.pdf", pdf_bytes, "application/pdf")},
            headers=headers,
        )
        mid = up.json()["item"]["material"]["id"]

        # 1. 只生成知识图谱：仅调 skeleton，跳过 notes / quiz / review
        calls: list[str] = []
        app.state.model_http_client = httpx.AsyncClient(
            transport=_mock_exampass_transport(calls)
        )
        res_graph = c_a.post(
            f"/api/conversations/{cid}/materials/{mid}/generate",
            json={"outputs": ["graph"], "start_page": 1, "end_page": 2},
            headers=headers,
        )
        assert res_graph.status_code == 200, res_graph.text
        art_graph = res_graph.json()["artifact"]
        assert calls == ["skeleton"]
        assert art_graph["graph_status"] == "ready"
        assert art_graph["notes_status"] == "none"
        assert art_graph["quiz_status"] == "none"
        assert art_graph["status"] == "ready"
        assert art_graph["graph_tree"]["nodes"]

        # 2. 笔记审校失败时草稿不发布，已生成的图谱仍可打开。
        app.state.model_http_client = httpx.AsyncClient(
            transport=_mock_exampass_transport(calls, fail_step="notes_review")
        )
        notes_failure = c_a.post(
            f"/api/conversations/{cid}/materials/{mid}/generate",
            json={"outputs": ["notes"], "start_page": 1, "end_page": 2},
            headers=headers,
        )
        assert notes_failure.status_code == 200, notes_failure.text
        partial = notes_failure.json()["artifact"]
        assert partial["id"] == art_graph["id"]
        assert partial["notes_status"] == "failed"
        assert not partial["notes_html"]
        assert partial["graph_status"] == "ready"
        assert calls == ["skeleton", "notes", "notes_review"]
        app.state.model_http_client = httpx.AsyncClient(
            transport=_mock_exampass_transport(calls)
        )
        res_notes = c_a.post(
            f"/api/conversations/{cid}/materials/{mid}/generate",
            json={"outputs": ["notes"], "start_page": 1, "end_page": 2},
            headers=headers,
        )
        assert res_notes.status_code == 200, res_notes.text
        art_notes = res_notes.json()["artifact"]
        assert art_notes["id"] == art_graph["id"]
        assert calls == ["skeleton", "notes", "notes_review", "notes", "notes_review"]
        assert "笔记审校发布" in art_notes["notes_html"]
        assert art_notes["notes_status"] == "ready"
        assert art_notes["graph_status"] == "ready"
        assert art_notes["quiz_status"] == "none"

        # 3. 追加章节测试并在 quiz 步骤失败：已完成笔记与图谱仍保持 ready，可打开
        app.state.model_http_client = httpx.AsyncClient(
            transport=_mock_exampass_transport(calls, fail_step="quiz")
        )
        res_quiz_fail = c_a.post(
            f"/api/conversations/{cid}/materials/{mid}/generate",
            json={"outputs": ["quiz"], "start_page": 1, "end_page": 2},
            headers=headers,
        )
        assert res_quiz_fail.status_code == 200
        art_fail = res_quiz_fail.json()["artifact"]
        assert art_fail["notes_status"] == "ready"
        assert art_fail["graph_status"] == "ready"
        assert art_fail["quiz_status"] == "failed"
        assert art_fail["notes_html"]  # 已生成笔记仍可查看

        # 4. 单步重试测试：只运行 quiz + review，不重跑 skeleton 和 notes
        app.state.model_http_client = httpx.AsyncClient(
            transport=_mock_exampass_transport(calls)
        )
        res_quiz_ok = c_a.post(
            f"/api/conversations/{cid}/materials/{mid}/generate",
            json={"outputs": ["quiz"], "start_page": 1, "end_page": 2},
            headers=headers,
        )
        assert res_quiz_ok.status_code == 200
        art_full = res_quiz_ok.json()["artifact"]
        assert calls == [
            "skeleton", "notes", "notes_review", "notes", "notes_review",
            "quiz", "quiz", "review",
        ]
        assert art_full["notes_status"] == "ready"
        assert art_full["quiz_status"] == "ready"
        assert art_full["graph_status"] == "ready"
        assert len(art_full["questions"]) == 2
        aid = art_full["id"]
        index = art_full["kc_index"]
        gauss = index["kc1.1"]
        assert gauss["note_anchor"] == "kc1.1" and gauss["dependents"] == ["kc1.2"]
        assert index["kc1.2"]["deps"] == ["kc1.1"]
        assert gauss["question_ids"] == [art_full["questions"][0]["id"]]
        assert gauss["pages"] == [1] and gauss["graph_node_id"]
        assert any("未匹配小节「附加讲解」" in warning for warning in art_full["coverage_warnings"])
        with sqlite3.connect(tmp_path / "study.sqlite3") as db:
            owner_id = db.execute("SELECT owner_id FROM conversations WHERE id=?", (cid,)).fetchone()[0]
            saved_html = db.execute("SELECT notes_html FROM material_artifacts WHERE id=?", (aid,)).fetchone()[0]
            assert '<h3 data-kc="kc1.1">' in saved_html and "保留未匹配的正文" in saved_html
            db.execute(
                """INSERT INTO material_graph_notes (id, owner_id, material_id, artifact_id, node_id, note_text, updated_at)
                   VALUES ('legacy-note', ?, ?, ?, ?, '旧批注', '2026-11-20T00:00:00+00:00')""",
                (owner_id, mid, aid, gauss["graph_node_id"]),
            )
        url = f"/api/materials/artifacts/{aid}"
        assert c_a.get(url).json()["kc_notes"]["kc1.1"] == "旧批注"
        assert c_b.put(url + "/kc-notes/kc1.1", json={"note_text": "越权"}, headers=headers).status_code == 404
        assert c_a.put(url + "/kc-notes/unknown", json={"note_text": "不存在"}, headers=headers).status_code == 404
        assert c_a.put(url + "/kc-notes/kc1.1", json={"note_text": "共享批注"}, headers=headers).status_code == 200
        assert c_a.get(url).json()["graph_notes"][gauss["graph_node_id"]] == "共享批注"
        assert c_a.put(url + f"/graph-notes/{gauss['graph_node_id']}", json={"note_text": ""}, headers=headers).status_code == 200
        assert c_a.get(url).json()["kc_notes"]["kc1.1"] == ""
        assert gauss["graph_node_id"] not in c_a.get(url).json()["graph_notes"]
        assert calls == ["skeleton", "notes", "notes_review", "notes", "notes_review", "quiz", "quiz", "review"]

        # 5. 刷新会话恢复资料卡与轮次卡片
        refreshed = c_a.get(f"/api/conversations/{cid}")
        assert refreshed.status_code == 200
        detail = refreshed.json()
        notes_turn = next(
            turn for turn in detail["turns"] if turn["request_id"] == res_notes.json()["turn_request_id"]
        )
        assert "可在复习工作台查看" in notes_turn["assistant_message"]
        assert "点成果卡" not in notes_turn["assistant_message"]
        assert len(detail["materials"]) == 1
        mat_entry = detail["materials"][0]
        assert mat_entry["artifacts"][0]["notes_status"] == "ready"
        assert mat_entry["artifacts"][0]["quiz_status"] == "ready"
        assert mat_entry["artifacts"][0]["graph_status"] == "ready"

        # 6. 从当前普通会话内测试题 / 知识点发起追问，留在原会话且不新建独立会话
        qid = art_full["questions"][0]["id"]
        study_q = c_a.post(
            f"/api/materials/artifacts/{art_full['id']}/questions/{qid}/study",
            json={"mode": "explain", "conversation_id": cid},
            headers=headers,
        )
        assert study_q.status_code == 200
        assert study_q.json()["conversation_id"] == cid

        study_kc = c_a.post(
            f"/api/materials/artifacts/{art_full['id']}/kcs/kc1.1/study",
            json={"mode": "exercise", "conversation_id": cid},
            headers=headers,
        )
        assert study_kc.status_code == 200
        assert study_kc.json()["conversation_id"] == cid

        # 验证当前会话提交追问能带原题、绑定同资料，即使调度器误调 explain_problem(mode="new")
        # 也不会覆盖原题干或触发“未提供具体题目内容”澄清，同时不泄露未解锁答案
        class _StudyReply:
            async def stream(self, material):
                assert any("闭合高斯面内净电荷为零时" in qm for qm in material.question_materials)
                assert material.answer_scope == "hint"
                assert "高斯定理" in material.workbench_context and "原页：1" in material.workbench_context
                yield "这道题考察闭合曲面通量定义，可以从高斯定理右端的净电荷思考。"

        scheduler = FakeScheduler(
            decisions=[
                ScheduleDecision(
                    actions=[
                        ActionPlan(
                            action="explain_problem",
                            parameters={"mode": "new", "target": "new"},
                        )
                    ]
                )
            ]
        )
        monkeypatch.setattr("app.api.conversations.SchedulerAgent", lambda *args, **kwargs: scheduler)
        monkeypatch.setattr("app.api.conversations.StreamingReply", lambda *args, **kwargs: _StudyReply())

        turn_resp = c_a.post(
            f"/api/conversations/{cid}/turns",
            json={
                "request_id": "11111111-1111-4111-8111-111111111111",
                "query": study_q.json()["prompt"],
                "kind": "auto",
                "material_artifact_id": art_full["id"],
                "material_question_id": qid,
                "material_kc_id": "kc1.1",
                "material_workbench_view": "quiz",
                "material_source_pages": [1, 9999],
                "material_study_mode": "explain",
            },
            headers=headers,
        )
        assert turn_resp.status_code == 200
        events = [
            json.loads(line.removeprefix("data: "))
            for line in turn_resp.text.splitlines()
            if line.startswith("data: ")
        ]
        result_ev = events[-1]
        assert result_ev["type"] == "result"
        assert result_ev["business_status"] == "succeeded"
        assert "高斯定理右端的净电荷" in result_ev["assistant_message"]
        assert any(event["type"] == "message_delta" for event in events)
        study_turn = c_a.get(f"/api/conversations/{cid}").json()["turns"][-1]
        assert study_turn["workbench_source"] == {
            "artifact_id": aid, "kc_id": "kc1.1", "view": "quiz",
            "question_id": qid, "label": "高斯定理", "pages": [1],
        }
        bad_target = {
            "request_id": "11111111-1111-4111-8111-111111111112", "query": "读这一题", "kind": "auto",
            "material_artifact_id": aid, "material_question_id": qid,
            "material_kc_id": "kc1.2", "material_workbench_view": "quiz",
        }
        assert c_a.post(f"/api/conversations/{cid}/turns", json=bad_target, headers=headers).status_code == 422
        other_cid = c_a.post("/api/conversations", json={}, headers=headers).json()["id"]
        assert c_a.post(f"/api/conversations/{other_cid}/turns", json=bad_target, headers=headers).status_code == 422
        # 确认资料与原题绑定未换题、未变成 unclear
        from app.conversations.repository import ConversationRepository

        with sqlite3.connect(tmp_path / "study.sqlite3") as db:
            owner_id = db.execute("SELECT owner_id FROM conversations WHERE id = ?", (cid,)).fetchone()[0]
        active_problem = ConversationRepository(tmp_path / "study.sqlite3", owner_id=owner_id).get_active_problem(cid)
        assert active_problem is not None
        assert active_problem.clarity == "clear"
        assert "闭合高斯面内净电荷为零时" in active_problem.statement_text
        assert active_problem.explanation is None
        assert qid in (active_problem.source.text or "")

        # 账号 B 不能借用账号 A 的 artifact_id 发起追问
        assert (
            c_b.post(
                f"/api/materials/artifacts/{art_full['id']}/questions/{qid}/study",
                json={"mode": "explain", "conversation_id": cid},
                headers=headers,
            ).status_code
            == 404
        )
    finally:
        app.state.model_http_client = prev_http
        c_a.__exit__(None, None, None)
        c_b.__exit__(None, None, None)


def test_multi_pdf_clarification_explicit_target_and_deletion_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://mock.deepseek.local")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")

    c_a, c_b, _, _ = _setup_clients(tmp_path, monkeypatch)
    headers = TEST_CSRF_HEADERS
    prev_http = getattr(app.state, "model_http_client", None)
    try:
        conv1 = c_a.post("/api/conversations", json={}, headers=headers).json()
        cid1 = conv1["id"]

        pdf_phys = _make_simple_pdf([
            "Electrostatics Gauss Law page one content explaining electric flux through closed surfaces and conservative fields in sufficient detail for text analysis.",
            "Electrostatics page two electric potential difference and equipotential surfaces in uniform fields.",
        ])
        pdf_calc = _make_simple_pdf([
            "Calculus Taylor Series expansion around zero with Lagrange remainder formula and convergence radius analysis for exams.",
            "Calculus page two integration by parts and substitution methods for rational and trigonometric functions.",
        ])

        up1 = c_a.post(
            f"/api/conversations/{cid1}/materials/upload",
            files={"file": ("chapter.pdf", pdf_phys, "application/pdf")},
            headers=headers,
        ).json()
        up2 = c_a.post(
            f"/api/conversations/{cid1}/materials/upload",
            files={"file": ("chapter.pdf", pdf_calc, "application/pdf")},
            headers=headers,
        ).json()
        mid1 = up1["item"]["material"]["id"]
        mid2 = up2["item"]["material"]["id"]

        calls: list[str] = []
        app.state.model_http_client = httpx.AsyncClient(
            transport=_mock_exampass_transport(calls)
        )

        # 1. 通过自然语言工具编排：list_conversation_materials -> generate_material_artifact
        # 未指定哪份资料且当前会话有 2 份资料时返回澄清，要求学生选定而不串文件
        class _StaticReply:
            def __init__(self, msg: str) -> None:
                self._msg = msg

            async def stream(self, material):
                yield self._msg

        # 1a. 指代不明（不传 material_id）-> clarification
        for index, parameters in enumerate((
            {"outputs": ["notes"]},
            {"material_id": "chapter.pdf", "outputs": ["notes"]},
        )):
            ambig_sched = FakeScheduler(
                decisions=[
                    ScheduleDecision(
                        actions=[ActionPlan(action="generate_material_artifact", parameters=parameters)],
                    )
                ]
            )
            monkeypatch.setattr("app.api.conversations.SchedulerAgent", lambda *args, **kwargs: ambig_sched)
            monkeypatch.setattr("app.api.conversations.StreamingReply", lambda *args, **kwargs: _StaticReply("请选择你想生成笔记的资料。"))
            resp_ambig = c_a.post(
                f"/api/conversations/{cid1}/turns",
                json={
                    "request_id": f"22222222-2222-4222-8222-{index:012d}",
                    "query": "帮我生成章节笔记",
                    "kind": "auto",
                },
                headers=headers,
            )
            assert resp_ambig.status_code == 200
            events = [
                json.loads(line.removeprefix("data: "))
                for line in resp_ambig.text.splitlines()
                if line.startswith("data: ")
            ]
            res_ambig = events[-1]
            assert res_ambig["type"] == "result"
            clarification = next(
                row for row in res_ambig["composite_results"]
                if row["action"] == "generate_material_artifact"
            )
            assert clarification["status"] == "clarification"
            if "material_id" in parameters:
                assert {item["material_id"] for item in clarification["details"]["materials"]} == {mid1, mid2}
            assert calls == []

        # 1b. 指代明确（指定资料 ID mid2）-> 准确生成且只运行骨架和笔记流程
        explicit_sched = FakeScheduler(
            decisions=[
                ScheduleDecision(
                    actions=[
                        ActionPlan(action="list_conversation_materials", parameters={}),
                        ActionPlan(action="generate_material_artifact", parameters={"material_id": mid2, "outputs": ["notes"]}),
                    ]
                )
            ]
        )
        monkeypatch.setattr("app.api.conversations.SchedulerAgent", lambda *args, **kwargs: explicit_sched)
        monkeypatch.setattr("app.api.conversations.StreamingReply", lambda *args, **kwargs: _StaticReply("已为你生成《chapter.pdf》的章节笔记。"))
        resp_ok = c_a.post(
            f"/api/conversations/{cid1}/turns",
            json={
                "request_id": "33333333-3333-4333-8333-333333333333",
                "query": "用高数那份 chapter.pdf 生成章节笔记",
                "kind": "auto",
            },
            headers=headers,
        )
        assert resp_ok.status_code == 200
        events_ok = [
            json.loads(line.removeprefix("data: "))
            for line in resp_ok.text.splitlines()
            if line.startswith("data: ")
        ]
        res_ok = events_ok[-1]
        gen_row = next(
            r for r in res_ok["composite_results"] if r.get("action") == "generate_material_artifact"
        )
        assert gen_row["status"] == "success"
        assert gen_row["details"]["material_id"] == mid2
        assert calls == ["skeleton", "notes", "notes_review"]

        # 2. 移除当前会话关联（不删原 PDF 与成果，也不删普通会话消息）
        det = c_a.delete(f"/api/conversations/{cid1}/materials/{mid1}", headers=headers)
        assert det.status_code == 200
        assert det.json()["detached"] is True
        mats_after = c_a.get(f"/api/conversations/{cid1}/materials").json()
        assert [m["material"]["id"] for m in mats_after["items"]] == [mid2]
        assert c_a.get(f"/api/materials/pdfs/{mid1}").status_code == 200

        # 3. 资料库可找回历史 PDF 与产物，并挂载到新会话
        lib = c_a.get("/api/materials/library").json()
        lib_ids = {it["material"]["id"] for it in lib["items"]}
        assert {mid1, mid2}.issubset(lib_ids)
        assert c_b.get("/api/materials/library").json()["items"] == []

        conv2 = c_a.post("/api/conversations", json={}, headers=headers).json()
        cid2 = conv2["id"]
        att = c_a.post(f"/api/conversations/{cid2}/materials/{mid2}/attach", headers=headers)
        assert att.status_code == 200
        assert len(att.json()["artifacts"]) == 1

        # 4. 永久删除原 PDF 前展示关联会话数；删除后保留普通会话及其无关消息
        impact_pdf = c_a.get(f"/api/materials/pdfs/{mid2}/delete-impact").json()
        assert impact_pdf["artifact_count"] == 1
        assert impact_pdf["linked_conversation_count"] == 2
        assert impact_pdf["preserves_regular_conversations"] is True

        del_pdf = c_a.delete(f"/api/materials/pdfs/{mid2}", headers=headers)
        assert del_pdf.status_code == 200
        assert del_pdf.json()["deleted"] is True
        # 两个普通会话依然存活，且原有普通轮次仍可读取
        conv1_after = c_a.get(f"/api/conversations/{cid1}")
        assert conv1_after.status_code == 200
        assert len(conv1_after.json()["turns"]) >= 2
        assert c_a.get(f"/api/conversations/{cid2}").status_code == 200

        # 5. 删除会话影响预览与删除会话后保留原 PDF（mid1 关联后删除会话）
        c_a.post(f"/api/conversations/{cid2}/materials/{mid1}/attach", headers=headers)
        impact_conv = c_a.get(f"/api/conversations/{cid2}/delete-impact").json()
        assert impact_conv["linked_material_count"] == 1
        assert impact_conv["materials_still_accessible"] is True
        assert c_b.get(f"/api/conversations/{cid2}/delete-impact").status_code == 404

        del_conv = c_a.delete(f"/api/conversations/{cid2}", headers=headers)
        assert del_conv.status_code == 200
        assert del_conv.json()["deleted"] is True
        assert c_a.get(f"/api/conversations/{cid2}").status_code == 404
        # mid1 原 PDF 仍在个人资料库完整保留
        assert c_a.get(f"/api/materials/pdfs/{mid1}").status_code == 200
    finally:
        app.state.model_http_client = prev_http
        c_a.__exit__(None, None, None)
        c_b.__exit__(None, None, None)


def test_scan_ocr_partial_retry_isolation_and_generation_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.quota.transport import QuotaTransport

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://mock.deepseek.local")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-flash")
    alice, bob, _, _ = _setup_clients(tmp_path, monkeypatch)
    headers = TEST_CSRF_HEADERS
    previous = app.state.model_http_client
    ocr_calls: list[int] = []
    generation_calls: list[str] = []
    gen = _mock_exampass_transport(generation_calls)

    def reply(request: httpx.Request) -> httpx.Response:
        messages = json.loads(request.content)["messages"]
        if isinstance(messages[1]["content"], str):
            return gen.handle_request(request)
        page_input, image_input = messages[1]["content"]
        assert "JSON" in messages[0]["content"]
        assert "JSON" in page_input["text"]
        page_no = int(page_input["text"].split()[-2])
        assert image_input["image_url"]["url"].startswith("data:image/jpeg;base64,")
        ocr_calls.append(page_no)
        if ocr_calls == [1, 2]:
            return httpx.Response(500, json={"error": {"message": "mock page failure"}})
        transcript = f"Scanned exam question from physical page {page_no}. Calculate the conditional probability P(A|B) and explain the sample space."
        return httpx.Response(200, json={
            "id": "ocr-response",
            "object": "chat.completion",
            "created": 1700000000,
            "model": "deepseek-flash",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": json.dumps({"text": transcript})},
                "finish_reason": "stop",
            }],
        })

    client = httpx.AsyncClient(transport=QuotaTransport(app.state.usage_gate, httpx.MockTransport(reply)))
    try:
        app.state.model_http_client = client
        cid = alice.post("/api/conversations", json={}, headers=headers).json()["id"]
        pdf = _make_simple_pdf([
            "", " ",
            "The sample space is finite. All outcomes are equally likely in this experiment. Determine the conditional probability of two events with a shared sample space. State your assumptions.",
        ])
        upload = alice.post(
            f"/api/conversations/{cid}/materials/upload",
            files={"file": ("mixed.pdf", pdf, "application/pdf")},
            headers=headers,
        )
        assert upload.status_code == 201, upload.text
        material = upload.json()["item"]["material"]
        mid = material["id"]
        ocr_url = f"/api/materials/pdfs/{mid}/ocr"
        generate_url = f"/api/conversations/{cid}/materials/{mid}/generate"
        graph_payload = {"start_page": 1, "end_page": 3, "outputs": ["graph"]}
        used = lambda: alice.get("/api/usage/today").json()["requests"]["used"]
        assert used() == 0
        assert bob.post(ocr_url, json={"pages": [1]}, headers=headers).status_code == 404
        assert alice.post(ocr_url, json={"pages": [4]}, headers=headers).status_code == 422
        assert alice.post(ocr_url, json={"pages": [1] * 9}, headers=headers).status_code == 422
        assert used() == 0

        blocked = alice.post(generate_url, json=graph_payload, headers=headers)
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["missing_pages"] == [1, 2]
        assert used() == 0
        old = alice.post(generate_url, json={**graph_payload, "coverage_mode": "partial"}, headers=headers)
        assert old.status_code == 200, old.text
        old_artifact = old.json()["artifact"]
        assert generation_calls == ["skeleton"]

        first = alice.post(ocr_url, json={"pages": [1, 2, 3]}, headers=headers).json()
        assert first["recognized_pages"] == [1]
        assert first["skipped_pages"] == [3]
        assert [row["page_no"] for row in first["failures"]] == [2]
        assert first["readability"]["ocr_pages"] == [1]
        assert first["readability"]["scanned_pages"] == [2]
        assert first["readability"]["pages"][1]["status"] == "ocr_failed"
        assert ocr_calls == [1, 2]
        assert used() == 3

        # A byte-identical upload can reuse only this account's completed visual checks.
        for account, expect_reuse in ((alice, True), (bob, False)):
            new_cid = account.post("/api/conversations", json={}, headers=headers).json()["id"]
            duplicate = account.post(
                f"/api/conversations/{new_cid}/materials/upload",
                files={"file": ("same-content-different-name.pdf", pdf, "application/pdf")},
                headers=headers,
            )
            assert duplicate.status_code == 201, duplicate.text
            duplicate_mid = duplicate.json()["item"]["material"]["id"]
            checked = account.post(
                f"/api/materials/pdfs/{duplicate_mid}/readability",
                json={"start_page": 1, "end_page": 3}, headers=headers,
            )
            assert checked.status_code == 200, checked.text
            states = checked.json()["pages"]
            assert states[0]["source"] == ("ocr" if expect_reuse else "pdf_text")
            assert states[1]["status"] == "pending_ocr"
            assert states[2]["source"] == "pdf_text"
            if expect_reuse:
                hit = account.post(
                    f"/api/materials/pdfs/{duplicate_mid}/ocr",
                    json={"pages": [1]}, headers=headers,
                )
                assert hit.json()["reused_pages"] == [1]
        assert ocr_calls == [1, 2] and used() == 3

        retry = alice.post(ocr_url, json={"pages": [1, 2]}, headers=headers).json()
        assert retry["recognized_pages"] == [2]
        assert retry["reused_pages"] == [1]
        assert retry["readability"]["scanned_pages"] == []
        assert used() == 4
        repeat = alice.post(ocr_url, json={"pages": [1, 2]}, headers=headers).json()
        assert repeat["reused_pages"] == [1, 2]
        assert ocr_calls == [1, 2, 2]
        assert used() == 4

        for page_no in (1, 2):
            page = alice.get(f"/api/materials/pdfs/{mid}/pages/{page_no}").json()
            assert page["source"] == "ocr"
            assert "P(A|B)" in page["extracted_text"]
        assert alice.get(f"/api/materials/pdfs/{mid}/pages/3").json()["source"] == "pdf_text"
        assert alice.get(f"/api/materials/pdfs/{mid}/file").content == pdf

        new = alice.post(generate_url, json=graph_payload, headers=headers)
        assert new.status_code == 200, new.text
        artifact = new.json()["artifact"]
        assert artifact["graph_status"] == "ready"
        assert artifact["id"] != old_artifact["id"]
        assert artifact["coverage"]["included_pages"] == [1, 2, 3]
        assert artifact["coverage"]["missing_pages"] == []
        assert [page["version"] for page in artifact["coverage"]["input_pages"][:2]] == ["vision-ocr-v1"] * 2
        assert old_artifact["coverage"]["included_pages"] == [3]
        assert old_artifact["coverage"]["missing_pages"] == [1, 2]
        historical = alice.get(f"/api/materials/artifacts/{old_artifact['id']}").json()
        assert historical["coverage"] == old_artifact["coverage"]
        assert generation_calls == ["skeleton", "skeleton"]
        assert used() == 5
        again = alice.post(generate_url, json=graph_payload, headers=headers).json()["artifact"]
        assert again["id"] == artifact["id"]
        assert used() == 5
    finally:
        alice.portal.call(client.aclose)
        app.state.model_http_client = previous
        alice.__exit__(None, None, None)
        bob.__exit__(None, None, None)


def test_uploaded_exam_asks_scope_and_solves_original_questions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.quota.transport import QuotaTransport

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://mock.deepseek.local")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-flash")
    alice, bob, _, _ = _setup_clients(tmp_path, monkeypatch)
    previous = app.state.model_http_client
    calls: list[str] = []
    first = {
        "reference": "第8题",
        "source_pages": [1],
        "statement": "已知联合分布表，求 α 和 β。",
        "solution": "用独立性求得 β=1/8，进而 α=3/8。",
    }
    second = {
        "reference": "第五大题第4小问",
        "source_pages": [2],
        "statement": "求 X 在已知 Y 时的条件密度。",
        "solution": "在边缘密度为正时，对联合密度除以 Y 的边缘密度。",
    }

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        content = body["messages"][1]["content"]
        query = content[0]["text"]
        assert sum(part["type"] == "image_url" for part in content) == 4
        calls.append(query)
        if "选择题" in query:
            payload = {"answers": [], "unresolved": [], "no_match": "这份试卷没有选择题。"}
        elif "填空题" in query:
            assert "solution 仅写最终答案" in query
            payload = {"answers": [{**first, "solution": "α=3/8，β=1/8", "verification": "核验用等式"}]}
        elif "带表格" in query:
            payload = {"answers": [first]}
        elif "第99题" in query:
            payload = {"answers": [], "unresolved": ["原卷中找不到第99题。"]}
        elif "核对失败" in query:
            payload = {"answers": [first], "unresolved": ["第8题：条件没有看清。"]}
        else:
            payload = {"answers": [first, second], "unresolved": []}
        return httpx.Response(200, json={
            "id": "exam", "object": "chat.completion", "created": 1700000000, "model": "deepseek-flash",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(payload, ensure_ascii=False),
            }}],
        })

    transport = QuotaTransport(app.state.usage_gate, httpx.MockTransport(answer))
    client = httpx.AsyncClient(transport=transport)
    headers = TEST_CSRF_HEADERS
    try:
        app.state.model_http_client = client
        cid = alice.post("/api/conversations", json={}, headers=headers).json()["id"]
        upload = alice.post(
            f"/api/conversations/{cid}/materials/upload",
            files={"file": ("概率论期中试卷.pdf", _make_simple_pdf(["", " "]), "application/pdf")},
            headers=headers,
        )
        assert upload.status_code == 201, upload.text
        body = upload.json()
        assert body["item"]["material"]["is_exam"] is True
        assert "你想看哪几题，还是整张试卷" in body["assistant_notice"]
        assert "章节测试" not in body["assistant_notice"]
        assert calls == []
        mid = body["item"]["material"]["id"]

        def turn(text: str, index: int, material_id: str | None = None, *, answer_only: bool = False):
            args = {"answer_only": answer_only}
            if material_id is not None:
                args["material_id"] = material_id
            scheduler = FakeScheduler(decisions=[ScheduleDecision(actions=[
                ActionPlan(action="solve_uploaded_exam", parameters=args),
            ])])
            monkeypatch.setattr("app.api.conversations.SchedulerAgent", lambda *args, **kwargs: scheduler)
            response = alice.post(f"/api/conversations/{cid}/turns", json={
                "request_id": f"55555555-5555-4555-8555-{index:012d}", "query": text, "kind": "auto",
            }, headers=headers)
            assert response.status_code == 200, response.text
            events = [json.loads(line.removeprefix("data: ")) for line in response.text.splitlines() if line.startswith("data: ")]
            assert events[-1]["type"] == "result", events[-1]
            assert len(scheduler.calls) == 1
            return events[-1]

        no_choices = turn("给出选择题答案", 1, answer_only=True)
        assert no_choices["business_status"] == "succeeded"
        assert no_choices["assistant_message"] == "这份试卷没有选择题。"
        assert no_choices["composite_results"][0]["details"]["source_pages"] == [1, 2]
        selected = turn("第8题", 2)
        assert "PDF 第 1 页" in selected["assistant_message"]
        assert "β=1/8" in selected["assistant_message"]
        assert "条件密度" not in selected["assistant_message"]
        assert selected["composite_results"][0]["action"] == "solve_uploaded_exam"

        whole = turn("讲整张试卷", 3)
        assert "第8题" in whole["assistant_message"]
        assert "第五大题第4小问" in whole["assistant_message"]
        assert "PDF 第 2 页" in whole["assistant_message"]
        missing = turn("第99题", 4)
        assert "找不到第99题" in missing["assistant_message"]
        assert "**原题**" not in missing["assistant_message"]
        uncertain = turn("第8题，核对失败时标明", 6)
        assert "请对照原页确认题干和公式" in uncertain["assistant_message"]
        assert "**解答**" not in uncertain["assistant_message"]

        blanks = turn("填空题只要答案", 7, answer_only=True)
        assert "α=3/8，β=1/8" in blanks["assistant_message"]
        assert "已知联合分布表" not in blanks["assistant_message"] and "核验用等式" not in blanks["assistant_message"]
        table = turn("讲带表格的那道题", 8)
        assert "第8题" in table["assistant_message"]
        assert "第五大题" not in table["assistant_message"]

        other = alice.post("/api/conversations", json={}, headers=headers).json()["id"]
        unrelated = alice.post(
            f"/api/conversations/{other}/materials/upload",
            files={"file": ("另一份试卷.pdf", _make_simple_pdf([""]), "application/pdf")},
            headers=headers,
        ).json()["item"]["material"]["id"]
        invalid = turn("第8题", 5, unrelated)
        assert "哪一份试卷" in invalid["assistant_message"]
        assert len(calls) == 7
        assert bob.get(f"/api/conversations/{cid}").status_code == 404
        saved = alice.get(f"/api/conversations/{cid}").json()["turns"]
        assert any("β=1/8" in (row.get("assistant_message") or "") for row in saved)
        assert all(mid != row["details"].get("artifact_id") for row in whole["composite_results"])

        # 这份高数卷的两页原先没有触发文字质量告警，仍须把两页原图交给模型。
        real_pdf = Path("data/papers/高数试卷/2024-2025高数AII期末考试试题.pdf")
        assert real_pdf.is_file(), "真实高数 AII 样卷缺失"
        cid = alice.post("/api/conversations", json={}, headers=headers).json()["id"]
        upload_math = alice.post(
            f"/api/conversations/{cid}/materials/upload",
            files={"file": (real_pdf.name, real_pdf.read_bytes(), "application/pdf")},
            headers=headers,
        )
        assert upload_math.status_code == 201, upload_math.text
        math_result = turn("这是真题试卷，给我讲解一下选择题", 9)
        assert math_result["composite_results"][0]["action"] == "solve_uploaded_exam"
        assert len(calls) == 8
    finally:
        alice.portal.call(client.aclose)
        app.state.model_http_client = previous
        alice.__exit__(None, None, None)
        bob.__exit__(None, None, None)


def test_ocr_quota_pause_keeps_results_and_does_not_mark_unattempted_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.quota.transport import QuotaTransport

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://mock.deepseek.local")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-flash")
    monkeypatch.setenv("HHU_MODEL_DAILY_LIMIT", "2")
    alice, bob, _, _ = _setup_clients(tmp_path, monkeypatch)
    previous = app.state.model_http_client
    calls: list[int] = []

    def reply(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        page = int(body["messages"][1]["content"][0]["text"].split()[-2])
        calls.append(page)
        if page == 1:
            return httpx.Response(500, json={"error": {"message": "single page failure"}})
        return httpx.Response(200, json={
            "id": "ocr-response", "object": "chat.completion", "created": 1700000000,
            "model": "deepseek-flash",
            "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": '{"text":"","non_study":"cover"}'},
            }],
        })

    client = httpx.AsyncClient(transport=QuotaTransport(app.state.usage_gate, httpx.MockTransport(reply)))
    try:
        app.state.model_http_client = client
        headers = TEST_CSRF_HEADERS
        cid = alice.post("/api/conversations", json={}, headers=headers).json()["id"]
        response = alice.post(
            f"/api/conversations/{cid}/materials/upload",
            files={"file": ("mixed.pdf", _make_simple_pdf(["", " ", "  "]), "application/pdf")},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        mid = response.json()["item"]["material"]["id"]
        root = f"/api/materials/pdfs/{mid}"
        stopped = alice.post(
            f"/api/conversations/{cid}/materials/{mid}/generate",
            json={"start_page": 1, "end_page": 3, "outputs": ["graph"], "coverage_mode": "recognize"},
            headers=headers,
        )
        assert stopped.status_code == 429, stopped.text
        assert calls == [1, 2]

        report = alice.post(root + "/readability", json={"start_page": 1, "end_page": 3}, headers=headers)
        assert [p["status"] for p in report.json()["pages"]] == ["ocr_failed", "excluded", "pending_ocr"]
        reused = alice.post(root + "/ocr", json={"pages": [2]}, headers=headers)
        assert reused.json()["reused_pages"] == [2]
        assert calls == [1, 2]
    finally:
        app.state.model_http_client = previous
        alice.portal.call(client.aclose)
        alice.__exit__(None, None, None)
        bob.__exit__(None, None, None)



def test_full_coverage_required_from_chat_and_api_until_recognition_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.quota.transport import QuotaTransport

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://mock.deepseek.local")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-flash")
    alice, bob, _, _ = _setup_clients(tmp_path, monkeypatch)
    previous = app.state.model_http_client
    ocr_calls: list[int] = []
    generation: list[str] = []
    recite = _mock_exampass_transport(generation)

    def respond(request: httpx.Request) -> httpx.Response:
        content = json.loads(request.content)["messages"][1]["content"]
        if isinstance(content, str):
            return recite.handle_request(request)
        page = int(content[0]["text"].split()[-2])
        ocr_calls.append(page)
        if page == 2 and ocr_calls.count(2) == 1:
            return httpx.Response(500, json={"error": {"message": "one page failed"}})
        transcript = f"Page {page} of the electrostatics handout describes field strength, voltage, and examples for solving physics questions."
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 1700000000,
            "model": "deepseek-flash", "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps({"text": transcript})},
            }],
        })

    client = httpx.AsyncClient(transport=QuotaTransport(app.state.usage_gate, httpx.MockTransport(respond)))
    try:
        app.state.model_http_client = client
        headers = TEST_CSRF_HEADERS
        cid = alice.post("/api/conversations", json={}, headers=headers).json()["id"]
        source = _make_simple_pdf([""] * 9 + [
            "The last page of the electrostatic physics handout contains detailed discussion of electric potential, uniform electric fields and conservation of energy.",
        ])
        response = alice.post(
            f"/api/conversations/{cid}/materials/upload",
            files={"file": ("lecture.pdf", source, "application/pdf")}, headers=headers,
        )
        assert response.status_code == 201, response.text
        mid = response.json()["item"]["material"]["id"]
        scheduler = FakeScheduler(decisions=[ScheduleDecision(actions=[
            ActionPlan(action="generate_material_artifact", parameters={"outputs": ["graph"]}),
        ])])
        monkeypatch.setattr("app.api.conversations.SchedulerAgent",lambda *args,**kwargs: scheduler)
        chat = alice.post(f"/api/conversations/{cid}/turns", headers=headers, json={
            "request_id": "66666666-6666-4666-8666-000000000001", "query": "根据整份讲义生成图谱", "kind": "auto",
        })
        assert chat.status_code == 200, chat.text
        events = [json.loads(line[6:]) for line in chat.text.splitlines() if line.startswith("data: ")]
        outcome = next(row for row in events[-1]["composite_results"] if row["action"] == "generate_material_artifact")
        assert outcome["status"] == "clarification"
        assert outcome["details"]["code"] == "MATERIAL_COVERAGE_REQUIRED"
        assert outcome["details"]["missing_pages"] == list(range(1, 10))
        assert outcome["details"]["outputs"] == ["graph"] and "artifact_id" not in outcome["details"]
        assert "识别并生成" in events[-1]["assistant_message"]

        normal = f"/api/materials/pdfs/{mid}/artifacts"
        session = f"/api/conversations/{cid}/materials/{mid}/generate"
        body = {"start_page": 1, "end_page": 10, "outputs": ["graph"]}
        assert alice.post(normal, json=body, headers=headers).status_code == 409
        assert alice.post(session, json=body, headers=headers).status_code == 409
        assert ocr_calls == [] and generation == []

        requested = {**body, "coverage_mode": "recognize"}
        blocked = alice.post(session, json=requested, headers=headers)
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["detail"]["missing_pages"] == [2]
        assert ocr_calls == list(range(1, 10)), ocr_calls
        assert generation == []

        complete = alice.post(session, json=requested, headers=headers)
        assert complete.status_code == 200, complete.text
        art = complete.json()["artifact"]
        assert art["graph_status"] == "ready"
        assert art["coverage"]["included_pages"] == list(range(1, 11))
        assert art["coverage"]["missing_pages"] == []
        assert ocr_calls == [*range(1, 10), 2]
        assert generation == ["skeleton", "skeleton"]
        receipt = alice.get(f"/api/conversations/{cid}").json()["turns"][-1]
        assert "10 页正文均已纳入" in receipt["assistant_message"]
        assert "可在复习工作台查看" in receipt["assistant_message"]
    finally:
        app.state.model_http_client = previous
        alice.portal.call(client.aclose)
        alice.__exit__(None, None, None)
        bob.__exit__(None, None, None)
