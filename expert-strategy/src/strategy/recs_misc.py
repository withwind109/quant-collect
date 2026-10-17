"""scripts/recs 附属小模块的收口迁移 (2026-10-16) —— 被迁移模块引用的最小符号。

来源与迁移范围 (只迁被引用符号, 业务逻辑原样):
  * gates      <- scripts/recs/gates.py        (min_tier / strategy_id_for_row / check)
  * defaults   <- scripts/recs/defaults.py     (_config_names / default_strategy_name)
  * notify     <- scripts/recs/notify.py       (enqueue; flush/webhook 发送未迁移)
  * live_gate  <- scripts/recs/live_gate.py    (strategy_ok / cap_pct / parse_ref / check_order /
                                                candidates / ref_price / concept_of)
  * adapters   <- scripts/recs/adapters/expert_topn.py / resonance.py 的登记常量
                 (STRATEGY_ID/NAME/SPEC/DESC; 选股逻辑 _buy/_sell/rank_scores 未迁移)
以 SimpleNamespace 收口, 调用方 `from strategy.recs_misc import live_gate` 等用法与源一致。

迁移降级 (平台自动化/实盘联动不迁移):
  * gates.min_tier: 源读 settings.autotrade_min_tier; 本项目无该配置 -> 读环境变量
    AUTOTRADE_MIN_TIER, 默认 "E2";
  * defaults._config_names: 源列 configs/*.json5 (json5_loader); 本项目无 WF 策略配置 -> 返回 []
    (wf_config 分支将按源逻辑报 "找不到策略配置");
  * live_gate.ref_price: 源实时价取 scripts.dashboard.paper_trading._fetch_live_prices (平台自动化);
    改用 strategy.prices.close_prices 取生效日(pred_date)收盘价;
  * notify.flush: 企业微信 webhook 发送未迁移, enqueue 只落 xcq_notify_outbox 表。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple

from core.logger import logger
from strategy import ledger, registry
from strategy import recs_store as store
from paths import PROJECT_ROOT

# ── gates (scripts/recs/gates.py) ────────────────────────────────────────────


def min_tier() -> str:
    t = str(os.environ.get("AUTOTRADE_MIN_TIER", "E2") or "E2").upper()
    return t if t in registry.TIERS else "E2"


def strategy_id_for_row(plan_id: Optional[str]) -> str:
    """推荐文件行 -> 台账策略 id (与 adapters.file_recs 一致)。"""
    pid = str(plan_id or "").strip()
    return f"rec:plan:{pid}" if pid else f"rec:baseline:{default_strategy_name()}"


def check(plan_id: Optional[str]) -> Tuple[bool, Dict[str, Any]]:
    sid = strategy_id_for_row(plan_id)
    tier = registry.tier_of(sid)
    need = min_tier()
    return registry.tier_rank(tier) >= registry.tier_rank(need), {"strategy_id": sid, "tier": tier, "need": need}


# ── defaults (scripts/recs/defaults.py) ──────────────────────────────────────

_CFG_PATH = PROJECT_ROOT / "configs" / "recommend_default.json"


def _config_names() -> list:
    """WF 策略配置名列表。迁移降级: 源列 configs/*.json5 (json5_loader), 本项目无 WF 配置 -> []。"""
    return []


def default_strategy_name() -> str:
    """返回显式指定的基线策略配置名。

    文件缺失/名称不存在 -> 明确告警并退回字典序第一个(旧行为), 不静默换策略。
    """
    names = _config_names()
    try:
        name = str(json.loads(_CFG_PATH.read_text(encoding="utf-8")).get("strategy") or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[recs] 读取 {_CFG_PATH.name} 失败({e}), 退回字典序第一个配置")
        name = ""
    if name and name in names:
        return name
    if name:
        logger.warning(f"[recs] {_CFG_PATH.name} 指定的策略 {name!r} 不存在于配置列表, 退回字典序第一个")
    return names[0] if names else "short_term_tactic_v3"


# ── notify (scripts/recs/notify.py, 仅 enqueue) ──────────────────────────────

MAX_BYTES = 3900          # 企业微信 markdown 单条上限约 4096 字节, 留余量


def _truncate(md: str) -> str:
    b = md.encode("utf-8")
    if len(b) <= MAX_BYTES:
        return md
    return b[:MAX_BYTES - 60].decode("utf-8", errors="ignore") + "\n…(内容过长)"


def enqueue(event_key: str, kind: str, title: str, content_md: str, payload: Optional[Dict[str, Any]] = None) -> bool:
    """入队。返回 True=新事件, False=已存在(幂等, 不重复推)。

    迁移降级: flush (企业微信 webhook 发送) 未迁移, 事件只落 xcq_notify_outbox 表。
    """
    store.ensure_schema()
    n = store.run(
        "INSERT OR IGNORE INTO xcq_notify_outbox (event_key,kind,title,content_md,payload_json,status,created_at) "
        "VALUES (:k,:kind,:t,:c,:p,'pending',:now)",
        {"k": event_key[:160], "kind": kind, "t": title[:200], "c": _truncate(content_md),
         "p": json.dumps(payload, ensure_ascii=False, default=str) if payload is not None else None, "now": datetime.now()})
    return n > 0


# ── live_gate (scripts/recs/live_gate.py) ────────────────────────────────────

DEFAULT_CAP = {"E2": 20.0, "E3": 50.0}


def cap_pct(st: Dict[str, Any]) -> float:
    if st.get("live_cap_pct") is not None:
        return float(st["live_cap_pct"])
    return DEFAULT_CAP.get(str(st.get("tier")), 0.0)


def strategy_ok(strategy_id: str) -> Tuple[bool, str]:
    """(是否允许用于实盘, 原因)。"""
    st = registry.get_strategy(strategy_id)
    if st is None:
        return False, f"策略 {strategy_id} 不存在"
    need = min_tier()
    if registry.tier_rank(st["tier"]) < registry.tier_rank(need):
        return False, f"策略等级 {st['tier']} 低于要求的 {need}"
    if st["status"] != "active":
        return False, "策略已暂停"
    if not int(st.get("live_approved") or 0):
        return False, "策略未获批准用于实盘(需要你在策略档案里显式批准)"
    return True, "ok"


def parse_ref(signal_ref: Optional[str]) -> Optional[Tuple[str, int]]:
    ref = str(signal_ref or "")
    if "#" not in ref:
        return None
    sid, _, rid = ref.rpartition("#")
    return (sid, int(rid)) if sid and rid.isdigit() else None


def check_order(signal_ref: Optional[str]) -> Tuple[bool, str]:
    """买单确认/提交前的复核。没有来源策略的买单一律拒绝(决策 D9: 不能读"所有买入行")。"""
    p = parse_ref(signal_ref)
    if p is None:
        return False, "买单没有来源策略(signal_ref)，不能确认: 实盘只接受来自已批准策略的推荐"
    return strategy_ok(p[0])


def candidates(pred_date: str) -> Dict[str, Any]:
    """返回 {approved: [{strategy, recs}], blocked: {strategy_id: (n_buys, reason)}}。"""
    approved, blocked = [], {}
    for st in registry.list_strategies("active"):
        rows = [r for r in ledger.get_recs(pred_date, [st["strategy_id"]], ["买入"])
                if int(r["valid_forward"]) == 1 and not int(r["backfilled"])]
        if not rows:
            continue
        ok, why = strategy_ok(st["strategy_id"])
        if ok:
            approved.append({"strategy": st, "recs": sorted(rows, key=lambda r: (r["rank_no"] or 9999, r["code"]))})
        else:
            blocked[st["strategy_id"]] = (len(rows), why)
    return {"approved": approved, "blocked": blocked}


def ref_price(rec: Dict[str, Any]) -> float:
    """参考价: 优先推荐自带的盘前价(文件类推荐), 否则取生效日收盘价; 都没有则返回 0(该单跳过, 不臆测价格)。

    迁移降级: 源实时价走 scripts.dashboard.paper_trading._fetch_live_prices (平台自动化, 未迁移),
    改用 strategy.prices.close_prices(code, pred_date, pred_date)。
    """
    meta = rec.get("meta_json")
    meta = json.loads(meta) if isinstance(meta, str) else (meta or {})
    try:
        if meta.get("ref_price"):
            return float(meta["ref_price"])
    except (TypeError, ValueError):
        pass
    try:
        from strategy import prices
        d = str(rec.get("pred_date") or "")[:10]
        return float(prices.close_prices(rec["code"], d, d).get(d) or 0) if d else 0.0
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[live_gate] 取参考价失败 {rec.get('code')}: {e}")
        return 0.0


def concept_of(rec: Dict[str, Any]) -> str:
    meta = rec.get("meta_json")
    meta = json.loads(meta) if isinstance(meta, str) else (meta or {})
    return str(meta.get("concept") or meta.get("top_concept") or (meta.get("concepts") or [""])[0] or "")


# ── adapters 登记常量 (scripts/recs/adapters/{expert_topn,resonance}.py) ──────

expert_topn = SimpleNamespace(
    STRATEGY_ID="expert_top10:v1",
    TOP_N=10,
    SPEC={"top_n": 10, "rank": "proxy(冷启动)->自建跟单track_record(实盘/模拟盘各自: 有资格高手>=30位且持续性检验通过后各自切换; 2026-09-29 实盘历史成交纳入)", "sides": ["买入", "卖出"],
          "min_experts": 1, "pool": "当日有交易记录的高手", "exit_mode": "event",
          "proxy_market_split": "v2 2026-09-29: 实盘/模拟盘分开组内归一; 实盘用重建净值累计收益(as_of前)",
          "priority": "live_first 2026-09-29: 实盘高手优先入选, 不足10位用模拟盘补满"},
    NAME="社区高手 Top10 买卖",
    DESC="当日有交易的高手中可信度前10位的买入/卖出(冷启动=综合代理分, 跟随记录累积后自动切换)",
)

resonance = SimpleNamespace(
    STRATEGY_ID="resonance:v1",
    SPEC={"buy": "共振买入(R>=0.65且M>=0.60)", "sell": "共振卖出(镜像: R_sell>=0.65且M_bear>=0.60)",
          "weights": {"E": 0.40, "M": 0.25, "G": 0.15, "S": 0.20}, "exit_mode": "event",
          "credibility": "proxy; 自建跟单track record 实盘/模拟盘各自达标(有资格高手>=30位且持续性检验通过)后各自切换 (2026-09-29 实盘历史成交纳入)",
          "proxy_market_split": "v2 2026-09-29: 实盘/模拟盘分开组内归一; 实盘用重建净值累计收益(as_of前)"},
    NAME="信号共振 买卖",
    DESC="高手行为 × 概念预测 × 图传播 × 个股模型 四维同向(买入/对称的卖出)",
)

# ── SimpleNamespace 收口 (调用方 from strategy.recs_misc import xxx) ──────────

gates = SimpleNamespace(min_tier=min_tier, strategy_id_for_row=strategy_id_for_row, check=check)
defaults = SimpleNamespace(_config_names=_config_names, default_strategy_name=default_strategy_name)
notify = SimpleNamespace(enqueue=enqueue)
live_gate = SimpleNamespace(cap_pct=cap_pct, strategy_ok=strategy_ok, parse_ref=parse_ref,
                            check_order=check_order, candidates=candidates, ref_price=ref_price,
                            concept_of=concept_of)
