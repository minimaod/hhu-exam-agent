"""F017 Fix-009 回归测试：资料处理阶段进度、OCR 逐页计数与会话隔离。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from starlette.datastructures import State

from app.api.conversations import (
    ConversationServiceState,
    _material_turn_tasks,
    _stream_dispatch_turn,
    get_conversation_material_progress,
    stop_material_turn,
)
from app.config import MaterialConfigState
from app.conversations import schema
from app.conversations.models import TurnProgressEvent
from app.conversations.repository import ConversationRepository
from app.main import app
from app.materials.ocr import OcrPage
from app.materials.pdf_parser import ParsedPage
from app.materials.repository import MaterialRepository
from app.materials.service import MaterialService
from app.querying.errors import QueryError
from tests.f012_support import (
    TEST_CSRF_HEADERS,
    TEST_USER_ID,
    create_conversation,
    create_invite,
    ensure_test_user,
    register,
)


class _RecordingExamPassGenerator:
    def __init__(self, on_step: Any) -> None:
        self._on_step = on_step

    async def generate_skeleton(self, **_kwargs: Any) -> dict[str, object]:
        self._on_step("skeleton")
        return {
            "title": "静电场",
            "chapters": [
                {"id": "c1", "label": "静电场", "kcs": [{"id": "kc1", "label": "高斯定理", "source_refs": ["1"]}]}
            ],
        }

    async def generate_notes(self, **_kwargs: Any) -> str:
        self._on_step("notes")
        return "<h2>静电场</h2><p>核心结论</p>"

    async def generate_questions(self, **_kwargs: Any) -> list[dict[str, object]]:
        self._on_step("quiz")
        return [
            {
                "id": "q1",
                "sort_order": 1,
                "kc_id": "kc1",
                "q_type": "tf",
                "stem": "闭合面通量为零时场强必为零。",
                "options": None,
                "points": 5,
                "pitfall": "",
                "cognitive": "understand",
                "difficulty": "basic",
                "source_pages": [1],
                "answer": "false",
                "explanation": "净电荷为零即可。",
            }
        ]

    async def review_and_refine(self, **_kwargs: Any) -> tuple[list[str], list[dict[str, object]] | None]:
        self._on_step("review")
        return ([], None)


class _MixedOcr:
    def __init__(self, service: MaterialService, conv_id: str, seen: list[tuple[int, int, list[int]]]) -> None:
        self._service = service
        self._conv_id = conv_id
        self._seen = seen

    async def transcribe(self, _image: bytes, page_no: int) -> OcrPage:
        items = self._service.list_conversation_progress(self._conv_id)
        if items:
            self._seen.append((items[0].ocr_done, items[0].ocr_total, list(items[0].ocr_failed_pages)))
        if page_no == 2:
            raise QueryError("MODEL_ERROR", "第 2 页识别超时")
        return OcrPage(
            page_no=page_no,
            char_count=120,
            scanned_suspect=False,
            garbled_ratio=0.0,
            formula_warning=False,
            extracted_text=f"第 {page_no} 页识别内容，包含电场与高斯定理。",
        )


def _setup_repo_and_material(tmp_path: Path, owner_id: str = TEST_USER_ID) -> tuple[MaterialRepository, str, str]:
    db_path = tmp_path / "study.db"
    schema.initialize_database(db_path)
    ensure_test_user(db_path)
    conv_repo = ConversationRepository(db_path, owner_id=owner_id)
    conv = conv_repo.create_conversation()
    mat_repo = MaterialRepository(db_path, owner_id=owner_id)
    mat_root = tmp_path / "materials"
    mat_root.mkdir(parents=True, exist_ok=True)
    stored_name = "sample.pdf"
    (mat_root / stored_name).write_bytes(b"%PDF-1.4\n%test\n")
    mat = mat_repo.create_material(
        material_id="33333333-3333-4333-8333-333333333333",
        display_name="静电场讲义.pdf",
        stored_name=stored_name,
        file_sha256="a" * 64,
        size_bytes=128,
        page_count=2,
        max_per_user=10,
    )
    mat_repo.attach_material_to_conversation(conversation_id=conv.id, material_id=mat.id)
    return mat_repo, conv.id, mat.id


def test_generate_artifact_reports_real_stages_and_clears_progress(tmp_path: Path, monkeypatch: Any) -> None:
    mat_repo, conv_id, mat_id = _setup_repo_and_material(tmp_path)
    mat_repo.save_parsed_pages(
        mat_id,
        [
            ParsedPage(
                page_no=1,
                char_count=200,
                scanned_suspect=False,
                garbled_ratio=0.0,
                formula_warning=False,
                extracted_text="静电场与高斯定理详细推导" * 10,
            ),
            ParsedPage(
                page_no=2,
                char_count=200,
                scanned_suspect=False,
                garbled_ratio=0.0,
                formula_warning=False,
                extracted_text="电势与电容能量推导" * 10,
            ),
        ],
    )

    recorded_progress: list[tuple[str, str]] = []
    callbacks: list[tuple[str, str]] = []

    def on_step(step: str) -> None:
        items = service.list_conversation_progress(conv_id)
        assert len(items) == 1
        assert items[0].material_name == "静电场讲义.pdf"
        assert items[0].start_page == 1
        assert items[0].end_page == 2
        assert items[0].outputs == ["notes", "quiz"]
        recorded_progress.append((step, items[0].stage_label))

    service = MaterialService(
        mat_repo,
        config=MaterialConfigState(),
        materials_root=tmp_path / "materials",
        exampass_generator_factory=lambda: _RecordingExamPassGenerator(on_step),  # type: ignore[arg-type, return-value]
    )

    artifact = asyncio.run(
        service.generate_or_get_artifact(
            mat_id,
            start_page=1,
            end_page=2,
            outputs=["notes", "quiz"],
            conversation_id=conv_id,
            progress_callback=lambda stage, label: callbacks.append((stage, label)),
        )
    )
    assert artifact.notes_status == "ready"
    assert artifact.quiz_status == "ready"
    assert [s for s, _ in recorded_progress] == ["skeleton", "notes", "quiz", "review"]
    assert [s for s, _ in callbacks] == ["checking_text", "skeleton", "notes", "quiz", "review"]
    assert service.list_conversation_progress(conv_id) == []


@pytest.mark.parametrize("stage,stop", [
    ("generating_material", False),
    ("generating_material", True),
    ("scheduling", False),
])
def test_dispatch_stream_disconnect_and_explicit_stop(
    stage: str, stop: bool, monkeypatch: Any,
) -> None:
    from uuid import UUID

    async def run() -> None:
        conv_id = UUID("11111111-1111-4111-8111-111111111111")
        req_id = UUID("22222222-2222-4222-8222-222222222222")
        app_state = State()
        scope = {"type": "http", "app": SimpleNamespace(state=app_state), "asgi": {"spec_version": "2.3"}}
        disconnected = asyncio.Event()
        allow_result = asyncio.Event()
        completed = asyncio.Event()
        cancelled = asyncio.Event()

        async def receive() -> dict[str, str]:
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if stage.encode() in message.get("body", b""):
                disconnected.set()

        class Service:
            def _require_owned_conversation(self, conversation_id: str) -> None:
                assert conversation_id == str(conv_id)

            async def stream_prepared(self, prepared: Any, request: Request):
                try:
                    yield TurnProgressEvent(
                        conversation_id=prepared.conversation_id,
                        request_id=prepared.request_id,
                        stage=stage,
                        message="processing",
                    )
                    await allow_result.wait()
                    assert not await request.is_disconnected()
                    completed.set()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

        request = Request(scope, receive=receive)
        service = Service()
        owner = SimpleNamespace(id=TEST_USER_ID)
        state = ConversationServiceState(service=service, error=None)
        monkeypatch.setattr(
            "app.api.conversations._material_service",
            lambda *_: SimpleNamespace(list_conversation_progress=lambda _conv: []),
        )
        response = _stream_dispatch_turn(
            request, TEST_USER_ID, service,
            SimpleNamespace(conversation_id=str(conv_id), request_id=str(req_id)),
        )
        await asyncio.wait_for(response(scope, receive, send), timeout=2)
        key = (TEST_USER_ID, str(conv_id), str(req_id))
        if stage == "scheduling":
            assert cancelled.is_set()
            assert key not in _material_turn_tasks(request)
            return

        task = _material_turn_tasks(request)[key]
        assert not task.done()
        progress = await get_conversation_material_progress(conv_id, request, owner, state)
        assert progress.chat_turn_request_id == str(req_id)
        if stop:
            unknown_id = UUID("33333333-3333-4333-8333-333333333333")
            assert (await stop_material_turn(conv_id, unknown_id, request, owner, state)).status_code == 204
            assert not task.cancelling()
            result = await stop_material_turn(conv_id, req_id, request, owner, state)
            assert result.status_code == 204
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
            assert cancelled.is_set()
        else:
            allow_result.set()
            await asyncio.wait_for(task, timeout=2)
            assert completed.is_set()
        assert key not in _material_turn_tasks(request)
        progress = await get_conversation_material_progress(conv_id, request, owner, state)
        assert progress.chat_turn_request_id is None

    asyncio.run(run())


def test_ocr_reports_processed_and_failed_pages(tmp_path: Path, monkeypatch: Any) -> None:
    mat_repo, conv_id, mat_id = _setup_repo_and_material(tmp_path)
    mat_repo.save_parsed_pages(
        mat_id,
        [
            ParsedPage(
                page_no=1,
                char_count=0,
                scanned_suspect=True,
                garbled_ratio=0.0,
                formula_warning=False,
                extracted_text="",
            ),
            ParsedPage(
                page_no=2,
                char_count=0,
                scanned_suspect=True,
                garbled_ratio=0.0,
                formula_warning=False,
                extracted_text="",
            ),
        ],
    )
    seen: list[tuple[int, int, list[int]]] = []

    class _NoopUsageGate:
        def ensure_available(self, _owner_id: str) -> None:
            return None

    monkeypatch.setattr("app.materials.service.render_ocr_page_image", lambda *_args, **_kwargs: b"png")
    service = MaterialService(
        mat_repo,
        config=MaterialConfigState(),
        materials_root=tmp_path / "materials",
        usage_gate=_NoopUsageGate(),  # type: ignore[arg-type]
        ocr_factory=lambda: _MixedOcr(service, conv_id, seen),  # type: ignore[arg-type, return-value]
    )
    res = asyncio.run(service.recognize_pages(mat_id, [1, 2], conversation_id=conv_id))
    assert res.recognized_pages == [1]
    assert [f.page_no for f in res.failures] == [2]
    assert seen == [(0, 2, []), (1, 2, [])]
    assert service.list_conversation_progress(conv_id) == []


def test_notes_only_reports_independent_review_stage(tmp_path: Path, monkeypatch: Any) -> None:
    from app.config import DeepSeekSettings
    from app.materials.exampass_llm import DeepSeekExamPassGenerator

    mat_repo, conv_id, mat_id = _setup_repo_and_material(tmp_path)
    mat_repo.save_parsed_pages(
        mat_id,
        [
            ParsedPage(
                page_no=1,
                char_count=200,
                scanned_suspect=False,
                garbled_ratio=0.0,
                formula_warning=False,
                extracted_text="静电场与高斯定理详细推导" * 10,
            ),
        ],
    )

    gen = DeepSeekExamPassGenerator(
        DeepSeekSettings(api_key="test-key", base_url="https://example.com", model="deepseek-chat")
    )
    observed: list[tuple[str, str]] = []

    async def fake_invoke(*, tag: str, **_kwargs: Any) -> str:
        items = service.list_conversation_progress(conv_id)
        if items:
            observed.append((tag, items[0].stage_label))
        if tag == "exampass_skeleton":
            return '{"title": "静电场", "chapters": [{"id": "c1", "label": "静电场", "kcs": [{"id": "kc1", "label": "高斯定理", "source_refs": ["1"]}]}]}'
        if tag == "exampass_notes":
            return "<h2>静电场</h2><p>草稿</p>"
        if tag == "exampass_notes_review":
            return "<h2>静电场</h2><p>审校后笔记</p>"
        raise AssertionError(f"unexpected tag: {tag}")

    monkeypatch.setattr(gen, "_invoke", fake_invoke)

    callbacks: list[tuple[str, str]] = []
    service = MaterialService(
        mat_repo,
        config=MaterialConfigState(),
        materials_root=tmp_path / "materials",
        exampass_generator_factory=lambda: gen,
    )
    artifact = asyncio.run(
        service.generate_or_get_artifact(
            mat_id,
            start_page=1,
            end_page=1,
            outputs=["notes"],
            conversation_id=conv_id,
            progress_callback=lambda stage, label: callbacks.append((stage, label)),
        )
    )
    assert artifact.notes_status == "ready"
    assert ("exampass_notes", "生成章节笔记") in observed
    assert ("exampass_notes_review", "审校") in observed
    assert [s for s, _ in callbacks] == ["checking_text", "skeleton", "notes", "notes", "review"]
    assert service.list_conversation_progress(conv_id) == []
