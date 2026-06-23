"""Bootstrap: гарантирует sys.path при любом способе запуска."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent


def ensure_project_path() -> Path:
    root = str(PROJECT_DIR)
    if root not in sys.path:
        sys.path.insert(0, root)
    return PROJECT_DIR


ensure_project_path()