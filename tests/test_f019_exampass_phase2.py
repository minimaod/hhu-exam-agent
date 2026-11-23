"""F019 第二阶段检查点 2：ExamPass 样例导入、PDF 代理字符修复、原页渲染、答案边界与图谱笔记隔离测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth.dependencies import require_user
from app.main import app
from app.materials import storage as mat_storage
from app.materials.exampass_adapter import grade_objective_question, sanitize_notes_html
from app.materials.pdf_parser import _analyze_page_text
from tests.f012_support import TEST_CSRF_HEADERS, create_invite, register
from tests.test_materials_pdf_parser import _make_simple_pdf


def _setup_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, TestClient, Path]:
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
    register(c_a, username="alice_p2", password="Password123!", invite_code=create_invite(db_path))
    register(c_b, username="bob_p2", password="Password123!", invite_code=create_invite(db_path))
    return c_a, c_b, mat_root


def test_surrogate_characters_sanitized_without_crash() -> None:
    analyzed = _analyze_page_text(5, "电场强度 E = \ud835\udc38 / q")
    assert "\ud835" not in analyzed.extracted_text
    assert analyzed.formula_warning is True
    analyzed.extracted_text.encode("utf-8")


def test_sanitize_notes_html_strips_scripts_and_events() -> None:
    dirty = (
        '<h3 onclick="alert(1)">1.1 库仑定律 <span data-slides="3"></span></h3>'
        "<script>window.hacked=true</script>"
        '<div class="tldr"><strong>结论：</strong>$E = k q / r^2$</div>'
    )
    clean = sanitize_notes_html(dirty)
    assert "<script" not in clean
    assert "onclick" not in clean
    assert "data-slides=\"3\"" in clean
    assert '<div class="tldr">' in clean


def test_objective_grading_rules() -> None:
    status, ok, score, _ = grade_objective_question(
        q_type="tf", stem="高斯定理成立。", options=None, correct_answer=True, user_answer=True, max_points=5
    )
    assert status == "auto_graded" and ok is True and score == 5.0

    status_fill, ok_fill, score_fill, _ = grade_objective_question(
        q_type="fill",
        stem="真空介电常数为 ____。",
        options=None,
        correct_answer="ε0",
        user_answer=["  ε0  "],
        max_points=4,
    )
    assert status_fill == "auto_graded" and ok_fill is True and score_fill == 4.0

    # 数学填空等价形式（如 \lambda/(2\pi\varepsilon_0 a) vs \frac{\lambda}{2\pi\varepsilon_0 a}）不武断判零分
    status_math, ok_math, score_math, _ = grade_objective_question(
        q_type="fill",
        stem="无限长直导线场强为 ____。",
        options=None,
        correct_answer=r"$\frac{\lambda}{2\pi\varepsilon_0 a}$",
        user_answer=[r"\lambda/(2\pi\varepsilon_0 a)"],
        max_points=5,
    )
    assert status_math == "pending_review" and ok_math is None and score_math is None


def test_sample_artifact_workflow_isolation_and_answer_boundary(
    tmp_path: Path, monkeypatch
) -> None:
    sample_dir = (
        Path(__file__).resolve().parents[2]
        / "河海大学考试复习"
        / "tmp"
        / "exampass-physics"
        / ".epa_work"
    )
    if not sample_dir.is_dir():
        return

    c_a, c_b, mat_root = _setup_client(tmp_path, monkeypatch)
    headers = TEST_CSRF_HEADERS

    # 上传无关测试 PDF：不允许绑定静电场样例
    wrong_pdf = _make_simple_pdf(["Unrelated calculus page 1"])
    up_wrong = c_a.post(
        "/api/materials/pdfs",
        files={"file": ("calc.pdf", wrong_pdf, "application/pdf")},
        headers=headers,
    )
    assert up_wrong.status_code == 201
    wrong_mid = up_wrong.json()["id"]
    imp_wrong = c_a.post(
        f"/api/materials/pdfs/{wrong_mid}/artifacts/import-sample",
        json={"sample_name": "exampass-physics"},
        headers=headers,
    )
    assert imp_wrong.status_code == 422

    # 上传 36 页 PDF 并授权其指纹作为样例原件进行隔离与作答边界测试
    pdf_bytes = _make_simple_pdf([f"Page {i} physics lecture content" for i in range(1, 37)])
    up = c_a.post(
        "/api/materials/pdfs",
        files={"file": ("physics.pdf", pdf_bytes, "application/pdf")},
        headers=headers,
    )
    assert up.status_code == 201
    mid = up.json()["id"]
    sha = up.json()["file_sha256"]
    monkeypatch.setattr("app.materials.service.EXAMPASS_PHYSICS_SAMPLE_SHA256", sha)

    # 1. 导入样例产物
    imp = c_a.post(
        f"/api/materials/pdfs/{mid}/artifacts/import-sample",
        json={"sample_name": "exampass-physics"},
        headers=headers,
    )
    assert imp.status_code == 200
    art = imp.json()
    assert art["is_sample"] is True
    assert len(art["questions"]) == 12
    assert "<script" not in art["notes_html"]
    assert "必考" not in art["notes_html"]
    assert art["graph_tree"]["nodes"]

    # 答案边界：未作答且未查看前，不返回答案与解析
    first_q = art["questions"][0]
    assert first_q["revealed_answer"] is None
    assert first_q["revealed_explanation"] is None

    # 2. 原页图片渲染接口受鉴权保护
    img_ok = c_a.get(f"/api/materials/pdfs/{mid}/pages/1/image")
    assert img_ok.status_code == 200
    assert img_ok.headers["content-type"] == "image/jpeg"
    assert c_b.get(f"/api/materials/pdfs/{mid}/pages/1/image").status_code == 404

    # 3. 客观题先答错（0分）解锁答案并记录 revealed_solution_at，再按答案订正（5分）应标为看答案后订正且保留初次 0 分
    sub_wrong = c_a.post(
        f"/api/materials/artifacts/{art['id']}/questions/{first_q['id']}/attempt",
        json={"user_answer": 3},
        headers=headers,
    )
    assert sub_wrong.status_code == 200
    q0_wrong = sub_wrong.json()["questions"][0]
    assert q0_wrong["attempt"]["grading_status"] == "auto_graded"
    assert q0_wrong["attempt"]["is_correct"] is False
    assert q0_wrong["attempt"]["score"] == 0.0
    assert q0_wrong["attempt"]["revealed_solution_at"] is not None
    assert q0_wrong["attempt"]["answered_after_revealed"] is False
    assert q0_wrong["revealed_answer"] is not None

    sub_retry = c_a.post(
        f"/api/materials/artifacts/{art['id']}/questions/{first_q['id']}/attempt",
        json={"user_answer": 0},
        headers=headers,
    )
    assert sub_retry.status_code == 200
    q0_retry = sub_retry.json()["questions"][0]
    assert q0_retry["attempt"]["is_correct"] is True
    assert q0_retry["attempt"]["score"] == 5.0
    assert q0_retry["attempt"]["answered_after_revealed"] is True
    assert q0_retry["attempt"]["first_is_correct"] is False
    assert q0_retry["attempt"]["first_score"] == 0.0

    # 4. 主观题先查看参考答案，再作答，应标记 answered_after_revealed=True 且不误报完整分
    calc_q = next(q for q in art["questions"] if q["q_type"] == "calc")
    rev = c_a.post(
        f"/api/materials/artifacts/{art['id']}/questions/{calc_q['id']}/reveal",
        headers=headers,
    )
    assert rev.status_code == 200
    calc_rev = next(q for q in rev.json()["questions"] if q["id"] == calc_q["id"])
    assert calc_rev["revealed_answer"] is not None
    assert calc_rev["attempt"]["revealed_solution_at"] is not None

    sub_calc = c_a.post(
        f"/api/materials/artifacts/{art['id']}/questions/{calc_q['id']}/attempt",
        json={"user_answer": "我的推导过程"},
        headers=headers,
    )
    assert sub_calc.status_code == 200
    calc_after = next(q for q in sub_calc.json()["questions"] if q["id"] == calc_q["id"])
    assert calc_after["attempt"]["grading_status"] == "pending_review"
    assert calc_after["attempt"]["answered_after_revealed"] is True

    # 5. 图谱节点笔记持久化与 B 隔离
    put_note = c_a.put(
        f"/api/materials/artifacts/{art['id']}/graph-notes/n2",
        json={"note_text": "重点复习对称性投影"},
        headers=headers,
    )
    assert put_note.status_code == 200
    assert c_a.get(f"/api/materials/artifacts/{art['id']}").json()["graph_notes"]["n2"] == "重点复习对称性投影"
    assert c_b.get(f"/api/materials/artifacts/{art['id']}").status_code == 404
    assert c_b.put(
        f"/api/materials/artifacts/{art['id']}/graph-notes/n2",
        json={"note_text": "越权写入"},
        headers=headers,
    ).status_code == 404

    # 6. 删除资料时清理产物与缩略图目录
    slide_dir = mat_root / "slides" / mid
    assert slide_dir.is_dir()
    assert c_a.delete(f"/api/materials/pdfs/{mid}", headers=headers).status_code == 200
    assert not slide_dir.exists()


def _mock_exampass_transport(
    calls: list[str], *, fail_on_call_number: int | None = None
):
    import json
    import httpx

    skeleton_json = json.dumps(
        {
            "title": "线性代数：矩阵与秩",
            "chapters": [
                {
                    "id": "ch1",
                    "label": "矩阵的秩与初等变换",
                    "summary": "初等行变换不改变矩阵的秩。",
                    "kcs": [
                        {
                            "id": "kc1.1",
                            "label": "矩阵的秩",
                            "type": "concept",
                            "importance": "key",
                            "deps": [],
                            "is_hub": True,
                            "source_refs": ["1-2"],
                        },
                        {
                            "id": "kc1.2",
                            "label": "初等行变换",
                            "type": "procedure",
                            "importance": "key",
                            "deps": ["kc1.1"],
                            "is_hub": False,
                            "source_refs": ["2-99"],  # 故意超界，测试页码钳位
                        },
                    ],
                }
            ],
        },
        ensure_ascii=False,
    )
    notes_html = (
        "<h2>一、矩阵与秩 <span data-slides=\"1\"></span></h2>"
        "<h3>1.1 矩阵的秩 <span data-slides=\"1\"></span></h3>"
        "<div class=\"tldr\"><strong>核心结论：</strong>行阶梯形非零行数即为秩。</div>"
        "<script>alert(1)</script>"
        "<div class=\"checkpoint\">自测：零矩阵的秩是多少？"
        "<details class=\"cp-answer\"><summary>参考答案</summary>0</details></div>"
    )
    quiz_json = json.dumps(
        {
            "questions": [
                {
                    "type": "choice",
                    "points": 5,
                    "kc_id": "kc1.1",
                    "question": "3阶单位矩阵的秩是：",
                    "options": ["0", "1", "2", "3"],
                    "answer": 2,  # 故意错标，测试第4步 review 补丁修正为 3
                    "explanation": "单位矩阵满秩，故秩为 3。",
                    "cognitive": "remember",
                    "difficulty": "basic",
                    "pitfall": "不要与行列式混淆",
                },
                {
                    "type": "tf",
                    "points": 5,
                    "kc_id": "kc1.2",
                    "question": "初等行变换不改变矩阵的秩。",
                    "answer": True,
                    "explanation": "初等行变换等价于左乘可逆矩阵，保持秩不变。",
                    "cognitive": "understand",
                    "difficulty": "basic",
                    "pitfall": "求极大无关组时列向量关系保持，行向量关系可能改变",
                },
            ]
        },
        ensure_ascii=False,
    )
    review_json = json.dumps(
        {
            "coverage_warnings": ["覆盖完整，第 2 页公式请核对原件。"],
            "patches": [{"index": 0, "answer": 3}],
        },
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
        if fail_on_call_number is not None and len(calls) == fail_on_call_number:
            return httpx.Response(500, json={"error": {"message": "simulated failure"}})

        return httpx.Response(
            200,
            json={
                "id": f"chatcmpl-{len(calls)}",
                "object": "chat.completion",
                "created": 1700000000,
                "model": "deepseek-chat",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200},
            },
        )

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    ("finish_reason", "invalid_content", "reason"),
    [
        ("length", '{"questions":[', "truncated"),
        ("stop", "", "empty"),
        ("stop", '{"questions":[}', "invalid_json"),
        ("stop", '{"questions":[]}', "invalid_questions"),
    ],
)
def test_chapter_quiz_batches_cover_large_outline_and_retry_only_the_failed_batch(
    finish_reason: str, invalid_content: str, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    import asyncio
    import json

    import httpx

    from app.config import DeepSeekSettings
    from app.materials.exampass_llm import DeepSeekExamPassGenerator

    module_sizes = (4, 4, 3, 3, 3, 2)
    chapters = [
        {
            "label": f"模块 {module_no}",
            "kcs": [
                {
                    "id": f"kc{module_no}.{item_no}",
                    "label": f"知识点 {module_no}.{item_no}",
                    "source_refs": [str(module_no)],
                }
                for item_no in range(1, size + 1)
            ],
        }
        for module_no, size in enumerate(module_sizes, 1)
    ]
    expected_ids = [kc["id"] for chapter in chapters for kc in chapter["kcs"]]
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        user_content = json.loads(request.content)["messages"][1]["content"]
        batch = json.loads(user_content.split("出一题：\n", 1)[1].split("\n\n", 1)[0])
        ids = [kc["id"] for kc in batch]
        calls.append(ids)
        if len(calls) == 3:
            content, reason_for_this_call = invalid_content, finish_reason
        else:
            content = json.dumps({
                "questions": [
                    {"type": "tf", "kc_id": kc_id, "question": f"{kc_id} 的题目", "answer": True}
                    for kc_id in ids
                ],
            }, ensure_ascii=False)
            reason_for_this_call = "stop"
        return httpx.Response(200, json={
            "id": "quiz-test",
            "object": "chat.completion",
            "created": 1700000000,
            "model": "deepseek-chat",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": reason_for_this_call,
            }],
        })

    async def generate() -> list:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            generator = DeepSeekExamPassGenerator(
                DeepSeekSettings("test-key", "https://mock.deepseek.local", "deepseek-chat"),
                http_async_client=client,
            )
            return await generator.generate_questions(
                display_name="36 页测试讲义.pdf",
                start_page=1,
                end_page=36,
                skeleton={"chapters": chapters},
                pages=[],
            )

    questions = asyncio.run(generate())
    expected_batches = [expected_ids[i:i + 4] for i in range(0, len(expected_ids), 4)]
    assert calls == [*expected_batches[:3], *expected_batches[2:]]
    assert [q["kc_id"] for q in questions] == expected_ids
    assert [q["sort_order"] for q in questions] == list(range(1, 20))
    assert questions[-1]["source_pages"] == [6]
    assert f"batch=3/5 attempt=1 reason={reason}" in caplog.text


def test_exampass_multistep_generation_and_checkpoint_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import sqlite3
    from uuid import uuid4

    import httpx

    from app.conversations.repository import ConversationRepository
    from tests.tutoring_fakes import FakeScheduler

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://mock.deepseek.local")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")

    c_a, c_b, _ = _setup_client(tmp_path, monkeypatch)
    headers = TEST_CSRF_HEADERS

    pdf_bytes = _make_simple_pdf(
        [
            "Chapter 1 Matrix Rank: The rank of a matrix is the dimension of its row space and column space.",
            "Section 1.2 Elementary row operations preserve the rank of any matrix over a field.",
        ]
    )
    up = c_a.post(
        "/api/materials/pdfs",
        files={"file": ("linalg.pdf", pdf_bytes, "application/pdf")},
        headers=headers,
    )
    assert up.status_code == 201
    mid = up.json()["id"]

    # 1. 第 4 步（quiz）模拟失败，保留 skeleton 与 notes 中间结果
    calls: list[str] = []
    app.state.model_http_client = httpx.AsyncClient(
        transport=_mock_exampass_transport(calls, fail_on_call_number=4)
    )
    fail_res = c_a.post(
        f"/api/materials/pdfs/{mid}/artifacts",
        json={"start_page": 1, "end_page": 2},
        headers=headers,
    )
    assert fail_res.status_code == 200
    assert fail_res.json()["status"] == "failed"
    assert fail_res.json()["current_step"] == "quiz"
    assert calls == ["skeleton", "notes", "notes_review", "quiz"]

    detail_after_fail = c_a.get(f"/api/materials/pdfs/{mid}").json()
    assert len(detail_after_fail["artifacts"]) == 1
    assert detail_after_fail["artifacts"][0]["status"] == "failed"
    assert detail_after_fail["artifacts"][0]["current_step"] == "quiz"

    # 2. 重试断点续传：跳过 skeleton 与 notes，仅执行 quiz 与 review
    app.state.model_http_client = httpx.AsyncClient(
        transport=_mock_exampass_transport(calls, fail_on_call_number=None)
    )
    resume_res = c_a.post(
        f"/api/materials/pdfs/{mid}/artifacts",
        json={"start_page": 1, "end_page": 2},
        headers=headers,
    )
    assert resume_res.status_code == 200
    assert calls == ["skeleton", "notes", "notes_review", "quiz", "quiz", "review"]

    art = resume_res.json()
    assert art["status"] == "ready"
    assert art["is_sample"] is False
    assert art["reused"] is False
    assert "<script" not in art["notes_html"]
    assert len(art["questions"]) == 2
    assert any("覆盖完整" in w for w in art["coverage_warnings"])

    # 验证 review 补丁已将第 1 题答案修正为 3
    q0 = art["questions"][0]
    sub = c_a.post(
        f"/api/materials/artifacts/{art['id']}/questions/{q0['id']}/attempt",
        json={"user_answer": 3},
        headers=headers,
    )
    assert sub.status_code == 200
    assert sub.json()["questions"][0]["attempt"]["is_correct"] is True

    # 3. 再次请求同一范围直接复用，模型调用计数不增加
    reuse_res = c_a.post(
        f"/api/materials/pdfs/{mid}/artifacts",
        json={"start_page": 1, "end_page": 2},
        headers=headers,
    )
    assert reuse_res.status_code == 200
    assert reuse_res.json()["reused"] is True
    assert len(calls) == 6

    # 4. 检查点 4：从章节测试题目跳转专用会话学习（含答案边界与归属校验）
    q1 = art["questions"][1]
    study_res = c_a.post(
        f"/api/materials/artifacts/{art['id']}/questions/{q1['id']}/study",
        json={"mode": "explain"},
        headers=headers,
    )
    assert study_res.status_code == 200
    study_payload = study_res.json()
    assert study_payload["conversation_id"] == art["conversation_id"]
    assert study_payload["artifact_id"] == art["id"]
    assert study_payload["question_id"] == q1["id"]

    # 验证发出聊天消息后，调度器能使用所选题及其解答权限。
    class ChapterReply:
        async def stream(self, material):
            yield "我们来看看这道章节测试题。"

    scheduler = FakeScheduler()
    monkeypatch.setattr("app.api.conversations.SchedulerAgent", lambda *args, **kwargs: scheduler)
    monkeypatch.setattr("app.api.conversations.StreamingReply", lambda *args, **kwargs: ChapterReply())
    db_path = tmp_path / "study.sqlite3"

    with sqlite3.connect(db_path) as db:
        owner_id = db.execute(
            "SELECT owner_id FROM conversations WHERE id = ?", (art["conversation_id"],)
        ).fetchone()[0]
    conversation_repo = ConversationRepository(db_path, owner_id=owner_id)
    for question in (q1, q0):
        turn_response = c_a.post(
            f"/api/conversations/{art['conversation_id']}/turns",
            json={
                "request_id": str(uuid4()),
                "kind": "auto",
                "query": study_payload["prompt"],
                "material_artifact_id": art["id"],
                "material_question_id": question["id"],
                "material_study_mode": "explain",
            },
            headers=headers,
        )
        assert turn_response.status_code == 200
        events = [
            json.loads(line.removeprefix("data: "))
            for line in turn_response.text.splitlines()
            if line.startswith("data: ")
        ]
        assert events[-1]["type"] == "result", events
        assert events[-1]["reply_status"] == "complete", events
        active_problem = conversation_repo.get_active_problem(art["conversation_id"])
        assert active_problem is not None
        assert question["id"] in (active_problem.source.text or "")
        assert active_problem.statement_text.startswith(question["stem"])
        assert (active_problem.explanation is not None) == (question["id"] == q0["id"])
    assert len(scheduler.calls) == 2

    # 另一账号尝试跳转返回 404
    assert (
        c_b.post(
            f"/api/materials/artifacts/{art['id']}/questions/{q1['id']}/study",
            json={"mode": "explain"},
            headers=headers,
        ).status_code
        == 404
    )


def test_invalid_page_refs_not_fallback_to_page1_and_legacy_attempt_compat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx
    import json
    import sqlite3

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://mock.deepseek.local")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")

    c_a, _, _ = _setup_client(tmp_path, monkeypatch)
    headers = TEST_CSRF_HEADERS

    pdf_bytes = _make_simple_pdf(
        [
            "Chapter 1 Matrix Rank: The rank of a matrix is the dimension of its row space and column space, invariant under elementary operations.",
            "Section 1.2 Elementary row operations preserve matrix rank and help solve linear systems cleanly across all dimensions.",
        ]
    )
    up = c_a.post(
        "/api/materials/pdfs",
        files={"file": ("linalg2.pdf", pdf_bytes, "application/pdf")},
        headers=headers,
    )
    assert up.status_code == 201
    mid = up.json()["id"]

    calls: list[str] = []
    app.state.model_http_client = httpx.AsyncClient(
        transport=_mock_exampass_transport(calls, fail_on_call_number=None)
    )
    res = c_a.post(
        f"/api/materials/pdfs/{mid}/artifacts",
        json={"start_page": 1, "end_page": 2, "course_hint": "几何与线性代数"},
        headers=headers,
    )
    assert res.status_code == 200
    art = res.json()

    # 1. kc1.2 的 source_refs=["2-99"] 越界后应置空，不再伪造第 1 页
    kc2 = art["skeleton"]["chapters"][0]["kcs"][1]
    assert kc2["source_refs"] == []
    q1 = art["questions"][1]
    assert q1["source_pages"] == []

    # 2. 模拟历史旧作答记录（无 evaluation_json.first_attempt），重答正确时保留旧 0 分
    db_path = tmp_path / "study.sqlite3"
    conn = sqlite3.connect(db_path)
    try:
        owner_id = conn.execute("SELECT owner_id FROM personal_materials WHERE id = ?", (mid,)).fetchone()[0]
        conn.execute(
            """
            INSERT INTO material_quiz_attempts (
                id, question_id, artifact_id, owner_id, user_answer_json,
                is_correct, score, max_points, grading_status, evaluation_json,
                revealed_solution_at, answered_after_revealed, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, 0.0, 5.0, 'auto_graded', '{}', '2026-11-17T00:00:00Z', 0, '2026-11-17T00:00:00Z', '2026-11-17T00:00:00Z')
            """,
            ("legacy-att-1", q1["id"], art["id"], owner_id, "false"),
        )
        conn.commit()
    finally:
        conn.close()

    retry = c_a.post(
        f"/api/materials/artifacts/{art['id']}/questions/{q1['id']}/attempt",
        json={"user_answer": True},
        headers=headers,
    )
    assert retry.status_code == 200
    q1_after = retry.json()["questions"][1]
    assert q1_after["attempt"]["is_correct"] is True
    assert q1_after["attempt"]["answered_after_revealed"] is True
    assert q1_after["attempt"]["first_is_correct"] is False
    assert q1_after["attempt"]["first_score"] == 0.0

    # 3. 学习记忆应采集该章节测试证据，且首错后看答案订正不得记为独立掌握
    mem = c_a.get("/api/learning-memory", params={"course": "几何与线性代数"}).json()
    entries = mem.get("entries", [])
    quiz_entry = next((e for e in entries if e["entry_id"] == f"material_quiz:{q1['id']}"), None)
    assert quiz_entry is not None
    assert quiz_entry["first_verdict"] == "wrong_independent"
    assert quiz_entry["verdict"] == "after_reveal"

    # 4. 笔记/图谱知识点跳转学习接口
    kc_study = c_a.post(
        f"/api/materials/artifacts/{art['id']}/kcs/kc1.1/study",
        json={"mode": "explain"},
        headers=headers,
    )
    assert kc_study.status_code == 200
    assert kc_study.json()["kc_id"] == "kc1.1"

    # 5. 主观题（或待核对填空）AI 评价回写
    q0 = art["questions"][0]
    eval_mock = json.dumps(
        {
            "is_correct": False,
            "needs_supplement": False,
            "final_answer_assessment": "矩阵秩概念理解有误。",
            "step_by_step_analysis": "",
            "guidance": "把 3 阶单位阵的秩误写为 2，建议回顾满秩矩阵定义。",
        },
        ensure_ascii=False,
    )
    app.state.model_http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={
                    "id": "eval-1",
                    "object": "chat.completion",
                    "created": 1700000001,
                    "model": "deepseek-chat",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": eval_mock}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 40, "total_tokens": 90},
                },
            )
        )
    )
    assert (
        c_a.post(
            f"/api/materials/artifacts/{art['id']}/questions/{q0['id']}/attempt",
            json={"user_answer": 2},
            headers=headers,
        ).status_code
        == 200
    )
    ev_res = c_a.post(
        f"/api/materials/artifacts/{art['id']}/questions/{q0['id']}/evaluate",
        headers=headers,
    )
    assert ev_res.status_code == 200
    q0_eval = ev_res.json()["questions"][0]
    assert q0_eval["attempt"]["grading_status"] == "evaluated"
    assert q0_eval["attempt"]["is_correct"] is False
    assert "矩阵秩概念理解有误" in q0_eval["attempt"]["feedback"]


def test_generating_artifact_blocks_quiz_and_preserves_question_ids_on_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.conversations import schema
    from app.materials.repository import MaterialRepository

    db_path = tmp_path / "study.sqlite3"
    schema.initialize_database(db_path)
    conn = schema._open(db_path)
    conn.execute(
        "INSERT INTO users (id, username, username_normalized, password_hash, created_at) VALUES ('u1', 'u1', 'u1', 'h', '2026-11-18T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    repo = MaterialRepository(db_path, owner_id="u1")
    mat = repo.create_material(
        material_id="m-lock",
        display_name="test.pdf",
        stored_name="u1/m-lock.pdf",
        file_sha256="a" * 64,
        size_bytes=100,
        page_count=5,
        max_per_user=20,
    )
    acq = repo.acquire_artifact_generation(
        material_id=mat.id,
        file_sha256=mat.file_sha256,
        start_page=1,
        end_page=5,
        rule_version="exampass-v1",
        course_hint="",
        default_title="test",
    )
    assert acq.generation_token is not None

    # 同一资料另一重叠页段同时生成必须被拦截
    from app.materials.errors import MaterialConflictError

    with pytest.raises(MaterialConflictError):
        repo.acquire_artifact_generation(
            material_id=mat.id,
            file_sha256=mat.file_sha256,
            start_page=2,
            end_page=4,
            rule_version="exampass-v1",
            course_hint="",
            default_title="test",
        )

    assert repo.save_artifact_step_quiz(
        artifact_id=acq.artifact_id,
        generation_token=acq.generation_token,
        questions=[
            {
                "sort_order": 1,
                "kc_id": "kc1.1",
                "q_type": "choice",
                "stem": "题干1",
                "options": ["A", "B", "C", "D"],
                "answer": 0,
                "explanation": "原解析",
                "pitfall": "",
                "cognitive": "remember",
                "difficulty": "basic",
                "points": 5,
                "source_pages": [1],
            }
        ],
    )
    art_before = repo.get_artifact_detail(acq.artifact_id)
    qid_before = art_before.questions[0].id

    # 生成尚未 ready 前，禁止读取/作答
    with pytest.raises(MaterialConflictError):
        repo.get_quiz_question_row(acq.artifact_id, qid_before)

    # 复核完成就地更新答案，question_id 必须保持不变
    assert repo.complete_artifact_generation(
        artifact_id=acq.artifact_id,
        generation_token=acq.generation_token,
        coverage_warnings=[],
        questions_override=[
            {
                "sort_order": 1,
                "kc_id": "kc1.1",
                "q_type": "choice",
                "stem": "题干1",
                "options": ["A", "B", "C", "D"],
                "answer": 2,
                "explanation": "修正后解析",
                "pitfall": "新增易错",
                "cognitive": "remember",
                "difficulty": "basic",
                "points": 5,
                "source_pages": [1],
            }
        ],
    )
    art_after = repo.get_artifact_detail(acq.artifact_id)
    assert art_after.questions[0].id == qid_before
    row_after = repo.get_quiz_question_row(acq.artifact_id, qid_before)
    assert row_after["answer_json"] == "2"


def test_late_eval_rejected_reveal_before_first_attempt_and_page_clamping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx
    import json
    from app.materials.exampass_llm import _clamp_notes_slide_refs, _format_compact_page_refs

    # 1. 离散页码不扩成区间，笔记越界 data-slides 被移除
    assert _format_compact_page_refs([3, 8]) == ["3", "8"]
    assert _format_compact_page_refs([3, 4, 5, 8]) == ["3-5", "8"]
    clamped = _clamp_notes_slide_refs(
        '<h3 data-slides="27">超界</h3><h3 data-slides="3,27">部分</h3>',
        1,
        8,
    )
    assert "27" not in clamped
    assert 'data-slides="3"' in clamped

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://mock.deepseek.local")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")

    c_a, _, _ = _setup_client(tmp_path, monkeypatch)
    headers = TEST_CSRF_HEADERS
    pdf_bytes = _make_simple_pdf(
        [
            "Chapter 1 Matrix Rank: The rank of a matrix is the dimension of its row space and column space, invariant under elementary operations.",
            "Section 1.2 Elementary row operations preserve matrix rank and help solve linear systems cleanly across all dimensions.",
        ]
    )
    up = c_a.post(
        "/api/materials/pdfs",
        files={"file": ("linalg3.pdf", pdf_bytes, "application/pdf")},
        headers=headers,
    )
    assert up.status_code == 201
    mid = up.json()["id"]

    calls: list[str] = []
    app.state.model_http_client = httpx.AsyncClient(
        transport=_mock_exampass_transport(calls, fail_on_call_number=None)
    )
    art = c_a.post(
        f"/api/materials/pdfs/{mid}/artifacts",
        json={"start_page": 1, "end_page": 2, "course_hint": "几何与线性代数"},
        headers=headers,
    ).json()
    q0, q1 = art["questions"][0], art["questions"][1]

    # 2. 先看答案再首次作答，学习记忆 first_verdict 必须为 after_reveal
    assert c_a.post(f"/api/materials/artifacts/{art['id']}/questions/{q1['id']}/reveal", headers=headers).status_code == 200
    assert (
        c_a.post(
            f"/api/materials/artifacts/{art['id']}/questions/{q1['id']}/attempt",
            json={"user_answer": True},
            headers=headers,
        ).status_code
        == 200
    )
    mem = c_a.get("/api/learning-memory", params={"course": "几何与线性代数"}).json()
    entry_q1 = next(e for e in mem["entries"] if e["entry_id"] == f"material_quiz:{q1['id']}")
    assert entry_q1["first_verdict"] == "after_reveal"
    assert entry_q1["verdict"] == "after_reveal"

    # 3. 迟到评价拒绝覆盖新作答 B
    assert (
        c_a.post(
            f"/api/materials/artifacts/{art['id']}/questions/{q0['id']}/attempt",
            json={"user_answer": 0},
            headers=headers,
        ).status_code
        == 200
    )

    def _race_handler(request: httpx.Request) -> httpx.Response:
        # 模拟评价请求飞在半路时，另一请求提交了新答案 B (下标 1)
        import sqlite3
        from app.materials.repository import MaterialRepository

        db_p = tmp_path / "study.sqlite3"
        conn = sqlite3.connect(db_p)
        owner_id = conn.execute("SELECT owner_id FROM personal_materials WHERE id = ?", (mid,)).fetchone()[0]
        conn.close()
        MaterialRepository(db_p, owner_id=owner_id).save_quiz_attempt(
            artifact_id=art["id"],
            question_id=q0["id"],
            user_answer=1,
            is_correct=False,
            score=0.0,
            max_points=5.0,
            grading_status="auto_graded",
            feedback="选择有误。",
        )
        eval_mock = json.dumps(
            {
                "is_correct": False,
                "needs_supplement": False,
                "final_answer_assessment": "旧答案A有误。",
                "step_by_step_analysis": "",
                "guidance": "请重做。",
            },
            ensure_ascii=False,
        )
        return httpx.Response(
            200,
            json={
                "id": "eval-race",
                "object": "chat.completion",
                "created": 1700000002,
                "model": "deepseek-chat",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": eval_mock}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50},
            },
        )

    app.state.model_http_client = httpx.AsyncClient(transport=httpx.MockTransport(_race_handler))
    race_eval = c_a.post(
        f"/api/materials/artifacts/{art['id']}/questions/{q0['id']}/evaluate",
        headers=headers,
    )
    assert race_eval.status_code == 409
    latest = c_a.get(f"/api/materials/artifacts/{art['id']}").json()
    assert latest["questions"][0]["attempt"]["user_answer"] == 1


def test_notes_sanitization_preserves_comparisons_and_escapes_tags_inside_math():
    from app.materials.exampass_adapter import sanitize_notes_html

    raw = (
        '<h3 data-kc="p21-kc2.3" onclick="bad()">分段场强</h3>'
        r'$$E=\begin{cases}0,&r<a\\f(r),&a<r<b\\0,&r>b\end{cases}$$'
        '<p>补充 $r&lt;a$ 和 $<img src=x onerror=bad()>$</p>'
        '<script>bad()</script>'
    )
    sanitized = sanitize_notes_html(raw)
    assert 'data-kc="p21-kc2.3"' in sanitized
    assert r'a&lt;r&lt;b\\0,&amp;r&gt;b' in sanitized
    assert '$r&lt;a$' in sanitized
    assert '<img' not in sanitized
    assert '<script' not in sanitized
    assert 'onclick' not in sanitized
    assert sanitize_notes_html(sanitized) == sanitized



def test_quiz_cap_keeps_later_chapters(monkeypatch):
    import asyncio
    import json

    from app.config import DeepSeekSettings
    from app.materials.exampass_llm import DeepSeekExamPassGenerator, GeneratedQuizQuestion

    generator = DeepSeekExamPassGenerator(
        DeepSeekSettings("test-key", "https://mock.deepseek.local", "deepseek-chat")
    )

    async def fake_batch(request, **kwargs):
        points = json.loads(request.splitlines()[1])
        return [
            GeneratedQuizQuestion(type="tf", kc_id=point["id"], question=point["label"], answer=True)
            for point in points
        ]

    monkeypatch.setattr(generator, "_generate_quiz_batch", fake_batch)
    chapters = [
        {
            "label": f"章节 {n}",
            "kcs": [
                {"id": f"p{n}-{i}", "label": f"知识点 {i}", "source_refs": [str(n)], "importance": "key"}
                for i in range(size)
            ],
        }
        for n, size in enumerate((12, 12, 5, 6, 4), start=1)
    ]
    questions = asyncio.run(generator.generate_questions(
        display_name="讲义.pdf", start_page=1, end_page=5, skeleton={"chapters": chapters}, pages=[],
    ))
    assert len(questions) == 24
    assert {p for question in questions for p in question["source_pages"]} == {1, 2, 3, 4, 5}
