"""Сторож: замечает замирания event loop и пишет в лог, кто виноват — код бота или система.

Отдельный поток следит за пульсом event loop. Если пульса нет дольше порога, после восстановления в лог
попадает: длительность, стоял ли сам поток (тогда замер весь процесс, а не loop), сколько процессора
потратил процесс, сколько контейнер ждал cpu/io/memory (PSI) и стек главного потока во время замирания.
"""

import asyncio
import logging
import sys
import threading
import time
import traceback
from pathlib import Path

logger = logging.getLogger(__name__)
TICK = 0.25
PSI_DIR = Path("/sys/fs/cgroup")


def read_psi(base: Path = PSI_DIR) -> dict[str, float]:
    """Сколько секунд контейнер ждал ресурс с момента старта. Вне Linux/cgroup v2 — пусто."""
    out = {}
    for name in ("cpu", "io", "memory"):
        try:
            some = (base / f"{name}.pressure").read_text(encoding="ascii").splitlines()[0]
            out[name] = int(some.rsplit("total=", 1)[1]) / 1e6
        except (OSError, IndexError, ValueError):
            continue
    return out


def verdict(stall: float, thread_lag: float, cpu: float, psi: dict[str, float], clock_shift: float) -> str:
    """Вывод по замерам."""
    if abs(clock_shift) > 1:
        return "скачок системных часов"
    if cpu > 0.5 * stall:
        return "занят код бота (см. стек)"
    waited = {k: v for k, v in psi.items() if v > 0.5 * stall}
    if waited:
        return "контейнер ждал ресурс: " + ", ".join(waited)
    if thread_lag > 0.5 * stall:
        return "замер весь процесс, ресурсов контейнер не ждал: похоже на паузу VM или хоста"
    return "event loop стоял в блокирующем вызове (см. стек)"


class LoopWatchdog:
    def __init__(self, threshold: float = 1.0) -> None:
        self.threshold = threshold
        self._beat = time.monotonic()
        self._stop = threading.Event()
        self._task: asyncio.Task | None = None
        self._thread: threading.Thread | None = None
        self._main_id = threading.get_ident()

    def start(self) -> None:
        self._main_id = threading.get_ident()
        self._beat = time.monotonic()
        self._task = asyncio.get_running_loop().create_task(self._pulse())
        self._thread = threading.Thread(target=self._watch, name="loop-watchdog", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _pulse(self) -> None:
        while True:
            self._beat = time.monotonic()
            await asyncio.sleep(TICK)

    def _stack(self) -> str:
        frame = sys._current_frames().get(self._main_id)
        return "".join(traceback.format_stack(frame)[-6:]) if frame else ""

    def _snapshot(self) -> tuple[float, float, float, dict[str, float]]:
        return time.monotonic(), time.time(), time.process_time(), read_psi()

    def _watch(self) -> None:
        base = self._snapshot()  # последний замер, когда всё было хорошо
        stalled, stack, lag = False, "", 0.0
        before = time.monotonic()
        while not self._stop.wait(TICK):
            now = time.monotonic()
            step_lag, before = max(0.0, now - before - TICK), now
            alive = now - self._beat <= self.threshold
            if not stalled and alive and step_lag <= self.threshold:
                base = self._snapshot()
                continue
            if not stalled:
                stalled, stack = True, self._stack()
            lag = max(lag, step_lag)
            if alive:  # пульс вернулся
                self._report(base, lag, stack)
                base, stalled, lag = self._snapshot(), False, 0.0
                before = time.monotonic()

    def _report(self, base: tuple, lag: float, stack: str) -> None:
        mono, wall, cpu, psi = self._snapshot()
        stall = mono - base[0]
        delta = {k: v - base[3].get(k, 0.0) for k, v in psi.items()}
        used, shift = cpu - base[2], (wall - base[1]) - stall
        waited = " ".join(f"{k}={v:.1f}с" for k, v in delta.items()) or "н/д"
        logger.warning(
            "Замирание %.1f с: %s. Поток сторожа стоял %.1f с, процессор %.1f с, ожидание %s, сдвиг часов %.1f с\n%s",
            stall, verdict(stall, lag, used, delta, shift), lag, used, waited, shift, stack,
        )
