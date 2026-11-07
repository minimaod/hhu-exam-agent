"""F008：调度层的原生工具契约。

职责边界：
- 每个业务能力对应一个独立工具（名称、参数模型、JSON Schema、对应 ActionType），
  不提供“任意动作名 + 任意参数”的总工具。
- 参数校验由 Pydantic 完成；JSON Schema 只负责告诉模型有哪些字段，二者由
  ``tests/test_f008_tool_schema.py`` 断言一一对应，防止漂移。
- 本模块不执行任何业务、不访问数据库；只做“模型请求 → 规范化 ActionPlan”
  与“执行结果 → 可公开细节”的转换。

工具名与 ActionType 的映射保持一对一，因此服务层、依赖表、恢复检查点和
历史记录都不需要随协议改变而改变。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.conversations.protocol import ActionPlan

ToolName = Literal[
    "confirm_course",
    "query_papers",
    "search_questions",
    "list_paper_questions",
    "select_question",
    "explain_problem",
    "answer_followup",
    "generate_exercise",
    "evaluate_exercise",
    "reveal_solution",
    "show_question_answer",
    "save_mistake",
    "record_feedback",
    "ask_user",
    "review_learning_memory",
    "correct_learning_memory",
    "delete_learning_memory",
    # F010 B：四个只读工具。
    "view_workspace",
    "view_question_detail",
    "list_shown_questions",
    "view_learning_summary",
]

ExamType = Literal["midterm", "final", "other"]
QuestionType = Literal["choice", "fill", "code"]

# 只允许进入“最终回复材料”的 details 键。新增业务字段时必须先在这里登记，
# 否则回复模型看不到，避免把答案正文一类的私有字段顺手带进正文。
_PUBLIC_DETAIL_KEYS = frozenset(
    {
        "status",
        "count",
        "question_ids",
        "selected",
        "question_id",
        "index",
        "course",
        "exam_type",
        "paper_count",
        "filters",
        "record_id",
        "exercise_id",
        "question_type",
        "number_label",
        "selected",
        "skipped",
        "rejected",
        "error_code",
        "retryable",
        "needs_supplement",
        "independence",
        "mode",
        "version",
        # 整卷浏览：总数、本次范围、下一批位置，都要能进正文。
        "total",
        "offset",
        "returned",
        "next_offset",
        "has_more",
        "paper_id",
        "paper_title",
        "papers",
        # 查卷唯一命中的下载入口与定位条件：正文只写这里给出的链接。
        "download_url",
        "academic_year",
        "term",
        "solution_image_count",
        "feedback_saved",
        "user_feedback",
        "followup_target",
        # 学习记忆：只放公开事实与可追溯标识，不放答案正文。
        "memory_entry_id",
        "memory_entry",
        "memory_entries",
        "memory_course",
        "memory_source",
        "memory_range",
        "memory_counts",
        "memory_groups",
        "memory_suggestions",
        "memory_pending",
        "memory_removed",
        "memory_message",
        # F010 B：只读工作区的公开引用与版本元数据。
        "ref",
        "shown_count",
        "from_snapshot",
        "build_id",
        "content_sha256",
        "has_more",
    }
)


class _Strict(BaseModel):
    """所有工具参数都拒绝多余字段，防止模型夹带未定义参数。"""

    model_config = ConfigDict(extra="forbid")


class ConfirmCourseArgs(_Strict):
    course: str = Field(min_length=1, max_length=40)


class BasisArgs(_Strict):
    """F010 D：受保护动作的可追溯依据；只证明“本轮有依据”，不代替授权。"""

    kind: Literal[
        "student_request", "confirmed_suggestion", "button", "tool_result"
    ]
    quote: str | None = Field(default=None, max_length=200)
    suggestion_id: str | None = Field(default=None, max_length=80)
    depends_on_call_id: str | None = Field(default=None, max_length=80)


class QueryPapersArgs(_Strict):
    course: str | None = Field(default=None, max_length=40)
    exam_type: ExamType | None = None
    academic_year: str | None = Field(default=None, max_length=20)
    term: Literal["s1", "s2"] | None = None
    variant: Literal["A", "B"] | None = None
    paper_id: str | None = Field(default=None, max_length=80)
    # 学生明确“不限期中期末”：null 与省略无法区分，必须显式给布尔。
    all_exam_types: bool | None = None


class SearchQuestionsArgs(_Strict):
    course: str = Field(min_length=1, max_length=40)
    query: str | None = Field(default=None, max_length=200)
    exam_type: ExamType | None = None
    question_type: QuestionType | None = None
    knowledge_points: list[str] | None = Field(default=None, max_length=5)
    number_label: str | None = Field(default=None, max_length=20)
    paper_id: str | None = Field(default=None, max_length=80)
    offset: int | None = Field(default=None, ge=0)
    continue_from_previous: bool = False
    auto_explain: bool = False
    scope: Literal["current", "paper", "bank"] = "current"
    exclude_question_id: str | None = Field(default=None, max_length=80)
    # 学生说“换一道/再来一道”：排除上一轮已经展示的题。
    exclude_previous: bool = False
    defer_answer: bool = False

    @model_validator(mode="after")
    def _one_page_target(self) -> "SearchQuestionsArgs":
        if self.offset is not None and self.continue_from_previous:
            raise ValueError("offset 与 continue_from_previous 只能选一个")
        return self


class ListPaperQuestionsArgs(_Strict):
    course: str | None = Field(default=None, max_length=40)
    paper_id: str | None = Field(default=None, max_length=80)
    question_type: QuestionType | None = None
    offset: int | None = Field(default=None, ge=0)
    continue_from_previous: bool = False
    limit: int | None = Field(default=None, ge=1, le=64)

    @model_validator(mode="after")
    def _one_page_target(self) -> "ListPaperQuestionsArgs":
        if self.offset is not None and self.continue_from_previous:
            raise ValueError("offset 与 continue_from_previous 只能选一个")
        return self


class SelectQuestionArgs(_Strict):
    index: int | None = Field(default=None, ge=1, le=64)
    number_label: str | None = Field(default=None, max_length=20)
    # F010 B：工作区短引用（Q1…Qn），与 index/number_label 互斥。
    ref: str | None = Field(default=None, pattern=r"^Q[1-9][0-9]{0,3}$")
    question_type: QuestionType | None = None
    paper_id: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def _one_target(self) -> "SelectQuestionArgs":
        chosen = [
            value is not None
            for value in (self.index, self.number_label, self.ref)
        ]
        if sum(chosen) > 1:
            raise ValueError("index、number_label、ref 只能填一个")
        return self


class ViewQuestionDetailArgs(_Strict):
    ref: str | None = Field(default=None, pattern=r"^Q[1-9][0-9]{0,3}$")


class ListShownQuestionsArgs(_Strict):
    question_type: QuestionType | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)


class ViewLearningSummaryArgs(_Strict):
    course: str | None = Field(default=None, max_length=40)
    days: int = Field(default=30, ge=1, le=365)
    limit: int = Field(default=20, ge=1, le=50)


class ExplainProblemArgs(_Strict):
    mode: Literal["new", "supplement", "correction"] = "new"
    course: str | None = Field(default=None, max_length=40)
    ref: str | None = Field(default=None, pattern=r"^Q[1-9][0-9]{0,3}$")
    scope: Literal["full", "hint", "topic"] = "full"
    # current=讲工作区里的题；new=讲本轮新输入/新题，避免误绑旧题卡。
    target: Literal["current", "new"] = "current"


class AnswerFollowupArgs(_Strict):
    question: str = Field(min_length=1, max_length=500)
    target: Literal["auto", "problem", "exercise"] = "auto"
    ref: str | None = Field(default=None, pattern=r"^Q[1-9][0-9]{0,3}$")


class SaveMistakeArgs(_Strict):
    user_feedback: str | None = Field(default=None, max_length=2000)
    basis: BasisArgs | None = None


class GenerateExerciseArgs(_Strict):
    from_question: bool = False
    ref: str | None = Field(default=None, pattern=r"^Q[1-9][0-9]{0,3}$")
    basis: BasisArgs | None = None


class AnswerRevealArgs(_Strict):
    """查看真题答案或练习参考解答：可带引用与依据。"""

    ref: str | None = Field(default=None, pattern=r"^Q[1-9][0-9]{0,3}$")
    basis: BasisArgs | None = None


class NoArgs(_Strict):
    """查看解答、查看真题答案、评价作答、保存错题都不接受参数。"""


class RecordFeedbackArgs(_Strict):
    understood: bool = True


class AskUserArgs(_Strict):
    question: str = Field(min_length=1, max_length=300)
    options: list[str] | None = Field(default=None, max_length=5)
    reason_code: Literal[
        "missing_course",
        "ambiguous_paper",
        "ambiguous_type",
        "ambiguous_suggestion",
        "reject_ack",
        "other",
    ] | None = None


class ReviewMemoryArgs(_Strict):
    course: str | None = Field(default=None, max_length=40)
    days: int = Field(default=30, ge=1, le=365)
    limit: int = Field(default=20, ge=1, le=50)
    # “这几条”只能由模型显式给出，不能再从原话里猜。
    entry_ids: list[str] = Field(default_factory=list, max_length=20)


class CorrectMemoryArgs(_Strict):
    entry_id: str = Field(min_length=6, max_length=80)
    course: str | None = Field(default=None, max_length=40)
    knowledge_point: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=500)
    basis: BasisArgs | None = None

    @model_validator(mode="after")
    def _require_change(self) -> "CorrectMemoryArgs":
        if all(
            value is None
            for value in (self.course, self.knowledge_point, self.note)
        ):
            raise ValueError("至少要说明要纠正的课程、知识点或补充说明")
        return self


class DeleteMemoryArgs(_Strict):
    entry_id: str = Field(min_length=6, max_length=80)
    reason: str | None = Field(default=None, max_length=200)
    basis: BasisArgs | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    action: str
    description: str
    args_model: type[BaseModel]
    schema: dict[str, Any]


def _object(
    properties: dict[str, Any],
    *,
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        payload["required"] = list(required)
    return payload


def _nullable(kind: str) -> dict[str, Any]:
    return {"type": [kind, "null"]}


def _basis_schema() -> dict[str, Any]:
    """可追溯依据的扁平 schema；授权仍由执行层独立判断。"""

    return {
        "type": ["object", "null"],
        "additionalProperties": False,
        "properties": {
            "kind": {
                "type": "string",
                "enum": [
                    "student_request",
                    "confirmed_suggestion",
                    "button",
                    "tool_result",
                ],
            },
            "quote": _nullable("string"),
            "suggestion_id": _nullable("string"),
            "depends_on_call_id": _nullable("string"),
        },
        "required": ["kind"],
    }


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="confirm_course",
        action="introduce",
        description=(
            "确认学生的复习课程并介绍真实可用的能力（查试卷、真题讲解、按需练习、"
            "错题整理）。学生说“我要复习 Python”这类宽泛需求时使用。"
        ),
        args_model=ConfirmCourseArgs,
        schema=_object(
            {"course": {"type": "string", "description": "学生所说的课程名，如 Python、高等数学、转专业"}},
            required=("course",),
        ),
    ),
    ToolSpec(
        name="query_papers",
        action="query_papers",
        description=(
            "按课程和考试类型查询本地登记的真实试卷，返回数量和下载入口。"
            "学生要求查看、下载试卷时使用。未提到期中/期末时不要填 exam_type。"
        ),
        args_model=QueryPapersArgs,
        schema=_object(
            {
                "course": _nullable("string"),
                "exam_type": {
                    "type": ["string", "null"],
                    "enum": ["midterm", "final", "other", None],
                    "description": "学生本轮明确要求的考试类型；未说明时留空",
                },
                "academic_year": _nullable("string"),
                "term": {
                    "type": ["string", "null"],
                    "enum": ["s1", "s2", None],
                },
                "variant": {
                    "type": ["string", "null"],
                    "enum": ["A", "B", None],
                },
                "paper_id": _nullable("string"),
                "all_exam_types": {
                    "type": "boolean",
                    "description": "学生明确不限期中/期末时置 true；不留空表示沿用条件",
                },
            }
        ),
    ),
    ToolSpec(
        name="search_questions",
        action="search_questions",
        description=(
            "在已审核真题库中按课程、考试类型、题型、题号或知识内容检索真题。"
            "指定了试卷时返回该条件下的题目（同卷同题型一次给全，并报告总数与本次范围）；"
            "没指定试卷时按相关度返回，最多 20 道。查题结果是中间事实：拿到结果后可以继续"
            "选题、讲解或直接回答学生的问题，不必一律让学生先作答。只有真正返回的卡片才算真题。"
        ),
        args_model=SearchQuestionsArgs,
        schema=_object(
            {
                "course": {"type": "string", "description": "课程名，如 Python语言程序设计"},
                "query": _nullable("string"),
                "exam_type": {
                    "type": ["string", "null"],
                    "enum": ["midterm", "final", "other", None],
                },
                "question_type": {
                    "type": ["string", "null"],
                    "enum": ["choice", "fill", "code", None],
                },
                "knowledge_points": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "maxItems": 5,
                },
                "number_label": _nullable("string"),
                "paper_id": {
                    "type": ["string", "null"],
                    "description": "已确定的试卷 ID；不确定时留空，程序会沿用当前试卷",
                },
                "offset": {
                    "type": ["integer", "null"],
                    "minimum": 0,
                    "description": "具体页码（从 0 起）。学生说“继续”时留空并置 continue_from_previous",
                },
                "continue_from_previous": {
                    "type": "boolean",
                    "description": "接上一页时置 true；必须与上一页的卷、题型和条件相同，不能与 offset 同时给",
                },
                "auto_explain": {
                    "type": "boolean",
                    "description": "仅当学生明确要求直接讲解或直接看答案时才为 true",
                },
                "scope": {
                    "type": "string",
                    "enum": ["current", "paper", "bank"],
                    "description": "current=当前卷，paper=指定卷，bank=不限试卷",
                },
                "exclude_question_id": _nullable("string"),
                "exclude_previous": {
                    "type": "boolean",
                    "description": "学生明确要“换一道/再来一道”时置 true，排除上一轮展示过的题",
                },
                "defer_answer": {
                    "type": "boolean",
                    "description": "学生明确要求先自己做、暂不看答案时为 true",
                },
            },
            required=("course",),
        ),
    ),
    ToolSpec(
        name="list_paper_questions",
        action="list_paper_questions",
        description=(
            "按整卷题型浏览题目：给定试卷和题型，返回这一部分题目的总数、本次范围和下一批位置。"
            "学生说“这张卷的选择题”“所有单选”“前五题”“继续看下一批”时使用；"
            "试卷没说明时沿用当前试卷。浏览是中间事实，学生要考点或讲解时可以继续处理。"
        ),
        args_model=ListPaperQuestionsArgs,
        schema=_object(
            {
                "course": _nullable("string"),
                "paper_id": {
                    "type": ["string", "null"],
                    "description": "试卷 ID；不填就用当前试卷",
                },
                "question_type": {
                    "type": ["string", "null"],
                    "enum": ["choice", "fill", "code", None],
                    "description": "题型；不填表示整卷所有题型",
                },
                "offset": {
                    "type": ["integer", "null"],
                    "minimum": 0,
                    "description": "从第几道开始（0 起）；具体页码与 continue_from_previous 二选一",
                },
                "continue_from_previous": {
                    "type": "boolean",
                    "description": "接上一页时置 true；沿用上一页的卷、题型和检索条件",
                },
                "limit": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "maximum": 64,
                },
            }
        ),
    ),
    ToolSpec(
        name="select_question",
        action="select_question",
        description=(
            "学生从刚才的查题结果中指定一道题时使用。只按学生明示的序号选择，"
            "不能替学生决定。学生说的是原卷题号时优先用 number_label。"
        ),
        args_model=SelectQuestionArgs,
        schema=_object(
            {
                "index": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "maximum": 64,
                    "description": "结果列表里从 1 开始的序号；“第 5 题”这类原卷题号用 number_label",
                },
                "number_label": _nullable("string"),
                "ref": {
                    "type": ["string", "null"],
                    "pattern": "^Q[1-9][0-9]{0,3}$",
                    "description": "工作区里的短引用（如 Q8），来自 view_workspace/list_shown_questions；与 index/number_label 三选一",
                },
                "question_type": {
                    "type": ["string", "null"],
                    "enum": ["choice", "fill", "code", None],
                },
                "paper_id": _nullable("string"),
            }
        ),
    ),
    ToolSpec(
        name="explain_problem",
        action="explain_problem",
        description=(
            "对当前题目进行讲解，或对当前题目补充题干、纠正题干。"
            "开始一道完全不同的新题时 mode=new；给尚未讲清的题补充信息用 supplement；"
            "纠正已讲解题目的题干用 correction。"
        ),
        args_model=ExplainProblemArgs,
        schema=_object(
            {
                "mode": {
                    "type": "string",
                    "enum": ["new", "supplement", "correction"],
                    "description": "本次输入与当前题目的关系",
                },
                "course": _nullable("string"),
                "ref": {
                    "type": ["string", "null"],
                    "pattern": "^Q[1-9][0-9]{0,3}$",
                },
                "scope": {
                    "type": "string",
                    "enum": ["full", "hint", "topic"],
                },
                "target": {
                    "type": "string",
                    "enum": ["current", "new"],
                    "description": "讲工作区当前题=current；讲本轮新题=new",
                },
            }
        ),
    ),
    ToolSpec(
        name="answer_followup",
        action="followup_problem",
        description="回答学生对母题或当前变式练习的追问。追问练习题或其解答时 target=exercise；明确问原题时 target=problem；不明确时 auto。",
        args_model=AnswerFollowupArgs,
        schema=_object(
            {
                "question": {
                    "type": "string",
                    "description": "把学生的追问原话整理后放进来",
                },
                "target": {"type": "string", "enum": ["auto", "problem", "exercise"]},
                "ref": {
                    "type": ["string", "null"],
                    "pattern": "^Q[1-9][0-9]{0,3}$",
                },
            },
            required=("question",),
        ),
    ),
    ToolSpec(
        name="generate_exercise",
        action="generate_exercise",
        description=(
            "学生明确要求练习时生成一道与当前题核心知识点相关的类似题。"
            "生成后必须等待学生作答，参考解答不能提前展示。"
        ),
        args_model=GenerateExerciseArgs,
        schema=_object(
            {
                "from_question": {
                    "type": "boolean",
                    "description": "从当前真题生成变式时为 true",
                },
                "ref": {
                    "type": ["string", "null"],
                    "pattern": "^Q[1-9][0-9]{0,3}$",
                },
                "basis": _basis_schema(),
            }
        ),
    ),
    ToolSpec(
        name="evaluate_exercise",
        action="evaluate_exercise",
        description="学生提交当前练习的答案或解题过程后，评价作答。不作答时不调用。",
        args_model=NoArgs,
        schema=_object({}),
    ),
    ToolSpec(
        name="reveal_solution",
        action="reveal_solution",
        description="学生主动要求查看当前练习的参考解答时使用。",
        args_model=AnswerRevealArgs,
        schema=_object(
            {
                "ref": {
                    "type": ["string", "null"],
                    "pattern": "^Q[1-9][0-9]{0,3}$",
                },
                "basis": _basis_schema(),
            }
        ),
    ),
    ToolSpec(
        name="show_question_answer",
        action="view_question_answer",
        description=(
            "学生明确要求查看当前已选中真题的答案或参考代码时使用。"
            "学生没有主动要求时不能调用。"
        ),
        args_model=AnswerRevealArgs,
        schema=_object(
            {
                "ref": {
                    "type": ["string", "null"],
                    "pattern": "^Q[1-9][0-9]{0,3}$",
                },
                "basis": _basis_schema(),
            }
        ),
    ),
    ToolSpec(
        name="save_mistake",
        action="record_mistake",
        description=(
            "把当前题目保存到错题本。只有学生明确要求保存、或确认了清晰的保存建议时才调用。"
        ),
        args_model=SaveMistakeArgs,
        schema=_object({
            "user_feedback": {
                "type": ["string", "null"], "maxLength": 2000,
                "description": "学生明确要求保存的备注原文；未要求备注时省略，不自行编写。",
            },
            "basis": _basis_schema(),
        }),
    ),
    ToolSpec(
        name="record_feedback",
        action="record_feedback",
        description=(
            "学生表示听懂了、明白了时记录本次自述理解。"
            "记录理解不等于出题，也不能代替保存错题。"
        ),
        args_model=RecordFeedbackArgs,
        schema=_object({}),
    ),
    ToolSpec(
        name="ask_user",
        action="clarify_or_reply",
        description=(
            "需要学生澄清、补充信息，或温和回应拒绝、寒暄时使用。"
            "多个建议之后学生只回答“好”而无法判断指向时，必须用本工具澄清。"
        ),
        args_model=AskUserArgs,
        schema=_object(
            {
                "question": {"type": "string", "description": "要问学生的话"},
                "options": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "maxItems": 5,
                },
                "reason_code": {
                    "type": ["string", "null"],
                    "enum": [
                        "missing_course",
                        "ambiguous_paper",
                        "ambiguous_type",
                        "ambiguous_suggestion",
                        "reject_ack",
                        "other",
                        None,
                    ],
                },
            },
            required=("question",),
        ),
    ),
    ToolSpec(
        name="review_learning_memory",
        action="review_memory",
        description=(
            "查看学生自己的学习记忆并给出有依据的复习建议：只统计真实练习与作答记录，"
            "按知识点汇总“独立作答被判错 / 看过解答后作答 / 过程不足 / 还没作答 / 自述理解”，"
            "并返回可追溯的条目编号。学生问“先补什么”“最近学得怎么样”“从哪开始复习”时使用。"
            "不给掌握率，不把一次答对说成已掌握；没有记录就直说没有。"
        ),
        args_model=ReviewMemoryArgs,
        schema=_object(
            {
                "course": {
                    "type": ["string", "null"],
                    "description": "课程名；不填就用当前会话已确认的课程",
                },
                "days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 365,
                    "description": "回看多少天，默认 30",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "最多返回多少条证据，默认 20",
                },
                "entry_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "学生明确指到具体记忆条目时填写这些编号；否则留空",
                },
            }
        ),
    ),
    ToolSpec(
        name="correct_learning_memory",
        action="correct_memory",
        description=(
            "学生指出学习记忆里的课程归属、知识点或描述不对时，纠正指定条目。"
            "必须传 review_learning_memory 返回的 entry_id；至少给出课程、知识点、"
            "补充说明中的一项。只改记忆，不改原会话记录。"
        ),
        args_model=CorrectMemoryArgs,
        schema=_object(
            {
                "entry_id": {
                    "type": "string",
                    "description": "形如 exercise:<uuid> 的条目编号，来自学习记忆结果",
                },
                "course": {
                    "type": ["string", "null"],
                    "description": "纠正后的课程名",
                },
                "knowledge_point": {
                    "type": ["string", "null"],
                    "description": "纠正后的知识点（只写一个）",
                },
                "note": {
                    "type": ["string", "null"],
                    "description": "学生补充的说明，如“这题我当时看错条件了”",
                },
                "basis": _basis_schema(),
            },
            required=("entry_id",),
        ),
    ),
    ToolSpec(
        name="delete_learning_memory",
        action="delete_memory",
        description=(
            "学生明确要求把某条学习记忆从复习建议里移除时使用。必须传 entry_id。"
            "移除只影响学习记忆和复习建议，原会话、练习与错题记录都保留；"
            "说明时必须原样告诉学生这一点。"
        ),
        args_model=DeleteMemoryArgs,
        schema=_object(
            {
                "entry_id": {
                    "type": "string",
                    "description": "要移除的条目编号，来自学习记忆结果",
                },
                "reason": {
                    "type": ["string", "null"],
                    "description": "学生给出的原因原文；没说明就留空",
                },
                "basis": _basis_schema(),
            },
            required=("entry_id",),
        ),
    ),
    ToolSpec(
        name="view_workspace",
        action="view_workspace",
        description=(
            "读取当前复习工作区：已确认课程、当前试卷与浏览位置、当前选中题、"
            "本会话展示过的题及短引用（Q1…Qn）、当前练习与待确认事项。"
            "学生提到“刚才那张卷”“之前那道题”“接着看”时先调用它，再决定下一步。"
        ),
        args_model=NoArgs,
        schema=_object({}),
    ),
    ToolSpec(
        name="view_question_detail",
        action="view_question_detail",
        description=(
            "按工作区短引用读取一道已展示真题的公开题面与版本信息（题干、选项、"
            "知识点、来源卷、答案是否可用）。只读：不展示答案正文，不改变选中状态。"
            "学生问“第 5 题考什么”“刚才那道题题干是什么”时使用。"
        ),
        args_model=ViewQuestionDetailArgs,
        schema=_object(
            {
                "ref": {
                    "type": ["string", "null"],
                    "pattern": "^Q[1-9][0-9]{0,3}$",
                    "description": "短引用，如 Q8；不填表示当前选中题",
                }
            }
        ),
    ),
    ToolSpec(
        name="list_shown_questions",
        action="list_shown_questions",
        description=(
            "列出本会话已经展示过的题目（分页），可按题型筛选。用于回答“刚才列了哪些题”"
            "或找回某道题的短引用；只返回公开卡片信息，不含答案。"
        ),
        args_model=ListShownQuestionsArgs,
        schema=_object(
            {
                "question_type": {
                    "type": ["string", "null"],
                    "enum": ["choice", "fill", "code", None],
                },
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            }
        ),
    ),
    ToolSpec(
        name="view_learning_summary",
        action="view_learning_summary",
        description=(
            "只读查看学习记忆摘要（练习与作答记录、待补知识点）。学生问“我最近学得怎么样”"
            "“先补什么”时使用。与 review_learning_memory 不同：本工具不切换当前课程、"
            "不改会话上下文。"
        ),
        args_model=ViewLearningSummaryArgs,
        schema=_object(
            {
                "course": {
                    "type": ["string", "null"],
                    "description": "要查看的课程；不填就用当前会话课程",
                },
                "days": {"type": "integer", "minimum": 1, "maximum": 365},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            }
        ),
    ),
)


TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}
ACTION_TO_TOOL: dict[str, str] = {spec.action: spec.name for spec in TOOL_SPECS}


def openai_tools() -> list[dict[str, Any]]:
    """转成 bind_tools/OpenAI function-calling 需要的工具定义。"""

    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.schema,
            },
        }
        for spec in TOOL_SPECS
    ]


@dataclass(frozen=True)
class ToolRejection:
    """模型请求了不可执行的工具调用；仍要按 tool_call_id 回传一条结果。"""

    tool_call_id: str
    tool: str
    action: str
    code: Literal["UNKNOWN_TOOL", "INVALID_ARGUMENTS", "TOO_MANY_CALLS"]
    message: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": "failed",
            "tool": self.tool,
            "error_code": self.code,
            "message": self.message,
            "needs_user": False,
        }

    def to_observation(self) -> dict[str, Any]:
        return {
            "action": self.action or self.tool,
            "status": "failed",
            "message": self.message,
            "details": {
                "tool": self.tool,
                "error_code": self.code,
                "retryable": False,
                "rejected": True,
            },
            "needs_user": False,
            "executed": False,
        }


def _format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for item in exc.errors()[:3]:
        location = ".".join(str(piece) for piece in item.get("loc", ())) or "参数"
        parts.append(f"{location}：{item.get('msg', '不合法')}")
    return "；".join(parts)


def parse_tool_call(
    tool: str, raw_args: Any, *, tool_call_id: str
) -> tuple[ActionPlan | None, ToolRejection | None]:
    """把一次原生工具调用规范化成内部 ActionPlan。

    参数非法和未知工具都返回拒绝对象，由调用方按 tool_call_id 回传结果，
    并计入本轮执行预算。
    """

    spec = TOOLS_BY_NAME.get(tool)
    if spec is None:
        return None, ToolRejection(
            tool_call_id=tool_call_id,
            tool=tool,
            # 用 tool:<name> 而不是内部动作名：拒绝不是“已执行过该动作”，
            # 用内部名会让修正后的重试被单次动作规则误伤。
            action=f"tool:{tool}",
            code="UNKNOWN_TOOL",
            message=f"不存在名为“{tool}”的工具，请改用已提供的工具。",
        )

    if isinstance(raw_args, str):
        try:
            payload: Any = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError:
            payload = None
    else:
        payload = raw_args

    if not isinstance(payload, dict):
        return None, ToolRejection(
            tool_call_id=tool_call_id,
            tool=tool,
            action=f"tool:{tool}",
            code="INVALID_ARGUMENTS",
            message=f"“{tool}”的参数不是合法的 JSON 对象，本次调用没有执行。",
        )

    try:
        parsed = spec.args_model.model_validate(payload)
    except ValidationError as exc:
        return None, ToolRejection(
            tool_call_id=tool_call_id,
            tool=tool,
            action=f"tool:{tool}",
            code="INVALID_ARGUMENTS",
            message=(
                f"“{tool}”的参数不符合要求（{_format_validation_error(exc)}），"
                "本次调用没有执行。"
            ),
        )

    parameters = parsed.model_dump(mode="json", exclude_none=True)
    return (
        ActionPlan(
            action=spec.action,  # type: ignore[arg-type]
            parameters=parameters,
            reason="",
            tool_call_id=tool_call_id,
            tool=tool,
        ),
        None,
    )


def public_details(details: dict[str, Any] | None) -> dict[str, Any]:
    """过滤成可进入最终回复材料的字段；未登记的键一律丢弃。"""

    if not details:
        return {}
    return {key: value for key, value in details.items() if key in _PUBLIC_DETAIL_KEYS}


def tool_label(tool: str | None) -> str:
    """给日志和观测用的中文标签。"""

    if not tool:
        return "未知工具"
    spec = TOOLS_BY_NAME.get(tool)
    if spec is None:
        return tool
    return {
        "confirm_course": "确认课程",
        "query_papers": "查找试卷",
        "search_questions": "查找真题",
        "list_paper_questions": "浏览整卷题目",
        "select_question": "选择真题",
        "explain_problem": "题目讲解",
        "answer_followup": "回答追问",
        "generate_exercise": "生成练习",
        "evaluate_exercise": "评价作答",
        "reveal_solution": "查看解答",
        "show_question_answer": "查看真题答案",
        "save_mistake": "保存错题",
        "record_feedback": "记录反馈",
        "ask_user": "澄清提问",
        "review_learning_memory": "查看学习记忆",
        "correct_learning_memory": "纠正学习记忆",
        "delete_learning_memory": "移除学习记忆",
        "view_workspace": "读取工作区",
        "view_question_detail": "查看题目详情",
        "list_shown_questions": "列出已展示题目",
        "view_learning_summary": "查看学习摘要",
    }.get(tool, tool)
