# nonebot-plugin-douchong-status

NoneBot2 插件。启动后每 3 分钟并行请求 VR、PSP、VUP 的 `/health/check`，新故障通过 OneBot v11 私信发送给 QQ `308025580`。

- 三个目标分别是 `https://vr.qianqiuzy.cn`、`https://psp.qianqiuzy.cn` 和 `https://vup.qianqiuzy.cn`，每个请求最多等待 10 秒。
- 健康和恢复时保持静默。接口不可达、返回异常或报告服务降级时，私信包含出问题的主机与可用的诊断明细，例如异常依赖、未连接房间和失败的上游接口。
- 同一主机上的持续故障只在首次成功发送时通报。确认恢复后再次出现会重新通报。接口暂时不可达或采样过期时，保留此前已通报的组件故障记录。
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
