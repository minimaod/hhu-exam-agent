"""视觉转写与题库记录的确定性比对。

这里只做可解释的文本/代码/选项/空号比较，不调用模型，也不改写记录。
比较阈值刻意保守：轻微换行、空格和全半角差异算 format_only；关键 token、
选项内容、代码缩进、空号或答案不一致一律进入 needs_review。
"""

from __future__ import annotations

import ast
import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from typing import Any, Iterable

CODE_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*$")
CRITICAL_RE = re.compile(
    r"[A-Za-z_]\w*"
    r"|\d+(?:\.\d+)?"
    r"|'(?:[^'\\]|\\.)*'"
    r"|\"(?:[^\"\\]|\\.)*\""
    r"|\*\*|//|==|!=|<=|>=|->|:="
    r"|[+\-*/%<>=^()\[\]{}]"
)
STRING_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", re.S)
NEGATION_RE = re.compile(r"不|无|非|未|没有|错误|非法|不能|不可|不得|无需|无须|除外")
CODE_START_RE = re.compile(
    r"^\s*(?:def|class|import|from|for|while|if|elif|else|try|except|with|print|return)\b"
    r"|^\s*[A-Za-z_]\w*\s*(?:=|\+=|-=|\*=|/=)"
    r"|^\s*[A-Za-z_]\w*\s*\("
)
LEN_RE = re.compile(r"len\(\s*(['\"])((?:\\.|(?!\1).)*)\1\s*\)", re.S)
OPERATOR_ORDER_RE = re.compile(r"\*\*|//|[+\-*/%()\[\]{}]")
CODE_TOKEN_RE = re.compile(
    r"[A-Za-z_]\w*"
    r"|\d+(?:\.\d+)?"
    r"|==|!=|<=|>=|\*\*|//|->|:="
    r"|'(?:[^'\\]|\\.)*'"
    r"|\"(?:[^\"\\]|\\.)*\""
    r"|[+\-*/%<>=()[\]{},.:]"
)
BLANK_RE = re.compile(r"[（(]\s*(\d{1,3})\s*[)）]")
SPACE_RE = re.compile(r"[\s\u00a0\u3000]+")
BLANK_UNDERSCORE_RE = re.compile(r"_{2,}")
# 只把独立出现的 `(9)`/`（9）` 当作填空空号；`f(2)`、`(2+3)` 里的数字括号
# 属于表达式，必须参与严格比较。



def nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def compact(text: str, *, normalize: bool = True) -> str:
    value = nfkc(text) if normalize else (text or "")
    value = BLANK_UNDERSCORE_RE.sub("", value)
    return SPACE_RE.sub("", value)


def clean_model_text(text: str) -> str:
    """去掉模型偶尔加的 markdown fence，保留代码缩进。"""

    value = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = value.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip("\n")


def critical_tokens(text: str) -> list[str]:
    # NFKC 统一全半角括号，括号也属于语义 token；缺括号不能软通过。
    tokens = CRITICAL_RE.findall(nfkc(text or ""))
    result: list[str] = []
    for token in tokens:
        # 连续下划线只是填空占位符，不是表达式/标识符内容。
        if not token.strip("_"):
            continue
        # 引号风格是格式；内容仍以 <str:...> 形式参与增删和顺序比较。
        if len(token) >= 2 and token[0] in ("'", '"') and token[-1] == token[0]:
            body = token[1:-1].replace("\\'", "'").replace('\\"', '"')
            result.append(f"<str:{body}>")
        else:
            result.append(token)
    return result


def normalized_full_text(text: str) -> str:
    """只做明确白名单规范化，保留全部正文、数字和标点。"""

    value = nfkc(text or "")
    value = BLANK_UNDERSCORE_RE.sub("", value)
    value = re.sub(r"\s+", "", value)
    # 引号风格是格式；字符串内容仍由 string_literals 单独严格比较。
    value = value.replace("'", '"')
    return value


def string_literals(text: str) -> list[str]:
    """提取字符串字面量的内容（不含引号），用于严格比较空格/转义内容。"""

    values: list[str] = []
    for raw in STRING_RE.findall(text or ""):
        body = raw[1:-1]
        body = body.replace("\\'", "'").replace('\\"', '"')
        values.append(body)
    return values


def negation_tokens(text: str) -> Counter[str]:
    return Counter(NEGATION_RE.findall(compact(text)))


def operator_order(text: str) -> list[str]:
    """按出现顺序比较运算符和括号；数量相同但位置不同也不能通过。"""

    return OPERATOR_ORDER_RE.findall(nfkc(text or ""))


def embedded_code(text: str) -> str:
    """从题面中截出从第一行代码开始的程序段，保留原始缩进。

    选择题/填空题常把程序直接嵌在 statement 中，code_lines 为空；不能只比较
    去空白后的整段文本，否则函数体缩进丢失会被误判为通过。
    """

    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        # 代码行后面可能跟中文说明（如 `len("x")的值为`）；函数调用/赋值
        # 直接认可。控制关键字若后面只有中文（如“for 语句”），不是代码。
        if re.match(r"^\s*[A-Za-z_]\w*\s*\(", line) or re.match(
            r"^\s*[A-Za-z_]\w*\s*(?:=|\+=|-=|\*=|/=)", line
        ):
            return "\n".join(lines[index:]).strip()
        if CODE_START_RE.search(line):
            if re.search(r"[\u4e00-\u9fff]", line) and not re.search(r"[():=]", line):
                continue
            return "\n".join(lines[index:]).strip()
    return ""


def len_literal_check(statement: str, expected_values: Iterable[str]) -> dict[str, Any] | None:
    """只对 `len("字面量")` 做窄检查，避免执行任意程序。"""

    matches = LEN_RE.findall(statement or "")
    if len(matches) != 1:
        return None
    expected: int | None = None
    for value in expected_values:
        numbers = re.findall(r"-?\d+", str(value or ""))
        if numbers:
            expected = int(numbers[-1])
            break
    if expected is None:
        return None
    quote, body = matches[0]
    try:
        value = ast.literal_eval(quote + body + quote)
    except (SyntaxError, ValueError):
        return None
    actual = len(value)
    return {
        "ok": actual == expected,
        "actual": actual,
        "expected": expected,
        "literal": body,
    }


def token_f1(expected: str, observed: str) -> float:
    left = Counter(critical_tokens(expected))
    right = Counter(critical_tokens(observed))
    if not left and not right:
        return 1.0
    overlap = sum((left & right).values())
    if not overlap:
        return 0.0
    precision = overlap / sum(right.values())
    recall = overlap / sum(left.values())
    return 2 * precision * recall / (precision + recall)


def token_diff(expected: str, observed: str, limit: int = 12) -> dict[str, list[str]]:
    left = Counter(critical_tokens(expected))
    right = Counter(critical_tokens(observed))
    missing = list((left - right).elements())[:limit]
    extra = list((right - left).elements())[:limit]
    return {"missing": missing, "extra": extra}


def compare_text(
    expected: str,
    observed: str,
    *,
    ok_ratio: float = 0.90,
    ok_f1: float = 0.95,
    format_ratio: float = 0.80,
) -> dict[str, Any]:
    expected_original = expected or ""
    observed_original = clean_model_text(observed)
    expected = expected_original
    observed = observed_original
    expected_compact = compact(expected)
    observed_compact = compact(observed)
    expected_full = normalized_full_text(expected)
    observed_full = normalized_full_text(observed)
    full_equal = expected_full == observed_full
    expected_tokens = critical_tokens(expected)
    observed_tokens = critical_tokens(observed)
    token_order_diff = expected_tokens != observed_tokens
    diff = token_diff(expected, observed)
    string_diff = Counter(string_literals(expected)) - Counter(string_literals(observed))
    string_extra = Counter(string_literals(observed)) - Counter(string_literals(expected))
    expected_negation = negation_tokens(expected)
    observed_negation = negation_tokens(observed)
    negation_missing = list((expected_negation - observed_negation).elements())
    negation_extra = list((observed_negation - expected_negation).elements())
    semantic_diff = bool(negation_missing or negation_extra)
    expected_operators = operator_order(expected)
    observed_operators = operator_order(observed)
    operator_diff = expected_operators != observed_operators
    hard_semantic = (
        not full_equal
        or semantic_diff
        or operator_diff
        or token_order_diff
        or bool(diff["missing"])
        or bool(diff["extra"])
        or bool(string_diff)
        or bool(string_extra)
    )
    ok = not hard_semantic
    format_only = ok and (
        expected_original != observed_original
        or compact(expected_original) != compact(observed_original)
    )
    ratio = SequenceMatcher(None, expected_compact, observed_compact).ratio()
    f1 = token_f1(expected, observed)
    return {
        "ok": bool(ok),
        "format_only": bool(format_only),
        "ratio": round(ratio, 4),
        "critical_f1": round(f1, 4),
        "full_text_equal": bool(full_equal),
        "missing_tokens": diff["missing"],
        "extra_tokens": diff["extra"],
        "semantic_diff": bool(semantic_diff),
        "operator_order_diff": bool(operator_diff),
        "token_order_diff": bool(token_order_diff),
        "negation_missing": negation_missing,
        "negation_extra": negation_extra,
        "string_missing": list(string_diff.elements()),
        "string_extra": list(string_extra.elements()),
        "expected_chars": len(expected_compact),
        "observed_chars": len(observed_compact),
    }


def normalize_code_lines(lines: Iterable[str]) -> list[str]:
    result: list[str] = []
    for raw in lines:
        line = (raw or "").replace("\t", "    ").rstrip()
        if line.strip() == "":
            continue
        result.append(line)
    return result


def code_lines_from_text(text: str) -> list[str]:
    value = clean_model_text(text)
    return normalize_code_lines(value.splitlines())


def _indent_profile(lines: list[str]) -> list[int]:
    return [len(line) - len(line.lstrip(" ")) for line in lines]


def _normalize_code_tokens(lines: list[str]) -> list[str]:
    text = "\n".join(lines)
    text = re.sub(r"__blank_(\d+)__", r"(\1)", text)
    # 单双引号不改变 Python 语义；视觉转写经常混用，比较时统一。
    text = text.replace('"', "'")
    return CODE_TOKEN_RE.findall(text)


def compare_code(expected: Iterable[str] | str, observed: str) -> dict[str, Any]:
    if isinstance(expected, str):
        expected_lines = code_lines_from_text(expected)
    else:
        expected_lines = normalize_code_lines(expected)
    observed_lines = code_lines_from_text(observed)
    expected_tokens = _normalize_code_tokens(expected_lines)
    observed_tokens = _normalize_code_tokens(observed_lines)
    tokens_equal = expected_tokens == observed_tokens
    expected_indent = _indent_profile(expected_lines)
    observed_indent = _indent_profile(observed_lines)
    indent_equal = expected_indent == observed_indent
    line_count_equal = len(expected_lines) == len(observed_lines)
    expected_strings = Counter(string_literals("\n".join(expected_lines)))
    observed_strings = Counter(string_literals("\n".join(observed_lines)))
    string_missing = list((expected_strings - observed_strings).elements())
    string_extra = list((observed_strings - expected_strings).elements())
    strings_equal = not string_missing and not string_extra
    if not expected_lines and not observed_lines:
        return {
            "ok": True,
            "format_only": False,
            "tokens_equal": True,
            "indent_equal": True,
            "line_count_equal": True,
            "strings_equal": True,
            "expected_lines": 0,
            "observed_lines": 0,
        }
    ok = bool(tokens_equal and indent_equal and line_count_equal and strings_equal)
    format_only = bool(tokens_equal and strings_equal and not (indent_equal and line_count_equal))
    return {
        "ok": ok,
        "format_only": format_only,
        "tokens_equal": bool(tokens_equal),
        "indent_equal": bool(indent_equal),
        "line_count_equal": bool(line_count_equal),
        "strings_equal": bool(strings_equal),
        "string_missing": string_missing,
        "string_extra": string_extra,
        "expected_lines": len(expected_lines),
        "observed_lines": len(observed_lines),
        "expected_indent": expected_indent[:40],
        "observed_indent": observed_indent[:40],
    }


def _label(value: Any) -> str:
    text = str(value or "").strip().upper()
    match = re.match(r"^([A-Z])", text)
    return match.group(1) if match else text


def compare_options(
    expected: list[dict[str, Any]],
    observed: list[dict[str, Any]],
) -> dict[str, Any]:
    expected_map = {_label(item.get("label")): str(item.get("text") or "") for item in expected}
    observed_map: dict[str, str] = {}
    for item in observed or []:
        if isinstance(item, dict):
            observed_map[_label(item.get("label"))] = str(item.get("text") or "")
    labels_ok = list(expected_map) == list(observed_map)
    checks: dict[str, Any] = {}
    texts_ok = True
    format_only = False
    for label, text in expected_map.items():
        if label not in observed_map:
            checks[label] = {"ok": False, "reason": "missing"}
            texts_ok = False
            continue
        compared = compare_text(text, observed_map[label], ok_ratio=0.86, ok_f1=0.92, format_ratio=0.75)
        checks[label] = compared
        if not compared["ok"]:
            texts_ok = False
        if compared.get("format_only"):
            format_only = True
    extras = [label for label in observed_map if label not in expected_map]
    ok = bool(labels_ok and texts_ok and not extras)
    return {
        "ok": ok,
        "format_only": bool(ok and format_only),
        "labels_ok": labels_ok,
        "extras": extras,
        "options": checks,
    }


def blank_numbers(text: str) -> list[int]:
    return [int(value) for value in BLANK_RE.findall(text or "")]


def compare_blanks(expected: Iterable[int], observed_text: str) -> dict[str, Any]:
    expected_set = {int(value) for value in expected}
    observed = blank_numbers(observed_text)
    observed_set = set(observed)
    if not expected_set:
        return {
            "ok": True,
            "expected": [],
            "observed": observed,
            "missing": [],
            "extra": [],
        }
    return {
        "ok": expected_set == observed_set,
        "expected": sorted(expected_set),
        "observed": observed,
        "missing": sorted(expected_set - observed_set),
        "extra": sorted(observed_set - expected_set),
    }


def compare_choice_answer(expected: str | None, observed: Any) -> dict[str, Any]:
    if expected is None:
        return {"ok": True, "expected": None, "observed": observed}
    left = _label(expected)
    right = _label(observed)
    return {"ok": left == right, "expected": left, "observed": right}


def compare_fill_answer(expected: str | None, observed: Any) -> dict[str, Any]:
    if expected is None:
        return {"ok": True, "expected": None, "observed": observed}
    observed_text = "" if observed is None else str(observed)
    compared = compare_text(str(expected), observed_text, ok_ratio=0.92, ok_f1=0.96, format_ratio=0.80)
    return {
        "ok": bool(compared["ok"]),
        "format_only": bool(compared.get("format_only")),
        "expected": str(expected),
        "observed": observed_text,
        "ratio": compared["ratio"],
        "critical_f1": compared["critical_f1"],
        "missing_tokens": compared["missing_tokens"],
        "extra_tokens": compared["extra_tokens"],
        "semantic_diff": compared.get("semantic_diff", False),
        "full_text_equal": compared.get("full_text_equal", False),
        "operator_order_diff": compared.get("operator_order_diff", False),
        "negation_missing": compared.get("negation_missing", []),
        "negation_extra": compared.get("negation_extra", []),
        "string_missing": compared.get("string_missing", []),
        "string_extra": compared.get("string_extra", []),
    }


def next_question_contamination(
    current_statement: str,
    next_number: str | None,
    next_statement: str | None,
) -> bool:
    """检测下一题编号和题面是否串进当前题。"""

    if not next_number or not next_statement:
        return False
    number = str(next_number).strip().rstrip(".、．")
    if not number:
        return False
    text = compact(next_statement)[:16]
    if len(text) < 6:
        return False
    for line in (current_statement or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not re.match(rf"^{re.escape(number)}(?:[.、．]|\s|$)", stripped):
            continue
        if text in compact(stripped):
            return True
    return False


def classify(
    *,
    isolated: bool,
    missing_answer: bool,
    model_ok: bool,
    confidence: str,
    uncertain: list[Any],
    statement: dict[str, Any] | None,
    options: dict[str, Any] | None,
    code: dict[str, Any] | None,
    blanks: dict[str, Any] | None,
    answer: dict[str, Any] | None,
    contamination: bool,
) -> tuple[str, list[str]]:
    """返回 (结论, 疑点列表)。"""

    if isolated:
        return "isolated", ["原卷双 B 异常，继续隔离，不进入发布候选"]
    if not model_ok:
        return "model_unreadable", ["没有可用的视觉转写证据"]
    issues: list[str] = []
    if contamination:
        issues.append("疑似下一题内容串入当前题")
    if str(confidence or "").lower() != "high":
        issues.append(f"模型置信度不是 high：{confidence or '未提供'}")
    if uncertain:
        issues.append("模型标记不确定：" + "；".join(str(item) for item in uncertain[:5]))
    for name, check in (
        ("题干", statement),
        ("选项", options),
        ("代码", code),
        ("空号", blanks),
        ("答案", answer),
    ):
        if check is None:
            continue
        if not check.get("ok"):
            issues.append(f"{name}不一致")
        elif check.get("format_only"):
            issues.append(f"{name}仅格式差异")
        if name == "题干" and check.get("missing_tokens"):
            issues.append("题干缺少关键 token：" + "、".join(map(str, check["missing_tokens"][:6])))
        if name == "题干" and check.get("extra_tokens"):
            issues.append("题干多出关键 token：" + "、".join(map(str, check["extra_tokens"][:6])))
        if name == "代码" and check.get("tokens_equal") is False:
            issues.append("代码 token 不一致")
        if name == "代码" and check.get("indent_equal") is False:
            issues.append("代码缩进/行数不一致")
        if name == "空号" and check.get("source") != "answer_page" and (check.get("missing") or check.get("extra")):
            issues.append(
                "空号不一致：缺 "
                + str(check.get("missing"))
                + "，多 "
                + str(check.get("extra"))
            )
        if name == "答案" and not check.get("ok"):
            issues.append("视觉答案与原记录不一致")
    hard_fail = any(
        check is not None
        and not (name == "空号" and check.get("source") == "answer_page")
        and (not check.get("ok") or check.get("tokens_equal") is False or check.get("indent_equal") is False)
        for name, check in (
            ("题干", statement),
            ("选项", options),
            ("代码", code),
            ("空号", blanks),
            ("答案", answer),
        )
    )
    if hard_fail or contamination or str(confidence or "").lower() != "high" or uncertain:
        return "needs_review", issues
    if missing_answer:
        return "missing_answer", issues
    format_only = any(
        check is not None and check.get("format_only")
        for check in (statement, options, code, blanks, answer)
    )
    if format_only:
        return "format_only", issues
    return "pass", issues