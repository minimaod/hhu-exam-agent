"""测试夹具：隔离资料目录、可替换依赖的客户端、真实资料客户端。

关键点：用 app.dependency_overrides 把 get_paper_service 整体替换掉，
这样异常用例可以在临时目录里造“文件缺失 / 路径越界 / 目录损坏”，
完全不需要移动、改名或删除用户的原始 PDF。
"""

import os
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

# 测试临时根目录固定在项目的 tmp/pytest-tmp。必须在导入 app 之前设置：
# pytest 的 tmp_path、tempfile 和 jieba 缓存都以 tempfile.gettempdir() 为根；
# 未配置 TEMP/TMP 时它会退回当前工作目录，文件就会散落到项目根目录。
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_TEST_TMP_ROOT = (_PROJECT_ROOT / "tmp" / "pytest-tmp").resolve()
_TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
_TEST_TMP_TEXT = str(_TEST_TMP_ROOT)

os.environ.pop("TMPDIR", None)  # POSIX 变量；无效值会让 tempfile 退回 cwd
os.environ["TEMP"] = _TEST_TMP_TEXT
os.environ["TMP"] = _TEST_TMP_TEXT
os.environ["PYTEST_DEBUG_TEMPROOT"] = _TEST_TMP_TEXT
tempfile.tempdir = _TEST_TMP_TEXT  # 覆盖 tempfile 已缓存的目录

import pytest
from fastapi.testclient import TestClient

import app.config as query_config
import app.conversations.repository as conversation_repository
from app.main import app
from app.papers.repository import PaperRepository
from app.papers.service import PaperService, get_paper_service
from tests.support import sample_record, write_catalog


@pytest.fixture(autouse=True)
def _isolate_query_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """把查询配置与开发者本地 .env、终端环境变量隔离开。

    否则本机真实密钥会让“缺配置返回 503”一类用例变得不确定。
    """

    monkeypatch.setattr(query_config, "ENV_FILE", tmp_path / "absent.env")
    for name in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _isolate_study_db(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """把本地学习数据库指到临时目录。

    lifespan 会在每个 TestClient 启动时初始化数据库；若不隔离，跑测试就会
    在项目的 data/local 下创建或修改真实的个人学习数据。

    build_store_state 优先读取 HHU_STUDY_DB；先清除外部变量，再替换默认路径，
    保证每个用例只写自己的 tmp_path。
    """

    monkeypatch.delenv("HHU_STUDY_DB", raising=False)
    monkeypatch.setattr(
        conversation_repository,
        "DEFAULT_DB_PATH",
        tmp_path / "study.sqlite3",
    )


@pytest.fixture(autouse=True)
def _isolate_question_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认测试不加载真实题库/Qdrant；需要真题的用例自行替换运行时。"""

    from app.questions.runtime import QuestionRuntime

    monkeypatch.setattr(
        "app.main.build_question_runtime",
        lambda: QuestionRuntime(None, error="测试环境未启用题目库"),
    )


@pytest.fixture
def study_db_path(tmp_path: Path) -> Path:
    """当前用例使用的隔离数据库路径；多次启动应用会复用同一个文件。"""

    return tmp_path / "study.sqlite3"


@pytest.fixture
def sample_root(tmp_path: Path) -> Path:
    """临时资料根目录：两份假 PDF 加一份合法清单。"""

    root = tmp_path / "papers"
    course_dir = root / "高数试卷"
    course_dir.mkdir(parents=True)
    (course_dir / "期中样例.pdf").write_bytes(b"%PDF-1.4\n% midterm fixture\n")
    (course_dir / "期末样例.pdf").write_bytes(b"%PDF-1.4\n% final fixture\n")
    write_catalog(
        root,
        [
            sample_record("sample-midterm", "midterm", "2023-11-08", "期中样例.pdf"),
            sample_record("sample-final", "final", "2025-01-16", "期末样例.pdf"),
        ],
    )
    return root


@pytest.fixture
def make_client() -> Iterator[Callable[[Path], TestClient]]:
    """按指定资料目录创建客户端；一个用例可以创建多个不同目录的客户端。"""

    clients: list[TestClient] = []

    def _make(root: Path) -> TestClient:
        app.dependency_overrides[get_paper_service] = lambda: PaperService(
            PaperRepository(root)
        )
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    try:
        yield _make
    finally:
        for client in clients:
            client.__exit__(None, None, None)
        app.dependency_overrides.clear()


@pytest.fixture
def client(sample_root: Path, make_client: Callable[[Path], TestClient]) -> TestClient:
    return make_client(sample_root)


@pytest.fixture
def real_client() -> Iterator[TestClient]:
    """使用项目真实 data/papers 的客户端；只读，不修改原始 PDF。"""

    assert not app.dependency_overrides, "上一个用例遗留了依赖替换"
    with TestClient(app) as test_client:
        yield test_client
