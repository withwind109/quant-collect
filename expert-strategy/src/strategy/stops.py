"""策略自带止损/止盈: 模拟盘按"该策略自己的回测/配置口径"止损、止盈, 而不是所有账户统一 8% 和同一套移动止盈。

来源优先级(resolve):
  1. spec 里显式写了 stop_loss_pct        (预留; 目前没有策略写)
  2. wf_config  → configs/<name>.json5 的 backtest.stop_loss        (WF 回测里实际生效的止损)
  3. pipeline / baseline → xcq_pipeline_configs 的 normal_stop_loss (回测里随市场状态在 normal/cautious/defense 三档切换,
                            模拟盘不跟踪市场状态, 只取 normal 档 → 标 approx)
  4. 其余(高手Top10 / 共振 / 组合) 没有"自带止损" → 全局默认 8%, 明确标注来源, 不假装是策略自己的

止盈(resolve_exit, D24 决策"跟策略配置走"):
  wf_config → backtest.take_profit=false 就没有止盈/时间平仓(只按排名/持仓保留卖出 + 止损); =true 用 take_profit_params 覆盖分档
  pipeline / baseline → 按管线 exit_strategy: exit=分档移动止盈(取管线 trail_pct_*)+20日时间平仓; hold3/hold5=到期平仓; buyhold=不止盈
                        (回测里还有顶分型止盈/双轨退出/概念增强持有等, 模拟盘无法逐条复刻 → 标 approx)
  其余(高手Top10/共振/组合) 事件驱动, 没有自带止盈 → 沿用模拟盘统一规则(移动止盈+20日时间平仓)

不写进 spec_json: 止损/止盈只影响模拟盘的成交, 不影响推荐台账与前向证据(证据按固定持有期算), 写进 spec 会触发
"版本升级 → 证据清零 → 撤销实盘批准"。

迁移自 scripts/recs/stops.py (2026-10-16)。变更:
  * 存储 MySQL -> 本地 SQLite (strategy.recs_store); 路径常量 _ROOT (parents[2]) 删除;
  * 迁移降级: WF 配置 configs/*.json5 (json5_loader) 未迁移 -> _wf_stop/_wf_cfg 恒返回 None
    (与源 "读配置失败" 分支同行为: 用全局默认/平台统一规则);
  * 迁移降级: scripts/expert_follow/config.py (策略文件 risk_controls 读取) 未迁移 ->
    _cfg_risk 恒返回 {} (平台统一口径);
  * xcq_pipeline_configs 本项目无数据, recs_store 建空表保持查询链 (查不到 -> 全局默认, 同源行为)。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

from core.logger import logger
from strategy import registry, recs_store as store

DEFAULT_STOP = 0.08
FLOOR_STOP = 0.20      # [2026-09-26 D1] 兜底止损: 任何策略/账户都不会比 −20% 更宽 (策略关闭止损也一样)
_TTL = 300
_CACHE: Dict[str, Any] = {}


def _pipeline_stop(name: str) -> Optional[float]:
    rows = store.fetch("SELECT extra_params FROM xcq_pipeline_configs WHERE pipeline_name=:n", {"n": name})
    if not rows:
        return None
    try:
        v = (json.loads(rows[0]["extra_params"] or "{}").get("params") or {}).get("normal_stop_loss")
    except Exception:  # noqa: BLE001
        return None
    return abs(float(v)) / 100.0 if v not in (None, "") and float(v) != 0 else None


def _wf_stop(config_name: str) -> Optional[float]:
    """WF 配置止损。迁移降级: configs/*.json5 (json5_loader) 未迁移 -> 恒 None (调用方用全局默认)。"""
    logger.warning(f"[recs.stops] WF 配置 {config_name} 未迁移, 用全局默认")
    return None


_TRAIL_KEYS = ("trail_pct_small", "trail_pct_medium", "trail_pct_large", "trail_pct_huge")


def _wf_cfg(config_name: str) -> Optional[Dict[str, Any]]:
    """WF 回测配置 backtest 段。迁移降级: configs/*.json5 (json5_loader) 未迁移 -> 恒 None (调用方用平台统一规则)。"""
    logger.warning(f"[recs.stops] 读不到 WF 配置 {config_name} (未迁移), 用平台统一规则")
    return None


def _pipeline_row(name: str) -> Optional[Dict[str, Any]]:
    rows = store.fetch("SELECT exit_strategy, extra_params FROM xcq_pipeline_configs WHERE pipeline_name=:n", {"n": name})
    if not rows:
        return None
    try:
        params = (json.loads(rows[0]["extra_params"] or "{}").get("params") or {})
    except Exception:  # noqa: BLE001
        params = {}
    return {"exit": rows[0]["exit_strategy"] or "exit", "params": params}


def _default(why: str) -> Dict[str, Any]:
    return {"pct": DEFAULT_STOP, "own": False, "approx": False, "source": "default",
            "label": f"全局默认 {DEFAULT_STOP * 100:.0f}%", "note": why}


def _resolve(strategy: Dict[str, Any]) -> Dict[str, Any]:
    try:
        spec = json.loads(strategy.get("spec_json") or "{}")
    except Exception:  # noqa: BLE001
        spec = {}
    v = spec.get("stop_loss_pct")
    if v not in (None, "") and float(v) > 0:
        p = float(v)
        return {"pct": p, "own": True, "approx": False, "source": "spec",
                "label": f"策略设定 {p * 100:g}%", "note": "策略定义里显式指定"}
    engine = spec.get("engine")
    if not engine and str(strategy.get("strategy_id", "")).startswith("rec:"):
        return {"pct": 0.07, "own": False, "approx": True, "source": "rec_file", "label": "推荐参考止损约 7%(固定值)",
                "note": "推荐文件里每只票带一个固定参考止损(约 -7%), 与策略配置无关; 该策略不开模拟盘账户, 只用于个股推荐展示"}
    if engine == "wf_config":
        cfg = spec.get("config_name", "")
        p = _wf_stop(cfg)
        if p:
            return {"pct": p, "own": True, "approx": False, "source": "wf_config",
                    "label": f"WF 配置止损 {p * 100:g}%", "note": f"{cfg}.json5 的 backtest.stop_loss, 与 WF 回测一致"}
        return _default(f"WF 配置 {cfg} 未设置止损, 用全局默认")
    if engine in ("pipeline", "paper_signal"):
        name = spec.get("pipeline_name") or spec.get("arena_ref")
        p = _pipeline_stop(name) if name else None
        if p:
            return {"pct": p, "own": True, "approx": True, "source": "pipeline",
                    "label": f"管线止损 {p * 100:g}%",
                    "note": f"管线 {name} 的 normal_stop_loss; 回测里止损随市场状态在 正常/谨慎/防御 三档切换, "
                            f"模拟盘不跟踪市场状态, 只取正常档"}
        return _default(f"找不到管线 {name} 的止损参数, 用全局默认")
    if engine == "combo":
        return _default("组合策略由多个成员合成, 成员止损各不相同, 暂用全局默认")
    if engine == "expert_follow_cfg":
        rc = _cfg_risk(spec)
        if rc.get("stop_loss") not in (None, ""):
            p = float(rc["stop_loss"])
            return {"pct": p, "own": True, "approx": False, "source": "strategy_file",
                    "label": f"策略文件止损 {p * 100:g}%", "note": f"configs/expert_follow/strategies/{spec.get('config_id')}.json5 的 risk_controls"}
        return _default("策略文件未设置止损 (risk_controls.stop_loss=null), 用平台统一止损")
    return _default("该策略是事件驱动(卖出靠明确的卖出推荐/止盈/20日平仓), 没有自带止损, 用全局默认作兜底")


def _exit_default(why: str) -> Dict[str, Any]:
    return {"mode": "default", "params": None, "own": False, "approx": False, "source": "default",
            "label": "模拟盘统一止盈规则", "note": why}


def _resolve_exit(strategy: Dict[str, Any]) -> Dict[str, Any]:
    try:
        spec = json.loads(strategy.get("spec_json") or "{}")
    except Exception:  # noqa: BLE001
        spec = {}
    engine = spec.get("engine")
    if engine == "wf_config":
        cfg = spec.get("config_name", "")
        bt = _wf_cfg(cfg)
        if bt is None:
            return _exit_default(f"读不到 WF 配置 {cfg}, 用统一规则")
        if "take_profit" not in bt:          # [2026-09-26] 配置没写 → 平台统一规则 (与 WF 回测引擎同口径)
            return _exit_default(f"{cfg}.json5 未设置 backtest.take_profit, 用平台统一规则")
        if not bt.get("take_profit"):
            return {"mode": "off", "params": {"enabled": False}, "own": True, "approx": False, "source": "wf_config",
                    "label": "无止盈/时间平仓", "note": f"{cfg}.json5 的 backtest.take_profit=false: 只按排名/持仓保留卖出, 加止损"}
        over = {k: v for k, v in (bt.get("take_profit_params") or {}).items() if k in _TRAIL_KEYS or k == "max_hold_days"}
        over["top_fractal"] = True             # WF 回测的止盈含顶分止盈 (stock_walkforward._take_profit_signal)
        return {"mode": "trail", "params": over, "own": True, "approx": False, "source": "wf_config",
                "label": "分档移动止盈" + ("(配置覆盖 " + ",".join(f"{k}={v}" for k, v in over.items()) + ")" if over else ""),
                "note": f"{cfg}.json5 的 backtest.take_profit=true"}
    if engine in ("pipeline", "paper_signal"):
        name = spec.get("pipeline_name") or spec.get("arena_ref")
        row = _pipeline_row(name) if name else None
        if not row:
            return _exit_default(f"找不到管线 {name}, 用统一规则")
        ex, params = row["exit"], row["params"]
        approx = ("回测的止损随市场状态在 正常/谨慎/防御 三档切换 (依赖回测内部的概念预测准确率), 模拟盘只取正常档; "
                  "回测还有双轨退出、概念增强持有等规则, 模拟盘只复刻主干规则 (止损/时间平仓/移动止盈/顶分止盈/持有N日)")
        if ex == "buyhold":
            return {"mode": "off", "params": {"enabled": False}, "own": True, "approx": True, "source": "pipeline",
                    "label": "无止盈/时间平仓", "note": f"管线 {name} 的退出策略 buyhold(不主动卖出); {approx}"}
        if ex in ("hold3", "hold5"):
            n = int(ex[-1])
            return {"mode": "time", "params": {"trail": False, "max_hold_days": n}, "own": True, "approx": True,
                    "source": "pipeline", "label": f"持有满 {n} 日平仓", "note": f"管线 {name} 的退出策略 {ex}; {approx}"}
        over = {k: params[k] for k in _TRAIL_KEYS if params.get(k) not in (None, "")}
        over["top_fractal"] = True             # 管线 exit 模式含顶分止盈 (backtest_pipeline.detect_sell_signal)
        return {"mode": "trail", "params": over, "own": True, "approx": True, "source": "pipeline",
                "label": "分档移动止盈(取管线参数)" if over else "分档移动止盈",
                "note": f"管线 {name} 的退出策略 exit: 盈利≥8% 后按回撤(小/中/大/超大盈利档 " +
                        "/".join(str(over.get(k, "默认")) for k in _TRAIL_KEYS) + "%)止盈, 持有超20日平仓; " + approx}
    if engine == "expert_follow_cfg":
        tp = _cfg_risk(spec).get("take_profit", "default")
        if tp == "off":
            return {"mode": "off", "params": {"enabled": False}, "own": True, "approx": False, "source": "strategy_file",
                    "label": "无止盈/时间平仓", "note": "策略文件 risk_controls.take_profit=off"}
        if isinstance(tp, dict):
            return {"mode": "trail", "params": tp, "own": True, "approx": False, "source": "strategy_file",
                    "label": "策略文件止盈参数", "note": f"risk_controls.take_profit={tp}"}
        return _exit_default("策略文件未设置止盈 (take_profit=default), 沿用统一规则")
    if engine == "expert_imitation_live":
        mh = int(spec.get("max_hold_days") or 20)
        sl = float(spec.get("stop_loss_pct") or 0.08)
        return {"mode": "off", "params": {"enabled": False}, "own": True, "approx": False, "source": "strategy",
                "label": f"无移动止盈 (满{mh}日平仓在夜间决策里)",
                "note": f"与实盘模仿模型回测一致: 只有 {sl * 100:g}% 止损 + 跟卖 + 持有满 {mh} 日平仓"}
    return _exit_default("该策略是事件驱动, 没有自带止盈, 沿用统一规则(移动止盈 + 20日时间平仓)")


def _cfg_risk(spec: Dict[str, Any]) -> Dict[str, Any]:
    """expert_follow_cfg 策略: 读策略文件的 risk_controls (读不到 → 平台统一)。

    迁移降级: scripts/expert_follow/config.py (策略文件 json5 加载) 未迁移 -> 恒 {} (平台统一口径)。
    """
    return {}


def resolve_exit(strategy: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """策略 → 止盈规则 {mode: default|off|time|trail, params(交给盘中止盈判断的覆盖项), own, approx, label, note}。"""
    if not strategy:
        return _exit_default("未绑定策略")
    key = f"exit:{strategy['strategy_id']}@{strategy.get('version')}"
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    out = _resolve_exit(strategy)
    _CACHE[key] = (time.time(), out)
    return out


def exit_params_for_account(acct: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """账户盘中止盈用的覆盖参数; None = 用模拟盘统一规则。"""
    sid = acct.get("strategy_id")
    if not sid:
        return None
    st = registry.get_strategy(sid)
    return (resolve_exit(st)["params"] or None) if st else None


def resolve(strategy: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """策略 → {pct(小数), own(是否策略自带), approx, source, label, note}; 缓存 5 分钟(配置极少变)。"""
    if not strategy:
        return _default("未绑定策略")
    key = f"{strategy['strategy_id']}@{strategy.get('version')}"
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    out = _floor(_resolve(strategy))
    _CACHE[key] = (time.time(), out)
    return out


def _floor(r: Dict[str, Any]) -> Dict[str, Any]:
    """D1: 策略设置的止损宽于 −20% (或未开启) 时, 按 −20% 兜底。"""
    p = float(r.get("pct") or 0)
    if p <= 0 or p > FLOOR_STOP:
        return {**r, "pct": FLOOR_STOP, "label": f"兜底止损 {FLOOR_STOP * 100:.0f}% (原: {r.get('label')})",
                "note": (r.get("note") or "") + f"; 策略止损宽于 {FLOOR_STOP * 100:.0f}% 或未设置, 按平台兜底"}
    return r


def clear_cache() -> None:
    _CACHE.clear()


def for_account(acct: Dict[str, Any]) -> float:
    """账户实际生效的止损比例(小数)。绑定了策略 → 策略自带; 否则用账户自身配置。"""
    sid = acct.get("strategy_id")
    if sid:
        st = registry.get_strategy(sid)
        if st:
            return float(resolve(st)["pct"])
    v = acct.get("stop_loss_pct")
    p = float(v) if v not in (None, "") else DEFAULT_STOP
    return FLOOR_STOP if p <= 0 or p > FLOOR_STOP else p


def sync_accounts() -> list:
    """把绑定策略的账户 stop_loss_pct 列同步成策略解析值(供直接读该列的旧代码/展示一致)。幂等。"""
    store.ensure_schema()
    out = []
    for a in store.fetch("SELECT id,strategy_id,stop_loss_pct FROM xcq_paper_accounts WHERE strategy_id IS NOT NULL AND strategy_id<>''"):
        st = registry.get_strategy(a["strategy_id"])
        if not st:
            continue
        p = round(float(resolve(st)["pct"]), 4)
        if abs(float(a["stop_loss_pct"] or 0) - p) > 1e-9:
            store.run("UPDATE xcq_paper_accounts SET stop_loss_pct=:p WHERE id=:i", {"p": p, "i": a["id"]})
            out.append({"account_id": a["id"], "from": float(a["stop_loss_pct"] or 0), "to": p})
    return out
