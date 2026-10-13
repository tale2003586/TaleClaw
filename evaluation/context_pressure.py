"""Independent checks for the synthetic context-pressure diagnostic fixtures."""
from __future__ import annotations
import json
import re
from pathlib import Path

from tools.executor import ToolHook


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


class PressureReceiptHook(ToolHook):
    """Opt-in full receipts for synthetic fixtures; never part of normal traces."""
    name = 'context_pressure_receipt'
    def __init__(self):
        self.records = []
    def matches(self, request):
        return True
    def after(self, request, result):
        origins = []
        if request.tool_name == 'read_artifact' and result.status == 'success':
            try:
                payload = json.loads(result.output)
            except ValueError:
                payload = {}
            references = {match.get('event_ref') for match in payload.get('matches', [])}
            from runtime.context.events import thaw
            for event in getattr(request.session, 'event_log', ()):
                if event.session_id != request.session_id or f'event://{event.event_id}' not in references:
                    continue
                values = set(_strings(thaw(event.payload)))
                for record in self.records:
                    if record['tool_name'] == 'read_file' and record['status'] == 'success' and record['output'] in values:
                        origins.append(record['call_id'])
        self.records.append({'call_id': request.call_id, 'tool_name': request.tool_name,
            'arguments': result.final_arguments, 'status': result.status, 'exit_code': result.exit_code,
            'output': str(result.output), 'metadata': dict(result.metadata),
            'recovered_from_calls': list(dict.fromkeys(origins))})


def inspect_pressure_run(task, row, *, repo_root: Path) -> dict:
    workspace = Path(row['workspace_path'])
    source = repo_root / task.fixture_repo
    allowed = set(task.expected.get('modified', []))
    protected = [p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file()]
    changed = []
    for name in protected:
        target = workspace / name
        if target.is_symlink() or not target.is_file() or target.read_bytes() != (source / name).read_bytes():
            changed.append(name)
    extras = []
    for p in workspace.rglob('*'):
        rel = p.relative_to(workspace)
        if set(rel.parts) & {'.git', '__pycache__', '.pytest_cache'}:
            continue
        if (p.is_file() or p.is_symlink()) and rel.as_posix() not in protected:
            extras.append(rel.as_posix())
    events = [json.loads(line) for line in (Path(row['run_dir']) / 'trace.jsonl').read_text().splitlines() if line.strip()]
    positions = {id(event): i for i, event in enumerate(events)}
    receipt_path = Path(row['run_dir']) / 'pressure_tool_receipts.json'
    receipts = json.loads(receipt_path.read_text())
    receipt_by_id = {r['call_id']: r for r in receipts}
    successful = [e for e in events if e.get('event') == 'tool.call.completed' and e.get('payload', {}).get('status') == 'success']
    reads = []
    for e in successful:
        payload = e['payload']
        if payload.get('tool_name') != 'read_file':
            continue
        receipt = receipt_by_id.get(payload.get('tool_call_id'))
        if not receipt or receipt['status'] != 'success':
            continue
        args = receipt['arguments']
        name = str(args.get('path', '')).removeprefix('./')
        original = source / name
        if name in {f'observations/phase{i}.txt' for i in range(4)} and original.is_file() and args.get('offset', 0) == 0 and all(line in receipt['output'] for line in original.read_text().splitlines()):
            reads.append((name, e.get('step'), payload.get('tool_call_id'), positions[id(e)]))
    required = [f'observations/phase{i}.txt' for i in range(4)]
    # Require distinct model steps: parallel reads do not exercise progressive context pressure.
    ordered = len(reads) >= 4 and [p for p, _, _, _ in reads[:4]] == required and all(isinstance(step, int) for _, step, _, _ in reads[:4]) and all(reads[i][1] < reads[i+1][1] for i in range(3))
    compact_steps = [e.get('step', -1) for e in successful if e['payload'].get('tool_name') == 'compact' and ordered and positions[id(e)] > reads[3][3]]
    compact_after = bool(ordered and compact_steps)
    checks = {
        'only_allowed_files_changed': bool(changed) and set(changed) <= allowed and not extras,
        'sequential_full_log_reads': ordered,
        'compact_after_log_reads': compact_after,
        'independent_tests_pass': row.get('verification', {}).get('outcome') == 'pass',
        'execution_completed': row.get('execution_status') == 'completed',
        'within_step_budget': row.get('within_budget') is True,
    }
    recovery = [e for e in successful if e['payload'].get('tool_name') == 'read_artifact']
    writes = [e.get('step', -1) for e in successful if e['payload'].get('tool_name') in {'edit_file', 'write_file'}]
    if 'repair' in task.id:
        test_receipts = [(positions[id(e)], receipt_by_id.get(e.get('payload', {}).get('tool_call_id'))) for e in events if e.get('event') in {'tool.call.completed', 'tool.call.failed'} and e.get('payload', {}).get('tool_name') == 'bash']
        test_receipts = [(step, r) for step, r in test_receipts if r and r['arguments'].get('command', '').strip() == 'python -m pytest -q']
        write_positions = [positions[id(e)] for e in successful if e['payload'].get('tool_name') in {'edit_file', 'write_file'}]
        checks['failed_test_before_edit'] = bool(write_positions and any(step < min(write_positions) and r['exit_code'] == 1 for step, r in test_receipts))
        checks['successful_test_after_edit'] = bool(write_positions and any(step > max(write_positions) and r['exit_code'] == 0 and r['status'] == 'success' for step, r in test_receipts))
    if 'recovery' in task.id:
        selected = re.search(r'FINAL_SELECTION key=(K\d+)', (source / 'observations/phase3.txt').read_text()).group(1)
        code = re.search(r'record key=' + re.escape(selected) + r' recovery_code=([a-f0-9]+)', (source / 'observations/phase0.txt').read_text()).group(1)
        exact = False
        for e in recovery:
            receipt = receipt_by_id.get(e['payload'].get('tool_call_id'))
            if not receipt or not compact_steps or not writes or not reads:
                continue
            exact |= (min(compact_steps) < e.get('step', -1) < min(writes)
                      and receipt['arguments'].get('artifact_ref') == 'event://session'
                      and selected in receipt['arguments'].get('query', '')
                      and reads[0][2] in receipt.get('recovered_from_calls', [])
                      and selected in receipt['output'] and code in receipt['output'])
        checks['exact_evidence_recovered_after_compact'] = exact

    return {'schema_version': 1, 'passed': all(checks.values()), 'checks': checks,
            'changed_files': sorted(changed), 'extra_files': sorted(extras),
            'successful_recovery_calls': len(recovery),
            'scope': 'Synthetic pressure compliance and independently verified fixture outcome; a recovery call alone does not prove exact recovery.'}


def is_native_capability_unsupported(row: dict) -> bool:
    """Recognize a host capability rejection before any model/tool execution."""
    resolution = (row.get('policy_observation') or {}).get('resolution') or {}
    budget = row.get('execution_budget') or {}
    if (resolution.get('requested') not in (None, 'native')
            or resolution.get('effective') is not None
            or row.get('execution_status') == 'completed' or row.get('run_status') == 'completed'
            or row.get('actual_model_calls') or row.get('tool_calls')
            or row.get('error_type') not in (None, '', 'NativeContextUnsupported')):
        return False
    for key in ('model_attempts', 'tool_attempts', 'model_attempts_in_flight', 'known_tokens'):
        if key in budget and (type(budget[key]) is not int or budget[key] != 0):
            return False
    # Keep explicit host exceptions supported when no budget snapshot exists.
    if row.get('error_type') == 'NativeContextUnsupported':
        return resolution.get('unsupported', True) is True
    if not (resolution.get('requested') == 'native' and 'effective' in resolution
            and resolution['effective'] is None and resolution.get('unsupported') is True):
        return False
    if type(budget.get('model_attempts')) is int and budget['model_attempts'] == 0:
        return True
    # Scripted matrices can reject native before creating a runtime/budget.
    return (not budget and row.get('execution_status') == 'not_run'
            and (row.get('verification') or {}).get('reason') == 'native_capability_unsupported')


def apply_pressure_verification(task, row, *, repo_root: Path) -> dict:
    if is_native_capability_unsupported(row):
        row.setdefault('fixture_verification', dict(row.get('verification') or {}))
        if row.get('pressure_verification') and not row['pressure_verification'].get('skipped'):
            row.setdefault('prior_pressure_verification', row['pressure_verification'])
        detail = {'schema_version': 1, 'passed': None, 'skipped': True,
                  'reason': 'native_capability_unsupported', 'checks': {}}
        row['pressure_verification'] = detail
        row['verification'] = {**(row.get('verification') or {}), 'outcome': 'inconclusive',
                               'reason': 'native_capability_unsupported', 'verifier_id': 'context_pressure.v1'}
        row.update(passed=None, verifier_passed=None, status='inconclusive',
                   failure_category='native_capability_unsupported', failure_reason='native_capability_unsupported')
        if row.get('run_dir'):
            (Path(row['run_dir']) / 'pressure_verification.json').write_text(json.dumps(detail, indent=2) + '\n')
        return row
    detail = inspect_pressure_run(task, row, repo_root=repo_root)
    row['pressure_verification'] = detail
    row['fixture_verification'] = row['verification']
    row['verification'] = {**row['verification'], 'outcome': 'pass' if detail['passed'] else 'fail',
                           'reason': 'context_pressure_checks', 'verifier_id': 'context_pressure.v1'}
    row['passed'] = detail['passed']
    row['verifier_passed'] = detail['passed']
    row['status'] = 'pass' if detail['passed'] else 'fail'
    if not detail['passed']:
        row['failure_category'] = 'context_pressure_checks'
        row['failure_reason'] = ', '.join(name for name, ok in detail['checks'].items() if not ok)
    (Path(row['run_dir']) / 'pressure_verification.json').write_text(json.dumps(detail, indent=2) + '\n')
    return row
