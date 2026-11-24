import asyncio
import json
import logging
import time

import httpx
import pytest

from todobot.service import TaskService
from todobot.watchdog import LoopWatchdog, read_psi, verdict
from todobot.worker import Worker

from .conftest import CHAT


async def test_watchdog_reports_blocking_call(caplog):
    dog = LoopWatchdog(threshold=0.5)
    with caplog.at_level(logging.WARNING, logger="todobot.watchdog"):
        dog.start()
        await asyncio.sleep(0.8)
        assert not caplog.records  # спокойная работа — тишина
        time.sleep(1.5)  # блокируем event loop
        await asyncio.sleep(0.8)
        await dog.stop()
    [rec] = caplog.records
    text = rec.getMessage()
    assert "Замирание" in text and "блокирующем вызове" in text and "time.sleep(1.5)" in text


def test_verdict():
    assert "код бота" in verdict(4, 0, 3.8, {}, 0)
    assert verdict(4, 4, 0, {"cpu": 3.9, "io": 0}, 0).endswith("cpu")
    assert "VM" in verdict(4, 3.9, 0, {"cpu": 0.1}, 0)
    assert "часов" in verdict(4, 0, 0, {}, 30)


def test_read_psi(tmp_path):
    (tmp_path / "cpu.pressure").write_text(
        "some avg10=0.0 avg60=0.0 avg300=0.0 total=2500000\nfull avg10=0.0 avg60=0.0 avg300=0.0 total=1\n"
    )
    assert read_psi(tmp_path) == {"cpu": 2.5}


async def test_claim_repeats_same_job_after_lost_response(service: TaskService):
    _, job = await service.delegate(CHAT, "claude", "первое")
    _, other = await service.delegate(CHAT, "claude", "второе")
    first = await service.claim_job(["claude"], "pc", "req-1")
    again = await service.claim_job(["claude"], "pc", "req-1")  # ответ не дошёл, воркер повторил
    assert first.id == again.id == job.id
    assert (await service.claim_job(["claude"], "pc", "req-2")).id == other.id
    await service.finish_job(job.id, True, "ок")
    assert await service.claim_job(["claude"], "pc", "req-1") is None


async def test_worker_keeps_claim_id_until_job_received(settings):
    seen = []

    def reply(request):
        seen.append(json.loads(request.content)["claim_id"])
        if len(seen) == 1:
            raise httpx.ReadTimeout("")
        return httpx.Response(204) if len(seen) == 2 else httpx.Response(200, json={"job": {"id": "j"}})

    settings.MCP_TOKEN = "t"
    w = Worker(settings, "pc")
    w.http = httpx.AsyncClient(transport=httpx.MockTransport(reply), base_url="http://bot")
    with pytest.raises(httpx.ReadTimeout):
        await w.claim(["claude"])
    assert await w.claim(["claude"]) is None
    assert (await w.claim(["claude"]))["job"]["id"] == "j"
    assert len(set(seen)) == 1 and w.claim_id != seen[0]
