"""辅导、图片与错题记录摘要的对外契约。

这些模型既是模型输出的校验目标，也是 SSE 和会话详情返回给前端的结构。
内部数据库行不直接透出，服务层负责把行转换成这里的 ProblemView/结果模型。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SourceType = Literal["none", "student_text", "registered_paper"]
ProblemStatus = Literal["awaiting_supplement", "explained"]
Clarity = Literal["clear", "unclear"]
AttachmentRole = Literal[
    "statement",
    "solution",
    "supplement",
    # 练习作答附件的角色；与题目附件共用视图结构。
    "student_answer",
    "student_solution",
]


@dataclass(frozen=True)
class ImagePayload:
    """传给模型的图片字节与真实 MIME；不经过前端路径。"""

    data: bytes
    mime: str


class Source(BaseModel):
    """题目来源；registered_paper 必须已通过目录校验。"""

    model_config = ConfigDict(extra="forbid")

    type: SourceType = "none"
    text: str | None = None
    paper_id: str | None = None
    paper_title: str | None = None

    @field_validator("text")
    @classmethod
    def _strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @model_validator(mode="after")
    def _check_shape(self) -> "Source":
        if self.type == "none":
            if self.text is not None or self.paper_id is not None:
                raise ValueError("无来源时不能带来源文字或 paper_id。")
            return self
        if self.type == "student_text":
            if not self.text:
                raise ValueError("学生自述来源必须提供文字。")
            if self.paper_id is not None:
                raise ValueError("学生自述来源不能带 registered paper_id。")
            return self
        if not self.paper_id:
            raise ValueError("已登记来源必须提供 paper_id。")
        if self.text is not None:
            raise ValueError("已登记来源不能同时带自述文字。")
        return self


class KnowledgePoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    explanation: str = Field(min_length=1, max_length=800)


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=1, le=20)
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=2000)


class ProblemUnderstanding(BaseModel):
    """模型对题目的整理结果，不包含解答。"""

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1, max_length=4000)
    clarity: Clarity
    unclear_points: list[str] = Field(default_factory=list, max_length=8)
    uncertainties: list[str] = Field(default_factory=list, max_length=8)
    multiple_problems: bool = False

    @field_validator("unclear_points", "uncertainties")
    @classmethod
    def _clean_items(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        if len(cleaned) > 8:
            raise ValueError("最多 8 条说明。")
        return cleaned

    @model_validator(mode="after")
    def _check_clarity(self) -> "ProblemUnderstanding":
        if self.multiple_problems and self.clarity != "unclear":
            raise ValueError("图片含多道题时必须标记为 unclear。")
        if self.clarity == "unclear" and not self.unclear_points and not self.multiple_problems:
            raise ValueError("不清楚的题目必须说明不清楚的点。")
        return self


class Explanation(BaseModel):
    """一次讲解的结构化内容。"""

    model_config = ConfigDict(extra="forbid")

    knowledge_points: list[KnowledgePoint] = Field(min_length=1, max_length=5)
    steps: list[Step] = Field(min_length=1, max_length=12)
    final_answer: str = Field(min_length=1, max_length=1600)
    error_analysis: str | None = Field(default=None, max_length=2400)
    uncertainties: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("error_analysis")
    @classmethod
    def _normalize_error(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None

    @field_validator("uncertainties")
    @classmethod
    def _clean_uncertainties(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()][:8]

    @model_validator(mode="after")
    def _check_steps(self) -> "Explanation":
        if [step.index for step in self.steps] != list(range(1, len(self.steps) + 1)):
            raise ValueError("步骤编号必须从 1 连续递增。")
        return self


class FollowupAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1, max_length=3000)
    related_steps: list[int] = Field(default_factory=list, max_length=12)

    @field_validator("related_steps")
    @classmethod
    def _clean_steps(cls, value: list[int]) -> list[int]:
        return sorted({int(item) for item in value if int(item) > 0})[:12]


class AttachmentView(BaseModel):
    id: str
    role: AttachmentRole
    mime: str
    width: int
    height: int
    url: str


class AttachmentMeta(BaseModel):
    """上传成功后的附件元数据，不含磁盘路径。"""

    id: str
    mime: str
    width: int
    height: int
    size_bytes: int
    created_at: datetime


class ProblemView(BaseModel):
    """当前题目的对外摘要；讲解内容在轮次结果中。"""

    id: str
    root_problem_id: str
    version: int
    statement_text: str
    status: ProblemStatus
    clarity: Clarity
    source: Source
    attachments: list[AttachmentView]
    student_answer_text: str | None
    solution_text: str | None
    self_reported_understood: bool
    created_at: datetime
    updated_at: datetime


class ExplanationResult(BaseModel):
    kind: Literal["explanation"] = "explanation"
    problem: ProblemView
    knowledge_points: list[KnowledgePoint]
    steps: list[Step]
    final_answer: str
    error_analysis: str | None
    uncertainties: list[str]
    self_reported_understood: bool
    record_id: str | None = None


class NeedsClarificationResult(BaseModel):
    kind: Literal["needs_clarification"] = "needs_clarification"
    problem: ProblemView
    questions: list[str] = Field(min_length=1, max_length=5)
    unclear_points: list[str]


class FollowupResult(BaseModel):
    kind: Literal["followup_answer"] = "followup_answer"
    problem_id: str
    answer: str
    related_steps: list[int]


class FeedbackResult(BaseModel):
    kind: Literal["feedback_recorded"] = "feedback_recorded"
    problem_id: str
    message: str


class RecordSummary(BaseModel):
    """错题记录摘要；错题本列表和轮次结果共用。"""

    id: str
    title: str
    statement_excerpt: str
    source: Source
    problem_version: int
    self_reported_understood: bool
    created_at: datetime
    updated_at: datetime


class RecordCreatedResult(BaseModel):
    kind: Literal["record_created"] = "record_created"
    record: RecordSummary


class RecordExistsResult(BaseModel):
    kind: Literal["record_exists"] = "record_exists"
    record: RecordSummary
    needs_update: bool
    message: str


class IntentClarificationResult(BaseModel):
    kind: Literal["intent_clarification"] = "intent_clarification"
    message: str
    options: list[str] = Field(min_length=1, max_length=6)


TutoringResult = Annotated[
    Union[
        ExplanationResult,
        NeedsClarificationResult,
        FollowupResult,
        FeedbackResult,
        RecordCreatedResult,
        RecordExistsResult,
        IntentClarificationResult,
    ],
    Field(discriminator="kind"),
]


class TutoringProgress(BaseModel):
    stage: Literal[
        "reading_problem",
        "checking_clarity",
        "explaining",
        "answering",
        "recording",
        "updating_record",
    ]
    message: str
