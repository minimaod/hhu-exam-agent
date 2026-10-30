"""F007 剩余题目视觉复核与逐题证据生成。

离线工具，不进入应用运行时。流程：
prepare  渲染剩余题涉及的题面/答案页，生成页面清单和逐页任务；
pilot    先跑 12 道真实困难样例，失败率过高时停止；
run      并发调用视觉模型，保存每次输入哈希、原始响应和解析结果；
assemble 把页面任务合并成逐题结果，做程序比对并生成复审清单；
annotate 在隔离候选 build 建好后补记校订后哈希；
verify   对账 1071 题、83 题保护、54 题缺答案和 2 题隔离。

模型输出只作为待比对资料。本模块不执行模型返回的任何文本，也不根据它
修改题库；校订由独立的人工/程序门槛另行生成并只在隔离 build 中应用。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pymupdf

from app.question_bank.config import DEFAULT_PAPERS_ROOT, PROJECT_ROOT
from app.question_bank.paths import BankPaths, read_current
from app.question_bank.repository import QuestionRepository
from tools.question_bank.db import content_payload
from tools.question_bank.publish import candidate_ids
from tools.question_bank.vision import (
    VisionClient,
    VisionError,
    VisionSettings,
    VisionTruncated,
    VisionUnsupported,
)
from tools.question_bank.visual_compare import (
    classify,
    compare_blanks,
    compare_choice_answer,
    compare_code,
    compare_fill_answer,
    compare_options,
    compare_text,
    compact,
    embedded_code,
    len_literal_check,
    next_question_contamination,
)

DEFAULT_OUT = PROJECT_ROOT / "data" / "local" / "question-bank" / "review" / "visual"
PAGE_DPI = 170
PILOT_IDS = [
    "py-2021-2022-s1-midterm-fill-02",
    "py-2020-2021-s1-final-choice-10",
    "py-2020-2021-s1-final-choice-04",
    "py-2022-2023-s2-midterm-code-02",
    "py-2024-2025-s2-midterm-code-02",
    "py-2020-2021-s2-final-fill-15",
    "py-2023-2024-s2-midterm-code-01",
    "py-2022-2023-s1-midterm-fill-16",
    "py-2021-2022-s2-midterm-choice-30",
    "py-2022-2023-s1-final-choice-11",
    "py-2023-2024-s1-final-choice-19",
    "py-2021-2022-s1-midterm-fill-12",
]
ISOLATED_IDS = {
    "py-2022-2023-s1-final-choice-11",
    "py-2023-2024-s1-final-choice-19",
}
_MARK_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha_json(payload: Any) -> str:
    material = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _payload_without_solutions(record: dict[str, Any]) -> dict[str, Any]:
    payload = content_payload(record)
    payload["independent_solutions"] = []
    return payload


def _sha_without_solutions(record: dict[str, Any]) -> str:
    return _sha_json(_payload_without_solutions(record))


def _norm_number(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"\d+", text)
    return str(int(match.group())) if match else text


def _paper_pdf(paper: dict[str, Any]) -> Path:
    rel = paper.get("pdf_rel_path")
    if not rel:
        raise SystemExit(f"卷 {paper.get('paper_id')} 没有 pdf_rel_path")
    return DEFAULT_PAPERS_ROOT / str(rel)


def _render(pdf_path: Path, page_no: int, target: Path, *, clip: tuple[float, float] | None = None) -> None:
    if target.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(pdf_path)
    try:
        page = doc[page_no - 1]
        rect = page.rect
        clip_rect = None
        if clip is not None:
            y0, y1 = clip
            clip_rect = pymupdf.Rect(rect.x0, rect.y0 + rect.height * y0, rect.x1, rect.y0 + rect.height * y1)
        pix = page.get_pixmap(dpi=PAGE_DPI, colorspace=pymupdf.csRGB, alpha=False, clip=clip_rect)
        target.write_bytes(pix.tobytes("png"))
    finally:
        doc.close()


def _load_inventory(bank_root: Path) -> dict[str, Any]:
    paths = BankPaths(bank_root)
    current = read_current(paths)
    if current is None:
        raise SystemExit("题目库尚未建立")
    repository = QuestionRepository(paths.build_dir(current["build_id"]))
    try:
        ids = [
            str(row[0])
            for row in repository.conn.execute(
                "SELECT question_id FROM questions ORDER BY paper_id, question_type, "
                "CAST(number_label AS INTEGER), question_id"
            )
        ]
        summaries = repository.fetch_summaries(ids)
        records = {}
        for question_id in ids:
            detail = repository.fetch_detail(question_id)
            if detail is not None:
                records[question_id] = detail
        papers = repository.papers()
        reviews_path = paths.reviews_file
        reviews = json.loads(reviews_path.read_text(encoding="utf-8")) if reviews_path.is_file() else {}
    finally:
        repository.close()

    approved = {str(key) for key in (reviews.get("decisions") or {})}
    published_candidates, _ = candidate_ids()
    # candidate_ids 还包含两道被排除的双 B 题；视觉复核的保护范围是实际已批准的 83 题。
    first83 = set(published_candidates) & approved
    isolated = {
        question_id
        for question_id, record in records.items()
        if question_id in ISOLATED_IDS
        or (
            record.get("question_type") == "choice"
            and [str(item.get("label")) for item in record.get("options") or []] == ["A", "B", "B", "D"]
        )
    }
    remaining = [question_id for question_id in ids if question_id not in approved]
    missing = [
        question_id
        for question_id in remaining
        if (records[question_id].get("answer_json") or {}).get("state") == "missing"
    ]
    protected = [question_id for question_id in ids if question_id in approved]
    return {
        "bank_root": str(bank_root),
        "build_id": current["build_id"],
        "source_version": current["source_version"],
        "generated_at": _now(),
        "records": records,
        "papers": papers,
        "ids": ids,
        "approved": sorted(approved),
        "first83": sorted(first83),
        "remaining": remaining,
        "missing": missing,
        "isolated": sorted(isolated),
        "protected": protected,
        "counts": {
            "total": len(ids),
            "approved": len(protected),
            "remaining": len(remaining),
            "normal": len(remaining) - len(isolated),
            "isolated": len(isolated),
            "missing": len(missing),
            "by_type": dict(Counter(records[qid]["question_type"] for qid in remaining)),
            "remaining_by_answer": dict(
                Counter((records[qid].get("answer_json") or {}).get("state") for qid in remaining)
            ),
        },
    }


def _aliases(record: dict[str, Any], page: int, overrides: dict[str, Any]) -> list[str]:
    values = []
    paper = record["paper_id"]
    for item in (overrides.get(paper) or {}).get("code_label_fixes") or []:
        if int(item.get("canonical", -1)) == int(record.get("number_label", -1)):
            values.append(_norm_number(item.get("printed")))
    return [value for value in values if value and value != _norm_number(record.get("number_label"))]


def _expected_item(record: dict[str, Any], *, aliases: list[str] | None = None) -> dict[str, Any]:
    answer = record.get("answer_json") or {}
    return {
        "question_id": record["question_id"],
        "number": _norm_number(record.get("number_label")),
        "aliases": list(aliases or []),
        "type": record["question_type"],
        "answer_state": answer.get("state"),
        "blank_marks": [int(value) for value in record.get("blank_marks") or []],
        "statement_pages": list(record.get("pages_statement") or []),
        "answer_pages": list(record.get("pages_answer") or []),
    }


def _prompt_statement(paper_title: str, page: int, expected: list[dict[str, Any]], *, single: bool = False) -> str:
    numbers = "、".join(item["number"] for item in expected[:40])
    scope = (
        f"只转写第 {expected[0]['number']} 题；本页其他题忽略。"
        if single and expected
        else f"本页应出现这些题号：{numbers}。"
    )
    return f"""你在核对一份中文 Python 考试 PDF 的物理第 {page} 页（{paper_title}）。
{scope}

要求：
1. 只根据图片中真实可见的内容转写，不要补全、不要猜测、不要把相邻题合并。
2. 逐题保留题号、中文题干、四个选项（A-D）、代码、公式、空位标记 (n) 和续行。
3. 代码必须保留每一行前导空格；不要把下一题内容写进当前题。
4. 如果题目从上一页继续或延续到下一页，在 continuation 写 from_previous/to_next/both。
5. 任何看不清、被裁掉或不确定的内容放进 uncertain；不要用常识补。
6. 只返回 JSON 对象，不要 markdown。

JSON schema:
{{"page":{page},"questions":[{{"number":"1","statement":"...","options":[{{"label":"A","text":"..."}}],"code":"","continuation":"none","confidence":"high|medium|low","uncertain":[]}}],"uncertain":[],"notes":""}}"""


def _prompt_answer(paper_title: str, page: int, expected: list[dict[str, Any]]) -> str:
    choice = [item["number"] for item in expected if item["type"] == "choice"]
    fill = sorted({int(blank) for item in expected for blank in item.get("blank_marks") or []})
    code = [item["number"] for item in expected if item["type"] == "code"]
    return f"""你在核对一份中文 Python 考试 PDF 的物理第 {page} 页答案（{paper_title}）。
本页应出现的答案槽：
- 选择题题号：{"、".join(choice) if choice else "无"}
- 填空印刷空号：{"、".join(map(str, fill)) if fill else "无"}
- 编程题号：{"、".join(code) if code else "无"}

要求：
1. 逐项读取原卷答案，不要根据题目内容推导或改写。
2. 选择题按题号输出字母；涂黑格看不清时写 null 并列入 uncertain。
3. 填空按原卷印刷空号输出原始字符串，保留引号、反斜杠、分号和换行；不要按数组下标重排。
4. 编程答案按题号输出代码，严格保留缩进、空行和方法编号；跨页代码在 continuation 说明。
5. 看不清的内容写 null 并列入 uncertain，不要猜。
6. 只返回 JSON 对象，不要 markdown。

JSON schema:
{{"page":{page},"choice":{{"1":"A"}},"fill":{{"41":"..."}},"code":[{{"question":"1","text":"","continuation":"none","confidence":"high|medium|low"}}],"uncertain":[],"notes":""}}"""


def _prompt_code_answer(paper_title: str, expected: dict[str, Any], pages: list[int]) -> str:
    return f"""你在核对一份中文 Python 考试 PDF 的编程题参考答案（{paper_title}）。
目标：只转写第 {expected['number']} 题的参考代码；相关物理页为 {pages}，图片按页顺序给出。

要求：
1. 严格保留缩进、空行、注释、方法编号和字符串中的反斜杠。
2. 不要把上一题/下一题代码混入；如果代码跨页，在 continuation 写 to_next/from_previous/both。
3. 只转写图片中真实可见的代码，不补全缺失行；看不清的写进 uncertain。
4. 只返回 JSON 对象，不要 markdown。

JSON schema:
{{"page":{pages[0] if pages else 0},"code":[{{"question":"{expected['number']}","text":"","continuation":"none","confidence":"high|medium|low"}}],"uncertain":[],"notes":""}}"""


def build_jobs(inventory: dict[str, Any], *, ids: Iterable[str] | None = None, single: bool = False) -> list[dict[str, Any]]:
    selected = set(ids) if ids is not None else set(inventory["remaining"])
    records = inventory["records"]
    overrides_path = Path(inventory["bank_root"]) / "overrides.json"
    overrides = json.loads(overrides_path.read_text(encoding="utf-8")) if overrides_path.is_file() else {}
    papers = inventory["papers"]
    jobs: dict[tuple[str, int, str], dict[str, Any]] = {}

    for question_id in sorted(selected):
        record = records.get(question_id)
        if record is None:
            continue
        paper = papers[record["paper_id"]]
        for page in record.get("pages_statement") or []:
            key = (record["paper_id"], int(page), "statement")
            job = jobs.setdefault(
                key,
                {
                    "job_id": f"{key[0]}:p{key[1]}:{key[2]}",
                    "paper_id": key[0],
                    "page": key[1],
                    "role": key[2],
                    "paper_title": paper["title"],
                    "expected": [],
                    "single": single,
                },
            )
            job["expected"].append(_expected_item(record, aliases=_aliases(record, int(page), overrides)))
        for page in record.get("pages_answer") or []:
            key = (record["paper_id"], int(page), "answer")
            job = jobs.setdefault(
                key,
                {
                    "job_id": f"{key[0]}:p{key[1]}:{key[2]}",
                    "paper_id": key[0],
                    "page": key[1],
                    "role": key[2],
                    "paper_title": paper["title"],
                    "expected": [],
                    "single": single,
                },
            )
            job["expected"].append(_expected_item(record, aliases=_aliases(record, int(page), overrides)))

        statement_pages = [int(page) for page in record.get("pages_statement") or []]
        if record["question_type"] == "code":
            if statement_pages:
                key = (record["paper_id"], statement_pages[0], "statement_code")
                jobs[key] = {
                    "job_id": f"{key[0]}:p{key[1]}:{key[2]}:{_norm_number(record['number_label'])}",
                    "paper_id": key[0],
                    "page": key[1],
                    "role": key[2],
                    "paper_title": paper["title"],
                    "expected": [_expected_item(record)],
                    "single": True,
                    "pages": statement_pages,
                }
        elif len(statement_pages) > 1:
            key = (record["paper_id"], statement_pages[0], "statement_question")
            jobs[key] = {
                "job_id": f"{key[0]}:p{key[1]}:{key[2]}:{_norm_number(record['number_label'])}",
                "paper_id": key[0],
                "page": key[1],
                "role": key[2],
                "paper_title": paper["title"],
                "expected": [_expected_item(record)],
                "single": True,
                "pages": statement_pages,
            }
        if record["question_type"] == "code":
            answer_pages = [int(page) for page in record.get("pages_answer") or []]
            if answer_pages:
                key = (record["paper_id"], answer_pages[0], "code_answer")
                jobs[key] = {
                    "job_id": f"{key[0]}:p{key[1]}:{key[2]}:{_norm_number(record['number_label'])}",
                    "paper_id": key[0],
                    "page": key[1],
                    "role": key[2],
                    "paper_title": paper["title"],
                    "expected": [_expected_item(record)],
                    "single": True,
                    "pages": answer_pages,
                }
    return sorted(jobs.values(), key=lambda item: (item["paper_id"], item["page"], item["role"], item["job_id"]))


def _job_images(job: dict[str, Any], page_dir: Path, *, split: bool = False) -> list[Path]:
    pages = job.get("pages") or [job["page"]]
    paper_dir = page_dir / job["paper_id"]
    paths: list[Path] = []
    for page in pages:
        stem = f"p{int(page):03d}"
        if split:
            paths.extend([paper_dir / f"{stem}-top.png", paper_dir / f"{stem}-bottom.png"])
        else:
            paths.append(paper_dir / f"{stem}.png")
    return paths


def _job_prompt(job: dict[str, Any]) -> str:
    role = job["role"]
    if role == "answer":
        return _prompt_answer(job["paper_title"], job["page"], job["expected"])
    if role == "code_answer":
        return _prompt_code_answer(job["paper_title"], job["expected"][0], job.get("pages") or [job["page"]])
    return _prompt_statement(job["paper_title"], job["page"], job["expected"], single=bool(job.get("single")))


def _merge_questions(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for parsed in parts:
        for item in parsed.get("questions") or []:
            if not isinstance(item, dict):
                continue
            number = _norm_number(item.get("number"))
            if not number:
                continue
            prior = merged.get(number)
            if prior is None:
                merged[number] = item
                continue
            prior_text = str(prior.get("statement") or "")
            new_text = str(item.get("statement") or "")
            if len(new_text) > len(prior_text):
                merged[number] = item
            if not prior.get("options") and item.get("options"):
                merged[number]["options"] = item["options"]
            if not prior.get("code") and item.get("code"):
                merged[number]["code"] = item["code"]
    return [merged[key] for key in sorted(merged, key=lambda value: int(value) if value.isdigit() else value)]


def _merge_parsed(role: str, parts: list[dict[str, Any]]) -> dict[str, Any]:
    if role in ("statement", "statement_code"):
        return {
            "page": parts[0].get("page") if parts else None,
            "questions": _merge_questions(parts),
            "uncertain": [item for parsed in parts for item in (parsed.get("uncertain") or [])],
            "notes": " / ".join(str(parsed.get("notes") or "") for parsed in parts if parsed.get("notes")),
        }
    if role == "code_answer":
        blocks = [item for parsed in parts for item in (parsed.get("code") or [])]
        return {
            "page": parts[0].get("page") if parts else None,
            "code": blocks,
            "uncertain": [item for parsed in parts for item in (parsed.get("uncertain") or [])],
            "notes": " / ".join(str(parsed.get("notes") or "") for parsed in parts if parsed.get("notes")),
        }
    choice: dict[str, Any] = {}
    fill: dict[str, Any] = {}
    code = []
    uncertain = []
    for parsed in parts:
        choice.update(parsed.get("choice") or {})
        fill.update(parsed.get("fill") or {})
        code.extend(parsed.get("code") or [])
        uncertain.extend(parsed.get("uncertain") or [])
    return {
        "page": parts[0].get("page") if parts else None,
        "choice": choice,
        "fill": fill,
        "code": code,
        "uncertain": uncertain,
        "notes": " / ".join(str(parsed.get("notes") or "") for parsed in parts if parsed.get("notes")),
    }


def _write_call(calls_dir: Path, paper_id: str, call: dict[str, Any]) -> None:
    calls_dir.mkdir(parents=True, exist_ok=True)
    path = calls_dir / f"{paper_id}.jsonl"
    with _MARK_LOCK:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(call, ensure_ascii=False) + "\n")


def _execute_job(
    job: dict[str, Any],
    *,
    page_dir: Path,
    jobs_dir: Path,
    calls_dir: Path,
    client: VisionClient,
) -> dict[str, Any]:
    prompt = _job_prompt(job)
    result = {
        "job_id": job["job_id"],
        "paper_id": job["paper_id"],
        "page": job["page"],
        "role": job["role"],
        "model": client.settings.model,
        "expected": job["expected"],
        "status": "failed",
        "parsed": None,
        "call_ids": [],
        "errors": [],
        "images": [],
        "created_at": _now(),
    }
    attempts = []
    for split in (False, True):
        images = _job_images(job, page_dir, split=split)
        if not images or any(not path.is_file() for path in images):
            result["errors"].append("缺少页面图片")
            break
        try:
            call = client.complete_json(images, prompt)
        except VisionUnsupported as exc:
            result["errors"].append(f"{exc.code}: {exc}")
            break
        except VisionError as exc:
            result["errors"].append(f"{exc.code}: {exc}")
            continue
        attempts.append(call.to_dict())
        result["call_ids"].append(call.call_id)
        result["images"] = call.image_paths
        if call.status == "success" and call.parsed is not None:
            result["status"] = "success"
            result["parsed"] = call.parsed
            result["errors"] = []
            break
        result["errors"].append(f"{call.error_code or 'vision_failed'}: {call.error_message or ''}")
        if call.error_code == "vision_auth":
            break
    result["attempts"] = attempts
    for call in attempts:
        _write_call(calls_dir, job["paper_id"], call)
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / f"{job['job_id'].replace(':', '_')}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def run_jobs(
    jobs: list[dict[str, Any]],
    *,
    pages_dir: Path,
    jobs_dir: Path,
    calls_dir: Path,
    client: VisionClient,
    workers: int = 3,
    limit: int | None = None,
) -> dict[str, Any]:
    jobs_dir.mkdir(parents=True, exist_ok=True)
    calls_dir.mkdir(parents=True, exist_ok=True)
    existing = {}
    for path in jobs_dir.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        existing[str(payload.get("job_id"))] = payload
    pending = []
    for job in jobs[: limit if limit is not None else None]:
        prior = existing.get(job["job_id"])
        if prior and prior.get("status") == "success":
            continue
        pending.append(job)
    results = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(
                _execute_job,
                job,
                page_dir=pages_dir,
                jobs_dir=jobs_dir,
                calls_dir=calls_dir,
                client=client,
            ): job
            for job in pending
        }
        for index, future in enumerate(as_completed(futures), start=1):
            result = future.result()
            results.append(result)
            if index % 10 == 0 or index == len(futures):
                print(f"  视觉任务 {index}/{len(futures)}：{result['job_id']} {result['status']}")
    successes = sum(1 for item in results if item["status"] == "success")
    return {
        "requested": len(jobs[: limit if limit is not None else None]),
        "pending": len(pending),
        "success": successes,
        "failed": len(results) - successes,
        "skipped": len(jobs[: limit if limit is not None else None]) - len(pending),
    }


def prepare(bank_root: Path, out: Path) -> dict[str, Any]:
    inventory = _load_inventory(bank_root)
    out.mkdir(parents=True, exist_ok=True)
    pages_dir = out / "pages"
    page_manifest: dict[str, Any] = {}
    needed: set[tuple[str, int]] = set()
    for question_id in inventory["remaining"]:
        record = inventory["records"][question_id]
        for page in record.get("pages_statement") or []:
            needed.add((record["paper_id"], int(page)))
        for page in record.get("pages_answer") or []:
            needed.add((record["paper_id"], int(page)))
    for paper_id, page in sorted(needed):
        paper = inventory["papers"][paper_id]
        pdf = _paper_pdf(paper)
        if not pdf.is_file():
            raise SystemExit(f"原 PDF 不存在：{pdf}")
        paper_dir = pages_dir / paper_id
        full = paper_dir / f"p{page:03d}.png"
        top = paper_dir / f"p{page:03d}-top.png"
        bottom = paper_dir / f"p{page:03d}-bottom.png"
        _render(pdf, page, full)
        _render(pdf, page, top, clip=(0.0, 0.56))
        _render(pdf, page, bottom, clip=(0.44, 1.0))
        page_manifest[f"{paper_id}:p{page}"] = {
            "paper_id": paper_id,
            "page": page,
            "full": str(full.relative_to(out)),
            "top": str(top.relative_to(out)),
            "bottom": str(bottom.relative_to(out)),
            "full_sha256": _sha_file(full),
            "top_sha256": _sha_file(top),
            "bottom_sha256": _sha_file(bottom),
        }
    inventory["page_manifest"] = page_manifest
    inventory["protected_hashes"] = {
        question_id: inventory["records"][question_id]["content_sha256"]
        for question_id in inventory["protected"]
    }
    (out / "inventory.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "protected-83.json").write_text(
        json.dumps(
            {
                "build_id": inventory["build_id"],
                "count": len(inventory["protected"]),
                "questions": [
                    {
                        "question_id": question_id,
                        "content_sha256": inventory["records"][question_id]["content_sha256"],
                        "paper_id": inventory["records"][question_id]["paper_id"],
                        "pages_statement": inventory["records"][question_id]["pages_statement"],
                        "pages_answer": inventory["records"][question_id]["pages_answer"],
                    }
                    for question_id in inventory["protected"]
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps(inventory["counts"], ensure_ascii=False, indent=2))
    return inventory["counts"]


def _load_jobs(jobs_dir: Path) -> dict[tuple[str, int, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, int, str], list[dict[str, Any]]] = defaultdict(list)
    for path in sorted(jobs_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("status") != "success" or not payload.get("parsed"):
            continue
        grouped[(payload["paper_id"], int(payload["page"]), payload["role"])].append(payload)
    return grouped


def _question_from_jobs(
    jobs: dict[tuple[str, int, str], list[dict[str, Any]]],
    record: dict[str, Any],
    page: int,
) -> dict[str, Any] | None:
    aliases: set[str] = set()
    for role in ("statement", "statement_question", "statement_code"):
        for job in jobs.get((record["paper_id"], page, role), []):
            for entry in job.get("expected") or []:
                if entry.get("question_id") == record["question_id"]:
                    aliases.update(_norm_number(value) for value in entry.get("aliases") or [])
    candidates = []
    for role in ("statement_question", "statement_code", "statement"):
        for job in jobs.get((record["paper_id"], page, role), []):
            for item in (job.get("parsed") or {}).get("questions") or []:
                if not isinstance(item, dict):
                    continue
                number = _norm_number(item.get("number"))
                if number == _norm_number(record.get("number_label")) or number in aliases:
                    candidates.append((role, job, item))
    if not candidates:
        return None
    role, job, item = candidates[0]
    return {
        "role": role,
        "number": item.get("number"),
        "statement": str(item.get("statement") or ""),
        "options": item.get("options") or [],
        "code": str(item.get("code") or ""),
        "continuation": item.get("continuation") or "none",
        "confidence": item.get("confidence") or "low",
        "uncertain": item.get("uncertain") or [],
        "job_id": job["job_id"],
        "call_ids": job.get("call_ids") or [],
        "image_sha256": [Path(path).name for path in job.get("images") or []],
    }


def _answer_observations(
    jobs: dict[tuple[str, int, str], list[dict[str, Any]]],
    record: dict[str, Any],
) -> dict[str, Any] | None:
    if not record.get("pages_answer"):
        return None
    choice = None
    fill: dict[str, Any] = {}
    code_blocks: list[dict[str, Any]] = []
    call_ids: list[str] = []
    confidence = "high"
    uncertain: list[Any] = []
    for page in record.get("pages_answer") or []:
        for job in jobs.get((record["paper_id"], int(page), "answer"), []):
            parsed = job.get("parsed") or {}
            choice = choice if choice is not None else (parsed.get("choice") or {}).get(
                _norm_number(record["number_label"])
            )
            for key, value in (parsed.get("fill") or {}).items():
                fill[str(key)] = value
            code_blocks.extend(parsed.get("code") or [])
            call_ids.extend(job.get("call_ids") or [])
        for job in jobs.get((record["paper_id"], int(page), "code_answer"), []):
            parsed = job.get("parsed") or {}
            for item in parsed.get("code") or []:
                if _norm_number(item.get("question")) == _norm_number(record["number_label"]):
                    code_blocks.append(item)
                    confidence = item.get("confidence") or confidence
            call_ids.extend(job.get("call_ids") or [])
            uncertain.extend(parsed.get("uncertain") or [])
    if not call_ids:
        return None
    return {
        "choice": choice,
        "fill": fill,
        "code": code_blocks,
        "call_ids": call_ids,
        "confidence": confidence,
        "uncertain": uncertain,
    }


def _looks_like_code(text: str) -> bool:
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return False
    code_starts = re.compile(r"^\s*(import|from|def|class|for|while|if|elif|else|print|return|with|try|except)\b")
    return any(code_starts.search(line) or "=" in line or "(" in line for line in lines)


def _fill_expected_map(record: dict[str, Any]) -> dict[str, str]:
    answer = record.get("answer_json") or {}
    text = str(answer.get("text") or "")
    blanks = [int(value) for value in record.get("blank_marks") or []]
    mapped: dict[str, str] = {}
    if not blanks:
        return mapped
    markers = list(re.finditer(r"[（(]\s*(\d{1,3})\s*[)）]", text))
    if markers:
        for index, marker in enumerate(markers):
            number = int(marker.group(1))
            if number not in blanks:
                continue
            end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
            mapped[str(number)] = text[marker.end() : end].strip(" ;；,，\n")
    if not mapped and len(blanks) == 1:
        mapped[str(blanks[0])] = text.strip()
    if not mapped:
        pieces = [piece.strip() for piece in re.split(r"[;；]", text) if piece.strip()]
        if len(pieces) == len(blanks):
            mapped = {str(blank): piece for blank, piece in zip(blanks, pieces)}
    return mapped


def _reference_code(record: dict[str, Any]) -> str:
    if record.get("reference_code"):
        return str(record["reference_code"])
    blocks = record.get("code_blocks") or []
    return "\n".join(str(item.get("text") or "") for item in blocks)


def _next_record(
    record: dict[str, Any],
    ordered: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for index, item in enumerate(ordered):
        if item["question_id"] == record["question_id"]:
            return ordered[index + 1] if index + 1 < len(ordered) else None
    return None


def _result_for(
    record: dict[str, Any],
    *,
    jobs: dict[tuple[str, int, str], list[dict[str, Any]]],
    ordered: list[dict[str, Any]],
    isolated: bool,
) -> dict[str, Any]:
    answer = record.get("answer_json") or {}
    missing = answer.get("state") == "missing"
    page_obs = []
    pages_statement = [int(page) for page in record.get("pages_statement") or []]
    if pages_statement:
        first = _question_from_jobs(jobs, record, pages_statement[0])
        if first and first.get("role") in ("statement_question", "statement_code"):
            page_obs.append(first)
        else:
            for page in pages_statement:
                observed = _question_from_jobs(jobs, record, page)
                if observed:
                    page_obs.append(observed)
    observed_statement = "\n".join(item["statement"] for item in page_obs if item["statement"])
    observed_options = next((item["options"] for item in page_obs if item["options"]), [])
    observed_code = "\n".join(item["code"] for item in page_obs if item["code"])
    if (
        record.get("question_type") != "code"
        and not record.get("code_lines")
        and observed_code
    ):
        # 填空/选择里的程序段有时被模型放进 code 字段，而抽取记录把它并入了
        # statement；比较时把两者拼回去，避免把“字段位置不同”误判成缺题。
        observed_statement = (observed_statement + "\n" + observed_code).strip()
    elif record.get("question_type") == "code" and observed_code and not _looks_like_code(observed_code):
        # 编程题的输出样例有时被模型归到 code 字段；它不是代码，拼回题面比较。
        observed_statement = (observed_statement + "\n" + observed_code).strip()
    confidence_rank = {"high": 3, "medium": 2, "low": 1}
    confidence = min(
        (item.get("confidence") or "low" for item in page_obs),
        key=lambda value: confidence_rank.get(str(value).lower(), 0),
        default="low",
    )
    uncertain = [value for item in page_obs for value in (item.get("uncertain") or [])]
    statement_check = (
        compare_text(record.get("statement") or "", observed_statement)
        if observed_statement
        else None
    )
    options_check = (
        compare_options(record.get("options") or [], observed_options)
        if record.get("options")
        else None
    )
    expected_code = record.get("code_lines") or embedded_code(record.get("statement") or "")
    observed_code_text = observed_code or embedded_code(observed_statement)
    code_source = "code_lines" if record.get("code_lines") else ("embedded" if expected_code else None)
    code_check = (
        compare_code(expected_code, observed_code_text)
        if expected_code and observed_code_text
        else None
    )
    blanks_check = (
        compare_blanks(record.get("blank_marks") or [], observed_statement)
        if record.get("blank_marks")
        else None
    )
    answer_check = None
    answer_obs = _answer_observations(jobs, record)
    if not missing:
        if record.get("question_type") == "choice":
            answer_check = compare_choice_answer(answer.get("text"), (answer_obs or {}).get("choice"))
        elif record.get("question_type") == "fill":
            expected_map = _fill_expected_map(record)
            checks = []
            for blank, expected in expected_map.items():
                checks.append(
                    compare_fill_answer(expected, (answer_obs or {}).get("fill", {}).get(str(blank)))
                )
            answer_check = {
                "ok": bool(checks) and all(item["ok"] for item in checks),
                "format_only": bool(checks) and all(item["ok"] for item in checks) and any(
                    item.get("format_only") for item in checks
                ),
                "blanks": checks,
            }
        elif record.get("question_type") == "code":
            expected_code = _reference_code(record)
            best = None
            if answer_obs and expected_code:
                for item in answer_obs.get("code") or []:
                    compared = compare_code(expected_code, str(item.get("text") or ""))
                    score = (
                        int(compared.get("tokens_equal") or False) * 2
                        + int(compared.get("indent_equal") or False)
                        + int(compared.get("line_count_equal") or False)
                    )
                    if best is None or score > best[0]:
                        best = (score, compared)
            answer_check = best[1] if best is not None else None
    semantic_check = (
        len_literal_check(
            record.get("statement") or "",
            _fill_expected_map(record).values(),
        )
        if record.get("question_type") == "fill" and not missing
        else None
    )
    if blanks_check and not blanks_check.get("ok") and answer_obs:
        observed_blanks = {
            str(key) for key in (answer_obs.get("fill") or {}) if str(key).isdigit()
        }
        expected_blanks = {str(value) for value in record.get("blank_marks") or []}
        if expected_blanks and expected_blanks.issubset(observed_blanks):
            blanks_check = {
                **blanks_check,
                "ok": True,
                "source": "answer_page",
                "note": "题面页面没有保留空位标记，但答案页按印刷空号读取完整",
            }
    model_ok = bool(page_obs) and (missing or answer_obs is not None)
    next_item = _next_record(record, ordered)
    contamination = next_question_contamination(
        observed_statement or record.get("statement") or "",
        _norm_number(next_item.get("number_label")) if next_item else None,
        next_item.get("statement") if next_item else None,
    )
    conclusion, issues = classify(
        isolated=isolated,
        missing_answer=missing,
        model_ok=model_ok,
        confidence=str(confidence),
        uncertain=uncertain,
        statement=statement_check,
        options=options_check,
        code=code_check,
        blanks=blanks_check,
        answer=answer_check,
        contamination=contamination,
    )
    if semantic_check is not None and not semantic_check.get("ok"):
        conclusion = "needs_review"
        issues.append(
            "题干 len 字面量计算值为 "
            f"{semantic_check['actual']}，原答案记录为 {semantic_check['expected']}；"
            "需回原卷裁定字符串空格/转义"
        )
    if conclusion in ("pass", "format_only"):
        suggested = "approve"
    elif conclusion in ("needs_review", "model_unreadable", "missing_answer"):
        suggested = "review"
    else:
        suggested = "reject"
    call_ids = [value for item in page_obs for value in item.get("call_ids") or []]
    if answer_obs:
        call_ids.extend(answer_obs.get("call_ids") or [])
    model_name = next(
        (
            job.get("model")
            for page in record.get("pages_statement") or []
            for job in jobs.get((record["paper_id"], int(page), "statement"), [])
            if job.get("model")
        ),
        None,
    )
    return {
        "question_id": record["question_id"],
        "paper_id": record["paper_id"],
        "paper_title": record.get("paper_title"),
        "question_type": record["question_type"],
        "number_label": record["number_label"],
        "answer_state": answer.get("state"),
        "answer_available": answer.get("state") not in (None, "", "missing"),
        "protected": False,
        "isolated": bool(isolated),
        "missing_answer": bool(missing),
        "before_content_sha256": record["content_sha256"],
        "after_content_sha256": record["content_sha256"],
        "proposed_after_sha256": None,
        "candidate_build_id": None,
        "vision": {
            "model": model_name,
            "pages": [int(page) for page in record.get("pages_statement") or []]
            + [int(page) for page in record.get("pages_answer") or []],
            "call_ids": call_ids,
            "confidence": confidence,
            "uncertain": uncertain,
            "image_sha256": [
                value
                for item in page_obs
                for value in (item.get("image_sha256") or [])
            ],
        },
        "program_checks": {
            "statement": statement_check,
            "options": options_check,
            "code": code_check,
            "code_source": code_source,
            "blanks": blanks_check,
            "answer": answer_check,
            "semantic": semantic_check,
        },
        "observed": {
            "statement": observed_statement,
            "options": observed_options,
            "code": observed_code,
            "answer": answer_obs,
        },
        "result": conclusion,
        "issues": issues,
        "suggested_decision": suggested,
        "reviewer_type": "vision_model+program",
        "mentor_review_required": True,
        "notes": "模型+程序建议，不是导师目视结论；正式审核需导师确认。",
    }


def assemble(inventory: dict[str, Any], out: Path, *, bank_root: Path | None = None) -> dict[str, Any]:
    jobs = _load_jobs(out / "jobs")
    records = inventory["records"]
    remaining = inventory["remaining"]
    ordered_by_paper: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for question_id in remaining:
        record = records[question_id]
        ordered_by_paper[record["paper_id"]].append(record)
    for items in ordered_by_paper.values():
        items.sort(key=lambda item: (item["question_type"], int(_norm_number(item["number_label"])) if _norm_number(item["number_label"]).isdigit() else 10**6, item["question_id"]))
    results = []
    for question_id in remaining:
        record = records[question_id]
        results.append(
            _result_for(
                record,
                jobs=jobs,
                ordered=ordered_by_paper[record["paper_id"]],
                isolated=question_id in inventory["isolated"],
            )
        )
    questions_dir = out / "questions"
    for result in results:
        paper_dir = questions_dir / result["paper_id"]
        paper_dir.mkdir(parents=True, exist_ok=True)
        (paper_dir / f"{result['question_id']}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    with (out / "results.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
    corrections = suggest_corrections(inventory, out, results)
    summary = _summarize(results, inventory, out)
    summary["corrections_suggested"] = len(corrections)
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _write_review_queue(results, inventory, out)
    return summary


def _trailing_math_fragment(statement: str) -> str | None:
    lines = [line.strip() for line in str(statement or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    first, trailing = lines[0], lines[1:]
    if not re.search(r"[\u4e00-\u9fff]", first):
        return None
    if any(re.search(r"[\u4e00-\u9fff]", line) for line in trailing):
        return None
    if not any(re.search(r"[√∑∫±×÷−𝒃𝟐𝟒]", line) for line in trailing):
        return None
    if any(re.search(r"(^|\s)(print|for|while|if|def|import|return)\b|=", line) for line in trailing):
        return None
    return first


def suggest_corrections(
    inventory: dict[str, Any],
    out: Path,
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """生成可复现校订建议；只处理有明确页级证据的残留公式碎片。

    真正的写入由隔离候选 build 的 curation 校验完成。这里不覆盖原答案，
    不把模型猜测写进题库。
    """
    items = []
    for result in results:
        if result["result"] not in ("needs_review", "model_unreadable"):
            continue
        record = inventory["records"].get(result["question_id"])
        if record is None or result.get("isolated") or result.get("missing_answer"):
            continue
        cleaned = _trailing_math_fragment(record.get("statement") or "")
        if not cleaned:
            continue
        observed = (result.get("observed") or {}).get("statement") or ""
        def skeleton(text: str) -> str:
            return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", str(text))

        if skeleton(cleaned) not in skeleton(observed):
            continue
        pages = [int(page) for page in record.get("pages_statement") or []]
        if not pages:
            continue
        paper = inventory["papers"][record["paper_id"]]
        patch = {"statement": cleaned}
        item = {
            "question_id": result["question_id"],
            "before_content_sha256": record["content_sha256"],
            "pdf_sha256": paper.get("pdf_sha256"),
            "evidence_pages": pages,
            "patch": patch,
            "patch_sha256": _sha_json(patch),
            "reason": "题面末尾残留下一题的公式碎片；视觉转写与首行题面一致，删去无中文的尾行。",
            "reviewed_by": "vision_model+program",
            "mentor_review_required": True,
            "applied": False,
            "model": (result.get("vision") or {}).get("model"),
            "model_confidence": (result.get("vision") or {}).get("confidence"),
            "evidence": {
                "visual_result": result.get("result"),
                "issues": result.get("issues"),
                "observed_statement": observed,
            },
        }
        items.append(item)
    payload = {
        "schema_version": 1,
        "generated_at": _now(),
        "build_id": inventory["build_id"],
        "note": "视觉模型+程序建议，不是导师校订；只在隔离候选 build 中应用，需导师复核。",
        "corrections": items,
    }
    (out / "corrections.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return items


def _summarize(results: list[dict[str, Any]], inventory: dict[str, Any], out: Path) -> dict[str, Any]:
    by_result = Counter(item["result"] for item in results)
    by_type = Counter(item["question_type"] for item in results)
    by_paper = Counter(item["paper_id"] for item in results)
    by_decision = Counter(item["suggested_decision"] for item in results)
    unresolved = [item for item in results if item["result"] not in ("pass", "format_only", "corrected", "isolated")]
    summary = {
        "generated_at": _now(),
        "build_id": inventory["build_id"],
        "source_version": inventory["source_version"],
        "counts": inventory["counts"],
        "processed": len(results),
        "by_result": dict(sorted(by_result.items())),
        "by_type": dict(sorted(by_type.items())),
        "by_paper": dict(sorted(by_paper.items())),
        "by_suggested_decision": dict(sorted(by_decision.items())),
        "clear": sum(by_result.get(key, 0) for key in ("pass", "format_only", "corrected")),
        "needs_review": sum(
            by_result.get(key, 0)
            for key in ("needs_review", "model_unreadable", "missing_answer")
        ),
        "isolated": by_result.get("isolated", 0),
        "missing_answer": sum(1 for item in results if item["missing_answer"]),
        "unresolved_ids": [item["question_id"] for item in unresolved],
    }
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def _write_review_queue(results: list[dict[str, Any]], inventory: dict[str, Any], out: Path) -> None:
    def row(item: dict[str, Any]) -> str:
        pages = "/".join(map(str, item["vision"]["pages"]))
        issue = "；".join(item["issues"][:4]) or "无"
        link = f"questions/{item['paper_id']}/{item['question_id']}.json"
        return (
            f"| `{item['question_id']}` | {item['question_type']} 第 {item['number_label']} 题 | {pages} | "
            f"{item['result']} | {item['suggested_decision']} | {issue} | [{item['question_id']}]({link}) |"
        )

    clear = [item for item in results if item["result"] in ("pass", "format_only", "corrected")]
    difficult = [item for item in results if item["result"] in ("needs_review", "model_unreadable")]
    defects = [item for item in results if item["missing_answer"]]
    isolated = [item for item in results if item["result"] == "isolated"]
    with (out / "review-queue.md").open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("# F007 剩余题目视觉复核清单\n\n")
        handle.write(f"- 生成时间：{_now()}\n")
        handle.write(f"- build：`{inventory['build_id']}`\n")
        handle.write(f"- 处理：{len(results)} 题；清晰 {len(clear)}；待复核 {len(difficult)}；缺答案 {len(defects)}；隔离 {len(isolated)}\n")
        handle.write("- 结果来自视觉模型+程序比对，不是导师目视结论；正式发布须导师复审。\n\n")
        handle.write("## 一、清晰题（建议 approve）\n\n")
        handle.write("| 题号 | 题型/题号 | 页 | 结论 | 建议 | 疑点 | 证据 |\n|---|---|---|---|---|---|---|\n")
        for item in clear:
            handle.write(row(item) + "\n")
        handle.write("\n## 二、疑难题（需导师优先复核）\n\n")
        handle.write("| 题号 | 题型/题号 | 页 | 结论 | 建议 | 疑点 | 证据 |\n|---|---|---|---|---|---|---|\n")
        for item in difficult:
            handle.write(row(item) + "\n")
        handle.write("\n## 三、原卷缺陷与缺答案\n\n")
        handle.write("| 题号 | 题型/题号 | 页 | 结论 | 建议 | 疑点 | 证据 |\n|---|---|---|---|---|---|---|\n")
        for item in defects:
            handle.write(row(item) + "\n")
        handle.write("\n## 四、隔离题（继续不发布）\n\n")
        handle.write("| 题号 | 题型/题号 | 页 | 结论 | 建议 | 疑点 | 证据 |\n|---|---|---|---|---|---|---|\n")
        for item in isolated:
            handle.write(row(item) + "\n")


def pilot(bank_root: Path, out: Path, *, workers: int = 3) -> dict[str, Any]:
    inventory = _load_inventory(bank_root)
    missing = [question_id for question_id in PILOT_IDS if question_id not in inventory["records"]]
    if missing:
        raise SystemExit("试点题目不存在：" + "、".join(missing))
    pilot_out = out / "pilot"
    pilot_out.mkdir(parents=True, exist_ok=True)
    pilot_inventory = dict(inventory)
    pilot_inventory["remaining"] = list(PILOT_IDS)
    pilot_inventory["records"] = {question_id: inventory["records"][question_id] for question_id in PILOT_IDS}
    (pilot_out / "inventory.json").write_text(
        json.dumps(pilot_inventory, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    jobs = build_jobs(pilot_inventory, ids=PILOT_IDS, single=True)
    client = VisionClient()
    stats = run_jobs(
        jobs,
        pages_dir=out / "pages",
        jobs_dir=pilot_out / "jobs",
        calls_dir=pilot_out / "calls",
        client=client,
        workers=workers,
    )
    summary = assemble(pilot_inventory, pilot_out, bank_root=bank_root)
    summary["pilot_stats"] = stats
    summary["pilot_ids"] = PILOT_IDS
    (pilot_out / "pilot-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def annotate(bank_root: Path, out: Path) -> dict[str, Any]:
    inventory = json.loads((out / "inventory.json").read_text(encoding="utf-8"))
    paths = BankPaths(bank_root)
    current = read_current(paths)
    if current is None:
        raise SystemExit("候选题目库尚未建立")
    old_results = [
        json.loads(line)
        for line in (out / "results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    repository = QuestionRepository(paths.build_dir(current["build_id"]))
    previous_id = current.get("previous_build_id")
    previous_repository = (
        QuestionRepository(paths.build_dir(previous_id))
        if previous_id and (paths.builds_dir / previous_id).is_dir()
        else None
    )
    real_paths = BankPaths(PROJECT_ROOT / "data" / "local" / "question-bank")
    real_current = read_current(real_paths)
    real_repository = (
        QuestionRepository(real_paths.build_dir(real_current["build_id"]))
        if real_current
        else None
    )
    try:
        records: dict[str, dict[str, Any]] = {}
        for old in old_results:
            record = repository.fetch_detail(old["question_id"])
            if record is None:
                raise SystemExit(f"候选 build 中找不到该题：{old['question_id']}")
            records[old["question_id"]] = record
    finally:
        repository.close()
    jobs = _load_jobs(out / "jobs")
    ordered_by_paper: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for question_id in inventory["remaining"]:
        record = records.get(question_id)
        if record is not None:
            ordered_by_paper[record["paper_id"]].append(record)
    for items in ordered_by_paper.values():
        items.sort(
            key=lambda item: (
                item["question_type"],
                int(_norm_number(item["number_label"]))
                if _norm_number(item["number_label"]).isdigit()
                else 10**6,
                item["question_id"],
            )
        )
    updated: list[dict[str, Any]] = []
    for old in old_results:
        record = records[old["question_id"]]
        fresh = _result_for(
            record,
            jobs=jobs,
            ordered=ordered_by_paper[record["paper_id"]],
            isolated=bool(old.get("isolated")),
        )
        fresh["before_content_sha256"] = old["before_content_sha256"]
        fresh["after_content_sha256"] = record["content_sha256"]
        fresh["proposed_after_sha256"] = old.get("proposed_after_sha256")
        fresh["candidate_build_id"] = current["build_id"]
        fresh["protected"] = bool(old.get("protected"))
        if record["content_sha256"] != old["before_content_sha256"]:
            previous_record = (
                previous_repository.fetch_detail(record["question_id"])
                if previous_repository is not None
                else None
            )
            real_record = (
                real_repository.fetch_detail(record["question_id"])
                if real_repository is not None
                else None
            )
            solution_only = bool(
                previous_record
                and real_record
                and _payload_without_solutions(previous_record)
                == _payload_without_solutions(real_record)
                and _payload_without_solutions(record)
                == _payload_without_solutions(previous_record)
            )
            if solution_only:
                # 独立解答不属于题干/选项/原卷答案，不能把它误记成文本校订。
                fresh["solution_only_change"] = True
                fresh["suggested_decision"] = "review"
            else:
                fresh["proposed_after_sha256"] = record["content_sha256"]
                fresh["correction"] = {
                    "applied": True,
                    "before_content_sha256": old["before_content_sha256"],
                    "after_content_sha256": record["content_sha256"],
                    "rechecked": True,
                    "recheck_result": fresh["result"],
                    "recheck_issues": list(fresh["issues"]),
                }
                if fresh["result"] in ("pass", "format_only"):
                    fresh["result"] = "needs_review"
                    fresh["suggested_decision"] = "review"
                    fresh["issues"].append(
                        "候选校订后文本复检通过，但校订尚未经导师裁定，不进入建议批准"
                    )
                else:
                    fresh["suggested_decision"] = "review"
                    fresh["issues"].append("候选校订后复检仍有疑点，未标记为已修正")
        updated.append(fresh)
    inventory["build_id"] = current["build_id"]
    inventory["source_version"] = current["source_version"]
    inventory["records"].update(records)
    (out / "inventory.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if previous_repository is not None:
        previous_repository.close()
    if real_repository is not None:
        real_repository.close()
    with (out / "results.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for result in updated:
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
    for result in updated:
        path = out / "questions" / result["paper_id"] / f"{result['question_id']}.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = _summarize(updated, inventory, out)
    candidate_payload = {
        "build_id": current["build_id"],
        "source_version": current["source_version"],
        "annotated_at": _now(),
    }
    (out / "candidate.json").write_text(
        json.dumps(candidate_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    correction_path = out / "corrections.json"
    if correction_path.is_file():
        payload = json.loads(correction_path.read_text(encoding="utf-8"))
        by_id = {item["question_id"]: item for item in updated}
        for item in payload.get("corrections") or []:
            result = by_id.get(item.get("question_id"))
            if result and result["after_content_sha256"] != result["before_content_sha256"]:
                item["applied"] = True
                item["candidate_build_id"] = current["build_id"]
                item["rechecked"] = True
                item["recheck_result"] = result["result"]
                item["recheck_issues"] = list(result["issues"])
        summary["corrections_suggested"] = len(payload.get("corrections") or [])
        (out / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (out / "applied-corrections.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    _write_review_queue(updated, inventory, out)
    return summary


def verify(bank_root: Path, out: Path) -> dict[str, Any]:
    inventory = json.loads((out / "inventory.json").read_text(encoding="utf-8"))
    results = [
        json.loads(line)
        for line in (out / "results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ids = [item["question_id"] for item in results]
    issues = []
    if len(ids) != len(set(ids)):
        issues.append("逐题结果存在重复 question_id")
    expected = set(inventory["remaining"])
    if set(ids) != expected:
        issues.append(f"逐题结果与剩余题不一致：缺 {len(expected - set(ids))}，多 {len(set(ids) - expected)}")
    if set(inventory["first83"]) & set(ids):
        issues.append("逐题结果包含首批 83 题")
    if len(results) != 1071:
        issues.append(f"结果数不是 1071：{len(results)}")
    for key in ("needs_review", "model_unreadable"):
        bad = [item["question_id"] for item in results if item["result"] == key and not item["issues"]]
        if bad:
            issues.append(f"{key} 有 {len(bad)} 题没有疑点说明")
    page_manifest = inventory.get("page_manifest") or {}
    missing_images = []
    for result in results:
        for page in result["vision"]["pages"]:
            key = f"{result['paper_id']}:p{page}"
            if key not in page_manifest:
                missing_images.append(key)
                break
    if missing_images:
        issues.append("缺少页面证据：" + "、".join(sorted(set(missing_images))[:5]))
    protected = {item["question_id"]: item["content_sha256"] for item in json.loads(
        (out / "protected-83.json").read_text(encoding="utf-8")
    )["questions"]}
    if len(protected) != 83:
        issues.append(f"保护清单不是 83 题：{len(protected)}")
    missing_models = [item["question_id"] for item in results if not (item.get("vision") or {}).get("model")]
    if missing_models:
        issues.append(f"{len(missing_models)} 题没有记录视觉模型名")
    if sum(1 for item in results if item.get("missing_answer")) != 54:
        issues.append("缺答案题数不是 54")
    if sum(1 for item in results if item.get("isolated")) != 2:
        issues.append("隔离题数不是 2")
    candidate = {}
    candidate_path = out / "candidate.json"
    if candidate_path.is_file():
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        paths = BankPaths(bank_root)
        current = read_current(paths)
        if current is None or current.get("build_id") != candidate.get("build_id"):
            issues.append("候选 build 指针与 candidate.json 不一致")
        else:
            repository = QuestionRepository(paths.build_dir(current["build_id"]))
            try:
                for item in results:
                    record = repository.fetch_detail(item["question_id"])
                    if record is None or record["content_sha256"] != item["after_content_sha256"]:
                        issues.append(f"{item['question_id']} 校订后哈希与候选 build 不一致")
                for question_id, digest in protected.items():
                    record = repository.fetch_detail(question_id)
                    if record is None or record["content_sha256"] != digest:
                        issues.append(f"首批题 {question_id} 在候选 build 中被改动")
            finally:
                repository.close()
    applied = []
    applied_path = out / "applied-corrections.json"
    if applied_path.is_file():
        applied = [
            item for item in json.loads(applied_path.read_text(encoding="utf-8")).get("corrections") or []
            if item.get("applied")
        ]
    payload = {
        "verified_at": _now(),
        "build_id": inventory["build_id"],
        "candidate_build": candidate.get("build_id"),
        "processed": len(results),
        "counts": inventory["counts"],
        "first83_protected": len(protected),
        "isolated": len(inventory["isolated"]),
        "missing_answer": len(inventory["missing"]),
        "corrections_applied": len(applied),
        "ok": not issues,
        "issues": issues,
    }
    (out / "verification.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="F007 剩余题目视觉复核")
    parser.add_argument("command", choices=("prepare", "pilot", "run", "assemble", "annotate", "verify"))
    parser.add_argument("--bank-root", type=Path, default=PROJECT_ROOT / "data" / "local" / "question-bank")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    out = args.out
    if args.command == "prepare":
        prepare(args.bank_root, out)
        return 0
    if args.command == "pilot":
        summary = pilot(args.bank_root, out, workers=args.workers)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    if args.command == "run":
        inventory = json.loads((out / "inventory.json").read_text(encoding="utf-8"))
        jobs = build_jobs(inventory)
        stats = run_jobs(
            jobs,
            pages_dir=out / "pages",
            jobs_dir=out / "jobs",
            calls_dir=out / "calls",
            client=VisionClient(),
            workers=args.workers,
            limit=args.limit,
        )
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0
    if args.command == "assemble":
        inventory = json.loads((out / "inventory.json").read_text(encoding="utf-8"))
        summary = assemble(inventory, out, bank_root=args.bank_root)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    if args.command == "annotate":
        summary = annotate(args.bank_root, out)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    payload = verify(args.bank_root, out)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())