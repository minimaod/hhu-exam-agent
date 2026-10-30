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

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
    }
)


class _Strict(BaseModel):
    """所有工具参数都拒绝多余字段，防止模型夹带未定义参数。"""

    model_config = ConfigDict(extra="forbid")


class ConfirmCourseArgs(_Strict):
    course: str = Field(min_length=1, max_length=40)


class QueryPapersArgs(_Strict):
    course: str | None = Field(default=None, max_length=40)
    exam_type: ExamType | None = None


class SearchQuestionsArgs(_Strict):
    course: str = Field(min_length=1, max_length=40)
    query: str | None = Field(default=None, max_length=200)
    exam_type: ExamType | None = None
    question_type: QuestionType | None = None
    knowledge_points: list[str] | None = Field(default=None, max_length=5)
    number_label: str | None = Field(default=None, max_length=20)
    paper_id: str | None = Field(default=None, max_length=80)
    offset: int = Field(default=0, ge=0)
    auto_explain: bool = False


class ListPaperQuestionsArgs(_Strict):
    course: str | None = Field(default=None, max_length=40)
    paper_id: str | None = Field(default=None, max_length=80)
    question_type: QuestionType | None = None
    offset: int | None = Field(default=None, ge=0)


class SelectQuestionArgs(_Strict):
    index: int | None = Field(default=None, ge=1, le=64)
    number_label: str | None = Field(default=None, max_length=20)


class ExplainProblemArgs(_Strict):
    mode: Literal["new", "supplement", "correction"] = "new"
    course: str | None = Field(default=None, max_length=40)


class AnswerFollowupArgs(_Strict):
    question: str = Field(min_length=1, max_length=500)


class GenerateExerciseArgs(_Strict):
    from_question: bool = False


class NoArgs(_Strict):
    """查看解答、查看真题答案、评价作答、保存错题都不接受参数。"""


class RecordFeedbackArgs(_Strict):
    understood: bool = True


class AskUserArgs(_Strict):
    question: str = Field(min_length=1, max_length=300)
    options: list[str] | None = Field(default=None, max_length=5)


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
                    "type": "integer",
                    "minimum": 0,
                    "description": "已展示到哪里；学生说“继续”且程序没有自动推进时才需要填",
                },
                "auto_explain": {
                    "type": "boolean",
                    "description": "仅当学生明确要求直接讲解或直接看答案时才为 true",
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
                    "description": "从第几道开始（0 起）；“继续”时留空由程序接上一次的位置",
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
            }
        ),
    ),
    ToolSpec(
        name="answer_followup",
        action="followup_problem",
        description="回答学生针对当前已讲解题目提出的追问。",
        args_model=AnswerFollowupArgs,
        schema=_object(
            {
                "question": {
                    "type": "string",
                    "description": "把学生的追问原话整理后放进来",
                }
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
                }
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
        args_model=NoArgs,
        schema=_object({}),
    ),
    ToolSpec(
        name="show_question_answer",
        action="view_question_answer",
        description=(
            "学生明确要求查看当前已选中真题的答案或参考代码时使用。"
            "学生没有主动要求时不能调用。"
        ),
        args_model=NoArgs,
        schema=_object({}),
    ),
    ToolSpec(
        name="save_mistake",
        action="record_mistake",
        description=(
            "把当前题目保存到错题本。只有学生明确要求保存、或确认了清晰的保存建议时才调用。"
        ),
        args_model=NoArgs,
        schema=_object({}),
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
            },
            required=("question",),
        ),
    ),
)

TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}


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
    }.get(tool, tool)