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
import app.images as images
from app.auth.dependencies import require_user
from app.auth.models import User
from app.main import app
from app.papers.repository import PaperRepository
from app.papers.service import PaperService, get_paper_service
from tests.f012_support import TEST_USER_ID, TEST_USERNAME, ensure_test_user
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


@pytest.fixture(autouse=True)
def _isolate_attachments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """上传文件也只能写到用例自己的临时目录。"""

    monkeypatch.setattr(images, "ATTACHMENTS_ROOT", tmp_path / "attachments")


@pytest.fixture(autouse=True)
def _allow_legacy_unscoped_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧测试直接构造无 owner 的仓库；生产 API 仍然传 scoped 仓库。"""

    from app.conversations.service import ConversationService

    original = ConversationService.__init__

    def wrapped(self, *args, **kwargs):
        kwargs.setdefault("allow_unscoped", True)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ConversationService, "__init__", wrapped)

    from app.memory.service import LearningMemoryService

    original_memory = LearningMemoryService.__init__

    def wrapped_memory(self, *args, **kwargs):
        kwargs.setdefault("owner_id", TEST_USER_ID)
        return original_memory(self, *args, **kwargs)

    monkeypatch.setattr(LearningMemoryService, "__init__", wrapped_memory)


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
        app.dependency_overrides[require_user] = lambda: User(
            id=TEST_USER_ID,
            username=TEST_USERNAME,
            created_at="2026-01-01T00:00:00+00:00",
        )
        client = TestClient(app)
        client.__enter__()
        ensure_test_user(conversation_repository.DEFAULT_DB_PATH)
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


@pytest.fixture(autouse=True)
def _default_legacy_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧测试默认以隔离用户身份运行；真实认证用例用 anon_client 清除。

    这里同时包住 build_store_state，在数据库初始化后补测试用户，保证
    旧测试直接造会话时 owner 外键有效。生产请求仍走真实 Cookie 会话。
    """

    import app.main as main_module

    original = main_module.build_store_state

    def build():
        state = original()
        if state.db_path is not None and state.error is None:
            ensure_test_user(state.db_path)
        return state

    monkeypatch.setattr(main_module, "build_store_state", build)
    app.dependency_overrides[require_user] = lambda: User(
        id=TEST_USER_ID,
        username=TEST_USERNAME,
        created_at="2026-01-01T00:00:00+00:00",
    )
    yield
    app.dependency_overrides.pop(require_user, None)


@pytest.fixture
def real_client() -> Iterator[TestClient]:
    """使用项目真实 data/papers 的客户端；只读，不修改原始 PDF。"""

    app.dependency_overrides.pop(require_user, None)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def anon_client() -> Iterator[TestClient]:
    """无认证 override 的真实客户端，用于认证/隔离/迁移启动用例。"""

    app.dependency_overrides.clear()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def legacy_client(
    sample_root: Path, make_client: Callable[[Path], TestClient]
) -> TestClient:
    """旧测试使用的已登录客户端；override 只在本夹具内生效。"""

    return make_client(sample_root)
