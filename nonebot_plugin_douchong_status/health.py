import asyncio
import re
from dataclasses import dataclass
from typing import Any

import httpx

SNAPSHOT_FAILURES = {
    "starting": "健康采样尚未完成",
    "stale": "健康采样已过期",
    "unavailable": "健康采样不可用",
    "degraded": "服务健康状态异常",
}


@dataclass(frozen=True)
class ProbeResult:
    # A missing section means its current state could not be observed.
    checks: dict[str, dict[str, str]]
    healthy: bool = False


def fields(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def token(value: object) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[\w-]{1,64}", value, re.ASCII) else ""


def error_suffix(payload: dict[str, Any]) -> str:
    error = token(payload.get("error_type"))
    return f"（{error}）" if error else ""


def collect_checks(payload: dict[str, Any]) -> dict[str, dict[str, str]]:
    checks: dict[str, dict[str, str]] = {}
    dependencies = fields(payload.get("dependencies"))
    redis = fields(dependencies.get("redis"))
    collector = fields(payload.get("collector"))
    components = (
        ("database", "MySQL", dependencies.get("database")),
        ("database_pool", "MySQL 连接池", dependencies.get("database_pool")),
        ("redis_metrics", "Redis 采集库", redis.get("metrics")),
        ("redis_db1", "Redis DB1", redis.get("whale_db1")),
        ("redis_db2", "Redis DB2", redis.get("api_db2")),
        ("redis_db3", "Redis DB3 弹幕人数", redis.get("danmaku_users_db3")),
        ("redis_db4", "Redis DB4 弹幕排行", redis.get("danmaku_rank_db4")),
        ("http_pool", "采集 HTTP 连接池", collector.get("http_pool")),
        ("api_cache", "API 缓存", payload.get("api_cache")),
    )
    for key, label, value in components:
        component = fields(value)
        status = token(component.get("status"))
        if status:
            normal = status == "ok" or (key == "api_cache" and status == "disabled")
            checks[key] = {} if normal else {"status": f"{label} {status}{error_suffix(component)}"}

    responsive = collector.get("loop_responsive")
    if responsive is True:
        checks["collector"] = {}
    elif responsive is False:
        checks["collector"] = {"loop": f"采集事件循环无响应{error_suffix(collector)}"}

    items = fields(collector.get("rooms")).get("items")
    if isinstance(items, list):
        rooms = checks["rooms"] = {}
        for item in items:
            room = fields(item)
            room_id = room.get("room_id")
            status = token(room.get("status"))
            if status in {"", "connected", "daily-reconnecting"}:
                continue
            if type(room_id) is int and room_id > 0:
                rooms[str(room_id)] = f"房间 {room_id} 未连接（{status}）"

    failing = fields(payload.get("upstream_api")).get("failing_endpoints")
    if isinstance(failing, list):
        checks["upstream"] = {
            name: f"B 站上游接口 {name} 异常" for entry in failing if (name := token(entry))
        }
    return checks


async def probe(client: httpx.AsyncClient, url: str) -> ProbeResult:
    try:
        response = await asyncio.wait_for(client.get(url), timeout=10)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return ProbeResult({"endpoint": {"request": "健康接口请求超时"}})
    except httpx.RequestError as exc:
        return ProbeResult({"endpoint": {"request": f"健康接口请求失败（{type(exc).__name__}）"}})

    code = response.status_code
    if code not in {200, 503}:
        return ProbeResult({"endpoint": {"request": f"健康接口返回 HTTP {code}"}})
    try:
        payload = fields(response.json())
    except ValueError:
        payload = {}

    status = token(payload.get("status"))
    if code == 200 and status == "ok" and payload.get("ok") is True:
        return ProbeResult({"endpoint": {}}, healthy=True)
    if status not in SNAPSHOT_FAILURES or payload.get("ok") is not False:
        return ProbeResult({"endpoint": {"request": f"HTTP {code} 健康响应格式异常"}})

    checks = collect_checks(payload) if status == "degraded" else {}
    has_details = any(checks.values())
    checks["endpoint"] = {}
    checks["snapshot"] = (
        {} if has_details else {status: f"{SNAPSHOT_FAILURES[status]}{error_suffix(payload)}"}
    )
    return ProbeResult(checks)
