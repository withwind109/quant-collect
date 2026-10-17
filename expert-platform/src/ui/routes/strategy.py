import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[2]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ui.routes._route_loader import render_view  # noqa: E402


render_view("ui.views.strategy")
