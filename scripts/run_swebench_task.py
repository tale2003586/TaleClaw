#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.swebench_adapter import (  # noqa: E402
    DEFAULT_SWEBENCH_DATASET,
    DEFAULT_SWEBENCH_EVAL_ROOT,
    DEFAULT_SWEBENCH_SPLIT,
    DEFAULT_SWEBENCH_WORKSPACE_ROOT,
    load_swebench_instance,
    run_swebench_instance,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one SWE-bench instance through the coding task runtime."
    )
    parser.add_argument("--instance-id", required=True, help="SWE-bench instance id.")
    parser.add_argument("--dataset-name", default=DEFAULT_SWEBENCH_DATASET)
    parser.add_argument("--split", default=DEFAULT_SWEBENCH_SPLIT)
    parser.add_argument("--eval-root", default=str(DEFAULT_SWEBENCH_EVAL_ROOT))
    parser.add_argument("--workspace-root", default=str(DEFAULT_SWEBENCH_WORKSPACE_ROOT))
    parser.add_argument("--model-name", default=None)
    parser.add_argument("--max-reasoning-steps", type=int, default=80)
    parser.add_argument("--reuse-workspace", action="store_true")
    parser.add_argument(
        "--swebench-repo",
        default="/home/tale/kaggle/bench/SWE-bench",
        help="Path to the official SWE-bench repo, used to print/run evaluation command.",
    )
    parser.add_argument(
        "--evaluate",
        action="store_true",
        help="After writing predictions.jsonl, run the official SWE-bench harness.",
    )
    args = parser.parse_args()

    def progress(event: dict) -> None:
        print(json.dumps(event, ensure_ascii=False), flush=True)

    instance = load_swebench_instance(
        dataset_name=args.dataset_name,
        split=args.split,
        instance_id=args.instance_id,
    )
    result = run_swebench_instance(
        instance=instance,
        eval_root=args.eval_root,
        workspace_root=args.workspace_root,
        model_name=args.model_name,
        max_reasoning_steps=args.max_reasoning_steps,
        reuse_workspace=args.reuse_workspace,
        progress=progress,
        swebench_repo=args.swebench_repo,
        evaluate=args.evaluate,
        dataset_name=args.dataset_name,
    )
    summary = {
        "instance_id": result.instance.instance_id,
        "workspace": str(result.workspace),
        "run_dir": str(result.run_dir),
        "predictions_path": str(result.predictions_path),
        "patch_bytes": len(result.model_patch.encode("utf-8")),
        "official_eval_command": result.official_eval_command or [],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
