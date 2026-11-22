from pathlib import Path

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, field_validator


class Config(BaseModel):
    model_config = ConfigDict(validate_default=True)

    douchong_status_vr_url: AnyHttpUrl = AnyHttpUrl("https://vr.qianqiuzy.cn/health/check")
    douchong_status_psp_url: AnyHttpUrl = AnyHttpUrl("https://psp.qianqiuzy.cn/health/check")
    douchong_status_vup_url: AnyHttpUrl = AnyHttpUrl("https://vup.qianqiuzy.cn/health/check")
    douchong_status_bot_id: str | None = None
    douchong_status_state_file: Path = Path("data/nonebot_plugin_douchong_status/reported.json")

    @field_validator("douchong_status_vr_url", "douchong_status_psp_url", "douchong_status_vup_url")
    @classmethod
    def public_health_url(cls, url: AnyHttpUrl) -> AnyHttpUrl:
        if any(part is not None for part in (url.username, url.password, url.query, url.fragment)):
            raise ValueError("Health URLs cannot contain credentials, query strings or fragments")
        return url
