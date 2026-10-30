"""Finishing packet safeguards, without models or the user's live databases."""
from copy import deepcopy

import pytest

from tools.question_bank.finalize_remaining import align_code, validate_release


def question():
    return {"question_id": "q", "paper_id": "p", "course": "Python", "section": {},
            "number_label": "1", "question_type": "choice", "statement": "题干",
            "options": [{"label": k, "text": k} for k in "ABCD"],
            "answer": {"state": "original", "text": "B"}, "pages": {}}


def test_protected_content_cannot_change():
    r = question()
    changed = deepcopy(r)
    changed["statement"] = "改动"
    with pytest.raises(ValueError, match="保护题"):
        validate_release({"q": r}, {"q": changed}, {"q"})


def test_original_answer_cannot_be_replaced():
    r = question()
    changed = deepcopy(r)
    changed["answer"]["text"] = "D"
    with pytest.raises(ValueError, match="原卷答案"):
        validate_release({"q": r}, {"q": changed}, set())


def test_missing_answer_requires_separate_solution():
    r = question()
    r["answer"] = {"state": "missing", "text": None}
    with pytest.raises(ValueError, match="缺少独立解答"):
        validate_release({"q": r}, {"q": r}, set())
    changed = deepcopy(r)
    changed["independent_solutions"] = [{"text": "D"}]
    validate_release({"q": r}, {"q": changed}, set())


def row(text, x, page=1):
    return {"text": text, "x": x, "y": 100, "font": 10.5, "page": page}


def test_layout_uses_real_variable_indentation_step():
    result = align_code("def f():\nprint(1)", [row("def f():", 100), row("print(1)", 110.5)])
    assert result[0] == "def f():\n    print(1)"


def test_layout_never_adopts_changed_operators_from_model():
    assert align_code("x=1\nprint(x[::3])", [row("x=1", 100), row("print(x[::-3])", 100)]) is None


def test_layout_does_not_reset_nested_suite_on_new_page():
    result = align_code("if True:\nprint(1)\nprint(2)",
                        [row("if True:", 100), row("print(1)", 120), row("print(2)", 120, 2)])
    assert result[0] == "if True:\n    print(1)\n    print(2)"


def test_ambiguous_repeated_code_is_not_automatically_repaired():
    rows = [row("x=1", 100), row("print(x)", 100), row("x=1", 100), row("print(x)", 100)]
    assert align_code("x=1\nprint(x)", rows) is None


def test_corrected_while_answer_is_uniquely_d_with_step_bound():
    states = []
    for value, op, delta in [(5, ">", 1), (-5, "<", -1), (5, "<=", -1), (5, ">", -1)]:
        predicate = {">": lambda x: x > 0, "<": lambda x: x < 0, "<=": lambda x: x <= 5}[op]
        steps = 0
        while predicate(value) and steps < 100:
            value += delta
            steps += 1
        states.append(not predicate(value))
    assert states == [False, False, False, True]
