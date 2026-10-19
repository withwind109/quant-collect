"""迁移自 scripts/experts/pit_nav.py (2026-10-16)。变更 (仅导入路径/存储后端/文件路径, 业务逻辑逐行保持):
  * 价格后端: scripts/stock_graph/data.py (qlib bin 直读) -> strategy.prices (本地 SQLite price_daily);
    PriceCtx.__init__ 直接调 prices.load_panel, 之后把「本地完全没有行情的代码」(adjclose/close 全 NaN 的列)
    从 codes/cidx 剔除 —— 等价原版按 qlib 目录存在性过滤; adj/raw/fac/limits 同步裁剪;
    _prices() 保留为价格面板适配器 (adjclose/close/factor 走 prices.load_panel;
    open/high/low 直接读 price_daily 表, load_panel 不返回这三个字段);
  * sim 历史: experts_crawl/data/history_bulk/*.json -> paths.HISTORY_BULK_DIR/*.json;
  * live 流水: MySQL xcq_expert_trades -> core.db.q 读 expert_trades 视图 (crawl_trade 的
    market_type='live' 投影, SQL 原文保留); ensure_expert_trades_weight_label 删除 (SQLite 无需);
  * OUT_FP / OUT_FP_LIVE: experts_crawl/data/pit_nav*.npz -> paths.DATA_DIR/pit_nav*.npz。

高手组合逐日净值重建 + 时点(PIT)排名 (2026-09-24, 个股图谱 GNN 第二轮)。

为什么要重建: 站点"高手操作流水"按**当前**总收益选人, 回补历史时直接用这批人和他们当前的
收益率 = 用未来信息选人(7 月买入的票之所以"看起来好", 部分正是因为这些人 7-9 月赚了钱才上榜)。
这里只用 d-1 及以前的数据, 算每个组合"截至当时"的近 60 个交易日收益, 按当时排名选人。

输入: data/history_bulk/<zjzh>.json (src/bulk_history.py 拉的调仓记录, 含调仓前后仓位%)
价格: adjclose / close / factor (strategy/prices.py 本地 SQLite, 替代 qlib bin 直读)

逐日重建 (交易日 t):
  未调仓的持股:  贡献 w·(adjclose_t/adjclose_{t-1} - 1)
  当日调仓的持股: 贡献 w_before·(成交价·factor_t/adjclose_{t-1} - 1) + w_after·(close_t/最后成交价 - 1)
                 (w_before 用站点报的调仓前仓位, 比我们漂移出来的更准)
  组合日收益 R_t = 各项贡献之和(现金收益记 0); 之后按 (1+r_i)/(1+R_t) 漂移权重。
窗口开始前就持有、窗口内第一次出现在调仓记录里的股票(调仓前仓位>0), 视为从窗口开始就持有,
初始仓位按价格回推近似。重建精度用"调仓时我们漂移出的仓位 vs 站点报的调仓前仓位"的误差衡量。

局限(已在方案文档声明): 名单本身 = 2026-09 中下旬出现在站点流水里的组合(幸存者), 这一层偏差
PIT 排名消不掉; 能消掉的是"用当前收益率排名/加权"这一层前视。
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from core.logger import logger
from paths import DATA_DIR, HISTORY_BULK_DIR
from strategy import prices

OUT_FP = DATA_DIR / "pit_nav.npz"          # 模拟盘组合逐日净值 (原 experts_crawl/data/pit_nav.npz)

NAV_START = "2025-12-01"
# [2026-09-27 用户决策: 用全部实盘流水] 实盘数据环境从 2024-07-01 起重放 (之前只从 2025-12-01, 12 月前建的仓位成了"卖出未见买入")
NAV_START_LIVE = "2024-07-01"
TRAIL_DAYS = 60        # 排名用的近N个交易日收益
MIN_W = 1e-4


def load_histories(env: str = "sim") -> Dict[str, Dict[str, Any]]:
    """高手交易历史, 按数据环境加载 [2026-09-25]。
    sim: data/history_bulk/*.json (模拟盘组合全量历史, 4194 组合);
    live: expert_trades 视图 (market_type='live', rtV3 实盘流水, 每日增量累积)。
    两者输出同构 {expert_id: {name, records[{trade_date,stock_code,action,price,weight_*}]}}。
    """
    if env == "live":
        return _load_live_histories()
    out = {}
    for fp in sorted(HISTORY_BULK_DIR.glob("*.json")):
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        recs = [r for r in d.get("records") or [] if r.get("trade_date") and r.get("stock_code")]
        recs.sort(key=lambda r: r.get("trade_time") or r["trade_date"])
        out[d["zjzh"]] = {"name": d.get("name") or "", "records": recs}
    return out


def _load_live_histories() -> Dict[str, Dict[str, Any]]:
    """实盘: 从 DB 读 rtV3 流水转成与 history_bulk 同构的 records。"""
    from core.db import q

    def _f(v):
        try:
            s = str(v or "").strip()
            return float(s) if s else None
        except (TypeError, ValueError):
            return None

    rows = q("""SELECT expert_id, expert_name, trade_date, trade_time, stock_code, stock_name,
                       action, price, weight_before, weight_after, volume, weight_label
                FROM expert_trades
                WHERE market_type='live' AND stock_code<>'' AND price IS NOT NULL
                ORDER BY expert_id, trade_date, trade_time""")
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows or []:
        eid = str(r["expert_id"] or "")
        if not eid:
            continue
        rec = out.setdefault(eid, {"name": r.get("expert_name") or eid, "records": []})
        rec["records"].append({
            "trade_date": str(r.get("trade_date") or ""),
            "trade_time": str(r.get("trade_time") or ""),
            "stock_code": str(r.get("stock_code") or ""),
            "stock_name": str(r.get("stock_name") or ""),
            "action": str(r.get("action") or ""),
            "price": _f(r.get("price")),
            "weight_before": _f(r.get("weight_before")),
            "weight_after": _f(r.get("weight_after")),
            "volume": _f(r.get("volume")),
            "weight_label": (str(r.get("weight_label") or "").strip() or None),
        })
    return out


# ── 实盘 rtV3 流水 → 调仓前后仓位 (2026-09-25) ──
# positionRatio 语义 (用 19 个带成交量的组合核对): 买卖都是 成交金额 / 组合总资产, 按"成"向下取整分档
#   ('k成' = [k, k+1)成; '1成以下' < 1成; '9成以上' ≥ 9成)。证据: 同月买入与卖出反推的总资产中位数之比 0.99;
#   '1成' 的卖出有 57% 是整只清仓 (所以不是"卖出持仓的比例")。
# 旧口径把它当成"调仓后仓位", 卖出后仍留 10%, 期末持仓里 84% 是已卖出的幽灵仓位。
LIVE_MAX_UNKNOWN_SELL = 0.20      # 卖出了"没见过买入"的股票占卖出笔数的比例上限 (超过 = 起点持仓不明, 组合不可用)
LIVE_ORPHAN_FRAC = 0.02           # 无原文记录 ≤ 2% (且零星) 视为平台已撤下的记录, 丢弃并记缺口日
LIVE_CLEAR_FRAC = 0.0             # 卖出金额 ≥ 该股市值×此比例 视为清仓。用 36 个带成交量组合的股数账本校准:
                                  # 0.9/0.6/0.4/0.25/0 → 期末持仓一致率中位 0.28/0.33/0.50/0.50/1.00 (均值 0.36→0.84),
                                  # 分档太粗、分辨不出部分卖出, "每笔卖出即清仓"最接近真实持仓


def live_ratio(label: Optional[str]) -> Optional[float]:
    """成交金额占总资产比例 (分档中点): '1成以下'→0.06, 'k成'→k/10+0.05, '9成以上'→0.95, '半成'→0.05; 无法解析 → None。"""
    t = str(label or "").strip()
    if not t:
        return None
    if "半" in t:
        return 0.05
    try:
        k = float(t.replace("成", "").replace("以上", "").replace("以下", ""))
    except ValueError:
        return None
    if "以下" in t:
        return max(0.02, k / 10 - 0.04)
    if "以上" in t:
        return min(1.0, k / 10 + 0.05)
    return min(1.0, k / 10 + 0.05)


def normalize_live(hist: Dict[str, Dict[str, Any]], px: "PriceCtx") -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """实盘流水按"现金 + 持仓"记账重放, 给每条记录填上与模拟盘同口径的 weight_before/after(%)。

    买入金额 = 比例 × 总资产 (不超过现金); 卖出 = 清仓 (LIVE_CLEAR_FRAC 校准结论, 见常量注释)。
    用户决策 (2026-09-25): 没有原文(weight_label)的记录不猜 —— 零星的记缺口日丢弃, 成段缺失的只用之后的连续区间;
    起点持仓不明 (卖出未见买入的比例过高) 的组合整体不可用。带成交量的组合另用股数账本核对期末持仓。
    返回 (可用组合的 hist, 体检 {expert_id: {...}})。
    """
    out: Dict[str, Dict[str, Any]] = {}
    health: Dict[str, Any] = {}
    adj, fac, cidx = px.adj, px.fac, px.cidx
    for eid, h in hist.items():
        recs = sorted(h["records"], key=lambda r: (r.get("trade_time") or r["trade_date"]))
        n_all = len(recs)
        unl = [k for k, r in enumerate(recs) if not r.get("weight_label")]
        gap_days: List[str] = []
        if unl and len(unl) <= max(2, LIVE_ORPHAN_FRAC * n_all):
            # 零星无原文记录 = 重新抓取时平台已不再列出 (撤单/更正), 丢弃并把当天记为缺口日
            gap_days = sorted({recs[k]["trade_date"] for k in unl})
            tail = [r for r in recs if r.get("weight_label")]
        else:
            last_unl = max(unl, default=-1)    # 成段缺原文: 只用最后一条无原文记录之后的连续区间
            tail = recs[last_unl + 1:]
        info = {"name": h.get("name") or eid, "n_records": n_all, "n_labeled": n_all - len(unl),
                "first_date": recs[0]["trade_date"] if recs else None, "last_date": recs[-1]["trade_date"] if recs else None,
                "usable_from": tail[0]["trade_date"] if tail else None, "gap_days": gap_days, "unknown_sells": 0,
                "sells": 0, "phantom_end": 0, "n_hold_end": 0, "cash_end": None, "unknown_sell_ratio": None,
                "qty_check": None, "usable": False, "reason": ""}
        health[eid] = info
        if not tail:
            info["reason"] = "无原始仓位文字 (等重新抓取)"
            continue
        cash = 1.0
        val: Dict[int, float] = {}         # 股票 → 市值(以组合初始 1.0 计)
        mark: Dict[int, float] = {}        # 股票 → 最近一次估值用的复权价
        qty: Dict[int, float] = defaultdict(float)
        ever: set = set()                  # 记录里买过的股票 (卖出从没买过的 = 起点就持有, 持仓不明)
        # [2026-09-27 用户决策: 放宽] 起点持仓回填: 卖出了"记录里从没买过"的股票 = 数据起点之前就持有。
        # 把它们补进起点持仓: 起点市值 = 卖出比例 × (起点前收 / 卖出成交价) (按组合净值≈1 近似), 合计 ≤ 95%, 其余为现金;
        # 之后这笔卖出就是普通的清仓。以前这类组合 (卖出未见买入 > 20%) 整体不可用, 会漏掉数据起点前就建仓的高手。
        seeds = _seed_start_holdings(tail, px)
        seed_seen: set = set()
        if seeds:
            tot0 = sum(seeds.values())
            k = min(1.0, LIVE_SEED_MAX / tot0) if tot0 > 0 else 1.0
            t_s = px.day_of(tail[0]["trade_date"])
            for j, v0 in seeds.items():
                val[j] = v0 * k
                mark[j] = float(adj[max(0, (t_s or 0) - 1), j])
                ever.add(j)
            cash = 1.0 - sum(val.values())
            info["seeded"] = len(seeds)
            info["seeded_weight"] = round(1.0 - cash, 3)
            # [2026-10-19 修] 透出起点回填明细 {code: 起始权重(组合净值=1 计)}，
            # 供镜像回测 simulate_expert 初始化同样的起点持仓（否则这些仓位的卖出在镜像里无仓可卖、被静默丢弃）
            info["seed_map"] = {px.codes[j]: round(val[j], 4) for j in seeds}
        has_qty = all(r.get("volume") for r in tail)
        cur_t = None
        norm: List[Dict[str, Any]] = []

        def roll_to(t_new: int) -> None:   # 把持仓估值推进到 t_new-1 收盘
            for j in list(val):
                p = adj[t_new - 1, j] if t_new >= 1 else np.nan
                if np.isfinite(p) and p > 0 and mark.get(j, 0) > 0:
                    val[j] *= p / mark[j]
                    mark[j] = p
        for r in tail:
            t = px.day_of(r["trade_date"])
            c = r.get("stock_code")
            if t is None or c not in cidx:
                continue
            j = cidx[c]
            if cur_t is None or t != cur_t:
                roll_to(t)
                cur_t = t
            buy = r.get("action") in ("买入", "加仓", "申购")
            if has_qty:
                qty[j] = max(0.0, qty[j] + (1 if buy else -1) * float(r["volume"]))
            f = live_ratio(r.get("weight_label"))
            pr = float(r.get("price") or 0)
            pa = pr * fac[t, j] if pr > 0 and np.isfinite(fac[t, j]) else np.nan
            if j in val and np.isfinite(pa) and pa > 0 and mark.get(j, 0) > 0:   # 持仓按成交价估值
                val[j] *= 1 + float(np.clip(pa / mark[j] - 1, -px.limits[j], px.limits[j]))
                mark[j] = pa
            nav = cash + sum(val.values())
            wb = val.get(j, 0.0) / nav if nav > 0 else 0.0
            if f is None or nav <= 0:
                continue
            if buy:
                amt = min(f * nav, cash)
                if amt <= 0:
                    continue
                cash -= amt
                val[j] = val.get(j, 0.0) + amt
                ever.add(j)
                if np.isfinite(pa) and pa > 0:
                    mark[j] = pa
                elif adj[t, j] > 0:
                    mark[j] = adj[t, j]
            else:
                info["sells"] += 1
                if j in seeds and j not in seed_seen:
                    seed_seen.add(j)
                    info["unknown_sells"] += 1          # 起点持仓 (已回填) 的第一次卖出, 仍计数供体检显示
                if j not in val:
                    if j not in ever:
                        info["unknown_sells"] += 1
                    continue
                amt = f * nav
                if amt >= LIVE_CLEAR_FRAC * val[j]:
                    cash += val.pop(j)
                    mark.pop(j, None)
                else:
                    cash += amt
                    val[j] -= amt
            wa = val.get(j, 0.0) / nav
            norm.append({**r, "weight_before": round(wb * 100, 4), "weight_after": round(wa * 100, 4)})
        tot = cash + sum(val.values())
        info["n_hold_end"] = len(val)
        info["cash_end"] = round(cash / tot, 3) if tot > 0 else None
        if has_qty:                        # 股数账本核对: 期末"持有/已清仓"判断一致的比例
            codes = set(val) | {j for j, x in qty.items() if x > 0}
            agree = sum(1 for j in codes if (j in val) == (qty.get(j, 0) > 0))
            info["qty_check"] = round(agree / len(codes), 3) if codes else 1.0
            info["phantom_end"] = sum(1 for j in val if qty.get(j, 0) <= 0)
        us = info["unknown_sells"] / info["sells"] if info["sells"] else 0.0
        info["unknown_sell_ratio"] = round(us, 3)
        if not norm:
            info["reason"] = "可用区间内没有可重放的记录"
        else:
            info["usable"] = True
            if us > LIVE_MAX_UNKNOWN_SELL:
                info["start_approx"] = True
                info["reason"] = f"可用 (近似): 卖出未见买入 {us:.0%}, 起点持仓已回填 {info.get('seeded', 0)} 只"
            out[eid] = {"name": h.get("name") or eid, "records": norm}
    return out, health


LIVE_SEED_MAX = 0.95


def _seed_start_holdings(tail: List[Dict[str, Any]], px: "PriceCtx") -> Dict[int, float]:
    """起点持仓回填: {股票列: 起点市值 (组合初始 1.0 计)}。只看每只股票"第一次出现就是卖出"的记录。"""
    if not tail:
        return {}
    t_s = px.day_of(tail[0]["trade_date"])
    if t_s is None:
        return {}
    first: Dict[int, Dict[str, Any]] = {}
    for r in tail:
        c = r.get("stock_code")
        if c not in px.cidx:
            continue
        j = px.cidx[c]
        if j not in first:
            first[j] = r
    out: Dict[int, float] = {}
    for j, r in first.items():
        if r.get("action") in ("买入", "加仓", "申购"):
            continue
        f = live_ratio(r.get("weight_label"))
        t = px.day_of(r["trade_date"])
        pr = float(r.get("price") or 0)
        p0 = px.adj[max(0, t_s - 1), j]
        if not f or t is None or pr <= 0 or not (np.isfinite(p0) and p0 > 0):
            continue
        ps = pr * px.fac[t, j]
        if np.isfinite(ps) and ps > 0:
            out[j] = float(f * p0 / ps)
    return out


def qlib_inst(code: str) -> str:
    """裸代码 → qlib 目录名。按代码规则判交易所, **不能**按"哪个目录存在"去猜:
    000688/000001 这类代码在 sh 下是指数(科创50/上证指数), 在 sz 下才是股票。"""
    c = str(code)
    if c.startswith(("6", "5", "9")) and not c.startswith("92"):
        return "sh" + c
    if c.startswith(("4", "8", "92")):
        return "bj" + c
    return "sz" + c


def _prices(codes: List[str], start: str, end: str, fields=("adjclose", "close", "factor")):
    """价格面板 (迁移自 scripts/stock_graph/data.py 的 load_panel; 存储后端 = 本地 prices SQLite)。

    codes 顺序 = 传入顺序 (裸代码); 本地没有行情的代码返回全 NaN 列 (原版按 qlib 目录存在性
    过滤, 由调用方按需剔除)。adjclose/close/factor 走 prices.load_panel; open/high/low 直接读
    price_daily 表 (prices.load_panel 不返回这三个字段)。
    """
    fields = list(fields)
    if not codes:
        return prices.Panel(dates=[], codes=[], f={k: np.zeros((0, 0)) for k in fields})
    if all(f in ("adjclose", "close", "factor") for f in fields):
        return prices.load_panel(start, end, codes, fields=fields)
    import pandas as pd
    prices.ensure_schema()
    conn = prices.connect()
    ph = ",".join("?" for _ in codes)
    rows = conn.execute(
        f"SELECT code, date, {', '.join(fields)} FROM price_daily "
        f"WHERE code IN ({ph}) AND date >= ? AND date <= ? ORDER BY date",
        (*codes, start, end)).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    dates = sorted(df["date"].unique().tolist()) if not df.empty else []
    if not dates:
        return prices.Panel(dates=[], codes=list(codes), f={k: np.zeros((0, len(codes))) for k in fields})
    T = len(dates)
    didx = {d: i for i, d in enumerate(dates)}
    cidx = {c: j for j, c in enumerate(codes)}
    f: Dict[str, np.ndarray] = {}
    for k in fields:
        a = np.full((T, len(codes)), np.nan)
        for code, g in df.groupby("code"):
            j = cidx.get(code)
            if j is None:
                continue
            for d, v in zip(g["date"], g[k]):
                a[didx[d], j] = v if v is not None else np.nan
        f[k] = a
    return prices.Panel(dates=dates, codes=list(codes), f=f)


class PriceCtx:
    """重建用的价格上下文(停牌/缺失前值填充)。"""

    def __init__(self, codes: List[str], end: str, start: str = NAV_START):
        import pandas as pd
        # 本地缺行情的代码先补下载（限量防限流）；失败不阻塞，缺行情的列稍后按全 NaN 剔除
        try:
            done = prices.ensure_prices(codes, start, end, max_download=800)
            if done:
                logger.info(f"[pit_nav] 补齐行情 {len(done)} 只")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[pit_nav] 行情补齐失败（按本地已有数据继续）: {exc}")
        p = prices.load_panel(start, end, codes)
        self.dates = p.dates
        self.didx = {d: i for i, d in enumerate(p.dates)}
        # 原版 _prices 按 qlib 目录存在性过滤 codes; 新版 load_panel 对本地没有行情的代码返回全 NaN 列,
        # 这里把该列从 codes/cidx 剔除 (与原版过滤行为等价), adj/raw/fac/limits 同步裁剪
        adj_a = pd.DataFrame(p.f["adjclose"]).ffill().to_numpy(dtype=np.float64)
        raw_a = pd.DataFrame(p.f["close"]).ffill().to_numpy(dtype=np.float64)
        fac_a = pd.DataFrame(p.f["factor"]).ffill().to_numpy(dtype=np.float64)
        keep = [j for j in range(len(p.codes))
                if np.isfinite(adj_a[:, j]).any() or np.isfinite(raw_a[:, j]).any()]
        self.codes = [p.codes[j] for j in keep]
        self.cidx = {c: k for k, c in enumerate(self.codes)}
        self.adj = adj_a[:, keep]
        self.raw = raw_a[:, keep]
        self.fac = fac_a[:, keep]
        self.n_factor_fixes = self._fix_factor_glitches()
        # 单日涨跌幅上限(复权收益截断用): 北交所30%, 创业板/科创板20%, 其余10%; 各加1%余量(新股上市首日等例外按上限截断)
        self.limits = np.array([0.31 if c.startswith(("4", "8", "92")) else
                                (0.21 if c.startswith(("300", "301", "688", "689")) else 0.11) for c in self.codes])

    def _fix_factor_glitches(self) -> int:
        """qlib factor 偶有"复权因子跳了、原始价没动"的脏数据(实测 000908/600165 等 2026-07-06
        复权价单日 +30~50%)。复权日收益超出涨跌停、而原始价日收益在涨跌停内时, 判为因子错误:
        当日复权收益改用原始价收益, 并把之后的复权价整体平移, 同时修正 factor 保持 adj = raw×factor。"""
        adj, raw = self.adj, self.raw
        lim = np.array([0.31 if c.startswith(("4", "8", "92")) else 0.21 for c in self.codes])
        n = 0
        with np.errstate(divide="ignore", invalid="ignore"):
            for t in range(1, adj.shape[0]):
                ra = adj[t] / adj[t - 1] - 1
                rr = raw[t] / raw[t - 1] - 1
                bad = np.isfinite(ra) & np.isfinite(rr) & (np.abs(ra) > lim) & (np.abs(rr) <= lim)
                for j in np.where(bad)[0]:
                    k = (1 + rr[j]) / (1 + ra[j])        # 把 t 及之后的复权价乘回去
                    adj[t:, j] *= k
                    self.fac[t:, j] *= k
                    n += 1
        return n

    def day_of(self, trade_date: str) -> Optional[int]:
        t = self.didx.get(trade_date)
        if t is None:                                  # 非交易日成交(极少), 归到下一个交易日
            import bisect
            i = bisect.bisect_right(self.dates, trade_date)
            t = i if i < len(self.dates) else None
        return t


def replay_expert(recs: List[Dict[str, Any]], px: PriceCtx, keep_holdings: bool = False,
                  errs: Optional[List[float]] = None) -> Tuple[np.ndarray, Optional[List[Dict[int, float]]]]:
    """单个组合逐日重建。返回 (nav[T] 首笔记录前为 NaN, holdings[T] 每日收盘后仓位 {股票列: 权重} 或 None)。"""
    T = len(px.dates)
    nav = np.full(T, np.nan)
    hold: Optional[List[Dict[int, float]]] = [dict() for _ in range(T)] if keep_holdings else None
    by_day: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for r in recs:
        t = px.day_of(r["trade_date"])
        if t is not None and r["stock_code"] in px.cidx:
            by_day[t].append(r)
    if not by_day:
        return nav, hold
    adj, raw, fac, cidx = px.adj, px.raw, px.fac, px.cidx
    lim = px.limits
    t0 = min(by_day)
    # 窗口开始前已持有的票: 第一笔记录的调仓前仓位>0, 视为从 t0 起就持有(按价格回推初始仓位)
    w: Dict[int, float] = {}
    seen = set()
    for t in sorted(by_day):
        for r in by_day[t]:
            j = cidx[r["stock_code"]]
            if j in seen:
                continue
            seen.add(j)
            wb = (r.get("weight_before") or 0) / 100
            if wb > 0.005 and np.isfinite(adj[t0, j]) and np.isfinite(adj[t, j]) and adj[t, j] > 0:
                w[j] = wb * adj[t0, j] / adj[t, j]
    v = 1.0
    nav[t0] = v
    if hold is not None:
        hold[t0] = dict(w)
    for t in range(t0 + 1, T):
        traded: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        for r in by_day.get(t, []):
            traded[cidx[r["stock_code"]]].append(r)
        R = 0.0
        new_w: Dict[int, float] = {}
        for j, wj in w.items():
            if j in traded:
                continue
            rj = adj[t, j] / adj[t - 1, j] - 1 if adj[t - 1, j] > 0 else 0.0
            rj = float(np.clip(rj, -lim[j], lim[j])) if np.isfinite(rj) else 0.0
            R += wj * rj
            new_w[j] = wj * (1 + rj)
        for j, rs in traded.items():
            first, last = rs[0], rs[-1]
            wb = (first.get("weight_before") or 0) / 100
            if errs is not None and j in w and wb > 0:
                errs.append(abs(w[j] - wb))
            pf, pl = first.get("price") or 0, last.get("price") or 0
            # 成交价与行情对不上(单位/复权口径异常)时, 单笔贡献截到涨跌停幅度内, 不让一条脏记录炸掉净值
            if pf > 0 and adj[t - 1, j] > 0:
                R += wb * float(np.clip(pf * fac[t, j] / adj[t - 1, j] - 1, -lim[j], lim[j]))
            wa = min(1.0, (last.get("weight_after") or 0) / 100)
            if pl > 0 and np.isfinite(raw[t, j]):
                gain = float(np.clip(raw[t, j] / pl - 1, -lim[j], lim[j]))
                R += wa * gain
                new_w[j] = wa * (1 + gain)
            else:
                new_w[j] = wa
        R = float(np.clip(R, -0.5, 0.5))                 # 防单日异常(数据错位)炸掉净值
        v *= 1 + R
        nav[t] = v
        w = {j: x / (1 + R) for j, x in new_w.items() if x / (1 + R) > MIN_W}
        # 模拟组合不能加杠杆: 总仓位 >100% 说明漂移出来的旧仓位和站点报的新仓位对不上,
        # 以当天有成交记录的(站点报的, 权威)为准, 按比例压缩其余仓位
        tot = sum(w.values())
        if tot > 1.0:
            fixed = sum(v for j, v in w.items() if j in traded)
            rest = tot - fixed
            if fixed < 1.0 and rest > 0:
                k = (1.0 - fixed) / rest
                w = {j: (v if j in traded else v * k) for j, v in w.items()}
            else:
                w = {j: v / tot for j, v in w.items()}
        if hold is not None:
            hold[t] = dict(w)
    return nav, hold


def rebuild(histories: Dict[str, Dict[str, Any]], end: str) -> Dict[str, Any]:
    """返回 {dates, experts, nav [T,E], diag}。nav 在组合首笔记录之前为 NaN。"""
    codes = sorted({r["stock_code"] for h in histories.values() for r in h["records"]})
    px = PriceCtx(codes, end)
    experts = sorted(histories)
    nav = np.full((len(px.dates), len(experts)), np.nan, dtype=np.float64)
    errs: List[float] = []
    for ei, eid in enumerate(experts):
        nav[:, ei], _ = replay_expert(histories[eid]["records"], px, errs=errs)
        if (ei + 1) % 500 == 0:
            logger.info(f"[pit_nav] {ei + 1}/{len(experts)}")
    e = np.array(errs) if errs else np.array([np.nan])
    diag = {"n_experts": len(experts), "n_trade_checks": len(errs),
            "weight_err_median_pp": float(np.nanmedian(e) * 100), "weight_err_p90_pp": float(np.nanpercentile(e, 90) * 100)}
    logger.info(f"[pit_nav] 重建完成 {diag}")
    return {"dates": px.dates, "experts": experts, "nav": nav, "diag": diag}


def trailing_return(nav: np.ndarray, days: int = TRAIL_DAYS) -> np.ndarray:
    """ret[t] = nav[t]/nav[t-days]-1; 窗口起点前组合还没开始(NaN)则为 NaN。"""
    out = np.full_like(nav, np.nan)
    out[days:] = nav[days:] / nav[:-days] - 1
    return out


def build(end: str) -> Dict[str, Any]:
    h = load_histories()
    res = rebuild(h, end)
    np.savez(OUT_FP, dates=np.array(res["dates"]), experts=np.array(res["experts"]), nav=res["nav"],
             names=np.array([h[e]["name"] for e in res["experts"]]),
             diag=np.array(json.dumps(res["diag"])))
    return res


OUT_FP_LIVE = DATA_DIR / "pit_nav_live.npz"


def build_live(end: str) -> Dict[str, Any]:
    """[2026-09-29 A] 实盘组合逐日净值 (与 follow_backtest_v2.Ctx(env='live') 同口径: rtV3 仓位原文 → 记账重放 → 逐日重建),
    供共振 / Top10 给实盘高手算可信度 (实盘接口没有平台收益率与粉丝数)。无原文/起点不明的组合不在内。"""
    h = load_histories("live")
    codes = sorted({r["stock_code"] for x in h.values() for r in x["records"]})
    px = PriceCtx(codes, end, start=NAV_START_LIVE)
    h, health = normalize_live(h, px)
    experts = sorted(h)
    nav = np.full((len(px.dates), len(experts)), np.nan, dtype=np.float64)
    for i, e in enumerate(experts):
        nav[:, i], _ = replay_expert(h[e]["records"], px)
    diag = {"n_experts": len(experts), "n_unusable": sum(1 for v in health.values() if not v.get("usable", True))}
    np.savez(OUT_FP_LIVE, dates=np.array(px.dates), experts=np.array(experts), nav=nav,
             names=np.array([h[e].get("name") or e for e in experts]), diag=np.array(json.dumps(diag)))
    logger.info(f"[pit_nav] 实盘净值重建完成 {diag}")
    return {"dates": px.dates, "experts": experts, "nav": nav, "diag": diag}


def load(env: str = "sim") -> Optional[Dict[str, Any]]:
    fp = OUT_FP_LIVE if env == "live" else OUT_FP
    if not fp.exists():
        return None
    z = np.load(fp, allow_pickle=False)
    return {"dates": z["dates"].tolist(), "experts": z["experts"].tolist(), "nav": z["nav"],
            "names": z["names"].tolist(), "diag": json.loads(str(z["diag"]))}
