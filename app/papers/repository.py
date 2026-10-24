"""JSON 试卷目录的读取、校验、筛选与按 ID 查找。

这是目前唯一知道“目录存在哪个文件、长什么结构”的模块。以后换成 MySQL 时，
新增一个实现相同方法的仓库类替换它即可，服务层和接口层不改。
"""

import json
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from app.papers.courses import course_matches
from app.papers.errors import CatalogUnavailableError
from app.papers.models import ExamType, PaperRecord

CATALOG_FILENAME = "catalog.json"


def _academic_year_start(value: str) -> int:
    """取学年字符串的前四位；无法解析时按 0 处理，排在最后。"""

    head = value.strip()[:4]
    return int(head) if head.isdigit() else 0


class PaperRepository:
    """按资料根目录读取目录文件；每次调用都重新读取，页面不会看到过期数据。"""

    def __init__(self, papers_root: Path) -> None:
        self._papers_root = papers_root

    @property
    def papers_root(self) -> Path:
        """资料根目录，下载时用它做路径包含性检查。"""

        return self._papers_root

    def list_papers(
        self,
        course: str | None = None,
        exam_type: ExamType | None = None,
    ) -> list[PaperRecord]:
        """按可选条件筛选，并按学年、考试日期从近到远返回。"""

        records = self._load()

        records = [
            record for record in records if course_matches(course, record.course)
        ]

        if exam_type is not None:
            records = [record for record in records if record.exam_type == exam_type]

        # 三次稳定排序，优先级从低到高：
        # 1. id 升序，作为最终稳定的次序；
        # 2. 同一学年内考试日期降序，空日期垫底；
        # 3. 学年起始年降序，让最近学年整体排在前面。
        records.sort(key=lambda record: record.id)
        records.sort(key=lambda record: record.exam_date or date.min, reverse=True)
        records.sort(
            key=lambda record: _academic_year_start(record.academic_year),
            reverse=True,
        )
        return records

    def get_paper(self, paper_id: str) -> PaperRecord | None:
        """按稳定 ID 查找；查不到返回 None，由上层决定如何反馈。"""

        for record in self._load():
            if record.id == paper_id:
                return record
        return None

    def _load(self) -> list[PaperRecord]:
        """读取并校验收整个目录文件，任何结构问题都转成“目录不可用”。"""

        catalog_path = self._papers_root / CATALOG_FILENAME

        try:
            raw_text = catalog_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise CatalogUnavailableError("试卷目录文件不存在，暂时无法查询试卷。") from exc
        except UnicodeDecodeError as exc:
            # read_text 的编码错误是 UnicodeDecodeError（ValueError 子类），不是 OSError；
            # 不单独处理就会冒着 500 直接穿透到客户端。
            raise CatalogUnavailableError(
                "试卷目录不是合法的 UTF-8 文本，暂时无法查询试卷。"
            ) from exc
        except OSError as exc:
            raise CatalogUnavailableError("试卷目录文件无法读取，暂时无法查询试卷。") from exc

        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise CatalogUnavailableError("试卷目录不是合法 JSON，暂时无法查询试卷。") from exc

        if not isinstance(payload, list):
            raise CatalogUnavailableError("试卷目录格式错误：顶层应为记录数组。")

        records: list[PaperRecord] = []
        for index, item in enumerate(payload, start=1):
            try:
                records.append(PaperRecord.model_validate(item))
            except ValidationError as exc:
                raise CatalogUnavailableError(
                    f"试卷目录第 {index} 条记录字段不合法，暂时无法查询试卷。"
                ) from exc

        paper_ids = [record.id for record in records]
        if len(paper_ids) != len(set(paper_ids)):
            raise CatalogUnavailableError("试卷目录存在重复的 id，暂时无法查询试卷。")

        return records
