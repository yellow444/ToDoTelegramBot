"""Расшифровка голоса/аудио/видео.

local  — faster-whisper прямо в процессе бота (CPU, бесплатно). Звук из видео достаёт PyAV, ffmpeg не нужен.
openai — любой OpenAI-совместимый POST /audio/transcriptions (whisper.cpp server, speaches, llm-gateway…).

У Bot API нет метода «расшифровать голосовое» (это функция Telegram Premium в клиентах), поэтому
расшифровка своя.
"""

import asyncio
import logging
from pathlib import Path

import httpx

from .config import Settings

logger = logging.getLogger(__name__)


class STTError(RuntimeError):
    pass


class Transcriber:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._model = None
        self._lock = asyncio.Lock()

    @property
    def provider(self) -> str:
        """Свой способ: основной, если он local/openai, иначе запасной (STT_FALLBACK)."""
        p = self.s.STT_PROVIDER
        return p if p in ("local", "openai") else self.s.STT_FALLBACK

    @property
    def enabled(self) -> bool:
        return self.provider in ("local", "openai")

    async def transcribe(self, path: Path, hint: str = "") -> str:
        """hint — подсказка-словарь (имена, термины): whisper охотнее пишет знакомые слова правильно."""
        if self.provider == "openai":
            return await self._openai(path, hint)
        if self.provider == "local":
            async with self._lock:  # одна расшифровка за раз — не душим CPU
                return await asyncio.to_thread(self._local, path, hint)
        raise STTError("Расшифровка выключена (STT_PROVIDER=off)")

    def _local(self, path: Path, hint: str = "") -> str:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise STTError("faster-whisper не установлен") from exc
        if self._model is None:
            logger.info("Загружаю модель распознавания %s (%s)…", self.s.STT_MODEL, self.s.STT_DEVICE)
            compute = "int8" if self.s.STT_DEVICE == "cpu" else "float16"
            self._model = WhisperModel(self.s.STT_MODEL, device=self.s.STT_DEVICE, compute_type=compute)
        from .media import decode_pcm16k

        # декодируем сами: встроенный декодер faster-whisper несовместим с новыми PyAV
        audio = decode_pcm16k(path)
        segments, _info = self._model.transcribe(
            audio, language=self.s.STT_LANGUAGE or None, vad_filter=True, beam_size=5,
            initial_prompt=hint or None,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()

    async def _openai(self, path: Path, hint: str = "") -> str:
        if not self.s.STT_BASE_URL:
            raise STTError("STT_BASE_URL не задан")
        data = {"model": self.s.STT_MODEL}
        if self.s.STT_LANGUAGE:
            data["language"] = self.s.STT_LANGUAGE
        if hint:
            data["prompt"] = hint
        async with httpx.AsyncClient(timeout=600) as client:
            with path.open("rb") as f:
                r = await client.post(
                    self.s.STT_BASE_URL.rstrip("/") + "/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self.s.STT_API_KEY}"},
                    data=data,
                    files={"file": (path.name, f)},
                )
        if r.status_code >= 400:
            raise STTError(f"STT ответил {r.status_code}: {r.text[:200]}")
        return (r.json().get("text") or "").strip()
