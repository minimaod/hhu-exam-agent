"""生成时的输入记录与各成果实际引用的页码。"""

import hashlib
import html
import json
import re

from app.materials.exampass_adapter import parse_source_pages
from app.materials.models import (
    ArtifactCoverage,
    ArtifactOutputCoverage,
    OutputKind,
    QuizQuestionPublicView,
)
from app.materials.ocr import describe_page
from app.materials.pdf_parser import ParsedPage


def bind_inputs(
    rule: str, pages: list[ParsedPage]
) -> tuple[str, ArtifactCoverage, list[ParsedPage]]:
    snapshot = [describe_page(p) for p in pages]
    supplied = [page for page, state in zip(pages, snapshot) if state.status == "usable"]
    inputs = [
        (page.page_no, state.version, page.extracted_text, state.needs_review)
        for page, state in zip(pages, snapshot) if state.status in ("usable", "excluded")
    ]
    digest = hashlib.sha256(json.dumps(inputs, ensure_ascii=False).encode("utf-8")).hexdigest()
    missing = [p.page_no for p in snapshot if p.status in ("pending_ocr", "ocr_failed", "unchecked")]
    coverage = ArtifactCoverage(
        input_pages=snapshot,
        included_pages=[p.page_no for p in supplied],
        missing_pages=missing,
        review_pages=[p.page_no for p in snapshot if p.needs_review],
        excluded_pages=[p.page_no for p in snapshot if p.status == "excluded"],
        partial=bool(missing),
    )
    return f"{rule}:input:{digest}", coverage, supplied


def _plain(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def _notes_sections(notes: str) -> dict[str, set[int]]:
    sections: dict[str, set[int]] = {}
    for part in re.split(r"(?=<h[23]\b)", notes):
        heading = re.match(r"<h3\b([^>]*)>(.*?)</h3>(.*)", part, flags=re.S)
        if not heading or not _plain(heading[3]):
            continue
        kc_id = re.search(r'\bdata-kc="([^"]+)"', heading[1])
        label = kc_id[1] if kc_id else _plain(heading[2])
        raw_refs = re.findall(r'\bdata-slides="([^"]+)"', part)
        sections.setdefault(label, set()).update(
            parse_source_pages(raw_refs, 10000)
        )
    return sections


def describe_outputs(
    coverage: ArtifactCoverage | None,
    skeleton: dict | None,
    notes_html: str,
    questions: list[QuizQuestionPublicView],
    notes_status: str,
    quiz_status: str,
    graph_status: str,
) -> dict[OutputKind, ArtifactOutputCoverage]:
    if coverage is None or skeleton is None:
        return {}
    allowed = set(coverage.included_pages)
    end_page = max(allowed, default=0)
    points = [kc for ch in skeleton.get("chapters", []) for kc in ch.get("kcs", [])]
    result: dict[OutputKind, ArtifactOutputCoverage] = {}
    if not points:
        return result
    sections = _notes_sections(notes_html)
    tested = {q.kc_id for q in questions}
    for kind, status in (("notes", notes_status), ("quiz", quiz_status), ("graph", graph_status)):
        if status != "ready":
            continue
        missing: list[str] = []
        cited: set[int] = set()
        for kc in points:
            expected = set(parse_source_pages(kc.get("source_refs"), end_page)) & allowed
            if kind == "notes":
                actual = sections.get(kc["id"], sections.get(kc["label"], set())) & expected
            elif kind == "quiz":
                actual = {
                    page for q in questions if q.kc_id == kc["id"] for page in q.source_pages
                } & expected
            else:
                actual = expected
            if kind == "quiz" and kc["id"] not in tested and kc.get("importance") == "info":
                continue
            if not actual:
                missing.append(kc["label"])
            cited.update(actual)
        uncited = sorted(allowed - cited)
        result[kind] = ArtifactOutputCoverage(
            cited_pages=sorted(cited),
            missing_knowledge_points=missing,
            uncited_pages=uncited,
            partial=coverage.partial or bool(missing) or (kind != "quiz" and bool(uncited)),
        )
    return result
