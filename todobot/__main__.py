"""Запуск: бот (long polling) + веб (Mini App, /mcp) в одном event loop.

    python -m todobot
"""

import asyncio
import logging

import uvicorn

from . import tools
from .config import get_settings
from .llm import Agent, LLMClient
from .service import TaskService
from .storage import create_repository
from .watchdog import LoopWatchdog

logger = logging.getLogger("todobot")


async def run() -> None:
    settings = get_settings()
    repo = create_repository(settings)
    await repo.init()
    service = TaskService(repo, settings)
    tools.bind(service)

    llm = LLMClient(settings) if settings.llm_enabled else None
    agent = Agent(llm, service) if llm else None
    logger.info("LLM: %s", f"{settings.LLM_BASE_URL} ({settings.LLM_MODEL})" if llm else "выключена")

    bot_app = None
    if settings.RUN_BOT:
        if not settings.TOKEN:
            raise SystemExit("TOKEN не задан")
        from .bot.app import build_bot, on_startup
        from .bot.handlers import Deps

        from .stt import Transcriber
        from .userbot import TelegramTranscriber

        tg_transcriber = TelegramTranscriber(settings)
        try:
            await tg_transcriber.start()
        except Exception:
            logger.exception("Userbot не запустился — расшифровка будет локальной")
        bot_app = build_bot(Deps(settings, service, agent, Transcriber(settings), tg_transcriber))

    server = None
    if settings.WEB_ENABLED:
        from .mcp_server import build_mcp
        from .web.app import build_web

        web = build_web(service, settings, agent, build_mcp())
        server = uvicorn.Server(
            uvicorn.Config(
                web, host=settings.WEB_HOST, port=settings.WEB_PORT, log_level="info", proxy_headers=True,
                timeout_keep_alive=settings.WEB_KEEP_ALIVE,
            )
        )

    watchdog = LoopWatchdog(settings.STALL_WARN_SECONDS) if settings.STALL_WARN_SECONDS > 0 else None
    if watchdog:
        watchdog.start()
    try:
        if bot_app:
            await bot_app.initialize()
            await on_startup(bot_app)
            await bot_app.start()
            await bot_app.updater.start_polling(allowed_updates=["message", "callback_query"])
            logger.info("Бот запущен: @%s", bot_app.bot.username)
        if server:
            await server.serve()  # до SIGINT/SIGTERM
        else:
            await asyncio.Event().wait()
    finally:
        if watchdog:
            await watchdog.stop()
        if bot_app:
            if bot_app.updater.running:
                await bot_app.updater.stop()
            if bot_app.running:
                await bot_app.stop()
            await bot_app.shutdown()
        if bot_app and bot_app.bot_data["deps"].tg_transcriber:
            await bot_app.bot_data["deps"].tg_transcriber.stop()
        if llm:
            await llm.close()
        await repo.close()


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
