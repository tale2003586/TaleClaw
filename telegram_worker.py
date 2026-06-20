import asyncio
import logging

from dotenv import load_dotenv

from runtime.bootstrap import build_runtime
from gateway.telegram import TelegramGateway


async def main_async() -> None:
    load_dotenv(override=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    gateway = TelegramGateway.from_env(build_runtime())
    print("taleclaw Telegram gateway started.")
    try:
        await gateway.run_forever()
    finally:
        await gateway.close()


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("taleclaw Telegram gateway stopped.")


if __name__ == "__main__":
    main()
