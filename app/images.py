"""图片校验与本地存储。

安全边界：
- 只接受 JPEG/PNG/GIF/WebP，按真实内容识别，不信任文件名或声明 MIME；
- 用 Pillow 解码校验，限制单图大小、尺寸和总像素；
- 文件名由服务端生成，数据库只保存相对路径；
- 读取时解析到附件根目录内，拒绝越界。
"""

import hashlib
import os
import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.conversations.errors import (
    AttachmentNotFoundError,
    AttachmentStorageError,
    AttachmentTooLargeError,
    InvalidImageError,
    UnsupportedImageError,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ATTACHMENTS_ROOT = PROJECT_ROOT / "data" / "local" / "attachments"

MAX_IMAGE_BYTES = 6 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_IMAGES_PER_TURN = 4
MAX_PENDING_PER_CONVERSATION = 20
MAX_DIMENSION = 8192
MAX_PIXELS = 40_000_000
PENDING_TTL_SECONDS = 24 * 60 * 60

_FORMATS = {
    "JPEG": ("image/jpeg", "jpg"),
    "PNG": ("image/png", "png"),
    "GIF": ("image/gif", "gif"),
    "WEBP": ("image/webp", "webp"),
}


@dataclass(frozen=True)
class ValidatedImage:
    data: bytes
    mime: str
    extension: str
    width: int
    height: int
    sha256: str


def validate_image(data: bytes) -> ValidatedImage:
    """解码并校验图片；失败抛领域异常，不返回半成品。"""

    if not data:
        raise InvalidImageError("图片内容为空。")
    if len(data) > MAX_IMAGE_BYTES:
        raise AttachmentTooLargeError("单张图片不能超过 6 MiB。")

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                image_format = image.format
                width, height = image.size
                if image_format not in _FORMATS:
                    raise UnsupportedImageError(
                        "只支持 JPEG、PNG、GIF、WebP 图片。"
                    )
                if width <= 0 or height <= 0:
                    raise InvalidImageError("图片尺寸不合法。")
                if width > MAX_DIMENSION or height > MAX_DIMENSION:
                    raise InvalidImageError("图片单边不能超过 8192 像素。")
                if width * height > MAX_PIXELS:
                    raise InvalidImageError("图片总像素过大，请压缩后再试。")
                image.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidImageError("图片文件损坏或不是可解码的图片。") from exc
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise InvalidImageError("图片像素过大，已被拒绝。") from exc

    mime, extension = _FORMATS[image_format]
    return ValidatedImage(
        data=data,
        mime=mime,
        extension=extension,
        width=width,
        height=height,
        sha256=hashlib.sha256(data).hexdigest(),
    )


def save_attachment(
    root: Path,
    conversation_id: str,
    attachment_id: str,
    image: ValidatedImage,
) -> str:
    """原子写入图片，返回相对附件根目录的 stored_name。"""

    directory = root / conversation_id
    filename = f"{attachment_id}.{image.extension}"
    target = directory / filename
    temp = directory / f".{attachment_id}.tmp"
    stored_name = f"{conversation_id}/{filename}"

    try:
        directory.mkdir(parents=True, exist_ok=True)
        with temp.open("wb") as handle:
            handle.write(image.data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except OSError as exc:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise AttachmentStorageError("图片保存失败，请稍后再试。") from exc

    return stored_name


def resolve_attachment(root: Path, stored_name: str) -> Path:
    """把相对 stored_name 解析为根目录内的真实文件。"""

    base = root.resolve()
    candidate = (base / stored_name).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise AttachmentNotFoundError("图片不存在。") from exc
    if not candidate.is_file():
        raise AttachmentNotFoundError("图片不存在。")
    return candidate


def delete_attachment(root: Path, stored_name: str) -> None:
    """删除文件；文件已不存在时忽略。"""

    try:
        resolve_attachment(root, stored_name).unlink()
    except AttachmentNotFoundError:
        return
    except OSError:
        # 清理失败不应阻止启动或请求；保留行，下次再试。
        return
