"""课程名称与别名的集中映射。

页面输入、接口参数和目录记录都通过这里比较，避免别名规则散落在各处。
当前目录把课程合并为 13 门：高等数学、概率论与数理统计、几何与线性代数、
复变函数与积分变换、离散数学、大学物理、C语言程序设计、Python语言程序设计、
中国近现代史纲要、大学英语、思想道德与法治、马克思主义基本原理、转专业；
A/B、上/下、卷别等只保留在标题里。转专业不是科目，但作为一类考试单独成课，
方便学生按“转专业”检索。

注意：转专业是课程类别，不是考试类型；其试卷的 exam_type 记为 other（其他），
与期中、期末并列，不单独占用一种课程层之外的类型。
"""

import re

CANONICAL_COURSE = "高等数学"
COURSE_FAMILY = "高等数学"

# 键统一写成去空白后的形式，并按 ASCII 大写比较（见 normalize_course）。
ALIASES = {
    # 高等数学：BⅠ、AⅡ、上/下、A/B 等写法全部归到同一门课程。
    "高数": COURSE_FAMILY,
    "高等数学": COURSE_FAMILY,
    "高等数学BⅠ": COURSE_FAMILY,
    "高等数学BI": COURSE_FAMILY,
    "高等数学B1": COURSE_FAMILY,
    "高等数学B上": COURSE_FAMILY,
    "高等数学B下": COURSE_FAMILY,
    "高等数学BⅡ": COURSE_FAMILY,
    "高等数学BII": COURSE_FAMILY,
    "高等数学B2": COURSE_FAMILY,
    "高等数学AⅠ": COURSE_FAMILY,
    "高等数学AI": COURSE_FAMILY,
    "高等数学A1": COURSE_FAMILY,
    "高等数学A上": COURSE_FAMILY,
    "高等数学AⅡ": COURSE_FAMILY,
    "高等数学AII": COURSE_FAMILY,
    "高等数学A2": COURSE_FAMILY,
    "高等数学A下": COURSE_FAMILY,
    "高等数学（上）": COURSE_FAMILY,
    "高等数学(上)": COURSE_FAMILY,
    "高等数学上": COURSE_FAMILY,
    "高等数学（下）": COURSE_FAMILY,
    "高等数学(下)": COURSE_FAMILY,
    "高等数学下": COURSE_FAMILY,
    "高数BⅠ": COURSE_FAMILY,
    "高数BI": COURSE_FAMILY,
    "高数B1": COURSE_FAMILY,
    "高数B上": COURSE_FAMILY,
    "高数B下": COURSE_FAMILY,
    "高数AⅠ": COURSE_FAMILY,
    "高数AI": COURSE_FAMILY,
    "高数A1": COURSE_FAMILY,
    "高数A上": COURSE_FAMILY,
    "高数AⅡ": COURSE_FAMILY,
    "高数AII": COURSE_FAMILY,
    "高数A2": COURSE_FAMILY,
    "高数A下": COURSE_FAMILY,
    "高数（上）": COURSE_FAMILY,
    "高数(上)": COURSE_FAMILY,
    "高数上": COURSE_FAMILY,
    "高数（下）": COURSE_FAMILY,
    "高数(下)": COURSE_FAMILY,
    "高数下": COURSE_FAMILY,
    # 概率论与数理统计：A 版与非 A 版不区分课程，A、工科、经管只保留在标题里。
    "概率论": "概率论与数理统计",
    "概率": "概率论与数理统计",
    "概统": "概率论与数理统计",
    "概率统计": "概率论与数理统计",
    "概率论与数理统计": "概率论与数理统计",
    "概率论与数理统计A": "概率论与数理统计",
    "概率论与数理统计(A,B)": "概率论与数理统计",
    "概率论与数理统计（A,B）": "概率论与数理统计",
    "概率论A": "概率论与数理统计",
    "概率A": "概率论与数理统计",
    "概统A": "概率论与数理统计",
    # 几何与线性代数：几何与代数、线性代数、线性代数B 等写法归到同一门课程。
    "几何与线性代数": "几何与线性代数",
    "几何与代数": "几何与线性代数",
    "几何线代": "几何与线性代数",
    "几何代数": "几何与线性代数",
    "线性代数": "几何与线性代数",
    "线性代数A": "几何与线性代数",
    "线性代数B": "几何与线性代数",
    "线代": "几何与线性代数",
    "线代A": "几何与线性代数",
    "线代B": "几何与线性代数",
    # 复变函数与积分变换：A/B 版只保留在标题里。
    "复变函数与积分变换": "复变函数与积分变换",
    "复变函数": "复变函数与积分变换",
    "复变": "复变函数与积分变换",
    "积分变换": "复变函数与积分变换",
    "复变函数与积分变换A": "复变函数与积分变换",
    "复变函数与积分变换B": "复变函数与积分变换",
    "复变函数A": "复变函数与积分变换",
    "复变函数B": "复变函数与积分变换",
    "复变A": "复变函数与积分变换",
    "复变B": "复变函数与积分变换",
    # 离散数学：A/B/C 卷只保留在标题里。
    "离散数学": "离散数学",
    "离散": "离散数学",
    # 大学物理：A/B、I/II 等只保留在标题里。
    "大学物理": "大学物理",
    "大物": "大学物理",
    "物理": "大学物理",
    "大学物理A": "大学物理",
    "大学物理B": "大学物理",
    "大学物理I": "大学物理",
    "大学物理II": "大学物理",
    "大学物理AI": "大学物理",
    "大学物理AII": "大学物理",
    "大学物理BI": "大学物理",
    "大学物理BII": "大学物理",
    "大物A": "大学物理",
    "大物B": "大学物理",
    # C 语言程序设计：C/C语言/程序设计等写法归到同一门课程。
    "C": "C语言程序设计",
    "C语言": "C语言程序设计",
    "C语言程序设计": "C语言程序设计",
    "C程序设计": "C语言程序设计",
    "C语言程序": "C语言程序设计",
    # Python 语言程序设计：Python/Python语言等写法归到同一门课程。
    "PYTHON": "Python语言程序设计",
    "PYTHON语言": "Python语言程序设计",
    "PYTHON程序设计": "Python语言程序设计",
    "PYTHON语言程序设计": "Python语言程序设计",
    # 中国近现代史纲要：卷面有“中国近代史纲要”“中国近现代史纲要”两种写法，合并。
    "中国近现代史纲要": "中国近现代史纲要",
    "中国近代史纲要": "中国近现代史纲要",
    "中国近现代史": "中国近现代史纲要",
    "中国近代史": "中国近现代史纲要",
    "近现代史": "中国近现代史纲要",
    "近代史": "中国近现代史纲要",
    "史纲": "中国近现代史纲要",
    # 大学英语：I/II、1/2 版只保留在标题里。
    "大学英语": "大学英语",
    "大学英语I": "大学英语",
    "大学英语II": "大学英语",
    "大学英语1": "大学英语",
    "大学英语2": "大学英语",
    "英语": "大学英语",
    "英语I": "大学英语",
    "英语II": "大学英语",
    # 思想道德与法治：旧课程名“思想道德修养与法律基础”合并到现名。
    "思想道德与法治": "思想道德与法治",
    "思想道德修养与法律基础": "思想道德与法治",
    "思想道德修养": "思想道德与法治",
    "思想道德": "思想道德与法治",
    "思修": "思想道德与法治",
    # 马克思主义基本原理：概论版、马原、马基等写法归到同一门课程。
    "马克思主义基本原理": "马克思主义基本原理",
    "马克思主义基本原理概论": "马克思主义基本原理",
    "马原": "马克思主义基本原理",
    "马基": "马克思主义基本原理",
    # 转专业：不是科目，作为一类选拔考试单独成课，方便按“转专业”检索。
    "转专业": "转专业",
    "转专业考试": "转专业",
    "转专业真题": "转专业",
}

_WHITESPACE = re.compile(r"\s+")


def _key(raw: str) -> str:
    """把用户写法规范成可比较的键：去掉所有空白，ASCII 字母转大写。"""

    return _WHITESPACE.sub("", raw).upper()


def detect_course(text: str | None) -> str | None:
    """从自由文本中识别学生提到的课程。

    只做确定性匹配：在文本里找命中的别名，忽略单字母别名（如 C），
    命中多门不同课程时返回 None，交给模型澄清。
    返回规范课程名；没提课程也返回 None。
    """

    if not text:
        return None
    haystack = _key(text)
    hits = {
        course
        for alias, course in ALIASES.items()
        if len(alias) >= 2 and alias in haystack
    }
    return hits.pop() if len(hits) == 1 else None


def normalize_course(raw: str | None) -> str | None:
    """把课程写法转换为规范课程名。

    - None 或只有空白：返回 None，表示“不按课程过滤”。
    - 命中别名：返回规范课程名；“高数/高等数学”返回课程族名。
    - 未命中：返回规范化后的原值，因此查询结果为空，不会被替换成已知课程。
    """

    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    normalized = _key(text)
    return ALIASES.get(normalized, normalized)


def course_in_family(course: str) -> bool:
    """目录课程是否属于高等数学课程族。"""

    normalized = normalize_course(course)
    return normalized is not None and normalized.startswith(COURSE_FAMILY)


def course_matches(query: str | None, course: str) -> bool:
    """判断一条目录记录是否满足课程筛选。

    - 未给课程：全部匹配。
    - 族查询（高数/高等数学）：匹配所有高等数学记录。
    - 具体课程：按规范名精确匹配。
    """

    wanted = normalize_course(query)
    if wanted is None:
        return True
    if wanted == COURSE_FAMILY:
        return course_in_family(course)
    return normalize_course(course) == wanted
