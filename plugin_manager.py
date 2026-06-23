"""
Плагины: файл plugins/*.py с функцией setup(register).
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Callable

import bootstrap  # noqa: F401
import config

logger = logging.getLogger(__name__)

_loaded: list[str] = []


def load_plugins(register: Callable | None = None) -> list[str]:
    if not config.PLUGINS_ENABLED:
        return []
    if register is None:
        from tools import register_tool as register

    out: list[str] = []
    for path in sorted((config.PLUGINS_DIR).glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            mod_name = f"windy_plugin_{path.stem}"
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if not spec or not spec.loader:
                continue
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            setup = getattr(mod, "setup", None)
            if callable(setup):
                setup(register)
                out.append(path.name)
                logger.info("plugin: %s", path.name)
        except Exception as exc:
            logger.error("plugin %s: %s", path.name, exc)

    _loaded.clear()
    _loaded.extend(out)
    return out


def get_loaded_plugins() -> list[str]:
    return list(_loaded)