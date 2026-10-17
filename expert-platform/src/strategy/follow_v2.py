"""迁移自 scripts/experts/follow_v2.py (2026-10-16)。变更 (仅导入路径/存储后端/文件路径, 业务逻辑逐行保持):
  * 导入: scripts.* -> core.* / strategy.*; 回测引擎别名 V -> FB (strategy.backtest_v2);
    perf 按任务约定 `from core import perf` 引入 (装饰器相应为 @perf.timed, 源为 @_perf_timed);
  * 路径: SET_DIR (experts_crawl/data/follow_v2) -> paths.FOLLOW_V2_DIR;
    关注列表 SQL 原样 (xcq_expert_watchlist, 与 strategy/engine.py 约定一致);
    一处日志文案里的 "qlib 行情" 随价格后端改为 "行情"。

「跟随社区高手 v2」实盘信号 (2026-09-24)。

与回测(strategy/backtest_v2.py)**共用同一套选人/加权/目标仓位函数**(Ctx /
follow_set / target_weights), 保证模拟盘跑的就是回测验证过的那套逻辑, 不另写一份。

每日(盘前, 推荐台账生成时):
  data_date(T) 收盘后的跟随集合 = 关注列表 ∪ 按 spec.metric(默认当日收益) 排名前 top_n 的社区高手;
  资金权重 = 集合内线性名次权重; 目标仓位 = Σ 资金权重 × 高手仓位, 单票≤20%, <1% 忽略。
  → 台账: 每只目标股一条"买入", target_weight=目标仓位, meta 里记是哪些高手贡献的;
  → 当日跟随集合落盘 data/follow_v2/<pred_date>.json, 供盘中跟单(paper_trading.on_expert_trades)用。

数据依赖: data/history_bulk(夜间增量拉取) + 本地价格库(prices)到 T 的行情。数据没覆盖到 T 时返回 None
(台账记 missing, 账户当天不据此交易), 不拿旧数据硬凑。
"""
from __future__ import annotations

import json
import threading
from typing import Any, Dict, List, Optional

from core import perf
from core.logger import logger
from paths import FOLLOW_V2_DIR

STRATEGY_ID = "expert_follow:v2"
NAME = "跟随社区高手v2(关注+当日收益前10·收益加权)"
# [2026-09-24] 全量名单回测(4077个组合, 2026-02~09)只有 v2_day_t1(当日收益前10·T+1开盘)通过闸门, 也正是
# 需求原文"每日收益排名前10"的字面定义; 近20日收益/盘中跟单三个变体都是负收益, 见 docs/跟随社区高手v2_方案与回测_20260924.md
DEFAULT_VARIANT = "v2_day_t1"
DEFAULT_SPEC = {"engine": "expert_follow_v2", "top_n": 10, "metric": "day", "weighting": "rank",
                "exec": "t1_open", "stock_cap": 0.20, "exit_mode": "target",
                "arena_ref": "expert_follow_v2:" + DEFAULT_VARIANT}

_CTX: Dict[str, Any] = {}
_LOCK = threading.Lock()


@perf.timed("follow_v2.ctx")   # [2026-09-26] 性能测量 (core/perf.py)
def _ctx(data_date: str):
    from core.db import q
    from strategy import backtest_v2 as FB
    with _LOCK:
        # 缓存里的上下文若是在行情还没同步到 data_date 时建的, 行情到了之后要重建(否则会一直"数据没覆盖")
        if _CTX.get("date") != data_date or data_date not in _CTX["ctx"].px.didx:
            # [2026-09-25 决策] 实盘高手(market_type=live)只入库+推送观察, 不进模拟盘信号
            watch = [(r["expert_id"], r.get("name") or r["expert_id"])
                     for r in q("SELECT expert_id, name FROM xcq_expert_watchlist "
                                "WHERE COALESCE(market_type,'sim') <> 'live'")]
            _CTX.clear()
            _CTX.update({"date": data_date, "ctx": FB.Ctx(data_date, watch)})
        return _CTX["ctx"]


def compute(data_date: str, spec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """{follow_set: [...], targets: [{code,name,weight,experts}]}; 数据没覆盖到 data_date 返回 None。"""
    from strategy import backtest_v2 as FB
    ctx = _ctx(data_date)
    if data_date not in ctx.px.didx:
        logger.warning(f"[follow-v2] 行情还没有 {data_date}, 不生成")
        return None
    t = ctx.px.didx[data_date]
    cfg = {"top_n": int(spec.get("top_n", 10)), "metric": spec.get("metric", "ret20"),
           "weighting": spec.get("weighting", "rank")}
    fs = FB.follow_set(ctx, t, cfg)
    tw = FB.target_weights(ctx, fs, t)
    names: Dict[str, str] = {}
    contrib: Dict[int, List[str]] = {}
    for e, we, _ in fs:
        for r in ctx.hist[e]["records"]:
            names.setdefault(r["stock_code"], r.get("stock_name") or "")
        for j, w in ctx.holdings(e)[t].items():
            if j in tw:
                contrib.setdefault(j, []).append(f"{(ctx.hist[e]['name'] or e)[:10]}({we * w:.1%})")
    targets = sorted(({"code": ctx.px.codes[j], "name": names.get(ctx.px.codes[j], ""), "weight": round(w, 4),
                       "experts": contrib.get(j, [])} for j, w in tw.items()), key=lambda x: -x["weight"])
    follow = [{"expert_id": e, "name": ctx.hist[e]["name"] or ctx.watch_names.get(e, e), "weight": round(we, 4),
               "metric": (None if not (m == m) or m in (float("inf"), float("-inf")) else round(m, 4)),
               "watchlist": e in ctx.watch} for e, we, m in fs]
    return {"data_date": data_date, "metric": cfg["metric"], "follow_set": follow, "targets": targets}


def generate_recs(spec: Dict[str, Any], data_date: str, pred_date: str):
    """paper_signals.generate 的 expert_follow_v2 分支。"""
    from strategy.ledger import Rec
    res = compute(data_date, spec)
    if res is None:
        return None
    FOLLOW_V2_DIR.mkdir(parents=True, exist_ok=True)
    (FOLLOW_V2_DIR / f"{pred_date}.json").write_text(json.dumps({**res, "pred_date": pred_date, "spec": spec},
                                                                 ensure_ascii=False, indent=1), encoding="utf-8")
    recs = []
    for i, x in enumerate(res["targets"], 1):
        recs.append(Rec(code=x["code"], name=x["name"], action="买入", reason_code="FOLLOW_V2",
                        reason_text=f"跟随高手v2 · 目标仓位{x['weight']:.1%} · " + "、".join(x["experts"][:3]),
                        score=x["weight"], rank_no=i, target_weight=x["weight"],
                        pool_ref=f"follow_v2:{pred_date}", meta={"experts": x["experts"]}))
    logger.info(f"[follow-v2] {data_date}→{pred_date}: 跟随 {len(res['follow_set'])} 位高手, 目标 {len(recs)} 只")
    return recs


def follow_set_for(pred_date: str) -> Optional[Dict[str, Any]]:
    fp = FOLLOW_V2_DIR / f"{pred_date}.json"
    return json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else None
