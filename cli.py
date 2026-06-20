import asyncio
from pathlib import Path

from runtime.bootstrap import build_runtime


async def print_cli_message(message) -> None:
    if message.channel == "cli":
        print(message.content)


async def main_async() -> None:
    print(f"Agent Harness - {Path.cwd()}")
    print("Set coding workspace with: /workspace /path/to/project")
    print("Type 'q' to quit.\n")

    runtime = build_runtime()
    runtime.bus.subscribe_outbound("cli", print_cli_message)
    runtime.start()
    workspace_root: str | None = None

    try:
        while True:
            try:
                query = await asyncio.to_thread(input, "\033[36m>> \033[0m")
            except (EOFError, KeyboardInterrupt):
                print("\nExiting.")
                break

            if not query.strip():
                continue
            if query.strip().lower() in ("q", "quit", "exit"):
                break
            if query.strip().startswith("/workspace "):
                workspace_root = query.strip().split(" ", 1)[1].strip()
                print(f"Workspace set to: {workspace_root}")
                print("---")
                continue

            metadata = {"workspace_root": workspace_root} if workspace_root else None
            await runtime.submit_user_message(query, metadata=metadata)
            await runtime.run_once()
            print("---")
    finally:
        await runtime.stop()


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
