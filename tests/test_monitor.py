import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import nonebot
import pytest
from nonebot.adapters.onebot.v11 import Bot
from nonebot.exception import AdapterException

HEALTHY = {"ok": True, "status": "ok"}
DATABASE = {"database": {"status": "unavailable", "error_type": "TimeoutError"}}


def degraded(**fields):
    return httpx.Response(503, json={"ok": False, "status": "degraded", **fields})


@pytest.fixture(scope="module")
def plugin():
    nonebot.init(driver="~fastapi", _env_file=None)
    loaded = nonebot.load_plugin("nonebot_plugin_douchong_status")
    assert loaded is not None
    return loaded.module


def test_timer_follows_nonebot_lifecycle_and_survives_a_failed_check(plugin, monkeypatch):
    assert plugin.CHECK_INTERVAL_SECONDS == 180
    monkeypatch.setattr(plugin, "CHECK_INTERVAL_SECONDS", 0.01)

    async def run():
        second_check_started = asyncio.Event()

        async def slow_check():
            if check.await_count == 1:
                raise RuntimeError("simulated failed check")
            second_check_started.set()
            await asyncio.Future()

        check = AsyncMock(side_effect=slow_check)
        monkeypatch.setattr(plugin, "check_health", check)
        app = nonebot.get_driver().server_app
        async with app.router.lifespan_context(app):
            task = plugin._check_task
            assert task is not None
            await plugin._start_checks()
            assert plugin._check_task is task
            await asyncio.wait_for(second_check_started.wait(), timeout=2)

        assert check.await_count == 2
        assert task.cancelled()
        assert plugin._check_task is None

    asyncio.run(run())


def test_check_sends_only_new_problems(plugin, tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    monitor = plugin.Monitor(plugin.monitor.targets, state_file)
    monkeypatch.setattr(plugin, "monitor", monitor)

    bot = Mock(spec=Bot, self_id="1000", call_api=AsyncMock())
    monkeypatch.setattr(plugin, "get_bots", lambda: {"1000": bot})
    responses = {}
    seen = []

    def serve(request):
        seen.append((request.method, request.url.host, request.url.path))
        response = responses.get(request.url.host)
        if response is not None:
            return response
        return httpx.Response(
            200, json={**HEALTHY, "last_hour": {"totals": {"room_disconnect": 9}}}
        )

    transport = httpx.MockTransport(serve)
    monkeypatch.setattr(
        plugin,
        "httpx",
        SimpleNamespace(AsyncClient=lambda **kw: httpx.AsyncClient(transport=transport, **kw)),
    )

    async def run():
        await plugin.check_health()
        assert sorted(seen) == [
            ("GET", f"{host}.qianqiuzy.cn", "/health/check") for host in ("psp", "vr", "vup")
        ]
        bot.call_api.assert_not_awaited()

        vr = "vr.qianqiuzy.cn"
        responses[vr] = degraded(dependencies=DATABASE)
        await plugin.check_health()
        args, kwargs = bot.call_api.await_args
        assert args == ("send_private_msg",)
        assert kwargs["user_id"] == 308025580
        assert "VR" in kwargs["message"].data["text"]
        assert "MySQL unavailable" in kwargs["message"].data["text"]

        # Restart the monitor while the database remains down, then add a room problem.
        monkeypatch.setattr(plugin, "monitor", plugin.Monitor(monitor.targets, state_file))
        responses[vr] = degraded(
            dependencies=DATABASE,
            collector={
                "rooms": {"items": [{"room_id": 123, "status": "authenticating"}]},
            },
        )
        await plugin.check_health()
        assert bot.call_api.await_count == 2
        message = bot.call_api.await_args.kwargs["message"].data["text"]
        assert "房间 123 未连接" in message
        assert "MySQL" not in message

        # An unavailable endpoint yields a separate alert and cannot confirm recovery.
        healthy_response = httpx.Response(200, json=HEALTHY)
        still_unhealthy = responses[vr]
        responses[vr] = httpx.Response(503, text="<html>gateway error</html>")
        await plugin.check_health()
        assert bot.call_api.await_count == 3
        assert "HTTP 503" in bot.call_api.await_args.kwargs["message"].data["text"]
        responses[vr] = still_unhealthy
        await plugin.check_health()
        assert bot.call_api.await_count == 3

        responses[vr] = healthy_response
        await plugin.check_health()
        assert bot.call_api.await_count == 3
        assert state_file.read_text(encoding="utf-8") == "{}"
        responses[vr] = still_unhealthy
        await plugin.check_health()
        assert bot.call_api.await_count == 4
        message = bot.call_api.await_args.kwargs["message"].data["text"]
        assert "房间 123 未连接" in message and "MySQL" in message

    asyncio.run(run())


def test_missing_bot_and_failed_delivery_do_not_acknowledge(plugin, tmp_path):
    state_file = tmp_path / "state.json"
    url = "https://example.test/health/check"
    monitor = plugin.Monitor({"PSP": url}, state_file)
    send = AsyncMock(side_effect=[False, True])

    def offline(request):
        raise httpx.ConnectError("private transport diagnostic", request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(offline)) as client:
            await monitor.run(client, None)
            assert not state_file.exists()
            await monitor.run(client, send)
            assert not state_file.exists()
            await monitor.run(client, send)
            assert state_file.exists()
            await monitor.run(client, send)
        assert send.await_count == 2
        assert "ConnectError" in send.await_args.args[0]
        assert "private transport diagnostic" not in send.await_args.args[0]

        bot = Mock(spec=Bot, call_api=AsyncMock(side_effect=AdapterException("OneBot V11")))
        assert not await plugin.send_alert(bot, "test alert")

    asyncio.run(run())
