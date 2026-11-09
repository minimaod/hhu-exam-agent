"""自然语言查询的本地配置。

配置在应用启动时读取一次并保存为快照；修改 .env 后需要重启后端。
读取时环境变量优先；.env 的值不会写回 os.environ。
"""

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 只认项目根目录这一处；测试会把它替换成临时路径。
ENV_FILE = PROJECT_ROOT / ".env"

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

_BASE_URL_ERROR = "自然语言查询配置无效：DEEPSEEK_BASE_URL 必须是合法的 http(s) 地址。"
_ENV_FILE_ERROR = "无法读取 .env 配置文件，请检查文件编码或访问权限。"


class QueryConfigError(Exception):
    """配置缺失或非法。

    启动时由 load_config_state 转成错误状态；接口层在建立 SSE 流之前
    返回 503。消息里不包含密钥。
    """


@dataclass(frozen=True)
class DeepSeekSettings:
    """一次查询需要的 DeepSeek 配置；不含超时等实现参数。"""

    api_key: str
    base_url: str
    model: str


@dataclass(frozen=True)
class QueryConfigState:
    """启动时固定的配置结果；error 只含可展示文案，不含密钥。"""

    settings: DeepSeekSettings | None
    error: str | None


class _SettingsSource:
    """一次配置读取的值来源：环境变量优先，缺失项才读取 .env。"""

    def __init__(self, env_file: Path) -> None:
        self._env_file = env_file
        self._file_values: dict[str, str] | None = None

    def get(self, name: str, default: str = "") -> str:
        if name in os.environ:
            return os.environ[name].strip()
        self._ensure_file_loaded()
        assert self._file_values is not None
        return self._file_values.get(name, default).strip()

    def _ensure_file_loaded(self) -> None:
        if self._file_values is not None:
            return
        try:
            values = dotenv_values(self._env_file, encoding="utf-8")
        except (OSError, ValueError) as exc:
            # UnicodeDecodeError 是 ValueError 的子类；权限问题属于 OSError。
            raise QueryConfigError(_ENV_FILE_ERROR) from exc
        self._file_values = {
            key: value for key, value in values.items() if value is not None
        }


def _normalize_base_url(raw: str) -> str:
    """校验并去掉末尾斜杠；畸形 URL 统一转成配置错误。"""

    if any(ch.isspace() or ord(ch) < 32 for ch in raw):
        raise QueryConfigError(_BASE_URL_ERROR)

    try:
        parsed = urlparse(raw)
        hostname = parsed.hostname
        parsed.port  # 触发端口数字/范围校验
    except ValueError as exc:
        raise QueryConfigError(_BASE_URL_ERROR) from exc

    if parsed.scheme not in {"http", "https"} or not hostname:
        raise QueryConfigError(_BASE_URL_ERROR)

    return raw.rstrip("/")


def load_query_settings() -> DeepSeekSettings:
    """读取并校验一次配置。

    正常运行时由启动快照调用一次；测试可以直接调用，用来检查环境变量
    优先、文件内容或畸形值。每次调用都会重新读取文件。
    """

    source = _SettingsSource(ENV_FILE)

    api_key = source.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise QueryConfigError("自然语言查询尚未配置：缺少 DEEPSEEK_API_KEY。")

    base_url = _normalize_base_url(
        source.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL
    )
    model = source.get("DEEPSEEK_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL

    return DeepSeekSettings(api_key=api_key, base_url=base_url, model=model)


def load_config_state() -> QueryConfigState:
    """启动时调用：把成功配置或可展示错误固定成快照。"""

    try:
        return QueryConfigState(settings=load_query_settings(), error=None)
    except QueryConfigError as exc:
        return QueryConfigState(settings=None, error=str(exc))


# ---------------------------------------------------------------------------
# F012：登录会话与 CSRF 配置
# ---------------------------------------------------------------------------

DEFAULT_SESSION_TTL_DAYS = 14
DEFAULT_ALLOWED_ORIGINS = (
    "http://127.0.0.1:18080",
    "http://localhost:18080",
    "http://127.0.0.1:5173",
    "http://localhost:5173",
)


@dataclass(frozen=True)
class AuthConfigState:
    cookie_name: str = "hhu_session"
    cookie_secure: bool = False
    session_ttl_days: int = DEFAULT_SESSION_TTL_DAYS
    allowed_origins: tuple[str, ...] = DEFAULT_ALLOWED_ORIGINS
    error: str | None = None


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError("HHU_COOKIE_SECURE 只接受 true/false。")


def load_auth_config_state() -> AuthConfigState:
    """读取登录 Cookie 配置；非法值不阻止健康检查/试卷，但认证返回 503。"""

    source = _SettingsSource(ENV_FILE)
    try:
        cookie_secure = _parse_bool(source.get("HHU_COOKIE_SECURE"), False)
        ttl_raw = source.get("HHU_SESSION_TTL_DAYS")
        ttl = DEFAULT_SESSION_TTL_DAYS if not ttl_raw else int(ttl_raw)
        if not 1 <= ttl <= 365:
            raise ValueError("HHU_SESSION_TTL_DAYS 必须在 1–365 之间。")
        origins_raw = source.get("HHU_ALLOWED_ORIGINS")
        origins = (
            tuple(
                item.strip().rstrip("/")
                for item in origins_raw.split(",")
                if item.strip()
            )
            if origins_raw
            else DEFAULT_ALLOWED_ORIGINS
        )
        return AuthConfigState(
            cookie_secure=cookie_secure,
            session_ttl_days=ttl,
            allowed_origins=origins,
        )
    except (ValueError, QueryConfigError) as exc:
        return AuthConfigState(error=f"登录配置无效：{exc}")
