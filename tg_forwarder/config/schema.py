from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ReplacementRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    regex: str
    replace: str = ""

    @field_validator("regex")
    @classmethod
    def validate_regex(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid regex `{value}`: {exc}") from exc
        return value


class TemplateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    replacements: list[ReplacementRule] = Field(default_factory=list)


class FiltersConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    whitelist: list[str] = Field(default_factory=list)
    blacklist: list[str] = Field(default_factory=list)

    @field_validator("whitelist", "blacklist")
    @classmethod
    def normalize_keywords(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item and item.strip()]


class ForwardJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    source: int
    target: int
    mode: Literal["userbot", "bot"] = "userbot"
    filters: FiltersConfig = Field(default_factory=FiltersConfig)
    use_template: str | None = None
    modifications: list[ReplacementRule] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("job name must not be empty")
        return stripped

    @model_validator(mode="after")
    def validate_source_target(self) -> ForwardJob:
        if self.source == self.target:
            raise ValueError(f"job `{self.name}` has identical source and target")
        return self


class UserbotSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_id: int
    api_hash: str
    phone: str

    @field_validator("api_hash", "phone")
    @classmethod
    def non_empty_str(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped


class BotSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str

    @field_validator("token")
    @classmethod
    def token_non_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("bot token must not be empty")
        return stripped


class SessionsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    userbot: UserbotSession
    bot: BotSession | None = None


class SelfForwardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    target: int | Literal["saved"] = "saved"
    strip_attribution: bool = True
    append_source: bool = False
    source_format: str = "\n\n📌 来源: {source_name}"
    apply_modifications: bool = False
    album_wait_seconds: float = 2.0

    @field_validator("album_wait_seconds")
    @classmethod
    def positive_wait(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("album_wait_seconds must be > 0")
        return value


class ProtectedExtractorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    max_file_size_mb: int = 100
    temp_dir: str = "/tmp/tg-extract"
    rate_limit: int = 5

    @field_validator("max_file_size_mb", "rate_limit")
    @classmethod
    def positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be > 0")
        return value


class MonitoringConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_channel: int | None = None
    admin_id: int | None = None


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sessions: SessionsConfig
    self_forward: SelfForwardConfig = Field(default_factory=SelfForwardConfig)
    templates: dict[str, TemplateConfig] = Field(default_factory=dict)
    jobs: list[ForwardJob] = Field(default_factory=list)
    protected_extractor: ProtectedExtractorConfig = Field(
        default_factory=ProtectedExtractorConfig
    )
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)

    @field_validator("templates")
    @classmethod
    def validate_template_names(
        cls, value: dict[str, TemplateConfig]
    ) -> dict[str, TemplateConfig]:
        normalized: dict[str, TemplateConfig] = {}
        for name, template in value.items():
            key = name.strip()
            if not key:
                raise ValueError("template name must not be empty")
            normalized[key] = template
        return normalized

    @model_validator(mode="after")
    def validate_integrity(self) -> AppConfig:
        template_names = set(self.templates.keys())
        seen_jobs: set[str] = set()
        requires_bot = False

        for job in self.jobs:
            if job.name in seen_jobs:
                raise ValueError(f"duplicated job name `{job.name}`")
            seen_jobs.add(job.name)

            if job.use_template and job.use_template not in template_names:
                raise ValueError(
                    f"job `{job.name}` references missing template `{job.use_template}`"
                )
            if job.mode == "bot":
                requires_bot = True

        if requires_bot and self.sessions.bot is None:
            raise ValueError(
                "at least one bot-mode job exists but sessions.bot is not configured"
            )

        return self
