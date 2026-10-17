"""把成交明细渲染成直观的 PNG 看板（买入 / 卖出分表，按个股合并，保留操作者名单）。

只在需要输出 png 时才会被 import，matplotlib 因此属于可选依赖。
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .models import TradeRecord
from .utils import to_float

# ── 主题 ─────────────────────────────────────────────────
DPI = 110
WIDTH = 1640
ROW_H = 36
HEAD_H = 40
PAD = 44

COLOR_BUY = "#C0392B"      # 买入：红（A 股习惯，涨红跌绿）
COLOR_SELL = "#1E8449"     # 卖出：绿
COLOR_TEXT = "#1F2733"
COLOR_MUTED = "#7A869A"
COLOR_GRID = "#E3E7ED"
COLOR_ROW_ALT = "#F6F8FA"
COLOR_HEAD_TEXT = "#FFFFFF"

FONT_CANDIDATES = (
    "PingFang SC", "PingFang HK", "Heiti SC", "STHeiti", "Heiti TC",
    "Songti SC", "Arial Unicode MS", "Hiragino Sans GB", "Microsoft YaHei",
)

#: 渠道显示名
CHANNEL_LABEL = {
    "东财高手操作": "东财社区",
    "东财实盘组合调仓": "东财实盘",
    "雪球组合调仓": "雪球",
}

_CORP_SUFFIX = re.compile(r"(股份有限公司|有限责任公司|有限公司)")
_BRANCH_SUFFIX = re.compile(r"(证券营业部|营业部)$")


def _font():
    """挑选本机可用的中文字体。"""
    import matplotlib
    from matplotlib import font_manager

    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in FONT_CANDIDATES:
        if name in available:
            return font_manager.FontProperties(family=name)
    matplotlib.rcParams["axes.unicode_minus"] = False
    return font_manager.FontProperties()


def _pick_backend_defaults(font_prop) -> None:
    import matplotlib

    matplotlib.rcParams["font.family"] = font_prop.get_family()
    matplotlib.rcParams["axes.unicode_minus"] = False


def shorten_actor(name: str) -> str:
    """压缩过长的席位/组合名：`国信证券股份有限公司北京分公司` → `国信证券·北京分公司`。"""
    text = str(name or "").strip()
    if not text:
        return "—"
    if len(text) <= 12:
        return text
    match = re.match(r"^(.*?(?:证券|基金|投资|银行))((?:股份有限公司|有限责任公司|有限公司))(.*)$", text)
    if match:
        corp, _, branch = match.groups()
        branch = _BRANCH_SUFFIX.sub("", branch)
        text = f"{corp}·{branch}" if branch else corp
    else:
        text = _CORP_SUFFIX.sub("", text)
    text = _BRANCH_SUFFIX.sub("", text)
    return text or "—"


def _money(value: float | None) -> str:
    if not value:
        return "—"
    amount = abs(value)
    if amount >= 1e8:
        return f"{value / 1e8:.2f}亿"
    if amount >= 1e4:
        return f"{value / 1e4:.0f}万"
    return f"{value:.0f}"


def _clip(text: str, max_px: float, fs: float) -> str:
    """按渲染宽度粗略截断（中文按全宽、ASCII 按半宽估算）。"""
    budget = max_px
    out = []
    for ch in text:
        cost = (fs if ord(ch) > 0x2E80 else fs * 0.55)
        if budget - cost < 0:
            out.append("…")
            break
        budget -= cost
        out.append(ch)
    return "".join(out)


# ── 聚合 ─────────────────────────────────────────────────
def _aggregate(records: list[TradeRecord]) -> list[dict]:
    """按股票合并同一批成交记录。"""
    buckets: dict[str, list[TradeRecord]] = {}
    for r in records:
        if r.stock_code:
            buckets.setdefault(r.stock_code, []).append(r)

    rows: list[dict] = []
    for code, items in buckets.items():
        actors: list[str] = []
        for it in items:
            name = it.expert_name or it.owner or it.expert_id or "—"
            if name not in actors:
                actors.append(name)
        prices = [to_float(i.price) for i in items if to_float(i.price)]
        amount = sum(to_float(i.amount) or 0.0 for i in items) or None
        rows.append(
            {
                "code": code,
                "name": next((i.stock_name for i in items if i.stock_name), ""),
                "actors": actors,
                "records": len(items),
                "experts": len(actors),
                "avg_price": round(sum(prices) / len(prices), 2) if prices else None,
                "amount": amount,
                "channels": sorted({CHANNEL_LABEL.get(i.channel, i.channel) for i in items}),
            }
        )
    rows.sort(key=lambda r: (r["amount"] or 0, r["experts"], r["records"]), reverse=True)
    return rows


def _actor_text(actors: list[str], limit: int = 3) -> str:
    names = [shorten_actor(a) for a in actors[:limit]]
    text = "、".join(names)
    if len(actors) > limit:
        text += f" 等{len(actors)}位"
    return text


# ── 渲染 ─────────────────────────────────────────────────
def _columns() -> list[tuple[str, int, int, str]]:
    """列定义：(标题, x, 宽度, 对齐)。"""
    return [
        ("#", PAD, 56, "left"),
        ("股票", PAD + 62, 250, "left"),
        ("均价", PAD + 312, 100, "right"),
        ("操作者", PAD + 428, 88, "right"),
        ("笔数", PAD + 532, 78, "right"),
        ("金额", PAD + 626, 140, "right"),
        ("渠道", PAD + 782, 168, "left"),
        ("主要操作者", PAD + 950, WIDTH - PAD - 950, "left"),
    ]


def render_side_png(
    records: list[TradeRecord],
    action_label: str,
    trade_date: str,
    path: Path,
    top: int = 30,
) -> Path | None:
    """渲染单侧（买入或卖出）的 PNG 看板；无数据时返回 None。"""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    rows = _aggregate(records)
    if not rows:
        return None

    shown = rows[:top]
    height = 254 + len(shown) * ROW_H + 48

    font = _font()
    _pick_backend_defaults(font)
    accent = COLOR_BUY if action_label == "买入" else COLOR_SELL

    fig = plt.figure(figsize=(WIDTH / DPI, height / DPI), dpi=DPI)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, WIDTH)
    ax.set_ylim(height, 0)
    ax.axis("off")
    fig.patch.set_facecolor("white")

    # 标题
    ax.text(PAD, 66, f"社区高手 · {action_label}榜", fontsize=32, color=COLOR_TEXT,
            va="center", fontproperties=font, fontweight="bold")
    ax.text(PAD, 104, f"交易日 {trade_date}　|　数据来源：东方财富（社区高手操作）／雪球社区组合",
            fontsize=15, color=COLOR_MUTED, va="center", fontproperties=font)

    # 摘要卡片
    total_amount = sum(r["amount"] or 0 for r in rows)
    cards = [
        (f"{len(rows)}", "涉及个股"),
        (f"{sum(r['experts'] for r in rows)}", "操作者人次"),
        (f"{sum(r['records'] for r in rows)}", f"{action_label}笔数"),
        (_money(total_amount) if total_amount else "—", "合计金额"),
    ]
    card_w = (WIDTH - PAD * 2 - 3 * 14) / 4
    for idx, (value, label) in enumerate(cards):
        x = PAD + idx * (card_w + 14)
        ax.add_patch(Rectangle((x, 126), card_w, 74, facecolor=COLOR_ROW_ALT,
                               edgecolor=COLOR_GRID, linewidth=1, zorder=0))
        ax.add_patch(Rectangle((x, 126), 4, 74, facecolor=accent, zorder=1))
        ax.text(x + 20, 152, value, fontsize=25, color=accent, va="center",
                fontproperties=font, fontweight="bold", zorder=2)
        ax.text(x + 20, 181, label, fontsize=13, color=COLOR_MUTED, va="center",
                fontproperties=font, zorder=2)

    # 表头
    ax.add_patch(Rectangle((PAD, 214), WIDTH - PAD * 2, HEAD_H, facecolor=accent, zorder=0))
    for title, x, width, align in _columns():
        ax.text(x if align == "left" else x + width - 12, 214 + HEAD_H / 2, title,
                fontsize=15, color=COLOR_HEAD_TEXT, va="center", ha=align,
                fontproperties=font, fontweight="bold", zorder=2)

    # 数据行
    for r_idx, row in enumerate(shown):
        y = 214 + HEAD_H + r_idx * ROW_H
        if r_idx % 2 == 1:
            ax.add_patch(Rectangle((PAD, y), WIDTH - PAD * 2, ROW_H,
                                   facecolor=COLOR_ROW_ALT, zorder=0))
        ax.add_patch(Rectangle((PAD, y), WIDTH - PAD * 2, ROW_H, facecolor="none",
                               edgecolor=COLOR_GRID, linewidth=0.8, zorder=1))

        stock = f"{row['code']} {row['name']}".strip()
        actors_text = _actor_text(row["actors"])
        values = [
            str(r_idx + 1),
            stock,
            f"{row['avg_price']:.2f}" if row["avg_price"] else "—",
            str(row["experts"]),
            str(row["records"]),
            _money(row["amount"]),
            " / ".join(row["channels"]),
            actors_text,
        ]
        for (_, x, width, align), value in zip(_columns(), values):
            ha = align
            tx = x + 10 if align == "left" else x + width - 12
            if ha == "left":
                value = _clip(value, width - 18, 15.5)
            ax.text(tx, y + ROW_H / 2, value, fontsize=15.5, color=COLOR_TEXT,
                    va="center", ha=ha, fontproperties=font, zorder=2)

    # 脚注
    foot_y = height - 26
    omitted = len(rows) - len(shown)
    tail = f"，其余 {omitted} 只未展示" if omitted > 0 else ""
    ax.text(PAD, foot_y, f"共 {len(rows)} 只个股{tail}；完整明细见同目录 CSV / Excel。"
                         f"本图仅为公开社区数据汇总，不构成投资建议。",
            fontsize=13, color=COLOR_MUTED, va="center", fontproperties=font)
    stamp = datetime.now().strftime("生成于 %Y-%m-%d %H:%M")
    ax.text(WIDTH - PAD, foot_y, stamp, fontsize=13, color=COLOR_MUTED, va="center",
            ha="right", fontproperties=font)

    fig.savefig(path, dpi=DPI, facecolor="white", bbox_inches="tight", pad_inches=0.2)
    plt.close(fig)
    return path


def render_trade_pngs(
    trades: list[TradeRecord],
    trade_date: str,
    out_dir: Path,
    top: int = 30,
) -> list[Path]:
    """输出买入 / 卖出两张看板图。"""
    from loguru import logger

    from .models import BUY_ACTIONS

    buy_records = [t for t in trades if t.action in BUY_ACTIONS]
    sell_records = [t for t in trades if t.action not in BUY_ACTIONS]
    written: list[Path] = []
    for label, records, filename in (
        ("买入", buy_records, "report_buy.png"),
        ("卖出", sell_records, "report_sell.png"),
    ):
        saved = render_side_png(records, label, trade_date, out_dir / filename, top=top)
        if saved:
            written.append(saved)
            logger.info(f"已写入 {saved}")
        else:
            logger.warning(f"[图片] 无{label}数据，跳过 {filename}")
    return written


# ── 高手盈利 Top 榜 ─────────────────────────────────────
def _columns_expert() -> list[tuple[str, int, int, str]]:
    """高手盈利榜列定义：(标题, x, 宽度, 对齐)。"""
    return [
        ("#", PAD, 50, "left"),
        ("高手", PAD + 56, 300, "left"),
        ("收益率", PAD + 356, 130, "right"),
        ("关注", PAD + 486, 100, "right"),
        ("买入股数", PAD + 586, 110, "right"),
        ("买入股票", PAD + 696, WIDTH - PAD - 696, "left"),
    ]


def render_expert_png(
    expert_buys: list[dict],
    trade_date: str,
    path: Path,
    top: int = 30,
) -> Path | None:
    """渲染『高手盈利 Top 榜』：按收益率（盈利）降序，列出其当日买入股票。

    expert_buys 即 pipeline.build_expert_buys 的输出（已按 return_rate 降序、仅含当日有买入者）。
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    if not expert_buys:
        return None

    shown = expert_buys[:top]
    height = 254 + len(shown) * ROW_H + 48

    font = _font()
    _pick_backend_defaults(font)

    fig = plt.figure(figsize=(WIDTH / DPI, height / DPI), dpi=DPI)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, WIDTH)
    ax.set_ylim(height, 0)
    ax.axis("off")
    fig.patch.set_facecolor("white")

    # 标题
    ax.text(PAD, 66, "社区高手 · 盈利 Top 榜", fontsize=32, color=COLOR_TEXT,
            va="center", fontproperties=font, fontweight="bold")
    ax.text(PAD, 104, f"交易日 {trade_date}　|　按收益率（页面公开盈利）降序，展开其当日买入股票",
            fontsize=15, color=COLOR_MUTED, va="center", fontproperties=font)

    # 摘要卡片
    max_rate = shown[0].get("return_rate") or 0
    all_stocks: set[str] = set()
    for eb in shown:
        for s in eb.get("stocks", []):
            if s.get("stock_code"):
                all_stocks.add(s["stock_code"])
    total_buys = sum(eb.get("buy_count", 0) for eb in shown)
    cards = [
        (f"{len(shown)}", "盈利高手(Top)"),
        (f"{max_rate:.2f}%", "最高收益率"),
        (f"{len(all_stocks)}", "买入个股"),
        (f"{total_buys}", "买入笔数"),
    ]
    card_w = (WIDTH - PAD * 2 - 3 * 14) / 4
    for idx, (value, label) in enumerate(cards):
        x = PAD + idx * (card_w + 14)
        ax.add_patch(Rectangle((x, 126), card_w, 74, facecolor=COLOR_ROW_ALT,
                               edgecolor=COLOR_GRID, linewidth=1, zorder=0))
        ax.add_patch(Rectangle((x, 126), 4, 74, facecolor=COLOR_BUY, zorder=1))
        ax.text(x + 20, 152, value, fontsize=25, color=COLOR_BUY, va="center",
                fontproperties=font, fontweight="bold", zorder=2)
        ax.text(x + 20, 181, label, fontsize=13, color=COLOR_MUTED, va="center",
                fontproperties=font, zorder=2)

    # 表头
    ax.add_patch(Rectangle((PAD, 214), WIDTH - PAD * 2, HEAD_H, facecolor=COLOR_BUY, zorder=0))
    for title, x, width, align in _columns_expert():
        ax.text(x if align == "left" else x + width - 12, 214 + HEAD_H / 2, title,
                fontsize=15, color=COLOR_HEAD_TEXT, va="center", ha=align,
                fontproperties=font, fontweight="bold", zorder=2)

    # 数据行
    for r_idx, eb in enumerate(shown):
        y = 214 + HEAD_H + r_idx * ROW_H
        if r_idx % 2 == 1:
            ax.add_patch(Rectangle((PAD, y), WIDTH - PAD * 2, ROW_H,
                                   facecolor=COLOR_ROW_ALT, zorder=0))
        ax.add_patch(Rectangle((PAD, y), WIDTH - PAD * 2, ROW_H, facecolor="none",
                               edgecolor=COLOR_GRID, linewidth=0.8, zorder=1))

        stocks_text = "、".join(
            f"{s.get('stock_name', '')}({s.get('stock_code', '')})" for s in eb.get("stocks", [])
        )
        values = [
            str(r_idx + 1),
            eb.get("expert_name") or eb.get("owner") or "—",
            f"{eb.get('return_rate') or 0:.2f}%",
            str(eb.get("followers") or 0),
            str(eb.get("buy_count") or 0),
            stocks_text,
        ]
        for (_, x, width, align), value in zip(_columns_expert(), values):
            ha = align
            tx = x + 10 if align == "left" else x + width - 12
            if ha == "left":
                value = _clip(value, width - 18, 15.5)
            ax.text(tx, y + ROW_H / 2, value, fontsize=15.5, color=COLOR_TEXT,
                    va="center", ha=ha, fontproperties=font, zorder=2)

    # 脚注
    foot_y = height - 26
    omitted = len(expert_buys) - len(shown)
    tail = f"，其余 {omitted} 位未展示" if omitted > 0 else ""
    ax.text(PAD, foot_y, f"共 {len(expert_buys)} 位盈利高手{tail}；完整明细见同目录 expert_buys.csv / .json。"
                         f"本图仅为公开社区数据汇总，不构成投资建议。",
            fontsize=13, color=COLOR_MUTED, va="center", fontproperties=font)
    stamp = datetime.now().strftime("生成于 %Y-%m-%d %H:%M")
    ax.text(WIDTH - PAD, foot_y, stamp, fontsize=13, color=COLOR_MUTED, va="center",
            ha="right", fontproperties=font)

    fig.savefig(path, dpi=DPI, facecolor="white", bbox_inches="tight", pad_inches=0.2)
    plt.close(fig)
    return path


def render_dashboard_pngs(
    trades: list[TradeRecord],
    experts: list,
    expert_buys: list[dict],
    trade_date: str,
    out_dir: Path,
    top: int = 30,
) -> list[Path]:
    """输出完整看板：高手盈利 Top 榜 + 个股买入 / 卖出榜。"""
    from loguru import logger

    written: list[Path] = []
    saved = render_expert_png(expert_buys, trade_date, out_dir / "report_experts.png", top=top)
    if saved:
        written.append(saved.name)
        logger.info(f"已写入 {saved}")
    else:
        logger.warning("[图片] 无盈利高手数据，跳过 report_experts.png")
    written += [
        p.name for p in render_trade_pngs(trades, trade_date, out_dir, top=top)
    ]
    return written
