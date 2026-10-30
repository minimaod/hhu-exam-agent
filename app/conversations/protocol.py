"""F008：调度协议的基础数据类型。

单独抽出 ``ActionPlan``，是为了让“工具契约模块”（tools）和“有界循环模块”
（graph）都能引用它而不形成循环导入：

    protocol  ←  tools  ←  graph

``ActionPlan`` 是内部规范化动作，字段与 F006 的恢复检查点保持兼容；新增的
``tool_call_id`` / ``tool`` 只用于原生工具协议关联和日志，不参与去重判断。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ActionType = Literal[
    "introduce",
    "query_papers",
    "explain_problem",
    "followup_problem",
    "generate_exercise",
    "evaluate_exercise",
    "reveal_solution",
    "record_mistake",
    "record_feedback",
    "search_questions",
    "list_paper_questions",
    "select_question",
    "view_question_answer",
    "clarify_or_reply",
]

ActionStatus = Literal["success", "failed", "clarification"]


class ActionPlan(BaseModel):
    """一个待执行的业务动作。

    - ``action``/``parameters`` 是 F006 以来稳定的内部契约，恢复检查点按此保存；
    - ``tool_call_id`` 只在原生工具协议里把结果回传给模型，不作为幂等键；
    - ``tool`` 保存模型实际请求的工具名，便于日志和失败诊断。
    """

    model_config = ConfigDict(extra="forbid")

    action: ActionType
    parameters: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=200)
    tool_call_id: str | None = None
    tool: str | None = None