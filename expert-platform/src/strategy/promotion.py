"""模拟盘 → 正式交易 的晋级流程 (2026-09-24, 跟随高手 v3 首个使用者, 通用于任何绑定了模拟账户的策略)。

硬规则不变: **任何订单都不会自动下到券商**, 这里只生成待确认单, 必须由你在「自动交易」页逐笔确认
(确认时 live_gate 还会再复核一次策略状态)。

状态机 (promotions 表, 一个策略一行):
  requested  申请实盘 (要求: 该策略回测闸门已过 + 有模拟账户); 等前向证据
  approved   你批准 (registry.set_live_approval 要求 等级 ≥ E2 且策略在用) → 开始镜像下单建议
  halted     熔断: 实盘资金从峰值回撤 ≥ max_dd_pct → 撤销批准、取消该策略所有待确认单; 恢复须重新批准
  revoked    你撤销
镜像: 模拟账户每一笔成交 → 按 实盘资金/模拟本金 缩放股数(100 股整数倍) → 待确认单 (signal_ref = <策略>#<模拟成交id>)
实盘价值: 资金 + 已成交(fill)买卖现金流 + 持仓 × 最新价; 峰值随之更新 (没有成交时 = 资金)。

迁移自 scripts/expert_v3/promotion.py (2026-10-16)。变更:
  * 表 xcq_live_promotions (MySQL) -> promotions (core.db 已建: strategy_id PK / state / capital /
    max_dd_pct / note / requested_at / decided_at / history_json)。源表多出的 account_id /
    peak_value / value_now / approved_at / halted_at / halt_reason / updated_at 存入 history_json,
    get()/list_all()/tick() 读回时合并进行, 下游用法不变;
  * ON DUPLICATE KEY UPDATE -> ON CONFLICT(strategy_id) DO UPDATE (只更指定列, 保留 decided_at);
    GREATEST/COALESCE -> max()/or 判断; CONCAT 相关(_cancel_pending) 随自动交易一起降级;
  * list_all 排序: 源按 updated_at, 目标 promotions 表无该列 -> 按 requested_at DESC;
  * 平台自动化 (自动交易/实盘联动) 未迁移, 最小降级:
      - mirror_orders 恒返回 0 (scripts.core.autotrade 下单/待确认单未迁移);
      - _cancel_pending 恒返回 0 (xcq_autotrade_orders 未迁移, 无实盘单可取消);
      - live_value 返回 capital (实盘成交流水未迁移, 与源 "无已成交单" 时的结果一致);
      - tick 的熔断状态机结构保留 (回撤恒为 0, 不会触发熔断)。
"""
from __future__ import annotations

import json
from datetime import date as _date, datetime
from typing import Any, Dict, List, Optional

from core.logger import logger
from strategy import recs_store as S
from strategy import registry
from strategy.v3_store import ensure_schema

DEFAULT_CAPITAL = 1_000_000.0
DEFAULT_MAX_DD = 8.0


def _ext(row: Dict[str, Any]) -> Dict[str, Any]:
    """history_json 里的扩展字段 (源 xcq_live_promotions 有独立列、目标 promotions 没有的列)。"""
    try:
        return json.loads(row.get("history_json") or "{}")
    except Exception:  # noqa: BLE001
        return {}


def _merged(row: Dict[str, Any]) -> Dict[str, Any]:
    """行 + history_json 扩展字段 (扩展键与列名不重名)。"""
    row.update({k: v for k, v in _ext(row).items() if k not in row})
    return row


def get(strategy_id: str) -> Optional[Dict[str, Any]]:
    ensure_schema()
    r = S.fetch("SELECT * FROM promotions WHERE strategy_id=:s", {"s": strategy_id})
    return _merged(r[0]) if r else None


def list_all() -> List[Dict[str, Any]]:
    ensure_schema()
    rows = S.fetch("SELECT p.*, s.tier, s.status AS strategy_status, s.live_approved, s.name FROM promotions p "
                   "LEFT JOIN xcq_strategies s ON s.strategy_id=p.strategy_id ORDER BY p.requested_at DESC")
    for r in rows:
        _merged(r)
        r["live_ready"] = _ready(r)
    return rows


def _ready(p: Dict[str, Any]) -> Dict[str, Any]:
    from strategy.recs_misc import live_gate
    ok, why = live_gate.strategy_ok(p["strategy_id"])
    return {"ok": ok and p["state"] == "approved", "reason": why if not ok else ("" if p["state"] == "approved" else p["state"])}


def request(strategy_id: str, capital: float = DEFAULT_CAPITAL, max_dd_pct: float = DEFAULT_MAX_DD,
            note: str = "") -> Dict[str, Any]:
    ensure_schema()
    st = registry.get_strategy(strategy_id)
    if st is None:
        raise ValueError(f"策略不存在: {strategy_id}")
    acct = S.fetch("SELECT id FROM xcq_paper_accounts WHERE strategy_id=:s ORDER BY exit_policy_version, id", {"s": strategy_id})   # 并行的统一退出口径账户不参与晋级镜像
    if not acct:
        raise ValueError("该策略还没有模拟账户: 先发布到模拟盘累积前向证据")
    if not (0 < float(capital) <= 1e8) or not (0 < float(max_dd_pct) <= 50):
        raise ValueError("资金或熔断线不合法")
    now = datetime.now()
    ext = {"account_id": acct[0]["id"], "peak_value": float(capital), "value_now": float(capital),
           "approved_at": None, "halted_at": None, "halt_reason": None, "updated_at": now.isoformat()}
    S.run("INSERT INTO promotions (strategy_id,state,capital,max_dd_pct,note,requested_at,history_json) "
          "VALUES (:s,'requested',:c,:d,:n,:t,:h) "
          "ON CONFLICT(strategy_id) DO UPDATE SET state='requested',capital=excluded.capital,"
          "max_dd_pct=excluded.max_dd_pct,requested_at=excluded.requested_at,note=excluded.note,"
          "history_json=excluded.history_json",
          {"s": strategy_id, "c": float(capital), "d": float(max_dd_pct), "n": note[:300],
           "t": now, "h": json.dumps(ext, ensure_ascii=False, default=str)})
    registry.log_event(strategy_id, f"申请正式交易: 资金 {capital:,.0f}, 回撤熔断 {max_dd_pct}%" + (f" ({note})" if note else ""))
    return get(strategy_id)  # type: ignore[return-value]


def approve(strategy_id: str, note: str = "") -> Dict[str, Any]:
    p = get(strategy_id)
    if not p or p["state"] not in ("requested", "halted", "revoked"):
        raise ValueError("只有已申请/已熔断/已撤销的策略可以(重新)批准")
    registry.set_live_approval(strategy_id, True, note=note or "跟随高手v3 实盘晋级")   # 等级 < E2 会在这里被拒绝
    now = datetime.now()
    v_now = float(p["value_now"]) if p.get("value_now") is not None else float(p["capital"])
    peak = max(v_now, 0.0)
    ext = _ext(p)
    ext.update({"approved_at": now.isoformat(), "halted_at": None, "halt_reason": None,
                "peak_value": peak, "updated_at": now.isoformat()})
    S.run("UPDATE promotions SET state='approved', decided_at=:t, history_json=:h WHERE strategy_id=:s",
          {"t": now, "s": strategy_id, "h": json.dumps(ext, ensure_ascii=False, default=str)})
    return get(strategy_id)  # type: ignore[return-value]


def revoke(strategy_id: str, note: str = "") -> Dict[str, Any]:
    if not get(strategy_id):
        raise ValueError("没有该策略的实盘申请")
    try:
        registry.set_live_approval(strategy_id, False, note=note)
    except ValueError:
        pass
    S.run("UPDATE promotions SET state='revoked' WHERE strategy_id=:s", {"s": strategy_id})
    _cancel_pending(strategy_id)
    return get(strategy_id)  # type: ignore[return-value]


def _cancel_pending(strategy_id: str) -> int:
    """取消该策略所有待确认实盘单。迁移降级: 自动交易 (xcq_autotrade_orders/autotrade) 未迁移,
    本项目无实盘单 -> 恒返回 0。"""
    return 0


def live_value(p: Dict[str, Any]) -> float:
    """资金 + 已成交现金流 + 持仓市值(最新收盘/实时价)。

    迁移降级: 实盘订单 (xcq_autotrade_orders) 与实时价 (_fetch_live_prices) 未迁移, 本项目无实盘
    成交 -> 返回 capital (与源实现在 "无已成交单" 时的结果一致)。
    """
    return float(p["capital"])


def mirror_orders(p: Dict[str, Any], trade_date: str) -> int:
    """模拟账户今天的新成交 → 实盘待确认单。已镜像过的(signal_ref 相同)不重复。

    迁移降级: 自动交易 (scripts.core.autotrade 下单 / 待确认单 / xcq_paper_trades) 未迁移
    -> 恒返回 0, 不生成任何实盘单。
    """
    return 0


def tick(trade_date: Optional[str] = None) -> Dict[str, Any]:
    """盘中每轮 / 收盘调用: 更新实盘价值与峰值 → 熔断检查 → 已批准且未熔断的策略镜像下单建议。

    迁移降级: live_value 恒为 capital / mirror_orders 恒 0 (实盘联动未迁移), 熔断状态机结构保留。
    """
    ensure_schema()
    trade_date = trade_date or _date.today().isoformat()
    out: Dict[str, Any] = {}
    for p in S.fetch("SELECT * FROM promotions WHERE state='approved'"):
        sid = p["strategy_id"]
        try:
            _merged(p)
            v = live_value(p)
            peak = max(float(p.get("peak_value") or p["capital"]), v)
            now = datetime.now()
            ext = _ext(p)
            ext.update({"value_now": v, "peak_value": peak, "updated_at": now.isoformat()})
            S.run("UPDATE promotions SET history_json=:h WHERE strategy_id=:s",
                  {"h": json.dumps(ext, ensure_ascii=False, default=str), "s": sid})
            dd = (1 - v / peak) * 100 if peak > 0 else 0.0
            if dd >= float(p["max_dd_pct"]):
                why = f"实盘回撤 {dd:.2f}% ≥ 熔断线 {float(p['max_dd_pct']):.1f}% (峰值 {peak:,.0f} → {v:,.0f})"
                ext.update({"halted_at": now.isoformat(), "halt_reason": why[:300]})
                S.run("UPDATE promotions SET state='halted', decided_at=:t, history_json=:h WHERE strategy_id=:s",
                      {"t": now, "s": sid, "h": json.dumps(ext, ensure_ascii=False, default=str)})
                try:
                    registry.set_live_approval(sid, False, note="熔断: " + why, actor="auto")
                except ValueError:
                    pass
                n = _cancel_pending(sid)
                logger.warning(f"[promotion] {sid} 熔断: {why}; 取消待确认单 {n} 笔")
                out[sid] = {"halted": why}
                continue
            ready = _ready({**p, "state": "approved"})
            out[sid] = {"value": round(v, 2), "dd_pct": round(dd, 2),
                        "mirrored": mirror_orders(p, trade_date) if ready["ok"] else 0,
                        "blocked": None if ready["ok"] else ready["reason"]}
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[promotion] {sid} 处理失败: {e}")
            out[sid] = {"error": str(e)[:200]}
    return out
