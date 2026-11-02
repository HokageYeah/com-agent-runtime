"""帮哄四节点、未知提交恢复及隐私边界；正文使用临时随机哨兵。"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.contracts.tools import TOOL_ERROR_SPECS_V1_1
from app.db.sqlalchemy_db import Base
from app.models import AgentDefinition, AgentRun, AgentToolCall
from app.runtime.executor import WorkflowNodeError
from app.runtime.interfaces import LeaseContext
from app.runtime.model_gateway import ModelGatewayResult
from app.runtime.state import AgentState
from app.runtime.tool_gateway import BusinessConnector, ToolGateway
from app.services.tool_call_audit_service import ToolCallAuditService

NODES = ['load_turn_context', 'compose_turn', 'validate_turn', 'submit_turn_result']


def private_text():
    return uuid4().hex.translate(str.maketrans('0123456789', 'ghijklmnop'))


def reply_context():
    return {'mode': 'reply', 'chat_text': private_text(), 'background': None,
            'tone': 'any', 'previous_replies': []}


def simulator_context():
    return {'mode': 'simulator', 'scenario': {'key': 'practice', 'title': private_text(),
            'anger_reason': private_text(), 'hidden_need': private_text()}, 'partner_gender': 'female',
            'partner_name': 'TA', 'forgiveness': 20, 'round_count': 0, 'history': [], 'user_message': private_text()}


def reply_draft():
    return {'conflict_type': 'solvable', 'conflict_points': [private_text()],
            'partner_feeling': private_text(),
            'replies': [{'style': style, 'text': private_text()} for style in ['sincere', 'light', 'action']],
            'avoid': [private_text()], 'follow_up': {'if_softened': private_text(), 'if_still_cold': private_text()},
            'safety_flag': False}


def simulator_draft():
    return {'reply': private_text(), 'delta_suggestion': 20, 'comment': private_text(), 'safety_flag': False}


@pytest.fixture
def soothe_environment():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        now = datetime.now(UTC)
        run = AgentRun(run_id='soothe-run', agent_id='soothe_agent', agent_version='1.0.0',
                       package_digest='sha256:test', contract_version='1.0.0',
                       business_type='soothe_reply', business_id='reply-turn', status='running', dispatch_state='claimed',
                       input_json={'session_id': 'session', 'turn_id': 'reply-turn'},
                       authorization_version=1, caller_id='caller', tenant_id='tenant', create_idempotency_key='key',
                       callback_target_id='soothe_callback', business_connector_id='couple_diary_backend', trace_id='trace',
                       execution_attempt=1, lease_owner='worker', fencing_token=1,
                       lease_expires_at=now+timedelta(minutes=5), run_deadline_at=now+timedelta(minutes=10))
        session.add_all([run, AgentDefinition(agent_id='soothe_agent', version='1.0.0', runtime_type='workflow',
                        definition_json={}, package_digest='sha256:test', contract_version='1.0.0', status='active',
                        status_changed_at=now, status_changed_by='test', status_change_reason='fixture')])
        session.commit()
        lease = LeaseContext(execution_attempt=1, lease_owner='worker', fencing_token=1,
                             lease_expires_at=run.lease_expires_at, privacy_version=1, authorization_version=1)
        yield session, run, lease
    engine.dispose()


class ModelBoundary:
    def __init__(self, output):
        self.output = output

    def call(self, run_id, node_id, request):
        return self.output if isinstance(self.output, ModelGatewayResult) else ModelGatewayResult('succeeded', self.output)


def make_runner(environment, *, context=None, draft=None, committed=False, reject_submit=False):
    from app.agents.soothe_agent.runner import SootheNodeRunner

    session, run, lease = environment
    context = context if context is not None else reply_context()
    draft = draft if draft is not None else reply_draft()
    observed, sent, submissions = committed, [], []
    def handler(request):
        nonlocal observed
        name = request.headers['X-Agent-Tool-Name']
        sent.append((name, request.headers.get('Idempotency-Key')))
        if name.endswith('get_turn_context'):
            output = {'turn_context': context}
        elif name.endswith('get_turn_result'):
            output = {'submitted': observed, **({'revision': 1} if observed else {})}
        else:
            if reject_submit:
                spec = TOOL_ERROR_SPECS_V1_1['IDEMPOTENCY_CONFLICT']
                return httpx.Response(409, json={'error_code': 'IDEMPOTENCY_CONFLICT',
                    'error_type': spec['error_type'], 'retryable': spec['retryable'],
                    'safe_message': spec['safe_message'], 'details_visible_to_model': False})
            submissions.append(json.loads(request.content)['input']['turn_result'])
            observed = True
            output = {'submitted': True, 'revision': 1}
        return httpx.Response(200, json={'output': output})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    gateway = ToolGateway({'couple_diary_backend': BusinessConnector('https://8.8.8.8', 'runtime', 'key', 'test-only')}, client)
    runner = SootheNodeRunner(gateway, ToolCallAuditService(session), ModelBoundary(draft))
    runner.bind_lease_context(lease)
    return runner, client, sent, submissions


def walk(runner, run):
    return [runner.run_node({'node_id': node}, run, AgentState()) for node in NODES]


@pytest.mark.parametrize('business_type', ['soothe_reply', 'soothe_simulator'])
def test_four_nodes_submit_once_with_stable_query_key_and_safe_summaries(soothe_environment, business_type, caplog):
    session, run, _ = soothe_environment
    run.business_type = business_type
    context = reply_context() if business_type == 'soothe_reply' else simulator_context()
    draft = reply_draft() if business_type == 'soothe_reply' else simulator_draft()
    runner, client, sent, submissions = make_runner(soothe_environment, context=context, draft=draft)
    with client, caplog.at_level(logging.INFO):
        result = walk(runner, run)
        walk(runner, run)
    matches = submissions == [draft]
    assert matches, 'wrong submission content or duplicate submit'
    assert [item['node_id'] for item in result] == NODES
    assert len({key for name, key in sent if not name.endswith('get_turn_context')}) == 1
    assert session.scalar(select(AgentToolCall)).status == 'succeeded'
    surfaces = str(result) + caplog.text
    private_free = context.get('chat_text', context.get('user_message')) not in surfaces
    assert private_free, 'private body escaped Runner'


def test_mode_mismatch_fails_before_compose(soothe_environment):
    _, run, _ = soothe_environment
    runner, client, _, submissions = make_runner(soothe_environment, context=simulator_context())
    with client, pytest.raises(WorkflowNodeError):
        runner.run_node({'node_id': NODES[0]}, run, AgentState())
    assert not submissions


def test_unvalidated_cannot_submit(soothe_environment):
    _, run, _ = soothe_environment
    runner, client, _, submissions = make_runner(soothe_environment)
    with client, pytest.raises(WorkflowNodeError, match='ANSWER_VALIDATION_FAILED'):
        runner.run_node({'node_id': NODES[-1]}, run, AgentState())
    assert not submissions


@pytest.mark.parametrize('bad,code', [('not JSON', 'STRUCTURED_OUTPUT_INVALID'), ('banned', 'ANSWER_VALIDATION_FAILED')])
def test_bad_model_output_fails_without_submit(soothe_environment, bad, code):
    _, run, _ = soothe_environment
    draft = reply_draft()
    if bad == 'banned':
        draft['replies'][0]['text'] = '别生气了'
    else:
        draft = bad
    runner, client, _, submissions = make_runner(soothe_environment, draft=draft)
    with client:
        runner.run_node({'node_id': NODES[0]}, run, AgentState())
        with pytest.raises(WorkflowNodeError, match=code):
            runner.run_node({'node_id': NODES[1]}, run, AgentState())
    assert not submissions


def test_already_submitted_reuses_receipt(soothe_environment):
    _, run, _ = soothe_environment
    runner, client, sent, submissions = make_runner(soothe_environment, committed=True)
    with client:
        walk(runner, run)
    assert not submissions and sent[-1][0] == 'soothe.get_turn_result'


def test_submit_409_unknown_recovery_only_queries_and_never_resends(soothe_environment):
    session, run, _ = soothe_environment
    runner, client, sent, submissions = make_runner(soothe_environment, reject_submit=True)
    with client:
        for _ in range(2):
            with pytest.raises(WorkflowNodeError, match='TOOL_OUTCOME_UNKNOWN'):
                walk(runner, run)
    assert not submissions
    assert sum(name == 'soothe.submit_turn_result' for name, _ in sent) == 1
    assert session.scalar(select(AgentToolCall)).status == 'outcome_unknown'
