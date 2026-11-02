from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agents.soothe_agent.state import ReplyContext, ReplyDraft, SimulatorDraft
from app.agents.soothe_agent.validation import BANNED_PHRASES, validate_draft
from app.runtime.executor import WorkflowNodeError


def context():
    return ReplyContext(
        mode="reply",
        chat_text=uuid4().hex,
        background=None,
        tone="any",
        previous_replies=[],
    )


def draft():
    return ReplyDraft(
        conflict_type="solvable",
        conflict_points=["约定"],
        partner_feeling="可能委屈",
        replies=[
            {"style": s, "text": uuid4().hex} for s in ["sincere", "light", "action"]
        ],
        avoid=["推卸责任"],
        follow_up={"if_softened": "听听需求", "if_still_cold": "留些空间"},
        safety_flag=False,
    )


def test_valid_and_safety():
    c, d = context(), draft()
    validate_draft("soothe_reply", c, d)
    d.conflict_type, d.safety_flag, d.replies = "safety", True, []
    validate_draft("soothe_reply", c, d)


@pytest.mark.parametrize(
    "change", ["flag", "type", "count", "style", "duplicate", "blank"]
)
def test_semantic_rejection(change):
    c, d = context(), draft()
    if change == "flag":
        d.safety_flag = True
    if change == "type":
        d.conflict_type = "safety"
    if change == "count":
        d.replies.pop()
    if change == "style":
        d.replies[1].style = d.replies[0].style
    if change == "duplicate":
        c.previous_replies = [d.replies[0].text]
    if change == "blank":
        d.follow_up.if_softened = " "
    with pytest.raises(WorkflowNodeError, match="^ANSWER_VALIDATION_FAILED$"):
        validate_draft("soothe_reply", c, d)


@pytest.mark.parametrize("phrase", BANNED_PHRASES)
def test_banned(phrase):
    d = draft()
    d.replies[0].text = phrase
    with pytest.raises(WorkflowNodeError):
        validate_draft("soothe_reply", context(), d)


@pytest.mark.parametrize(
    "field,value",
    [
        ("partner_feeling", "x" * 81),
        ("conflict_points", []),
        ("avoid", ["x" * 41]),
        ("replies", [{"style": "other", "text": "x"}]),
        ("safety_flag", 1),
        ("next_node", "private"),
        ("authorization_version", 1),
    ],
)
def test_schema_rejection(field, value):
    payload = deepcopy(draft().model_dump())
    payload[field] = value
    with pytest.raises(ValidationError):
        ReplyDraft.model_validate(payload)


@pytest.mark.parametrize("delta", [-50, 50])
def test_simulator_delta_boundary(delta):
    assert (
        SimulatorDraft(
            reply="好", delta_suggestion=delta, comment="具体", safety_flag=False
        ).delta_suggestion
        == delta
    )


@pytest.mark.parametrize("delta", [-51, 51, True, "1"])
def test_simulator_delta_rejection(delta):
    with pytest.raises(ValidationError):
        SimulatorDraft(
            reply="好", delta_suggestion=delta, comment="具体", safety_flag=False
        )


def simulator_context():
    from app.agents.soothe_agent.state import SimulatorContext

    return SimulatorContext(
        mode="simulator",
        scenario={
            "key": "k",
            "title": "约定",
            "anger_reason": "迟到",
            "hidden_need": uuid4().hex,
        },
        partner_gender="female",
        partner_name="TA",
        forgiveness=0,
        round_count=0,
        history=[],
        user_message=uuid4().hex,
    )


@pytest.mark.parametrize("safety", [False, True])
def test_simulator_safety_and_phrases(safety):
    c = simulator_context()
    d = SimulatorDraft(
        reply="我们先停下来好吗",
        delta_suggestion=0,
        comment="留出空间",
        safety_flag=safety,
    )
    validate_draft("soothe_simulator", c, d)
    for phrase in BANNED_PHRASES:
        d.reply = phrase
        with pytest.raises(WorkflowNodeError):
            validate_draft("soothe_simulator", c, d)
    d.reply = " "
    with pytest.raises(WorkflowNodeError):
        validate_draft("soothe_simulator", c, d)


def test_safety_rejects_nonempty_and_context_mismatch():
    c, d = context(), draft()
    d.safety_flag, d.conflict_type = True, "safety"
    with pytest.raises(WorkflowNodeError):
        validate_draft("soothe_reply", c, d)
    for business in ["unknown", "soothe_simulator"]:
        with pytest.raises(WorkflowNodeError):
            validate_draft(business, c, d)
    with pytest.raises(WorkflowNodeError):
        validate_draft("soothe_reply", simulator_context(), d)


def test_all_models_strict_extra_and_private_errors():
    from app.agents.soothe_agent.state import SootheSubmitResult, SootheWorkingState

    models = [
        context(),
        draft(),
        *draft().replies,
        draft().follow_up,
        simulator_context(),
        simulator_context().scenario,
        SimulatorDraft(
            reply="好", delta_suggestion=0, comment="具体", safety_flag=False
        ),
        SootheSubmitResult(submitted=False),
        SootheWorkingState(),
    ]
    from app.agents.soothe_agent.state import HistoryItem

    models.append(HistoryItem(user_message=uuid4().hex, reply=uuid4().hex))
    for model in models:
        sentinel = uuid4().hex
        payload = model.model_dump()
        payload["next_node"] = sentinel
        with pytest.raises(ValidationError) as error:
            type(model).model_validate(payload)
        assert sentinel not in str(error.value)
        assert model.model_config["strict"] is True
        assert model.model_config["extra"] == "forbid"


def test_every_schema_length_numeric_enum_boundary():
    """从声明元数据逐字段测试边界，覆盖上下文与所有嵌套草稿。"""
    from typing import Annotated, Literal, get_args, get_origin

    from app.agents.soothe_agent.state import HistoryItem, SootheSubmitResult

    models = [
        context(),
        draft(),
        draft().replies[0],
        draft().follow_up,
        simulator_context(),
        simulator_context().scenario,
        HistoryItem(user_message="x", reply="x"),
        SimulatorDraft(
            reply="好", delta_suggestion=0, comment="具体", safety_flag=False
        ),
        SootheSubmitResult(submitted=True, revision=1),
    ]
    for model in models:
        for name, info in type(model).model_fields.items():
            payload = model.model_dump()
            original = payload[name]
            for constraint in info.metadata:
                for bound in ["min_length", "max_length", "ge", "le"]:
                    limit = getattr(constraint, bound, None)
                    if limit is None:
                        continue
                    if bound.endswith("length"):

                        def value_at(size, original=original, name=name):
                            if isinstance(original, str) or original is None:
                                return "x" * size
                            item = (
                                original[0]
                                if original
                                else (
                                    {"user_message": "x", "reply": "x"}
                                    if name == "history"
                                    else "x"
                                )
                            )
                            return [deepcopy(item) for _ in range(size)]

                        payload[name] = value_at(limit)
                        type(model).model_validate(payload)
                        payload[name] = value_at(
                            limit + (1 if bound == "max_length" else -1)
                        )
                    else:
                        payload[name] = limit
                        type(model).model_validate(payload)
                        payload[name] = limit + (1 if bound == "le" else -1)
                    with pytest.raises(ValidationError):
                        type(model).model_validate(payload)
                    payload[name] = original
            if get_origin(info.annotation) is Literal:
                for allowed in get_args(info.annotation):
                    payload[name] = allowed
                    type(model).model_validate(payload)
                payload[name] = "invalid"
                with pytest.raises(ValidationError):
                    type(model).model_validate(payload)
            if get_origin(info.annotation) is list:
                element = get_args(info.annotation)[0]
                if get_origin(element) is Annotated:
                    for constraint in get_args(element)[1:]:
                        for length in [
                            getattr(constraint, "min_length", None),
                            getattr(constraint, "max_length", None),
                        ]:
                            if length is None:
                                continue
                            payload[name] = ["x" * length]
                            type(model).model_validate(payload)
                    payload[name] = ["x" * 121]
                    with pytest.raises(ValidationError):
                        type(model).model_validate(payload)


def test_blank_strings_anywhere_and_exact_duplicate_only():
    c, d = context(), draft()
    c.previous_replies = [d.replies[0].text + " "]
    validate_draft("soothe_reply", c, d)
    for path in [
        ("partner_feeling",),
        ("conflict_points", 0),
        ("avoid", 0),
        ("replies", 0, "text"),
        ("follow_up", "if_still_cold"),
    ]:
        payload = d.model_dump()
        target = payload
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = " "
        with pytest.raises(WorkflowNodeError):
            validate_draft("soothe_reply", c, ReplyDraft.model_validate(payload))
    c.background = " "
    with pytest.raises(WorkflowNodeError):
        validate_draft("soothe_reply", c, d)


def test_strict_scalar_coercions_and_nested_controls():
    from app.agents.soothe_agent.state import SootheSubmitResult

    for model, field, value in [
        (context(), "chat_text", 1234567890),
        (simulator_context(), "forgiveness", True),
        (simulator_context(), "round_count", "1"),
        (SootheSubmitResult(submitted=True, revision=1), "revision", True),
        (SootheSubmitResult(submitted=True, revision=1), "submitted", 1),
    ]:
        payload = model.model_dump()
        payload[field] = value
        with pytest.raises(ValidationError):
            type(model).model_validate(payload)
    for field in ["replies", "follow_up"]:
        payload = draft().model_dump()
        target = payload[field][0] if field == "replies" else payload[field]
        target["authorization_version"] = 1
        with pytest.raises(ValidationError):
            ReplyDraft.model_validate(payload)
