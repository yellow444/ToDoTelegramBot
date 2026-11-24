import time

import pytest
from fastapi.testclient import TestClient

from todobot.mcp_server import build_mcp
from todobot.web.app import build_web
from todobot.web.auth import InitDataError, sign_init_data, validate_init_data

from .conftest import CHAT, TOKEN


def init_data(user_id=CHAT, token=TOKEN):
    return sign_init_data({"auth_date": int(time.time()), "user": {"id": user_id, "first_name": "T"}}, token)


def test_init_data_validation():
    data = validate_init_data(init_data(), TOKEN)
    assert data["user"]["id"] == CHAT
    with pytest.raises(InitDataError):
        validate_init_data(init_data(token="other:token"), TOKEN)


@pytest.fixture
def client(service, settings):
    settings.MCP_TOKEN = "secret"
    with TestClient(build_web(service, settings, None, build_mcp())) as c:
        c.headers["X-Telegram-Init-Data"] = init_data()
        yield c


def test_requires_auth(service, settings):
    with TestClient(build_web(service, settings)) as c:
        assert c.get("/api/tasks").status_code == 401
        assert c.get("/healthz").json() == {"status": "ok"}


def test_task_crud(client):
    r = client.post("/api/tasks", json={"text": "демо завтра в 10"})
    assert r.status_code == 201, r.text
    task = r.json()
    assert task["text"] == "демо" and task["when_input"].endswith("T10:00")

    r = client.patch(f"/api/tasks/{task['id']}", json={"when": "2030-01-02T08:30", "repeat": "weekly"})
    assert r.json()["when_input"] == "2030-01-02T08:30" and r.json()["repeat"] == "weekly"

    r = client.patch(f"/api/tasks/{task['id']}", json={"when": None})
    assert r.json()["remind_at"] is None

    r = client.patch(f"/api/tasks/{task['id']}", json={"status": "done"})
    assert r.json()["status"] == "done"
    assert [t["id"] for t in client.get("/api/tasks?status=done").json()] == [task["id"]]

    assert client.delete(f"/api/tasks/{task['id']}").status_code == 204
    assert client.get(f"/api/tasks/{task['id']}").status_code == 404


def test_settings_and_me(client):
    r = client.patch("/api/settings", json={"work_start": "10:00", "work_end": "19:00", "workdays": [1, 2, 3, 4]})
    assert r.json()["workdays"] == [1, 2, 3, 4]
    me = client.get("/api/me").json()
    assert me["settings"]["work_start"] == "10:00" and len(me["presets"]) == 6
    assert client.patch("/api/settings", json={"work_start": "20:00"}).status_code == 422


def test_other_user_cannot_touch(client):
    task = client.post("/api/tasks", json={"text": "моё"}).json()
    r = client.get(f"/api/tasks/{task['id']}", headers={"X-Telegram-Init-Data": init_data(user_id=777)})
    assert r.status_code == 404


def test_mcp_http(client):
    assert client.post("/mcp", json={}).status_code == 401
    headers = {"Authorization": "Bearer secret", "Accept": "application/json, text/event-stream"}
    init = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    }
    r = client.post("/mcp", json=init, headers=headers)
    assert r.status_code == 200, r.text
    assert "todo-telegram" in r.text


def test_host_gateways(tmp_path):
    from todobot.web.app import host_gateways

    route = tmp_path / "route"
    route.write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        "eth1\t00000000\t01001FAC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
        "eth0\t000016AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
        "eth1\t00001FAC\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n",
        encoding="ascii",
    )
    assert host_gateways(str(route)) == {"172.31.0.1", "172.22.0.1"}
    assert host_gateways(str(tmp_path / "missing")) == set()


def test_mcp_stateless_tools_call(client):
    headers = {"Authorization": "Bearer secret", "Accept": "application/json, text/event-stream"}
    call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_settings", "arguments": {}}}
    r = client.post("/mcp", json=call, headers=headers)  # без initialize: сервер без сессий
    assert r.status_code == 200, r.text
    assert "Europe/Moscow" in r.text
