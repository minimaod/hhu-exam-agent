"""题目库版本构建、校验、切换与回滚。

用法：
    python -m tools.question_bank.build plan
    python -m tools.question_bank.build build
    python -m tools.question_bank.build verify --build iv-xxxxxxxxxxxxxxxx
    python -m tools.question_bank.build activate --build iv-xxxxxxxxxxxxxxxx
    python -m tools.question_bank.build rollback
    python -m tools.question_bank.build status
    python -m tools.question_bank.build prune --keep 3 --yes
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from app.question_bank.config import (
    COLLECTION,
    CONTENT_BUDGET,
    COURSE,
    DEFAULT_BANK_ROOT,
    DEFAULT_PAPERS_ROOT,
    MAX_TOKENS,
    MODEL_DIM,
    MODEL_ID,
    RetrievalConfig,
    SCHEMA_VERSION,
    SPECIAL_TOKENS,
)
from app.question_bank.embedding import FastEmbedder, model_fingerprint
from app.question_bank.paths import (
    BankLayoutError,
    BankPaths,
    atomic_write_json,
    read_current,
    relative,
    write_current,
)
from app.question_bank.taxonomy import LabelMatch, Taxonomy, load_taxonomy
from app.question_bank.textbuild import FragmentTooLongError, build_retrieval_text, make_fragments
from app.question_bank.tokenization import load_stopwords, tokenize
from app.question_bank.vector_store import VectorStore
from tools.question_bank import db as QDB
from tools.question_bank.curation import CurationError, apply_corrections

BUILD_SCHEMA = 1
SOURCE_DIR = "source/full"
CROPS = "review/full/crops"


class BuildError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha_json(payload: Any) -> str:
    material = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha_text(material)


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def build_lock(paths: BankPaths):
    paths.db_dir.mkdir(parents=True, exist_ok=True)
    lock = paths.db_dir / ".build.lock"
    if lock.exists():
        raise BuildError(f"构建锁已存在：{lock}（确认没有其他构建进程后删除）")
    lock.write_text(json.dumps({"pid": os.getpid(), "at": _now()}), encoding="utf-8")
    try:
        yield
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def load_bank_inputs(
    paths: BankPaths,
    corrections: Iterable[Path] | None = None,
) -> tuple[list[dict], dict, list[dict]]:
    source_dir = paths.root / SOURCE_DIR
    manifest_path = source_dir / "manifest.json"
    if not manifest_path.is_file():
        raise BuildError(f"源数据清单不存在：{manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records: list[dict] = []
    for path in sorted(source_dir.glob("*.jsonl")):
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                raise BuildError(f"{path.name} 第 {line_no} 行不是 JSON") from exc
            if not record.get("question_id"):
                raise BuildError(f"{path.name} 第 {line_no} 行缺少 question_id")
            records.append(record)
    ids = [record["question_id"] for record in records]
    if len(ids) != len(set(ids)):
        raise BuildError("源数据存在重复 question_id")
    catalog = {row["id"]: row for row in json.loads((DEFAULT_PAPERS_ROOT / "catalog.json").read_text(encoding="utf-8"))}
    papers: list[dict] = []
    for item in manifest.get("papers") or []:
        paper_id = item["paper_id"]
        meta = catalog.get(paper_id) or {}
        papers.append(
            {
                "paper_id": paper_id,
                "title": item.get("title") or meta.get("title") or paper_id,
                "course": item.get("course") or meta.get("course") or COURSE,
                "exam_type": meta.get("exam_type"),
                "academic_year": meta.get("academic_year"),
                "exam_date": meta.get("exam_date"),
                "pdf_rel_path": meta.get("file_path"),
                "pdf_sha256": item.get("pdf_sha256"),
                "physical_pages": item.get("pages"),
            }
        )
    if len(papers) != 20:
        raise BuildError(f"源数据应有 20 卷，实际 {len(papers)} 卷")
    by_paper = {paper["paper_id"] for paper in papers}
    unknown = {record["paper_id"] for record in records} - by_paper
    if unknown:
        raise BuildError(f"题目引用了未知试卷：{sorted(unknown)}")
    correction_paths = (
        [Path(path) for path in corrections]
        if corrections is not None
        else [paths.root / "review/p4/mentor-corrections.json"]
    )
    for path in correction_paths:
        try:
            records = apply_corrections(records, papers, Path(path))
        except CurationError as exc:
            raise BuildError(str(exc)) from exc
    return records, manifest, papers


def source_version(records: Iterable[dict], papers: Iterable[dict]) -> str:
    material = {
        "schema": BUILD_SCHEMA,
        "course": COURSE,
        "papers": sorted(
            [
                {
                    "paper_id": paper["paper_id"],
                    "title": paper["title"],
                    "exam_type": paper.get("exam_type"),
                    "academic_year": paper.get("academic_year"),
                    "pdf_sha256": paper.get("pdf_sha256"),
                    "pages": paper.get("physical_pages"),
                }
                for paper in papers
            ],
            key=lambda item: item["paper_id"],
        ),
        "records": [QDB.content_payload(record) for record in sorted(records, key=lambda item: item["question_id"])],
    }
    return "sv-" + _sha_json(material)[:16]


def taxonomy_version(paths: BankPaths) -> str:
    return "tx-" + _sha_file(paths.taxonomy_file)[:16]


def ensure_reviews(paths: BankPaths) -> dict[str, Any]:
    paths.db_dir.mkdir(parents=True, exist_ok=True)
    if not paths.reviews_file.exists():
        payload = {
            "schema_version": 1,
            "decisions": {},
            "updated_at": _now(),
        }
        atomic_write_json(paths.reviews_file, payload)
        return payload
    try:
        payload = json.loads(paths.reviews_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError("reviews.json 无法读取") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("decisions"), dict):
        raise BuildError("reviews.json 缺少 decisions")
    return payload


def reviews_version(payload: dict[str, Any]) -> str:
    return "rv-" + _sha_json(payload.get("decisions") or {})[:16]


def prepare_labels(
    records: list[dict],
    taxonomy: Taxonomy,
    decisions: dict[str, dict],
) -> tuple[dict[str, list[dict]], int]:
    labels: dict[str, list[dict]] = {}
    verified = 0
    for record in records:
        matches: list[LabelMatch] = taxonomy.label(record)
        rows = [
            {
                "slug": match.slug,
                "basis": match.basis,
                "rule_id": match.rule_id,
                "status": "candidate",
            }
            for match in matches
        ]
        decision = decisions.get(record["question_id"])
        if decision and decision.get("content_sha256") == QDB.content_sha256(record):
            for slug in decision.get("labels_verified") or []:
                if slug not in taxonomy.by_slug:
                    continue
                existing = next((row for row in rows if row["slug"] == slug), None)
                if existing is None:
                    rows.append(
                        {
                            "slug": slug,
                            "basis": "导师复核",
                            "rule_id": "mentor:verified",
                            "status": "verified",
                        }
                    )
                else:
                    existing["status"] = "verified"
                verified += 1
        labels[record["question_id"]] = rows
    return labels, verified


def prepare_texts(
    records: list[dict],
    labels: dict[str, list[dict]],
    taxonomy: Taxonomy,
    embedder: FastEmbedder,
) -> tuple[dict[str, str], dict[str, list[dict]], list[dict]]:
    texts: dict[str, str] = {}
    fragments: dict[str, list[dict]] = {}
    flat: list[dict] = []
    for record in records:
        if record.get("question_type") == "choice" and QDB.content_issue(record):
            continue
        names = [
            taxonomy.name(row["slug"])
            for row in labels.get(record["question_id"], [])
            if row["status"] == "verified"
        ]
        text = build_retrieval_text(record, names)
        pieces = make_fragments(text, embedder.token_count_exact)
        texts[record["question_id"]] = text
        fragments[record["question_id"]] = [
            {
                "fragment_id": f"{record['question_id']}#{piece.fragment_id}",
                "ordinal": piece.ordinal,
                "text": piece.text,
                "token_count": piece.token_count,
            }
            for piece in pieces
        ]
        for item in fragments[record["question_id"]]:
            flat.append(
                {
                    "fragment_id": item["fragment_id"],
                    "question_id": record["question_id"],
                    "text": item["text"],
                    "token_count": item["token_count"],
                }
            )
    return texts, fragments, flat


def index_material(
    *,
    source_v: str,
    taxonomy_v: str,
    review_v: str,
    texts: dict[str, str],
    labels: dict[str, list[dict]],
    model: dict,
    stopwords_hash: str,
    config: RetrievalConfig,
) -> dict[str, Any]:
    return {
        "schema": BUILD_SCHEMA,
        "source_version": source_v,
        "taxonomy_version": taxonomy_v,
        "review_version": review_v,
        "model_sha256": model.get("sha256"),
        "model_id": model.get("id"),
        "tokenizer": {
            "implementation": "jieba.cut_for_search+code-operators",
            "stopwords_sha256": stopwords_hash,
        },
        "chunking": {
            "max_tokens": MAX_TOKENS,
            "special_tokens": SPECIAL_TOKENS,
            "content_budget": CONTENT_BUDGET,
            "strategy": "line-boundary-greedy",
        },
        "bm25": {"k1": config.bm25_k1, "b": config.bm25_b},
        "rrf_k": config.rrf_k,
        "retrieval": config.as_dict(),
        "collection": COLLECTION,
        "texts": {key: _sha_text(value) for key, value in sorted(texts.items())},
        "labels": {
            key: [[row["slug"], row["status"]] for row in rows]
            for key, rows in sorted(labels.items())
        },
    }


def build(
    paths: BankPaths,
    *,
    force: bool = False,
    config: RetrievalConfig | None = None,
    corrections: Iterable[Path] | None = None,
) -> dict[str, Any]:
    config = config or RetrievalConfig()
    records, manifest, papers = load_bank_inputs(paths, corrections)
    taxonomy = load_taxonomy(paths.taxonomy_file)
    review_payload = ensure_reviews(paths)
    decisions = review_payload.get("decisions") or {}
    embedder = FastEmbedder(paths.model_dir)
    model = embedder.fingerprint()

    source_v = source_version(records, papers)
    taxonomy_v = taxonomy_version(paths)
    review_v = reviews_version(review_payload)
    labels, verified_labels = prepare_labels(records, taxonomy, decisions)
    texts, fragments, flat = prepare_texts(records, labels, taxonomy, embedder)
    stopwords_hash = _sha_file(paths.stopwords_file)
    material = index_material(
        source_v=source_v,
        taxonomy_v=taxonomy_v,
        review_v=review_v,
        texts=texts,
        labels=labels,
        model=model,
        stopwords_hash=stopwords_hash,
        config=config,
    )
    index_v = "iv-" + _sha_json(material)[:16]
    target = paths.build_dir(index_v)

    if target.exists() and not force:
        result = verify_build(target)
        if result.get("ok"):
            print(f"已存在且校验通过：{relative(target)}")
            return {"build_id": index_v, "created": False, "verify": result}
        raise BuildError(f"目标 build 已存在但校验失败：{relative(target)}")

    temp = paths.builds_dir / f".tmp-{index_v}-{os.getpid()}"
    if temp.exists():
        shutil.rmtree(temp)
    temp.mkdir(parents=True)

    try:
        asset_rows = QDB.copy_assets(
            records,
            review_root=paths.root / CROPS,
            target_dir=temp / "assets",
        )
        asset_map = {(row["question_id"], row["ordinal"]): row for row in asset_rows}

        conn = sqlite3.connect(temp / "questions.sqlite3")
        conn.row_factory = sqlite3.Row
        QDB.create_schema(conn)
        for paper in papers:
            QDB.insert_paper(conn, paper)
        for index, point in enumerate(taxonomy.points, start=1):
            conn.execute(
                "INSERT INTO knowledge_points(slug,name,definition,aliases_json,sort_order) VALUES(?,?,?,?,?)",
                (
                    point.slug,
                    point.name,
                    point.definition,
                    json.dumps(list(point.aliases), ensure_ascii=False),
                    index,
                ),
            )
        counts = {"choice": 0, "fill": 0, "code": 0}
        answer_states: dict[str, int] = {}
        issues: list[dict] = []
        for record in records:
            if QDB.content_issue(record):
                issues.append(
                    {
                        "question_id": record["question_id"],
                        "issue": QDB.content_issue(record),
                    }
                )
            QDB.insert_question(
                conn,
                record,
                labels=labels.get(record["question_id"], []),
                asset_map=asset_map,
            )
            counts[record["question_type"]] = counts.get(record["question_type"], 0) + 1
            state = str((record.get("answer") or {}).get("state") or "missing")
            answer_states[state] = answer_states.get(state, 0) + 1
        for question_id, text in texts.items():
            QDB.insert_fragments(conn, question_id, text, fragments[question_id])
        conn.commit()
        QDB.write_meta(
            conn,
            {
                "build_id": index_v,
                "source_version": source_v,
                "taxonomy_version": taxonomy_v,
                "review_version": review_v,
                "created_at": _now(),
            },
        )
        conn.commit()
        db_integrity = QDB.integrity(conn)
        conn.close()

        from app.question_bank.bm25 import Bm25Document, Bm25Index

        stopwords = load_stopwords(paths.stopwords_file)
        documents = [
            Bm25Document(
                fragment_id=item["fragment_id"],
                question_id=item["question_id"],
                tokens=tuple(tokenize(item["text"], stopwords)),
            )
            for item in flat
        ]
        bm25 = Bm25Index.build(documents, k1=config.bm25_k1, b=config.bm25_b)
        (temp / "bm25").mkdir(parents=True, exist_ok=True)
        bm25.save(temp / "bm25" / "index.json")

        vectors = embedder.embed_documents([item["text"] for item in flat])
        question_by_id = {record["question_id"]: record for record in records}
        vector_fragments = [
            {
                "fragment_id": item["fragment_id"],
                "payload": {
                    "fragment_id": item["fragment_id"],
                    "question_id": item["question_id"],
                    "course": question_by_id[item["question_id"]]["course"],
                    "paper_id": question_by_id[item["question_id"]]["paper_id"],
                    "exam_type": next(
                        paper["exam_type"] for paper in papers if paper["paper_id"] == question_by_id[item["question_id"]]["paper_id"]
                    ),
                    "question_type": question_by_id[item["question_id"]]["question_type"],
                    "searchable": True,
                    "kp_slugs": [row["slug"] for row in labels.get(item["question_id"], [])],
                },
            }
            for item in flat
        ]
        store = VectorStore(temp / "qdrant", COLLECTION, MODEL_DIM).open(require_collection=False)
        store.create()
        store.upsert(vector_fragments, vectors)
        vector_count = store.count()
        store.close()

        db_sha = _sha_file(temp / "questions.sqlite3")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "build_schema": BUILD_SCHEMA,
            "build_id": index_v,
            "index_version": index_v,
            "source_version": source_v,
            "taxonomy_version": taxonomy_v,
            "review_version": review_v,
            "course": COURSE,
            "created_at": _now(),
            "collection": COLLECTION,
            "model": model,
            "retrieval": config.as_dict(),
            "bm25": {"k1": config.bm25_k1, "b": config.bm25_b},
            "rrf_k": config.rrf_k,
            "chunking": material["chunking"],
            "tokenizer": material["tokenizer"],
            "source": {
                "manifest_generated_at": manifest.get("generated_at"),
                "papers": len(papers),
                "records": len(records),
                "issues": len(manifest.get("issues") or []),
                "doc_sha256": (manifest.get("reconciliation") or {}).get("mentor_doc", {}).get("sha256"),
            },
            "counts": {
                "questions": len(records),
                "searchable": sum(1 for record in records if not QDB.content_issue(record)),
                "by_type": counts,
                "answer_states": answer_states,
                "fragments": len(flat),
                "knowledge_points": len(taxonomy.points),
                "verified_labels": verified_labels,
                "assets": len(asset_rows),
                "content_issues": len(issues),
            },
            "content_issues": issues,
            "assets": [
                {"asset_id": row["asset_id"], "path": row["file_rel_path"], "sha256": row["sha256"]}
                for row in asset_rows
            ],
            "db_sha256": db_sha,
            "bm25_docs": bm25.n,
            "vector_points": vector_count,
            "db_integrity": db_integrity,
        }
        atomic_write_json(temp / "build.json", payload)
        result = verify_build(temp, write=True)
        if not result.get("ok"):
            raise BuildError("构建后自检失败：" + json.dumps(result, ensure_ascii=False))
        paths.builds_dir.mkdir(parents=True, exist_ok=True)
        os.replace(temp, target)
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise

    print(f"构建完成：{relative(target)}")
    print(
        f"题量 {len(records)}（可检索 {payload['counts']['searchable']}），"
        f"片段 {len(flat)}，知识点 {len(taxonomy.points)}，题图 {len(asset_rows)}"
    )
    return {"build_id": index_v, "created": True, "verify": result, "payload": payload}


def verify_build(build_dir: Path, *, write: bool = False) -> dict[str, Any]:
    manifest_path = build_dir / "build.json"
    if not manifest_path.is_file():
        return {"ok": False, "errors": ["build.json 不存在"]}
    build = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    checks: dict[str, Any] = {}
    db_path = build_dir / "questions.sqlite3"
    if not db_path.is_file():
        return {"ok": False, "errors": ["事实库不存在"]}
    try:
        conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        checks["quick_check"] = conn.execute("PRAGMA quick_check").fetchone()[0]
        if checks["quick_check"] != "ok":
            errors.append("SQLite quick_check 未通过")
        counts = build.get("counts") or {}
        checks["questions"] = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        checks["searchable"] = conn.execute("SELECT COUNT(*) FROM questions WHERE searchable=1").fetchone()[0]
        checks["fragments"] = conn.execute("SELECT COUNT(*) FROM fragments").fetchone()[0]
        checks["max_fragment_tokens"] = conn.execute("SELECT COALESCE(MAX(token_count),0) FROM fragments").fetchone()[0]
        checks["content_issues"] = conn.execute("SELECT COUNT(*) FROM questions WHERE content_status!='ok'").fetchone()[0]
        if checks["questions"] != counts.get("questions"):
            errors.append("题量与 build.json 不一致")
        if checks["searchable"] != counts.get("searchable"):
            errors.append("可检索题量与 build.json 不一致")
        if checks["fragments"] != counts.get("fragments"):
            errors.append("片段数与 build.json 不一致")
        if checks["max_fragment_tokens"] > CONTENT_BUDGET:
            errors.append("存在超过 token 预算的片段")
        if checks["content_issues"] != counts.get("content_issues"):
            errors.append("内容问题数与 build.json 不一致")

        mismatch = 0
        for row in conn.execute("SELECT question_id, text, fragment_count FROM retrieval_texts"):
            pieces = [
                item[0]
                for item in conn.execute(
                    "SELECT text FROM fragments WHERE question_id=? ORDER BY ordinal",
                    (row["question_id"],),
                )
            ]
            if "".join(pieces) != row["text"] or len(pieces) != row["fragment_count"]:
                mismatch += 1
        checks["fragment_coverage_mismatch"] = mismatch
        if mismatch:
            errors.append("片段拼接与检索文本不一致")

        # 内容哈希与事实库逐字段对照：仅比数量无法发现“题干被改、
        # raw_json 未动”或子表被篡改。这里同时核对 raw_json 派生的哈希和
        # 实际落盘字段，避免校验只证明“文件还在”。
        options_map: dict[str, list[dict[str, Any]]] = {}
        for row in conn.execute("SELECT question_id, label, text FROM options ORDER BY question_id, ordinal"):
            options_map.setdefault(row["question_id"], []).append(
                {"label": str(row["label"]), "text": str(row["text"])}
            )
        code_lines_map: dict[str, list[str]] = {}
        for row in conn.execute("SELECT question_id, text FROM code_lines ORDER BY question_id, ordinal"):
            code_lines_map.setdefault(row["question_id"], []).append(str(row["text"]))
        blanks_map: dict[str, list[int]] = {}
        for row in conn.execute("SELECT question_id, blank_no FROM blank_marks ORDER BY question_id, ordinal"):
            blanks_map.setdefault(row["question_id"], []).append(int(row["blank_no"]))
        formulas_map: dict[str, list[dict[str, Any]]] = {}
        for row in conn.execute("SELECT question_id, text, page FROM formulas ORDER BY question_id, ordinal"):
            formulas_map.setdefault(row["question_id"], []).append(
                {"text": str(row["text"]), "page": row["page"]}
            )
        assets_map: dict[str, list[dict[str, Any]]] = {}
        for row in conn.execute(
            "SELECT question_id, page, bbox_json, description, transcription "
            "FROM assets ORDER BY question_id, ordinal"
        ):
            assets_map.setdefault(row["question_id"], []).append(
                {
                    "page": row["page"],
                    "bbox": json.loads(row["bbox_json"] or "[]"),
                    "description": row["description"],
                    "transcription": row["transcription"],
                }
            )
        blocks_map: dict[str, list[dict[str, Any]]] = {}
        for row in conn.execute(
            "SELECT * FROM code_blocks ORDER BY question_id, ordinal"
        ):
            blocks_map.setdefault(row["question_id"], []).append(
                {
                    "block_id": row["block_id"],
                    "method": row["method"],
                    "method_label": row["method_label"],
                    "column": row["column"],
                    "kind": row["kind"],
                    "name": row["name"],
                    "text": str(row["text"]),
                    "page": row["page"],
                    "pages": json.loads(row["pages_json"] or "[]"),
                    "depends_on": json.loads(row["depends_on_json"] or "[]"),
                    "bbox": json.loads(row["bbox_json"] or "null"),
                }
            )
        annotations_map: dict[str, list[dict[str, Any]]] = {}
        for row in conn.execute(
            "SELECT * FROM code_annotations ORDER BY question_id, ordinal"
        ):
            annotations_map.setdefault(row["question_id"], []).append(
                {
                    "role": row["role"],
                    "text": str(row["text"]),
                    "page": row["page"],
                    "bbox": json.loads(row["bbox_json"] or "null"),
                }
            )
        examples_map: dict[str, list[dict[str, Any]]] = {}
        for row in conn.execute(
            "SELECT question_id, label, text, page FROM code_examples ORDER BY question_id, ordinal"
        ):
            examples_map.setdefault(row["question_id"], []).append(
                {"label": row["label"], "text": str(row["text"]), "page": row["page"]}
            )
        solutions_map: dict[str, list[dict[str, Any]]] = {}
        for row in conn.execute(
            "SELECT question_id, author, text, note FROM independent_solutions "
            "ORDER BY question_id, ordinal"
        ):
            solutions_map.setdefault(row["question_id"], []).append(
                {"author": row["author"], "text": str(row["text"]), "note": row["note"]}
            )

        content_bad = 0
        material_bad = 0
        material_examples: list[str] = []
        for row in conn.execute(
            "SELECT question_id, statement, content_sha256, answer_json, "
            "pages_statement_json, pages_answer_json, reference_code, "
            "reference_code_pages_json, raw_json FROM questions"
        ):
            question_id = str(row["question_id"])
            try:
                raw = json.loads(row["raw_json"])
            except (TypeError, ValueError):
                content_bad += 1
                material_examples.append(f"{question_id}:raw_json")
                continue
            if QDB.content_sha256(raw) != row["content_sha256"]:
                content_bad += 1
                material_examples.append(f"{question_id}:content_sha256")
            if str(raw.get("statement") or "") != str(row["statement"] or ""):
                material_bad += 1
                material_examples.append(f"{question_id}:statement")
            expected_options = [
                {"label": str(item.get("label")), "text": str(item.get("text"))}
                for item in raw.get("options") or []
            ]
            if options_map.get(question_id, []) != expected_options:
                material_bad += 1
                material_examples.append(f"{question_id}:options")
            if code_lines_map.get(question_id, []) != [str(item) for item in raw.get("code_lines") or []]:
                material_bad += 1
                material_examples.append(f"{question_id}:code_lines")
            if blanks_map.get(question_id, []) != [int(item) for item in raw.get("blank_marks") or []]:
                material_bad += 1
                material_examples.append(f"{question_id}:blank_marks")
            expected_formulas = [
                {"text": str(item.get("text") or ""), "page": str(item.get("page") or "") or None}
                for item in raw.get("formulas") or []
            ]
            if formulas_map.get(question_id, []) != expected_formulas:
                material_bad += 1
                material_examples.append(f"{question_id}:formulas")
            expected_assets = [
                {
                    "page": item.get("page"),
                    "bbox": item.get("bbox") or [],
                    "description": item.get("description"),
                    "transcription": item.get("transcription"),
                }
                for item in raw.get("assets") or []
            ]
            if assets_map.get(question_id, []) != expected_assets:
                material_bad += 1
                material_examples.append(f"{question_id}:assets")
            expected_blocks = [
                {
                    "block_id": item.get("block_id") or f"{question_id}:{index}",
                    "method": item.get("method"),
                    "method_label": item.get("method_label"),
                    "column": item.get("column"),
                    "kind": item.get("kind"),
                    "name": item.get("name"),
                    "text": str(item.get("text") or ""),
                    "page": item.get("page"),
                    "pages": item.get("pages") or [],
                    "depends_on": item.get("depends_on") or [],
                    "bbox": item.get("bbox") or None,
                }
                for index, item in enumerate(raw.get("code_blocks") or [], start=1)
            ]
            if blocks_map.get(question_id, []) != expected_blocks:
                material_bad += 1
                material_examples.append(f"{question_id}:code_blocks")
            expected_annotations = [
                {
                    "role": item.get("role"),
                    "text": str(item.get("text") or ""),
                    "page": item.get("page"),
                    "bbox": item.get("bbox") or None,
                }
                for item in raw.get("code_annotations") or []
            ]
            if annotations_map.get(question_id, []) != expected_annotations:
                material_bad += 1
                material_examples.append(f"{question_id}:code_annotations")
            expected_examples = [
                {"label": item.get("label"), "text": str(item.get("text") or ""), "page": item.get("page")}
                for item in raw.get("code_examples") or []
            ]
            if examples_map.get(question_id, []) != expected_examples:
                material_bad += 1
                material_examples.append(f"{question_id}:code_examples")
            expected_solutions = [
                {"author": item.get("author"), "text": str(item.get("text") or ""), "note": item.get("note")}
                for item in raw.get("independent_solutions") or []
            ]
            if solutions_map.get(question_id, []) != expected_solutions:
                material_bad += 1
                material_examples.append(f"{question_id}:independent_solutions")
            db_answer = json.loads(row["answer_json"]) if row["answer_json"] else {}
            if db_answer != (raw.get("answer") or {}):
                material_bad += 1
                material_examples.append(f"{question_id}:answer")
            raw_pages = raw.get("pages") or {}
            if json.loads(row["pages_statement_json"] or "[]") != (
                raw_pages.get("statement_all") or raw_pages.get("statement") or []
            ):
                material_bad += 1
                material_examples.append(f"{question_id}:pages_statement")
            if json.loads(row["pages_answer_json"] or "[]") != (raw_pages.get("answer") or []):
                material_bad += 1
                material_examples.append(f"{question_id}:pages_answer")
            if row["reference_code"] != raw.get("reference_code"):
                material_bad += 1
                material_examples.append(f"{question_id}:reference_code")
            if json.loads(row["reference_code_pages_json"] or "[]") != (raw.get("reference_code_pages") or []):
                material_bad += 1
                material_examples.append(f"{question_id}:reference_code_pages")
        checks["content_hash_mismatch"] = content_bad
        checks["material_mismatch"] = material_bad
        if content_bad:
            errors.append(f"内容哈希不一致：{content_bad} 条（{', '.join(material_examples[:3])}）")
        if material_bad:
            errors.append(f"事实库字段与 raw_json 不一致：{material_bad} 处（{', '.join(material_examples[:3])}）")

        text_hash_bad = 0
        for row in conn.execute("SELECT question_id, text, text_sha256 FROM retrieval_texts"):
            if hashlib.sha256(str(row["text"]).encode("utf-8")).hexdigest() != row["text_sha256"]:
                text_hash_bad += 1
        checks["retrieval_text_hash_mismatch"] = text_hash_bad
        if text_hash_bad:
            errors.append("检索文本哈希不一致")

        question_ids = {str(row[0]) for row in conn.execute("SELECT question_id FROM questions")}
        searchable_ids = {
            str(row[0])
            for row in conn.execute("SELECT question_id FROM questions WHERE searchable=1")
        }
        fragment_question: dict[str, str] = {}
        fragment_ref_bad = 0
        for row in conn.execute("SELECT fragment_id, question_id FROM fragments"):
            fragment_id = str(row["fragment_id"])
            question_id = str(row["question_id"])
            if question_id not in question_ids:
                fragment_ref_bad += 1
            if fragment_id in fragment_question:
                fragment_ref_bad += 1
            fragment_question[fragment_id] = question_id
        retrieval_ids = {
            str(row[0]) for row in conn.execute("SELECT question_id FROM retrieval_texts")
        }
        if searchable_ids - retrieval_ids:
            fragment_ref_bad += len(searchable_ids - retrieval_ids)
        checks["fragment_ref_mismatch"] = fragment_ref_bad
        if fragment_ref_bad:
            errors.append("片段引用了不存在/重复的题目，或有可检索题缺少检索文本")

        bad_tokens = conn.execute(
            "SELECT COUNT(*) FROM fragments WHERE token_count > ?", (CONTENT_BUDGET,)
        ).fetchone()[0]
        checks["over_budget_fragments"] = bad_tokens
        if bad_tokens:
            errors.append("存在超预算片段")

        asset_bad = 0
        for row in conn.execute("SELECT file_rel_path, sha256 FROM assets"):
            path = build_dir / row["file_rel_path"]
            if not path.is_file() or _sha_file(path) != row["sha256"]:
                asset_bad += 1
        checks["asset_mismatch"] = asset_bad
        if asset_bad:
            errors.append("题图缺失或哈希不一致")

        labels = conn.execute("SELECT COUNT(*) FROM question_knowledge_points").fetchone()[0]
        checks["labels"] = labels
        conn.close()
    except sqlite3.Error as exc:
        return {"ok": False, "errors": [f"事实库校验失败：{exc}"], "checks": checks}

    bm25_path = build_dir / "bm25" / "index.json"
    bm25_alignment_bad = 0
    try:
        bm25 = json.loads(bm25_path.read_text(encoding="utf-8"))
        docs = bm25.get("docs") or []
        checks["bm25_docs"] = len(docs)
        checks["bm25_n"] = int(bm25.get("n") or 0)
        if checks["bm25_docs"] != checks["fragments"]:
            errors.append("BM25 文档数与片段数不一致")
        if checks["bm25_n"] != len(docs):
            errors.append("BM25 清单 n 与 docs 数量不一致")
        seen: set[str] = set()
        for doc in docs:
            fragment_id = str(doc.get("fragment_id") or "")
            question_id = str(doc.get("question_id") or "")
            if fragment_id in seen:
                bm25_alignment_bad += 1
                continue
            seen.add(fragment_id)
            if fragment_question.get(fragment_id) != question_id:
                bm25_alignment_bad += 1
        if seen != set(fragment_question):
            bm25_alignment_bad += len(set(fragment_question) ^ seen)
    except (OSError, ValueError):
        errors.append("BM25 索引缺失或损坏")
    checks["bm25_alignment_mismatch"] = bm25_alignment_bad
    if bm25_alignment_bad:
        errors.append("BM25 的片段/题目 ID 与事实库不一致")

    vector_count = None
    vector_alignment_bad = 0
    qdrant_dir = build_dir / "qdrant"
    if not qdrant_dir.is_dir():
        errors.append("Qdrant 目录不存在")
    else:
        store = VectorStore(qdrant_dir, str(build.get("collection") or COLLECTION), MODEL_DIM)
        try:
            store.open()
            vector_count = store.count()
            seen_vector: set[str] = set()
            for payload in store.payloads():
                fragment_id = str(payload.get("fragment_id") or "")
                question_id = str(payload.get("question_id") or "")
                if not fragment_id or fragment_id in seen_vector:
                    vector_alignment_bad += 1
                    continue
                seen_vector.add(fragment_id)
                if fragment_question.get(fragment_id) != question_id:
                    vector_alignment_bad += 1
            if seen_vector != set(fragment_question):
                vector_alignment_bad += len(set(fragment_question) ^ seen_vector)
        except Exception as exc:
            errors.append(f"Qdrant 校验失败：{exc}")
        finally:
            store.close()
    checks["vector_points"] = vector_count
    checks["vector_alignment_mismatch"] = vector_alignment_bad
    if vector_count is not None and vector_count != checks["fragments"]:
        errors.append("向量点位数与片段数不一致")
    if vector_alignment_bad:
        errors.append("向量的片段/题目 ID 与事实库不一致")

    result = {
        "ok": not errors,
        "errors": errors,
        "checks": checks,
        "build_id": build.get("build_id"),
        "verified_at": _now(),
    }
    if write:
        atomic_write_json(build_dir / "verify.json", result)
    return result


def activate(paths: BankPaths, build_id: str) -> dict[str, Any]:
    target = paths.build_dir(build_id)
    result = verify_build(target, write=True)
    if not result.get("ok"):
        raise BuildError("校验未通过，拒绝切换：" + "；".join(result.get("errors") or []))
    build = json.loads((target / "build.json").read_text(encoding="utf-8"))
    current = read_current(paths)
    previous = current["build_id"] if current else None
    write_current(
        paths,
        build_id=build_id,
        source_version=str(build["source_version"]),
        index_version=str(build["index_version"]),
        previous_build_id=previous,
        activated_at=_now(),
    )
    print(f"已切换到 {build_id}（上一版 {previous or '无'}）")
    return {"build_id": build_id, "previous": previous}


def rollback(paths: BankPaths) -> dict[str, Any]:
    current = read_current(paths)
    if current is None:
        raise BuildError("当前没有可回滚的版本")
    previous = current.get("previous_build_id")
    if not previous:
        raise BuildError("当前版本没有记录上一版")
    target = paths.build_dir(str(previous))
    result = verify_build(target, write=True)
    if not result.get("ok"):
        raise BuildError("上一版校验未通过：" + "；".join(result.get("errors") or []))
    build = json.loads((target / "build.json").read_text(encoding="utf-8"))
    write_current(
        paths,
        build_id=str(previous),
        source_version=str(build["source_version"]),
        index_version=str(build["index_version"]),
        previous_build_id=str(current["build_id"]),
        activated_at=_now(),
    )
    print(f"已回滚到 {previous}（当前版本 {current['build_id']} 保留）")
    return {"build_id": str(previous), "previous": str(current["build_id"])}


def list_builds(paths: BankPaths) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not paths.builds_dir.is_dir():
        return rows
    for path in paths.builds_dir.iterdir():
        if not path.is_dir() or path.name.startswith("."):
            continue
        manifest = path / "build.json"
        if not manifest.is_file():
            continue
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        rows.append(
            {
                "build_id": path.name,
                "created_at": payload.get("created_at"),
                "questions": (payload.get("counts") or {}).get("questions"),
                "searchable": (payload.get("counts") or {}).get("searchable"),
                "size_mb": round(sum(item.stat().st_size for item in path.rglob("*") if item.is_file()) / 1024 / 1024, 1),
            }
        )
    return sorted(rows, key=lambda item: str(item.get("created_at")), reverse=True)


def status(paths: BankPaths) -> dict[str, Any]:
    current = read_current(paths)
    return {
        "current": current,
        "builds": list_builds(paths),
        "reviews": str(paths.reviews_file) if paths.reviews_file.exists() else None,
    }


def prune(paths: BankPaths, keep: int, yes: bool) -> list[str]:
    current = read_current(paths)
    protected = {current["build_id"]} if current else set()
    if current and current.get("previous_build_id"):
        protected.add(current["previous_build_id"])
    rows = list_builds(paths)
    remove = [row for row in rows if row["build_id"] not in protected][keep:]
    if not remove:
        print("没有可清理的旧 build")
        return []
    for row in remove:
        print(f"待删除 {row['build_id']}（{row['size_mb']} MB）")
    if not yes:
        print("未加 --yes，未删除")
        return []
    for row in remove:
        target = paths.build_dir(str(row["build_id"]))
        if target.resolve().parent != paths.builds_dir.resolve():
            raise BuildError(f"拒绝删除越界路径：{target}")
        shutil.rmtree(target)
        print(f"已删除 {row['build_id']}")
    return [str(row["build_id"]) for row in remove]


def plan(paths: BankPaths, *, corrections: Iterable[Path] | None = None) -> dict[str, Any]:
    records, manifest, papers = load_bank_inputs(paths, corrections)
    taxonomy = load_taxonomy(paths.taxonomy_file)
    issues = [record for record in records if QDB.content_issue(record)]
    model = model_fingerprint(paths.model_dir)
    payload = {
        "course": COURSE,
        "papers": len(papers),
        "questions": len(records),
        "by_type": {
            key: sum(1 for record in records if record["question_type"] == key)
            for key in ("choice", "fill", "code")
        },
        "searchable": len(records) - len(issues),
        "content_issues": len(issues),
        "fragments_estimate": None,
        "source_version": source_version(records, papers),
        "taxonomy_version": taxonomy_version(paths),
        "knowledge_points": len(taxonomy.points),
        "model": {"id": model["id"], "sha256": model["sha256"]},
        "builds": list_builds(paths),
    }
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="题目事实库与混合检索索引工具")
    parser.add_argument("--bank-root", default=str(DEFAULT_BANK_ROOT))
    sub = parser.add_subparsers(dest="command", required=True)
    plan_p = sub.add_parser("plan")
    plan_p.add_argument("--corrections", action="append", default=None)
    build_p = sub.add_parser("build")
    build_p.add_argument("--force", action="store_true")
    build_p.add_argument("--corrections", action="append", default=None)
    verify_p = sub.add_parser("verify")
    verify_p.add_argument("--build", required=True)
    act = sub.add_parser("activate")
    act.add_argument("--build", required=True)
    sub.add_parser("rollback")
    sub.add_parser("status")
    prune_p = sub.add_parser("prune")
    prune_p.add_argument("--keep", type=int, default=3)
    prune_p.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    paths = BankPaths(Path(args.bank_root))

    try:
        if args.command == "plan":
            print(json.dumps(plan(paths, corrections=args.corrections), ensure_ascii=False, indent=2))
        elif args.command == "build":
            with build_lock(paths):
                build(paths, force=args.force, corrections=args.corrections)
        elif args.command == "verify":
            result = verify_build(paths.build_dir(args.build), write=True)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("ok") else 1
        elif args.command == "activate":
            activate(paths, args.build)
        elif args.command == "rollback":
            rollback(paths)
        elif args.command == "status":
            print(json.dumps(status(paths), ensure_ascii=False, indent=2))
        elif args.command == "prune":
            prune(paths, args.keep, args.yes)
    except (BuildError, BankLayoutError, FileNotFoundError, FragmentTooLongError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
