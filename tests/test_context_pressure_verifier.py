import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from evaluation.context_pressure import inspect_pressure_run


def case(tmp_path):
    source = tmp_path / 'repo/fixture'
    source.mkdir(parents=True)
    (source / 'observations').mkdir()
    for i in range(4):
        (source / f'observations/phase{i}.txt').write_text(''.join(f'baseline {line}\n' for line in range(105)))
    (source / 'impl.py').write_text('old\n')
    (source / 'test_impl.py').write_text('protected\n')
    workspace = tmp_path / 'workspace'
    shutil.copytree(source, workspace)
    (workspace / 'impl.py').write_text('fixed\n')
    run = tmp_path / 'run'
    run.mkdir()
    task = SimpleNamespace(id='repair', fixture_repo='fixture', expected={'modified': ['impl.py']})
    events = []
    for i in range(4):
        events.append(event('read_file', i+1, {'path': f'observations/phase{i}.txt'}))
    events.append(event('compact', 5, {'focus': 'repair'}))
    events.append(event('bash', 6, {'command': 'python -m pytest -q'}, status='error'))
    events[-1]['payload']['exit_code'] = 1
    events[-1]['event'] = 'tool.call.failed'
    events.append(event('edit_file', 7, {'path': 'impl.py'}))
    events.append(event('bash', 8, {'command': 'python -m pytest -q'}))
    events[-1]['payload']['exit_code'] = 0
    row = {'workspace_path': str(workspace), 'run_dir': str(run), 'verification': {'outcome': 'pass'}, 'execution_status': 'completed', 'within_budget': True}
    return source, workspace, run, task, events, row


def event(name, step, args, status='success'):
    return {'event': 'tool.call.completed', 'step': step, 'payload': {'tool_call_id': f'{name}-{step}', 'tool_name': name, 'status': status, 'final_arguments_preview': json.dumps(args)}}


def check(data):
    source, workspace, run, task, events, row = data
    events = sorted(events, key=lambda e: e['step'])
    (run / 'trace.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))
    receipts = []
    for e in events:
        p = e['payload']
        args = json.loads(p['final_arguments_preview'])
        output = p.get('output_preview', '')
        if p['tool_name'] == 'read_file':
            lines = (source / args['path']).read_text().splitlines()
            output = '\n'.join(lines[args.get('offset', 0):args.get('limit', len(lines))])
        receipts.append({'call_id': p['tool_call_id'], 'tool_name': p['tool_name'], 'arguments': args, 'status': p['status'], 'exit_code': p.get('exit_code'), 'output': output, 'recovered_from_calls': p.get('recovered_from_calls', [])})
    (run / 'pressure_tool_receipts.json').write_text(json.dumps(receipts))
    return inspect_pressure_run(task, row, repo_root=source.parent)


def test_pressure_independent_success(tmp_path):
    assert check(case(tmp_path))['passed'] is True


@pytest.mark.parametrize('violation', ['test_edit', 'extra_file', 'parallel_reads', 'partial_read', 'failed_read', 'early_compact', 'stopped_execution', 'failed_tests'])
def test_pressure_rejects_false_success(tmp_path, violation):
    data = case(tmp_path)
    source, workspace, run, task, events, row = data
    if violation == 'test_edit':
        (workspace / 'test_impl.py').write_text('')
    elif violation == 'extra_file':
        (workspace / 'secret.txt').write_text('extra')
    elif violation == 'parallel_reads':
        events[1]['step'] = events[0]['step']
    elif violation == 'partial_read':
        events[0]['payload']['final_arguments_preview'] = json.dumps({'path': 'observations/phase0.txt', 'limit': 10})
    elif violation == 'failed_read':
        events[0]['payload']['status'] = 'error'
    elif violation == 'early_compact':
        events[4]['step'] = 1
    elif violation == 'stopped_execution':
        row['execution_status'] = 'stopped'
    else:
        row['verification']['outcome'] = 'fail'
    assert check(data)['passed'] is False


def test_recovery_call_without_exact_source_cannot_pass(tmp_path):
    data = case(tmp_path)
    source, workspace, run, task, events, row = data
    task.id = 'recovery'
    (source / 'observations/phase0.txt').write_text('record key=K078 recovery_code=abc123\n')
    (source / 'observations/phase3.txt').write_text('FINAL_SELECTION key=K078\n')
    for i in (0, 3):
        shutil.copyfile(source / f'observations/phase{i}.txt', workspace / f'observations/phase{i}.txt')
    recovered = event('read_artifact', 6, {'artifact_ref': 'event://session', 'query': 'K078'})
    recovered['payload']['recovered_from_calls'] = [events[0]['payload']['tool_call_id']]
    recovered['payload']['output_preview'] = 'No match found'
    events.append(recovered)
    assert check(data)['passed'] is False
    recovered['payload']['output_preview'] = 'record key=K078 recovery_code=abc123'
    assert check(data)['passed'] is True
    recovered['step'] = 4
    assert check(data)['passed'] is False


def test_test_after_edit_in_same_model_step_uses_actual_tool_order(tmp_path):
    data = case(tmp_path)
    data[4][-1]['step'] = data[4][-2]['step']
    assert check(data)['passed'] is True


def test_compact_after_fourth_read_in_same_model_step_is_valid(tmp_path):
    data = case(tmp_path)
    data[4][4]['step'] = data[4][3]['step']
    assert check(data)['passed'] is True


def native_rejection_row():
    return {
        'status': 'fail', 'passed': False, 'verifier_passed': False,
        'verification': {'outcome': 'fail', 'reason': 'fixture_checks', 'verifier_id': 'fixture.v1'},
        'execution_status': 'stopped', 'run_status': 'stopped', 'error_type': '',
        'execution_budget': {'model_attempts': 0, 'tool_attempts': 0, 'known_tokens': 0},
        'actual_model_calls': [], 'tool_calls': 0,
        'policy_observation': {'resolution': {'requested': 'native', 'effective': None,
            'unsupported': True, 'fallback_reason': 'Native server requires server_compaction and input-token counting'}},
    }


def test_native_host_rejection_skips_pressure_without_changing_fixture_or_trace(tmp_path):
    from copy import deepcopy
    from evaluation.context_pressure import apply_pressure_verification

    row = native_rejection_row()
    row.update(run_dir=str(tmp_path), verifier={'checks': [{'name': 'fixture', 'passed': False}]},
               pressure_verification={'passed': False, 'checks': {'sequential_full_log_reads': False}})
    before = deepcopy(row)
    trace = tmp_path / 'trace.jsonl'
    trace.write_text('original host rejection trace\n')
    result = apply_pressure_verification(None, row, repo_root=tmp_path)
    assert result['verification']['outcome'] == 'inconclusive'
    assert result['verification']['reason'] == 'native_capability_unsupported'
    assert result['fixture_verification'] == before['verification']
    assert result['prior_pressure_verification'] == before['pressure_verification']
    assert result['pressure_verification']['skipped'] is True
    assert result['passed'] is result['verifier_passed'] is None
    for field in ('verifier', 'execution_status', 'run_status', 'execution_budget', 'actual_model_calls', 'policy_observation', 'error_type'):
        assert result[field] == before[field]
    assert trace.read_text() == 'original host rejection trace\n'
    assert json.loads((tmp_path / 'pressure_verification.json').read_text()) == result['pressure_verification']
    assert apply_pressure_verification(None, row, repo_root=tmp_path)['fixture_verification'] == before['verification']


@pytest.mark.parametrize('contradiction', [
    'model_attempt', 'tool_attempt', 'tokens', 'bool_attempts', 'missing_attempts',
    'trace_call', 'completed', 'other_error', 'fallback', 'wrong_request', 'unconfirmed', 'missing_effective',
])
def test_native_unsupported_does_not_hide_execution_or_unconfirmed_capability(contradiction):
    from evaluation.context_pressure import is_native_capability_unsupported

    row = native_rejection_row()
    resolution = row['policy_observation']['resolution']
    if contradiction in {'model_attempt', 'tool_attempt', 'tokens'}:
        row['execution_budget'][{'model_attempt': 'model_attempts', 'tool_attempt': 'tool_attempts', 'tokens': 'known_tokens'}[contradiction]] = 1
    elif contradiction == 'bool_attempts':
        row['execution_budget']['model_attempts'] = False
    elif contradiction == 'missing_attempts':
        row.pop('execution_budget')
    elif contradiction == 'trace_call':
        row['actual_model_calls'] = [{'model': 'real-model'}]
    elif contradiction == 'completed':
        row['execution_status'] = 'completed'
    elif contradiction == 'other_error':
        row['error_type'] = 'ValueError'
    elif contradiction == 'fallback':
        resolution['effective'] = 'adaptive'
    elif contradiction == 'wrong_request':
        resolution['requested'] = 'current'
    elif contradiction == 'unconfirmed':
        resolution['unsupported'] = 1
    elif contradiction == 'missing_effective':
        resolution.pop('effective')
    assert not is_native_capability_unsupported(row)


def test_native_typed_exception_and_scripted_not_run_remain_supported_classifications():
    from evaluation.context_pressure import is_native_capability_unsupported

    assert is_native_capability_unsupported({'error_type': 'NativeContextUnsupported'})
    scripted = native_rejection_row()
    scripted.pop('execution_budget')
    scripted.update(execution_status='not_run', verification={'outcome': 'inconclusive', 'reason': 'native_capability_unsupported'})
    assert is_native_capability_unsupported(scripted)


def test_native_fallback_success_still_undergoes_pressure_checks(tmp_path):
    from evaluation.context_pressure import apply_pressure_verification

    data = case(tmp_path)
    check(data)
    source, _, _, task, _, row = data
    row.update(policy_observation={'resolution': {'requested': 'native', 'effective': 'adaptive', 'unsupported': True}},
               execution_budget={'model_attempts': 8}, error_type='')
    result = apply_pressure_verification(task, row, repo_root=source.parent)
    assert result['verification']['outcome'] == 'pass'
    assert result['verification']['reason'] == 'context_pressure_checks'
    assert not result['pressure_verification'].get('skipped')


def test_trial_summary_counts_host_native_rejection_but_not_fallback_or_real_failure():
    from copy import deepcopy
    from scripts.run_context_policy_trial import summarize_trial

    unsupported = native_rejection_row()
    fallback = deepcopy(unsupported)
    fallback['policy_observation']['resolution']['effective'] = 'adaptive'
    fallback.update(execution_status='completed', verification={'outcome': 'pass'})
    fallback['execution_budget'].update(model_attempts=4, known_tokens=80)
    failure = deepcopy(unsupported)
    failure['execution_budget'].update(model_attempts=1, known_tokens=20)
    failure['error_type'] = 'RuntimeError'
    rows = [unsupported, fallback, failure]
    before = deepcopy(rows)
    summary = summarize_trial({'directory': 'trial', 'cells': [{'rows': rows}]}, route={'mode': 'real'}, limits={})
    assert summary['unsupported_task_runs'] == 1
    assert summary['planned_task_runs'] == 3
    assert summary['completed_task_runs'] == summary['verified_pass'] == 1
    assert summary['known_tokens'] == 100
    assert rows == before
