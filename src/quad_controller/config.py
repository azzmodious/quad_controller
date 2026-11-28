"""Configuration loading.

Order of precedence, lowest to highest:
1. config/default.yaml shipped in the repo
2. config/local.yaml (git-ignored) or the file named by QC_CONFIG / --config
3. Environment variables QC_TARGET and QC_CONNECTION
4. CLI flags --target and --connection
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "default.yaml"
LOCAL_CONFIG = REPO_ROOT / "config" / "local.yaml"

VALID_TARGETS = ("sitl", "hardware")


class ConfigError(ValueError):
    """Raised when the configuration is missing or invalid."""


@dataclass(frozen=True)
class TargetConfig:
    name: str
    connection: str
    baud: int = 115200


@dataclass(frozen=True)
class MavlinkConfig:
    source_system: int = 1
    source_component: int = 191
    heartbeat_timeout_s: float = 5.0
    status_interval_s: float = 2.0


def _default_streams() -> dict[str, float]:
    return {
        "ATTITUDE": 20.0,
        "GLOBAL_POSITION_INT": 5.0,
        "GPS_RAW_INT": 2.0,
        "SYS_STATUS": 2.0,
        "VFR_HUD": 4.0,
        "EKF_STATUS_REPORT": 2.0,
        "RC_CHANNELS": 5.0,
    }


@dataclass(frozen=True)
class LinkConfig:
    """Phase 1 link layer: our heartbeat, watchdog thresholds and stream rates."""

    heartbeat_hz: float = 1.0
    degraded_after_s: float = 1.0
    lost_after_s: float = 3.0
    # Message name -> rate in Hz that we ask the autopilot for.
    streams: dict[str, float] = field(default_factory=_default_streams)


@dataclass(frozen=True)
class RecorderConfig:
    dir: str = ""  # empty: <log dir>/recordings
    rate_hz: float = 10.0


@dataclass(frozen=True)
class LoggingConfig:
    dir: str = ""
    level: str = "INFO"
    max_bytes: int = 5 * 1024 * 1024
    backup_count: int = 10
    console: bool = True


@dataclass(frozen=True)
class SafetyConfig:
    allow_real_aircraft_arming: bool = False


@dataclass(frozen=True)
class Config:
    target: TargetConfig
    mavlink: MavlinkConfig = field(default_factory=MavlinkConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    link: LinkConfig = field(default_factory=LinkConfig)
    recorder: RecorderConfig = field(default_factory=RecorderConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    sources: tuple[str, ...] = ()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def _section(raw: dict[str, Any], name: str, cls: type) -> Any:
    data = raw.get(name) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"'{name}' must be a mapping")
    allowed = set(cls.__dataclass_fields__)
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"unknown key(s) in '{name}': {', '.join(sorted(unknown))}")
    return cls(**data)


def _link_section(raw: dict[str, Any]) -> LinkConfig:
    data = dict(raw.get("link") or {})
    streams = data.pop("streams", None)
    link = _section({"link": data}, "link", LinkConfig)
    if link.degraded_after_s >= link.lost_after_s:
        raise ConfigError("'link.degraded_after_s' must be less than 'link.lost_after_s'")
    if streams is None:
        return link
    if not isinstance(streams, dict):
        raise ConfigError("'link.streams' must map message names to rates in Hz")
    try:
        rates = {str(k).upper(): float(v) for k, v in streams.items()}
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"'link.streams': {exc}") from exc
    return LinkConfig(**{**link.__dict__, "streams": rates})


def load_config(
    path: str | os.PathLike[str] | None = None,
    *,
    target: str | None = None,
    connection: str | None = None,
    env: dict[str, str] | None = None,
) -> Config:
    """Load and validate configuration."""
    env = os.environ if env is None else env

    raw = _read_yaml(DEFAULT_CONFIG) if DEFAULT_CONFIG.exists() else {}
    sources = [str(DEFAULT_CONFIG)] if DEFAULT_CONFIG.exists() else []

    override_path = path or env.get("QC_CONFIG")
    if override_path:
        p = Path(override_path).expanduser()
        if not p.exists():
            raise ConfigError(f"config file not found: {p}")
    else:
        p = LOCAL_CONFIG if LOCAL_CONFIG.exists() else None
    if p is not None:
        raw = _deep_merge(raw, _read_yaml(p))
        sources.append(str(p))

    target_name = target or env.get("QC_TARGET") or raw.get("target") or "sitl"
    if target_name not in VALID_TARGETS:
        raise ConfigError(f"target must be one of {VALID_TARGETS}, got {target_name!r}")

    targets = raw.get("targets") or {}
    tdata = dict(targets.get(target_name) or {})
    conn = connection or env.get("QC_CONNECTION") or tdata.get("connection")
    if not conn:
        raise ConfigError(f"no connection string configured for target {target_name!r}")
    target_cfg = TargetConfig(
        name=target_name, connection=str(conn), baud=int(tdata.get("baud", 115200))
    )

    return Config(
        target=target_cfg,
        mavlink=_section(raw, "mavlink", MavlinkConfig),
        logging=_section(raw, "logging", LoggingConfig),
        link=_link_section(raw),
        recorder=_section(raw, "recorder", RecorderConfig),
        safety=_section(raw, "safety", SafetyConfig),
        sources=tuple(sources),
    )
