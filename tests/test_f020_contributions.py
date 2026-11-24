"""F020：资料自愿贡献与私有待审目录隔离测试。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from pypdf import PdfReader
import pytest

from app.auth.dependencies import require_user
from app.contributions import storage as contrib_storage
from app.contributions.errors import ContributionStorageError
from app.main import app
from tests.f012_support import TEST_CSRF_HEADERS, create_invite, register
from tests.test_materials_pdf_parser import _make_simple_pdf


def _setup_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    db_path = tmp_path / "study.sqlite3"
    contrib_root = tmp_path / "contributions"
    monkeypatch.setenv("HHU_STUDY_DB", str(db_path))
    monkeypatch.setattr("app.conversations.repository.DEFAULT_DB_PATH", db_path)
    monkeypatch.setenv("CONTRIBUTION_STORAGE_DIR", str(contrib_root))
    app.dependency_overrides.pop(require_user, None)
    return db_path, contrib_root


def test_submit_contribution_requires_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)
    pdf_bytes = _make_simple_pdf(["2024 HHU Calculus Final Exam"])
    with TestClient(app) as client:
        response = client.post(
            "/api/contributions",
            headers=TEST_CSRF_HEADERS,
            data={"course": "高等数学", "year": "2024"},
            files={"file": ("exam.pdf", pdf_bytes, "application/pdf")},
        )
    assert response.status_code == 401


def test_submit_contribution_saves_valid_pdf_to_private_dir_and_stays_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, contrib_root = _setup_env(tmp_path, monkeypatch)
    pdf_bytes = _make_simple_pdf(["2024 Higher Mathematics Final Exam Problem 1"])

    with TestClient(app) as student_a:
        invite_a = create_invite(db_path)
        register(student_a, invite_code=invite_a, username="student_a")

        response = student_a.post(
            "/api/contributions",
            headers=TEST_CSRF_HEADERS,
            data={"course": "高等数学", "year": "2024"},
            files={"file": ("高数2024期末.pdf", pdf_bytes, "application/pdf")},
        )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["course"] == "高等数学"
    assert payload["year"] == "2024"
    assert payload["status"] == "pending_review"
    assert payload["message"] == "已提交，等待审核"

    cid = payload["contribution_id"]
    entry_dir = contrib_root / cid
    saved_pdf = entry_dir / "source.pdf"
    assert saved_pdf.read_bytes() == pdf_bytes
    reader = PdfReader(str(saved_pdf))
    assert len(reader.pages) == 1

    meta = json.loads((entry_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["contribution_id"] == cid
    assert meta["username"] == "student_a"
    assert meta["course"] == "高等数学"
    assert meta["year"] == "2024"
    assert meta["original_filename"] == "高数2024期末.pdf"
    assert meta["page_count"] == 1
    assert meta["status"] == "pending_review"

    # 另一个学生账号：不能通过任何网站接口下载待审原 PDF，也不能查到未审核内容
    with TestClient(app) as student_b:
        invite_b = create_invite(db_path)
        register(student_b, invite_code=invite_b, username="student_b")

        assert student_b.get(f"/api/papers/{cid}/download").status_code == 404
        assert student_b.get(f"/api/contributions/{cid}").status_code == 404
        assert student_b.get(f"/api/contributions/{cid}/file").status_code == 404
        assert student_b.get(f"/data/local/contributions/{cid}/source.pdf").status_code == 404


def test_submit_contribution_rejects_corrupted_pdf_without_persisting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, contrib_root = _setup_env(tmp_path, monkeypatch)
    corrupted_pdf = b"%PDF-1.4\n%%EOF\n"

    with TestClient(app) as client:
        invite = create_invite(db_path)
        register(client, invite_code=invite, username="student_bad_pdf")

        response = client.post(
            "/api/contributions",
            headers=TEST_CSRF_HEADERS,
            data={"course": "高等数学", "year": "2024"},
            files={"file": ("broken.pdf", corrupted_pdf, "application/pdf")},
        )

    assert response.status_code == 422
    assert not contrib_root.exists() or list(contrib_root.iterdir()) == []


def test_submit_contribution_rolls_back_when_metadata_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, contrib_root = _setup_env(tmp_path, monkeypatch)
    pdf_bytes = _make_simple_pdf(["Valid PDF page 1"])

    real_atomic_write = contrib_storage._atomic_write_bytes

    def fail_on_metadata(target: Path, content: bytes) -> None:
        if target.name == "metadata.json":
            raise ContributionStorageError("模拟元数据写入失败")
        real_atomic_write(target, content)

    monkeypatch.setattr(contrib_storage, "_atomic_write_bytes", fail_on_metadata)

    with TestClient(app) as client:
        invite = create_invite(db_path)
        register(client, invite_code=invite, username="student_rollback")

        response = client.post(
            "/api/contributions",
            headers=TEST_CSRF_HEADERS,
            data={"course": "高等数学", "year": "2024"},
            files={"file": ("exam.pdf", pdf_bytes, "application/pdf")},
        )

    assert response.status_code == 503
    assert contrib_root.exists()
    assert list(contrib_root.iterdir()) == []


def test_submit_contribution_validates_fields_and_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, _ = _setup_env(tmp_path, monkeypatch)
    pdf_bytes = _make_simple_pdf(["Valid page"])

    with TestClient(app) as client:
        invite = create_invite(db_path)
        register(client, invite_code=invite, username="student_val")

        blank_course = client.post(
            "/api/contributions",
            headers=TEST_CSRF_HEADERS,
            data={"course": "   ", "year": "2024"},
            files={"file": ("exam.pdf", pdf_bytes, "application/pdf")},
        )
        assert blank_course.status_code == 422

        blank_year = client.post(
            "/api/contributions",
            headers=TEST_CSRF_HEADERS,
            data={"course": "高等数学", "year": "  "},
            files={"file": ("exam.pdf", pdf_bytes, "application/pdf")},
        )
        assert blank_year.status_code == 422

        not_pdf = client.post(
            "/api/contributions",
            headers=TEST_CSRF_HEADERS,
            data={"course": "高等数学", "year": "2024"},
            files={"file": ("notes.txt", b"plain text", "text/plain")},
        )
        assert not_pdf.status_code == 422

        empty_pdf = client.post(
            "/api/contributions",
            headers=TEST_CSRF_HEADERS,
            data={"course": "高等数学", "year": "2024"},
            files={"file": ("empty.pdf", b"", "application/pdf")},
        )
        assert empty_pdf.status_code == 422


def test_submit_contribution_rejects_oversized_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, _ = _setup_env(tmp_path, monkeypatch)
    with TestClient(app) as client:
        app.state.contribution_config = app.state.contribution_config.__class__(
            max_bytes=16,
            storage_dir=str(tmp_path / "contributions"),
        )
        invite = create_invite(db_path)
        register(client, invite_code=invite, username="student_big")

        response = client.post(
            "/api/contributions",
            headers=TEST_CSRF_HEADERS,
            data={"course": "高等数学", "year": "2024"},
            files={"file": ("big.pdf", b"%PDF-1.4\n" + b"0" * 32, "application/pdf")},
        )
    assert response.status_code == 413
