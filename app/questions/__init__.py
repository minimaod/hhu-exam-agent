"""F007 P4：真题题库与会话的集成层。

数据与检索仍由 ``app.question_bank`` 负责；本包只处理：
- 进程生命周期（Qdrant 单例、线程串行）与降级状态；
- 已审核候选到会话卡片的转换、公开快照；
- 当前/历史 build 的答案与题图回读（只读、校验哈希）；
- 给模型的私有材料组装。

答案正文不写入学习数据库；只有显式查看或讲解时才按对象读取。
"""

from app.questions.errors import (
    QuestionAssetChangedError,
    QuestionAssetMissingError,
    QuestionBankUnavailableError,
    QuestionError,
    QuestionNotFoundError,
    QuestionNotRevealedError,
    QuestionSelectionInvalidError,
    QuestionSourceChangedError,
    QuestionSourcePrunedError,
)
from app.questions.models import (
    AnswerAvailability,
    QuestionAssetView,
    QuestionCapabilityView,
    QuestionCard,
    QuestionFormula,
    QuestionKnowledgePoint,
    QuestionOption,
    QuestionPages,
    QuestionReview,
    QuestionSearchResult,
    QuestionSelection,
    QuestionSourceView,
)

__all__ = [
    "AnswerAvailability",
    "QuestionAssetChangedError",
    "QuestionAssetMissingError",
    "QuestionAssetView",
    "QuestionBankUnavailableError",
    "QuestionCapabilityView",
    "QuestionCard",
    "QuestionError",
    "QuestionFormula",
    "QuestionKnowledgePoint",
    "QuestionNotFoundError",
    "QuestionNotRevealedError",
    "QuestionOption",
    "QuestionPages",
    "QuestionReview",
    "QuestionSearchResult",
    "QuestionSelection",
    "QuestionSelectionInvalidError",
    "QuestionSourceChangedError",
    "QuestionSourcePrunedError",
    "QuestionSourceView",
]