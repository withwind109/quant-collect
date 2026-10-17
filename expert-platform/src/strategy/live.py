"""跟随高手 · 策略文件 → 模拟盘 / 正式交易 (2026-09-25)。

发布: 回测运行里的一个方案 (策略文件, 或 策略文件+参数扫描) → 策略台账 expert_follow_cfg:<id> + 模拟账户。
  - 通过闸门的方案: 直接发布;
  - 未通过的方案: 只能以"前向观察"发布 (须显式 observe=true; 账户名带 🧪, 等级从 E0 起, 达到 E2 前不能申请正式交易) ——
    与 2026-09-25 v2.1 候选/对照账户的做法一致, 不放宽回测闸门本身;
  - 参数扫描出来的方案先落成 strategies/user/<id>.json5, 账户绑定这份文件。
执行: paper_signals 的 expert_follow_cfg 分支 → generate_recs (与回测同一个 Selector / 目标仓位函数)。
  策略文件被改动 (指纹与发布时不同) → 当天不出信号并告警: 改文件 = 新版本, 需要重新发布、证据重新累积。
已在跑的旧账户 (v1 / v2 / v2.1 候选与对照) 保留原策略 ID 与原执行代码, 由策略文件的 bind_strategy_id 绑定展示 (用户决策)。

迁移自 scripts/expert_follow/live.py (2026-10-16): 导入改绝对路径 (strategy.* / core.*);
RUN_ROOT 由 PROJECT_ROOT/models/expert_follow 改为 paths.MODELS_DIR/expert_follow;
源文件未引用 scripts.recs.live_gate / scripts.dashboard.* 等平台自动化模块, 无需降级处理;
其余逐行原样。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from core.logger import logger
from paths import MODELS_DIR
from strategy import config as C

RUN_ROOT = MODELS_DIR / "expert_follow"
KIND = "expert_follow_cfg"


def strategy_id(cfg_id: str) -> str:
    return f"{KIND}:{cfg_id}"


def load_summary(run_id: str) -> Optional[Dict[str, Any]]:
    if not re.match(r"^[0-9_]{8,32}$", str(run_id or "")):
        return None
    fp = RUN_ROOT / run_id / "summary.json"
    return json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else None


def gate_for(name: str) -> Dict[str, Any]:
    """arena.gate_for('expert_follow_cfg', '<run_id>:<方案 key>')。"""
    run_id, _, key = str(name).partition(":")
    s = load_summary(run_id)
    if not s or key not in (s.get("variants") or {}):
        return {"ok": False, "reasons": [f"找不到回测运行 {run_id} 的方案 {key}"], "warn": None, "basis": "跟随高手统一回测"}
    g = s["variants"][key].get("gate") or {}
    bad = [c for c, ok in (g.get("checks") or {}).items() if not ok]
    return {"ok": bool(g.get("passed")), "reasons": [f"未过: {c}" for c in bad], "warn": None,
            "basis": f"跟随高手统一回测 {run_id}", "run_id": run_id}


def _materialize(summary: Dict[str, Any], key: str) -> Dict[str, Any]:
    """方案 key → 策略文件 (内置文件直接用; 参数扫描方案另存到 strategies/user/)。"""
    v = summary["variants"][key]
    same = C.load_strategy(v["ref"])
    same["universe"]["data_env"] = summary["data_env"]
    if "[" in key and C.strategy_hash(same) == v["strategy_hash"]:      # 扫描方案与策略文件本身相同
        return same
    if "[" not in key:
        s = C.load_strategy(v["ref"])
        s["universe"]["data_env"] = summary["data_env"]
        if C.strategy_hash(s) != v["strategy_hash"]:
            raise ValueError(f"策略文件 {v['ref']} 在回测之后被改过 (指纹不同), 请重新回测后再发布")
        return s
    ent = None
    b = C.load_backtest(summary["backtest_id"])
    for e in b.get("strategies") or []:
        if e["ref"] == v["ref"]:
            ent = e
    for k, s in C.expand(ent or {"ref": v["ref"]}, summary["data_env"]):
        if k == key:
            if C.strategy_hash(s) != v["strategy_hash"]:
                raise ValueError("策略文件或回测配置在回测之后被改过 (指纹不同), 请重新回测后再发布")
            suffix = re.sub(r"[^A-Za-z0-9]+", "_", key[len(v["ref"]):]).strip("_")[:40]
            s["id"] = f"{v['ref']}__{suffix}"[:60]
            s["name"] = f"{s['name']} [{key[len(v['ref']):].strip('[]')}]"[:100]
            s["bind_strategy_id"] = None
            if C.strategy_path(s["id"]) is None:
                C.save_user_strategy(s)
            return C.load_strategy(s["id"])
    raise ValueError(f"回测配置里找不到方案 {key}")


def register(s: Dict[str, Any], observe: bool, basis: str) -> Dict[str, Any]:
    from strategy import registry
    eng = s["execution"]["engine"]
    spec = {"engine": KIND, "config_id": s["id"], "config_hash": C.strategy_hash(s), "data_env": s["universe"]["data_env"],
            "exec": "t1_open", "exit_mode": "event" if eng == "signal" else "target",
            "stock_cap": s["execution"]["stock_cap"], "arena_ref": f"{KIND}:{s['id']}"}
    return registry.upsert_strategy(strategy_id(s["id"]), ("🧪 " if observe else "") + s["name"][:90], "expert_follow", spec,
                                    backtestable=True, lifecycle_ref="expert_follow",
                                    description=f"{C.describe(s)}; 策略文件 {s['id']}.json5@{spec['config_hash']}; {basis}")


def publish(run_id: str, key: str, observe: bool = False, capital: float = 1_000_000.0,
            name: Optional[str] = None) -> Dict[str, Any]:
    from core.db import q
    from strategy import accounts as A
    s0 = load_summary(run_id)
    if not s0 or key not in (s0.get("variants") or {}):
        raise ValueError(f"找不到回测运行 {run_id} 的方案 {key}")
    v = s0["variants"][key]
    if v.get("engine") == "v3_tiers":
        raise ValueError("v3 分批+模型方案依赖该次训练的模型, 请在 v3 模块内发布 (统一流程暂不支持)")
    passed = bool((v.get("gate") or {}).get("passed"))
    if not passed and not observe:
        raise ValueError("该方案未通过回测闸门; 如需累积前向证据, 请以'前向观察'方式发布 (observe=true)")
    s = _materialize(s0, key)
    for sid in [x for x in (s.get("bind_strategy_id"), strategy_id(s["id"])) if x]:
        have = q(f"SELECT id FROM xcq_paper_accounts WHERE strategy_id='{sid}'")
        if have:
            raise ValueError(f"该策略已有模拟账户 {have[0]['id']} 在运行 (前向证据在那里累积), 不重复发布")
    basis = f"回测 {run_id} 方案 {key} " + ("通过闸门" if passed else "未过闸门, 前向观察")
    st = register(s, observe=not passed, basis=basis)
    acct_id = ("ef_" + re.sub(r"[^A-Za-z0-9]+", "_", s["id"]).lower())[:60]
    if q(f"SELECT id FROM xcq_paper_accounts WHERE id='{acct_id}'"):
        raise ValueError(f"已发布过 (账户 {acct_id})")
    label = name or (("🧪 " if not passed else "🧭 ") + s["name"] + (" (前向观察)" if not passed else ""))
    A.create_account(st["strategy_id"], name=label[:100], account_id=acct_id, initial_capital=float(capital),
                     max_positions=int(s["execution"].get("signal", {}).get("max_positions", 30)))
    logger.info(f"[expert-follow] 发布 {key} → {acct_id} ({basis})")
    return {"account_id": acct_id, "strategy_id": st["strategy_id"], "config_id": s["id"], "passed": passed}


def publish_config(cfg_id: str, observe: bool, basis: str, capital: float = 1_000_000.0,
                   account_id: Optional[str] = None, name: Optional[str] = None) -> Dict[str, Any]:
    """直接按策略文件开前向观察账户 (用户明确指示时用, 如 2026-09-25 关注 3 位实盘高手)。不能绕过闸门开"通过"账户。"""
    from core.db import q
    from strategy import accounts as A
    if not observe:
        raise ValueError("不经回测直接发布只能是前向观察")
    s = C.load_strategy(cfg_id)
    st = register(s, observe=True, basis=basis)
    acct_id = account_id or ("ef_" + re.sub(r"[^A-Za-z0-9]+", "_", cfg_id).lower())[:60]
    if q(f"SELECT id FROM xcq_paper_accounts WHERE id='{acct_id}'"):
        raise ValueError(f"账户 {acct_id} 已存在")
    A.create_account(st["strategy_id"], name=(name or f"🧪 {s['name']} (前向观察)")[:100], account_id=acct_id,
                     initial_capital=float(capital), max_positions=30)
    return {"account_id": acct_id, "strategy_id": st["strategy_id"], "config_id": cfg_id}


# ── 每日信号 (paper_signals.generate 的 expert_follow_cfg 分支) ──

def compute(s: Dict[str, Any], data_date: str) -> Optional[Dict[str, Any]]:
    from strategy import backtest_v2 as FB
    from strategy import engine as E
    from strategy.selector import Selector
    env = s["universe"]["data_env"]
    ctx = E.get_ctx(env, data_date)
    if data_date not in ctx.px.didx:
        logger.warning(f"[expert-follow] {s['id']}: 行情还没有 {data_date}, 不生成")
        return None
    t = ctx.px.didx[data_date]
    sel = Selector(ctx, s)
    fs = sel.follow(t)
    names: Dict[str, str] = {}
    for e, _, _ in fs:
        for r in ctx.hist[e]["records"]:
            names.setdefault(r["stock_code"], r.get("stock_name") or "")
    follow = [{"expert_id": e, "name": ctx.hist[e].get("name") or e, "weight": round(w, 4)} for e, w, _ in fs]
    if s["execution"]["engine"] == "signal":
        g = s["execution"]["signal"]
        last: Dict[int, int] = {}
        for e, _, _ in fs:
            ev = ctx.events(e)
            for tt in range(max(0, t - int(g["lookback_days"]) + 1), t + 1):
                for r in ev.get(tt, []):
                    if r.get("action") in E.BUY_ACTIONS and float(r.get("weight_after") or 0) >= 1 \
                            and float(r.get("weight_after") or 0) > float(r.get("weight_before") or 0):
                        j = ctx.px.cidx[r["stock_code"]]
                        last[j] = max(last.get(j, -1), tt)
        picks = sorted(last, key=lambda j: -last[j])[:int(g["top_n"])]
        return {"follow_set": follow, "picks": [{"code": ctx.px.codes[j], "name": names.get(ctx.px.codes[j], "")} for j in picks]}
    old = FB.STOCK_CAP
    FB.STOCK_CAP = s["execution"]["stock_cap"]
    try:
        tw = FB.target_weights(ctx, fs, t)
    finally:
        FB.STOCK_CAP = old
    targets = sorted(({"code": ctx.px.codes[j], "name": names.get(ctx.px.codes[j], ""), "weight": round(float(w), 4)}
                      for j, w in tw.items()), key=lambda x: -x["weight"])
    return {"follow_set": follow, "targets": targets}


def generate_recs(spec: Dict[str, Any], data_date: str, pred_date: str):
    from strategy.ledger import Rec
    try:
        s = C.load_strategy(spec["config_id"])
    except C.ConfigError as exc:
        logger.warning(f"[expert-follow] {spec.get('config_id')}: {exc}")
        return None
    s["universe"]["data_env"] = spec.get("data_env") or s["universe"]["data_env"]
    if C.strategy_hash(s) != spec.get("config_hash"):
        logger.warning(f"[expert-follow] 策略文件 {s['id']} 已修改 (指纹 {C.strategy_hash(s)} ≠ 发布时 {spec.get('config_hash')}), "
                       f"不出信号: 改文件 = 新版本, 需重新回测/发布")
        return None
    res = compute(s, data_date)
    if res is None:
        return None
    who = "、".join(x["name"][:8] for x in res["follow_set"][:3])
    recs: List[Any] = []
    if "picks" in res:
        for i, x in enumerate(res["picks"], 1):
            recs.append(Rec(code=x["code"], name=x["name"], action="买入", reason_code="FOLLOW_CFG",
                            reason_text=f"{s['name']} · 信号#{i} · {who}", score=None, rank_no=i,
                            pool_ref=f"{KIND}:{s['id']}:{pred_date}", meta={"experts": [f["expert_id"] for f in res["follow_set"]]}))
    else:
        for i, x in enumerate(res["targets"], 1):
            recs.append(Rec(code=x["code"], name=x["name"], action="买入", reason_code="FOLLOW_CFG",
                            reason_text=f"{s['name']} · 目标仓位{x['weight']:.1%} · 跟随 {len(res['follow_set'])} 位: {who}",
                            score=x["weight"], rank_no=i, target_weight=x["weight"],
                            pool_ref=f"{KIND}:{s['id']}:{pred_date}", meta={"experts": [f["expert_id"] for f in res["follow_set"]]}))
    logger.info(f"[expert-follow] {s['id']} {data_date}→{pred_date}: 跟随 {len(res['follow_set'])} 位, 推荐 {len(recs)} 只")
    return recs
