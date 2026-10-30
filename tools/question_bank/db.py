"""SQLite 事实库 schema、内容哈希与导入。

事实库只保存题目事实和审核状态，不保存向量/BM25；索引损坏时可以只重建
索引。`content_sha256` 是复核决定的绑定对象：重抽取若改变题干/选项/答案/
页码等内容，旧决定会自动变成 stale，而不是继续生效。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from app.question_bank.config import CONTENT_BUDGET, SCHEMA_VERSION

SCHEMA = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE papers (
    paper_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    course TEXT NOT NULL,
    exam_type TEXT,
    academic_year TEXT,
    exam_date TEXT,
    pdf_rel_path TEXT,
    pdf_sha256 TEXT,
    physical_pages INTEGER
);

CREATE TABLE questions (
    question_id TEXT PRIMARY KEY,
    paper_id TEXT NOT NULL REFERENCES papers(paper_id),
    course TEXT NOT NULL,
    section_code TEXT NOT NULL,
    section_title TEXT NOT NULL,
    number_label TEXT NOT NULL,
    question_type TEXT NOT NULL,
    statement TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    automated_status TEXT NOT NULL,
    visual_status TEXT NOT NULL,
    review_status TEXT NOT NULL,
    content_status TEXT NOT NULL,
    content_issue TEXT,
    searchable INTEGER NOT NULL,
    answer_state TEXT NOT NULL,
    answer_available INTEGER NOT NULL,
    answer_label TEXT,
    answer_json TEXT,
    pages_statement_json TEXT NOT NULL,
    pages_answer_json TEXT NOT NULL,
    reference_code TEXT,
    reference_code_pages_json TEXT,
    raw_json TEXT NOT NULL
);
CREATE INDEX idx_questions_course ON questions(course, searchable);
CREATE INDEX idx_questions_paper ON questions(paper_id);
CREATE INDEX idx_questions_type ON questions(question_type);

CREATE TABLE options (
    question_id TEXT NOT NULL REFERENCES questions(question_id),
    ordinal INTEGER NOT NULL,
    label TEXT NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (question_id, ordinal)
);

CREATE TABLE code_lines (
    question_id TEXT NOT NULL REFERENCES questions(question_id),
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (question_id, ordinal)
);

CREATE TABLE blank_marks (
    question_id TEXT NOT NULL REFERENCES questions(question_id),
    ordinal INTEGER NOT NULL,
    blank_no INTEGER NOT NULL,
    PRIMARY KEY (question_id, ordinal)
);

CREATE TABLE formulas (
    question_id TEXT NOT NULL REFERENCES questions(question_id),
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    page TEXT,
    PRIMARY KEY (question_id, ordinal)
);

CREATE TABLE assets (
    question_id TEXT NOT NULL REFERENCES questions(question_id),
    ordinal INTEGER NOT NULL,
    asset_id TEXT NOT NULL,
    page INTEGER,
    bbox_json TEXT NOT NULL,
    description TEXT,
    transcription TEXT,
    file_rel_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY (question_id, ordinal)
);

CREATE TABLE code_blocks (
    question_id TEXT NOT NULL REFERENCES questions(question_id),
    ordinal INTEGER NOT NULL,
    block_id TEXT,
    method INTEGER,
    method_label TEXT,
    column INTEGER,
    kind TEXT,
    name TEXT,
    text TEXT NOT NULL,
    page INTEGER,
    pages_json TEXT NOT NULL,
    depends_on_json TEXT NOT NULL,
    bbox_json TEXT,
    PRIMARY KEY (question_id, ordinal)
);

CREATE TABLE code_annotations (
    question_id TEXT NOT NULL REFERENCES questions(question_id),
    ordinal INTEGER NOT NULL,
    role TEXT,
    text TEXT NOT NULL,
    page INTEGER,
    bbox_json TEXT,
    PRIMARY KEY (question_id, ordinal)
);

CREATE TABLE code_examples (
    question_id TEXT NOT NULL REFERENCES questions(question_id),
    ordinal INTEGER NOT NULL,
    label TEXT,
    text TEXT NOT NULL,
    page INTEGER,
    PRIMARY KEY (question_id, ordinal)
);

CREATE TABLE independent_solutions (
    question_id TEXT NOT NULL REFERENCES questions(question_id),
    ordinal INTEGER NOT NULL,
    author TEXT,
    text TEXT NOT NULL,
    note TEXT,
    PRIMARY KEY (question_id, ordinal)
);

CREATE TABLE answer_sources (
    question_id TEXT NOT NULL REFERENCES questions(question_id),
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL,
    text TEXT,
    detail TEXT,
    pairing_verified INTEGER,
    pages_json TEXT NOT NULL,
    PRIMARY KEY (question_id, ordinal)
);

CREATE TABLE knowledge_points (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    definition TEXT NOT NULL,
    aliases_json TEXT NOT NULL,
    sort_order INTEGER NOT NULL
);

CREATE TABLE question_knowledge_points (
    question_id TEXT NOT NULL REFERENCES questions(question_id),
    kp_slug TEXT NOT NULL REFERENCES knowledge_points(slug),
    ordinal INTEGER NOT NULL,
    status TEXT NOT NULL,
    basis TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    PRIMARY KEY (question_id, kp_slug)
);
CREATE INDEX idx_qkp_slug ON question_knowledge_points(kp_slug, question_id);

CREATE TABLE retrieval_texts (
    question_id TEXT PRIMARY KEY REFERENCES questions(question_id),
    text TEXT NOT NULL,
    text_sha256 TEXT NOT NULL,
    fragment_count INTEGER NOT NULL
);

CREATE TABLE fragments (
    fragment_id TEXT PRIMARY KEY,
    question_id TEXT NOT NULL REFERENCES questions(question_id),
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    token_count INTEGER NOT NULL
);
CREATE INDEX idx_fragments_question ON fragments(question_id);
"""


def content_payload(record: dict[str, Any]) -> dict[str, Any]:
    """参与 content_sha256 的稳定投影；排除校录/审核/副本等过程字段。"""

    answer = record.get("answer") or {}
    return {
        "question_id": record["question_id"],
        "paper_id": record["paper_id"],
        "course": record["course"],
        "section": record.get("section"),
        "number_label": record.get("number_label"),
        "question_type": record.get("question_type"),
        "statement": record.get("statement"),
        "options": record.get("options") or [],
        "code_lines": record.get("code_lines") or [],
        "blank_marks": record.get("blank_marks") or [],
        "pages": {
            "statement": (record.get("pages") or {}).get("statement_all")
            or (record.get("pages") or {}).get("statement")
            or [],
            "answer": (record.get("pages") or {}).get("answer") or [],
        },
        "answer": {
            "state": answer.get("state"),
            "text": answer.get("text"),
            "note": answer.get("note"),
            "origin": answer.get("origin"),
            "label": answer.get("label"),
            "sources": answer.get("sources") or [],
        },
        "independent_solutions": record.get("independent_solutions") or [],
        "label_corrections": record.get("label_corrections") or [],
        "header_notes": record.get("header_notes") or [],
        "formulas": [
            {"text": item.get("text"), "page": item.get("page")}
            for item in record.get("formulas") or []
        ],
        "assets": [
            {
                "kind": item.get("kind"),
                "page": item.get("page"),
                "bbox": item.get("bbox"),
                "description": item.get("description"),
                "transcription": item.get("transcription"),
            }
            for item in record.get("assets") or []
        ],
        "code_blocks": record.get("code_blocks") or [],
        "code_annotations": record.get("code_annotations") or [],
        "code_examples": record.get("code_examples") or [],
        "reference_code": record.get("reference_code"),
        "reference_code_pages": record.get("reference_code_pages") or [],
    }


def content_sha256(record: dict[str, Any]) -> str:
    material = json.dumps(content_payload(record), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def content_issue(record: dict[str, Any]) -> str | None:
    if record.get("question_type") != "choice":
        return None
    options = record.get("options") or []
    labels = [str(item.get("label")) for item in options]
    if labels == ["A", "B", "C", "D"]:
        return None
    if len(options) == 4:
        return f"选择题选项标签异常（原文保留：{','.join(labels)}）"
    return f"选择题选项未完整还原（识别到 {len(options)} 项：{','.join(labels) or '无'}）"


def content_status(record: dict[str, Any]) -> str:
    issue = content_issue(record)
    if issue is None:
        return "ok"
    return "options_label_anomaly" if len(record.get("options") or []) == 4 else "options_incomplete"


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def copy_assets(
    records: Iterable[dict[str, Any]],
    *,
    review_root: Path,
    target_dir: Path,
) -> list[dict[str, Any]]:
    """把题目引用图复制进 build，按内容命名；保留来源说明。"""

    target_dir.mkdir(parents=True, exist_ok=True)
    assets: list[dict[str, Any]] = []
    for record in records:
        for ordinal, item in enumerate(record.get("assets") or [], start=1):
            source = review_root / str(item.get("crop") or "")
            if not source.is_file():
                raise FileNotFoundError(f"题图不存在：{source}")
            data = source.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            asset_id = f"asset-{digest[:16]}"
            relative = f"assets/{asset_id}{source.suffix.lower() or '.png'}"
            target = target_dir.parent / relative
            if not target.exists():
                shutil.copyfile(source, target)
            assets.append(
                {
                    "question_id": record["question_id"],
                    "ordinal": ordinal,
                    "asset_id": asset_id,
                    "file_rel_path": relative,
                    "sha256": digest,
                    "source": str(item.get("crop")),
                }
            )
    return assets


def insert_paper(conn: sqlite3.Connection, paper: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO papers(paper_id,title,course,exam_type,academic_year,exam_date,"
        "pdf_rel_path,pdf_sha256,physical_pages) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            paper["paper_id"],
            paper["title"],
            paper["course"],
            paper.get("exam_type"),
            paper.get("academic_year"),
            paper.get("exam_date"),
            paper.get("pdf_rel_path"),
            paper.get("pdf_sha256"),
            paper.get("physical_pages"),
        ),
    )


def insert_question(
    conn: sqlite3.Connection,
    record: dict[str, Any],
    *,
    labels: list[dict[str, Any]],
    asset_map: dict[tuple[str, int], dict[str, Any]],
) -> None:
    issue = content_issue(record)
    answer = record.get("answer") or {}
    pages = record.get("pages") or {}
    statement_pages = pages.get("statement_all") or pages.get("statement") or []
    review = record.get("review") or {}
    checks = record.get("checks") or {}
    conn.execute(
        "INSERT INTO questions(question_id,paper_id,course,section_code,section_title,"
        "number_label,question_type,statement,content_sha256,automated_status,visual_status,"
        "review_status,content_status,content_issue,searchable,answer_state,answer_available,"
        "answer_label,answer_json,pages_statement_json,pages_answer_json,reference_code,"
        "reference_code_pages_json,raw_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            record["question_id"],
            record["paper_id"],
            record["course"],
            (record.get("section") or {}).get("code", ""),
            (record.get("section") or {}).get("title", ""),
            str(record.get("number_label")),
            record["question_type"],
            record.get("statement") or "",
            content_sha256(record),
            str(checks.get("automated") or review.get("automated") or "pass"),
            str(review.get("visual") or checks.get("visual") or "not_reviewed"),
            "approved" if review.get("approved") else "candidate",
            content_status(record),
            issue,
            1 if issue is None else 0,
            str(answer.get("state") or "missing"),
            1 if answer.get("state") == "original" and answer.get("text") else 0,
            answer.get("label"),
            json.dumps(answer, ensure_ascii=False) if answer else None,
            json.dumps(statement_pages, ensure_ascii=False),
            json.dumps(pages.get("answer") or [], ensure_ascii=False),
            record.get("reference_code"),
            json.dumps(record.get("reference_code_pages") or [], ensure_ascii=False),
            json.dumps(record, ensure_ascii=False),
        ),
    )
    for index, item in enumerate(record.get("options") or [], start=1):
        conn.execute(
            "INSERT INTO options(question_id,ordinal,label,text) VALUES(?,?,?,?)",
            (record["question_id"], index, str(item.get("label") or ""), str(item.get("text") or "")),
        )
    for index, line in enumerate(record.get("code_lines") or [], start=1):
        conn.execute(
            "INSERT INTO code_lines(question_id,ordinal,text) VALUES(?,?,?)",
            (record["question_id"], index, str(line)),
        )
    for index, number in enumerate(record.get("blank_marks") or [], start=1):
        conn.execute(
            "INSERT INTO blank_marks(question_id,ordinal,blank_no) VALUES(?,?,?)",
            (record["question_id"], index, int(number)),
        )
    for index, item in enumerate(record.get("formulas") or [], start=1):
        conn.execute(
            "INSERT INTO formulas(question_id,ordinal,text,page) VALUES(?,?,?,?)",
            (record["question_id"], index, str(item.get("text") or ""), str(item.get("page") or "") or None),
        )
    for index, item in enumerate(record.get("assets") or [], start=1):
        asset = asset_map[(record["question_id"], index)]
        conn.execute(
            "INSERT INTO assets(question_id,ordinal,asset_id,page,bbox_json,description,"
            "transcription,file_rel_path,sha256) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                record["question_id"],
                index,
                asset["asset_id"],
                item.get("page"),
                json.dumps(item.get("bbox") or [], ensure_ascii=False),
                item.get("description"),
                item.get("transcription"),
                asset["file_rel_path"],
                asset["sha256"],
            ),
        )
    for index, item in enumerate(record.get("code_blocks") or [], start=1):
        block_id = item.get("block_id") or f"{record['question_id']}:{index}"
        conn.execute(
            "INSERT INTO code_blocks(question_id,ordinal,block_id,method,method_label,column,"
            "kind,name,text,page,pages_json,depends_on_json,bbox_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record["question_id"],
                index,
                block_id,
                item.get("method"),
                item.get("method_label"),
                item.get("column"),
                item.get("kind"),
                item.get("name"),
                str(item.get("text") or ""),
                item.get("page"),
                json.dumps(item.get("pages") or [], ensure_ascii=False),
                json.dumps(item.get("depends_on") or [], ensure_ascii=False),
                json.dumps(item.get("bbox"), ensure_ascii=False) if item.get("bbox") else None,
            ),
        )
    for index, item in enumerate(record.get("code_annotations") or [], start=1):
        conn.execute(
            "INSERT INTO code_annotations(question_id,ordinal,role,text,page,bbox_json) VALUES(?,?,?,?,?,?)",
            (
                record["question_id"],
                index,
                item.get("role"),
                str(item.get("text") or ""),
                item.get("page"),
                json.dumps(item.get("bbox"), ensure_ascii=False) if item.get("bbox") else None,
            ),
        )
    for index, item in enumerate(record.get("code_examples") or [], start=1):
        conn.execute(
            "INSERT INTO code_examples(question_id,ordinal,label,text,page) VALUES(?,?,?,?,?)",
            (
                record["question_id"],
                index,
                item.get("label"),
                str(item.get("text") or ""),
                item.get("page"),
            ),
        )
    for index, item in enumerate(record.get("independent_solutions") or [], start=1):
        conn.execute(
            "INSERT INTO independent_solutions(question_id,ordinal,author,text,note) VALUES(?,?,?,?,?)",
            (
                record["question_id"],
                index,
                item.get("author"),
                str(item.get("text") or ""),
                item.get("note"),
            ),
        )
    for index, item in enumerate(answer.get("sources") or [], start=1):
        conn.execute(
            "INSERT INTO answer_sources(question_id,ordinal,kind,text,detail,pairing_verified,pages_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                record["question_id"],
                index,
                str(item.get("kind") or ""),
                item.get("text"),
                item.get("detail"),
                None if item.get("pairing_verified") is None else int(bool(item.get("pairing_verified"))),
                json.dumps(item.get("pages") or [], ensure_ascii=False),
            ),
        )
    for index, item in enumerate(labels, start=1):
        conn.execute(
            "INSERT INTO question_knowledge_points(question_id,kp_slug,ordinal,status,basis,rule_id) "
            "VALUES(?,?,?,?,?,?)",
            (
                record["question_id"],
                item["slug"],
                index,
                item.get("status", "candidate"),
                item["basis"],
                item["rule_id"],
            ),
        )


def insert_fragments(
    conn: sqlite3.Connection,
    question_id: str,
    text: str,
    fragments: list[dict[str, Any]],
) -> None:
    conn.execute(
        "INSERT INTO retrieval_texts(question_id,text,text_sha256,fragment_count) VALUES(?,?,?,?)",
        (
            question_id,
            text,
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
            len(fragments),
        ),
    )
    for item in fragments:
        conn.execute(
            "INSERT INTO fragments(fragment_id,question_id,ordinal,text,token_count) VALUES(?,?,?,?,?)",
            (
                item["fragment_id"],
                question_id,
                item["ordinal"],
                item["text"],
                item["token_count"],
            ),
        )


def write_meta(conn: sqlite3.Connection, values: dict[str, Any]) -> None:
    for key, value in values.items():
        stored = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, stored))


def integrity(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "quick_check": conn.execute("PRAGMA quick_check").fetchone()[0],
        "questions": conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0],
        "fragments": conn.execute("SELECT COUNT(*) FROM fragments").fetchone()[0],
        "max_fragment_tokens": conn.execute("SELECT COALESCE(MAX(token_count),0) FROM fragments").fetchone()[0],
        "content_budget": CONTENT_BUDGET,
    }