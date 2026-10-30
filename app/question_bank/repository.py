"""只读 SQLite 事实库访问层。

服务不直接拼业务 JSON 的查询逻辑：过滤、回取、课程统计都在这里，返回
普通 dict，由 service 转成 Pydantic 模型。数据库以 `mode=ro` 打开，运行期
不写任何学习数据。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from app.question_bank.errors import QuestionBankUnavailableError


class QuestionRepository:
    def __init__(self, build_dir: Path) -> None:
        self.build_dir = Path(build_dir)
        self.build = self._load_manifest()
        self.db_path = self.build_dir / "questions.sqlite3"
        if not self.db_path.is_file():
            raise QuestionBankUnavailableError("事实库文件不存在")
        try:
            uri = self.db_path.resolve().as_uri() + "?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA query_only=ON")
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            raise QuestionBankUnavailableError("事实库无法打开") from exc
        if int(version) != int(self.build.get("schema_version", 1)):
            self.close()
            raise QuestionBankUnavailableError("事实库 schema 与清单不一致")

    def _load_manifest(self) -> dict[str, Any]:
        path = self.build_dir / "build.json"
        if not path.is_file():
            raise QuestionBankUnavailableError("build.json 不存在")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise QuestionBankUnavailableError("build.json 无法读取") from exc
        if not isinstance(payload, dict) or not payload.get("build_id"):
            raise QuestionBankUnavailableError("build.json 缺少 build_id")
        return payload

    def close(self) -> None:
        conn = getattr(self, "_conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if getattr(self, "_conn", None) is None:
            raise QuestionBankUnavailableError("事实库已关闭")
        return self._conn

    # ------------------------------------------------------------------
    # 过滤与计数
    # ------------------------------------------------------------------

    def _where(
        self,
        *,
        course: str,
        question_type: str | None = None,
        exam_type: str | None = None,
        paper_id: str | None = None,
        number_label: str | None = None,
        knowledge_points: Iterable[str] = (),
        excluded: Iterable[str] = (),
    ) -> tuple[str, list[Any]]:
        clauses = ["q.searchable = 1", "q.course = ?"]
        params: list[Any] = [course]
        if question_type:
            clauses.append("q.question_type = ?")
            params.append(question_type)
        if paper_id:
            clauses.append("q.paper_id = ?")
            params.append(paper_id)
        if number_label is not None:
            clauses.append("q.number_label = ?")
            params.append(str(int(number_label)))
        if exam_type:
            clauses.append("p.exam_type = ?")
            params.append(exam_type)
        excluded = list(excluded)
        if excluded:
            placeholders = ",".join("?" for _ in excluded)
            clauses.append(f"q.question_id NOT IN ({placeholders})")
            params.extend(excluded)
        kps = list(dict.fromkeys(knowledge_points))
        if kps:
            placeholders = ",".join("?" for _ in kps)
            clauses.append(
                "q.question_id IN ("
                "SELECT question_id FROM question_knowledge_points "
                f"WHERE kp_slug IN ({placeholders}) "
                "GROUP BY question_id HAVING COUNT(DISTINCT kp_slug) = ?)"
            )
            params.extend(kps)
            params.append(len(kps))
        return " AND ".join(clauses), params

    def list_question_ids(self, **filters: Any) -> list[str]:
        where, params = self._where(**filters)
        sql = (
            "SELECT q.question_id FROM questions q "
            "JOIN papers p ON p.paper_id = q.paper_id "
            f"WHERE {where} "
            "ORDER BY p.academic_year DESC, p.exam_type DESC, q.section_code, "
            "CAST(q.number_label AS INTEGER), q.question_id"
        )
        return [str(row[0]) for row in self.conn.execute(sql, params).fetchall()]

    def count_questions(self, **filters: Any) -> int:
        where, params = self._where(**filters)
        sql = (
            "SELECT COUNT(*) FROM questions q "
            "JOIN papers p ON p.paper_id = q.paper_id "
            f"WHERE {where}"
        )
        return int(self.conn.execute(sql, params).fetchone()[0])

    # ------------------------------------------------------------------
    # 回取
    # ------------------------------------------------------------------

    def papers(self) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM papers").fetchall()
        return {str(row["paper_id"]): dict(row) for row in rows}

    def get_paper(self, paper_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM papers WHERE paper_id = ?", (paper_id,)).fetchone()
        return dict(row) if row else None

    def fetch_summaries(self, question_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not question_ids:
            return {}
        placeholders = ",".join("?" for _ in question_ids)
        rows = self.conn.execute(
            "SELECT q.*, p.title AS paper_title, p.exam_type, p.academic_year "
            "FROM questions q JOIN papers p ON p.paper_id = q.paper_id "
            f"WHERE q.question_id IN ({placeholders})",
            question_ids,
        ).fetchall()
        records = {str(row["question_id"]): self._row_to_record(row) for row in rows}
        self._attach_children(records)
        return records

    def fetch_detail(self, question_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT q.*, p.title AS paper_title, p.exam_type, p.academic_year "
            "FROM questions q JOIN papers p ON p.paper_id = q.paper_id "
            "WHERE q.question_id = ?",
            (question_id,),
        ).fetchone()
        if row is None:
            return None
        record = self._row_to_record(row)
        record["answer_json"] = json.loads(row["answer_json"]) if row["answer_json"] else None
        record["reference_code"] = row["reference_code"]
        record["reference_code_pages"] = json.loads(row["reference_code_pages_json"] or "[]")
        self._attach_children({question_id: record})
        record["independent_solutions"] = [
            dict(item)
            for item in self.conn.execute(
                "SELECT author, text, note FROM independent_solutions "
                "WHERE question_id = ? ORDER BY ordinal",
                (question_id,),
            )
        ]
        record["answer_sources"] = [
            {
                **dict(item),
                "pages": json.loads(item["pages_json"] or "[]"),
            }
            for item in self.conn.execute(
                "SELECT kind, text, detail, pairing_verified, pages_json FROM answer_sources "
                "WHERE question_id = ? ORDER BY ordinal",
                (question_id,),
            )
        ]
        record["code_blocks"] = [
            {
                **dict(item),
                "pages": json.loads(item["pages_json"] or "[]"),
                "depends_on": json.loads(item["depends_on_json"] or "[]"),
                "bbox": json.loads(item["bbox_json"] or "null"),
            }
            for item in self.conn.execute(
                "SELECT * FROM code_blocks WHERE question_id = ? ORDER BY ordinal",
                (question_id,),
            )
        ]
        record["code_annotations"] = [
            {
                **dict(item),
                "bbox": json.loads(item["bbox_json"] or "null"),
            }
            for item in self.conn.execute(
                "SELECT * FROM code_annotations WHERE question_id = ? ORDER BY ordinal",
                (question_id,),
            )
        ]
        record["code_examples"] = [
            dict(item)
            for item in self.conn.execute(
                "SELECT label, text FROM code_examples WHERE question_id = ? ORDER BY ordinal",
                (question_id,),
            )
        ]
        return record

    def _row_to_record(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "question_id": row["question_id"],
            "paper_id": row["paper_id"],
            "paper_title": row["paper_title"],
            "course": row["course"],
            "exam_type": row["exam_type"],
            "academic_year": row["academic_year"],
            "section_code": row["section_code"],
            "section_title": row["section_title"],
            "number_label": row["number_label"],
            "question_type": row["question_type"],
            "statement": row["statement"],
            "content_sha256": row["content_sha256"],
            "automated_status": row["automated_status"],
            "visual_status": row["visual_status"],
            "review_status": row["review_status"],
            "content_status": row["content_status"],
            "content_issue": row["content_issue"],
            "searchable": bool(row["searchable"]),
            "answer_state": row["answer_state"],
            "answer_available": bool(row["answer_available"]),
            "answer_label": row["answer_label"],
            "pages_statement": json.loads(row["pages_statement_json"] or "[]"),
            "pages_answer": json.loads(row["pages_answer_json"] or "[]"),
            "options": [],
            "code_lines": [],
            "blank_marks": [],
            "formulas": [],
            "assets": [],
            "knowledge_points": [],
        }

    def _attach_children(self, records: dict[str, dict[str, Any]]) -> None:
        ids = list(records)
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        for row in self.conn.execute(
            f"SELECT question_id, ordinal, label, text FROM options WHERE question_id IN ({placeholders}) ORDER BY ordinal",
            ids,
        ):
            records[row["question_id"]]["options"].append(
                {"label": row["label"], "text": row["text"]}
            )
        for row in self.conn.execute(
            f"SELECT question_id, ordinal, text FROM code_lines WHERE question_id IN ({placeholders}) ORDER BY ordinal",
            ids,
        ):
            records[row["question_id"]]["code_lines"].append(row["text"])
        for row in self.conn.execute(
            f"SELECT question_id, ordinal, blank_no FROM blank_marks WHERE question_id IN ({placeholders}) ORDER BY ordinal",
            ids,
        ):
            records[row["question_id"]]["blank_marks"].append(int(row["blank_no"]))
        for row in self.conn.execute(
            f"SELECT question_id, ordinal, text, page FROM formulas WHERE question_id IN ({placeholders}) ORDER BY ordinal",
            ids,
        ):
            records[row["question_id"]]["formulas"].append(
                {"text": row["text"], "page": row["page"]}
            )
        for row in self.conn.execute(
            f"SELECT * FROM assets WHERE question_id IN ({placeholders}) ORDER BY ordinal",
            ids,
        ):
            records[row["question_id"]]["assets"].append(
                {
                    "asset_id": row["asset_id"],
                    "page": row["page"],
                    "bbox": json.loads(row["bbox_json"] or "[]"),
                    "description": row["description"],
                }
            )
        for row in self.conn.execute(
            f"SELECT k.question_id, k.kp_slug, k.status, p.name FROM question_knowledge_points k "
            f"JOIN knowledge_points p ON p.slug = k.kp_slug "
            f"WHERE k.question_id IN ({placeholders}) ORDER BY k.ordinal",
            ids,
        ):
            records[row["question_id"]]["knowledge_points"].append(
                {"slug": row["kp_slug"], "name": row["name"], "status": row["status"]}
            )

    # ------------------------------------------------------------------
    # 统计与词表
    # ------------------------------------------------------------------

    def course_stats(self, course: str) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT question_type, COUNT(*) AS total, "
            "SUM(searchable) AS searchable, SUM(answer_available) AS answered "
            "FROM questions WHERE course = ? GROUP BY question_type",
            (course,),
        ).fetchall()
        by_type = {str(row["question_type"]): int(row["total"]) for row in rows}
        searchable_by_type = {
            str(row["question_type"]): int(row["searchable"] or 0) for row in rows
        }
        total_row = self.conn.execute(
            "SELECT COUNT(*) AS total, SUM(searchable) AS searchable, "
            "SUM(answer_available) AS answered FROM questions WHERE course = ?",
            (course,),
        ).fetchone()
        return {
            "question_count": int(total_row["total"] or 0),
            "searchable_count": int(total_row["searchable"] or 0),
            "answered_count": int(total_row["answered"] or 0),
            "by_type": by_type,
            "searchable_by_type": searchable_by_type,
        }

    def verified_label_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM question_knowledge_points WHERE status = 'verified'"
        ).fetchone()
        return int(row[0])

    def all_knowledge_points(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT slug, name, aliases_json FROM knowledge_points ORDER BY sort_order, slug"
        ).fetchall()
        return [
            {
                "slug": row["slug"],
                "name": row["name"],
                "aliases": json.loads(row["aliases_json"] or "[]"),
            }
            for row in rows
        ]

    def fragment_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM fragments").fetchone()[0])

    def question_hashes(self) -> dict[str, str]:
        rows = self.conn.execute("SELECT question_id, content_sha256 FROM questions").fetchall()
        return {str(row["question_id"]): str(row["content_sha256"]) for row in rows}
