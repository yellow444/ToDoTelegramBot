"""Распознавание содержимого медиа-задач: голос/видео → текст, картинка → текст.

Карточка задачи — копия оригинала от имени бота: подпись = ваш текст + распознанное + даты и #id.
Если текст не влезает в подпись (или у медиа подписи не бывает, как у кружков), полный текст
приходит ответом под карточкой.
"""

import asyncio
import logging
import tempfile
from pathlib import Path

from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from ..media import AUDIO_TYPES, NATIVE_TG_TRANSCRIBE, extract_voice
from ..models import Task, utcnow
from . import cards, ui
from .handlers import deps

logger = logging.getLogger(__name__)

OCR_PROMPT = (
    "Распознай весь текст на изображении дословно, сохраняя порядок строк. "
    "Если текста нет — одним предложением опиши, что изображено. Без пояснений от себя."
)


def needs_extraction(t: Task) -> bool:
    if t.media_type in AUDIO_TYPES or t.media_type == "photo":
        return True
    mime = t.mime or ""
    return t.media_type == "document" and mime.split("/")[0] in ("audio", "video", "image")


def _category(t: Task) -> str:
    if t.media_type in AUDIO_TYPES:
        return "audio"
    if t.media_type == "photo":
        return "image"
    return {"audio": "audio", "video": "audio", "image": "image"}.get((t.mime or "").split("/")[0], "")


async def _transcribe(context: ContextTypes.DEFAULT_TYPE, t: Task, path: Path) -> str:
    d = deps(context)
    tg = d.tg_transcriber
    if d.settings.STT_PROVIDER == "telegram" and tg is not None and tg.ready:
        try:
            bot_username = context.bot.username
            if t.media_type in NATIVE_TG_TRANSCRIBE:
                # карточка — копия голосового/кружка: расшифровываем её же
                return await tg.transcribe_bot_message(bot_username, utcnow(), duration=t.media_duration, window=90)
            # видео/аудиофайл: только звуковая дорожка → временное голосовое → расшифровка → удалить
            voice, duration = await asyncio.to_thread(extract_voice, path)  # кодек блокирует loop
            tmp = await context.bot.send_voice(
                t.chat_id, voice, duration=duration, disable_notification=True, caption="🎙 звук для расшифровки…"
            )
            try:
                return await tg.transcribe_bot_message(bot_username, tmp.date, duration=duration)
            finally:
                await cards.safe_delete(context.bot, t.chat_id, tmp.message_id)
        except Exception as exc:
            logger.warning("Расшифровка Telegram не удалась (%s) — запасной способ %s", exc, d.settings.STT_FALLBACK)
            if d.settings.STT_FALLBACK in ("", "off"):
                raise
    if d.transcriber is None or not d.transcriber.enabled:
        raise RuntimeError("расшифровка недоступна")
    return await d.transcriber.transcribe(path, await vocabulary_hint(d, t.chat_id))


async def vocabulary_hint(d, chat_id: int, limit: int = 600) -> str:
    """Подсказка для whisper: словарь пользователя + общий словарь + тексты последних задач."""
    u = await d.service.user(chat_id)
    words = [w.strip() for w in (d.settings.STT_PROMPT or "").split(",") if w.strip()] + list(u.vocabulary)
    recent = [
        t.text for t in await d.service.list_tasks(chat_id, None, 60)
        if t.text and not t.text.startswith("(") and t.extracted_kind != "pending"
    ]
    hint = ", ".join(dict.fromkeys(words))
    for text in recent:
        if len(hint) + len(text) + 2 > limit:
            break
        hint = f"{hint}. {text}" if hint else text
    return hint[:limit]


FIX_PROMPT = """Ты исправляешь ошибки автоматического распознавания русской речи.
Исправь ТОЛЬКО явные ошибки в именах, терминах и словах, используя словарь пользователя.
Не перефразируй, не сокращай, не добавляй ничего от себя. Если ошибок нет — верни текст как есть.
Верни только исправленный текст, без пояснений.

Словарь пользователя: {vocab}"""


async def llm_fix(d, chat_id: int, text: str) -> str:
    """Консервативная автокоррекция расшифровки локальной LLM. При сомнениях возвращает исходник."""
    if not (d.agent and d.settings.STT_LLM_FIX and text.strip()):
        return text
    vocab = await vocabulary_hint(d, chat_id, limit=1500)
    if not vocab:
        return text
    try:
        msg = await d.agent.client.chat(
            [{"role": "system", "content": FIX_PROMPT.format(vocab=vocab)}, {"role": "user", "content": text}],
            temperature=0,
            max_tokens=max(200, len(text) * 2),
        )
    except Exception as exc:
        logger.warning("llm_fix: %s", exc)
        return text
    fixed = (msg.get("content") or "").strip().strip('"«»')
    # страховка от «творчества»: исправленный текст должен быть близок к исходному по длине
    if not fixed or abs(len(fixed) - len(text)) > max(15, len(text) * 0.3):
        return text
    return fixed


async def _ocr(context: ContextTypes.DEFAULT_TYPE, t: Task, path: Path) -> str:
    d = deps(context)
    if not (d.agent and d.settings.OCR_MODEL):
        raise RuntimeError("OCR недоступен (нужны LLM_BASE_URL и OCR_MODEL)")
    mime = t.mime if (t.mime or "").startswith("image/") else "image/jpeg"
    return await d.agent.client.vision(path.read_bytes(), OCR_PROMPT, d.settings.OCR_MODEL, mime)


async def process(context: ContextTypes.DEFAULT_TYPE, task_id: str) -> None:
    """Фоновая обработка медиа-задачи (запускается из on_message)."""
    d = deps(context)
    t = await d.service.repo.get_task(task_id)
    if t is None or not t.file_id:
        return
    category = _category(t)
    text, kind = None, "failed"
    try:
        tg_file = await context.bot.get_file(t.file_id)
        if tg_file.file_size and tg_file.file_size > d.settings.STT_MAX_MB * 1024 * 1024:
            raise RuntimeError(f"файл больше {d.settings.STT_MAX_MB} МБ — Bot API не отдаёт такие")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / (t.file_name or f"{t.media_type}.bin")
            await tg_file.download_to_drive(path)
            if category == "audio":
                raw = await _transcribe(context, t, path)
                text, kind = await llm_fix(d, t.chat_id, raw), "transcript"
                if text != raw:
                    await d.service.repo.update_task(t.id, extracted_raw=raw)
            elif category == "image":
                text, kind = await _ocr(context, t, path), "ocr"
    except Exception as exc:
        logger.warning("Распознавание %s (%s) не удалось: %s", t.id, t.media_type, exc)
        text = None

    t = await d.service.apply_extracted(t, text, kind)
    u = await d.service.user(t.chat_id)
    if kind == "transcript" and text and d.agent and not t.caption_text:
        t = await split_into_tasks(context, t, text) or t
    message_id = await cards.refresh_card(context.bot, t, u, d.settings.WEBAPP_URL)
    if message_id != t.message_id:
        t = await d.service.repo.update_task(t.id, message_id=message_id) or t
    if t.extracted_text and ui.card_truncated(t):
        try:
            first_id = None
            for chunk in ui.extracted_message(t, u):
                msg = await context.bot.send_message(
                    t.chat_id, chunk, reply_to_message_id=t.message_id if first_id is None else None,
                    disable_notification=True,
                )
                first_id = first_id or msg.message_id
            await d.service.repo.update_task(t.id, extracted_message_id=first_id)
        except TelegramError as exc:
            logger.warning("Не удалось отправить полный текст %s: %s", t.id, exc)



async def split_into_tasks(context: ContextTypes.DEFAULT_TYPE, t: Task, text: str) -> Task | None:
    """Надиктовано несколько дел: первое остаётся на карточке с голосовым, остальные — отдельными
    карточками ответом на неё. Возвращает обновлённую исходную задачу или None, если дело одно."""
    from ..split import item_phrase, looks_multi, split_llm
    from ..timeparse import parse

    d = deps(context)
    if not looks_multi(text):
        return None
    items = await split_llm(d.agent, t.chat_id, text)
    if not items:
        return None
    u = await d.service.user(t.chat_id)

    first = parse(item_phrase(items[0]), u)
    fields: dict = {"text": first.text, "period": first.period, "idea": first.idea, "nag_count": 0}
    if first.remind_at or first.idea:
        fields.update(scheduled_at=first.remind_at, remind_at=first.remind_at, repeat=first.repeat)
    t = await d.service.repo.update_task(t.id, **fields) or t
    for item in items[1:]:
        sub = await d.service.create(
            t.chat_id, item_phrase(item), parse_text=True, default_remind=True, source="voice-split"
        )
        msg = await context.bot.send_message(
            t.chat_id, ui.render_card(sub, u), parse_mode=ParseMode.HTML,
            reply_markup=ui.card_markup(sub, u, d.settings.WEBAPP_URL),
            reply_to_message_id=t.message_id, disable_notification=True,
        )
        await d.service.repo.update_task(sub.id, message_id=msg.message_id)
    logger.info("Голосовое %s разбито на %d задач", t.id, len(items))
    return t
