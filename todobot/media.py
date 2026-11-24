"""Работа с медиа: звуковая дорожка из видео/аудио → OGG/Opus (формат голосовых Telegram)."""

import io
from fractions import Fraction
from pathlib import Path

AUDIO_TYPES = {"voice", "video_note", "audio", "video"}  # есть что расшифровывать
NATIVE_TG_TRANSCRIBE = {"voice", "video_note"}  # Telegram расшифровывает напрямую
IMAGE_TYPES = {"photo"}


def extract_voice(src: Path, max_seconds: int = 3600) -> tuple[bytes, int]:
    """Берёт только звук из файла и кодирует как голосовое (Opus 48 кГц моно в OGG). → (байты, длительность, с)."""
    import av

    out_buf = io.BytesIO()
    with av.open(str(src)) as inp:
        if not inp.streams.audio:
            raise ValueError("В файле нет звуковой дорожки")
        in_stream = inp.streams.audio[0]
        with av.open(out_buf, "w", format="ogg") as out:
            stream = out.add_stream("libopus", rate=48000)
            stream.layout = "mono"
            stream.bit_rate = 32000
            resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
            samples = 0
            for frame in inp.decode(in_stream):
                frame.pts = None
                for rf in resampler.resample(frame):
                    samples += rf.samples
                    rf.time_base = Fraction(1, 48000)
                    for packet in stream.encode(rf):
                        out.mux(packet)
                if samples > max_seconds * 48000:
                    break
            for rf in resampler.resample(None):
                samples += rf.samples
                for packet in stream.encode(rf):
                    out.mux(packet)
            for packet in stream.encode(None):
                out.mux(packet)
    return out_buf.getvalue(), max(1, round(samples / 48000))


def decode_pcm16k(src: Path):
    """Звуковая дорожка → numpy float32, 16 кГц моно (вход для whisper). Работает и для видео."""
    import av
    import numpy as np

    chunks = []
    with av.open(str(src)) as inp:
        if not inp.streams.audio:
            raise ValueError("В файле нет звуковой дорожки")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in inp.decode(inp.streams.audio[0]):
            frame.pts = None
            for rf in resampler.resample(frame):
                chunks.append(rf.to_ndarray().reshape(-1))
        for rf in resampler.resample(None):
            chunks.append(rf.to_ndarray().reshape(-1))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0
