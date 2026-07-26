from __future__ import annotations

import argparse
import json

from applications.minecraft.models import ResourceGoal
from runtime.bootstrap import build_runtime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage TaleClaw Minecraft tasks")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("resource")
    start.add_argument("quantity", type=int)
    start.add_argument("--user-id", default="cli")
    start.add_argument("--session-id", default="cli:minecraft")
    start.add_argument("--bot-id", default="TaleClawBot")
    for name in ("status", "cancel"):
        command = commands.add_parser(name)
        command.add_argument("task_id")
        command.add_argument("--user-id", default="cli")
        command.add_argument("--session-id", default="cli:minecraft")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    runtime = build_runtime()
    app = runtime.minecraft_application
    if app is None:
        raise SystemExit("MINECRAFT_AGENT_ENABLED=1 is required")
    try:
        if args.command == "start":
            value = app.start_task(
                user_id=args.user_id,
                session_id=args.session_id,
                bot_id=args.bot_id,
                goal=ResourceGoal(resource=args.resource, quantity=args.quantity),
            )
        elif args.command == "status":
            value = app.get_status(
                args.task_id,
                user_id=args.user_id,
                session_id=args.session_id,
            )
        else:
            value = {
                "task_id": args.task_id,
                "cancel_requested": app.cancel_task(
                    args.task_id,
                    user_id=args.user_id,
                    session_id=args.session_id,
                ),
            }
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        print(json.dumps(value, ensure_ascii=False))
        return 0
    finally:
        app.close()


if __name__ == "__main__":
    raise SystemExit(main())
