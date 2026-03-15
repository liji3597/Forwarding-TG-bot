from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from tg_forwarder.config.schema import AppConfig

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


class ConfigLoadError(RuntimeError):
    pass


def _substitute_env_in_str(value: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        env_value = os.getenv(var_name)
        if env_value is None:
            raise ConfigLoadError(f"missing environment variable: {var_name}")
        return env_value

    return _ENV_PATTERN.sub(_replace, value)


def _substitute_env(data: Any) -> Any:
    if isinstance(data, dict):
        return {key: _substitute_env(value) for key, value in data.items()}
    if isinstance(data, list):
        return [_substitute_env(item) for item in data]
    if isinstance(data, str):
        return _substitute_env_in_str(data)
    return data


def _format_validation_error(exc: ValidationError) -> str:
    details: list[str] = []
    for err in exc.errors(include_url=False):
        loc = ".".join(str(part) for part in err.get("loc", []))
        msg = err.get("msg", "invalid value")
        details.append(f"{loc}: {msg}" if loc else msg)
    return "config validation failed:\n- " + "\n- ".join(details)


def load_config(path: str | Path) -> AppConfig:
    file_path = Path(path)
    if not file_path.exists():
        raise ConfigLoadError(f"config file not found: {file_path}")

    try:
        raw = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigLoadError(f"failed to read config file: {file_path}") from exc

    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigLoadError(f"invalid YAML in {file_path}: {exc}") from exc

    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise ConfigLoadError("top-level YAML value must be an object")

    substituted = _substitute_env(parsed)

    try:
        return AppConfig.model_validate(substituted)
    except ValidationError as exc:
        raise ConfigLoadError(_format_validation_error(exc)) from exc
