"""web 包：FastAPI 应用（替代原 Streamlit UI 层）。

架构参考源项目 xcq-platform/scripts/dashboard：
  * create_app() 工厂 + 模块级 app 单例（uvicorn web.app:app）
  * 扁平 routes_*.py 各自持有 APIRouter(prefix="/api/...")
  * 静态资源 StaticFiles 挂载，单 HTML 首页（hash 路由 + vanilla JS）
  * 后台任务 = 模块级 dict + subprocess.Popen(run_job.py) + 状态 JSON 文件

与源项目的差异：业务模块直连（src/ 下的 pipeline/strategy），无 handlers 兼容层；
存储为本地 SQLite（core.db），无 MySQL。
"""

from __future__ import annotations

import sys
from pathlib import Path

#: src/ 入 sys.path（web/ 与 src/ 同级于项目根）
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
for _p in (str(_ROOT), str(_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
