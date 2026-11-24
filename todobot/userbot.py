"""Расшифровка голосовых средствами Telegram Premium — от имени вашего аккаунта (MTProto, Telethon).

У Bot API нет метода расшифровки: это функция Premium-клиентов (messages.transcribeAudio). Поэтому бот
просит ваш аккаунт расшифровать голосовое/кружок в вашем диалоге с ботом. Для видео и аудиофайлов бот
сначала отправляет их звуковую дорожку временным голосовым.

Нужны TG_API_ID и TG_API_HASH (https://my.telegram.org → API development tools) и однократный вход:
    docker exec -it todobot python -m todobot.userbot login
Сессия хранится в TG_SESSION (по умолчанию data/userbot.session) — это ключ к аккаунту, не публикуйте её.
"""

import asyncio
import logging
from datetime import datetime

from .config import Settings, get_settings

logger = logging.getLogger(__name__)


class TranscribeError(RuntimeError):
    pass


class TelegramTranscriber:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.client = None
        self.ready = False
        self._pending: dict[int, asyncio.Future] = {}

    @property
    def configured(self) -> bool:
        return bool(self.s.TG_API_ID and self.s.TG_API_HASH)

    def _make_client(self):
        from telethon import TelegramClient

        return TelegramClient(self.s.TG_SESSION, self.s.TG_API_ID, self.s.TG_API_HASH, device_model="ToDoTelegramBot")

    async def start(self) -> None:
        if not self.configured:
            return
        from telethon import events
        from telethon.tl.types import UpdateTranscribedAudio

        self.client = self._make_client()
        await self.client.connect()
        if not await self.client.is_user_authorized():
            logger.warning("Userbot не авторизован: выполните `python -m todobot.userbot login` — пока расшифровка локальная")
            await self.client.disconnect()
            self.client = None
            return

        @self.client.on(events.Raw(UpdateTranscribedAudio))
        async def _on_transcribed(update):
            fut = self._pending.get(update.transcription_id)
            if fut and not fut.done() and not update.pending:
                fut.set_result(update.text)

        me = await self.client.get_me()
        self.ready = True
        logger.info("Userbot подключён: %s (premium=%s)", me.username or me.id, getattr(me, "premium", None))

    async def stop(self) -> None:
        if self.client:
            await self.client.disconnect()

    async def transcribe_bot_message(
        self, bot_username: str, sent_at: datetime, duration: int | None, window: float = 5
    ) -> str:
        """Расшифровать голосовое/кружок, который бот только что отправил в ваш диалог с ним."""
        if not self.ready:
            raise TranscribeError("userbot не подключён")
        from telethon.tl.functions.messages import TranscribeAudioRequest

        peer = await self.client.get_input_entity(bot_username)
        target = None
        for _ in range(10):  # сообщение может дойти до аккаунта с задержкой
            for m in await self.client.get_messages(peer, limit=30):
                if m.out or not (m.voice or m.video_note):
                    continue
                if abs((m.date - sent_at).total_seconds()) > window:
                    continue
                if duration and m.file and m.file.duration and abs(m.file.duration - duration) > 1:
                    continue
                target = m
                break
            if target:
                break
            await asyncio.sleep(1)
        if target is None:
            raise TranscribeError("не нашёл голосовое в диалоге с ботом")

        res = await self.client(TranscribeAudioRequest(peer=peer, msg_id=target.id))
        if not res.pending:
            return res.text.strip()
        fut = asyncio.get_running_loop().create_future()
        self._pending[res.transcription_id] = fut
        try:
            return (await asyncio.wait_for(fut, 180)).strip()
        except TimeoutError:
            raise TranscribeError("Telegram не вернул расшифровку за 3 минуты") from None
        finally:
            self._pending.pop(res.transcription_id, None)


async def _login() -> None:
    s = get_settings()
    if not (s.TG_API_ID and s.TG_API_HASH):
        raise SystemExit("Задайте TG_API_ID и TG_API_HASH в .env (https://my.telegram.org → API development tools)")
    t = TelegramTranscriber(s)
    client = t._make_client()
    await client.start()  # спросит телефон, код и пароль 2FA — вводите сами
    me = await client.get_me()
    print(f"Готово: вошли как {me.first_name} (@{me.username}), premium={getattr(me, 'premium', None)}")
    print(f"Сессия: {s.TG_SESSION}. Перезапустите бота, чтобы он начал расшифровывать через Telegram.")
    await client.disconnect()


def main() -> None:
    import sys

    if len(sys.argv) < 2 or sys.argv[1] != "login":
        print(__doc__)
        return
    asyncio.run(_login())


if __name__ == "__main__":
    main()
