"""A terminal test failure is diagnostic data, never proof of no shell effects."""
from copy import deepcopy
import shlex
import sys
from types import SimpleNamespace

import pytest

from runtime.context import ContextBuilder
from runtime.context.budget import ContextBudgeter
from runtime.execution.budget import ExecutionCancelled, ExecutionEffectUncertain
from runtime.execution.reasoning_loop import ReasoningLoop
from runtime.runtime import Runtime, RunContext
from runtime.sessions.session import Session
from tests.fakes import make_agent_spec
from tests.fakes.scripted_model import FinalResponse, ScriptedModel, ToolResponse
from models.provider import LLMResponse, ToolCall
from tools.command import run_command
from tools.executor import ToolExecutor, ToolHandlerOutput
from tools.tool_registry import build_lead_tool_registry


def test_actual_pytest_failure_can_be_diagnosed_then_fixed_and_retested(tmp_path):
    (tmp_path / 'value.py').write_text('VALUE = 0\n')
    (tmp_path / 'test_value.py').write_text('from value import VALUE\n\ndef test_value():\n    assert VALUE == 100\n')
    command = f'{shlex.quote(sys.executable)} -m pytest -q'
    provider = ScriptedModel([
        ToolResponse('bash', {'command': command}, call_id='initial-test'),
        ToolResponse('write_file', {'path': 'value.py', 'content': 'VALUE = 100\n'}, call_id='fix'),
        ToolResponse('bash', {'command': command}, call_id='retest'),
        FinalResponse('Done after observing failure and successful retest.'),
    ])
    session = Session('offline:debug-test-failure', metadata={'workspace_root': str(tmp_path)})
    runtime = Runtime(tools=build_lead_tool_registry(), provider=provider, model='scripted',
        tool_executor=ToolExecutor([]), max_tokens=200, max_reasoning_steps=5,
        context_builder=ContextBuilder(budgeter=ContextBudgeter(enabled=False)))
    runtime.run(make_agent_spec('coding', 'Debug the failing test.', 'coding'), 'Fix the failing test.', RunContext(session=session))
    assert len(provider.calls) == 4
    results = [m for m in session.messages if m.get('role') == 'tool']
    assert [m['status'] for m in results] == ['error', 'success', 'success']
    assert results[0]['metadata']['exit_code'] == 1
    assert results[0]['metadata']['effect_state'] == 'unknown'
    assert results[0]['metadata']['tool_side_effect'] is True
    assert results[2]['metadata']['exit_code'] == 0
    assert 'successful retest' in session.messages[-1]['content']


@pytest.mark.parametrize('command', [
    'python -m pytest; touch after', 'python -m pytest && touch after',
    'python -m pytest | tee out', 'python -m pytest > out',
    'python -m pytest $(touch unexpected)', 'python -m pytest `touch unexpected`',
    'python -m pytest\ntouch after', 'env python -m pytest',
    'python -c "raise SystemExit(1)"', 'touch changed; exit 1',
])
def test_compound_shell_or_non_test_error_is_not_a_diagnostic_exception(command):
    from tools.command import completed_diagnostic_failure
    output = _diagnostic_result(command=command)
    assert not completed_diagnostic_failure(output)


def _diagnostic_result(**overrides):
    metadata = {'command': 'python -m pytest -q', 'diagnostic_failure': 'pytest_tests_failed',
                'process_exited': True, 'process_group_exited': True, 'output_complete': True}
    metadata.update(overrides.pop('metadata', {}))
    if 'command' in overrides:
        metadata['command'] = overrides.pop('command')
    kwargs = dict(status='error', error_type='CommandExitError', exit_code=1,
                  effect_state='unknown', metadata=metadata)
    kwargs.update(overrides)
    return ToolHandlerOutput('test failure', **kwargs)


@pytest.mark.parametrize('overrides,expected', [
    ({'status': 'cancelled', 'error_type': 'CommandCancelled'}, ExecutionCancelled),
    ({'status': 'timeout', 'error_type': 'CommandTimeout'}, ExecutionEffectUncertain),
    ({'status': 'unknown', 'error_type': 'CommandExitUnconfirmed'}, ExecutionEffectUncertain),
    ({'error_type': 'UnfinishedCommandChildren'}, ExecutionEffectUncertain),
    ({'metadata': {'process_exited': False}}, ExecutionEffectUncertain),
    ({'metadata': {'process_group_exited': False}}, ExecutionEffectUncertain),
    ({'metadata': {'output_complete': False}}, ExecutionEffectUncertain),
    ({'metadata': {'managed_descendants_exited': False}}, ExecutionEffectUncertain),
    ({'exit_code': -9}, ExecutionEffectUncertain),
    ({'exit_code': 2}, ExecutionEffectUncertain),
    ({'effect_state': 'partial'}, ExecutionEffectUncertain),
    ({'metadata': {'diagnostic_failure': ''}}, ExecutionEffectUncertain),
    ({'command': 'touch changed; exit 1'}, ExecutionEffectUncertain),
])
def test_uncertain_or_interrupted_command_still_stops_pending_tool_batch(overrides, expected):
    result = _diagnostic_result(**deepcopy(overrides))
    executed = []
    class Tools:
        def execute(self, name, args, **kwargs):
            executed.append(name)
            assert len(executed) == 1
            return result
    session = Session('offline:diagnostic-boundary')
    loop = ReasoningLoop(tools=Tools(), tool_executor=ToolExecutor([]))
    response = LLMResponse(None, tool_calls=[
        ToolCall('test', 'bash', {'command': result.metadata['command']}),
        ToolCall('write', 'write_file', {'path': 'never', 'content': 'never'}),
    ])
    with pytest.raises(expected):
        loop._execute_tool_calls(session, response, make_agent_spec('coding', 'coding', 'coding'))
    assert executed == ['bash']
    assert session.messages[-1]['metadata']['effect_state'] == 'not_started'


def test_plain_failed_write_keeps_unknown_effect_and_no_diagnostic_flag(tmp_path):
    from tools.command import completed_diagnostic_failure
    result = run_command('printf changed > partial-write; exit 1', cwd=tmp_path)
    assert (tmp_path / 'partial-write').read_text() == 'changed'
    assert result.status == 'error' and result.effect_state == 'unknown'
    assert not completed_diagnostic_failure(result)


def test_repeating_failed_test_without_new_work_still_hits_side_effect_recovery_gate(tmp_path):
    (tmp_path / 'test_failure.py').write_text('def test_fails():\n    assert False\n')
    command = f'{shlex.quote(sys.executable)} -m pytest -q'
    provider = ScriptedModel([
        ToolResponse('bash', {'command': command}, call_id='first'),
        ToolResponse('bash', {'command': command}, call_id='blind-retry'),
        FinalResponse('Must not reach a success claim.'),
    ])
    session = Session('offline:do-not-auto-retry', metadata={'workspace_root': str(tmp_path)})
    from tools.hooks import ToolLoopGuardHook
    runtime = Runtime(tools=build_lead_tool_registry(), provider=provider, model='scripted',
        tool_executor=ToolExecutor([ToolLoopGuardHook(repeat_limit=2)]), max_tokens=200, max_reasoning_steps=5,
        context_builder=ContextBuilder(budgeter=ContextBudgeter(enabled=False)))
    runtime.run(make_agent_spec('coding', 'coding', 'coding'), 'Diagnose the test.', RunContext(session=session))
    assert len(provider.calls) == 2
    results = [m for m in session.messages if m.get('role') == 'tool']
    assert [m['status'] for m in results] == ['error', 'denied']
    assert results[-1]['metadata']['effect_state'] == 'not_started'
    assert provider.responses and isinstance(provider.responses[0], FinalResponse)
