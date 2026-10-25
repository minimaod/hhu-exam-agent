"""F005 测试替身：意图路由、辅导模型和图片生成。"""

import io
from collections.abc import Iterable

from PIL import Image

from app.conversations.dispatch import IntentDecision
from app.querying.errors import QueryError
from app.tutoring.models import (
    Explanation,
    FollowupAnswer,
    ImagePayload,
    KnowledgePoint,
    ProblemUnderstanding,
    Step,
)
from app.conversations.graph import ActionPlan, ScheduleDecision
from app.tutoring.exercise_llm import AnswerEvaluation, GeneratedExercise


class FakeTutor:
    """按脚本返回题目理解/讲解/追问；记录收到的图片和文字。"""

    def __init__(
        self,
        *,
        understandings: Iterable[ProblemUnderstanding | QueryError] = (),
        explanations: Iterable[Explanation | QueryError] = (),
        followups: Iterable[FollowupAnswer | QueryError] = (),
    ) -> None:
        self._understandings = list(understandings)
        self._explanations = list(explanations)
        self._followups = list(followups)
        self.understand_calls: list[tuple[str, list[ImagePayload]]] = []
        self.explain_calls: list[dict] = []
        self.followup_calls: list[dict] = []

    def push_understanding(self, item: ProblemUnderstanding | QueryError) -> None:
        self._understandings.append(item)

    def push_explanation(self, item: Explanation | QueryError) -> None:
        self._explanations.append(item)

    def push_followup(self, item: FollowupAnswer | QueryError) -> None:
        self._followups.append(item)

    async def understand_problem(
        self, text: str, images: list[ImagePayload]
    ) -> ProblemUnderstanding:
        self.understand_calls.append((text, images))
        if not self._understandings:
            raise AssertionError("FakeTutor 没有更多题目理解结果。")
        item = self._understandings.pop(0)
        if isinstance(item, QueryError):
            raise item
        return item

    async def explain(self, **kwargs) -> Explanation:
        self.explain_calls.append(kwargs)
        if not self._explanations:
            raise AssertionError("FakeTutor 没有更多讲解结果。")
        item = self._explanations.pop(0)
        if isinstance(item, QueryError):
            raise item
        return item

    async def answer_followup(self, **kwargs) -> FollowupAnswer:
        self.followup_calls.append(kwargs)
        if not self._followups:
            raise AssertionError("FakeTutor 没有更多追问结果。")
        item = self._followups.pop(0)
        if isinstance(item, QueryError):
            raise item
        return item


class FakeRouter:
    def __init__(self, decisions: Iterable[IntentDecision | QueryError]) -> None:
        self._decisions = list(decisions)
        self.calls: list[dict] = []

    def push(self, item: IntentDecision | QueryError) -> None:
        self._decisions.append(item)

    async def __call__(self, text: str, **kwargs) -> IntentDecision:
        self.calls.append({"text": text, **kwargs})
        if not self._decisions:
            raise AssertionError("FakeRouter 没有更多意图结果。")
        item = self._decisions.pop(0)
        if isinstance(item, QueryError):
            raise item
        return item


def png_bytes(width: int = 320, height: int = 140) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, "PNG")
    return buffer.getvalue()


def jpeg_bytes(width: int = 320, height: int = 140) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, "JPEG")
    return buffer.getvalue()


def clear_understanding(statement: str = "计算极限 lim_{x→0} (sin x)/x。") -> ProblemUnderstanding:
    return ProblemUnderstanding(
        statement=statement,
        clarity="clear",
        unclear_points=[],
        uncertainties=[],
        multiple_problems=False,
    )


def unclear_understanding(
    statement: str = "计算 ∫_0^1 f(x) dx。",
    points: list[str] | None = None,
) -> ProblemUnderstanding:
    return ProblemUnderstanding(
        statement=statement,
        clarity="unclear",
        unclear_points=points or ["被积函数 f(x) 看不清"],
        uncertainties=[],
        multiple_problems=False,
    )


def sample_explanation(
    final_answer: str = "1",
    *,
    error_analysis: str | None = None,
) -> Explanation:
    return Explanation(
        knowledge_points=[
            KnowledgePoint(name="重要极限", explanation="lim_{x→0} sin x / x = 1。")
        ],
        steps=[
            Step(index=1, title="识别形式", content="这是 0/0 型，但可用重要极限。"),
            Step(index=2, title="求值", content="lim_{x→0} sin x / x = 1。"),
        ],
        final_answer=final_answer,
        error_analysis=error_analysis,
        uncertainties=[],
    )


class FakeExerciseAgent:
    """按脚本返回练习生成与作答评价。"""

    def __init__(
        self,
        *,
        exercises: Iterable[GeneratedExercise | QueryError] = (),
        evaluations: Iterable[AnswerEvaluation | QueryError] = (),
    ) -> None:
        self._exercises = list(exercises)
        self._evaluations = list(evaluations)
        self.generate_calls: list[dict] = []
        self.evaluate_calls: list[dict] = []

    def push_exercise(self, item: GeneratedExercise | QueryError) -> None:
        self._exercises.append(item)

    def push_evaluation(self, item: AnswerEvaluation | QueryError) -> None:
        self._evaluations.append(item)

    async def generate_exercise(self, **kwargs) -> GeneratedExercise:
        self.generate_calls.append(kwargs)
        if not self._exercises:
            return GeneratedExercise(
                statement="类似练习：求极限 lim(x->0) (e^x - 1)/x",
                knowledge_point="等价无穷小代换",
                reference_solution="利用 e^x - 1 ~ x (x->0)，原式 = 1。",
            )
        item = self._exercises.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def evaluate_answer(self, **kwargs) -> AnswerEvaluation:
        self.evaluate_calls.append(kwargs)
        if not self._evaluations:
            return AnswerEvaluation(
                is_correct=True,
                final_answer_assessment="答案正确，极限值为 1。",
                step_by_step_analysis="正确识别出等价无穷小并完成代换，步骤严密。",
                guidance="完成得很好，继续保持对常见等价无穷小的熟练度。",
                needs_supplement=False,
            )
        item = self._evaluations.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeScheduler:
    """按脚本返回调度计划。"""

    def __init__(
        self,
        *,
        decisions: Iterable[ScheduleDecision | QueryError] = (),
    ) -> None:
        self._decisions = list(decisions)
        self.calls: list[dict] = []

    def push(self, item: ScheduleDecision | QueryError) -> None:
        self._decisions.append(item)

    async def plan(self, query: str, **kwargs) -> ScheduleDecision:
        self.calls.append({"query": query, **kwargs})
        if not self._decisions:
            # 默认返回空或澄清
            return ScheduleDecision(
                actions=[ActionPlan(action="clarify_or_reply", reason="默认兜底回复")],
                assistant_reply="请问有什么可以帮您？",
                suggested_actions=["查试卷", "辅导题目"],
            )
        item = self._decisions.pop(0)
        if isinstance(item, QueryError):
            raise item
        return item
