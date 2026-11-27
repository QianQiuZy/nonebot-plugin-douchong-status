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

    bot = Mock(spec=Bot, self_id="1000", call_api=AsyncMock(return_value={"message_id": -123}))
    monkeypatch.setattr(plugin, "get_bots", lambda: {"1000": bot})
    responses = {}
    seen = []

    def serve(request):
        assert request.headers["User-Agent"] == "nonebot-plugin-douchong-status/0.1.0"
        seen.append((request.method, request.url.host, request.url.path))
        response = responses.get(request.url.host)
        if response is not None:
            return response
        return httpx.Response(
            200, json={**HEALTHY, "last_hour": {"totals": {"room_disconnect": 9}}}
        )

    transport = httpx.MockTransport(serve)

    def create_client(**kwargs):
        assert kwargs["http2"] is True
        assert kwargs["http1"] is False
        return httpx.AsyncClient(transport=transport, **kwargs)

    monkeypatch.setattr(plugin, "httpx", SimpleNamespace(AsyncClient=create_client))

    async def run():
        await plugin.check_health()
        assert sorted(seen) == [
            ("GET", f"{host}.qianqiuzy.cn", "/health/check") for host in ("psp", "vr", "vup")
        ]
        bot.call_api.assert_not_awaited()

        vr = "vr.qianqiuzy.cn"
        planned = {"room_id": 456, "status": "daily-reconnecting"}
        responses[vr] = httpx.Response(
            200, json={**HEALTHY, "collector": {"rooms": {"items": [planned]}}}
        )
        await plugin.check_health()
        bot.call_api.assert_not_awaited()

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
                "rooms": {"items": [planned, {"room_id": 123, "status": "authenticating"}]},
            },
        )
        await plugin.check_health()
        assert bot.call_api.await_count == 2
        message = bot.call_api.await_args.kwargs["message"].data["text"]
        assert "房间 123 未连接" in message
        assert "456" not in message
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


@pytest.mark.parametrize(("receipt", "expected"), [
    ({"message_id": -123}, True), ({"message_id": 0}, True),
    ({}, None), (None, None), ({"message_id": True}, None),
    ({"message_id": "42"}, None), ({"message_id": 2**31}, None),
])
def test_onebot_receipt(plugin, receipt, expected):
    bot = Mock(spec=Bot, call_api=AsyncMock(return_value=receipt))
    assert asyncio.run(plugin.send_alert(bot, "test")) is expected


def test_independent_redis_alerts_and_bounded_unknown_receipts(plugin, tmp_path):
    url = "https://example.test/check"
    targets = {"VR": url}
    state = tmp_path / "state.json"
    redis = {}
    room = {"collector": {"rooms": {"items": [{"room_id": 123, "status": "offline"}]}}}

    async def run():
        monitor = plugin.Monitor(targets, state)
        def serve(request):
            return degraded(dependencies={"redis": redis}, **room)

        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            send = AsyncMock(return_value=True)
            await monitor.run(client, send)
            assert "房间 123" in send.await_args.args[0]
            redis["danmaku_users_db3"] = {"status": "unavailable"}
            send.return_value = None
            for attempt in range(3):
                monitor = plugin.Monitor(targets, state)
                await monitor.run(client, send)
                assert send.await_count == 2 + attempt
                assert "Redis DB3" in send.await_args.args[0]
                assert "房间" not in send.await_args.args[0]
                assert monitor.reported[f"VR {url}"] == {"rooms:123"}
            monitor = plugin.Monitor(targets, state)
            await monitor.run(client, send)
            assert send.await_count == 4
            redis["danmaku_rank_db4"] = {"status": "unavailable"}
            send.return_value = True
            await monitor.run(client, send)
            assert send.await_count == 5
            message = send.await_args.args[0]
            assert "Redis DB4" in message and "Redis DB3" not in message
            redis.clear()  # Older backends omit these roles, so recovery is unknown.
            await monitor.run(client, send)
            redis.update(
                danmaku_rank_db4={"status": "unavailable"}, danmaku_users_db3={"status": "ok"}
            )
            await monitor.run(client, send)
            redis["danmaku_users_db3"]["status"] = "unavailable"
            await monitor.run(client, send)
            assert send.await_count == 6
            message = send.await_args.args[0]
            assert "Redis DB3" in message and "Redis DB4" not in message
            await monitor.run(client, send)
            assert send.await_count == 6
    asyncio.run(run())
