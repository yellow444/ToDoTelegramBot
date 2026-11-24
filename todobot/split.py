"""Несколько задач в одном сообщении: список в тексте или надиктованные подряд дела в голосовом."""

import json
import logging
import re

logger = logging.getLogger(__name__)

BULLET = re.compile(r"^\s*(?:[-–—•*·]|\d{1,2}[.)])\s+")
MULTI_HINT = re.compile(
    r"\b(?:во-первых|во-вторых|в-третьих|первое|второе|третье|ещё|еще|также|потом|затем|и надо|и нужно|плюс)\b",
    re.IGNORECASE,
)

SPLIT_PROMPT = """Пользователь надиктовал или написал планы и идеи для своего списка дел. Раздели на отдельные задачи.
Для каждой задачи:
- "text" — короткая формулировка действия словами пользователя (не выдумывай, не объединяй разные дела);
- "when" — фраза срока ДОСЛОВНО как сказал пользователь («завтра в 10», «на следующей неделе», «в ноябре»,
  «в конце месяца», «в 2027 году», «15 ноября») или null, если срока нет;
- "idea": true — если это идея/желание без срока («было бы классно…», «идея:», «когда-нибудь»).
Если общий срок сказан для группы дел («в ноябре: …, …») — повтори его у каждой задачи группы.
Ответ строго JSON: {"tasks": [{"text": "...", "when": "...или null", "idea": false}]}"""


def split_list(text: str) -> list[str]:
    """Явный список в тексте: ≥2 строк с маркерами «-», «•», «1.» → отдельные задачи."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    items = [BULLET.sub("", ln).strip() for ln in lines if BULLET.match(ln)]
    if len(items) >= 2 and len(items) >= len(lines) - 1:
        return items
    return []


def looks_multi(text: str) -> bool:
    """Стоит ли спрашивать LLM: длинная диктовка или слова-связки «ещё», «потом», «во-вторых»."""
    words = len(text.split())
    return words >= 25 or (words >= 5 and MULTI_HINT.search(text) is not None) or (words >= 8 and text.count(".") >= 2)


async def split_llm(agent, chat_id: int, text: str) -> list[dict]:
    """[{"text", "when"}] через локальную LLM; пустой список — не удалось или задача одна."""
    ctx = await agent._context(chat_id)
    try:
        msg = await agent.client.chat(
            [{"role": "system", "content": SPLIT_PROMPT}, {"role": "user", "content": text}],
            temperature=0,
            max_tokens=1200,
            json_mode=True,
            model=ctx["u"].llm_model or None,
        )
    except Exception as exc:
        logger.warning("split_llm: %s", exc)
        return []
    m = re.search(r"\{.*\}", msg.get("content") or "", re.DOTALL)
    try:
        items = json.loads(m.group(0))["tasks"] if m else []
    except (json.JSONDecodeError, KeyError, TypeError):
        return []
    out = []
    for i in items:
        if not isinstance(i, dict) or not str(i.get("text") or "").strip():
            continue
        when = i.get("when") if i.get("when") not in ("null", "", None) else None
        out.append({"text": str(i["text"]).strip(), "when": str(when) if when else None, "idea": bool(i.get("idea"))})
    return out if len(out) >= 2 else []


def item_phrase(item: dict) -> str:
    """Текст задачи + фраза срока — дальше это разбирает обычный парсер (timeparse)."""
    text = item["text"]
    if item.get("idea") and not item.get("when"):
        return f"идея: {text}"
    return f"{text} {item['when']}" if item.get("when") else text
