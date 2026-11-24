"""PDF 整页渲染与幻灯片缓存模块。

来源与许可说明：
- 核心渲染与压缩思路复用自 WUBING2023/ExamPass-Assistant
  (commit 3812643baf5de8b92bac17fbb4e136adcc19c41b, `scripts/slide_renderer.py`)，
  原项目采用 CC BY-NC 4.0 许可证（仅限个人、教育与非商业用途）。
- 本站适配修改：改为按 `material_id` 缓存 JPEG 文件至受控目录，使用原子写入与
  路径校验，配合后端鉴权接口按需提供单页图像，不在 HTML 中内嵌巨型 base64。
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import uuid
from pathlib import Path

from PIL import Image

from app.materials.errors import MaterialInvalidError


def slides_dir_for_material(materials_root: Path, material_id: str) -> Path:
    safe_id = Path(material_id).name
    if not safe_id or safe_id in {".", ".."} or safe_id != material_id:
        raise ValueError("非法的资料标识。")
    root = materials_root.resolve()
    target = (root / "slides" / safe_id).resolve()
    target.relative_to(root)
    return target


def render_pdf_page_image(
    pdf_path: Path,
    slide_dir: Path,
    *,
    page_no: int,
    dpi: int = 120,
    max_width: int = 1000,
    quality: int = 76,
) -> Path:
    """将指定 1-based PDF 页面渲染并缓存为 JPEG 图像，返回缓存文件路径。"""

    if page_no < 1:
        raise ValueError("页码必须从 1 开始。")

    slide_dir.mkdir(parents=True, exist_ok=True)
    target = slide_dir / f"p{page_no}.jpg"
    if target.is_file() and target.stat().st_size > 0:
        return target

    import pymupdf

    doc = pymupdf.open(str(pdf_path))
    try:
        if page_no > len(doc):
            raise ValueError(f"页码超出范围（共 {len(doc)} 页）。")
        page = doc[page_no - 1]
        zoom = dpi / 72.0
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    finally:
        doc.close()

    if img.width > max_width:
        new_h = max(1, int(img.height * max_width / img.width))
        img = img.resize((max_width, new_h), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    raw = buf.getvalue()

    tmp_path = slide_dir / f".p{page_no}.{uuid.uuid4().hex}.tmp"
    try:
        tmp_path.write_bytes(raw)
        os.replace(tmp_path, target)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
    return target


def render_ocr_page_image(pdf_path: Path, page_no: int, *, timeout_seconds: float) -> bytes:
    """在隔离进程中渲染一页，识别用图片只保留在内存。"""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "app.materials.slide_renderer", str(pdf_path), str(page_no)],
            cwd=Path(__file__).resolve().parents[2],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
            check=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise MaterialInvalidError("渲染原页超时，请重试。") from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        raise MaterialInvalidError("无法渲染该原页。") from exc
    if not result.stdout.startswith(b"\xff\xd8"):
        raise MaterialInvalidError("原页渲染结果异常。")
    return result.stdout


def _ocr_worker(pdf_path: Path, page_no: int) -> None:
    import pymupdf

    with pymupdf.open(pdf_path) as doc:
        if page_no < 1 or page_no > len(doc):
            raise ValueError("物理页码越界")
        page = doc[page_no - 1]
        zoom = min(200 / 72, 2400 / max(page.rect.width, page.rect.height, 1))
        raster = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        sys.stdout.buffer.write(raster.tobytes("jpeg", jpg_quality=90))


if __name__ == "__main__":
    _ocr_worker(Path(sys.argv[1]), int(sys.argv[2]))
