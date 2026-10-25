"""测试公用的小工具：路径常量、样例记录、清单写入、哈希与文件独占锁。

放在独立模块而不是 conftest.py 里，测试文件可以直接导入，不必依赖
pytest 的 fixture 名称解析。
"""

import contextlib
import ctypes
import hashlib
import json
import os
from collections.abc import Iterator
from ctypes import wintypes
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REAL_PAPERS_ROOT = PROJECT_ROOT / "data" / "papers"
REAL_COURSE_DIR = REAL_PAPERS_ROOT / "高数试卷"
REAL_MIDTERM = REAL_COURSE_DIR / "河海大学 2022一2023学年第一学期《高等数学BI》期中试卷.pdf"
REAL_FINAL = REAL_COURSE_DIR / "2024-2025学年第一学期《高等数学BⅠ》期末试题及答案.pdf"
REAL_B1_2021_MIDTERM = REAL_COURSE_DIR / "2021-2022学年第一学期高数期中试卷.pdf"
REAL_A2_FINAL = REAL_COURSE_DIR / "2024-2025高数AII期末考试试题.pdf"
REAL_UP_DIR = REAL_COURSE_DIR
REAL_UP_2014_MIDTERM = REAL_UP_DIR / "2014级高等数学（上）期中试卷.pdf"
REAL_PROB_DIR = REAL_PAPERS_ROOT / "概率论试卷"
REAL_PROB_SPLIT_DIR = REAL_PROB_DIR
REAL_PROB_2018_ENG = (
    REAL_PROB_SPLIT_DIR / "2018-2019学年工科类概率论与数理统计试卷及答案.pdf"
)
REAL_PROB_2021_MID = REAL_PROB_DIR / "21概率期中+答案(工科).pdf"
REAL_PROB_2021_FINAL = REAL_PROB_DIR / "21-22概率论期末试卷带答案.pdf"
REAL_GEO_DIR = REAL_PAPERS_ROOT / "几何与线性代数试卷"
REAL_GEO_2024_FINAL = REAL_GEO_DIR / "2024-2025学年第一学期《几何与线性代数》期末试卷.pdf"
REAL_CV_DIR = REAL_PAPERS_ROOT / "复变函数试卷"
REAL_CV_2021_MID = REAL_CV_DIR / "2021-2022学年第二学期《复变函数与积分变换》期中试卷及答案.pdf"
REAL_DM_DIR = REAL_PAPERS_ROOT / "离散数学试卷"
REAL_DM_2022_FINAL = REAL_DM_DIR / "2022-2023学年第一学期《离散数学》期末试卷.pdf"
REAL_PHYS_DIR = REAL_PAPERS_ROOT / "大学物理"
REAL_PHYS_2024_MID = REAL_PHYS_DIR / "2024-2025学年第二学期《大学物理AI》期中试卷.pdf"
REAL_C_DIR = REAL_PAPERS_ROOT / "C语言"
REAL_C_2022_FINAL = REAL_C_DIR / "2022-2023学年第二学期《C语言程序设计》期末试卷（A卷）.pdf"
REAL_PY_DIR = REAL_PAPERS_ROOT / "Python"
REAL_PY_2024_FINAL = REAL_PY_DIR / "2024-2025学年第二学期《Python语言程序设计》期末试卷（A卷）及答案.pdf"
REAL_HIST_DIR = REAL_PAPERS_ROOT / "中国近代史"
REAL_HIST_2016_FINAL_A = (
    REAL_HIST_DIR / "2016-2017学年第一学期《中国近现代史纲要》期末试卷（A卷）.pdf"
)
REAL_ENG_DIR = REAL_PAPERS_ROOT / "大学英语"
REAL_ENG_2024_MID = (
    REAL_ENG_DIR / "2023-2024学年第二学期《大学英语II》期中试卷及答案.pdf"
)
REAL_ETHICS_DIR = REAL_PAPERS_ROOT / "思想道德与法治"
REAL_ETHICS_2020_FINAL_A = (
    REAL_ETHICS_DIR / "2019-2020学年第二学期《思想道德与法治》期末试卷及答案（A卷）.pdf"
)
REAL_MARX_DIR = REAL_PAPERS_ROOT / "马原"
REAL_MARX_2023_FINAL = (
    REAL_MARX_DIR / "2023-2024学年第一学期《马克思主义基本原理》期末试卷（A卷）.pdf"
)
REAL_TRANSFER_DIR = REAL_PAPERS_ROOT / "转专业"
REAL_TRANSFER_MATH_2021 = (
    REAL_TRANSFER_DIR / "2021年河海大学转专业《高等数学B（工科）》试卷.pdf"
)
REAL_PHYS_2021_FINAL = (
    REAL_PHYS_DIR / "2021-2022学年第一学期《大学物理AII》期末试卷.pdf"
)

_GENERIC_READ = 0x80000000
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def sample_record(
    record_id: str,
    exam_type: str,
    exam_date: str | None,
    file_name: str,
    *,
    academic_year: str = "2022-2023",
) -> dict[str, Any]:
    """构造一条结构合法的隔离记录，默认属于高等数学 BⅠ。"""

    return {
        "id": record_id,
        "title": f"样例试卷 {record_id}",
        "course": "高等数学BⅠ",
        "academic_year": academic_year,
        "exam_type": exam_type,
        "exam_date": exam_date,
        "file_path": f"高数试卷/{file_name}",
    }


def write_catalog(root: Path, payload: object) -> None:
    """把给定内容写成目录文件。

    payload 可以是 bytes（用来造非 UTF-8 等原始内容）、str，
    或其他可序列化对象。
    """

    root.mkdir(parents=True, exist_ok=True)
    target = root / "catalog.json"

    if isinstance(payload, bytes):
        target.write_bytes(payload)
    elif isinstance(payload, str):
        target.write_text(payload, encoding="utf-8")
    else:
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextlib.contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """以“不共享”方式独占打开文件，模拟被其他程序占用/不可读。

    使用 Win32 CreateFileW 且 dwShareMode=0，因此同一进程内的 Python open
    也会得到 PermissionError；这比 monkeypatch 假异常更接近真实故障。
    只读取打开，不写入文件内容。
    """

    if os.name != "nt":
        pytest.skip("文件独占锁依赖 Windows 的文件共享语义")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE

    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        str(path),
        _GENERIC_READ,
        0,
        None,
        _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle in (None, _INVALID_HANDLE_VALUE):
        raise OSError(ctypes.get_last_error(), f"无法独占打开测试文件：{path}")

    try:
        yield
    finally:
        close_handle(handle)
