"""校订失效必须阻止构建；真实首批发布保留答案来源并恢复可运行代码。"""

from copy import deepcopy
import ast
import json
from pathlib import Path

import pytest

from app.question_bank.paths import BankPaths
from app.question_bank.textbuild import build_retrieval_text
from tools.question_bank.build import load_bank_inputs
from tools.question_bank.curation import CurationError, apply_corrections
from tools.question_bank.db import content_sha256


@pytest.fixture
def correction_case(tmp_path):
    record = {
        "question_id": "p-fill-01", "paper_id": "p", "course": "Python语言程序设计",
        "question_type": "fill", "statement": "计算表达式", "pages": {"statement": [1]},
        "answer": {"state": "original", "text": "原卷误印", "origin": "paper"},
    }
    papers = [{"paper_id": "p", "pdf_sha256": "a" * 64, "physical_pages": 2}]
    item = {
        "question_id": record["question_id"], "before_content_sha256": content_sha256(record),
        "pdf_sha256": "a" * 64, "evidence_pages": [1], "reason": "原页恢复",
        "reviewed_by": "导师", "patch": {"statement": "恢复后的题干"},
    }
    path = tmp_path / "corrections.json"
    return record, papers, item, path


def write_case(path, items):
    path.write_text(json.dumps({"schema_version": 1, "corrections": items}), encoding="utf-8")


def test_curation_does_not_mutate_raw_input(correction_case):
    record, papers, item, path = correction_case
    before = deepcopy(record)
    write_case(path, [item])
    result = apply_corrections([record], papers, path)
    assert result[0]["statement"] == "恢复后的题干"
    assert record == before
    assert content_sha256(result[0]) != content_sha256(record)


@pytest.mark.parametrize("change", ["content", "pdf", "identity", "type", "page", "answer", "unknown"])
def test_stale_or_unsafe_curation_is_rejected(correction_case, change):
    record, papers, item, path = correction_case
    if change == "content":
        record["statement"] += "新版本"
    elif change == "pdf":
        papers[0]["pdf_sha256"] = "b" * 64
    elif change == "identity":
        item["patch"]["question_id"] = "another"
    elif change == "type":
        item["patch"]["statement"] = ["错误类型"]
    elif change == "page":
        item["evidence_pages"] = [3]
    elif change == "answer":
        item["patch"]["answer"] = {"state": "original", "text": "篡改答案", "origin": "paper"}
    else:
        item["question_id"] = "unknown"
    write_case(path, [item])
    with pytest.raises(CurationError):
        apply_corrections([record], papers, path)


def test_duplicate_curation_is_rejected(correction_case):
    record, papers, item, path = correction_case
    write_case(path, [item, item])
    with pytest.raises(CurationError, match="重复"):
        apply_corrections([record], papers, path)


@pytest.fixture(scope="module")
def release_records():
    paths = BankPaths()
    correction_path = paths.root / "review/p4/mentor-corrections.json"
    if not correction_path.exists():
        pytest.skip("本机没有首批校订资料")
    raw = {
        record["question_id"]: record
        for path in (paths.root / "source/full").glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        for record in [json.loads(line)]
    }
    records, _, _ = load_bank_inputs(paths)
    return raw, {record["question_id"]: record for record in records}


def test_release_changes_exactly_thirty_records_and_preserves_original_answers(release_records):
    raw, curated = release_records
    changed = [qid for qid in raw if content_sha256(raw[qid]) != content_sha256(curated[qid])]
    assert len(changed) == 30
    for qid in raw:
        assert {k: v for k, v in raw[qid]["answer"].items() if k != "note"} == {
            k: v for k, v in curated[qid]["answer"].items() if k != "note"
        }


def test_missing_original_answers_have_separate_teacher_solutions(release_records):
    _, curated = release_records
    prefix = "py-2023-2024-s2-midterm-"
    for suffix in ("choice-01", "fill-01", "fill-02", "code-01"):
        record = curated[prefix + suffix]
        assert record["answer"]["state"] == "missing"
        assert record["answer"]["text"] is None
        solution = record["independent_solutions"][0]
        assert "导师" in solution["author"]
        assert solution["text"] not in build_retrieval_text(record)


@pytest.mark.parametrize("qid,expected", [
    ("py-2021-2022-s2-midterm-choice-30", 7),
    ("py-2024-2025-s1-midterm-choice-40", 45),
])
def test_visually_restored_nested_loops_have_expected_output(release_records, capsys, qid, expected):
    _, curated = release_records
    code = "\n".join(curated[qid]["code_lines"])
    # 仅执行本文件列明的两段已目视审核算术循环，无输入、导入或文件操作。
    tree = ast.parse(code)
    assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(tree))
    exec(compile(tree, qid, "exec"), {"__builtins__": {"range": range, "print": print}})
    assert capsys.readouterr().out.strip() == str(expected)


def test_original_and_teacher_code_are_syntactically_valid(release_records):
    _, curated = release_records
    candidates = [json.loads(line)["question_id"] for line in (
        BankPaths().root / "review/p4/publish-candidates.jsonl"
    ).read_text(encoding="utf-8").splitlines() if line.strip()]
    for qid in candidates:
        record = curated[qid]
        for block in record.get("code_blocks") or []:
            # 首批已审核范围内的原卷参考代码均可独立解析。
            ast.parse(block["text"])
    record = curated["py-2020-2021-s2-final-code-01"]
    ast.parse(record["reference_code"])
    assert "range(1000,2000)" in record["reference_code"]
    assert "range(1000, 2001)" in record["independent_solutions"][0]["text"]


def test_formula_debris_and_foreign_footer_removed(release_records):
    _, curated = release_records
    record = curated["py-2020-2021-s1-midterm-code-01"]
    assert record["code_lines"] == [] and record["formulas"]
    assert curated["py-2020-2021-s1-midterm-choice-01"]["options"][-1]["text"].endswith("目标代码")
    record = curated["py-2021-2022-s1-final-code-02"]
    assert "学号" not in record["statement"]
    assert record["pages"]["statement"] == [2]
    assert record["assets"]


@pytest.mark.parametrize("qid,line", [
    ("py-2020-2021-s2-final-fill-15", "    writer.writerows(data)"),
    ("py-2024-2025-s2-final-fill-15", "    y = y - step"),
])
def test_with_and_loop_bodies_preserve_source_indentation(release_records, qid, line):
    _, curated = release_records
    assert line in curated[qid]["code_lines"]
    ast.parse("\n".join(curated[qid]["code_lines"]))


def test_published_bank_http_answer_source_and_teacher_boundary(client, study_db_path):
    from uuid import uuid4
    from app.conversations.repository import ConversationRepository
    from app.question_bank.models import SearchRequest
    from app.question_bank.service import QuestionBankService
    from app.questions.runtime import QuestionRuntime
    from tests.test_question_api import add_search_turn, create_conversation

    with QuestionBankService() as service:
        if not service.approved_count:
            pytest.skip("首批审核决定尚未应用")
        runtime = QuestionRuntime(service)
        previous = client.app.state.question_runtime
        client.app.state.question_runtime = runtime
        try:
            conversation = create_conversation(client)
            result = runtime.search(SearchRequest(
                course="Python", query="", paper_id="py-2023-2024-s2-midterm",
                question_type="fill", limit=20,
            ), conversation_id=conversation["id"])
            card = next(c for c in result.cards if c.question_id.endswith("fill-02"))
            assert card.review.approved and card.answer.state == "missing"
            assert not card.independent_solutions
            source_turn = add_search_turn(study_db_path, conversation["id"], card)
            endpoint = f"/api/conversations/{conversation['id']}/questions/{card.question_id}"
            response = client.post(f"/api/conversations/{conversation['id']}/turns", json={
                "request_id": str(uuid4()), "kind": "select_question",
                "question_id": card.question_id, "question_source_turn": source_turn,
            })
            assert response.status_code == 200
            assert client.get(endpoint + f"/answer?build={card.build_id}").status_code == 409
            response = client.post(f"/api/conversations/{conversation['id']}/turns", json={
                "request_id": str(uuid4()), "kind": "view_question_answer",
                "question_id": card.question_id, "question_source_turn": source_turn,
            })
            assert response.status_code == 200
            response = client.get(endpoint + f"/answer?build={card.build_id}")
            assert response.status_code == 200
            payload = response.json()
            assert payload["answer_detail"]["state"] == "missing"
            assert payload["answer_detail"]["text"] is None
            assert payload["independent_solutions"][0]["text"] == "(log(3*pi)+sqrt(6*x))/(8*e**4-5)"
            assert response.headers["cache-control"] == "private, no-store"
            source = client.get(endpoint + f"/source?build={card.build_id}&kind=statement&page=7")
            assert source.status_code == 200 and source.content.startswith(b"%PDF-")
            assert source.headers["content-disposition"].startswith("inline")
            assert client.get(endpoint + f"/source?build={card.build_id}&kind=statement&page=999").status_code == 404
            # 学习库只保存查看事实，独立答案正文仍在题库归档。
            assert b"(log(3*pi)+sqrt(6*x))/(8*e**4-5)" not in study_db_path.read_bytes()
            selected = ConversationRepository(study_db_path).get_active_question(conversation["id"])
            assert selected.question_id == card.question_id
        finally:
            client.app.state.question_runtime = previous


def test_turn_storage_removes_all_answer_body_fields(tmp_path):
    from uuid import uuid4
    from app.conversations.repository import ConversationRepository
    from app.questions.models import QuestionCard
    from tests.question_fakes import sample_card

    repository = ConversationRepository(tmp_path / "study.sqlite3")
    repository.initialize()
    conversation = repository.create_conversation()
    data = sample_card().model_dump()
    data.update({
        "reference_code": "REFERENCE-CODE-SENTINEL",
        "reference_code_pages": [99],
        "code_blocks": [{"text": "BLOCK-SENTINEL"}],
        "code_annotations": [{"text": "ANNOTATION-SENTINEL"}],
        "code_examples": ["EXAMPLE-SENTINEL"],
        "independent_solutions": [{"author": "导师", "text": "TEACHER-SENTINEL"}],
        "answer_revealed": True,
    })
    card = QuestionCard.model_validate(data)
    request_id = str(uuid4())
    repository.start_turn(conversation.id, request_id, "查看答案", kind="dynamic")
    repository.complete_dynamic_turn(
        conversation.id, request_id, kind="view_question_answer", question=card,
        question_results=[card], assistant_message="已查看", question_result_status="ok",
    )
    import sqlite3
    with sqlite3.connect(repository.db_path) as connection:
        output = json.loads(connection.execute("SELECT output_json FROM conversation_turns").fetchone()[0])
    for snapshot in [output["question"], *output["question_results"]]:
        assert snapshot["answer_revealed"] is True
        for key in ("answer_detail", "reference_code", "reference_code_pages", "code_blocks",
                    "code_annotations", "code_examples", "independent_solutions", "answer_url"):
            assert key not in snapshot
