"""Клиент OpenAI-совместимого chat/completions (llm-gateway, llama.cpp, Ollama, OpenRouter...)."""

import logging
import re

import httpx

from ..config import Settings

logger = logging.getLogger(__name__)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = settings.LLM_MODEL
        self._http = httpx.AsyncClient(
            base_url=settings.LLM_BASE_URL.rstrip("/"),
            timeout=settings.LLM_TIMEOUT,
            headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"},
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.3,
        max_tokens: int | None = None,
        json_mode: bool = False,
        model: str | None = None,
    ) -> dict:
        body: dict = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens or self.settings.LLM_MAX_TOKENS,
            **self.settings.LLM_EXTRA_BODY,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            r = await self._http.post("/chat/completions", json=body)
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM недоступна: {exc}") from exc
        if r.status_code >= 400:
            raise LLMError(f"LLM ответила {r.status_code}: {r.text[:300]}")
        data = r.json()
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Неожиданный ответ LLM: {str(data)[:300]}") from exc
        if msg.get("content"):
            msg["content"] = _THINK.sub("", msg["content"]).strip()
        logger.debug("LLM %s via %s", body["model"], r.headers.get("x-provider"))
        return msg

    async def vision(self, image: bytes, prompt: str, model: str, mime: str = "image/jpeg") -> str:
        """Запрос к vision-модели (OCR / описание картинки)."""
        import base64

        url = f"data:{mime};base64,{base64.b64encode(image).decode()}"
        msg = await self.chat(
            [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": url}},
            ]}],
            temperature=0,
            max_tokens=2000,
            model=model,
        )
        return (msg.get("content") or "").strip()

    async def list_models(self) -> list[dict]:
        """Модели шлюза: [{'id', 'tags', 'owned_by'}]. Пустой список, если endpoint не поддерживается."""
        try:
            r = await self._http.get("/models")
            r.raise_for_status()
            return r.json().get("data", [])
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("list_models: %s", exc)
            return []
