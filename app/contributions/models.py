"""自愿贡献资料接口的数据模型。"""

from typing import Literal

from pydantic import BaseModel


class ContributionSubmitResponse(BaseModel):
    """学生提交自愿贡献试卷后的响应。"""

    contribution_id: str
    course: str
    year: str
    status: Literal["pending_review"] = "pending_review"
    message: str = "已提交，等待审核"
