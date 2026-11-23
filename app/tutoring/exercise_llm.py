"""DeepSeek 练习题生成与学生作答评价模型适配器。

职责边界：
- 根据原题与知识点生成结构化类似题与参考解答；
- 对学生的作答（文字及图片过程）进行客观结构化评价；
- 遵循防泄露约定：参考解答由服务层仅存数据库，不在此处做外部事件分发。
"""

import base64
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.config import DeepSeekSettings
from app.llm import (
    build_chat_model,
    log_usage,
    map_provider_error,
    response_text,
)
from app.querying.errors import QueryError
from app.tutoring.models import ImagePayload
from app.tutoring.prompts import parse_json_object


def _image_block(image: ImagePayload) -> dict[str, Any]:
    encoded = base64.b64encode(image.data).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {
            "url": f"data:{image.mime};base64,{encoded}",
            "detail": "high",
        },
    }


class GeneratedExercise(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1, max_length=4000)
    knowledge_point: str = Field(min_length=1, max_length=120)
    reference_solution: str = Field(min_length=1, max_length=4000)


class AnswerEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_correct: bool | None = None
    final_answer_assessment: str = Field(min_length=1, max_length=1200)
    step_by_step_analysis: str = Field(default="", max_length=3000)
    guidance: str = Field(min_length=1, max_length=2000)
    needs_supplement: bool = False
    # 本次作答是否在参考解答展示之后完成；答案正确也不等于独立掌握。
    independence_assessment: Literal["independent", "after_reveal", "unclear"] | None = None
    independence_note: str | None = Field(default=None, max_length=800)

    @field_validator("step_by_step_analysis", mode="before")
    @classmethod
    def _coerce_none_analysis(cls, value: Any) -> Any:
        return "" if value is None else value


EXERCISE_GENERATION_PROMPT = """\
你是河海大学考试复习助手。请根据提供的原题、核心知识点和步骤，生成一道核心考点相同、题型相近但具体数值、条件、操作或背景有明显变化的“类似练习题”。

要求：
1. 题干必须完整清晰，表述严谨，符号准确。
2. 必须提供详尽无误的标准参考解答（步骤清晰、结果明确）。
3. 必须避开“近期已生成的题目”：不能只是换个标点或重复同一组数字与条件；优先改变数值、条件、操作顺序或情境。
4. 若已提供“避免重复的题干”，本次生成必须与它实质不同。
5. 严禁虚构年份、学校、学期或“真题来源”。
6. 请只输出一个 JSON 对象，不要包含 Markdown 标记或多余文字。格式：
{
  "statement": "练习题完整题干",
  "knowledge_point": "考查的核心知识点名称",
  "reference_solution": "练习题完整参考解答与关键步骤"
}
"""

STANDALONE_EXERCISE_PROMPT = """\
你是河海大学考试复习助手。当前没有对应真题原题，请严格围绕学生指定的复习课程和考点，独立设计一道难度适中、适合大学期末复习的自测练习题（非历年真题）。

要求：
1. 题干必须完整清晰、条件充足、表述严谨，符号规范。
2. 必须提供详尽无误的标准参考解答（关键推导清晰、结果明确）。
3. 严禁在题干中虚构任何学校、年份、学期、试卷名或“历年真题”字样。
4. 必须避开“近期已生成的题目”与“避免重复的题干”，保证题目具备实质变化。
5. 请只输出一个 JSON 对象，不要包含 Markdown 标记或多余文字。格式：
{
  "statement": "练习题完整题干",
  "knowledge_point": "考查的核心知识点名称",
  "reference_solution": "练习题完整参考解答与关键步骤"
}
"""

ANSWER_EVALUATION_PROMPT = """\
你是河海大学考试复习助手。请对比练习题干和标准参考解答，对学生的作答（包括最终答案以及选填的文字步骤或过程图片）进行客观、准确的评价。

评判规则：
1. 解答过程为选填项：当学生提交了明确的最终答案（即使未写文字过程也未上传过程图片）时，请直接对比标准参考解答判定最终答案对错，将 is_correct 设为 true 或 false，且必须将 needs_supplement 设为 false。严禁因为未填写选填的解题过程就把 needs_supplement 设为 true 或要求学生补写过程。
2. 仅在确实无法判别时设 needs_supplement 为 true：只有当学生上传了过程/手写图片但画面模糊、残缺、公式无法辨识，或者学生既没有提交明确的最终答案、过程也不足以判断结果时，才将 needs_supplement 设为 true、is_correct 设为 null，并在 guidance 中温和提示补充。
3. 若学生提供了文字解题过程或过程图片：在 step_by_step_analysis 中逐层评价其过程是否规范、推理是否严密，明确指出正确之处或具体错误步骤；不要在未提供过程时猜测错误原因。
4. 若学生未提供解题过程（既无文字过程也无过程图片）：step_by_step_analysis 必须输出空字符串 ""，不要写“未提供完整过程，暂无法定位具体错误步骤”等占位套话；如答案有误，只需在 final_answer_assessment 写明答案错在哪里，在 guidance 中简明给出本题核心思路点拨即可（无需催促补写过程，参考解答会随评价卡片一并展示）。
5. 独立性：结合“作答背景”判断这次作答是否在参考解答展示之后完成。若已展示参考解答，即使答案正确，也只能视为对照作答，不能表述为独立掌握；independence_assessment 必须是 after_reveal，并在 independence_note 中说明这是对照练习，建议遮住解答后脱稿重做。若未展示参考解答，independence_assessment 可为 independent；信息不足时用 unclear。
6. 请只输出一个 JSON 对象，不要包含 Markdown 标记或多余文字。格式：
{
  "is_correct": true 或 false 或 null,
  "final_answer_assessment": "对最终结果的评价说明",
  "step_by_step_analysis": "有解题过程时评价其规范性与步骤问题；未提供过程时为空字符串",
  "guidance": "针对性建议或核心思路点拨",
  "needs_supplement": true 或 false,
  "independence_assessment": "independent 或 after_reveal 或 unclear",
  "independence_note": "关于是否独立完成的简短说明，不得夸大掌握程度"
}
"""


class DeepSeekExerciseAgent:
    def __init__(
        self,
        settings: DeepSeekSettings | None = None,
        *,
        model: Any | None = None,
        http_async_client: Any | None = None,
    ) -> None:
        self._settings = settings
        self._model = model
        self._http_async_client = http_async_client

    async def generate_exercise(
        self,
        *,
        original_statement: str | None = None,
        knowledge_points: str = "",
        steps_summary: str | None = None,
        course: str | None = None,
        topic: str | None = None,
        recent_statements: list[str] | None = None,
        avoid_statements: list[str] | None = None,
    ) -> GeneratedExercise:
        model = self._model or build_chat_model(
            self._settings,  # type: ignore[arg-type]
            max_tokens=1500,
            timeout=45.0,
            json_mode=True,
            http_async_client=self._http_async_client,
        )
        course_hint = f"所属课程：{course}\n" if course else ""
        recent_block = ""
        if recent_statements:
            lines = "\n".join(
                f"{index}. {statement}"
                for index, statement in enumerate(recent_statements[:5], start=1)
            )
            recent_block = f"【近期已生成的题目（必须避开）】：\n{lines}\n\n"
        avoid_block = ""
        if avoid_statements:
            lines = "\n".join(f"- {statement}" for statement in avoid_statements[:3])
            avoid_block = (
                "【上一次生成与近期题目重复，本次必须实质变化】：\n"
                f"{lines}\n\n"
            )
        if original_statement and original_statement.strip():
            system_prompt = EXERCISE_GENERATION_PROMPT
            human_content = (
                f"{course_hint}"
                f"【原题内容】：\n{original_statement}\n\n"
                f"【核心知识点】：\n{knowledge_points}\n\n"
                f"【原题解题步骤简述】：\n{steps_summary or '无'}\n\n"
                f"{recent_block}"
                f"{avoid_block}"
                "请生成一道类似练习题并提供参考解答，输出 JSON。"
            )
        else:
            system_prompt = STANDALONE_EXERCISE_PROMPT
            target_topic = topic or knowledge_points or "核心知识点综合练习"
            human_content = (
                f"{course_hint}"
                f"【目标考点或题型要求】：\n{target_topic}\n\n"
                f"{recent_block}"
                f"{avoid_block}"
                "请按要求自主生成一道适合该课程复习的练习题（非真题）并提供参考解答，输出 JSON。"
            )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": human_content},
        ]

        try:
            response = await model.ainvoke(messages)
            log_usage("exercise_generation", response)
            raw = response_text(response)
            payload = parse_json_object(raw)
            return GeneratedExercise.model_validate(payload)
        except ValidationError as exc:
            raise QueryError(
                "MODEL_RESPONSE_INVALID",
                "生成的练习题格式不符合约定，请重试。",
            ) from exc
        except Exception as exc:
            if isinstance(exc, QueryError):
                raise
            mapped = map_provider_error(exc)
            if mapped is not None:
                raise mapped from exc
            raise QueryError(
                "MODEL_UNAVAILABLE", "模型服务暂时不可用，请稍后重试。"
            ) from exc

    async def evaluate_answer(
        self,
        *,
        statement: str,
        reference_solution: str,
        student_answer: str | None,
        student_solution_text: str | None = None,
        images: list[ImagePayload] | None = None,
        answered_after_revealed: bool = False,
        attempt_kind: str = "initial",
    ) -> AnswerEvaluation:
        has_images = bool(images)
        model = self._model or build_chat_model(
            self._settings,  # type: ignore[arg-type]
            max_tokens=1500,
            timeout=50.0,
            json_mode=not has_images,  # DeepSeek 多模态下保持纯文本输出解析
            http_async_client=self._http_async_client,
        )

        if answered_after_revealed:
            background = (
                "学生已经查看过本题的参考解答，之后才提交这次作答；"
                "这属于对照作答，答案正确也不代表独立掌握。"
            )
        else:
            background = "学生在查看本题参考解答之前提交这次作答。"
        kind_label = {
            "initial": "首次作答",
            "supplement": "补充过程后的再次评价",
            "correction": "订正后的再次评价",
        }.get(attempt_kind, "作答")
        has_process = bool((student_solution_text or "").strip()) or has_images
        process_block = (
            student_solution_text.strip()
            if (student_solution_text or "").strip()
            else (
                "（未写文字过程，请结合附图评价解题过程规范性）"
                if has_images
                else "未提供（解题过程为选填项；请直接对比参考解答判断最终答案对错，设 needs_supplement=false，step_by_step_analysis 留空，不要提示补写过程）"
            )
        )
        text_content = (
            f"【练习题目】：\n{statement}\n\n"
            f"【标准参考解答】：\n{reference_solution}\n\n"
            f"【本次作答类型】：{kind_label}\n\n"
            f"【作答背景】：{background}\n\n"
            f"【学生提交的最终答案】：\n{student_answer or '（未单独写最终答案）'}\n\n"
            f"【学生解题过程（选填，已提供={'是' if has_process else '否'}）】：\n{process_block}\n\n"
            "请对学生的作答进行客观评价，严格输出 JSON。"
        )

        user_content: Any
        if has_images:
            user_content = [{"type": "text", "text": text_content}]
            for img in images or []:
                user_content.append(_image_block(img))
        else:
            user_content = text_content

        messages = [
            {"role": "system", "content": ANSWER_EVALUATION_PROMPT},
            {"role": "user", "content": user_content},
        ]

        try:
            response = await model.ainvoke(messages)
            log_usage("answer_evaluation", response)
            raw = response_text(response)
            payload = parse_json_object(raw)
            return AnswerEvaluation.model_validate(payload)
        except ValidationError as exc:
            raise QueryError(
                "MODEL_RESPONSE_INVALID",
                "作答评价结果格式不符合约定，请重试。",
            ) from exc
        except Exception as exc:
            if isinstance(exc, QueryError):
                raise
            mapped = map_provider_error(exc)
            if mapped is not None:
                raise mapped from exc
            raise QueryError(
                "MODEL_UNAVAILABLE", "模型服务暂时不可用，请稍后重试。"
            ) from exc
