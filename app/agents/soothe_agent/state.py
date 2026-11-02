"""单次轮次的私密内存模型，禁止写入 checkpoint、日志或公共状态。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class _PrivateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class ReplyContext(_PrivateModel):
    # 帮我回素材来自已授权业务工具；正文只进入不可信素材槽。
    # 判别业务上下文模式，必须与运行的业务类型一致。
    mode: Literal["reply"]
    # 本轮聊天正文，作为不可信材料进入模型。
    chat_text: str = Field(min_length=10, max_length=2000)
    # 可空的补充背景，仅供本轮推理。
    background: str | None = Field(max_length=200)
    # 用户选择的语气，作为受控模板变量。
    tone: Literal["sincere", "light", "any"]
    # 上批回复，用于确定性拒绝重复发送。
    previous_replies: list[Annotated[str, Field(max_length=120)]] = Field(max_length=6)


class ScenarioInfo(_PrivateModel):
    # 静态场景配置可信，但隐藏诉求仍禁止进入日志与公共账本。
    # 业务端静态场景标识。
    key: str = Field(min_length=1, max_length=200)
    # 模型消费的静态场景标题。
    title: str = Field(min_length=1, max_length=200)
    # 静态场景中生气的原因。
    anger_reason: str = Field(min_length=1, max_length=200)
    # 只供模型理解的隐藏诉求，禁止落日志。
    hidden_need: str = Field(min_length=1, max_length=200)


class HistoryItem(_PrivateModel):
    # 历史对话只供本轮推理，不持久化在 Runtime。
    # 用户本轮或历史发言，作为不可信材料。
    user_message: str = Field(min_length=1, max_length=200)
    # 本轮或历史角色回复正文。
    reply: str = Field(min_length=1, max_length=120)


class SimulatorContext(_PrivateModel):
    # 业务端维护计分和轮数；Runtime 只消费快照，不自行修改。
    # 判别业务上下文模式，必须与运行的业务类型一致。
    mode: Literal["simulator"]
    # 业务端授权的静态场景配置。
    scenario: ScenarioInfo
    # 限定角色性别，映射到受控模板变量。
    partner_gender: Literal["male", "female"]
    # 用户提供的称呼，按不可信材料处理。
    partner_name: str = Field(min_length=1, max_length=8)
    # 业务端维护的当前原谅值。
    forgiveness: int = Field(ge=0, le=100)
    # 已经完成的轮数，当前轮不在其中。
    round_count: int = Field(ge=0, le=9)
    # 最多九轮历史，仅在本次执行器内存使用。
    history: list[HistoryItem] = Field(max_length=9)
    # 用户本轮或历史发言，作为不可信材料。
    user_message: str = Field(min_length=1, max_length=200)


class ReplyOption(_PrivateModel):
    # 三种风格的数量和唯一性由提交前语义校验强制执行。
    # 回复风格，语义校验要求三种风格各一条。
    style: Literal["sincere", "light", "action"]
    # 可直接发送的回复正文。
    text: str = Field(min_length=1, max_length=120)


class FollowUp(_PrivateModel):
    # 两种后续建议均为私密正文。
    # 对方缓和后的后续建议。
    if_softened: str = Field(min_length=1, max_length=60)
    # 对方仍冷淡时的后续建议。
    if_still_cold: str = Field(min_length=1, max_length=60)


class ReplyDraft(_PrivateModel):
    # 模型只能输出分析字段，不能夹带控制字段或运行状态。
    # 受限冲突分类，与安全标记双向一致。
    conflict_type: Literal[
        "misunderstanding", "solvable", "perpetual", "core", "safety"
    ]
    # 一到三个具体矛盾点。
    conflict_points: list[Annotated[str, Field(min_length=1, max_length=40)]] = Field(
        min_length=1, max_length=3
    )
    # 对另一半感受和诉求的推测。
    partner_feeling: str = Field(min_length=1, max_length=80)
    # 普通场景三条、安全场景为空，语义闸门校验。
    replies: list[ReplyOption] = Field(max_length=3)
    # 当前应避免的说法，允许解释禁用表达。
    avoid: list[Annotated[str, Field(min_length=1, max_length=40)]] = Field(
        min_length=1, max_length=3
    )
    # 两种后续沟通建议。
    follow_up: FollowUp
    # 模型识别的安全情形标记。
    safety_flag: bool


class SimulatorDraft(_PrivateModel):
    # 建议分数保留宽松边界，最终钳制与胜负由业务端负责。
    # 本轮或历史角色回复正文。
    reply: str = Field(min_length=1, max_length=120)
    # 模型建议分数，业务端另行钳制最终生效值。
    delta_suggestion: int = Field(ge=-50, le=50)
    # 教练视角的简短点评。
    comment: str = Field(min_length=1, max_length=40)
    # 模型识别的安全情形标记。
    safety_flag: bool


class SootheSubmitResult(_PrivateModel):
    # 查询未提交时允许没有 revision，提交节点另行校验成功回执。
    # 业务端是否已经确认提交。
    submitted: bool
    # 已提交版本号，未提交查询可省略。
    revision: int | None = Field(default=None, ge=1)


TurnContext = Annotated[ReplyContext | SimulatorContext, Field(discriminator="mode")]


class SootheWorkingState(_PrivateModel):
    # 首节点重读的私密上下文和草稿仅存在执行器内存。
    # 首节点读取的判别联合上下文。
    turn_context: TurnContext | None = None
    # 结构化草稿，只有校验通过才允许提交。
    turn_draft: ReplyDraft | SimulatorDraft | None = None
    # 业务回执不包含正文，但也不扩展公共 checkpoint。
    # 安全提交回执，不进入公共 checkpoint。
    submit_result: SootheSubmitResult | None = None
