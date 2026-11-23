"""隔离 PDF 解析、可读性与引用校验测试。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.materials.citation import verify_citations
from app.materials.errors import (
    MaterialInvalidError,
    MaterialPageLimitError,
    MaterialParseTimeoutError,
)
from app.materials.pdf_parser import (
    build_readability_report,
    run_isolated_pdf_parse,
    validate_pdf_header,
)


def _make_simple_pdf(page_texts: list[str]) -> bytes:
    """构造最小有效的多页 PDF（无外部字体生成依赖）。"""

    objects: list[bytes] = []
    # 1: Catalog, 2: Pages, 3: Font
    page_obj_ids: list[int] = []
    next_id = 4
    for _ in page_texts:
        page_obj_ids.append(next_id)
        next_id += 2  # page + content

    kids_ref = " ".join(f"{pid} 0 R" for pid in page_obj_ids)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids_ref}] /Count {len(page_texts)} >>".encode("ascii"))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for idx, text in enumerate(page_texts):
        pid = page_obj_ids[idx]
        cid = pid + 1
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1", errors="replace")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {cid} 0 R >>".encode("ascii")
        )
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = [0]
    for obj_id, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(f"{obj_id} 0 obj\n".encode("ascii"))
        out.extend(body)
        out.extend(b"\nendobj\n")

    xref_pos = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode(
            "ascii"
        )
    )
    return bytes(out)


def test_validate_pdf_header_rejects_non_pdf() -> None:
    with pytest.raises(MaterialInvalidError):
        validate_pdf_header(b"not a pdf at all")


def test_isolated_pdf_parse_extracts_pages_and_detects_scanned(tmp_path: Path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(
        _make_simple_pdf(
            [
                "Chapter 1 Limit and Continuity. Theorem: squeeze theorem holds when f(x) <= g(x) <= h(x) and limits match.",
                "",  # 空白/扫描页
                "Definition of derivative: limit of (f(x+h)-f(x))/h as h approaches 0. Must check left and right limits.",
            ]
        )
    )

    parsed = run_isolated_pdf_parse(pdf_path, max_pages=80, start_page=1, end_page=3)
    assert parsed.page_count == 3
    assert len(parsed.pages) == 3
    assert parsed.pages[0].scanned_suspect is False
    assert parsed.pages[1].scanned_suspect is True
    assert parsed.pages[2].scanned_suspect is False

    report = build_readability_report("m1", 1, 3, parsed.pages)
    assert report.sufficient is True
    assert [p.page_no for p in report.affected_pages] == [2]


def test_isolated_pdf_parse_rejects_too_many_pages(tmp_path: Path) -> None:
    pdf_path = tmp_path / "many.pdf"
    pdf_path.write_bytes(_make_simple_pdf(["page text"] * 5))
    with pytest.raises(MaterialPageLimitError):
        run_isolated_pdf_parse(pdf_path, max_pages=3)


def test_isolated_pdf_parse_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import time

    pdf_path = tmp_path / "timeout.pdf"
    pdf_path.write_bytes(_make_simple_pdf(["hello world"]))

    real_popen = subprocess.Popen

    def _silent_blocking_popen(cmd, **kwargs):
        return real_popen(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            **kwargs,
        )

    monkeypatch.setattr(subprocess, "Popen", _silent_blocking_popen)
    start = time.monotonic()
    with pytest.raises(MaterialParseTimeoutError):
        run_isolated_pdf_parse(pdf_path, max_pages=80, timeout_seconds=0.12)
    elapsed = time.monotonic() - start
    assert elapsed < 0.6


def test_verify_citations_filters_forged_and_out_of_range() -> None:
    page_texts = {
        2: "Theorem 1: Every bounded monotonic sequence converges to a finite limit.",
        3: "Example: Compute the limit of (1 + 1/n)^n as n goes to infinity.",
    }
    raw = [
        {"page_no": 2, "quote": "bounded monotonic sequence converges"},
        {"page_no": 3, "quote": "limit of (1 + 1/n)^n"},
        {"page_no": 1, "quote": "bounded monotonic"},  # 超出 start_page..end_page
        {"page_no": 4, "quote": "limit of (1 + 1/n)^n"},  # 超出范围
        {"page_no": 2, "quote": "completely fabricated sentence not on page 2"},  # 伪造引文
        {"page_no": "2", "quote": "bounded monotonic sequence"},  # 非整数页码
    ]
    verified = verify_citations(raw, page_texts=page_texts, start_page=2, end_page=3)
    assert [(v.page_no, v.verified) for v in verified] == [(2, True), (3, True)]


def test_isolated_pdf_parse_works_without_project_root_in_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    pdf_path = tmp_path / "no_root_pythonpath.pdf"
    pdf_path.write_bytes(
        _make_simple_pdf(["Chapter 1 Limit squeeze theorem holds without root in PYTHONPATH."])
    )
    project_root = str(Path(__file__).resolve().parents[1]).lower()
    raw_pp = os.environ.get("PYTHONPATH", "")
    kept = [
        p for p in raw_pp.split(os.pathsep) if p and str(Path(p).resolve()).lower() != project_root
    ]
    if kept:
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join(kept))
    else:
        monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.chdir(tmp_path)

    parsed = run_isolated_pdf_parse(pdf_path, max_pages=80, start_page=1, end_page=1)
    assert parsed.page_count == 1
    assert "squeeze theorem" in parsed.pages[0].extracted_text
