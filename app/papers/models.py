"""试卷目录的字段契约。

PaperRecord 是内部记录，含 file_path，只在后端流通。
PaperItem 是对外条目，不含 file_path，页面和接口只能看到它。
二者分开，避免“记录里多一个字段”就自动泄露到前端。
"""

from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# 目录里能出现的考试类型；页面分别显示为“期中”“期末”“其他”。
# “转专业”不是类型，而是课程/科目；转专业试卷的类型记为 other。
ExamType = Literal["midterm", "final", "other"]


class PaperRecord(BaseModel):
    """一条已登记的真实试卷记录，对应 catalog.json 中的一个对象。"""

    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    title: str = Field(min_length=1)
    course: str = Field(min_length=1)
    academic_year: str = Field(min_length=1)
    exam_type: ExamType
    # 考试日期来源不明时留空，不猜测。
    exam_date: date | None = None
    # 相对资料根目录 data/papers 的路径，仅供后端解析真实文件。
    file_path: str = Field(min_length=1)

    @field_validator("file_path")
    @classmethod
    def _reject_absolute_path(cls, value: str) -> str:
        """拒绝绝对路径：目录只允许登记“资料根目录下的相对位置”。

        含 `..` 的相对路径在这一步放行，由下载时的路径包含性检查拒绝，
        两处检查职责不同：这里是字段合法性，那里是文件访问安全。
        """

        if not value.strip():
            raise ValueError("file_path 不能为空")
        if Path(value).is_absolute() or value.startswith(("/", "\\")):
            raise ValueError("file_path 必须是相对 data/papers 的路径")
        return value


class PaperItem(BaseModel):
    """对外返回的一条试卷信息，页面直接渲染这些字段。"""

    id: str
    title: str
    course: str
    academic_year: str
    exam_type: ExamType
    exam_date: date | None
    download_url: str


class PaperListResponse(BaseModel):
    """/api/papers 的成功响应。total 等于本次返回的条数。"""

    items: list[PaperItem]
    total: int
