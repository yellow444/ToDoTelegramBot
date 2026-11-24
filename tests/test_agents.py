"""Агенты-коворкеры: очередь, API воркера, отчёты в Telegram, продолжение разговора."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from todobot import tools
from todobot.bot import handlers, jobs
from todobot.web.app import build_web
from todobot.worker import build_prompt

from .conftest import CHAT
from .test_bot import FakeBot, make_context, text_update


@pytest.fixture
def agent_settings(settings):
    settings.MCP_TOKEN = "w-secret"
    settings.AGENT_WORKDIRS = r"todo=F:\repos\todo;gw=F:\repos\gw"
    return settings


def test_workdirs_parsing(agent_settings):
    assert agent_settings.workdirs == {"todo": r"F:\repos\todo", "gw": r"F:\repos\gw"}


async def test_delegate_validates_workdir(service, agent_settings):
    with pytest.raises(ValueError, match="Неизвестная рабочая папка"):
        await service.delegate(CHAT, "claude", "сделай", workdir="nope")


async def test_full_cycle_via_worker_api(service, agent_settings):
    bot = FakeBot()
    ctx = make_context(service, agent_settings, bot)
    task, job = await service.delegate(CHAT, "claude", "добавь тесты", workdir="todo")
    assert task.assignee == "claude" and task.agent_status == "queued" and task.card_dirty

    with TestClient(build_web(service, agent_settings)) as c:
        assert c.post("/api/agent/claim", json={"agents": ["claude"]}).status_code == 401
        auth = {"Authorization": "Bearer w-secret"}
        r = c.post("/api/agent/claim", json={"agents": ["codex"]}, headers=auth)
        assert r.status_code == 204  # задание для Claude, Codex его не берёт
        claimed = c.post("/api/agent/claim", json={"agents": ["claude"], "worker": "pc"}, headers=auth).json()
        assert claimed["job"]["id"] == job.id and claimed["job"]["workdir_path"] == r"F:\repos\todo"
        assert (await service.get(task.id)).agent_status == "running"

        r = c.post(f"/api/agent/jobs/{job.id}/result", json={"ok": True, "result": "Готово: 5 тестов", "session_id": "S1"}, headers=auth)
        assert r.status_code == 200
        assert c.post(f"/api/agent/jobs/{job.id}/result", json={"ok": True}, headers=auth).status_code == 409

    await jobs.sync_cards(ctx)
    sent = [c for c in bot.calls if c[0] == "send"]
    report = sent[-1]
    assert "Claude · todo — готово" in report[2] and "Готово: 5 тестов" in report[2]
    assert [b.callback_data for b in report[3].inline_keyboard[0]] == [f"g|{task.id}|show", f"x|{task.id}|done"]
    job = await service.repo.get_job(job.id)
    assert job.notify == "none" and job.result_message_id == 101 + len(sent) - 1

    # ответ на отчёт → продолжение той же сессии
    upd = text_update("теперь обнови README", message_id=900)
    upd.effective_message.reply_to_message = SimpleNamespace(message_id=job.result_message_id)
    await handlers.on_message(upd, ctx)
    [follow, first] = await service.repo.list_jobs(CHAT)
    assert follow.parent_id == job.id and follow.session_id == "S1" and follow.task_id == task.id
    assert follow.status == "queued" and follow.workdir == "todo"
    assert (await service.job_history(follow))[0]["result"] == "Готово: 5 тестов"


async def test_inbox_and_report_tools(service, agent_settings):
    assert (await tools.agent_inbox("claude")) == {"job": None}
    res = await tools.delegate_to_agent("спланируй мою неделю", agent="claude")
    got = await tools.agent_inbox("claude")
    assert got["job"]["id"] == res["job"]["id"] and got["job"]["status"] == "running"
    done = await tools.agent_report(res["job"]["id"], "План готов", ok=True)
    assert done["job"]["status"] == "done"
    assert (await tools.list_agent_jobs())[0]["status_human"] == "готово"


async def test_cancel_and_stale(service, agent_settings):
    from datetime import timedelta

    from todobot.models import utcnow

    _, job = await service.delegate(CHAT, "codex", "x")
    assert (await service.cancel_job(job.id)).status == "cancelled"
    _, job2 = await service.delegate(CHAT, "codex", "y")
    await service.claim_job(["codex"], "pc")
    await service.repo.update_job(job2.id, started_at=utcnow() - timedelta(hours=5))
    await service.fail_stale_jobs()
    assert (await service.repo.get_job(job2.id)).status == "failed"


def test_llm_tools_exclude_worker_only():
    names = {f.__name__ for f in tools.LLM_TOOLS}
    assert "delegate_to_agent" in names and not {"agent_inbox", "agent_report", "notify"} & names


def test_build_prompt_history():
    job = {"prompt": "обнови README", "workdir_path": "F:/x"}
    hist = [{"prompt": "добавь тесты", "result": "сделал"}]
    p = build_prompt(job, hist, with_history=True)
    assert "добавь тесты" in p and "обнови README" in p and "F:/x" in p
    assert "добавь тесты" not in build_prompt(job, hist, with_history=False)


async def test_model_filter():
    models = [
        {"id": "general35", "tags": ["local", "tools"]},
        {"id": "paid", "tags": ["cloud", "tools"]},
        {"id": "free", "tags": ["cloud", "free", "tools"]},
        {"id": "vision-noTools", "tags": ["local"]},
    ]
    assert [m["id"] for m in handlers._free_or_local(models)] == ["general35", "free"]
