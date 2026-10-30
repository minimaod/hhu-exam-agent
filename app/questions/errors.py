"""题库集成层的稳定异常；HTTP 与 SSE 只展示中文原因，不含路径。"""


class QuestionError(Exception):
    """所有题目资源错误的基类。"""

    status_code = 409

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    @property
    def code(self) -> str:
        if self.status_code == 503:
            return "QUESTION_BANK_UNAVAILABLE"
        return "QUESTION_SEARCH_FAILED"


class QuestionBankUnavailableError(QuestionError):
    status_code = 503


class QuestionNotFoundError(QuestionError):
    status_code = 404


class QuestionSelectionInvalidError(QuestionError):
    status_code = 409


class QuestionNotRevealedError(QuestionError):
    status_code = 409


class QuestionSourceChangedError(QuestionError):
    status_code = 409


class QuestionSourcePrunedError(QuestionError):
    status_code = 410


class QuestionAssetMissingError(QuestionError):
    status_code = 404


class QuestionAssetChangedError(QuestionError):
    status_code = 409