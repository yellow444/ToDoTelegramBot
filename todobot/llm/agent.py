"""Ассистент: диалог с памятью + доступ к задачам через инструменты."""

import json
import logging
import re
from dataclasses import dataclass

from .. import tools
from ..models import REPEAT_LABELS
from ..service import TaskService
from ..timeutil import WEEKDAYS_SHORT, local_now, to_local
from .client import LLMClient, LLMError

logger = logging.getLogger(__name__)

MAX_STEPS = 6
MUTATING = {
    "add_task", "complete_task", "reopen_task", "delete_task", "edit_task", "set_reminder", "snooze_task",
    "delegate_to_agent", "add_tasks",
}

SYSTEM = """\
Ты — личный ассистент по задачам и рабочему дню в Telegram. Отвечай по-русски, кратко и по делу.
Сейчас: {now} ({weekday}), часовой пояс {tz}.
Рабочие дни: {workdays}; рабочие часы {work_start}–{work_end}.

Открытые задачи пользователя (id · когда · текст):
{tasks}

Правила:
- Чтобы создать, закрыть, перенести, удалить задачу или поставить напоминание — ОБЯЗАТЕЛЬНО вызывай инструменты,
  не делай вид, что сделал. id бери из списка выше или через search_tasks.
- Время передавай как местное в ISO без зоны: 2026-10-09T10:00. Учитывай рабочий график,
  если пользователь не сказал иного (утро рабочего дня — {work_start}).
- После действий коротко подтверди, что сделано и на когда.
- Ты — быстрый помощник для простого. Сложное поручай коворкерам через delegate_to_agent:
  программирование и правки в проектах, исследование с поиском, длинные многошаговые дела,
  планирование недели/жизни. Claude — по умолчанию, Codex — если просят Codex или для кода.
  Рабочие папки проектов: {workdirs_agents}. Сформулируй задание полно и самодостаточно.
- Если пользователь делится планами или идеями на дни/недели/месяцы/год — добавь их одним вызовом add_tasks,
  срок передавай словами как он сказал («в ноябре», «на следующей неделе»), идеи без срока — idea=true.
- Вопросы вида «что у меня сегодня/завтра/на неделе» — это запрос плана: вызови get_agenda (day) и ответь списком.
- Если просьба неоднозначна — уточни одним вопросом.
- Пиши простым текстом без markdown (никаких *, **, #) — можно эмодзи и нумерованные списки.
"""

EXTRACT = """\
Извлеки задачу из сообщения пользователя. Сейчас {now} ({weekday}), рабочий день начинается в {work_start}.
Верни строго JSON: {{"text": "<суть задачи без слов о времени>", "when": "<YYYY-MM-DDTHH:MM или null>",
"repeat": "<null|daily|workdays|weekly|monthly>"}}"""


@dataclass
class AgentReply:
    text: str
    changed: bool = False


def _tasks_block(tasks, u) -> str:
    if not tasks:
        return "(нет)"
    lines = []
    for t in tasks[:40]:
        when = to_local(t.scheduled_at, u).strftime("%d.%m %H:%M") if t.scheduled_at else "—"
        rep = f" [{REPEAT_LABELS[t.repeat]}]" if t.repeat else ""
        lines.append(f"{t.id} · {when}{rep} · {t.text[:120]}")
    return "\n".join(lines)


class Agent:
    def __init__(self, client: LLMClient, service: TaskService) -> None:
        self.client = client
        self.service = service
        self.tool_schemas = [tools.openai_schema(f) for f in tools.LLM_TOOLS]

    async def _context(self, chat_id: int) -> dict:
        u = await self.service.user(chat_id)
        now = local_now(u)
        return {
            "u": u,
            "now": now.strftime("%Y-%m-%d %H:%M"),
            "weekday": WEEKDAYS_SHORT[now.weekday()],
            "tz": u.tz,
            "workdays": ", ".join(WEEKDAYS_SHORT[d] for d in u.workdays),
            "work_start": u.work_start.strftime("%H:%M"),
            "work_end": u.work_end.strftime("%H:%M"),
            "workdirs_agents": ", ".join(self.service.settings.workdirs) or "не настроены",
        }

    async def ask(self, chat_id: int, text: str, focus: str | None = None) -> AgentReply:
        ctx = await self._context(chat_id)
        tasks = await self.service.list_tasks(chat_id, "open")
        system = SYSTEM.format(tasks=_tasks_block(tasks, ctx["u"]), **ctx)
        history = await self.service.repo.get_history(chat_id, self.service.settings.LLM_HISTORY)
        user_msg = f"{focus}\n\nЗапрос пользователя об этой задаче: {text}" if focus else text
        messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": user_msg}]

        changed = False
        model = ctx["u"].llm_model or self.client.model
        token = tools.current_chat_id.set(chat_id)
        use_tools = True
        answer = ""
        try:
            for _ in range(MAX_STEPS):
                try:
                    msg = await self.client.chat(messages, self.tool_schemas if use_tools else None, model=model)
                except LLMError as exc:
                    if use_tools and "tool" in str(exc).lower():
                        use_tools = False  # модель/провайдер без function calling
                        continue
                    fallback = self.service.settings.LLM_FALLBACK_MODEL
                    if fallback and model != fallback:
                        logger.warning("Модель %s недоступна (%s) — переключаюсь на %s", model, exc, fallback)
                        model = fallback
                        continue
                    raise
                calls = msg.get("tool_calls") or []
                if not calls:
                    answer = msg.get("content") or ""
                    break
                messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
                for call in calls:
                    name = call["function"]["name"]
                    try:
                        args = json.loads(call["function"].get("arguments") or "{}")
                        result = await tools.call(name, args)
                        changed |= name in MUTATING
                    except Exception as exc:  # ошибку возвращаем модели, пусть исправится
                        logger.info("tool %s failed: %s", name, exc)
                        result = {"error": str(exc)}
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id", name),
                            "content": json.dumps(result, ensure_ascii=False, default=str),
                        }
                    )
            else:
                answer = answer or "Не успел закончить — слишком много шагов. Переформулируйте, пожалуйста."
        finally:
            tools.current_chat_id.reset(token)

        answer = answer.strip() or ("Готово." if changed else "…")
        await self.service.repo.add_history(chat_id, "user", user_msg[:4000])
        await self.service.repo.add_history(chat_id, "assistant", answer)
        return AgentReply(answer, changed)

    async def extract_task(self, chat_id: int, text: str) -> dict | None:
        """Разбор новой задачи через LLM: {'text', 'when', 'repeat'} или None."""
        ctx = await self._context(chat_id)
        try:
            msg = await self.client.chat(
                [{"role": "system", "content": EXTRACT.format(**ctx)}, {"role": "user", "content": text}],
                temperature=0,
                max_tokens=300,
                json_mode=True,
            )
        except LLMError as exc:
            logger.warning("extract_task: %s", exc)
            return None
        m = re.search(r"\{.*\}", msg.get("content") or "", re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or not str(data.get("text") or "").strip():
            return None
        if data.get("repeat") not in REPEAT_LABELS:
            data["repeat"] = None
        if data.get("when") in ("null", ""):
            data["when"] = None
        return data

    async def summarize_day(self, chat_id: int, agenda: dict, kind: str) -> str | None:
        """Короткий план на утро / итог вечера на основе данных get_agenda."""
        prompt = (
            "Составь короткий план рабочего дня: что важно сделать в первую очередь, что просрочено. "
            "3–7 пунктов, без воды."
            if kind == "morning"
            else "Подведи короткий итог рабочего дня: что сделано, что осталось, что предложить перенести на завтра. "
            "3–6 пунктов, без воды."
        )
        try:
            msg = await self.client.chat(
                [
                    {"role": "system", "content": "Ты — ассистент по рабочему дню. Отвечай по-русски, кратко."},
                    {"role": "user", "content": prompt + "\nДанные:\n" + json.dumps(agenda, ensure_ascii=False, default=str)},
                ],
                max_tokens=800,
            )
        except LLMError as exc:
            logger.warning("summarize_day: %s", exc)
            return None
        return (msg.get("content") or "").strip() or None
