"""跟随高手 · 数据体检 (2026-09-25)。

模拟盘数据: data/history_bulk (站点给出调仓前后仓位, 重建可直接用) —— 报覆盖面与新鲜度。
实盘数据:   expert_trades 视图 (crawl_trade market_type=live, rtV3) —— 只有平台原始仓位文字
            (weight_label) 的记录能正确重建 (买卖都是 成交金额/组合总资产 分档, 每笔卖出按清仓处理)。
            用户决策: 缺原文的组合/时段标为不可用, 实盘回测时排除; 体检不通过时实盘回测一律显示
            "数据不可用"并隐藏。
结果缓存到 data/health.json (paths.HEALTH_CACHE; 重算: compute(refresh=True))。

迁移自 scripts/expert_follow/data_health.py (2026-10-16):
  * CACHE 由 logs/expert_follow/data_health.json 改为 paths.HEALTH_CACHE (data/health.json);
    live_verify 文件路径由 PROJECT_ROOT/models/... 改为 paths.MODELS_DIR/expert_follow/...;
  * 惰性导入改绝对路径 (strategy.pit_nav / strategy.backtest_v2 / strategy.prices / core.db);
  * 聚合查询的 xcq_expert_trades 表 → 本地 SQLite expert_trades 视图 (core/db.py
    EXPERT_TRADES_VIEW, 字段同源表); 视图基于 crawl_trade, 查询前先 ensure_schema (爬取基表)
    + ensure_strategy_schema (视图) —— 对应 MySQL 版"表由平台 DDL 预建"的语义;
  * 其余逐行原样 (返回结构 sim/live/generated_at 等键不变)。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Optional

from paths import HEALTH_CACHE, MODELS_DIR

CACHE = HEALTH_CACHE
DEFAULT_RULES = {"min_usable_experts": 20, "min_labeled_pct": 95, "min_days": 60}


def _sim() -> Dict[str, Any]:
    from paths import HISTORY_BULK_DIR
    fps = list(HISTORY_BULK_DIR.glob("*.json"))
    last = max((fp.stat().st_mtime for fp in fps), default=0)
    return {"source": "history_bulk (模拟盘组合调仓历史, 含调仓前后仓位)", "n_portfolios": len(fps),
            "updated_at": datetime.fromtimestamp(last).isoformat(timespec="seconds") if last else None,
            "ok": len(fps) >= 500, "reason": "" if len(fps) >= 500 else "模拟盘组合历史不足 500 个"}


def _live(rules: Dict[str, Any]) -> Dict[str, Any]:
    from core.db import ensure_schema as _ensure_crawl, ensure_strategy_schema, q
    from paths import DB_PATH
    from strategy.backtest_v2 import Ctx
    from strategy.prices import trading_calendar as _cal
    # [2026-10-16] SQLite: expert_trades 视图建在 crawl_trade 上, 基表/视图随Schema落库
    # (MySQL 版 xcq_expert_trades 由平台 DDL 预建, 这里等价补上初始化)
    _ensure_crawl(DB_PATH)
    ensure_strategy_schema(DB_PATH)
    agg = q("SELECT COUNT(*) n, SUM(weight_label IS NOT NULL) nl, COUNT(DISTINCT expert_id) e, "
            "MIN(trade_date) mn, MAX(trade_date) mx FROM expert_trades WHERE market_type='live' AND stock_code<>''")[0]
    n, nl = int(agg["n"] or 0), int(agg["nl"] or 0)
    ctx = Ctx(_cal()[-1], [], env="live")
    per = ctx.live_health or {}
    usable = [e for e, v in per.items() if v.get("usable")]
    days = 0
    if usable:
        import numpy as np
        cover = np.isfinite(ctx.nav).any(axis=1)
        days = int(cover.sum())
    labeled_pct = round(nl / n * 100, 1) if n else 0.0
    issues = []
    if len(usable) < rules["min_usable_experts"]:
        issues.append(f"可用组合 {len(usable)} < {rules['min_usable_experts']}")
    if labeled_pct < rules["min_labeled_pct"]:
        issues.append(f"有原始仓位文字的记录 {labeled_pct}% < {rules['min_labeled_pct']}%")
    if days < rules["min_days"]:
        issues.append(f"可重建交易日 {days} < {rules['min_days']}")
    phantom = sum(int(v.get("phantom_end") or 0) for v in per.values())
    verify = None                              # [2026-09-26 c10] 平台公布收益 vs 重建收益 (scripts/experts/live_verify.py)
    vfp = MODELS_DIR / "expert_follow" / "live_verify_latest.json"
    if vfp.exists():
        try:
            verify = json.loads(vfp.read_text(encoding="utf-8"))
            for e, why in (verify.get("flagged") or {}).items():
                if e in per:
                    per[e]["verify_flags"] = why
        except ValueError:
            verify = None
    return {"source": "expert_trades market_type=live (东财实盘 rtV3)", "n_records": n, "n_labeled": nl,
            "labeled_pct": labeled_pct, "n_portfolios": int(agg["e"] or 0), "n_usable": len(usable),
            "first_date": str(agg["mn"] or ""), "last_date": str(agg["mx"] or ""), "days_covered": days,
            "phantom_end": phantom, "rules": rules, "ok": not issues, "reason": "；".join(issues),
            "verify": None if not verify else {"as_of": verify.get("as_of"), "boards": {k: {x: v.get(x) for x in
                      ("window", "n", "corr", "median_abs_err_pp", "within_2pp")} for k, v in (verify.get("boards") or {}).items()},
                      "n_flagged": len(verify.get("flagged") or {})},
            "portfolios": {e: {k: v for k, v in info.items()} for e, info in per.items()}}


def compute(refresh: bool = False, rules: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if CACHE.exists() and not refresh:
        try:
            return json.loads(CACHE.read_text(encoding="utf-8"))
        except ValueError:
            pass
    r = {**DEFAULT_RULES, **(rules or {})}
    out = {"generated_at": datetime.now().isoformat(timespec="seconds"), "sim": _sim(), "live": _live(r)}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    tmp.replace(CACHE)
    return out


def env_ok(env: str, rules: Optional[Dict[str, Any]] = None, refresh: bool = False) -> Dict[str, Any]:
    h = compute(refresh=refresh, rules=rules)
    x = h.get(env) or {}
    return {"ok": bool(x.get("ok")), "reason": x.get("reason") or "", "generated_at": h.get("generated_at")}
