"""QA 节点、内存状态和安全失败的行为测试；正文只用临时随机哨兵。"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agents.qa_agent.runner import QaNodeRunner
from app.agents.qa_agent.state import QaWorkingState
from app.db.sqlalchemy_db import Base
from app.models import AgentDefinition, AgentRun, AgentToolCall
from app.runtime.executor import WorkflowNodeError
from app.runtime.interfaces import LeaseContext
from app.runtime.model_gateway import ModelGatewayResult
from app.runtime.state import AgentState, apply_safe_output
from app.runtime.tool_gateway import BusinessConnector, ToolGateway
from app.services.tool_call_audit_service import ToolCallAuditService


@pytest.fixture
def qa_environment():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        now = datetime.now(UTC)
        run = AgentRun(
            run_id='qa-run', agent_id='qa_agent', agent_version='1.0.0',
            package_digest='sha256:test', contract_version='1.0.0',
            business_type='business_qa', business_id='turn',
            status='running', dispatch_state='claimed',
            input_json={'conversation_id':'conversation', 'turn_id':'turn'},
            authorization_version=1, caller_id='caller', tenant_id='tenant',
            create_idempotency_key='key', callback_target_id='callback',
            business_connector_id='qa_business_backend', trace_id='trace',
            execution_attempt=1, lease_owner='worker', fencing_token=1,
            lease_expires_at=now+timedelta(minutes=5), run_deadline_at=now+timedelta(minutes=10),
        )
        session.add_all([run, AgentDefinition(
            agent_id='qa_agent', version='1.0.0', runtime_type='workflow',
            definition_json={}, package_digest='sha256:test', contract_version='1.0.0',
            status='active', status_changed_at=now, status_changed_by='test', status_change_reason='test',
        )])
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


def _runner(session, run, lease, *, evidence=True, model_output=None, committed=False, model_enabled=True):
    # 随机正文不作为 pytest 参数，失败消息不打印业务载荷。
    markers = tuple('private-' + uuid4().hex.translate(str.maketrans('0123456789', 'ghijklmnop')) for _ in range(3))
    seen = []
    submissions = []
    result_observed = committed

    def handler(request):
        nonlocal result_observed
        name = request.headers['X-Agent-Tool-Name']
        seen.append((name, request.headers.get('Idempotency-Key')))
        if name == 'qa.get_question_context':
            output = {'question':markers[0], 'evidence':[{'source_ref':'source-a', 'text':markers[1]}] if evidence else []}
        elif name == 'qa.get_answer_result':
            output = {'submitted':result_observed}
            if result_observed:
                output['revision'] = 1
        else:
            submissions.append(json.loads(request.content)['input']['document'])
            result_observed = True
            output = {'submitted':True, 'revision':1}
        return httpx.Response(200, json={'output':output})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    gateway = ToolGateway({'qa_business_backend':BusinessConnector('https://8.8.8.8','runtime','key','secret')},client)
    output = model_output if model_output is not None else {'answer':markers[2], 'source_refs':['source-a'] if evidence else [], 'no_evidence':not evidence}
    runner = QaNodeRunner(gateway, ToolCallAuditService(session), ModelBoundary(output) if model_enabled else None)
    runner.bind_lease_context(lease)
    return runner, markers, seen, submissions, client


def test_qa_state_forbids_extra_and_writes_are_atomic():
    state = QaWorkingState()
    with pytest.raises(ValidationError):
        QaWorkingState(authorization_version=1)
    for target, value in [('authorization_version',1),('question_context',{'token':'x'}),('question_context',42)]:
        before = state.model_dump()
        with pytest.raises(ValueError):
            apply_safe_output(state,target,value,{'question_context','answer_draft','submit_result'})
        assert state.model_dump() == before


@pytest.mark.parametrize('evidence',[True,False])
def test_four_nodes_keep_private_bodies_out_of_results_and_audit(qa_environment, caplog, evidence):
    session, run, lease = qa_environment
    runner, markers, seen, submissions, client = _runner(session,run,lease,evidence=evidence)
    runtime_state = AgentState()
    results = []
    with client, caplog.at_level(logging.INFO):
        for node_id in ['load_question_context','compose_answer','validate_answer','submit_answer']:
            results.append(runner.run_node({'node_id':node_id},run,runtime_state))
    assert len(submissions) == 1
    assert submissions[0]['no_evidence'] is (not evidence)
    assert [item[0] for item in seen] == ['qa.get_question_context','qa.get_answer_result','qa.submit_answer']
    audits = session.scalars(select(AgentToolCall)).all()
    assert len(audits) == 1 and audits[0].status == 'succeeded'
    safe = json.dumps([results, runtime_state.model_dump(), [row.input_summary for row in audits], [row.output_summary for row in audits]],default=str) + caplog.text
    assert not any(marker in safe for marker in markers), 'private body escaped QA memory'


@pytest.mark.parametrize('output',[
    {'answer':'safe','source_refs':['forged'],'no_evidence':False},
    {'answer':'safe','source_refs':[],'no_evidence':False},
    {'answer':'safe','source_refs':['source-a'],'no_evidence':False,'run_id':'forged'},
    {'answer':'x'*4001,'source_refs':['source-a'],'no_evidence':False},
    {'answer':'safe','source_refs':[],'no_evidence':True},
])
def test_invalid_answer_never_reaches_submit(qa_environment, output):
    session,run,lease = qa_environment
    runner,_,_,submissions,client = _runner(session,run,lease,model_output=output)
    with client:
        runner.run_node({'node_id':'load_question_context'},run,AgentState())
        with pytest.raises(WorkflowNodeError):
            runner.run_node({'node_id':'compose_answer'},run,AgentState())
    assert not submissions


def test_validate_failure_and_missing_model_are_controlled(qa_environment):
    session,run,lease = qa_environment
    runner,_,_,submissions,client = _runner(session,run,lease)
    with client:
        with pytest.raises(WorkflowNodeError,match='ANSWER_VALIDATION_FAILED'):
            runner.run_node({'node_id':'validate_answer'},run,AgentState())
        runner.run_node({'node_id':'load_question_context'},run,AgentState())
    assert not submissions
    runner_without_model,_,_,_,other_client = _runner(session,run,lease,model_enabled=False)
    with other_client:
        runner_without_model.run_node({'node_id':'load_question_context'},run,AgentState())
        with pytest.raises(WorkflowNodeError,match='MODEL_UNAVAILABLE'):
            runner_without_model.run_node({'node_id':'compose_answer'},run,AgentState())


def test_replayed_submit_queries_same_key_and_does_not_resubmit(qa_environment):
    session,run,lease = qa_environment
    runner,_,seen,submissions,client = _runner(session,run,lease)
    with client:
        for _ in range(2):
            for node_id in ['load_question_context','compose_answer','validate_answer','submit_answer']:
                runner.run_node({'node_id':node_id},run,AgentState())
    assert len(submissions) == 1
    keys = [key for name,key in seen if name != 'qa.get_question_context']
    assert len(set(keys)) == 1 and keys[0]


def test_already_committed_result_reused_even_when_recomputed_answer_changes(qa_environment):
    session,run,lease = qa_environment
    runner,_,seen,submissions,client = _runner(session,run,lease,committed=True)
    with client:
        for node_id in ['load_question_context','compose_answer','validate_answer','submit_answer']:
            runner.run_node({'node_id':node_id},run,AgentState())
    assert not submissions
    assert seen[-1][0] == 'qa.get_answer_result'


def _workflow(session, runner, run):
    from pathlib import Path

    from app.models import AgentPlan
    from app.runtime.artifact import ArtifactStore
    from app.runtime.checkpoint import CheckpointStore, FernetCheckpointCipher
    from app.runtime.executor import WorkflowExecutor
    from app.services.agent_package_service import AgentPackageService

    package = AgentPackageService(Path('app/agents')).load('qa_agent','1.0.0')
    session.add(AgentPlan(plan_id='qa-plan',run_id=run.run_id,strategy='static_workflow',
                          steps_json=[node.model_dump() for node in package.workflow_nodes],
                          stop_conditions_json={},fallback_policy_json={},status='planned'))
    session.commit()
    checkpoints = CheckpointStore(session,FernetCheckpointCipher.generate())
    return WorkflowExecutor(session,runner,checkpoints,ArtifactStore(session)),checkpoints


def test_qa_workflow_checkpoint_artifact_callback_and_store_exclude_bodies(qa_environment,caplog,capsys):
    from app.runtime.executor import _SAFE_CHECKPOINT_KEYS
    from app.services.lease_service import LeaseService

    session,run,lease = qa_environment
    runner,markers,_,submissions,client = _runner(session,run,lease)
    executor,checkpoints = _workflow(session,runner,run)
    with client, caplog.at_level(logging.INFO):
        result = executor.run(run.run_id,lease)
        assert result.status == 'succeeded'
        checkpoint = checkpoints.load_latest(run.run_id,lease)
        assert LeaseService(session).finish(result,lease)
    assert set(checkpoint) <= _SAFE_CHECKPOINT_KEYS
    assert checkpoint['completed_node_ids'] == ['compose_answer','load_question_context','submit_answer','validate_answer']
    # 遍历真实 Runtime 库各表，而不是只检查 Runner 返回，防止 callback/audit 中泄漏。
    persisted = str([session.execute(table.select()).all() for table in Base.metadata.sorted_tables])
    captured = capsys.readouterr()
    safe = persisted + json.dumps(checkpoint) + result.model_dump_json() + caplog.text + captured.out + captured.err
    assert not any(marker in safe for marker in markers), 'private body persisted or printed'
    assert len(submissions) == 1


@pytest.mark.parametrize('status,error_code,expected',[
    ('capability_disabled','MODEL_CAPABILITY_UNAVAILABLE','MODEL_UNAVAILABLE'),
    ('policy_denied','MODEL_CALL_LIMIT_EXCEEDED','MODEL_BUDGET_EXCEEDED'),
])
def test_qa_model_failure_has_explicit_terminal_code(qa_environment,status,error_code,expected):
    session,run,lease = qa_environment
    runner,_,_,submissions,client = _runner(
        session,run,lease,model_output=ModelGatewayResult(status,error_code=error_code),
    )
    executor,_ = _workflow(session,runner,run)
    with client:
        result = executor.run(run.run_id,lease)
    assert result.status == 'failed' and result.error_code == expected
    assert not submissions
