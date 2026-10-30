"""Mentor's reproducible F007 finishing packet. Original source/PDFs stay intact.

`audit` reads PDF text and glyph positions, and writes evidence in the authorized
review directory. It never treats a model transcription as a correction.
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import re
import sqlite3
import shutil
import unicodedata
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pymupdf

from tools.question_bank.db import content_sha256
from tools.question_bank.curation import PATCH_FIELDS
from tools.question_bank.visual_compare import embedded_code

ROOT = Path(__file__).resolve().parents[2]
BANK = ROOT / "data/local/question-bank"
OUT = BANK / "review/mentor-final-20261029"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", "", text).translate(str.maketrans("‘’“”", "''\"\""))


def load_snapshot(bank: Path):
    current = read_json(bank / "db/current.json")
    path = bank / "db/builds" / current["build_id"] / "questions.sqlite3"
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        records = {q: json.loads(raw) for q, raw in conn.execute("SELECT question_id,raw_json FROM questions")}
    return current, records


class PdfEvidence:
    def __init__(self, path: Path):
        self.doc = pymupdf.open(path)
        self.pages = {}

    def lines(self, number: int):
        if number in self.pages:
            return self.pages[number]
        rows = []
        for block in self.doc[number - 1].get_text("rawdict")["blocks"]:
            for line in block.get("lines", []):
                chars = [c for span in line["spans"] for c in span.get("chars", [])]
                text = "".join(c["c"] for c in chars)
                nonblank = [c for c in chars if not c["c"].isspace()]
                if not nonblank:
                    continue
                rows.append({"text": text.rstrip(), "x": round(nonblank[0]["bbox"][0], 3),
                             "y": round(line["bbox"][1], 3), "page": number,
                             "font": line["spans"][0]["size"]})
        rows.sort(key=lambda r: (round(r["y"] / 3), r["x"]))
        self.pages[number] = rows
        return rows

    def source_rows(self, record):
        pages = record["pages"].get("statement_all") or record["pages"]["statement"]
        return [r for p in pages for r in self.lines(p)]


def align_code(code: str, rows: list[dict]):
    """Find one compact, ordered exact-content match; ambiguity is not repaired."""
    source = [line for line in code.splitlines() if line.strip()]
    if len(source) < 2:
        return None
    candidates = []
    for start, row in enumerate(rows):
        if norm(row["text"]) != norm(source[0]):
            continue
        positions = [start]
        for line in source[1:]:
            found = next((i for i in range(positions[-1] + 1, min(len(rows), positions[-1] + 18))
                          if norm(rows[i]["text"]) == norm(line)), None)
            if found is None:
                break
            positions.append(found)
        if len(positions) == len(source):
            candidates.append(positions)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p[-1] - p[0])
    if len(candidates) > 1 and candidates[0][-1] - candidates[0][0] == candidates[1][-1] - candidates[1][0]:
        return None
    selected = [rows[i] for i in candidates[0]]
    # A suite may continue across a page: never reset its nesting at the page edge.
    margin = min(r["x"] for r in selected)
    offsets = [r["x"] - margin for r in selected]
    positive = [d for d in offsets if d > 3]
    unit = min(positive) if positive else 20.0
    levels = []
    for row in selected:
        delta = row["x"] - margin
        level = round(delta / unit)
        if level > 8 or abs(delta - level * unit) > max(6.0, unit * .4):
            return None
        levels.append(level)
    restored = "\n".join(" " * (4 * level) + line.strip() for level, line in zip(levels, source))
    return restored, selected


def audit():
    current, records = load_snapshot(BANK)
    reviews = read_json(BANK / "db/reviews.json")["decisions"]
    protected = set(reviews)
    if len(protected) != 83:
        raise ValueError("本次审核已收尾或基线已变化；拒绝覆盖原审核证据")
    catalog = {p["id"]: p for p in read_json(ROOT / "data/papers/catalog.json")}
    vision = {r["question_id"]: r for r in map(json.loads, (BANK / "review/visual/results.jsonl").read_text(encoding="utf-8").splitlines())}
    corrections, evidence = [], []
    docs = {}
    for qid, original in sorted(records.items()):
        if qid in protected:
            continue
        pid = original["paper_id"]
        if pid not in docs:
            docs[pid] = PdfEvidence(ROOT / "data/papers" / catalog[pid]["file_path"])
        rows = docs[pid].source_rows(original)
        # Repeated physical-page headers/footers interrupt a continued question.
        body = [r for r in rows if not (
            re.search(r"第\s*\d+\s*页\s*共\s*\d+\s*页", r["text"])
            or (r["y"] < 72 and ("考试对象" in r["text"] or "学年" in r["text"]))
        )]
        stream = norm("".join(r["text"] for r in body))
        record = copy.deepcopy(original)
        statement_grounded = norm(record["statement"]) in stream
        options_grounded = all(norm(o["text"]) in stream for o in record.get("options", []))
        alignment = None
        code = embedded_code(record["statement"])
        if code and len(code.splitlines()) > 1:
            alignment = align_code(code, rows)
            if alignment:
                restored, matched = alignment
                # Match the code tail without discarding a narrative prefix.
                lines = record["statement"].splitlines()
                first = next((i for i, line in enumerate(lines) if norm(line) == norm(code.splitlines()[0])), None)
                if first is not None:
                    old_lines = [line for line in lines[first:] if line.strip()]
                    new_lines = restored.splitlines()
                    old_indent = [len(line) - len(line.lstrip()) for line in old_lines]
                    new_indent = [len(line) - len(line.lstrip()) for line in new_lines]
                    if old_indent != new_indent:
                        record["statement"] = "\n".join(lines[:first] + [restored])
        patch = {k: record[k] for k in ("statement",) if record[k] != original[k]}
        if patch:
            corrections.append({"question_id": qid, "before_content_sha256": content_sha256(original),
                                "patch": patch, "reason": "按原 PDF 字符坐标恢复程序缩进；未采用模型改写正文"})
        evidence.append({"question_id": qid, "paper_id": pid,
                         "pages": original["pages"], "statement_grounded": statement_grounded,
                         "options_grounded": options_grounded,
                         "code_aligned": bool(alignment),
                         "code_positions": alignment[1] if alignment else [],
                         "vision_result": vision[qid]["result"],
                         "vision_issues": vision[qid]["issues"],
                         "before_content_sha256": content_sha256(original)})
    for doc in docs.values():
        doc.doc.close()
    write_json(OUT / "layout-proposals.json", {"baseline": current, "corrections": corrections})
    write_json(OUT / "source-audit.json", {"baseline": current, "records": evidence})
    print(json.dumps({"remaining": len(evidence), "layout_proposals": len(corrections),
                      "statement_not_grounded": sum(not r["statement_grounded"] for r in evidence),
                      "options_not_grounded": sum(not r["options_grounded"] for r in evidence),
                      "code_aligned": sum(r["code_aligned"] for r in evidence)}, ensure_ascii=False))


def mentor_solution(text, note):
    return {"author": "导师（Codex）", "text": text, "note": note}


def manual_corrections(records):
    """Concrete source-page decisions, independent of cached model suggestions."""
    changes = {}

    def change(qid, **patch):
        changes.setdefault(qid, {}).update(patch)

    def explain(qid, text, note):
        prior = changes.get(qid, {}).get("independent_solutions", records[qid].get("independent_solutions", []))
        change(qid, independent_solutions=prior + [mentor_solution(text, note)])

    # Punctuation at the right side of a question was sorted into an option.
    prefix = "py-2021-2022-s2-final-choice-"
    for number in (2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 14, 16, 17, 18):
        qid = prefix + f"{number:02}"
        r = records[qid]
        options = copy.deepcopy(r["options"])
        tails = []
        for option in options:
            if "\n" in option["text"]:
                body, tail = option["text"].split("\n", 1)
                if tail.strip().startswith(")"):
                    option["text"] = body.rstrip()
                    tails.append(tail.strip())
        if tails:
            lines = r["statement"].splitlines()
            lines[0] += " " + tails[0]
            change(qid, statement="\n".join(lines), options=options)
    change(prefix + "03", statement="下列程序中，while 后应填入( )，输出为105。\ni=1\ns=1\nwhile___________:\n    s=s*i\n    i+=2\nprint(s)")
    change(prefix + "16", statement="下列代码执行完毕后，输出结果是什么( )。\nnumber=10\ndef make():\n    print(number+1,end=',')\nmake()\nprint(number)")
    change(prefix + "19", statement='键盘输入数字7，以下代码的输出结果是( )。\nn=eval(input("请输入一个整数:"))\ns=0\nif n>=7:\n    n-=1\ns=4\nif n<7:\n    n-=1\ns=3\nprint(s)')
    r = records[prefix + "38"]
    change(prefix + "38", statement=r["statement"].replace("\nreturn x**2", "\n    return x**2"))

    p = "py-2021-2022-s2-midterm-choice-"
    options = copy.deepcopy(records[p + "23"]["options"])
    options[1]["text"] = "'leH'"
    change(p + "23", options=options)
    change(p + "31", options=[{"label": label, "text": text} for label, text in zip("ABCD", ("字符串", "range 函数", "列表", "randrange 函数"))])
    option_programs = {
        "29": ["if i<0:\n    x=-1\nelif i==0:\n    x=0\nelse i>0:\n    x=5", "if i<0:\n    x=-1\nif i==0:\n    x=0\nif i>0:\n    x=5", "x=0\nif i<0:\n    x=-1\nelif i>0:\n    x=5", "x=5\nif i<=0:\n    x=-1 if i<0 else 0"],
        "36": ["i=0\nwhile i<len(ls):\n    print(ls[i])\n    i=i+1", "while ls:\n    print(ls.pop(0))", "for i in range(0:len(ls)):\n    print(ls[i])", "for i in ls:\n    print(i)"],
    }
    for number, programs in option_programs.items():
        change(p + number, options=[{"label": label, "text": text} for label, text in zip("ABCD", programs)])
    qid = "py-2024-2025-s2-final-choice-12"
    opts = copy.deepcopy(records[qid]["options"])
    opts[3]["text"] = "a.sort(key=lambda x:(len(x),x))"
    change(qid, options=opts)

    # The following three tails belong to the mathematical formula in the NEXT question.
    for qid, text in {
        "py-2021-2022-s1-midterm-fill-02": 'len("python\\t 编程")的值为 (2)。',
        "py-2021-2022-s2-midterm-fill-03": 'len("")的值为 (4)。',
        "py-2023-2024-s1-midterm-fill-03": 'print("O\\113!")的结果是 (3)。',
    }.items():
        change(qid, statement=text)
    explain("py-2021-2022-s1-midterm-fill-02", "含普通空格时为10；不含该空格时为9。", "原卷给9。文本层在\\t和编程之间保留一个普通空格；按该字面量计算为10，按无额外空格的命题意图为9。原卷答案不覆盖。")

    # Remove registration fields from the end of programming tasks.
    for qid, r in records.items():
        if r["question_type"] == "code" and re.search(r"\n学院", r["statement"]):
            change(qid, statement=re.split(r"\n学院", r["statement"])[0].rstrip())
    change("py-2022-2023-s1-final-code-02", statement="使用jieba库对D盘根目录下的文件“狐狸和乌鸦.txt”中的文本进行分词（文件无编码错误），并对每个词出现的次数进行统计，将词频最高的前5个词语输出。（10分）\n要求：①定义函数f(ls,n)，统计ls中词语出现的频率并降序排序后返回前n项，统计过程中去除长度为1的词语；\n②编写主程序打开文件并读取文件内容，用jieba库精确分词，然后调用f函数统计词频后打印输出词频最高的前5个词语。\n打印格式参考：\n乌鸦:14\n狐狸:10\n没有:4\n大哥:4\n猪肉:4")
    explain("py-2022-2023-s1-final-code-02", "主程序打开文件时应使用D盘根目录的完整路径，例如 r'D:\\狐狸和乌鸦.txt'。", "原卷参考代码仅写相对路径，运行时须将文件放在工作目录，或按题目要求改为D盘绝对路径。")
    explain("py-2022-2023-s2-final-code-02", "按题目要求写当前文件夹：with open('data.txt', 'w') as f: ...", "原卷参考代码使用d:/data.txt，与题干的当前文件夹要求不同；保留原卷并提示调整路径。")

    qid = "py-2020-2021-s2-midterm-fill-15"
    introduction = "有以下两段程序。上述两段程序代码很相似，但功能却大不一样，左边代码中的else子句是与程序中的(18)组合的，右边代码中的else子句是与程序中的(19)组合的。"
    change(qid, statement=introduction + '\n左侧程序：\nn=int(input("输入一个正整数n(n>=2)："))\nfor i in range(2,n):\n    if n%i==0:\n        print(n,"不是素数")\n        break\nelse:\n    print(n,"是素数")\n右侧程序：\nn=int(input("输入一个正整数n(n>=2)："))\nfor i in range(2,n):\n    if n%i==0:\n        print(n,"不是素数")\n        break\n    else:\n        print(n,"是素数")')
    qid = "py-2021-2022-s2-final-fill-16"
    change(qid, statement='下面程序实现将列表ls中的素数去除，并输出去除素数后的列表及其长度。请在画线处添加适当的代码，将程序补充完整。\ndef prime(n):\n    if n<2:return False\n    for i in range(2,n):\n        if (16):\n            return False\n    return True\nls=[51,33,54,56,7,12,13,69,17,78]\nres=[]\nfor i in ls:\n    if prime(i)==False:\n        (17)\nprint("去除素数后的列表为：{}，列表长度为{}". (18))')
    qid = "py-2023-2024-s1-midterm-fill-13"
    change(qid, statement='下面程序输出的结果是 (14)。\nfor i in "happy":\n    print(i,end="")\n    if i=="p":\n        break')
    # Do not silently "fix" the original paper's erroneous example code.
    explain("py-2020-2021-s2-final-choice-39", "原卷程序的print(item)与for同行级，缺少循环体缩进；意图应为再缩进一级。按with上下文管理语义，文件会自动关闭。", "原卷排版/缩进有缺陷，保留原卷答案供对照，不将其宣称为可直接运行的正确代码。")
    explain("py-2020-2021-s1-midterm-code-02", 'for n in range(100,201):\n    for i in range(2,n):\n        if n%i==0:\n            break\n    else:\n        print(n,"是素数")', "独立整理的可执行版本；原卷红字代码缩进不一致，原文保留。")
    explain("py-2023-2024-s1-midterm-fill-16", "题干中最低分应使用min(grades)，最高分应使用max(grades)。", "原卷程序的最低分/最高分标签写反；本题所问空位仍按原空号填写。")
    # The no-answer paper: 39 choice + 14 fill + 1 code, already independently checked.
    _, candidate = load_snapshot(ROOT / "data/local/question-bank-remaining")
    missing_prefix = "py-2023-2024-s2-midterm-"
    for qid, r in records.items():
        if qid.startswith(missing_prefix) and not r.get("independent_solutions"):
            solutions = copy.deepcopy(candidate[qid].get("independent_solutions", []))
            if not solutions:
                raise ValueError(f"Missing independent solution: {qid}")
            for solution in solutions:
                solution["author"] = "导师（Codex）"
                solution["note"] = "独立解答，经导师复核；原卷未提供答案。" + solution.get("note", "")
            change(qid, independent_solutions=solutions)
    qid = missing_prefix + "choice-03"
    change(qid, independent_solutions=[mentor_solution("没有准确选项：运行时抛出TypeError。", "A写的是语法错，但代码可以通过语法解析；str与int相加时才抛出类型异常。A可能是命题意图，不能视为准确表述。")])
    qid = missing_prefix + "choice-09"
    change(qid, independent_solutions=[mentor_solution("A、D均能得到Hello，单选存在歧义。", "对给定只含首尾空格的字符串，strip()返回去空格结果，replace(' ','')也可去除这些空格并重新赋值。若还含中间空格，replace会一并删除。")])
    qid = missing_prefix + "choice-23"
    programs = ["i=5\nwhile i>0:\n    i+=1", "i=-5\nwhile i<0:\n    i-=1", "i=5\nwhile i<=5:\n    i-=1", "i=5\nwhile i>0:\n    i-=1"]
    change(qid, options=[{"label": k, "text": v} for k, v in zip("ABCD", programs)], independent_solutions=[mentor_solution("D", "D递减到0后退出；C不断减小，始终满足i<=5，不会正常结束。A、B同样不会正常结束。")])
    qid = missing_prefix + "fill-15"
    change(qid, statement="斐波那契数列从第三项开始，每项是前两项之和。请输出前12项，每行输出4个数字。\na,b=(15)\nn=0\nwhile n<12:\n    print(a,end=' ')\n    a,b=(16)\n    n=n+1\n    if (17):\n        print()")
    qid = missing_prefix + "fill-16"
    change(qid, statement='编写程序，提取输入字符串中的英文字母，不区分大小写，重复字母只计一次，按字母降序输出。如输入"I have 12 books"，输出[\'v\',\'s\',\'o\',\'k\',\'i\',\'h\',\'e\',\'b\',\'a\']，也允许统一为大写。\ns=input("请输入一段英文：")\ns=(18)  #统一大小写\nlst=[]\nfor c in s:\n    if c.isalpha() and (19):\n        lst.append(c)\n(20)\nprint(lst)')
    qid = missing_prefix + "code-02"
    change(qid, independent_solutions=[mentor_solution('import re\n\ns=input("请输入一句英文：")\ns=re.sub(r"\\d", "", s)\nwords=re.findall(r"[A-Za-z]+", s)\nif words:\n    longest=max(words,key=len)\n    print(f"最长单词是:{longest},它有 {len(longest)} 个字母")\nelse:\n    print("没有英文单词")', "先删除数字，再提取单词；空输入或全数字时有明确结果。并列最长取最先出现者。原卷无答案。")])
    return changes


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def validate_release(original, final, protected):
    if set(original) != set(final):
        raise ValueError("题号集合发生变化")
    for qid, before in original.items():
        after = final[qid]
        if qid in protected and content_sha256(before) != content_sha256(after):
            raise ValueError(f"保护题发生变化：{qid}")
        # Only notes can change inside original paper answers.
        a = {k: v for k, v in before.get("answer", {}).items() if k != "note"}
        b = {k: v for k, v in after.get("answer", {}).items() if k != "note"}
        if a != b:
            raise ValueError(f"原卷答案发生变化：{qid}")
        if after["answer"]["state"] == "missing" and not after.get("independent_solutions"):
            raise ValueError(f"缺少独立解答：{qid}")
        if after["question_type"] == "choice" and len(after.get("options", [])) != 4:
            raise ValueError(f"选择题选项数量错误：{qid}")


def prepare():
    from tools.question_bank.db import content_issue
    from tools.question_bank.build import load_bank_inputs
    from app.question_bank.paths import BankPaths
    current, records = load_snapshot(BANK)
    prior = read_json(BANK / "db/reviews.json")
    protected = set(prior["decisions"])
    if len(protected) != 83:
        raise ValueError("准备阶段预期首批83题；不可覆盖已执行的发布")
    layout = read_json(OUT / "layout-proposals.json")
    if layout["baseline"]["build_id"] != current["build_id"]:
        raise ValueError("布局证据与当前版本不一致")
    final = copy.deepcopy(records)
    reasons = {}
    for item in layout["corrections"]:
        qid = item["question_id"]
        if content_sha256(records[qid]) != item["before_content_sha256"]:
            raise ValueError(f"布局建议过期：{qid}")
        final[qid].update(item["patch"])
        reasons[qid] = [item["reason"]]
    manual = manual_corrections(records)
    for qid, patch in manual.items():
        if qid in protected:
            continue
        final[qid].update(patch)
        reasons.setdefault(qid, []).append("导师对照原页/独立求解裁定，见收尾报告与原物理页")
    validate_release(records, final, protected)
    _, _, papers = load_bank_inputs(BankPaths(BANK))
    by_paper = {p["paper_id"]: p for p in papers}
    changes = []
    for qid, after in sorted(final.items()):
        before = records[qid]
        patch = {field: after.get(field) for field in PATCH_FIELDS
                 if before.get(field) != after.get(field)}
        if not patch:
            continue
        changes.append({"question_id": qid, "before_content_sha256": content_sha256(before),
                        "pdf_sha256": by_paper[before["paper_id"]]["pdf_sha256"],
                        "evidence_pages": sorted(set(before["pages"].get("statement_all", []) + before["pages"].get("answer", []))),
                        "reason": "；".join(reasons[qid]), "reviewed_by": "导师（Codex）",
                        "patch": patch})
    write_json(OUT / "mentor-corrections.json", {"schema_version": 1, "reviewed_at": now(),
               "baseline_build_id": current["build_id"], "corrections": changes})
    # Save the exact baseline once; rebuilding is independent of the candidate directory.
    write_json(OUT / "current-before.json", current)
    write_json(OUT / "reviews-before.json", prior)
    all_reviews = copy.deepcopy(prior)
    audited = read_json(OUT / "source-audit.json")["records"]
    evidence = {r["question_id"]: r for r in audited}
    for qid, record in sorted(final.items()):
        if qid in protected:
            continue
        issue = content_issue(record)
        all_reviews["decisions"][qid] = {
            "action": "reject" if issue else "approve", "content_sha256": content_sha256(record),
            "visual": "source_checked", "labels_verified": [],
            "reviewed_by": "导师（Codex）", "reviewer_type": "mentor", "reviewed_at": now(),
            "note": ("原卷双B，保留异常并隔离。" if issue else
                     "原PDF文本/坐标、已有逐卷答案校录、缓存识图证据与导师疑点裁定综合审核；不是全题人工目视。")
                    + ("本题有校订或独立解答，详见mentor-corrections.json。" if qid in reasons else ""),
            "evidence": {"pages": evidence[qid]["pages"], "prior_visual_result": evidence[qid]["vision_result"],
                         "source_text_grounded": evidence[qid]["statement_grounded"],
                         "source_options_grounded": evidence[qid]["options_grounded"],
                         "source_code_aligned": evidence[qid]["code_aligned"],
                         "correction": qid in reasons},
        }
    all_reviews["updated_at"] = now()
    write_json(OUT / "staged-reviews.json", all_reviews)
    # Immutable preflight hashes also cover the real learning store and source PDFs.
    paths = [BANK / "source/full" / "manifest.json", *sorted((BANK / "source/full").glob("*.jsonl")), ROOT / "data/local/study.sqlite3"]
    paths += [ROOT / "data/papers" / p["pdf_rel_path"] for p in papers]
    write_json(OUT / "unchanged-files.json", {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()})
    print(json.dumps({"corrections": len(changes), "approved": sum(d["action"] == "approve" for d in all_reviews["decisions"].values()),
                      "rejected": sum(d["action"] == "reject" for d in all_reviews["decisions"].values())}))


def build_release():
    from app.question_bank.paths import BankPaths
    from tools.question_bank.build import build, build_lock
    class StagedPaths(BankPaths):
        @property
        def reviews_file(self):
            return OUT / "staged-reviews.json"
    paths = StagedPaths(BANK)
    with build_lock(paths):
        result = build(paths, corrections=[BANK / "review/p4/mentor-corrections.json", OUT / "mentor-corrections.json"])
    write_json(OUT / "build-result.json", result)
    print(json.dumps({"build_id": result["build_id"]}))


def record_provenance():
    """Record the replay chain next to the selected version, with file hashes."""
    from app.question_bank.paths import atomic_write_json
    current = read_json(BANK / "db/current.json")
    if current["build_id"] != read_json(OUT / "build-result.json")["build_id"]:
        raise ValueError("正式版本不是本发布构建，拒绝写重放清单")
    paths = [BANK / "review/p4/mentor-corrections.json", OUT / "mentor-corrections.json"]
    current["source_corrections"] = [p.relative_to(BANK).as_posix() for p in paths]
    current["source_correction_sha256"] = {p.relative_to(BANK).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    atomic_write_json(BANK / "db/current.json", current)


def publish_release():
    from app.question_bank.paths import BankPaths, atomic_write_json
    from tools.question_bank.build import activate, verify_build
    old = read_json(OUT / "current-before.json")
    current = read_json(BANK / "db/current.json")
    if current != old or read_json(BANK / "db/reviews.json") != read_json(OUT / "reviews-before.json"):
        raise ValueError("当前发布状态已变化，拒绝覆盖")
    build_id = read_json(OUT / "build-result.json")["build_id"]
    target = BANK / "db/builds" / build_id
    verification = verify_build(target)
    if not verification.get("ok"):
        raise ValueError("新版本完整性校验失败")
    with sqlite3.connect((target / "questions.sqlite3").resolve().as_uri() + "?mode=ro", uri=True) as db:
        hashes = dict(db.execute("SELECT question_id,content_sha256 FROM questions"))
    staged = read_json(OUT / "staged-reviews.json")
    if any(hashes.get(qid) != item["content_sha256"] for qid, item in staged["decisions"].items()):
        raise ValueError("审核内容哈希与新构建不同")
    # Backups are ready before either atomic pointer/review replacement.
    for name in ("current.json", "reviews.json"):
        shutil.copy2(BANK / "db" / name, OUT / (name + ".before-publish"))
    try:
        activate(BankPaths(BANK), build_id)
        atomic_write_json(BANK / "db/reviews.json", staged)
        record_provenance()
    except BaseException:
        atomic_write_json(BANK / "db/current.json", old)
        atomic_write_json(BANK / "db/reviews.json", read_json(OUT / "reviews-before.json"))
        raise
    write_json(OUT / "publish-result.json", {"at": now(), "build_id": build_id,
               "previous_build_id": old["build_id"], "approved": sum(d["action"] == "approve" for d in staged["decisions"].values()),
               "rejected": sum(d["action"] == "reject" for d in staged["decisions"].values()), "verification": verification})
    print("Published", build_id)


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("command", choices=["audit", "prepare", "build", "publish", "provenance"])
    args = parser.parse_args()
    if args.command == "audit":
        audit()
    elif args.command == "prepare":
        prepare()
    elif args.command == "build":
        build_release()
    elif args.command == "publish":
        publish_release()
    elif args.command == "provenance":
        record_provenance()


if __name__ == "__main__":
    main()
