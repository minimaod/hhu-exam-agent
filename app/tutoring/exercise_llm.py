"""DeepSeek 练习题生成与学生作答评价模型适配器。

职责边界：
- 根据原题与知识点生成结构化类似题与参考解答；
- 对学生的作答（文字及图片过程）进行客观结构化评价；
- 遵循防泄露约定：参考解答由服务层仅存数据库，不在此处做外部事件分发。
"""

import base64
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
    step_by_step_analysis: str = Field(min_length=1, max_length=3000)
    guidance: str = Field(min_length=1, max_length=2000)
    needs_supplement: bool = False
    # 本次作答是否在参考解答展示之后完成；答案正确也不等于独立掌握。
    independence_assessment: Literal["independent", "after_reveal", "unclear"] | None = None
    independence_note: str | None = Field(default=None, max_length=800)


EXERCISE_GENERATION_PROMPT = """\
你是河海大学考试复习助手。请根据提供的原题、核心知识点和步骤，生成一道核心考点相同、题型相近但具体数值、条件、操作或背景有明显变化的“类似练习题”。

要求：
1. 题干必须完整清晰，表述严谨，符号准确。
2. 必须提供详尽无误的标准参考解答（步骤清晰、结果明确）。
3. 必须避开“近期已生成的题目”：不能只是换个标点或重复同一组数字与条件；优先改变数值、条件、操作顺序或情境。
4. 若已提供“避免重复的题干”，本次生成必须与它实质不同。
5. 请只输出一个 JSON 对象，不要包含 Markdown 标记或多余文字。格式：
{
  "statement": "练习题完整题干",
  "knowledge_point": "考查的核心知识点名称",
  "reference_solution": "练习题完整参考解答与关键步骤"
}
"""

ANSWER_EVALUATION_PROMPT = """\
你是河海大学考试复习助手。请对比练习题干和标准参考解答，对学生的作答（包括文字答案及解题过程/图片）进行客观、详尽的评价。

评判规则：
1. 分别评价最终答案和推导过程。
2. 容错与补充原则：如果学生上传的解题图片模糊、关键公式无法辨识，或过程严重缺失无法确认思路，请将 needs_supplement 设为 true，is_correct 设为 null，并在 guidance 中温和请求补充过程，绝不能武断直接判为做错！
3. 若完全正确，is_correct 设为 true，肯定学生的思路并简析亮点。
4. 若答案或过程有明显错误，is_correct 设为 false，在 step_by_step_analysis 中指出具体错误发生在哪一步，在 guidance 中给出针对性点拨，引导学生纠错。
5. 独立性：结合“作答背景”判断这次作答是否在参考解答展示之后完成。若已展示参考解答，即使答案正确，也只能视为对照作答，不能表述为独立掌握；independence_assessment 必须是 after_reveal，并在 independence_note 中说明这是对照练习，建议遮住解答后脱稿重做。若未展示参考解答，independence_assessment 可为 independent；信息不足时用 unclear。
6. 请只输出一个 JSON 对象，不要包含 Markdown 标记或多余文字。格式：
{
  "is_correct": true 或 false 或 null,
  "final_answer_assessment": "对最终结果的评价说明",
  "step_by_step_analysis": "对解题过程和思路的逐层分析",
  "guidance": "针对性建议或纠错引导",
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
        original_statement: str,
        knowledge_points: str,
        steps_summary: str | None = None,
        course: str | None = None,
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
        human_content = (
            f"{course_hint}"
            f"【原题内容】：\n{original_statement}\n\n"
            f"【核心知识点】：\n{knowledge_points}\n\n"
            f"【原题解题步骤简述】：\n{steps_summary or '无'}\n\n"
            f"{recent_block}"
            f"{avoid_block}"
            "请生成一道类似练习题并提供参考解答，输出 JSON。"
        )
        messages = [
            {"role": "system", "content": EXERCISE_GENERATION_PROMPT},
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
            raise map_provider_error(exc) from exc

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
        text_content = (
            f"【练习题目】：\n{statement}\n\n"
            f"【标准参考解答】：\n{reference_solution}\n\n"
            f"【本次作答类型】：{kind_label}\n\n"
            f"【作答背景】：{background}\n\n"
            f"【学生提交的最终答案】：\n{student_answer or '（未单独写最终答案）'}\n\n"
            f"【学生文字解题过程】：\n{student_solution_text or '（未写文字过程）'}\n\n"
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
            raise map_provider_error(exc) from exc
