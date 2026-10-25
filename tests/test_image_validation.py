"""图片内容校验与会话归属、清理规则。"""

import sqlite3
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

import app.images as images
from app.conversations.errors import (
    AttachmentNotFoundError,
    AttachmentTooLargeError,
    InvalidImageError,
    UnsupportedImageError,
)
from app.conversations.repository import ConversationRepository
from app.images import (
    delete_attachment,
    resolve_attachment,
    save_attachment,
    validate_image,
)
from tests.tutoring_fakes import jpeg_bytes, png_bytes


def test_real_formats_are_detected_by_content() -> None:
    assert validate_image(png_bytes()).mime == "image/png"
    assert validate_image(jpeg_bytes()).mime == "image/jpeg"

    gif = BytesIO()
    Image.new("RGB", (20, 10), "white").save(gif, "GIF")
    assert validate_image(gif.getvalue()).mime == "image/gif"

    webp = BytesIO()
    Image.new("RGB", (20, 10), "white").save(webp, "WEBP")
    assert validate_image(webp.getvalue()).mime == "image/webp"


def test_unsupported_and_corrupt_images_are_rejected() -> None:
    bmp = BytesIO()
    Image.new("RGB", (10, 10), "white").save(bmp, "BMP")
    with pytest.raises(UnsupportedImageError):
        validate_image(bmp.getvalue())

    with pytest.raises(InvalidImageError):
        validate_image(b"not an image")
    with pytest.raises(InvalidImageError):
        validate_image(b"\x89PNG\r\n\x1a\n" + b"garbage")


def test_size_and_dimension_limits_are_enforced() -> None:
    with pytest.raises(AttachmentTooLargeError):
        validate_image(b"x" * (6 * 1024 * 1024 + 1))

    wide = BytesIO()
    Image.new("RGB", (9000, 10), "white").save(wide, "PNG")
    with pytest.raises(InvalidImageError):
        validate_image(wide.getvalue())


def test_saved_attachment_is_resolved_and_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(images, "ATTACHMENTS_ROOT", tmp_path / "attachments")
    image = validate_image(png_bytes())
    stored = save_attachment(
        images.ATTACHMENTS_ROOT, "conversation-1", "attachment-1", image
    )
    path = resolve_attachment(images.ATTACHMENTS_ROOT, stored)
    assert path.is_file()
    assert path.read_bytes() == image.data
    assert "conversation-1" in stored and ".png" in stored

    delete_attachment(images.ATTACHMENTS_ROOT, stored)
    with pytest.raises(AttachmentNotFoundError):
        resolve_attachment(images.ATTACHMENTS_ROOT, stored)


def test_pending_attachment_cleanup_only_removes_old_unused(
    tmp_path: Path,
) -> None:
    repository = ConversationRepository(tmp_path / "study.sqlite3")
    repository.initialize()
    conversation = repository.create_conversation()
    attachment = repository.create_attachment(
        conversation_id=conversation.id,
        attachment_id="pending-old",
        stored_name=f"{conversation.id}/pending-old.png",
        mime="image/png",
        width=10,
        height=10,
        size_bytes=10,
        sha256="0" * 64,
    )
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    with sqlite3.connect(repository.db_path) as connection:
        connection.execute(
            "UPDATE attachments SET created_at = ? WHERE id = ?",
            (old, attachment.id),
        )
        connection.commit()

    removed = repository.cleanup_pending_attachments(
        (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    )
    assert removed == [attachment.stored_name]
    assert repository.get_attachment(conversation.id, attachment.id) is None
