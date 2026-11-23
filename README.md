# nonebot-plugin-douchong-status

NoneBot2 插件。启动后每 3 分钟并行请求 VR、PSP、VUP 的 `/health/check`，新故障通过 OneBot v11 私信发送给 QQ `308025580`。

- 三个目标分别是 `https://vr.qianqiuzy.cn`、`https://psp.qianqiuzy.cn` 和 `https://vup.qianqiuzy.cn`，每个请求最多等待 10 秒。
- 健康和恢复时保持静默。接口不可达、返回异常或报告服务降级时，私信包含出问题的主机与可用的诊断明细，例如异常依赖、未连接房间和失败的上游接口。
- 房间状态 `daily-reconnecting` 表示每日计划重连，巡检将其视为正常。同一轮出现的其他房间或依赖故障仍会告警。
- 同一主机上的持续故障只在首次成功发送时通报。确认恢复后再次出现会重新通报。接口暂时不可达或采样过期时，保留此前已通报的组件故障记录。
- Redis DB3（弹幕人数）和 DB4（弹幕排行）各自告警。旧后端缺少字段时保留已有记录。
- 私信回执包含有效整数 `message_id` 才记作已通报。回执缺失或等待超时按结果未知处理，每个持续故障最多尝试三次，重启后延续次数，确认恢复后重置。
- 去重状态保存在本地 JSON 中，保留该文件即可在 Bot 重启后继续去重。Bot 离线或私信发送失败时，下一轮仍存在的故障会再尝试发送。

## 安装

在宿主 NoneBot 项目的 Python 环境安装该仓库，Python 版本至少为 3.10。

```bash
python -m pip install /mnt/c/Users/wxy/Documents/github/nonebot-plugin-douchong-status
```

将 `nonebot_plugin_douchong_status` 加入宿主 `pyproject.toml` 的 `[tool.nonebot]` `plugins` 列表，或在启动脚本加载。

```python
nonebot.load_plugin("nonebot_plugin_douchong_status")
```

宿主需要注册 OneBot v11 适配器并连接可以向目标 QQ 发私信的 Bot。插件随宿主启动创建 `asyncio` 定时任务，首次巡检在 3 分钟后执行，任务随宿主关闭而取消。使用一个 Bot 进程运行此巡检。

## 可选配置

在宿主的 `.env` 中设置。默认值可直接使用。

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `DOUCHONG_STATUS_VR_URL` | `https://vr.qianqiuzy.cn/health/check` | VR 健康接口完整 URL |
| `DOUCHONG_STATUS_PSP_URL` | `https://psp.qianqiuzy.cn/health/check` | PSP 健康接口完整 URL |
| `DOUCHONG_STATUS_VUP_URL` | `https://vup.qianqiuzy.cn/health/check` | VUP 健康接口完整 URL |
| `DOUCHONG_STATUS_BOT_ID` | 未设置 | 多个 OneBot v11 在线时可指定发送方 QQ，默认选择按 ID 排序的首个在线 Bot |
| `DOUCHONG_STATUS_STATE_FILE` | `data/nonebot_plugin_douchong_status/reported.json` | 去重状态路径，相对路径以宿主启动目录为基准 |
