"""跟随高手 v3 —— 策略配置 (默认值 = 2026-09-24 用户决策, 页面可改, 改了就是新版本)。

筛选(都用"信号日前一交易日收盘及以前、最近 window 个交易日"的数据, 逐日重算, 不用未来):
  ① 窗口收益 > min_ret                         (负收益排除)
  ② 窗口最大回撤 ≤ max_mdd                      (回撤超过 20% 排除)
  ③ 活跃: 窗口内调仓 ≥ min_trades 次, 且最近 recent_days 个交易日内有交易
  ④ 胜率: 窗口内平仓(减仓)笔数 ≥ min_closed, 且扣成本后盈利的平仓占比 ≥ min_win
正向批次: 四规则全部通过者按窗口收益排序, 取前 pos_tiers 各比例 (先过滤, 再取前 X%)
反向批次: 窗口收益 < 0 且活跃(③) 的高手按收益从低到高, 取倒数 neg_tiers 各比例 (1.0 = 全部负收益)

迁移自 scripts/expert_v3/config.py (2026-10-16)。变更:
  * live_sample_warning 的查询: scripts.core.db.q -> core.db.q; 表 xcq_expert_trades -> expert_trades
    (本地 SQLite 视图, 已过滤 market_type='live'; 原 WHERE 条件保留, 无害);
  * DEFAULT_SPEC / VARIANT_LABELS / normalize / spec_hash / pct_label 原样。
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Dict

from core.db import q

DEFAULT_SPEC: Dict[str, Any] = {
    # 模式: screen_rank = v3.1 长窗口只做质量筛选、短窗口排名 (2026-09-24 起默认);
    #       tiers = 第一轮, 用同一个长窗口既筛选又按收益分批 (已证实长窗口收益会反转)
    "mode": "screen_rank",
    "rank_windows": [1, 3, 5],   # v3.1 排名周期 (交易日)
    "top_k": 10,                 # v3.1 另跟前 K 名 (与 v2 可比); 百分比批次沿用 pos_tiers
    "window": 120,            # 筛选窗口(交易日), 页面可调
    "min_ret": 0.0,
    "max_mdd": 0.20,
    "min_trades": 8,
    "recent_days": 20,
    "min_closed": 5,
    "min_win": 0.55,
    "win_cost": 0.003,        # 平仓胜负判定扣的往返成本
    "min_pool": 10,           # 当天通过四规则的人数 < 此值 → 正向批次当天不出信号
    "pos_tiers": [0.05, 0.10, 0.20],
    "neg_tiers": [0.05, 0.10, 0.20, 0.30, 0.40, 0.50, 1.0],
    # 模型 / 执行 (训练前定死, 不调参)
    "label_h": 5,             # 买入模型标签: 跟单后持有 5 日扣成本超额 > 0
    "accept_q": 0.60,         # 买入模型: p ≥ 训练集 60 分位才跟
    "veto_q": 0.80,           # 反向模型: p(亏) ≥ 训练集 80 分位 → 否决这笔跟单
    "ex_q": 0.80,             # 卖出模型: p(3日跌超3%) ≥ 训练集 80 分位 → 次日开盘卖
    "min_train": 150,         # 每个测试月至少这么多条已知标签的训练样本, 否则该月该模型不出预测
    "stop_loss": 0.08,
    "max_hold": 20,
    "stock_cap": 0.20,
    "capital": 1_000_000.0,
    # 数据环境 [2026-09-25]: sim=模拟盘组合全量历史(history_bulk) / live=实盘 rtV3 流水
    # (xcq_expert_trades, 每日增量累积)。实盘样本不足时允许运行但结果页打大字警告。
    "data_env": "sim",
    # 限定高手宇宙 (2026-09-25 意图2: 只跟关注3人回测); None = 环境内全部组合
    "expert_ids": None,
}

VARIANT_LABELS = {
    "A": "直接跟单(不用模型)",
    "B": "买入模型过滤",
    "C": "买入模型 + 卖出模型",
    "D": "买入模型 + 反向否决(全部负收益)",
}


def live_sample_warning() -> str | None:
    """实盘数据样本充足性检查 (决策 2026-09-25: 不足不拒绝运行, 结果页打大字警告)。"""
    rows = q("""SELECT COUNT(DISTINCT expert_id) c, COUNT(DISTINCT trade_date) d,
                       SUM(action IN ('买入','加仓','申购')) b, MIN(trade_date) mn, MAX(trade_date) mx
                FROM expert_trades WHERE market_type='live' AND stock_code<>''""")
    # 迁移差异: MySQL 聚合恒返回一行; core.db.q 出错(如 expert_trades 视图未建成)返回 [] ->
    # 按空数据处理 (与 MySQL 空表行为一致: 样本不足警告), 不抛 IndexError
    srow = rows[0] if rows else {"c": 0, "d": 0, "b": 0, "mn": None, "mx": None}
    issues = []
    if int(srow["c"] or 0) < 5:
        issues.append(f"覆盖组合 {srow['c']} < 5")
    if int(srow["d"] or 0) < 60:
        issues.append(f"覆盖交易日 {srow['d']} < 60")
    if int(srow["b"] or 0) < 200:
        issues.append(f"买入样本 {srow['b']} < 200")
    if not issues:
        return None
    return ("⚠️ 实盘数据样本不足: " + "、".join(issues) +
            f" (当前 {srow['c']} 组合 / {srow['d']} 交易日 / {srow['b']} 笔买入, "
            f"覆盖 {srow['mn']} ~ {srow['mx']})。统计效力有限, 结果仅供参考, 等每日自动累积后重测。")


def normalize(spec: Dict[str, Any] | None) -> Dict[str, Any]:
    """合并默认值并做边界检查; 返回新 dict。"""
    out = copy.deepcopy(DEFAULT_SPEC)
    for k, v in (spec or {}).items():
        if k in out:
            out[k] = v
    out["window"] = int(max(20, min(180, int(out["window"]))))
    if out["mode"] not in ("screen_rank", "tiers"):
        raise ValueError("mode 只能是 screen_rank / tiers")
    if out.get("data_env") not in ("sim", "live"):
        raise ValueError("data_env 只能是 sim / live")
    out["rank_windows"] = sorted({int(x) for x in out["rank_windows"] if 1 <= int(x) <= 120})
    out["top_k"] = int(max(0, min(50, int(out["top_k"]))))
    if out.get("expert_ids"):
        out["expert_ids"] = [str(x) for x in out["expert_ids"]]
    if out["mode"] == "screen_rank" and not out["rank_windows"]:
        raise ValueError("排名周期不能为空")
    out["pos_tiers"] = sorted({round(float(x), 4) for x in out["pos_tiers"] if 0 < float(x) <= 1})
    out["neg_tiers"] = sorted({round(float(x), 4) for x in out["neg_tiers"] if 0 < float(x) <= 1})
    for k in ("min_ret", "max_mdd", "min_win", "win_cost", "accept_q", "veto_q", "ex_q", "stop_loss", "stock_cap"):
        out[k] = float(out[k])
    for k in ("min_trades", "recent_days", "min_closed", "min_pool", "label_h", "min_train", "max_hold"):
        out[k] = int(out[k])
    if not out["pos_tiers"]:
        raise ValueError("正向批次比例不能为空")
    return out


def spec_hash(spec: Dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(normalize(spec), sort_keys=True).encode()).hexdigest()[:10]


def pct_label(x: float) -> str:
    return "全部" if x >= 1 else f"{x * 100:g}%"
