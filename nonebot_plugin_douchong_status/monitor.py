import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from tempfile import NamedTemporaryFile

import httpx
from nonebot import logger
from pydantic import TypeAdapter

from .health import probe

REPORTED_STATE = TypeAdapter(dict[str, set[str]])
UNCERTAIN_STATE = TypeAdapter(dict[str, dict[str, int]])
MAX_UNCONFIRMED_SENDS = 3
MESSAGE_LIMIT = 1000


def save_reported(
    path: Path, reported: dict[str, set[str]], uncertain: dict[str, dict[str, int]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file = NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", delete=False)
    temporary = Path(file.name)
    try:
        with file:
            confirmed = {target: sorted(issues) for target, issues in reported.items()}
            state = {"reported": confirmed, "uncertain": uncertain} if uncertain else confirmed
            json.dump(state, file)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def batches(header: str, issues: dict[str, str]) -> Iterator[tuple[str, set[str]]]:
    message = header
    keys: set[str] = set()
    for key, detail in issues.items():
        line = f"\n- {detail}"
        if keys and len(message) + len(line) > MESSAGE_LIMIT:
            yield message, keys
            message, keys = header, set()
        message += line
        keys.add(key)
    if keys:
        yield message, keys


class Monitor:
    def __init__(self, targets: dict[str, str], state_file: Path) -> None:
        self.targets = targets
        self.state_file = state_file
        self.reported: dict[str, set[str]] | None = None
        self.uncertain: dict[str, dict[str, int]] = {}
        self.dirty = False

    async def _flush(self) -> None:
        if not self.dirty or self.reported is None:
            return
        try:
            await asyncio.to_thread(save_reported, self.state_file, self.reported, self.uncertain)
        except OSError as exc:
            logger.warning(f"斗虫告警状态保存失败（{type(exc).__name__}），下轮重试")
        else:
            self.dirty = False

    async def run(
        self, client: httpx.AsyncClient, send: Callable[[str], Awaitable[bool | None]] | None
    ) -> None:
        if self.reported is None:
            try:
                content = await asyncio.to_thread(self.state_file.read_bytes)
                state = json.loads(content)
                if isinstance(state, dict) and "uncertain" in state:
                    self.uncertain = UNCERTAIN_STATE.validate_python(state["uncertain"])
                    state = state.get("reported")
                self.reported = REPORTED_STATE.validate_python(state)
            except FileNotFoundError:
                self.reported = {}
            except (ValueError, OSError) as exc:
                logger.warning(f"斗虫告警状态读取失败（{type(exc).__name__}）")
                self.reported = {}
                self.uncertain = {}

        results = await asyncio.gather(*(probe(client, url) for url in self.targets.values()))
        missing_bot = False
        for (label, url), result in zip(self.targets.items(), results, strict=True):
            target = f"{label} {url}"
            active = {
                f"{scope}:{issue}": detail
                for scope, issues in result.checks.items()
                for issue, detail in issues.items()
            }
            previous = self.reported.get(target, set())
            uncertain = self.uncertain.get(target, {})
            unresolved = {
                issue
                for issue in previous | uncertain.keys()
                if not result.healthy
                and (issue in active or issue.split(":", 1)[0] not in result.checks)
            }
            for issue in uncertain.keys() - unresolved:
                del uncertain[issue]
                self.dirty = True
            if not uncertain:
                self.uncertain.pop(target, None)
            retained = previous & unresolved
            if retained != previous:
                self.dirty = True
                if retained:
                    self.reported[target] = retained
                else:
                    self.reported.pop(target, None)

            pending = {
                issue: detail for issue, detail in active.items()
                if issue not in retained and uncertain.get(issue, 0) < MAX_UNCONFIRMED_SENDS
            }
            if send is None:
                missing_bot |= bool(pending)
                continue
            for message, delivered in batches(f"【斗虫健康告警】{label}\n{url}", pending):
                receipt = await send(message)
                if receipt is False:
                    break
                if receipt is None:
                    for issue in delivered:
                        uncertain[issue] = uncertain.get(issue, 0) + 1
                    self.uncertain[target] = uncertain
                    if any(uncertain[issue] == MAX_UNCONFIRMED_SENDS for issue in delivered):
                        logger.warning(f"{label} 部分告警连续三次未确认，暂停重发直至故障恢复")
                else:
                    retained.update(delivered)
                    self.reported[target] = retained
                    for issue in delivered:
                        uncertain.pop(issue, None)
                    if not uncertain:
                        self.uncertain.pop(target, None)
                self.dirty = True
                await self._flush()

        if missing_bot:
            logger.warning("斗虫巡检发现异常，当前无可用的 OneBot v11 Bot，下轮重新检查")
        await self._flush()
