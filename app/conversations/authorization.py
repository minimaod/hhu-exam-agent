"""F010 D：受保护动作的独立授权。

授权只看**本轮学生原话**和本轮已经提交的事实，不看模型填的参数，也不看
``basis``。``basis`` 只负责追溯；即使引用了一句真实存在的话，只要那句话
是否定/假设/转述，这里仍然拒绝。

拒绝分两类：
- ``needs_user=False``：参数或引用可改，模型可以在同一轮换参数重试；
- ``needs_user=True``：目标本身有歧义，需要学生回答后才能继续。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.conversations.basis import PROTECTED_ACTIONS

_CLAUSE_SPLIT = re.compile(r"[，,。！？?!；;\n]+")

_NEGATION = re.compile(
    r"(别|不要|不用|不需要|不必|先不|暂不|取消|停止|不想|算了|别再|先别|不用给|不要给|不给)"
)

_SAVE = re.compile(
    r"(保存|存起来|存一下|存到|存进|存入|"
    r"(?:添加|加|收|记|放|存)(?:到|进|入)错题(?:本|集|册|里)?|"
    r"加入错题|记错题|收录(?:到|进)?错题|收藏|记下来|收进错题本|"
    r"留着复习|留个?档|记到?本子|放进?本子|备个?忘)"
)
# “练习”单独出现不等于要出新题：例如“查看上次练习的结果”。只有明确的
# 出题/练习动作才算请求；裸词“练习”不再授权。
_EXERCISE = re.compile(
    r"(再来一道|来一道|来道|出道题|出道|出一道|出个题|出几道|出题|自己出一?道|自主出题|"
    r"出练习|练一道|练一下|练练|"
    r"练习一下|再练习|再练|想练|要练|类似题|类似的?题|同类型|变式|做题|刷题|再做一道|"
    r"考考我|再做个|弄道.*题)"
)
_ANSWER = re.compile(
    r"(答案|参考答案|参考代码|参考解答|看答案|告诉我答案|完整解法|看解答|标准答案|正确答案)"
)
_DELETE = re.compile(r"(删除|删掉|删了|移除|去掉|不要这条|别算|不算|不参与|别记)")
_CORRECT = re.compile(r"(改一下|改成|纠正|更正|记错|修改|更新)")

# 这些是“问保存/删除本身会怎样”的元问题、状态询问或回忆，不是操作请求。
_SAVE_META = re.compile(
    r"(会发生|会怎样|会怎么样|有什么用|是什么意思|是什么|什么时候|为什么|"
    r"是否|如果|假如|假设|想知道|问一下|可以吗|行吗|好吗|"
    r"(?:保存|加入|添加|存|记|放|收|录)[^。！？?]{0,12}(?:了吗|过了吗|过吗|成功了吗)|"
    r"有没有[^。！？?]{0,10}(?:保存|加入|添加|存|记|放|收|录)|已经[^。！？?]{0,10}(?:保存|加入|添加|存|记|放|收|录))"
)
# 否定与假设/状态问句黑名单：任何受保护动作若整句处于单纯问状态/假设，一律一票否决。
_HYPOTHETICAL_OR_STATUS = re.compile(
    r"(如果|假如|假设|要是).{0,16}(保存|加入|出题|看答案|解答|记录|删除)"
    r"|(?:保存|加入|添加|存|记|放|收|录)[^。！？?]{0,12}(?:了吗|过了吗|过吗|成功了吗)"
    r"|有没有[^。！？?]{0,10}(?:保存|加入|添加|存|记|放|收|录)"
)
_FEEDBACK_ONLY_RE = re.compile(
    r"^(?:这题|这道题|这下|现在)?(?:我)?(?:终于|大概|基本)?(?:懂了|明白了|理解了|清楚了|会了|学会了|掌握了)[！!。.~～]*$"
)

_AFFIRMATIVE_SAVE = re.compile(
    r"^(?:好(?:的|呀|啊|吧)?|可以(?:呀|啊|吧)?|行(?:啊|呀|吧)?|嗯+|"
    r"是(?:的|啊)?|对|确认|保存吧|加吧|存吧|记下来吧|收进去吧|麻烦了|拜托了)[！!。.~～]*$"
)


def is_save_affirmative(text: str) -> bool:
    """判断用户输入是否为简短、不含否定或疑问的肯定确认。"""

    cleaned = str(text or "").strip()
    if not cleaned or _NEGATION.search(cleaned) or _SAVE_META.search(cleaned):
        return False
    return bool(_AFFIRMATIVE_SAVE.match(cleaned))
_RECALL_META = re.compile(
    r"(什么时候|何时|为什么|是不是|有没有|删除的|删掉的|删除过|修改的|改过的)"
)


@dataclass(frozen=True)
class AuthDecision:
    code: str
    message: str
    needs_user: bool = False
    remedies: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)


def _negated(text: str, match: re.Match[str]) -> bool:
    clause_start = 0
    for sep in _CLAUSE_SPLIT.finditer(text[: match.start()]):
        clause_start = sep.end()
    prefix = text[clause_start : match.start()]
    return bool(_NEGATION.search(prefix))


def _requested(pattern: re.Pattern[str], text: str) -> bool:
    return any(not _negated(text, match) for match in pattern.finditer(text or ""))


def requested_question_types(text: str) -> set[str]:
    """学生明确要求的题型集合，仅作目标依据核验。"""
    labels = {
        "choice": r"选择题|单选题|多选题",
        "fill": r"填空题?",
        "code": r"编程题|代码题|程序设计题",
        "solve": r"计算题|解答题|应用题|综合题|大题",
        "proof": r"证明题",
        "judge": r"判断题|是非题",
    }
    return {kind for kind, pattern in labels.items() if _requested(re.compile(pattern), text)}


def requested_question_type(text: str) -> str | None:
    """核对用户的选型依据，模型填入的题型本身不能消除同号歧义。"""
    requested = requested_question_types(text)
    return next(iter(requested)) if len(requested) == 1 else None


def requested_question_ref(text: str, ref: str) -> bool:
    """仅学生明确点名的短引用可当作选题依据。"""

    pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(ref) + r"(?![0-9])", re.IGNORECASE)
    return _requested(pattern, text)


def _step_action(step: dict[str, Any]) -> str:
    plan = step.get("plan")
    if isinstance(plan, dict):
        return str(plan.get("action") or "")
    return str(step.get("action") or "")


def _step_status(step: dict[str, Any]) -> str:
    outcome = step.get("outcome")
    if isinstance(outcome, dict):
        return str(outcome.get("status") or "")
    return ""


def _succeeded(transcript: list[dict[str, Any]], action: str) -> bool:
    return any(
        isinstance(step, dict)
        and _step_action(step) == action
        and _step_status(step) == "success"
        for step in transcript
    )


def _deny(
    action: str,
    code: str,
    message: str,
    remedies: list[str],
    *,
    needs_user: bool = False,
) -> AuthDecision:
    return AuthDecision(
        code=code,
        message=message,
        needs_user=needs_user,
        remedies=remedies,
        facts={"action": action},
    )


_REFUSAL = re.compile(r"(不用|不需要|算了|不要|别|取消|先不)")
_META = re.compile(
    r"(会发生|会怎样|有什么用|是什么意思|是什么|什么时候|为什么|是否|"
    r"如果|假如|假设|想知道|问一下|可以吗|行吗|好吗|查看|结果|记录|历史|上次)"
)


def needs_user_for_denial(action: str, text: str, default: bool) -> bool:
    """拒绝写操作时是否先追问，而不是直接报业务失败。

    只服务于授权结果，不决定下一步调用哪个工具；没有明确请求时先问一句，
    学生已拒绝或在问元问题时直接失败。
    """

    if default or not text:
        return default
    if action not in {"record_mistake", "generate_exercise"}:
        return default
    return not _REFUSAL.search(text) and not _META.search(text)


SemanticAuthOutcome = Literal["explicit_request", "clarify_needed", "not_requested"]


class SemanticAuthVerifier(Protocol):
    """独立语义授权复核接口，用于消除固定白名单词表误拒自然请求。"""

    async def verify(
        self,
        *,
        action: str,
        user_text: str,
        quote: str | None,
        suggestions: list[str],
        last_assistant_reply: str | None = None,
    ) -> SemanticAuthOutcome: ...


_ACTION_INTENT_DESCRIPTIONS = {
    "record_mistake": "把当前题目或错题写入/收藏/记录到错题本（含自然表达如“留着考前翻”“收进小本本”“记到我的本子上”等）。",
    "generate_exercise": "要求 AI 当场再出/生成一道练习题或同类型巩固题给学生做。",
    "view_question_answer": "明确要求直接展示/揭晓当前题目的完整答案、参考解答或参考代码（注意：只要提示/只看考点不算）。",
    "reveal_solution": "明确要求直接查看当前练习题的参考解答或标准答案。",
}

_AUTH_VERIFY_PROMPT = """\
你是权限安全复核器。请严格判断“学生本轮原话”结合最近一轮建议，是否明确请求执行目标操作。

只输出一个 JSON 对象：{"verdict": "explicit_request" | "clarify_needed" | "not_requested"}

规则：
1. explicit_request：学生在明确请求或同意执行该操作（哪怕换用自然口语，如“放进我本子里方便考前翻”“这题再弄个差不多的给我做做”“行吧弄一下”且上轮确有对应单建议）。
2. clarify_needed：表达模糊、缺少明确对象（如只说“处理一下”“那行”且存在多种可能，或不确定是否真想保存/出题/看答案）。
3. not_requested：只是感慨/评论（“这题好难”）、仅表示懂了（“我懂了”）、询问状态（“保存了吗？”）、假设提问（“如果保存会怎样？”）、明确拒绝或只问提示/考点。
"""


class DeepSeekAuthVerifier:
    """使用独立轻量模型实例复核未命中快速词表但具备合法引用依据的自然请求。"""

    def __init__(
        self,
        settings: Any,
        *,
        http_async_client: Any | None = None,
    ) -> None:
        self._settings = settings
        self._http_async_client = http_async_client

    async def verify(
        self,
        *,
        action: str,
        user_text: str,
        quote: str | None,
        suggestions: list[str],
        last_assistant_reply: str | None = None,
    ) -> SemanticAuthOutcome:
        from app.llm import build_chat_model, response_text

        goal = _ACTION_INTENT_DESCRIPTIONS.get(action, action)
        sug_text = "、".join(suggestions[:4]) if suggestions else "（无）"
        reply_snip = (last_assistant_reply or "").strip()[-240:] or "（无）"
        prompt = (
            f"目标操作：{action}（{goal}）\n"
            f"上一轮建议标签：{sug_text}\n"
            f"上一轮助手末尾话语：{reply_snip}\n"
            f"模型引用的原话片段：{quote or '（未提供）'}\n"
            f"学生本轮完整原话：{user_text}\n"
        )
        try:
            model = build_chat_model(
                self._settings,
                max_tokens=96,
                timeout=15.0,
                json_mode=True,
                http_async_client=self._http_async_client,
            )
            response = await model.ainvoke(
                [("system", _AUTH_VERIFY_PROMPT), ("human", prompt)]
            )
            raw = response_text(response)
            data = json.loads(raw)
            verdict = str(data.get("verdict") or "").strip()
            if verdict in {"explicit_request", "clarify_needed", "not_requested"}:
                return verdict  # type: ignore[return-value]
        except Exception:
            # 语义复核失败时必须保守拒绝，绝不默认放行写入。
            return "not_requested"
        return "not_requested"


def authorize_action(
    action: str,
    text: str,
    *,
    transcript: list[dict[str, Any]],
    entry_id: str | None = None,
    confirmable_save: bool = False,
    allow_semantic_fallback: bool = False,
    has_valid_basis: bool = False,
) -> AuthDecision | None:
    """返回拒绝决定；``None`` 表示本轮原话足以授权。

    安全分层：
    1. 否定、假设/状态元问题、纯“我懂了”等黑名单一律直接拦截（一票否决）；
    2. 命中明确快通道词表或单建议肯定确认直接放行；
    3. 未命中快通道但具备真实引用依据或明确文本信号时，可交由独立语义复核器裁决。
    """

    if action not in PROTECTED_ACTIONS:
        return None
    current = (text or "").strip()

    # 绝对黑名单：空文本、纯理解反馈、假设与状态元问题永远不放行写入或语义放行。
    if not current:
        return _deny(
            action,
            "AUTHORIZATION_REQUIRED",
            "这轮没有可授权的操作请求，我不会执行写入。",
            ["ask_user"],
        )

    if action == "record_mistake":
        if confirmable_save and is_save_affirmative(current):
            return None
        if _SAVE_META.search(current) or _HYPOTHETICAL_OR_STATUS.search(current):
            return _deny(
                action,
                "AUTHORIZATION_REQUIRED",
                "这轮你没有明确说要保存错题，我不会写入错题本。要保存的话，直接告诉我。",
                ["ask_user"],
            )
        if _requested(_SAVE, current):
            return None
        if (
            allow_semantic_fallback
            and has_valid_basis
            and not _NEGATION.search(current)
            and not _FEEDBACK_ONLY_RE.match(current)
        ):
            return _deny(
                action,
                "SEMANTIC_VERIFICATION_NEEDED",
                "这轮你没有明确说要保存错题，我不会写入错题本。要保存的话，直接告诉我。",
                ["ask_user"],
            )
        return _deny(
            action,
            "AUTHORIZATION_REQUIRED",
            "这轮你没有明确说要保存错题，我不会写入错题本。要保存的话，直接告诉我。",
            ["ask_user"],
        )

    if action == "generate_exercise":
        if _HYPOTHETICAL_OR_STATUS.search(current):
            return _deny(
                action,
                "AUTHORIZATION_REQUIRED",
                "这轮你没有要练习，我不会出题。要练一道的话，直接告诉我。",
                ["ask_user", "explain_problem"],
            )
        if _requested(_EXERCISE, current):
            return None
        if (
            allow_semantic_fallback
            and has_valid_basis
            and not _NEGATION.search(current)
            and not _FEEDBACK_ONLY_RE.match(current)
        ):
            return _deny(
                action,
                "SEMANTIC_VERIFICATION_NEEDED",
                "这轮你没有要练习，我不会出题。要练一道的话，直接告诉我。",
                ["ask_user", "explain_problem"],
            )
        return _deny(
            action,
            "AUTHORIZATION_REQUIRED",
            "这轮你没有要练习，我不会出题。要练一道的话，直接告诉我。",
            ["ask_user", "explain_problem"],
        )

    if action in {"view_question_answer", "reveal_solution"}:
        if action == "reveal_solution" and _succeeded(transcript, "evaluate_exercise"):
            return None
        if _HYPOTHETICAL_OR_STATUS.search(current):
            return _deny(
                action,
                "ANSWER_NOT_REQUESTED",
                "这轮你没有要看答案或完整解法，我不会公开答案。",
                ["explain_problem", "ask_user"],
            )
        if _requested(_ANSWER, current):
            return None
        if (
            allow_semantic_fallback
            and has_valid_basis
            and not _NEGATION.search(current)
            and not _FEEDBACK_ONLY_RE.match(current)
        ):
            return _deny(
                action,
                "SEMANTIC_VERIFICATION_NEEDED",
                "这轮你没有要看答案或完整解法，我不会公开答案。",
                ["explain_problem", "ask_user"],
            )
        return _deny(
            action,
            "ANSWER_NOT_REQUESTED",
            "这轮你没有要看答案或完整解法，我不会公开答案。",
            ["explain_problem", "ask_user"],
        )

    if action == "delete_memory":
        if not _requested(_DELETE, current) or _RECALL_META.search(current):
            return _deny(
                action,
                "AUTHORIZATION_REQUIRED",
                "要把这条从学习记忆和复习建议里移除吗？原会话记录会保留。",
                ["review_learning_memory", "ask_user"],
                needs_user=True,
            )
        return None

    if action == "correct_memory":
        if not _requested(_CORRECT, current) or _RECALL_META.search(current):
            return _deny(
                action,
                "AUTHORIZATION_REQUIRED",
                "这轮你没有明确要纠正这条学习记忆，我不会改写。",
                ["review_learning_memory", "ask_user"],
            )
        return None

    return None


__all__ = [
    "AuthDecision",
    "DeepSeekAuthVerifier",
    "SemanticAuthOutcome",
    "SemanticAuthVerifier",
    "authorize_action",
    "is_save_affirmative",
    "needs_user_for_denial",
]
