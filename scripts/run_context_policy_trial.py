"""Run bounded, isolated context-policy diagnostics with an explicitly pinned route.

Defaults to scripted mode (no API). Example real run:
  python scripts/run_context_policy_trial.py --runner real --profile openai_relay \
    --expected-host sub.umapis.com --expected-model gpt-5.6-sol --eval-root /tmp/trial
No production DB is used; model endpoints must already be authorized by the user.
"""
from __future__ import annotations
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlparse

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))


def summarize_trial(result, *, route, limits):
    from evaluation.context_pressure import is_native_capability_unsupported

    rows = [r for c in result['cells'] for r in c['rows']]
    return {'matrix': result['directory'], 'route': route, 'planned_task_runs': len(rows),
        'completed_task_runs': sum(r.get('execution_status') == 'completed' for r in rows),
        'verified_pass': sum(r.get('verification', {}).get('outcome') == 'pass' for r in rows),
        'unsupported_task_runs': sum(is_native_capability_unsupported(r) for r in rows),
        'known_tokens': sum(r.get('execution_budget', {}).get('known_tokens', 0) for r in rows),
        'scope': 'Small synthetic pressure diagnostic, deliberately constrained local context; not a production pass-rate or default-policy promotion study.',
        'limits_per_task': limits}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runner', choices=('scripted', 'real'), default='scripted')
    parser.add_argument('--profile', default='openai_relay')
    parser.add_argument('--expected-host')
    parser.add_argument('--expected-model')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--eval-root', type=Path, default=SOURCE / '.evals/context-pressure')
    parser.add_argument('--task-id')
    parser.add_argument('--policy', action='append', choices=('raw', 'current', 'masking', 'adaptive', 'native'))
    parser.add_argument('--repeat', type=int, default=1)
    args = parser.parse_args(argv)
    if not 1 <= args.repeat <= 2:
        parser.error('This bounded diagnostic allows one or two repetitions.')
    if args.runner == 'real' and (not args.expected_host or not args.expected_model):
        parser.error('Real runs require --expected-host and --expected-model; no implicit destination.')
    output = args.eval_root.resolve()
    if args.runner == 'real':
        from runtime.env_loader import load_dotenv_file
        load_dotenv_file((args.env_file or SOURCE / '.env').resolve(), override=True)
    scope = Path(tempfile.mkdtemp(prefix='context-policy-trial-'))
    os.environ.update({
        'TMPDIR': str(scope), 'PYTHON_DOTENV_DISABLED': '1', 'TRACE_INDEX_ENABLED': '0',
        'CONTEXT_ARTIFACT_ROOT': str(scope / 'artifacts'), 'TOOL_RESULT_STORE_BACKEND': 'artifact',
        'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1',
        'PATH': str(Path(sys.executable).parent) + ':/usr/bin:/bin',
        'DATABASE_URL': 'postgresql://invalid@/disabled?host=/nonexistent/context-policy-no-db',
    })
    tempfile.tempdir = None
    os.chdir(scope)
    import psycopg
    def deny_database(*a, **kw):
        raise RuntimeError('Context pressure trials forbid production database access')
    psycopg.connect = deny_database
    psycopg.Connection.connect = classmethod(deny_database)
    psycopg.AsyncConnection.connect = classmethod(deny_database)

    route = {'mode': args.runner}
    if args.runner == 'real':
        from models.model_pool import ModelPool, build_model_pool_from_env
        from openai import OpenAI
        original = build_model_pool_from_env().profile_named(args.profile)
        if (urlparse(original.base_url).hostname, original.model) != (args.expected_host, args.expected_model):
            raise RuntimeError('Configured profile does not match the explicitly selected destination/model')
        # A deliberately smaller LOCAL window creates pressure; this is not a
        # claim about the provider's actual capacity. Identical in every cell.
        chosen = replace(original, fallbacks=(), context_window_tokens=32000,
                         max_input_tokens=28000, output_reserve_tokens=2048, max_output_tokens=2048)
        locked = ModelPool(profiles={chosen.name: chosen}, default_profile=chosen.name,
            routes={p: (chosen.name,) for p in ('default', 'coding', 'summary', 'compact', 'task_conclusion', 'reflection', 'teammate')},
            client_factory=lambda **kw: OpenAI(**{**kw, 'timeout': 45, 'max_retries': 0}))
        import applications.bootstrap as bootstrap
        bootstrap.get_model_pool = lambda: locked
        route.update(profile=chosen.name, model=chosen.model, endpoint_host=args.expected_host,
                     wire_api=chosen.wire_api, fallbacks=[], local_context_window=32000,
                     local_max_input_tokens=28000, output_reserve_tokens=2048, max_output_tokens=2048)

    from evaluation.context_experiments import run_matrix
    from evaluation.context_pressure import apply_pressure_verification, PressureReceiptHook
    from evaluation.harness import CodingBenchmarkHarness
    class PressureHarness(CodingBenchmarkHarness):
        def __init__(self, **kwargs):
            super().__init__(**kwargs, workspace_root=Path(tempfile.mkdtemp(dir=scope, prefix='cell-')),
                             keep_workspace=True)
        def _extra_tool_hooks(self, workspace):
            self.receipt_hook = PressureReceiptHook()
            return [self.receipt_hook]
        def _provider_for_task(self, task):
            provider, model = super()._provider_for_task(task)
            if self.runner_mode == "scripted":
                provider.context_window_tokens = 32000
                provider.max_input_tokens = 28000
                provider.output_reserve_tokens = 2048
                provider.max_output_tokens = 2048
            return provider, model
        def run_task(self, task, **kwargs):
            row = super().run_task(task, **kwargs)
            (Path(row["run_dir"]) / "pressure_tool_receipts.json").write_text(json.dumps(self.receipt_hook.records, ensure_ascii=False, indent=2))
            row = apply_pressure_verification(task, row, repo_root=SOURCE)
            print(json.dumps({'task': task.id, 'policy': self.context_policy['name'],
                'verification': row['verification']['outcome'], 'execution': row['execution_status'],
                'tokens': row['execution_budget']['known_tokens'],
                'pressure': row.get('pressure_verification'),
                'intervention': row.get('context_intervention')}, ensure_ascii=False), flush=True)
            return row
    policies = args.policy or ['raw', 'current', 'masking', 'adaptive', 'native']
    config = [dict(name=name, recent_groups=1, preview_chars=160, soft_ratio=.45, target_ratio=.20) for name in policies]
    # Limits are per task, cooperative, and may overshoot by one in-flight response.
    limits = dict(max_model_attempts=20, max_tool_attempts=40, max_known_tokens=250000, max_seconds=360)
    output.mkdir(parents=True, exist_ok=True)
    result = run_matrix(policies=config, runner_mode=args.runner,
        benchmark_path=SOURCE / 'benchmarks/context_pressure_tasks.json', eval_root=output,
        task_id=args.task_id, repeat=args.repeat, seed=29, max_reasoning_steps=18,
        limits=limits, harness_factory=PressureHarness)
    summary = summarize_trial(result, route=route, limits=limits)
    (Path(result['directory']) / 'trial-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
