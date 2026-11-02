"""QA 私密工作态仅随一次执行器保存在内存，禁止传给 checkpoint 或账本。"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class QaEvidence(BaseModel):
    """业务端已授权的单条证据，禁止响应夹带控制字段。"""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    # 业务端提供的稳定引用，仅能引用本次读取的证据集合。
    source_ref: str = Field(min_length=1, max_length=128)
    # 仅供本次模型上下文使用的私密证据文本。
    text: str = Field(min_length=1, max_length=8192)


class QaQuestionContext(BaseModel):
    """问题和已授权证据只来自固定读取工具，不从 Run 请求读取正文。"""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    # 待回答问题，作为不可信数据进入模型消息。
    question: str = Field(min_length=1, max_length=8192)
    # 由业务端完成授权和检索的有限证据，Runtime 不扩大检索范围。
    evidence: list[QaEvidence] = Field(max_length=32)


class QaAnswerDraft(BaseModel):
    """模型答案只接受正文、证据引用和无证据标记三个字段。"""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    # 待业务端接收的回答正文；长度上限在模型解析及提交前重复校验。
    answer: str = Field(min_length=1, max_length=4000)
    # 只允许本次已授权证据中的引用，不能指向问题或任意外部数据。
    source_refs: list[str] = Field(max_length=32)
    # 无可用证据时显式标记，不能用于绕过有证据时的引用要求。
    no_evidence: bool


class QaSubmitResult(BaseModel):
    """业务端提交查询的安全回执，不接受回答正文或额外字段。"""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    # 是否已提交，false 表示查询未观察到提交。
    submitted: bool
    # 已提交时返回业务端版本号，未提交时可省略。
    revision: int | None = Field(default=None, ge=1)


class QaWorkingState(BaseModel):
    """不扩展公共 AgentState，恢复时重读、重算并按稳定键对账。"""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    # 首节点读取的私密问题和证据；只供后续模型节点消费。
    question_context: QaQuestionContext | None = None
    # 结构化解析后的私密答案草稿；校验通过后才允许提交。
    answer_draft: QaAnswerDraft | None = None
    # 业务端返回的安全提交事实；不进入公共 checkpoint。
    submit_result: QaSubmitResult | None = None
