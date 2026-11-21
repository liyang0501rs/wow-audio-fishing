"""Finder entry point for UU Fishing.app."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

APP_NAME = "UU Fishing"


def resource_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parents[1]


def launch_gui(support_dir: Path) -> int:
    from uu_fishing.cli import cmd_gui

    log_dir = support_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (support_dir / "user_templates").mkdir(exist_ok=True)
    config = support_dir / "config.toml"
    if not config.exists():
        default = (resource_root() / "config.toml").read_bytes()
        try:
            stream = config.open("xb")
        except FileExistsError:
            pass
        else:
            with stream:
                stream.write(default)
    return cmd_gui(argparse.Namespace(config=config, log_dir=log_dir))


if __name__ == "__main__":
    if sys.platform != "darwin":
        raise SystemExit("The single-button GUI requires macOS.")
    if not getattr(sys, "frozen", False):
        sys.path.insert(0, str(resource_root()))
    raise SystemExit(launch_gui(Path.home() / "Library" / "Application Support" / APP_NAME))
