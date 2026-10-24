"""自然语言查询的内部契约。

Understanding 是模型唯一被允许输出的内容，四个字段全部必填并经过组合校验。
ResultEvent 等事件模型是后端对前端的承诺，页面据此判断业务结果。
"""

from collections.abc import Awaitable, Callable
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.papers.models import ExamType, PaperItem

# 模型可以给出的理解结果。
Reason = Literal[
    "missing_course",
    "ambiguous_course",
    "unsupported_constraint",
    "out_of_scope",
]

# 对外错误码；具体映射见 app/querying/llm.py 与 service.py。
ErrorCode = Literal[
    "MODEL_UNAVAILABLE",
    "MODEL_AUTH_FAILED",
    "MODEL_RATE_LIMITED",
    "MODEL_TIMEOUT",
    "MODEL_RESPONSE_INVALID",
    "CATALOG_UNAVAILABLE",
    "QUERY_FAILED",
]

ProgressStage = Literal["understanding", "retrieving"]
QueryKind = Literal["found", "empty", "clarification", "unsupported"]


class Understanding(BaseModel):
    """模型对一句需求的全部理解，不能夹带试卷、ID、路径或代码。

    四个字段都必填：模型必须显式输出 null，避免“少一个字段”被误当成
    “不限制该条件”。未知字段一律拒绝。
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["search", "clarify", "unsupported"]
    course: str | None
    exam_type: ExamType | None
    reason: Reason | None

    @field_validator("course")
    @classmethod
    def _strip_course(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("course 不能是空白字符串；没有课程时请输出 null。")
        return text

    @model_validator(mode="after")
    def _check_combination(self) -> "Understanding":
        """action 与 reason、course 必须自洽，否则整条输出作废。"""

        if self.action == "search":
            if not self.course:
                raise ValueError("search 必须给出非空的 course。")
            if self.reason is not None:
                raise ValueError("search 的 reason 必须为 null。")
            return self

        if self.action == "clarify":
            if self.reason not in ("missing_course", "ambiguous_course"):
                raise ValueError("clarify 的 reason 必须是 missing_course 或 ambiguous_course。")
            return self

        if self.reason not in ("unsupported_constraint", "out_of_scope"):
            raise ValueError(
                "unsupported 的 reason 必须是 unsupported_constraint 或 out_of_scope。"
            )
        return self


class QueryFilters(BaseModel):
    """本次实际使用的检索条件；未检索时整个 filters 为 null。"""

    course: str | None
    exam_type: ExamType | None


class ProgressEvent(BaseModel):
    type: Literal["progress"] = "progress"
    stage: ProgressStage
    message: str


class ResultEvent(BaseModel):
    """一次查询的业务结果；find/empty/澄清/范围提示用 kind 区分。"""

    type: Literal["result"] = "result"
    kind: QueryKind
    message: str
    filters: QueryFilters | None
    items: list[PaperItem]
    total: int

    @model_validator(mode="after")
    def _check_shape(self) -> "ResultEvent":
        if self.total != len(self.items):
            raise ValueError("total 必须等于 items 的长度。")

        if self.kind == "found":
            if not self.items or self.filters is None:
                raise ValueError("found 必须有检索结果和 filters。")
        elif self.kind == "empty":
            if self.items or self.filters is None:
                raise ValueError("empty 必须没有结果但保留 filters。")
        elif self.items or self.filters is not None:
            raise ValueError("clarification/unsupported 不得包含结果或 filters。")

        return self


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    code: ErrorCode
    message: str


QueryEvent = ProgressEvent | ResultEvent | ErrorEvent

# 模型适配器只需实现“问题 → 已校验的理解结果”这一件事，测试可以整体替换。
Understander = Callable[[str], Awaitable[Understanding]]


class QueryState(TypedDict, total=False):
    """一次请求内的工作流状态，不是跨请求的学习记忆。"""

    query: str
    understanding: Understanding
    result: ResultEvent
