"""QA 包与真实工具入口：正文仅由运行时随机生成，断言失败不回显正文。"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.models import AgentRun
from app.runtime.tool_gateway import BusinessConnector, ToolGateway
from app.services.agent_package_service import AgentPackageService


def _package():
    return AgentPackageService(Path('app/agents')).load('qa_agent', '1.0.0')


def _context():
    run = AgentRun(agent_id='qa_agent', agent_version='1.0.0', run_id='qa-run',
                   business_type='business_qa', business_id='qa-turn', trace_id='qa-trace')
    return {'conversation_id': 'qa-conversation', 'turn_id': 'qa-turn', 'run_id': 'qa-run',
            'tool_context': ToolGateway.build_tool_context(run, 'load_question_context')}


def test_qa_package_loads_with_stable_digest_and_fixed_linear_workflow():
    package = _package()
    assert package.package_digest == _package().package_digest
    assert [node.node_id for node in package.workflow_nodes] == [
        'load_question_context', 'compose_answer', 'validate_answer', 'submit_answer']
    assert all(node.safe_to_rerun and not node.optional for node in package.workflow_nodes)
    assert all(node.node_type != 'bounded_loop' for node in package.workflow_nodes)
    assert package.input_schema['required'] == ['conversation_id', 'turn_id']
    assert package.input_schema['additionalProperties'] is False
    assert package.policy.max_model_calls == 2
    assert package.policy.max_steps == 8
    assert package.policy.max_run_seconds == 120


def test_qa_package_rejects_memoir_output_slot():
    package = _package()
    tools = [tool.model_copy(update={'output_to': 'snapshot'}) for tool in package.tools]
    with pytest.raises(ValidationError):
        type(package).model_validate({**package.model_dump(), 'tools': tools})


def test_qa_gateway_uses_own_references_and_wire_and_rejects_other_namespace():
    sent = []
    def handle(request):
        sent.append(request)
        return httpx.Response(200, json={'output': {'question': '', 'evidence': []}})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        gateway = ToolGateway({'qa_business_backend': BusinessConnector(
            'https://8.8.8.8', 'test-runtime', 'test-key', 'test-secret')}, client)
        context = _context()
        gateway.call(_package().tools[0], context)
        assert json.loads(sent[0].content)['input'] == {
            'conversation_id': 'qa-conversation', 'turn_id': 'qa-turn', 'run_id': 'qa-run'}
        assert sent[0].headers['X-Agent-Tool-Contract-Version'] == '1.1.0'
        memoir = AgentPackageService(Path('app/agents')).load('memoir_agent', '1.0.8')
        with pytest.raises(ValueError, match='^TOOL_MANIFEST_NOT_ALLOWED$'):
            gateway.call(memoir.tools[0], context)
        assert len(sent) == 1


@pytest.mark.parametrize('field,value,error', [
    ('turn_id', 'forged', 'TOOL_CONTEXT_BUSINESS_ID_MISMATCH'),
    ('run_id', 'forged', 'TOOL_CONTEXT_RUN_ID_MISMATCH'),
    ('conversation_id', '', 'TOOL_RUNTIME_CONTEXT_INVALID'),
])
def test_qa_gateway_rejects_forged_references_before_http(field, value, error):
    sent = []
    with httpx.Client(transport=httpx.MockTransport(lambda request: sent.append(request))) as client:
        gateway = ToolGateway({'qa_business_backend': BusinessConnector(
            'https://8.8.8.8', 'test-runtime', 'test-key', 'test-secret')}, client)
        context = {**_context(), field: value}
        with pytest.raises(ValueError, match=f'^{error}$'):
            gateway.call(_package().tools[0], context)
        assert not sent


class _MemorySocket:
    """使用真实 HTTP handler 的内存 socket，避免沙箱回环端口权限影响逻辑覆盖。"""
    def __init__(self, request):
        import io
        self.request = io.BytesIO(request)
        self.response = bytearray()

    def makefile(self, *_args, **_kwargs):
        return self.request

    def sendall(self, data):
        self.response.extend(data)


def _exchange(handler, path, input_data=None, *, key=None, control=None, method='POST'):
    import time

    from app.core.tool_security import tool_signature

    body = b'' if input_data is None else json.dumps({
        'input': input_data, 'context': _context()['tool_context']}).encode()
    timestamp = str(int(time.time()))
    headers = {
        'Content-Length': str(len(body)), 'X-Agent-Runtime-Id': 'agent-runtime-harness',
        'X-Agent-Key-Id': 'test', 'X-Agent-Timestamp': timestamp,
        'X-Agent-Signature': tool_signature(method, path, timestamp, body, 'harness-only-unit'),
        'X-Agent-Tool-Contract-Version': '1.1.0', 'X-Agent-Tool-Attempt': '1',
    }
    if key is not None:
        headers['Idempotency-Key'] = key
    if control is not None:
        headers['X-Harness-Control'] = control
    wire = f'{method} {path} HTTP/1.0\r\n'
    wire += ''.join(f'{name}: {value}\r\n' for name, value in headers.items())
    sock = _MemorySocket(wire.encode() + b'\r\n' + body)
    handler(sock, ('127.0.0.1', 1), object())
    status, response = bytes(sock.response).split(b'\r\n', 1)
    return int(status.split()[1]), json.loads(response.split(b'\r\n\r\n', 1)[1])


def test_qa_mock_queries_and_submits_idempotently_without_retaining_body(capsys):
    from uuid import uuid4

    from app.runtime.mock_business_server import _handler

    handler = _handler('unit')
    refs = {'conversation_id': 'qa-conversation', 'turn_id': 'qa-turn', 'run_id': 'qa-run'}
    query = '/api/v1/internal/agent-tools/qa.get_answer_result'
    submit = '/api/v1/internal/agent-tools/qa.submit_answer'
    code, response = _exchange(handler, query, refs, key='stable-key')
    assert code == 200 and response == {'output': {'submitted': False}}
    code, question = _exchange(handler, '/api/v1/internal/agent-tools/qa.get_question_context', refs)
    assert code == 200
    assert set(question['output']) == {'question', 'evidence'}
    assert set(question['output']['evidence'][0]) == {'source_ref', 'text'}
    sentinel = uuid4().hex
    document = {'answer': sentinel, 'source_refs': ['qa-evidence-1'], 'no_evidence': False}
    for _ in range(2):
        code, response = _exchange(handler, submit, {**refs, 'document': document}, key='stable-key')
        assert code == 200 and response == {'output': {'submitted': True, 'revision': 1}}
    code, response = _exchange(handler, query, refs, key='stable-key')
    assert code == 200 and response == {'output': {'submitted': True, 'revision': 1}}
    code, state = _exchange(handler, '/state', method='GET')
    assert code == 200 and state['qa_submissions'] == 1 and state['qa_context_reads'] == 1
    state_redacted = sentinel not in json.dumps(state)
    assert state_redacted
    captured = capsys.readouterr()
    output_redacted = sentinel not in captured.out + captured.err
    assert output_redacted
    # 同键不同正文按冻结的通用错误码拒绝，不把上次正文或 digest 放进错误。
    code, conflict = _exchange(handler, submit, {**refs, 'document': {'answer': uuid4().hex}}, key='stable-key')
    assert code == 409 and conflict['error_code'] == 'IDEMPOTENCY_CONFLICT'
    error_redacted = sentinel not in json.dumps(conflict)
    assert error_redacted
    # 不同业务轮次/Run 的查询不能命中其它提交。
    code, response = _exchange(handler, query, {**refs, 'conversation_id': 'other'}, key='stable-key')
    assert code == 200 and response == {'output': {'submitted': False}}


def test_qa_mock_block_occurs_after_commit_and_release_unblocks_response():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from uuid import uuid4

    from app.runtime.mock_business_server import _handler

    handler = _handler('unit')
    refs = {'conversation_id': 'qa-conversation', 'turn_id': 'qa-turn', 'run_id': 'qa-run'}
    code, _ = _exchange(handler, '/__harness__/block-next-submit', control='unit')
    assert code == 202
    started = Event()
    def submit():
        started.set()
        return _exchange(handler, '/api/v1/internal/agent-tools/qa.submit_answer',
                         {**refs, 'document': {'answer': uuid4().hex}}, key='stable-key')
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(submit)
        assert started.wait(1)
        # 查询入口由另一个线程请求；阻塞不能锁住已提交结果的读取。
        import time
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            _, state = _exchange(handler, '/state', method='GET')
            if state['qa_submit_started']:
                break
        assert state['qa_submissions'] == 1 and state['qa_submit_started']
        assert not future.done()
        code, response = _exchange(handler, '/api/v1/internal/agent-tools/qa.get_answer_result', refs, key='stable-key')
        assert code == 200 and response['output']['submitted']
        code, _ = _exchange(handler, '/__harness__/release-submit', control='unit')
        assert code == 202
        assert future.result(timeout=1)[0] == 200


def test_qa_submit_requires_matching_physical_attempt_and_sends_attempt_header():
    from app.agents.qa_agent.tools import submit_answer
    from app.models import AgentToolCall

    sent = []
    def handle(request):
        sent.append(request)
        return httpx.Response(200, json={'output': {'submitted': True, 'revision': 1}})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        gateway = ToolGateway({'qa_business_backend': BusinessConnector(
            'https://8.8.8.8', 'test-runtime', 'test-key', 'test-secret')}, client)
        context = {**_context(), 'answer_draft': {'answer': '', 'source_refs': [], 'no_evidence': True}}
        with pytest.raises(ValueError, match='^TOOL_AUDIT_REQUIRED$'):
            submit_answer(gateway, context, 'stable-key', None)
        with pytest.raises(ValueError, match='^TOOL_AUDIT_CONTEXT_INVALID$'):
            submit_answer(gateway, context, 'stable-key', AgentToolCall(
                run_id='other', side_effect=True, tool_attempt=1))
        assert not sent
        call = AgentToolCall(run_id='qa-run', side_effect=True, tool_attempt=2)
        assert submit_answer(gateway, context, 'stable-key', call)['submitted']
        assert sent[0].headers['X-Agent-Tool-Attempt'] == '2'
        assert sent[0].headers['Idempotency-Key'] == 'stable-key'


def test_qa_mock_generated_context_uses_only_letters_and_passes_gateway_scan():
    from app.runtime.mock_business_server import _handler

    handler = _handler('unit')
    refs = {'conversation_id': 'qa-conversation', 'turn_id': 'qa-turn', 'run_id': 'qa-run'}
    code, response = _exchange(handler, '/api/v1/internal/agent-tools/qa.get_question_context', refs)
    assert code == 200
    output = response['output']
    letters_only = output['question'].isalpha() and output['evidence'][0]['text'].isalpha()
    assert letters_only
    ToolGateway._validate_output(output, tool_name='qa.get_question_context')
