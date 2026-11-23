"""F017 Fix-004：验证摘要截断不会把 LaTeX 公式从中间切开。"""

from app.memory.evidence import _excerpt
from app.text_excerpt import math_safe_excerpt


def test_math_safe_excerpt_backs_off_before_split_inline_formula():
    text = (
        "设函数 $z = f(x,y)$ 具有连续的一阶偏导数，且 $x = u^2 + v^2$，$y = u v$。"
        "已知 $f_x(2,1) = 3$，$f_y(2,1) = -1$，求偏导数并给出过程说明"
    )
    clipped = math_safe_excerpt(text, 80)
    assert clipped.endswith("…")
    assert clipped.count("$") % 2 == 0
    assert "$f_y(2" not in clipped
    assert _excerpt(text) == clipped


def test_math_safe_excerpt_keeps_leading_long_formula_intact():
    text = r"$$\lim_{x\to 0}\frac{\sqrt{1+\tan x}-\sqrt{1+\sin x}}{x^3}$$ 后续说明文字"
    clipped = math_safe_excerpt(text, 20)
    assert clipped.startswith(r"$$\lim_{x\to 0}")
    assert "$$…" in clipped


def test_math_safe_excerpt_trims_unclosed_opener_in_head():
    text = "已知条件 $ 后面全是未闭合文本" + "很长" * 40
    clipped = math_safe_excerpt(text, 24)
    assert clipped == "已知条件…"


def test_math_safe_excerpt_trims_unclosed_opener_at_start():
    for opener in ("$", "$$", r"\[", r"\("):
        assert math_safe_excerpt(opener + "f_y(2 后续文字很长", 10) == "…"
        assert math_safe_excerpt(opener + "f_y(2 后续文字很长", 2) == "…"


def test_math_safe_excerpt_keeps_escaped_dollar_and_following_text():
    assert math_safe_excerpt(r"价格 \$5，后面还有很长一段说明", 12) == r"价格 \$5，后面还有…"


def test_math_safe_excerpt_leaves_short_text_untouched():
    assert math_safe_excerpt("求极限 $\\lim_{x\\to 0}\\frac{\\sin x}{x}$。", 80) == (
        "求极限 $\\lim_{x\\to 0}\\frac{\\sin x}{x}$。"
    )
