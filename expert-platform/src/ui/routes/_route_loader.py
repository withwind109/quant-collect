"""Streamlit route wrapper helpers（参考 multi-factor-stock ui/routes/_route_loader.py）。"""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[2]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def render_view(module_path: str) -> None:
    module = import_module(module_path)
    module.render()
