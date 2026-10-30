"""来源页权限、哈希校验及查看事实；只写隔离临时数据。"""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.questions.archive import BuildArchive
from app.questions.models import AssetPayload
from app.questions.errors import QuestionAssetMissingError, QuestionSourceChangedError
from tests.test_question_api import create_conversation, install_runtime, add_search_turn
from tests.question_fakes import sample_card


def test_source_archive_validates_page_hash_and_path(tmp_path: Path) -> None:
    paper = tmp_path / "original.pdf"
    paper.write_bytes(b"%PDF-1.4\noriginal source")
    metadata = {"pdf_rel_path": paper.name, "pdf_sha256": hashlib.sha256(paper.read_bytes()).hexdigest(), "physical_pages": 3}
    archive = BuildArchive(tmp_path / "bank", tmp_path)
    archive._detail = lambda *args: {"pages_statement": [1, 2], "pages_answer": [3], "_paper": metadata, "_build": {"source_version": "sv-test"}}
    ref = SimpleNamespace(build_id="iv-" + "0" * 16, question_id="q", content_sha256="a" * 64)
    assert archive.source(ref, "statement", 2).path == paper
    with pytest.raises(QuestionAssetMissingError):
        archive.source(ref, "answer", 1)
    paper.write_bytes(b"changed source")
    with pytest.raises(QuestionSourceChangedError):
        archive.source(ref, "answer", 3)
    metadata["pdf_rel_path"] = "../outside.pdf"
    with pytest.raises(QuestionAssetMissingError):
        archive.source(ref, "statement", 1)


def test_source_access_is_scoped_and_records_without_changing_target(client, study_db_path: Path, tmp_path: Path) -> None:
    from app.conversations.repository import ConversationRepository
    runtime = install_runtime(client)
    conversation = create_conversation(client)
    card = sample_card()
    add_search_turn(study_db_path, conversation["id"], card)
    paper = tmp_path / "source.pdf"
    paper.write_bytes(b"%PDF-1.4\nfixture")
    def source_for_card(candidate, kind, page):
        assert candidate.question_id == card.question_id
        if page not in candidate.pages.statement:
            raise QuestionAssetMissingError("来源页无效")
        return AssetPayload(path=paper, mime="application/pdf", page=page, description="原卷", source_version="sv-test")
    runtime.source_for_card = source_for_card
    base = f"/api/conversations/{conversation['id']}/questions/{card.question_id}/source?build={card.build_id}&kind=statement&page="
    repository = ConversationRepository(study_db_path)
    assert client.get(base + "999").status_code == 404
    assert repository.get_question_selection(conversation["id"], card.question_id) is None
    response = client.get(base + "1")
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("inline")
    assert response.headers["cache-control"] == "private, no-store"
    assert repository.get_active_question(conversation["id"]) is None
    selected = repository.get_question_selection(conversation["id"], card.question_id)
    assert selected.answer_revealed_at
    assert selected.snapshot["source_views"][0]["page"] == 1
    assert selected.snapshot["source_views"][0]["may_show_answer"] is True
    client.get(base + "1")
    assert len(repository.get_question_selection(conversation["id"], card.question_id).snapshot["source_views"]) == 1
    detail = client.get(f"/api/conversations/{conversation['id']}").json()
    assert detail["turns"][0]["question_results"][0]["source_views"][0]["page"] == 1
    other = create_conversation(client)
    assert client.get(base.replace(conversation["id"], other["id"]) + "1").status_code == 404
