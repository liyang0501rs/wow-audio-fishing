"""Strict TOML configuration; paths are relative to the configuration, not the shell."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import math
from pathlib import Path
import tomllib
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config.toml"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AudioConfig:
    device: str = "BlackHole"
    capture_rate: int = 48000
    block_seconds: float = 0.1
    read_timeout: float = 3.0
    queue_seconds: float = 2.0
    windows_device: str = "default"


@dataclass(frozen=True)
class DetectorConfig:
    threshold: float = 0.66
    onset_seconds: float = 0.6
    refractory_seconds: float = 3.0
    use_builtin: bool = False
    templates_dir: str = "user_templates"


@dataclass(frozen=True)
class TargetConfig:
    app_contains: str = ""
    title_contains: str = ""


@dataclass(frozen=True)
class InputConfig:
    action_key: str = "`"
    hold_seconds: float = 0.06
    start_delay_seconds: float = 5.0


@dataclass(frozen=True)
class TimingConfig:
    settle_seconds: float = 1.8
    bite_timeout_seconds: float = 31.0
    loot_wait_seconds: float = 0.5
    retry_wait_seconds: float = 1.0
    reel_delay_seconds: float = 0.0  # 0 = reel inside the block that detected the bite.


@dataclass(frozen=True)
class LimitsConfig:
    max_reels: int = 0
    duration_minutes: float = 0.0
    max_consecutive_timeouts: int = 3
    silence_timeout_seconds: float = 60.0


@dataclass(frozen=True)
class AppConfig:
    audio: AudioConfig = AudioConfig()
    detector: DetectorConfig = DetectorConfig()
    target: TargetConfig = TargetConfig()
    input: InputConfig = InputConfig()
    timing: TimingConfig = TimingConfig()
    limits: LimitsConfig = LimitsConfig()
    base_dir: Path = PROJECT_ROOT

    @property
    def templates_path(self) -> Path | None:
        text = self.detector.templates_dir
        if not text:
            return None
        path = Path(text).expanduser()
        return path.resolve() if path.is_absolute() else (self.base_dir / path).resolve()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["base_dir"] = str(self.base_dir)
        data["resolved_templates_dir"] = str(self.templates_path) if self.templates_path else None
        return data


_SECTIONS = {
    "audio": AudioConfig, "detector": DetectorConfig, "target": TargetConfig,
    "input": InputConfig, "timing": TimingConfig, "limits": LimitsConfig,
}


def _number(value: Any, name: str, lo: float, hi: float, *, integer: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} 必须是{'整数' if integer else '数字'}")
    if integer and not isinstance(value, int):
        raise ConfigError(f"{name} 必须是整数")
    if not math.isfinite(value) or not lo <= value <= hi:
        raise ConfigError(f"{name} 必须在 {lo:g}～{hi:g} 之间")


def _string(value: Any, name: str, *, empty: bool = True) -> None:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ConfigError(f"{name} 必须是{'非空' if not empty else ''}字符串")


def validate_config(cfg: AppConfig) -> AppConfig:
    a, d, i, t, limits = cfg.audio, cfg.detector, cfg.input, cfg.timing, cfg.limits
    _string(a.device, "audio.device", empty=False)
    _string(a.windows_device, "audio.windows_device", empty=False)
    _number(a.capture_rate, "audio.capture_rate", 16000, 192000, integer=True)
    _number(a.block_seconds, "audio.block_seconds", 0.02, 0.5)
    _number(a.read_timeout, "audio.read_timeout", 0.5, 30)
    _number(a.queue_seconds, "audio.queue_seconds", 0.2, 10)
    if a.queue_seconds < 2 * a.block_seconds:
        raise ConfigError("audio.queue_seconds 至少应为两块音频长度")
    _number(d.threshold, "detector.threshold", 0.01, 1)
    _number(d.onset_seconds, "detector.onset_seconds", 0.25, 2)
    _number(d.refractory_seconds, "detector.refractory_seconds", 0.1, 30)
    if type(d.use_builtin) is not bool:
        raise ConfigError("detector.use_builtin 必须是 true 或 false")
    _string(d.templates_dir, "detector.templates_dir")
    if not d.use_builtin and not d.templates_dir.strip():
        raise ConfigError("关闭内置模板时必须提供 detector.templates_dir")
    _string(cfg.target.app_contains, "target.app_contains")
    _string(cfg.target.title_contains, "target.title_contains")
    _string(i.action_key, "input.action_key", empty=False)
    # The platform layer validates the supported physical key as well.
    if len(i.action_key.strip()) > 20:
        raise ConfigError("input.action_key 不是有效的单键名称")
    _number(i.hold_seconds, "input.hold_seconds", 0.01, 0.5)
    _number(i.start_delay_seconds, "input.start_delay_seconds", 0, 30)
    _number(t.settle_seconds, "timing.settle_seconds", 0.1, 10)
    _number(t.bite_timeout_seconds, "timing.bite_timeout_seconds", 2, 120)
    _number(t.reel_delay_seconds, "timing.reel_delay_seconds", 0.0, 30)
    _number(t.loot_wait_seconds, "timing.loot_wait_seconds", 0.1, 30)
    _number(t.retry_wait_seconds, "timing.retry_wait_seconds", 0.1, 30)
    _number(limits.max_reels, "limits.max_reels", 0, 1000000, integer=True)
    _number(limits.duration_minutes, "limits.duration_minutes", 0, 1440)
    _number(limits.max_consecutive_timeouts, "limits.max_consecutive_timeouts", 1, 100, integer=True)
    _number(limits.silence_timeout_seconds, "limits.silence_timeout_seconds", 0, 3600)
    return cfg


def config_from_dict(data: dict[str, Any], *, base_dir: Path = PROJECT_ROOT) -> AppConfig:
    unknown = set(data) - (set(_SECTIONS) | {"version"})
    if unknown:
        raise ConfigError("未知配置节: " + ", ".join(sorted(unknown)))
    if type(data.get("version", 1)) is not int or data.get("version", 1) != 1:
        raise ConfigError("仅支持 version = 1 的配置")
    values: dict[str, Any] = {}
    for name, cls in _SECTIONS.items():
        section = data.get(name, {})
        if not isinstance(section, dict):
            raise ConfigError(f"[{name}] 必须是 TOML 表")
        extra = set(section) - {f.name for f in fields(cls)}
        if extra:
            raise ConfigError(f"[{name}] 中未知选项: " + ", ".join(sorted(extra)))
        values[name] = cls(**section)
    return validate_config(AppConfig(**values, base_dir=base_dir.resolve()))


def load_config(path: str | Path | None = None) -> AppConfig:
    candidate = Path(path).expanduser().resolve() if path is not None else DEFAULT_CONFIG
    if not candidate.exists():
        if path is not None:
            raise ConfigError(f"配置文件不存在: {candidate}")
        return config_from_dict({}, base_dir=PROJECT_ROOT)
    try:
        with candidate.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"无法读取配置 {candidate}: {exc}") from exc
    return config_from_dict(data, base_dir=candidate.parent)
