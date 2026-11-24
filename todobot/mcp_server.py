"""MCP-сервер задач для Claude Code / Codex / любых MCP-клиентов.

Два способа подключения:
  * stdio:  python -m todobot.mcp_server            (или `docker exec -i todobot python -m todobot.mcp_server`)
  * HTTP:   смонтирован в веб-сервер бота по адресу /mcp (streamable HTTP, Bearer MCP_TOKEN)

Сервер работает с той же базой, что и бот. Карточки задач и уведомления в Telegram отправляет
процесс бота (через флаг card_dirty и outbox), поэтому stdio-процессу токен бота не нужен.
"""

import asyncio
import functools
import logging

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import tools

INSTRUCTIONS = """\
Сервер личного таск-трекера пользователя (Telegram-бот ToDo).
- Время без часового пояса трактуется как местное время пользователя (см. get_settings → tz).
- Перед началом работы полезно вызвать get_agenda, чтобы увидеть план дня и рабочие часы.
- Ставь напоминания с учётом рабочего дня; для рабочих задач можно ставить workhours_only=true.
- Планы и идеи пользователя на дни/недели/месяцы/год заноси одним вызовом add_tasks: срок словами
  («завтра в 10», «на следующей неделе», «в ноябре», «в 2027 году»), идеи без срока — idea=true.
- notify шлёт пользователю сообщение в Telegram — используй для важных событий (готово, нужно решение),
  не спамь.
"""


def _expose_errors(fn):
    """Ожидаемые ошибки (ValueError) отдаём клиенту текстом, а не «Error executing tool»."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def build_mcp() -> MCPServer:
    mcp = MCPServer(name="todo-telegram", instructions=INSTRUCTIONS)
    for fn in tools.ALL_TOOLS:
        mcp.add_tool(_expose_errors(fn))
    return mcp


async def _run_stdio() -> None:
    from .config import get_settings
    from .service import TaskService
    from .storage import create_repository

    settings = get_settings()
    repo = create_repository(settings)
    await repo.init()
    tools.bind(TaskService(repo, settings))
    try:
        await build_mcp().run_stdio_async()
    finally:
        await repo.close()


def main() -> None:
    # stdout занят протоколом — логи только в stderr
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(_run_stdio())


if __name__ == "__main__":
    main()
