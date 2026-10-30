"""离线应用导师逐题校订；原始抽取文件保持不变。"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from tools.question_bank.db import content_sha256


class CurationError(ValueError):
    pass


PATCH_FIELDS = {
    "statement": str,
    "options": list,
    "code_lines": list,
    "reference_code": str,
    "code_blocks": list,
    "code_annotations": list,
    "independent_solutions": list,
    "answer": dict,
    "pages": dict,
}


def apply_corrections(records: list[dict], papers: list[dict], path: Path) -> list[dict]:
    """仅匹配原内容哈希和原卷指纹的校订可用；任何过期项使整次构建失败。"""
    if not path.exists():
        return records
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CurationError("导师校订文件无法读取") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise CurationError("导师校订 schema_version 不支持")
    corrections = payload.get("corrections")
    if not isinstance(corrections, list):
        raise CurationError("corrections 必须是列表")
    by_id = {record["question_id"]: record for record in records}
    by_paper = {paper["paper_id"]: paper for paper in papers}
    changes: dict[str, dict] = {}
    for item in corrections:
        if not isinstance(item, dict):
            raise CurationError("校订项必须是对象")
        qid = item.get("question_id")
        if not isinstance(qid, str) or qid not in by_id or qid in changes:
            raise CurationError(f"未知或重复校订题号：{qid}")
        original = by_id[qid]
        if item.get("before_content_sha256") != content_sha256(original):
            raise CurationError(f"{qid} 原内容哈希变化，必须重新审核")
        paper = by_paper.get(original["paper_id"], {})
        if not paper.get("pdf_sha256") or item.get("pdf_sha256") != paper["pdf_sha256"]:
            raise CurationError(f"{qid} 原卷指纹变化，必须重新审核")
        pages = item.get("evidence_pages")
        if (not isinstance(pages, list) or not pages or
                any(type(page) is not int or not 1 <= page <= paper["physical_pages"] for page in pages)):
            raise CurationError(f"{qid} 缺少有效原卷物理页证据")
        if not item.get("reason") or not item.get("reviewed_by"):
            raise CurationError(f"{qid} 缺少校订理由或审核者")
        patch = item.get("patch")
        if not isinstance(patch, dict) or not patch:
            raise CurationError(f"{qid} 缺少校订字段")
        for field, value in patch.items():
            if field not in PATCH_FIELDS or not isinstance(value, PATCH_FIELDS[field]):
                raise CurationError(f"{qid} 不允许校订字段或类型：{field}")
        if "answer" in patch:
            # 原卷答案只允许追加说明；导师解答必须独立保存。
            prior = {k: v for k, v in (original.get("answer") or {}).items() if k != "note"}
            after = {k: v for k, v in patch["answer"].items() if k != "note"}
            if prior != after:
                raise CurationError(f"{qid} 禁止覆写原卷答案，使用 independent_solutions")
        changed = deepcopy(original)
        changed.update(deepcopy(patch))
        changes[qid] = changed
    return [changes.get(record["question_id"], record) for record in records]
