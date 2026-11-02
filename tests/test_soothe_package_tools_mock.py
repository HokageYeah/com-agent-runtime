"""Soothe 工具必须遵守冻结 wire、可信引用与幂等边界；只用临时测试正文。"""
from __future__ import annotations

import io
import json
from uuid import uuid4

import httpx
import pytest

from app.agents.registry import get_tool_output_fields, get_tool_profile
from app.core.tool_security import verify_runtime_tool
from app.models import AgentRun, AgentToolCall
from app.runtime.mock_business_server import _handler
from app.runtime.tool_gateway import BusinessConnector, ToolGateway
from app.schemas.agent_package import ToolManifest


def _context(business_type='soothe_reply', turn_id='reply-turn'):
    run = AgentRun(agent_id='soothe_agent', agent_version='1.0.0', run_id='soothe-run',
                   business_type=business_type, business_id=turn_id, trace_id='soothe-trace')
    return {'session_id': 'soothe-session', 'turn_id': turn_id, 'run_id': run.run_id,
            'tool_context': ToolGateway.build_tool_context(run, 'load_turn_context')}


def _manifest(name):
    profile = get_tool_profile('soothe_agent', 'soothe_reply')
    assert profile is not None
    connector, method, path, source, side_effect, cancellation = profile.http_tools[name]
    return ToolManifest(name=name, version='1.0.0', connector_id=connector, method=method,
                        relative_path=path, input_from=source, side_effect=side_effect,
                        cancellation_behavior=cancellation,
                        output_to='turn_context' if name.endswith('get_turn_context') else 'submit_result')


class _Socket:
    def __init__(self, data):
        self.request, self.response = io.BytesIO(data), bytearray()

    def makefile(self, *_args, **_kwargs):
        return self.request

    def sendall(self, data):
        self.response.extend(data)


def _exchange(handler, request):
    wire = f'{request.method} {request.url.raw_path.decode()} HTTP/1.0\r\n'
    wire += ''.join(f'{name}: {value}\r\n' for name, value in request.headers.items())
    sock = _Socket(wire.encode() + b'\r\n' + request.content)
    handler(sock, ('127.0.0.1', 1), object())
    head, body = bytes(sock.response).split(b'\r\n\r\n', 1)
    return httpx.Response(int(head.split()[1]), content=body, request=request)


def test_soothe_two_business_types_share_registered_profile():
    reply = get_tool_profile('soothe_agent', 'soothe_reply')
    assert reply is not None, 'Soothe profile must be registered'
    assert reply is get_tool_profile('soothe_agent', 'soothe_simulator')
    assert reply.wire_versions == {'1.0.0': '1.1.0'}
    assert reply.business_id_field == 'turn_id' and reply.tool_prefix == 'soothe.'
    assert get_tool_output_fields('soothe_agent') == {'turn_context', 'submit_result'}
    assert get_tool_profile('soothe_agent', 'unknown') is None
    assert get_tool_profile('qa_agent', 'soothe_reply') is None


@pytest.mark.parametrize('business_type,turn_id,mode', [
    ('soothe_reply', 'reply-turn', 'reply'),
    ('soothe_simulator', 'simulator-turn', 'simulator'),
])
def test_soothe_mock_round_trip_uses_frozen_payload_signature_and_wire(business_type, turn_id, mode, capsys):
    from app.agents.soothe_agent.tools import submit_turn_result

    handler = _handler('soothe-unit')
    seen = []
    marker = uuid4().hex.translate(str.maketrans('0123456789', 'ghijklmnop'))
    document = {'reply': marker}
    key = 'soothe-stable-key'
    def transport(request):
        if request.method == 'POST':
            verify_runtime_tool(dict(request.headers), 'POST', request.url.path, request.content,
                                {'agent-runtime-harness': {'keys': {'test': 'harness-only-soothe-unit'}}}, 30)
            payload = json.loads(request.content)
            seen.append((request.url.path, set(payload), request.headers.get('Idempotency-Key')))
            assert request.headers['X-Agent-Tool-Contract-Version'] == '1.1.0'
            assert set(payload['context']) == {'agent_id', 'agent_version', 'run_id', 'step_id', 'business_type', 'business_id', 'trace_id'}
            if request.url.path.endswith('submit_turn_result'):
                assert set(payload['input']) == {'session_id', 'turn_id', 'run_id', 'turn_result'}
                matches = payload['input']['turn_result'] == document
                assert matches, 'submission body mismatch'
                assert request.headers['X-Agent-Tool-Attempt'] == '1'
        return _exchange(handler, request)
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        gateway = ToolGateway({'couple_diary_backend': BusinessConnector(
            'https://8.8.8.8', 'agent-runtime-harness', 'test', 'harness-only-soothe-unit')}, client)
        context = _context(business_type, turn_id)
        loaded = gateway.call(_manifest('soothe.get_turn_context'), context)
        assert set(loaded) == {'turn_context'} and loaded['turn_context']['mode'] == mode
        query = _manifest('soothe.get_turn_result')
        assert gateway.call(query, context, idempotency_key=key) == {'submitted': False}
        audit = AgentToolCall(run_id='soothe-run', side_effect=True, tool_attempt=1)
        for _ in range(2):
            assert submit_turn_result(gateway, {**context, 'turn_result': document}, key, audit) == {'submitted': True, 'revision': 1}
        assert gateway.call(query, context, idempotency_key=key) == {'submitted': True, 'revision': 1}
        other = _context(business_type, turn_id + '-other')
        assert gateway.call(query, other, idempotency_key=key) == {'submitted': False}
        state = _exchange(handler, httpx.Request('GET', 'https://8.8.8.8/state')).json()
        assert state['soothe_submissions'] == 1 and state['soothe_context_reads'] == 1
    assert [item[0].rsplit('/', 1)[1] for item in seen] == [
        'soothe.get_turn_context', 'soothe.get_turn_result', 'soothe.submit_turn_result',
        'soothe.submit_turn_result', 'soothe.get_turn_result', 'soothe.get_turn_result']
    captured = capsys.readouterr()
    private_free = marker not in str(state) + captured.out + captured.err
    assert private_free, 'mock retained or printed private body'


@pytest.mark.parametrize('field,value,code', [
    ('session_id', '', 'TOOL_RUNTIME_CONTEXT_INVALID'),
    ('turn_id', 'forged', 'TOOL_CONTEXT_BUSINESS_ID_MISMATCH'),
    ('run_id', 'forged', 'TOOL_CONTEXT_RUN_ID_MISMATCH'),
])
def test_soothe_references_rejected_before_network(field, value, code):
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail('unexpected HTTP'))) as client:
        gateway = ToolGateway({'couple_diary_backend': BusinessConnector('https://8.8.8.8', 'runtime', 'key', 'test-only')}, client)
        with pytest.raises(ValueError, match=f'^{code}$'):
            gateway.call(_manifest('soothe.get_turn_context'), {**_context(), field: value})


def test_soothe_submit_requires_audit_and_idempotency():
    from app.agents.soothe_agent.tools import submit_turn_result

    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail('unexpected HTTP'))) as client:
        gateway = ToolGateway({}, client)
        context = {**_context(), 'turn_result': {}}
        with pytest.raises(ValueError, match='^TOOL_AUDIT_REQUIRED$'):
            submit_turn_result(gateway, context, 'key', None)
        with pytest.raises(ValueError, match='^TOOL_AUDIT_CONTEXT_INVALID$'):
            submit_turn_result(gateway, context, 'key', AgentToolCall(run_id='other', side_effect=True))
        with pytest.raises(ValueError, match='^TOOL_IDEMPOTENCY_KEY_REQUIRED$'):
            submit_turn_result(gateway, context, '', AgentToolCall(run_id='soothe-run', side_effect=True))
