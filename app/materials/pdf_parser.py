"""隔离的 PDF 解析与可读性分析（基于 pypdf）。

安全与资源边界：
- 先校验 %PDF- 魔数；
- 在独立子进程中运行 pypdf 读取，强制超时（默认 15s）与输出上限，
  防止恶意构造或超大 PDF 阻塞或耗尽主服务进程内存；
- 支持按需提取指定物理页范围（1-based），返回扫描页、乱码与公式告警。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path

from app.materials.errors import (
    MaterialInvalidError,
    MaterialPageLimitError,
    MaterialParseTimeoutError,
)
from app.materials.models import PageReadability, ReadabilityReport

# 保留正文来源标识。缓存版本单独升级，重读后正文未变的成果仍可复用。
PARSER_VERSION = "pypdf-v1"
PDF_TEXT_CACHE_VERSION = "pypdf-v2"
MAX_PAGE_TEXT_CHARS = 50_000
MAX_WORKER_STDOUT_BYTES = 12 * 1024 * 1024
# Linux 备用解析器还会加载本地库，计入同一进程的地址空间上限。
MAX_WORKER_MEMORY_BYTES = 640 * 1024 * 1024
MAX_CONCURRENT_PARSES = 2
_PARSE_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_PARSES)
_SCANNED_CHAR_THRESHOLD = 25
_GARBLED_RATIO_THRESHOLD = 0.15
_MIN_SUFFICIENT_CHARS = 120

_FORMULA_FRAGMENT_RE = re.compile(r"(?:\b[a-zA-Z]\s+){5,}")
_REPLACEMENT_RUN_RE = re.compile(r"[\ufffd□■]{2,}")
_HAN_RE = re.compile(r"[\u3400-\u9fff\U00020000-\U000323af]")


@dataclass(frozen=True)
class ParsedPage:
    page_no: int
    extracted_text: str
    char_count: int
    scanned_suspect: bool
    garbled_ratio: float
    formula_warning: bool


@dataclass(frozen=True)
class ParsedPdfResult:
    page_count: int
    pages: list[ParsedPage]


def validate_pdf_header(data: bytes) -> None:
    if not data or b"%PDF-" not in data[:1024]:
        raise MaterialInvalidError("仅支持有效的 PDF 文件（缺少 PDF 文件头）。")


def _sanitize_extracted_text(raw_text: str) -> tuple[str, bool]:
    """清理 PDF 提取文本中的孤立 UTF-16 代理字符，防止 UTF-8 编码崩溃。"""

    had_surrogates = False
    chars: list[str] = []
    for ch in raw_text[:MAX_PAGE_TEXT_CHARS]:
        code = ord(ch)
        if 0xD800 <= code <= 0xDFFF:
            had_surrogates = True
            chars.append("\ufffd")
        else:
            chars.append(ch)
    return "".join(chars), had_surrogates


def _analyze_page_text(page_no: int, raw_text: str) -> ParsedPage:
    text, had_surrogates = _sanitize_extracted_text(raw_text)
    compact = "".join(text.split())
    char_count = len(compact)
    scanned_suspect = char_count < _SCANNED_CHAR_THRESHOLD

    if char_count == 0:
        garbled_ratio = 0.0
    else:
        bad_chars = sum(
            1
            for ch in compact
            if ch == "\ufffd"
            or (ord(ch) < 32 and ch not in "\t\n\r")
            or (0xE000 <= ord(ch) <= 0xF8FF)
        )
        garbled_ratio = round(bad_chars / char_count, 4)

    formula_warning = bool(
        had_surrogates
        or garbled_ratio > 0.03
        or _FORMULA_FRAGMENT_RE.search(text)
        or _REPLACEMENT_RUN_RE.search(text)
    )
    return ParsedPage(
        page_no=page_no,
        extracted_text=text,
        char_count=char_count,
        scanned_suspect=scanned_suspect,
        garbled_ratio=garbled_ratio,
        formula_warning=formula_warning,
    )


def _has_unmapped_identity_font(page) -> bool:
    resources = page.get("/Resources")
    fonts = resources.get_object().get("/Font") if resources else None
    if not fonts:
        return False
    for reference in fonts.get_object().values():
        font = reference.get_object()
        if (
            font.get("/Subtype") == "/Type0"
            and font.get("/Encoding") in ("/Identity-H", "/Identity-V")
            and not font.get("/ToUnicode")
        ):
            return True
    return False


def _extract_in_worker(
    pdf_path: Path,
    *,
    max_pages: int,
    start_page: int | None = None,
    end_page: int | None = None,
) -> dict[str, object]:
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path), strict=False)
    if reader.is_encrypted:
        # 尝试空密码解密（部分只读权限 PDF）
        try:
            if reader.decrypt("") == 0:
                return {"error": "invalid", "message": "暂不支持加密或受密码保护的 PDF。"}
        except Exception:
            return {"error": "invalid", "message": "暂不支持加密或受密码保护的 PDF。"}

    total_pages = len(reader.pages)
    if total_pages < 1:
        return {"error": "invalid", "message": "PDF 文件不包含任何页面。"}
    if total_pages > max_pages:
        return {
            "error": "page_limit",
            "message": f"PDF 总页数为 {total_pages} 页，超过上限（最多 {max_pages} 页）。",
        }

    pages_out: list[dict[str, object]] = []
    if start_page is not None and end_page is not None:
        s = max(1, start_page)
        e = min(total_pages, end_page)
        with ExitStack() as stack:
            alternate_pdf = None
            for pno in range(s, e + 1):
                page = reader.pages[pno - 1]
                try:
                    raw = page.extract_text() or ""
                except Exception:
                    raw = ""
                analyzed = _analyze_page_text(pno, raw)
                try:
                    if _has_unmapped_identity_font(page):
                        if alternate_pdf is None:
                            import pymupdf

                            alternate_pdf = stack.enter_context(pymupdf.open(pdf_path))
                        alternate = _analyze_page_text(pno, alternate_pdf[pno - 1].get_text())
                        if (
                            not alternate.scanned_suspect
                            and alternate.garbled_ratio <= _GARBLED_RATIO_THRESHOLD
                            and len(_HAN_RE.findall(alternate.extracted_text))
                            > len(_HAN_RE.findall(analyzed.extracted_text))
                        ):
                            analyzed = replace(alternate, formula_warning=True)
                except Exception:
                    # 备用解析器不可用时，保留已有的文字提取结果。
                    pass
                pages_out.append(
                    {
                        "page_no": analyzed.page_no,
                        "extracted_text": analyzed.extracted_text,
                        "char_count": analyzed.char_count,
                        "scanned_suspect": analyzed.scanned_suspect,
                        "garbled_ratio": analyzed.garbled_ratio,
                        "formula_warning": analyzed.formula_warning,
                    }
                )
    return {"page_count": total_pages, "pages": pages_out}


def run_isolated_pdf_parse(
    pdf_path: Path,
    *,
    max_pages: int,
    start_page: int | None = None,
    end_page: int | None = None,
    timeout_seconds: float = 15.0,
) -> ParsedPdfResult:
    """在独立子进程中解析 PDF 页数或指定物理页文本（限并发、流式输出上限与超时）。"""

    project_root = Path(__file__).resolve().parents[2]
    cmd = [
        sys.executable,
        "-m",
        "app.materials.pdf_parser",
        str(pdf_path),
        str(max_pages),
        str(start_page or 0),
        str(end_page or 0),
    ]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # 子进程逐页解析，无需本地依赖额外预留并行线程和分配区。
    env["OPENBLAS_NUM_THREADS"] = "1"
    env["OMP_NUM_THREADS"] = "1"
    env["MALLOC_ARENA_MAX"] = "2"

    with _PARSE_SEMAPHORE:
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(project_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
            )
        except OSError as exc:
            raise MaterialInvalidError("无法启动 PDF 解析器，请稍后再试。") from exc

        timed_out = threading.Event()

        def _on_timeout() -> None:
            timed_out.set()
            try:
                proc.kill()
            except OSError:
                pass

        timer = threading.Timer(timeout_seconds, _on_timeout)
        timer.daemon = True
        timer.start()

        chunks: list[bytes] = []
        total_bytes = 0
        try:
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_WORKER_STDOUT_BYTES:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    proc.wait()
                    raise MaterialInvalidError("PDF 提取文本过大，请缩小页码范围。")
                chunks.append(chunk)

            returncode = proc.wait()
        finally:
            timer.cancel()
            if proc.stdout:
                proc.stdout.close()

        if timed_out.is_set():
            if start_page is not None and end_page is not None:
                page_hint = (
                    f"第 {start_page} 页"
                    if start_page == end_page
                    else f"第 {start_page}–{end_page} 页"
                )
                raise MaterialParseTimeoutError(
                    f"{page_hint}解析超时，已保留原件，请缩小页码范围或稍后重试。"
                )
            raise MaterialParseTimeoutError(
                "PDF 解析超时，请缩小页码范围或更换文件后重试。"
            )

    if returncode != 0:
        raise MaterialInvalidError("PDF 文件损坏或无法解析。")

    stdout_bytes = b"".join(chunks)
    try:
        payload = json.loads(stdout_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise MaterialInvalidError("PDF 解析结果格式异常。") from exc

    error_kind = payload.get("error")
    if error_kind == "page_limit":
        raise MaterialPageLimitError(str(payload.get("message") or "PDF 页数超出限制。"))
    if error_kind:
        raise MaterialInvalidError(str(payload.get("message") or "PDF 文件无效。"))

    pages = [
        ParsedPage(
            page_no=int(item["page_no"]),
            extracted_text=str(item["extracted_text"]),
            char_count=int(item["char_count"]),
            scanned_suspect=bool(item["scanned_suspect"]),
            garbled_ratio=float(item["garbled_ratio"]),
            formula_warning=bool(item["formula_warning"]),
        )
        for item in payload.get("pages", [])
    ]
    return ParsedPdfResult(
        page_count=int(payload["page_count"]),
        pages=pages,
    )


def build_readability_report(
    material_id: str,
    start_page: int,
    end_page: int,
    pages: list[ParsedPage],
) -> ReadabilityReport:
    affected: list[PageReadability] = []
    total_chars = 0
    usable_pages = 0

    for page in pages:
        warnings: list[str] = []
        if page.scanned_suspect:
            warnings.append("疑似扫描页或空白页（可提取文字过少）")
        if page.garbled_ratio > _GARBLED_RATIO_THRESHOLD:
            warnings.append("乱码字符比例较高")
        if page.formula_warning:
            warnings.append("部分公式或特殊符号可能提取不完整")

        if not page.scanned_suspect and page.garbled_ratio <= _GARBLED_RATIO_THRESHOLD:
            usable_pages += 1
            total_chars += page.char_count

        if warnings:
            affected.append(
                PageReadability(
                    page_no=page.page_no,
                    char_count=page.char_count,
                    scanned_suspect=page.scanned_suspect,
                    garbled_ratio=page.garbled_ratio,
                    formula_warning=page.formula_warning,
                    warnings=warnings,
                )
            )

    sufficient = usable_pages > 0 and total_chars >= _MIN_SUFFICIENT_CHARS
    if not sufficient:
        reason = "选定页码范围内有效文本不足（可能为扫描版或乱码页），无法可靠生成复习清单。"
        recommendation = "请缩小到包含清晰文字层的页码范围，或更换可复制文本的 PDF 文件。"
    elif affected:
        affected_nums = "、".join(str(p.page_no) for p in affected)
        reason = f"第 {affected_nums} 页存在扫描页或公式提取告警，生成时将仅依据可提取文本并标注限制。"
        recommendation = "建议生成后点击页码回看原件核对公式细节。"
    else:
        reason = "选定范围文字层清晰，可直接生成复习清单。"
        recommendation = None

    return ReadabilityReport(
        material_id=material_id,
        start_page=start_page,
        end_page=end_page,
        sufficient=sufficient,
        reason=reason,
        recommendation=recommendation,
        total_chars=total_chars,
        affected_pages=affected,
    )


def _apply_worker_limits() -> None:
    try:
        import resource  # type: ignore[import-not-found]

        resource.setrlimit(
            resource.RLIMIT_AS,
            (MAX_WORKER_MEMORY_BYTES, MAX_WORKER_MEMORY_BYTES),
        )
    except Exception:
        pass


def _worker_main() -> int:
    if len(sys.argv) < 5:
        return 2
    _apply_worker_limits()
    pdf_path = Path(sys.argv[1])
    max_pages = int(sys.argv[2])
    start_page = int(sys.argv[3]) or None
    end_page = int(sys.argv[4]) or None
    try:
        result = _extract_in_worker(
            pdf_path,
            max_pages=max_pages,
            start_page=start_page,
            end_page=end_page,
        )
    except Exception as exc:
        result = {"error": "invalid", "message": f"PDF 解析失败：{exc.__class__.__name__}"}
    encoded = json.dumps(result, ensure_ascii=False).encode("utf-8", errors="replace")
    sys.stdout.buffer.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(_worker_main())
