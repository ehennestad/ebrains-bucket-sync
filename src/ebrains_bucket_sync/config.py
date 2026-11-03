"""The configuration folder of the user, where the package keeps its files.

The folder is the one that spec/README.md#configuration-folder gives for
the platform, which the MATLAB toolbox computes the same way, so that the
two tools read one registry of virtual buckets.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

APP_FOLDER = "ebrains-bucket-sync"
"""Name of the configuration folder on every platform."""

CONFIG_DIR_VARIABLE = "EBRAINS_BUCKET_SYNC_CONFIG_DIR"
"""Environment variable that names the configuration folder instead."""


def config_dir(
    *,
    platform: str = sys.platform,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """The configuration folder, which need not exist yet.

    Args:
        platform: sys.platform of the machine, for tests.
        environ: The environment, os.environ by default.
        home: The home folder of the user, Path.home() by default.
    """
    if environ is None:
        environ = os.environ
    override = environ.get(CONFIG_DIR_VARIABLE, "")
    if override:
        return Path(override)
    if home is None:
        home = Path.home()
    if platform == "darwin":
        return home / "Library" / "Application Support" / APP_FOLDER
    if platform == "win32":
        local = environ.get("LOCALAPPDATA", "")
        base = Path(local) if local else home / "AppData" / "Local"
        return base / APP_FOLDER
    xdg = environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg else home / ".config"
    return base / APP_FOLDER
