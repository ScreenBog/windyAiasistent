"""
Система плагинов Windy AI Assistant.

Каждый плагин — файл plugins/*.py с функцией setup(register).
register(tool_name, handler) добавляет инструмент в реестр.
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

ToolHandler = Callable[[dict], str]
RegisterFn = Callable[[str, ToolHandler], None]

_loaded_plugins: list[str] = []


def _import_plugin_module(path: Path):
    """Динамический импорт файла плагина."""
    module_name = f"windy_plugin_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Не удалось загрузить {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_plugins(
    plugins_dir: Path | None = None,
    register: RegisterFn | None = None,
) -> list[str]:
    """
    Сканирует plugins/ и вызывает setup(register) в каждом модуле.
    Возвращает список загруженных имён файлов.
    """
    if not config.PLUGINS_ENABLED:
        logger.info("Плагины отключены в настройках")
        return []

    if register is None:
        from tools import register_tool as register

    plugins_dir = plugins_dir or config.PLUGINS_DIR
    loaded: list[str] = []

    for path in sorted(plugins_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            module = _import_plugin_module(path)
            setup = getattr(module, "setup", None)
            if callable(setup):
                setup(register)
                loaded.append(path.name)
                logger.info("Плагин загружен: %s", path.name)
            else:
                logger.warning("Плагин %s не содержит setup(register)", path.name)
        except Exception as exc:
            logger.error("Ошибка загрузки плагина %s: %s", path.name, exc)

    _loaded_plugins.clear()
    _loaded_plugins.extend(loaded)
    return loaded


def get_loaded_plugins() -> list[str]:
    return list(_loaded_plugins)