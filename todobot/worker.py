"""Воркер агентов-коворкеров. Запускается на машине, где установлены Claude Code / Codex (по подписке).

Режимы:
  python -m todobot.worker                 фоновый исполнитель: берёт задания из очереди бота и запускает
                                           `claude -p` / `codex exec` в нужной папке, отчёт уходит в Telegram
  python -m todobot.worker --wait claude   ждёт ОДНО задание, забирает его, печатает JSON и выходит —
                                           для «чата-коворкера» в Claude Code / Codex (см. README)

Очередь опрашивается по HTTP без участия LLM, поэтому лимиты подписки тратятся только на реальную работу.
Нужны WORKER_API (адрес веб-сервера бота) и MCP_TOKEN из .env.
"""

import argparse
import asyncio
import json
import logging
import os
import platform
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import httpx

from .config import Settings, get_settings

logger = logging.getLogger("todobot.worker")
POLL_SECONDS = 3
API_KEY_VARS = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY"}

RULES = """\
Правила:
- Делай работу сам и доводи до результата. Уточнить у пользователя сразу не получится — принимай разумные решения.
- Не делай git push, удаление данных и других необратимых действий без явной просьбы в задании.
- Можно пользоваться MCP-сервером todo: ставить пользователю задачи и напоминания (add_task, set_reminder).
- Упоминая задачи пользователя, пиши их #id (например #52be3f): под отчётом появятся кнопки для них.
- В конце дай отчёт для человека, до 15 строк: что сделано, что проверить, открытые вопросы."""


def build_prompt(job: dict, history: list[dict], with_history: bool) -> str:
    parts = [
        "Ты — коворкер пользователя. Это задание из его Telegram-бота задач.",
        f"Рабочая папка: {job.get('workdir_path') or 'не задана (задание без проекта)'}.",
    ]
    if with_history and history:
        parts.append("Предыдущие шаги этого разговора:")
        for i, h in enumerate(history, 1):
            parts.append(f"[{i}] Задание: {h['prompt']}\n[{i}] Твой отчёт: {(h.get('result') or '')[:3000]}")
    parts += ["", "Задание:", job["prompt"], "", RULES]
    return "\n".join(parts)


class Worker:
    def __init__(self, settings: Settings, name: str) -> None:
        self.s = settings
        self.name = name
        self.claim_id = uuid.uuid4().hex
        if not settings.MCP_TOKEN:
            raise SystemExit("MCP_TOKEN не задан в .env — воркеру нечем авторизоваться в API бота")
        self.http = httpx.AsyncClient(
            base_url=settings.WORKER_API.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.MCP_TOKEN}"},
            timeout=30,
        )

    async def claim(self, agents: list[str]) -> dict | None:
        body = {"agents": agents, "worker": self.name, "claim_id": self.claim_id}
        r = await self.http.post("/api/agent/claim", json=body)
        if r.status_code == 204:
            return None
        r.raise_for_status()
        claimed = r.json()
        self.claim_id = uuid.uuid4().hex  # задание получено; до этого повторяем запрос с тем же номером
        return claimed

    async def report(self, job_id: str, ok: bool, result: str, session_id: str | None = None) -> None:
        r = await self.http.post(
            f"/api/agent/jobs/{job_id}/result", json={"ok": ok, "result": result, "session_id": session_id}
        )
        r.raise_for_status()

    def _cwd(self, job: dict) -> str:
        path = job.get("workdir_path") or self.s.AGENT_HOME or str(Path.home())
        if not Path(path).is_dir():
            raise RuntimeError(f"Рабочая папка не найдена: {path}")
        return path

    async def _run(self, args: list[str], cwd: str, stdin_text: str) -> tuple[int, str, str]:
        # Только подписка: ключи платного API не передаём, иначе CLI тарифицирует запросы по API.
        env = {k: v for k, v in os.environ.items() if k not in API_KEY_VARS}
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=cwd, env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin_text.encode("utf-8")), self.s.AGENT_TIMEOUT)
        except TimeoutError:
            proc.kill()
            raise RuntimeError(f"Агент не уложился в {self.s.AGENT_TIMEOUT} с") from None
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    async def run_claude(self, job: dict, history: list[dict]) -> tuple[bool, str, str | None]:
        exe = shutil.which(self.s.CLAUDE_CMD)
        if not exe:
            return False, "Claude Code CLI не найден. Установите Claude Code и войдите по подписке: claude → /login.", None
        session = job.get("session_id")
        args = [exe, "-p", "--output-format", "json", "--permission-mode", self.s.CLAUDE_PERMISSION_MODE]
        if session:
            args += ["--resume", session]
        if self.s.CLAUDE_ALLOWED_TOOLS.strip():
            args += ["--allowedTools", *self.s.CLAUDE_ALLOWED_TOOLS.split()]
        prompt = build_prompt(job, history, with_history=not session)
        code, out, err = await self._run(args, self._cwd(job), prompt)
        try:
            data = json.loads(out.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            return False, f"Claude завершился с кодом {code}: {(err or out)[-1500:]}", None
        result = data.get("result") or ""
        low = result.lower()
        if data.get("is_error") and ("authenticate" in low or "login" in low):
            result += "\n\nПодписка: выполните `claude setup-token` и задайте CLAUDE_CODE_OAUTH_TOKEN для воркера, либо `claude` → /login."
        denials = data.get("permission_denials") or []
        if denials:
            names = sorted({d.get("tool_name", "?") for d in denials})
            result += f"\n\n⚠️ Отклонены инструменты: {', '.join(names)} (CLAUDE_ALLOWED_TOOLS / CLAUDE_PERMISSION_MODE)."
        return not data.get("is_error", False), result, data.get("session_id")

    async def run_codex(self, job: dict, history: list[dict]) -> tuple[bool, str, str | None]:
        exe = shutil.which(self.s.CODEX_CMD)
        if not exe:
            return False, "Codex CLI не найден. Установите: npm i -g @openai/codex и войдите по подписке ChatGPT: codex login.", None
        with tempfile.TemporaryDirectory() as tmp:
            last = os.path.join(tmp, "last.md")
            args = [exe, "exec", "--skip-git-repo-check", "--sandbox", self.s.CODEX_SANDBOX, "--output-last-message", last, "-"]
            code, out, err = await self._run(args, self._cwd(job), build_prompt(job, history, with_history=True))
            result = Path(last).read_text(encoding="utf-8", errors="replace") if os.path.exists(last) else ""
        if not result.strip():
            result = (out or err)[-3000:]
        return code == 0, result, None

    async def handle(self, claimed: dict) -> None:
        job, history = claimed["job"], claimed.get("history") or []
        logger.info("Задание %s → %s: %s", job["id"], job["agent"], job["prompt"][:80])
        try:
            runner = self.run_claude if job["agent"] == "claude" else self.run_codex
            ok, result, session = await runner(job, history)
        except Exception as exc:  # отчёт об ошибке важнее падения воркера
            logger.exception("Задание %s упало", job["id"])
            ok, result, session = False, f"Ошибка воркера: {exc}", None
        await self.report(job["id"], ok, result or "(пустой ответ)", session)
        logger.info("Задание %s: %s", job["id"], "готово" if ok else "ошибка")

    async def serve(self, agents: list[str]) -> None:
        logger.info("Воркер %s: агенты %s, API %s", self.name, agents, self.s.WORKER_API)
        while True:
            try:
                claimed = await self.claim(agents)
            except httpx.HTTPError as exc:
                logger.warning("API бота недоступен: %s", str(exc) or type(exc).__name__)
                await asyncio.sleep(15)
                continue
            if claimed:
                await self.handle(claimed)
            else:
                await asyncio.sleep(POLL_SECONDS)

    async def wait_one(self, agent: str, timeout: float | None) -> dict | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout if timeout else None
        while deadline is None or loop.time() < deadline:
            try:
                claimed = await self.claim([agent])
                if claimed:
                    return claimed
            except httpx.HTTPError as exc:
                logger.warning("API бота недоступен: %s", str(exc) or type(exc).__name__)
            await asyncio.sleep(POLL_SECONDS)
        return None


async def amain() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wait", choices=["claude", "codex"], help="дождаться одного задания и вывести его JSON")
    ap.add_argument("--timeout", type=float, default=0, help="для --wait: сколько ждать, с (0 — бесконечно)")
    ap.add_argument("--agents", default=None, help="для фонового режима: claude,codex")
    ap.add_argument("--name", default=platform.node() or "worker")
    ap.add_argument("--report", metavar="JOB_ID", help="сдать результат задания (для чата-коворкера)")
    ap.add_argument("--fail", action="store_true", help="для --report: задание не выполнено")
    ap.add_argument("--text", help="для --report: текст отчёта")
    ap.add_argument("--file", help="для --report: файл с текстом отчёта (UTF-8)")
    a = ap.parse_args()
    settings = get_settings()
    w = Worker(settings, a.name if not a.wait else f"{a.name}-chat")
    if a.report:
        text = Path(a.file).read_text(encoding="utf-8") if a.file else (a.text or "")
        if not text.strip():
            raise SystemExit("Пустой отчёт: передайте --text или --file")
        await w.report(a.report, not a.fail, text)
        print(f"Отчёт по {a.report} отправлен")
        return 0
    if a.wait:
        claimed = await w.wait_one(a.wait, a.timeout or None)
        if not claimed:
            print(json.dumps({"job": None}))
            return 1
        job_id = claimed["job"]["id"]
        ctx_file = (Path(settings.AGENT_CONTEXT_DIR) / f"{a.wait}.md").resolve()
        claimed["context_file"] = str(ctx_file)
        claimed["instructions"] = (
            f"1) Прочитай журнал контекста {ctx_file} (если есть). "
            "2) Выполни задание (рабочая папка — job.workdir_path; данные задачи — task; файлы вложений — "
            "MCP todo.download_attachment). "
            f"3) Сдай отчёт: python -m todobot.worker --report {job_id} --file <отчёт.md> (или --fail), "
            f"либо MCP todo.agent_report(job_id='{job_id}', ...). "
            f"4) Допиши в {ctx_file} 2–5 строк: дата, #{job_id}, что сделано, что важно помнить. "
            "5) Снова запусти ожидание."
        )
        print(json.dumps(claimed, ensure_ascii=False, indent=2))
        return 0
    agents = [x.strip() for x in (a.agents or settings.WORKER_AGENTS).split(",") if x.strip()]
    await w.serve(agents)
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.exit(asyncio.run(amain()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
