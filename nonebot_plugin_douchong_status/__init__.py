import asyncio
from functools import partial

import httpx
from nonebot import get_bots, get_driver, get_plugin_config, logger
from nonebot.adapters.onebot.v11 import Bot, MessageSegment
from nonebot.exception import AdapterException
from nonebot.plugin import PluginMetadata

from .config import Config
from .monitor import Monitor

__plugin_meta__ = PluginMetadata(
    name="斗虫健康巡检",
    description="每 3 分钟检查 VR、PSP 和 VUP 斗虫服务，通过 QQ 私信报告新故障",
    usage="加载插件后自动巡检，告警发送至 QQ 308025580。",
    type="application",
    config=Config,
    supported_adapters={"~onebot.v11"},
)

CHECK_INTERVAL_SECONDS = 180
driver = get_driver()
config = get_plugin_config(Config)
monitor = Monitor(
    {
        "VR": str(config.douchong_status_vr_url),
        "PSP": str(config.douchong_status_psp_url),
        "VUP": str(config.douchong_status_vup_url),
    },
    config.douchong_status_state_file,
)
_check_task: asyncio.Task[None] | None = None


async def send_alert(bot: Bot, message: str) -> bool | None:
    try:
        receipt = await asyncio.wait_for(
            bot.call_api(
                "send_private_msg", user_id=308025580, message=MessageSegment.text(message)
            ),
            timeout=10,
        )
    except asyncio.TimeoutError:
        logger.warning("斗虫告警私信等待回执超时，送达状态未知")
        return None
    except AdapterException as exc:
        logger.warning(f"斗虫告警私信发送失败（{type(exc).__name__}），下轮重新检查")
        return False
    message_id = receipt.get("message_id") if isinstance(receipt, dict) else None
    if type(message_id) is int and -(2**31) <= message_id < 2**31:
        return True
    logger.warning("斗虫告警私信缺少有效 message_id，送达状态未知")
    return None


async def check_health() -> None:
    bot = next(
        (
            bot
            for _, bot in sorted(get_bots().items())
            if isinstance(bot, Bot) and config.douchong_status_bot_id in (None, bot.self_id)
        ),
        None,
    )
    async with httpx.AsyncClient(
        timeout=10,
        http2=True,
        http1=False,
        headers={"User-Agent": "nonebot-plugin-douchong-status/0.1.0"},
    ) as client:
        await monitor.run(client, partial(send_alert, bot) if bot is not None else None)


async def _check_loop() -> None:
    loop = asyncio.get_running_loop()
    interval = CHECK_INTERVAL_SECONDS
    next_check = loop.time() + interval
    while True:
        await asyncio.sleep(max(0, next_check - loop.time()))
        try:
            await check_health()
        except Exception as exc:
            logger.warning(f"斗虫巡检执行失败（{type(exc).__name__}），下轮继续巡检")

        # Keep the timer on the same cadence and skip slots a slow check already passed.
        elapsed = max(0, loop.time() - next_check)
        next_check += (elapsed // interval + 1) * interval


@driver.on_startup
async def _start_checks() -> None:
    global _check_task
    if _check_task is None or _check_task.done():
        _check_task = asyncio.create_task(_check_loop(), name="douchong-status-check")


@driver.on_shutdown
async def _stop_checks() -> None:
    global _check_task
    task = _check_task
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    _check_task = None
