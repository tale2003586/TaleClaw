import asyncio
import logging
import signal

from dotenv import load_dotenv

from applications.bootstrap import build_runtime
from gateway.feishu import FeishuGateway


async def main_async() -> None:
    load_dotenv(override=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    gateway = FeishuGateway.from_env(build_runtime())
    print(
        "taleclaw Feishu gateway started at "
        f"http://{gateway.host}:{gateway.port}{gateway.callback_path}"
    )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, stop_event.set)
            installed_signals.append(signum)
        except NotImplementedError:
            pass
    gateway_task = asyncio.create_task(
        gateway.run_forever(),
        name="feishu-gateway",
    )
    signal_task = asyncio.create_task(
        stop_event.wait(),
        name="feishu-signal-wait",
    )
    try:
        done, _ = await asyncio.wait(
            {gateway_task, signal_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if gateway_task in done:
            await gateway_task
        else:
            gateway_task.cancel()
            await asyncio.gather(gateway_task, return_exceptions=True)
    finally:
        signal_task.cancel()
        await asyncio.gather(signal_task, return_exceptions=True)
        for signum in installed_signals:
            loop.remove_signal_handler(signum)
        await gateway.close()


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("taleclaw Feishu gateway stopped.")


if __name__ == "__main__":
    main()
