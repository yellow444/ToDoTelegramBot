"""HTTP: Telegram Mini App (страница + REST API), /healthz и MCP (streamable HTTP) на /mcp."""

import contextlib
import hmac
import logging
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from .. import tools
from ..config import Settings
from ..llm import Agent, LLMError
from ..models import UserSettings, utcnow
from ..service import KEEP, TaskNotFound, TaskService
from ..timeparse import parse_when
from ..timeutil import fmt_local, local_now, to_local
from .auth import InitDataError, validate_init_data

logger = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def host_gateways(route_file: str = "/proc/net/route") -> set[str]:
    """IP шлюзов docker-сетей контейнера (адрес сети + 1). С них приходят запросы хоста через проброшенный порт.
    Вне Linux/контейнера — пустое множество."""
    import ipaddress
    import socket
    import struct

    out: set[str] = set()
    try:
        lines = open(route_file, encoding="ascii").read().splitlines()[1:]
    except OSError:
        return out
    for line in lines:
        parts = line.split()
        if len(parts) < 8:
            continue
        dest, gw, mask = (int(x, 16) for x in (parts[1], parts[2], parts[7]))
        if gw:  # маршрут по умолчанию: шлюз указан явно
            out.add(socket.inet_ntoa(struct.pack("<I", gw)))
        elif dest and mask:  # сеть интерфейса: шлюз docker — первый адрес сети
            net = ipaddress.IPv4Address(socket.inet_ntoa(struct.pack("<I", dest)))
            out.add(str(net + 1))
    return out

PRESETS = [("15m", "+15 мин"), ("1h", "+1 час"), ("eve", "Вечером"), ("tom", "Завтра утром"), ("work", "Начало раб. дня"), ("mon", "В понедельник")]


class TaskIn(BaseModel):
    text: str
    when: str | None = None
    repeat: Literal["daily", "workdays", "weekly", "monthly"] | None = None
    workhours_only: bool = False


class TaskPatch(BaseModel):
    text: str | None = None
    when: str | None = None  # null — снять напоминание; поле отсутствует — не менять
    repeat: Literal["daily", "workdays", "weekly", "monthly"] | None = None
    workhours_only: bool | None = None
    status: Literal["open", "done"] | None = None


class SettingsPatch(BaseModel):
    tz: str | None = None
    work_start: str | None = None
    work_end: str | None = None
    workdays: list[int] | None = None  # 1=Пн..7=Вс
    morning_digest: bool | None = None
    evening_review: bool | None = None
    smart_input: bool | None = None
    ui_mode: Literal["buttons", "webapp"] | None = None


class AskIn(BaseModel):
    text: str


class ClaimIn(BaseModel):
    agents: list[str]
    worker: str = "worker"
    claim_id: str | None = None


class ResultIn(BaseModel):
    ok: bool = True
    result: str = ""
    session_id: str | None = None


def build_web(service: TaskService, settings: Settings, agent: Agent | None = None, mcp=None) -> FastAPI:
    mcp_app = None
    if mcp is not None and settings.MCP_HTTP_ENABLED:
        from mcp.server.transport_security import TransportSecuritySettings

        # Host-проверку SDK отключаем: доступ закрыт Bearer-токеном / loopback (см. middleware ниже).
        # Один MCP-сервер на всех клиентов внутри процесса бота. stateless + JSON: без сессий на клиента,
        # поэтому десятки чатов Claude/Codex не держат ни процессов, ни памяти (в отличие от stdio через docker exec).
        mcp_app = mcp.streamable_http_app(
            streamable_http_path="/mcp",
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        if mcp_app is not None:
            async with mcp.session_manager.run():
                yield
        else:
            yield

    app = FastAPI(title="ToDoTelegramBot", lifespan=lifespan, docs_url=None, redoc_url=None)

    trusted_hosts = LOOPBACK | (host_gateways() if settings.MCP_TRUST_HOST else set())

    @app.middleware("http")
    async def mcp_auth(request: Request, call_next):
        if request.url.path.startswith("/mcp"):
            client = request.client.host if request.client else ""
            token = settings.MCP_TOKEN
            got = request.headers.get("authorization", "")
            if token and hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
                pass
            elif client in trusted_hosts and (settings.MCP_TRUST_HOST or not token):
                pass  # запрос с этой машины
            elif token:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            else:
                return JSONResponse({"error": "MCP_TOKEN не задан: /mcp доступен только с этой машины"}, status_code=403)
        return await call_next(request)

    async def current_user(x_telegram_init_data: str = Header(default="")) -> UserSettings:
        user_id: int | None = None
        if x_telegram_init_data:
            try:
                data = validate_init_data(x_telegram_init_data, settings.TOKEN)
            except InitDataError as exc:
                raise HTTPException(401, f"initData: {exc}") from None
            user_id = int(data.get("user", {}).get("id", 0)) or None
        elif settings.WEBAPP_DEV_USER_ID:
            user_id = settings.WEBAPP_DEV_USER_ID
        if not user_id:
            raise HTTPException(401, "Откройте приложение из Telegram")
        if settings.ALLOWED_USER_IDS and user_id not in settings.ALLOWED_USER_IDS:
            raise HTTPException(403, "Нет доступа")
        return await service.user(user_id)

    def view(t, u: UserSettings) -> dict:
        d = tools._task_view(t, u)
        d["when_input"] = to_local(t.scheduled_at, u).strftime("%Y-%m-%dT%H:%M") if t.scheduled_at else None
        d["when_human"] = fmt_local(t.scheduled_at, u) if t.scheduled_at else None
        return d

    def when(value: str | None, u: UserSettings):
        if value is None or not value.strip():
            return None
        try:
            return parse_when(value, u)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    async def owned(task_id: str, u: UserSettings):
        try:
            return await service.get(task_id, u.chat_id)
        except TaskNotFound:
            raise HTTPException(404, "Задача не найдена") from None

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/me")
    async def me(u: UserSettings = Depends(current_user)):
        from ..bot.ui import preset_time

        presets = []
        for code, label in PRESETS:
            dt = preset_time(code, u)
            presets.append({"label": label, "value": to_local(dt, u).strftime("%Y-%m-%dT%H:%M")})
        return {
            "user_id": u.chat_id,
            "settings": u.public(),
            "now_local": local_now(u).strftime("%Y-%m-%dT%H:%M"),
            "presets": presets,
            "llm": agent is not None,
        }

    @app.patch("/api/settings")
    async def patch_settings(body: SettingsPatch, u: UserSettings = Depends(current_user)):
        fields = body.model_dump(exclude_none=True)
        if "workdays" in fields:
            fields["workdays"] = sorted({d - 1 for d in fields["workdays"] if 1 <= d <= 7})
        try:
            u = await service.update_user(u.chat_id, **fields)
        except Exception as exc:
            raise HTTPException(422, str(exc)) from None
        if "ui_mode" in fields:
            for t in await service.list_tasks(u.chat_id, "open", 100):
                await service.repo.update_task(t.id, card_dirty=True)
        return u.public()

    @app.get("/api/tasks")
    async def list_tasks(status: Literal["open", "done", "all"] = "open", u: UserSettings = Depends(current_user)):
        tasks = await service.list_tasks(u.chat_id, None if status == "all" else status, 300)
        if status == "done":
            tasks.sort(key=lambda t: t.done_at or t.created_at, reverse=True)
        return [view(t, u) for t in tasks]

    @app.post("/api/tasks", status_code=201)
    async def create_task(body: TaskIn, u: UserSettings = Depends(current_user)):
        if not body.text.strip():
            raise HTTPException(422, "Пустой текст")
        t = await service.create(
            u.chat_id, body.text, when=when(body.when, u), repeat=body.repeat,
            workhours_only=body.workhours_only, source="webapp", parse_text=body.when is None, post_card=True,
        )
        return view(t, u)

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str, u: UserSettings = Depends(current_user)):
        return view(await owned(task_id, u), u)

    @app.patch("/api/tasks/{task_id}")
    async def patch_task(task_id: str, body: TaskPatch, u: UserSettings = Depends(current_user)):
        t = await owned(task_id, u)
        sent = body.model_fields_set
        if body.text is not None and body.text.strip() and body.text != t.text:
            t = await service.edit_text(t, body.text, touch_card=True)
        if "when" in sent or "repeat" in sent or "workhours_only" in sent:
            new_when = when(body.when, u) if "when" in sent else t.scheduled_at
            t = await service.set_reminder(
                t, new_when, repeat=body.repeat if "repeat" in sent else KEEP,
                workhours_only=body.workhours_only, touch_card=True,
            )
        if body.status == "done" and t.status == "open":
            t = await service.complete(t, touch_card=True)
        elif body.status == "open" and t.status == "done":
            t = await service.reopen(t, touch_card=True)
        return view(t, u)

    @app.delete("/api/tasks/{task_id}", status_code=204)
    async def delete_task(task_id: str, u: UserSettings = Depends(current_user)):
        await service.delete(await owned(task_id, u), delete_card=True)

    @app.post("/api/ask")
    async def ask(body: AskIn, u: UserSettings = Depends(current_user)):
        if agent is None:
            raise HTTPException(503, "LLM не настроена")
        try:
            reply = await agent.ask(u.chat_id, body.text)
        except LLMError as exc:
            raise HTTPException(502, str(exc)) from None
        return {"text": reply.text, "changed": reply.changed}

    # ---------- API воркера агентов (Bearer MCP_TOKEN) ----------
    async def worker_auth(authorization: str = Header(default="")) -> None:
        token = settings.MCP_TOKEN
        if not token or not hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
            raise HTTPException(401, "unauthorized")

    @app.post("/api/agent/claim", dependencies=[Depends(worker_auth)])
    async def agent_claim(body: ClaimIn):
        for agent_name in body.agents:
            service.worker_seen[agent_name] = utcnow()  # «чат-коворкер на связи»
        job = await service.claim_job(body.agents, body.worker, body.claim_id)
        if job is None:
            return Response(status_code=204)
        out = {"job": tools._job_view(job), "history": await service.job_history(job)}
        if job.task_id and (task := await service.repo.get_task(job.task_id)):
            out["task"] = tools.task_context(task, await service.user(task.chat_id))
        return out

    @app.post("/api/agent/jobs/{job_id}/result", dependencies=[Depends(worker_auth)])
    async def agent_result(job_id: str, body: ResultIn):
        try:
            job = await service.finish_job(job_id, body.ok, body.result, body.session_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return tools._job_view(job)

    if mcp_app is not None:
        app.mount("/", mcp_app)  # маршрут /mcp внутри; монтируется последним
    return app
