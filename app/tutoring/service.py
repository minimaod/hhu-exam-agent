"""辅导结果的组装与模型输入准备。

这里不直接处理 HTTP；会话服务和错题记录服务共用：
- 数据库题目行 → 前端 ProblemView；
- 附件行 → 模型需要的 ImagePayload；
- 模型理解/讲解 → 结果模型；
- 记录标题等纯程序规则。
"""

from pathlib import Path

from app.conversations.errors import AttachmentNotFoundError, InvalidImageError
from app.conversations.repository import AttachmentRecord, ProblemRecord
from app.images import resolve_attachment
from app.tutoring.models import (
    AttachmentView,
    Explanation,
    ExplanationResult,
    ImagePayload,
    NeedsClarificationResult,
    ProblemView,
)

_RECORD_TITLE_MAX = 40


def build_problem_view(
    problem: ProblemRecord,
    *,
    record_id: str | None = None,
) -> ProblemView:
    order = {"statement": 0, "solution": 1, "supplement": 2}
    attachments = [
        AttachmentView(
            id=item.id,
            role=item.role or "statement",  # type: ignore[arg-type]
            mime=item.mime,
            width=item.width,
            height=item.height,
            url=(
                f"/api/conversations/{problem.conversation_id}"
                f"/attachments/{item.id}"
            ),
        )
        for item in sorted(
            problem.attachments,
            key=lambda entry: (order.get(entry.role or "statement", 9), entry.id),
        )
    ]
    return ProblemView(
        id=problem.id,
        root_problem_id=problem.root_problem_id,
        version=problem.version,
        statement_text=problem.statement_text,
        status=problem.status,  # type: ignore[arg-type]
        clarity=problem.clarity,  # type: ignore[arg-type]
        source=problem.source,
        attachments=attachments,
        student_answer_text=problem.student_answer_text,
        solution_text=problem.solution_text,
        self_reported_understood=problem.self_reported_understood,
        created_at=problem.created_at,
        updated_at=problem.updated_at,
    )


def load_images(
    attachments: list[AttachmentRecord],
    *,
    root: Path,
    roles: set[str] | None = None,
) -> list[ImagePayload]:
    """读取附件文件为模型输入；文件缺失按可展示的图片错误处理。"""

    images: list[ImagePayload] = []
    for attachment in attachments:
        if roles is not None and (attachment.role or "statement") not in roles:
            continue
        try:
            data = resolve_attachment(root, attachment.stored_name).read_bytes()
        except (AttachmentNotFoundError, OSError) as exc:
            # 读取失败不能把磁盘路径交给模型或页面。
            raise InvalidImageError("题目图片暂时无法读取，请重新上传。") from exc
        images.append(ImagePayload(data=data, mime=attachment.mime))
    return images


def build_explanation_result(
    problem: ProblemRecord,
    explanation: Explanation,
    *,
    record_id: str | None,
) -> ExplanationResult:
    view = build_problem_view(problem, record_id=record_id)
    return ExplanationResult(
        problem=view,
        knowledge_points=explanation.knowledge_points,
        steps=explanation.steps,
        final_answer=explanation.final_answer,
        error_analysis=explanation.error_analysis,
        uncertainties=explanation.uncertainties,
        self_reported_understood=problem.self_reported_understood,
        record_id=record_id,
    )


def build_needs_clarification(problem: ProblemRecord) -> NeedsClarificationResult:
    points = problem.unclear_points or ["题目中的关键条件或符号仍不清楚。"]
    questions = [f"请补充或确认：{point}" for point in points[:5]]
    return NeedsClarificationResult(
        problem=build_problem_view(problem),
        questions=questions or ["请补充题目中看不清或缺失的条件。"],
        unclear_points=points,
    )


def record_title(statement: str) -> str:
    text = " ".join(statement.split())
    if not text:
        return "未命名错题"
    if len(text) <= _RECORD_TITLE_MAX:
        return text
    return text[: _RECORD_TITLE_MAX - 1] + "…"
