"""试卷业务服务：协调查询与下载，并对下载路径做安全校验。

接口层只传参数和返回结果，读 JSON、拼下载地址、判断文件是否安全都在这里。
"""

from dataclasses import dataclass
from pathlib import Path

from app.papers.errors import PaperUnavailableError
from app.papers.models import ExamType, PaperItem, PaperListResponse, PaperRecord
from app.papers.repository import PaperRepository

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PAPERS_ROOT = PROJECT_ROOT / "data" / "papers"
DOWNLOAD_URL_TEMPLATE = "/api/papers/{paper_id}/download"


@dataclass(frozen=True)
class DownloadTarget:
    """一次下载需要的信息：记录、已校验的真实路径、对外保留的原文件名。"""

    record: PaperRecord
    path: Path
    filename: str


class PaperService:
    def __init__(self, repository: PaperRepository) -> None:
        self._repository = repository

    def list_papers(
        self,
        course: str | None = None,
        exam_type: ExamType | None = None,
    ) -> PaperListResponse:
        records = self._repository.list_papers(course=course, exam_type=exam_type)
        items = [self._to_item(record) for record in records]
        return PaperListResponse(items=items, total=len(items))

    def get_paper(self, paper_id: str) -> PaperItem | None:
        """按登记 ID 取对外条目；仅供来源校验，不检查 PDF 是否存在。"""

        record = self._repository.get_paper(paper_id)
        return None if record is None else self._to_item(record)

    def get_download(self, paper_id: str) -> DownloadTarget:
        """把已登记 ID 换成可安全读取的 PDF 路径。

        依次检查：ID 是否登记 → 解析后的路径是否仍在资料根目录内
        （同时挡住 `..` 越界和符号链接逃逸）→ 是否为常规文件 → 是否为 PDF
        → 是否真的能读（在发送响应前发现可预见的读取失败）。
        """

        record = self._repository.get_paper(paper_id)
        if record is None:
            raise PaperUnavailableError("试卷不存在或未登记。")

        papers_root = self._repository.papers_root.resolve()
        candidate = (papers_root / record.file_path).resolve()

        try:
            candidate.relative_to(papers_root)
        except ValueError as exc:
            raise PaperUnavailableError("该试卷的文件位置不可用。") from exc

        if not candidate.is_file():
            raise PaperUnavailableError("该试卷的原始文件不存在或不可读取。")

        if candidate.suffix.lower() != ".pdf":
            raise PaperUnavailableError("该试卷的文件不是可下载的 PDF。")

        self._ensure_readable(candidate)

        return DownloadTarget(
            record=record,
            path=candidate,
            filename=Path(record.file_path).name,
        )

    @staticmethod
    def _ensure_readable(path: Path) -> None:
        """在发送响应前真正打开文件并读一个字节。

        FileResponse 是先发 200 头再打开文件，读失败时状态码已经发出去了，
        客户端只能拿到“200 + 0 字节”的坏响应。这里提前暴露被其他程序占用、
        权限不足、设备异常等可预见的读取失败，按契约返回 404。
        """

        try:
            with path.open("rb") as probe:
                probe.read(1)
        except OSError as exc:
            raise PaperUnavailableError(
                "该试卷的原始文件当前无法读取，请稍后再试。"
            ) from exc

    @staticmethod
    def _to_item(record: PaperRecord) -> PaperItem:
        """目录记录 → 对外条目：下载地址由后端生成，file_path 不出现。"""

        return PaperItem(
            id=record.id,
            title=record.title,
            course=record.course,
            academic_year=record.academic_year,
            exam_type=record.exam_type,
            exam_date=record.exam_date,
            download_url=DOWNLOAD_URL_TEMPLATE.format(paper_id=record.id),
        )


def get_paper_service() -> PaperService:
    """FastAPI 依赖：默认读取项目内 data/papers，测试可整体替换。"""

    return PaperService(PaperRepository(DEFAULT_PAPERS_ROOT))
