"""跟随高手策略配置化 (2026-09-25) —— 策略文件 / 回测配置 的加载、校验、参数扫描展开、指纹与中文描述。

文件位置 (用户决策): configs/expert_follow/
  strategies/<id>.json5        内置策略 (v1/v2/v2.1/v3/v3.1, 页面只读)
  strategies/user/<id>.json5   页面"另存为"的用户策略
  backtests/<id>.json5         回测配置 (测哪些策略 + 统一回测期 + 基准 + 随机对照 + 闸门 + 显示规则)

同一份策略文件既用于回测 (strategy/runner.py) 也用于模拟盘/正式交易 (strategy/live.py),
保证"回测验证的就是上线跑的"。内容指纹 (hash) 随运行和账户一起记录, 改文件 = 新版本。

迁移自 scripts/expert_follow/config.py (2026-10-16): 路径常量 (ROOT/CFG_DIR/STRAT_DIR/USER_DIR/
BT_DIR/BT_USER_DIR) 删除, 改从 paths 模块取 (FOLLOW_STRAT_DIR / FOLLOW_USER_DIR /
FOLLOW_BT_DIR_CFG / FOLLOW_BT_USER_DIR); curve 默认值改从 strategy.curve 导入; 其余逐行原样。
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import json5

from paths import (FOLLOW_BT_DIR_CFG, FOLLOW_BT_USER_DIR, FOLLOW_STRAT_DIR,
                   FOLLOW_USER_DIR)

STRAT_DIR = FOLLOW_STRAT_DIR
USER_DIR = FOLLOW_USER_DIR
BT_DIR = FOLLOW_BT_DIR_CFG
BT_USER_DIR = FOLLOW_BT_USER_DIR

FAMILIES = ("v1", "v2", "v2.1", "v3", "v3.1", "curve")
ENGINES = ("mirror", "signal", "v3_tiers")
SOURCES = ("all_active", "watchlist", "expert_ids")
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{2,60}$")

STRATEGY_DEFAULTS: Dict[str, Any] = {
    "id": "", "family": "v2", "name": "", "description": "", "bind_strategy_id": None,
    "universe": {"data_env": "sim", "source": "all_active", "include_watchlist": False, "expert_ids": None,
                 "max_idle_days": 60},
    "screen": None, "rank": None, "risk": None, "weighting": "rank",
    # [2026-09-27] PnL 曲线稳定向上筛选 (strategy/curve.py), null = 不筛
    "curve_screen": None,
    "execution": {"engine": "mirror", "exec": "t1_open", "stock_cap": 0.20},
    "models": None,
    # [2026-09-26 D1] 止损/止盈跟随策略设置; stop_loss=null → 平台统一 (8%); 任何情况下兜底 −20% (strategy/stops.FLOOR_STOP)
    # take_profit: "default" (平台统一: 分档移动止盈 + 满 20 日平仓) / "off" / {max_hold_days, trail_pct_*, trail}
    "risk_controls": {"stop_loss": None, "take_profit": "default"},
}
RISK_DEFAULTS = {"stop_loss": None, "take_profit": "default"}
SCREEN_DEFAULTS = {"window": 60, "min_ret": 0.0, "max_mdd": 0.20, "min_trades": 8, "recent_days": 20,
                   "min_closed": 5, "min_win": 0.55, "win_cost": 0.003, "min_pool": 10}
SIGNAL_DEFAULTS = {"lookback_days": 5, "top_n": 10, "max_positions": 8, "stop_loss": 0.08, "max_hold": 20}
GATE_DEFAULTS = {"alpha_t_gt": 0.0, "beat_null_own": True, "beat_null_multi": True, "alpha_gain_vs_baseline_pp": 1.0,
                 "halves_gain_pp": 0.0, "mdd_not_worse": True, "min_buys": 60, "alpha_gain_vs_unscreened_pp": 1.0,
                 "beat_random_exclusion": True}


class ConfigError(ValueError):
    pass


# ── 读写 ──

def _read(fp: Path) -> Dict[str, Any]:
    try:
        return json5.loads(fp.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"{fp.name} 解析失败: {exc}") from exc


def strategy_path(sid: str) -> Optional[Path]:
    for d in (STRAT_DIR, USER_DIR):
        fp = d / f"{sid}.json5"
        if fp.exists():
            return fp
    return None


def load_strategy(sid: str) -> Dict[str, Any]:
    fp = strategy_path(sid)
    if fp is None:
        raise ConfigError(f"找不到策略文件 {sid}.json5")
    s = normalize_strategy(_read(fp))
    if s["id"] != sid:
        raise ConfigError(f"{fp.name} 的 id={s['id']} 与文件名不一致")
    s["_builtin"] = fp.parent == STRAT_DIR
    return s


def list_strategies() -> List[Dict[str, Any]]:
    out = []
    for d, builtin in ((STRAT_DIR, True), (USER_DIR, False)):
        for fp in sorted(d.glob("*.json5")):
            try:
                s = load_strategy(fp.stem)
                out.append({**s, "_builtin": builtin, "hash": strategy_hash(s), "label": describe(s)})
            except ConfigError as exc:
                out.append({"id": fp.stem, "error": str(exc), "_builtin": builtin})
    return out


def save_user_strategy(spec: Dict[str, Any]) -> Dict[str, Any]:
    """页面"另存为": 只能写到 strategies/user/, 不能覆盖内置策略。"""
    s = normalize_strategy(spec)
    if (STRAT_DIR / f"{s['id']}.json5").exists():
        raise ConfigError(f"{s['id']} 是内置策略, 请换一个 id 另存")
    if s.get("bind_strategy_id"):
        raise ConfigError("用户策略不能绑定已有模拟账户的策略 ID (bind_strategy_id 只用于内置策略)")
    USER_DIR.mkdir(parents=True, exist_ok=True)
    body = {k: v for k, v in s.items() if not k.startswith("_")}
    (USER_DIR / f"{s['id']}.json5").write_text(
        "// 页面另存的用户策略 (strategy/config.py::save_user_strategy)\n"
        + json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return s


def backtest_path(bid: str) -> Optional[Path]:
    for d in (BT_DIR, BT_USER_DIR):
        fp = d / f"{bid}.json5"
        if fp.exists():
            return fp
    return None


def load_backtest(bid: str) -> Dict[str, Any]:
    fp = backtest_path(bid)
    if fp is None:
        raise ConfigError(f"找不到回测配置 {bid}.json5")
    b = _read(fp)
    b.setdefault("id", bid)
    if b["id"] != bid:
        raise ConfigError(f"{fp.name} 的 id 与文件名不一致")
    if b.get("data_env") not in ("sim", "live"):
        raise ConfigError("data_env 只能是 sim / live")
    per = b.get("period") or {}
    if not per.get("start") or not per.get("split"):
        raise ConfigError("period 需要 start 与 split")
    b["gate"] = {**GATE_DEFAULTS, **(b.get("gate") or {})}
    b["null"] = {"seeds": 20, "q": 0.80, **(b.get("null") or {})}
    b["display"] = {"top_n": 10, **(b.get("display") or {})}
    b["capital"] = float(b.get("capital") or 1_000_000)
    if not b.get("baseline"):
        raise ConfigError("需要 baseline (基准策略 id)")
    return b


def list_backtests() -> List[Dict[str, Any]]:
    out = []
    for fp in sorted(BT_DIR.glob("*.json5")) + sorted(BT_USER_DIR.glob("*.json5")):
        try:
            b = load_backtest(fp.stem)
            out.append({"id": b["id"], "name": b.get("name") or b["id"], "data_env": b["data_env"],
                        "period": b["period"], "n_strategies": len(b.get("strategies") or []),
                        "_builtin": fp.parent == BT_DIR})
        except ConfigError as exc:
            out.append({"id": fp.stem, "error": str(exc)})
    return out


def save_user_backtest(body: Dict[str, Any]) -> Dict[str, Any]:
    """页面"自定义回测": 写到 backtests/user/。闸门/随机对照分位/基准固定沿用内置 unified_<env> (运行前写定, 页面不能改, 不放宽)。"""
    bid = str(body.get("id") or "")
    if not _ID_RE.match(bid):
        raise ConfigError("回测 id 只能用字母/数字/下划线/连字符, 2~60 个字符")
    if (BT_DIR / f"{bid}.json5").exists():
        raise ConfigError(f"{bid} 是内置回测配置, 请换一个 id")
    env = body.get("data_env") or "sim"
    ref = load_backtest(f"unified_{env}")
    per = body.get("period") or {}
    start, split = str(per.get("start") or ref["period"]["start"]), str(per.get("split") or ref["period"]["split"])
    if not (re.match(r"^\d{4}-\d{2}-\d{2}$", start) and re.match(r"^\d{4}-\d{2}-\d{2}$", split) and start < split):
        raise ConfigError("回测期: start / split 需为 YYYY-MM-DD 且 start < split")
    ents = []
    for e in body.get("strategies") or []:
        load_strategy(e["ref"])
        ent = {"ref": e["ref"]}
        if e.get("sweep"):
            ent["sweep"] = e["sweep"]
        ents.append(ent)
    if not ents:
        raise ConfigError("至少选一个策略")
    b = {"id": bid, "name": str(body.get("name") or bid)[:60], "data_env": env,
         "period": {"start": start, "end": "latest", "split": split}, "capital": ref["capital"],
         "baseline": ref["baseline"], "strategies": ents,
         "null": {"seeds": int(max(5, min(50, int((body.get("null") or {}).get("seeds") or ref["null"]["seeds"])))),
                  "q": ref["null"]["q"]},
         "gate": ref["gate"], "display": {"top_n": int(max(3, min(50, int((body.get("display") or {}).get("top_n") or 10))))}}
    if ref.get("data_health"):
        b["data_health"] = ref["data_health"]
    for e in ents:                                   # 展开校验 (扫描参数不合法在这里就报错)
        expand(e, env)
    BT_USER_DIR.mkdir(parents=True, exist_ok=True)
    (BT_USER_DIR / f"{bid}.json5").write_text(
        "// 页面自定义回测 (strategy/config.py::save_user_backtest); 闸门/基准沿用 unified_" + env + "\n"
        + json.dumps(b, ensure_ascii=False, indent=2), encoding="utf-8")
    return load_backtest(bid)


# ── 校验 ──

def _merge(base: Dict[str, Any], over: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = v
    return out


def normalize_strategy(raw: Dict[str, Any]) -> Dict[str, Any]:
    s = copy.deepcopy(STRATEGY_DEFAULTS)
    for k, v in (raw or {}).items():
        if k in s and isinstance(s[k], dict) and isinstance(v, dict):
            s[k] = _merge(s[k], v)
        elif k in s or k.startswith("_"):
            s[k] = copy.deepcopy(v)
    if not _ID_RE.match(str(s["id"] or "")):
        raise ConfigError("id 只能用字母/数字/下划线/连字符, 2~60 个字符")
    if s["family"] not in FAMILIES:
        raise ConfigError(f"family 只能是 {FAMILIES}")
    u = s["universe"]
    if u["data_env"] not in ("sim", "live"):
        raise ConfigError("universe.data_env 只能是 sim / live")
    if u["source"] not in SOURCES:
        raise ConfigError(f"universe.source 只能是 {SOURCES}")
    if u["source"] == "expert_ids" and not u.get("expert_ids"):
        raise ConfigError("source=expert_ids 时需要 expert_ids")
    if u.get("expert_ids"):
        u["expert_ids"] = [str(x) for x in u["expert_ids"]]
    u["max_idle_days"] = int(u.get("max_idle_days") or 60)
    ex = s["execution"]
    if ex.get("engine") not in ENGINES:
        raise ConfigError(f"execution.engine 只能是 {ENGINES}")
    if ex.get("exec", "t1_open") != "t1_open":
        raise ConfigError("目前只支持 exec=t1_open (次日开盘执行; 盘中跟单在 v2 回测里已证实更差)")
    ex["stock_cap"] = float(ex.get("stock_cap") or 0.20)
    if ex["engine"] == "signal":
        ex["signal"] = _merge(SIGNAL_DEFAULTS, ex.get("signal"))
    if s["screen"] is not None:
        s["screen"] = _merge(SCREEN_DEFAULTS, s["screen"])
        s["screen"]["window"] = int(max(20, min(180, int(s["screen"]["window"]))))
    if s.get("curve_screen") is not None:
        from strategy.curve import CURVE_DEFAULTS
        bad = set(s["curve_screen"]) - set(CURVE_DEFAULTS)
        if bad:
            raise ConfigError(f"curve_screen 不认识的字段 {sorted(bad)}")
        s["curve_screen"] = {**CURVE_DEFAULTS, **s["curve_screen"]}
    if ex["engine"] == "mirror":
        r = s["rank"] or {}
        pick = r.get("pick") or {}
        if not (pick.get("all") or pick.get("top_k") or pick.get("top_pct") or pick.get("band")):
            raise ConfigError("mirror 引擎需要 rank.pick: {top_k} / {top_pct} / {band:[lo,hi]} / {all:true}")
        r["days"] = int(max(1, min(120, int(r.get("days") or 1))))
        if pick.get("top_pct") is not None and not (0 < float(pick["top_pct"]) <= 1):
            raise ConfigError("rank.pick.top_pct 应在 (0, 1]")
        s["rank"] = r
    if ex["engine"] == "v3_tiers" and (s["screen"] is None or not s.get("models")):
        raise ConfigError("v3_tiers 引擎需要 screen 与 models")
    if s["weighting"] not in ("rank", "equal"):
        raise ConfigError("weighting 只能是 rank / equal")
    rc = s["risk_controls"] = {**RISK_DEFAULTS, **(s.get("risk_controls") or {})}
    if rc["stop_loss"] is not None and not (0 < float(rc["stop_loss"]) <= 1):
        raise ConfigError("risk_controls.stop_loss 应为 null (平台统一) 或 (0, 1] 的比例; 宽于 0.20 时按 −20% 兜底")
    if not (rc["take_profit"] in ("default", "off") or isinstance(rc["take_profit"], dict)):
        raise ConfigError("risk_controls.take_profit 只能是 default / off / {参数}")
    if s["risk"] is not None:
        bad = set(s["risk"]) - {"max_day_ret", "max_top_w", "min_age", "label"}
        if bad:
            raise ConfigError(f"risk 不认识的字段 {sorted(bad)}")
    return s


def strategy_hash(s: Dict[str, Any]) -> str:
    body = {k: v for k, v in s.items() if not k.startswith("_") and k not in ("name", "description")}
    if body.get("risk_controls") in (None, RISK_DEFAULTS):     # 默认止损止盈不进指纹 (旧文件/已发布账户指纹不变)
        body.pop("risk_controls", None)
    if body.get("curve_screen") is None:                          # [2026-09-27] 新字段, 没用到时不进指纹 (已发布账户指纹不变)
        body.pop("curve_screen", None)
    return hashlib.sha1(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:10]


# ── 参数扫描 ──

def _set_path(d: Dict[str, Any], path: str, v: Any) -> None:
    ks = path.split(".")
    cur = d
    for k in ks[:-1]:
        if cur.get(k) is None:
            cur[k] = {}
        cur = cur[k]
    cur[ks[-1]] = copy.deepcopy(v)


def _short(v: Any) -> str:
    if isinstance(v, dict):
        return "+".join(f"{k}{_short(x)}" for k, x in v.items())
    if v is None:
        return "off"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def expand(entry: Dict[str, Any], data_env: str) -> List[Tuple[str, Dict[str, Any]]]:
    """回测配置里的一项 {ref, sweep?} → [(方案 key, 解析后的策略)]。数据环境以回测配置为准。"""
    base = load_strategy(entry["ref"])
    base["universe"]["data_env"] = data_env
    sw = entry.get("sweep") or {}
    out: List[Tuple[str, Dict[str, Any]]] = []
    if sw.get("variants"):
        for v in sw["variants"]:
            s = copy.deepcopy(base)
            for p, x in (v.get("set") or {}).items():
                _set_path(s, p, x)
            out.append((f"{base['id']}[{v['name']}]", normalize_strategy(s)))
    elif sw.get("grid"):
        keys = list(sw["grid"])
        for combo in itertools.product(*[sw["grid"][k] for k in keys]):
            s = copy.deepcopy(base)
            for p, x in zip(keys, combo):
                _set_path(s, p, x)
            name = ",".join(f"{p.split('.')[-1]}={_short(x)}" for p, x in zip(keys, combo))
            out.append((f"{base['id']}[{name}]", normalize_strategy(s)))
    else:
        out.append((base["id"], base))
    return out


# ── 中文描述 (页面/结果标签统一由此生成, 与配置内容一致) ──

def describe(s: Dict[str, Any]) -> str:
    ex = s["execution"]
    u = s["universe"]
    parts: List[str] = []
    if u["source"] == "watchlist":
        who = "关注高手"
    elif u["source"] == "expert_ids":
        who = f"指定 {len(u.get('expert_ids') or [])} 位高手"
    else:
        who = "全体活跃高手" + (" ∪ 关注" if u.get("include_watchlist") else "")
    parts.append(who)
    sc = s.get("screen")
    if sc:
        rules = []
        if sc.get("min_ret") is not None:
            rules.append("收益>0")
        rules += [f"回撤≤{sc['max_mdd'] * 100:g}%", "活跃", f"胜率≥{sc['min_win'] * 100:g}%"]
        parts.append(f"{sc['window']}日筛选({'/'.join(rules)})")
    cs = s.get("curve_screen")
    if cs:
        parts.append(f"曲线稳定向上(最低累计≥{cs['min_cum_floor'] * 100:g}%/R²≥{cs['min_r2']:g}/上涨段≥{cs['min_up_seg'] * 100:.0f}%)")
    if ex["engine"] == "signal":
        g = ex["signal"]
        parts.append(f"近{g['lookback_days']}日买入信号前{g['top_n']} · {g['max_positions']}槽等权 · 止损{g['stop_loss'] * 100:g}%")
        return " · ".join(parts)
    if ex["engine"] == "v3_tiers":
        parts.append("按收益分批 + 模型")
        return " · ".join(parts)
    r = s.get("rank") or {}
    pick = r.get("pick") or {}
    if pick.get("all"):
        parts.append("全部跟随")
    else:
        if pick.get("top_k"):
            g = f"前{pick['top_k']}名"
        elif pick.get("top_pct"):
            g = f"前{float(pick['top_pct']) * 100:g}%"
        else:
            g = f"第{pick['band'][0]}~{pick['band'][1]}名"
        parts.append(f"近{r['days']}日收益{g}")
    rk = s.get("risk") or {}
    rr = []
    if rk.get("max_day_ret") is not None:
        rr.append(f"剔除当日涨>{rk['max_day_ret'] * 100:g}%")
    if rk.get("max_top_w") is not None:
        rr.append(f"剔除单票>{rk['max_top_w'] * 100:g}%")
    if rk.get("min_age") is not None:
        rr.append(f"剔除开户<{rk['min_age']}日")
    if rr:
        parts.append("、".join(rr))
    parts.append("名次加权" if s["weighting"] == "rank" else "等权")
    return " · ".join(parts)
