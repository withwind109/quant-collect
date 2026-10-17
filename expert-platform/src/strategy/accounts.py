"""模拟盘账户 <-> 策略 (P2: 一账户一策略)。

账户只读推荐台账, 不再自带选股逻辑; 本金/成交规则/成本全局统一(见 paper_trading.py), 账户之间只有策略不同。

迁移自 scripts/recs/accounts.py (2026-10-16)。变更:
  * 存储 MySQL -> 本地 SQLite (strategy.recs_store: q/qexec 语义, 表 xcq_paper_accounts /
    xcq_strategies 已由 core.db 建基表 + recs_store 补列); 导入改绝对路径;
  * 平台自动化未迁移: scripts.dashboard.paper_automation (ensure_schema) 调用删除;
    scripts.expert_imitation / scripts.expert_v3.live / scripts.experts.follow_v2 /
    scripts.experts.follow_v21 依赖的实盘联动策略种类 (expert_imitation / expert_follow_v3 /
    expert_follow_v21 / expert_follow_v2) 在 create_strategy 里降级为显式 ValueError;
  * 实时价格 (_fetch_live_prices/_get_close_price) 本文件未用到, 无需替换
    (需要收盘价处用 strategy.prices.close_prices, 见 recs_misc.live_gate.ref_price)。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.logger import logger
from strategy import registry, recs_store as store

# 现有 5 个账户迁移到的策略 (D8: 保留历史, 标注切换日)
MIGRATION: Dict[str, Dict[str, Any]] = {
    "paper_baseline": {
        "strategy_id": "paper:baseline_pipeline:v1", "name": "baseline管线(全量概念+gbdt_tech)", "kind": "pipeline",
        "spec": {"engine": "paper_signal", "source": "baseline_pipeline", "top_n": 10, "exit_mode": "rank",
                 "arena_ref": "baseline"},
        "backtestable": True, "note": "选股函数不变(原 _signal_baseline_pipeline), 只是改为经推荐台账留档"},
    "paper_resonance": {
        "strategy_id": "resonance:v1", "name": None, "kind": "resonance", "spec": None, "backtestable": False,
        "note": "改为共振买卖: 买入=共振买入, 卖出=共振卖出/止损/止盈/20日时间平仓; 不再因'信号消失'卖出(事件类策略)"},
    "paper_resonance_top10": {
        "strategy_id": "resonance_top10:v1", "name": "共振榜前10", "kind": "resonance",
        "spec": {"engine": "resonance_top10", "top_n": 10, "exit_mode": "rank", "market": "live_first",
                 "proxy_market_split": "v2 2026-09-29: 实盘/模拟盘分开组内归一; 实盘用重建净值累计收益(as_of前)"}, "backtestable": False,
        "note": "选股函数不变(原 _signal_resonance_top10), 改为经推荐台账留档"},
    "wf_long_term_tactic_v6": {
        "strategy_id": "wf:long_term_tactic_v6", "name": "WF·5日持仓策略(long_term_tactic_v6)", "kind": "wf_config",
        "spec": {"engine": "wf_config", "config_name": "long_term_tactic_v6", "top_n": 10, "exit_mode": "rank",
                 "arena_ref": "long_term_tactic_v6"},
        "backtestable": True, "note": "选股函数不变(原 _signal_wf_config_replay+持仓保留), 改为经推荐台账留档"},
    "wf_short_term_tactic_v5": {
        "strategy_id": "wf:short_term_tactic_v5", "name": "WF·2日短线策略(short_term_tactic_v5)", "kind": "wf_config",
        "spec": {"engine": "wf_config", "config_name": "short_term_tactic_v5", "top_n": 10, "exit_mode": "rank",
                 "arena_ref": "short_term_tactic_v5"},
        "backtestable": True, "note": "选股函数不变(原 _signal_wf_config_replay+持仓保留), 改为经推荐台账留档"},
}


def slug(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z_]+", "_", s).strip("_")[:40]


def get_account(account_id: str) -> Optional[Dict[str, Any]]:
    r = store.fetch("SELECT * FROM xcq_paper_accounts WHERE id=:i", {"i": account_id})
    return r[0] if r else None


def bind(account_id: str, strategy_id: str, note: str = "") -> None:
    store.ensure_schema()
    if registry.get_strategy(strategy_id) is None:
        raise ValueError(f"策略不存在: {strategy_id}")
    store.run("UPDATE xcq_paper_accounts SET strategy_id=:s, bound_at=:t, bound_note=:n WHERE id=:i",
              {"s": strategy_id, "t": datetime.now(), "n": note[:300], "i": account_id})


def migrate_existing(dry_run: bool = False) -> List[Dict[str, Any]]:
    """把现有账户绑定到策略 (历史净值/成交保留, 记录切换时间)。幂等: 已绑定的跳过。"""
    from strategy.recs_misc import resonance
    store.ensure_schema()
    out = []
    for aid, m in MIGRATION.items():
        acct = get_account(aid)
        if not acct:
            out.append({"account_id": aid, "status": "no_account"})
            continue
        if acct.get("strategy_id"):
            out.append({"account_id": aid, "status": "already_bound", "strategy_id": acct["strategy_id"]})
            continue
        sid = m["strategy_id"]
        if sid == "resonance:v1":
            name, spec, desc = resonance.NAME, resonance.SPEC, resonance.DESC
        else:
            name, spec, desc = m["name"], m["spec"], m["name"]
        if not dry_run:
            kind = m["kind"]
            registry.upsert_strategy(sid, name, kind, spec, backtestable=m["backtestable"], eval_horizon=5,
                                     description=desc,
                                     lifecycle_ref=("wf_config:" + spec["config_name"]) if spec.get("engine") == "wf_config" else None)
            bind(aid, sid, m["note"])
        out.append({"account_id": aid, "status": "bound" if not dry_run else "would_bind", "strategy_id": sid})
    return out


def create_account(strategy_id: str, name: Optional[str] = None, initial_capital: float = 1_000_000.0,
                   max_positions: int = 10, stop_loss_pct: Optional[float] = None, account_id: Optional[str] = None,
                   exit_policy_version: int = 1) -> Dict[str, Any]:
    """为某策略新开一个模拟子账户 (本金/持仓上限为全局默认值; 止损默认取策略自带止损)。一个策略只能对应一个账户。"""
    store.ensure_schema()
    st = registry.get_strategy(strategy_id)
    if st is None:
        raise ValueError(f"策略不存在: {strategy_id}")
    # [2026-09-28] 同一策略可以再有一个"统一退出口径 v2"的并行账户 (用户决策 3B), 每种口径各一个
    # (平台自动化 paper_automation.ensure_schema 未迁移, 列由 recs_store.ensure_schema 补)
    dup = store.fetch("SELECT id FROM xcq_paper_accounts WHERE strategy_id=:s AND exit_policy_version=:v",
                      {"s": strategy_id, "v": int(exit_policy_version)})
    if dup:
        raise ValueError(f"策略 {strategy_id} 已有模拟账户 {dup[0]['id']}")
    if stop_loss_pct is None:       # 默认取策略自带止损 (见 stops.py)
        from strategy import stops
        stop_loss_pct = round(float(stops.resolve(st)["pct"]), 4)
    aid = account_id or ("ps_" + slug(strategy_id))
    if get_account(aid):
        raise ValueError(f"账户 {aid} 已存在")
    store.run(
        "INSERT INTO xcq_paper_accounts (id,name,signal_source,initial_capital,cash,max_positions,stop_loss_pct,enabled,"
        "strategy_id,bound_at,bound_note) VALUES (:i,:n,'strategy',:c,:c,:m,:sl,1,:s,:t,'新建账户, 直接绑定策略')",
        {"i": aid, "n": (name or st["name"])[:100], "c": initial_capital, "m": max_positions, "sl": stop_loss_pct,
         "s": strategy_id, "t": datetime.now()})
    if int(exit_policy_version) != 1:
        store.run("UPDATE xcq_paper_accounts SET exit_policy_version=:v WHERE id=:i", {"v": int(exit_policy_version), "i": aid})
    logger.info(f"[recs] 新建模拟账户 {aid} -> 策略 {strategy_id}")
    return {"account_id": aid, "strategy_id": strategy_id}


def create_strategy(kind: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """按种类登记一个新策略 (只登记, 不开账户)。
    kind: pipeline / wf_config / combo / expert_top10 / resonance / expert_follow / expert_follow_v2。"""
    from strategy.recs_misc import expert_topn, resonance
    store.ensure_schema()
    if kind == "expert_follow":
        # 跟随 xcq_expert_watchlist 关注列表高手(方案见
        # docs/社区高手实盘模拟盘分离与指定高手跟踪_方案_20260923.md)，与"expert_top10"
        # (全站高手综合排名Top10)是两个不同策略：这个只跟人工关注的具体高手。
        # exit_mode=event: 高手换仓频率低, rank(名单外即卖)会导致"无信号=强制清仓",
        # 跟 follow_backtest.py 自己"信号驱动、无信号则持有"的语义不一致。
        spec = {"engine": "expert_follow", "top_n": 10, "exit_mode": "event",
                "arena_ref": "expert_follow"}
        return registry.upsert_strategy("expert_follow:v1", "跟随社区高手(关注列表)", "expert_follow", spec,
                                        backtestable=True,
                                        description="镜像 xcq_expert_watchlist 关注高手的持仓权重(次日开盘执行)",
                                        lifecycle_ref="expert_follow")
    if kind == "expert_imitation":
        # [2026-09-24] 高手模仿模型(scripts/expert_imitation): M2 盘中打分跟单(可叠加 EX 提前退出) / M1 盘前选股
        # 迁移降级: 依赖 LightGBM 训练/推理与实盘联动, 未迁移 -> 不接受登记
        raise ValueError("expert_imitation 未迁移: 依赖 scripts/expert_imitation (LightGBM 高手模仿模型)")
    if kind == "expert_imitation_live":
        # [2026-09-27] 实盘模仿模型 (scripts/expert_imitation/live_account.py), 模型钉死在 run_id
        # 迁移降级: 实盘联动未迁移 -> 不接受登记
        raise ValueError("expert_imitation_live 未迁移: 依赖 scripts/expert_imitation/live_account (实盘模仿模型)")
    if kind == "expert_follow_v3":
        # [2026-09-24] v3: 四规则高手池(PIT) 正向批次 × 方式(A直接/B模型/C+卖出模型/D+反向否决) 或 R 反向否决;
        # 一次回测运行(run_id) + 一个方案 = 一个策略, 模型/阈值钉死在该运行, 重训不会悄悄改变已上线策略
        # 迁移降级: 依赖 scripts/expert_v3/live.py 的回测运行接入(load_run/is_mirror), 未迁移 -> 不接受登记
        raise ValueError("expert_follow_v3 未迁移: 依赖 scripts/expert_v3/live.py 的回测运行接入")
    if kind == "expert_follow_v21":
        # [2026-09-25] v2.1 短周期风控 (scripts/experts/follow_v21.py); 回测闸门未过, 用户指示开模拟账户做前向观察
        # 迁移降级: 依赖 scripts/experts/follow_v21, 未迁移 -> 不接受登记
        raise ValueError("expert_follow_v21 未迁移: 依赖 scripts/experts/follow_v21")
    if kind == "expert_follow_v2":
        # [2026-09-24] v2: 关注列表 ∪ 每日动态收益前N, 资金按收益名次加权, 目标仓位制(见 scripts/experts/follow_v2.py)
        # 迁移降级: 依赖 scripts/experts/follow_v2, 未迁移 -> 不接受登记
        raise ValueError("expert_follow_v2 未迁移: 依赖 scripts/experts/follow_v2")
    if kind == "expert_top10":
        return registry.upsert_strategy(expert_topn.STRATEGY_ID, expert_topn.NAME, "expert_topn", expert_topn.SPEC,
                                        description=expert_topn.DESC, eval_horizon=5)
    if kind == "resonance":
        return registry.upsert_strategy(resonance.STRATEGY_ID, resonance.NAME, "resonance", resonance.SPEC,
                                        description=resonance.DESC, eval_horizon=5)
    if kind == "pipeline":
        name = str(params.get("pipeline_name") or "").strip()
        if not name or not store.fetch("SELECT 1 FROM xcq_pipeline_configs WHERE pipeline_name=:n", {"n": name}):
            raise ValueError(f"找不到管线配置: {name}")
        spec = {"engine": "pipeline", "pipeline_name": name, "top_n": 10, "exit_mode": "rank", "arena_ref": name}
        return registry.upsert_strategy(f"pipeline:{name}", f"管线·{name}", "pipeline", spec, backtestable=True,
                                        description="单管线复刻(选股器/概念口径见管线配置; 简化说明见 paper_signals)",
                                        lifecycle_ref=f"pipeline:{name}")
    if kind == "wf_config":
        cfg = str(params.get("config_name") or "").strip()
        if not cfg:
            raise ValueError("config_name 必填")
        from strategy.recs_misc import defaults
        if cfg not in defaults._config_names():
            raise ValueError(f"找不到策略配置: {cfg}")
        spec = {"engine": "wf_config", "config_name": cfg, "top_n": 10, "exit_mode": "rank", "arena_ref": cfg}
        return registry.upsert_strategy(f"wf:{cfg}", f"WF·{cfg}", "wf_config", spec, backtestable=True,
                                        description="WF组合策略复刻: 多选股器静态权重+持仓保留",
                                        lifecycle_ref=f"wf_config:{cfg}")
    if kind == "combo":
        members = [str(m) for m in (params.get("members") or [])]
        rule = params.get("rule", "union")
        k = int(params.get("k") or 2)
        if len(members) < 2:
            raise ValueError("组合至少需要 2 个成员策略")
        if rule not in ("union", "vote"):
            raise ValueError("rule 只支持 union(并集) / vote(票数>=k)")
        for m in members:
            ms = registry.get_strategy(m)
            if ms is None:
                raise ValueError(f"成员策略不存在: {m}")
            if json.loads(ms["spec_json"] or "{}").get("engine") == "combo":
                raise ValueError("暂不支持组合套组合")
        if rule == "vote" and not (1 < k <= len(members)):
            raise ValueError(f"票数阈值 k 需在 2..{len(members)}")
        import hashlib
        tag = f"{'U' if rule == 'union' else f'V{k}'}_" + hashlib.sha1("|".join(sorted(members)).encode()).hexdigest()[:8]
        spec = {"engine": "combo", "members": members, "rule": rule, "k": k, "exit_mode": "rank", "top_n": 10}
        nm = "组合·" + ("并集" if rule == "union" else f"≥{k}票") + "(" + ",".join(
            (registry.get_strategy(m) or {}).get("name", m) for m in members) + ")"
        return registry.upsert_strategy(f"combo:{tag}"[:80], nm[:120], "pipeline_combo", spec, backtestable=False,
                                        description="管线组合: 成员当日买入推荐的并集/票数>=k (只做前向, 不回测)")
    raise ValueError(f"未知策略种类 {kind}")


def load_targets(strategy_id: str, pred_date: str) -> Dict[str, Any]:
    """账户读取其策略在 pred_date 的推荐。ok=False 表示台账里没有该策略当日的成功运行 (账户不应据此交易)。"""
    st = registry.get_strategy(strategy_id)
    if st is None:
        return {"ok": False, "note": f"策略不存在 {strategy_id}"}
    spec = json.loads(st["spec_json"] or "{}")
    run = store.fetch("SELECT status,n_recs FROM xcq_rec_runs WHERE pred_date=:p AND strategy_id=:s",
                      {"p": pred_date, "s": strategy_id})
    if not run or run[0]["status"] != "ok":
        return {"ok": False, "note": f"{pred_date} 台账无 {strategy_id} 的成功运行({run[0]['status'] if run else '未运行'})"}
    rows = store.fetch("SELECT code,name,action,score,rank_no,reason_text,target_weight FROM xcq_recommendations "
                       "WHERE pred_date=:p AND strategy_id=:s ORDER BY rank_no,code", {"p": pred_date, "s": strategy_id})
    buys = [(r["code"], r["name"] or "", f"{strategy_id}#{r['rank_no'] or ''} {r['reason_text'] or ''}"[:95])
            for r in rows if r["action"] == "买入"]
    holds = {r["code"] for r in rows if r["action"] == "持有"}
    sells = {r["code"] for r in rows if r["action"] in ("卖出", "减仓")}
    # exit_mode=target(跟随高手v2): 目标仓位制, 账户按 weights 调仓(加仓/减仓/清仓), 不是"名单内等权"
    weights = {r["code"]: float(r["target_weight"]) for r in rows
               if r["action"] == "买入" and r.get("target_weight") is not None}
    return {"ok": True, "buys": buys, "holds": holds, "sells": sells, "weights": weights,
            "exit_mode": spec.get("exit_mode", "rank"), "spec": spec, "n_recs": len(rows)}
