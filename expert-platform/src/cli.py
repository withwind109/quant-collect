"""命令行入口。"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import click

from paths import CRAWL_DATA_DIR, DATA_DIR
from pipeline import build_sources, run
from settings import DEFAULT_CONFIG, load_settings, sqlite_path

SOURCE_CHOICES = ["xueqiu", "eastmoney", "tonghuashun", "alipay"]
DEFAULT_SOURCES = "eastmoney,tonghuashun"


def _parse_date(value: str) -> date | None:
    """解析 --date；auto/空 = 自动推断交易日（各源候选日期投票）。"""
    if value in {"auto", "today", ""}:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


@click.group()
def cli() -> None:
    """社区高手 & 高手跟随策略平台 · 命令行工具。"""


@cli.command()
@click.option("--sources", default=DEFAULT_SOURCES, show_default=True, help=f"逗号分隔：{','.join(SOURCE_CHOICES)}")
@click.option("--date", "date_str", default="auto", show_default=True,
              help="交易日 YYYY-MM-DD；auto=自动推断最近有数据的交易日（推荐，避免周末/节假日抓空）")
@click.option("--top", default=50, show_default=True, help="每个榜单抓取的高手/席位数量上限")
@click.option("--out", "out_dir", default=None, help="输出目录，默认取配置 output.dir")
@click.option("--format", "fmt", default=None, help="输出格式：csv,json,xlsx,png，默认取配置 output.formats")
@click.option("--config", "config_path", default=None, help="配置文件路径")
def crawl(sources: str, date_str: str, top: int, out_dir: str | None, fmt: str | None, config_path: str | None) -> None:
    """抓取指定交易日社区高手的买入明细。"""
    settings = load_settings(Path(config_path) if config_path else DEFAULT_CONFIG)
    names = [s.strip() for s in sources.split(",") if s.strip()]
    unknown = [n for n in names if n not in SOURCE_CHOICES]
    if unknown:
        raise click.UsageError(f"未知数据源：{unknown}，可选 {SOURCE_CHOICES}")

    target = _parse_date(date_str)
    formats = tuple(f.strip() for f in (fmt or ",".join(settings["output"]["formats"])).split(",") if f.strip())
    out_path = Path(out_dir) if out_dir else CRAWL_DATA_DIR

    summary = run(settings, names, target, top=top, out_dir=out_path, formats=formats,
                  db_path=sqlite_path(settings))
    click.echo("── 采集摘要 ──")
    for key, value in summary.items():
        click.echo(f"  {key}: {value}")


@cli.command()
@click.option("--config", "config_path", default=None, help="配置文件路径")
def check(config_path: str | None) -> None:
    """自检：凭据状态与各源接口连通性。"""
    settings = load_settings(Path(config_path) if config_path else DEFAULT_CONFIG)
    click.echo("── 凭据状态 ──")
    click.echo(f"  东财登录 Cookie: {'已配置' if settings['eastmoney'].get('cookie') else '未配置（匿名抓取, 可能被 WAF 拦截）'}")
    click.echo(f"  雪球登录 Cookie: {'已配置' if settings['xueqiu'].get('cookie') else '未配置（调仓明细不可用）'}")
    click.echo(
        "  同花顺登录: "
        + ("已配置" if settings["tonghuashun"].get("cookie") else "未配置（榜单可试匿名，交易明细需登录）")
    )
    click.echo(
        "  支付宝实盘端点: "
        + ("已配置" if settings["alipay"].get("rank_url") else "未配置（需用户提供抓包端点，详见 alipay 源注释）")
    )

    click.echo("── 接口连通性 ──")
    clients = build_sources(settings)
    for name in SOURCE_CHOICES:
        ok, msg = clients[name].healthcheck()
        click.echo(f"  [{'OK' if ok else 'FAIL'}] {name}: {msg}")


@cli.command()
@click.option("--zjzh", required=True, help="模拟盘组合号（1000… 长号）")
@click.option("--since", default="", help="只回补该日（含）之后的记录 YYYY-MM-DD；空=全历史")
@click.option("--out", "out_dir", default=None, help="输出目录，默认取配置 output.dir")
@click.option("--config", "config_path", default=None, help="配置文件路径")
def history(zjzh: str, since: str, out_dir: str | None, config_path: str | None) -> None:
    """回补单个高手的全部历史调仓（写入 data/history/<zjzh>.json）。"""
    from history import run_history

    settings = load_settings(Path(config_path) if config_path else DEFAULT_CONFIG)
    out_path = Path(out_dir) if out_dir else DATA_DIR
    summary = run_history(settings, zjzh.strip(), since=since.strip(), out_dir=out_path)
    click.echo("── 历史回补摘要 ──")
    for key, value in summary.items():
        click.echo(f"  {key}: {value}")


@cli.command("bulk-history")
@click.option("--since", default="2025-12-01", show_default=True, help="回补起点 YYYY-MM-DD")
@click.option("--limit", type=int, default=0, help="只跑前 N 个(调试)")
@click.option("--max-fail", type=int, default=25, help="连续失败这么多次就停(疑似限流)")
@click.option("--incremental", is_flag=True, help="已有文件只补最近几天")
@click.option("--config", "config_path", default=None, help="配置文件路径")
def bulk_history(since: str, limit: int, max_fail: int, incremental: bool, config_path: str | None) -> None:
    """批量回补高手组合调仓历史（data/history_bulk/，供跟随策略回测使用）。"""
    from bulk_history import main as _main

    _main(since=since, limit=limit, max_fail=max_fail, incremental=incremental,
          config_path=Path(config_path) if config_path else None)


@cli.command("prices")
@click.option("--index", "index_code", default="", help="下载指数行情，如 000300.SH")
@click.option("--codes", default="", help="下载指定股票代码（逗号分隔）")
@click.option("--start", default="20150101", show_default=True, help="下载起点 YYYYMMDD")
@click.option("--stats", "show_stats", is_flag=True, help="查看库统计")
def prices(index_code: str, codes: str, start: str, show_stats: bool) -> None:
    """行情价格库管理（data/prices.db，回测/净值曲线数据基础）。"""
    from strategy import prices as P

    P.ensure_calendar()
    click.echo(f"[prices] 交易日历就绪 {P.price_db_path()}")
    if index_code:
        n = P.download_index(index_code, start=start)
        click.echo(f"[prices] 指数 {index_code} 写入 {n} 行")
    for code in [c.strip() for c in codes.split(",") if c.strip()]:
        n = P.download_stock(code, start=start)
        click.echo(f"[prices] 股票 {code} 写入 {n} 行")
    st = P.stats()
    click.echo("── 价格库统计 ──")
    for key, value in st.items():
        click.echo(f"  {key}: {value}")


@cli.command("db-sync")
@click.option("--all", "do_all", is_flag=True, help="补齐 data/* 下所有历史交易日的爬取数据（幂等）")
@click.option("--date", "date_str", default="", help="仅同步指定交易日 YYYY-MM-DD（重跑也幂等）")
@click.option("--ensure-schema", "ensure_schema_only", is_flag=True, help="仅建表后退出，不写数据")
@click.option("--data-dir", "data_dir", default=None, help="数据根目录，默认取配置 output.dir")
@click.option("--config", "config_path", default=None, help="配置文件路径")
def db_sync(do_all: bool, date_str: str, ensure_schema_only: bool, data_dir: str | None, config_path: str | None) -> None:
    """将已爬取的 JSON 结果入库到本地 SQLite（按交易日落库，幂等）。"""
    from core import db

    settings = load_settings(Path(config_path) if config_path else DEFAULT_CONFIG)
    db_fp = sqlite_path(settings)
    data_root = Path(data_dir) if data_dir else CRAWL_DATA_DIR

    if ensure_schema_only:
        ok = db.ensure_schema(db_fp)
        click.echo(f"建表: {'OK' if ok else 'FAIL'}")
        return

    if not data_root.is_dir():
        click.echo(f"❌ 数据目录不存在: {data_root}")
        return

    if date_str:
        day_dir = data_root / date_str
        if not (day_dir / "expert_buys.json").exists():
            click.echo(f"❌ {date_str} 无爬取数据（缺 expert_buys.json）")
            return
        if not db.ensure_schema(db_fp):
            click.echo("❌ 建表失败，终止")
            return
        res = db.write_day(date_str, day_dir, db_path=db_fp)
        click.echo(f"── {date_str} 入库 ──")
        for k, v in res.items():
            click.echo(f"  {k}: {v}")
        return

    if do_all:
        summary = db.backfill_all(data_root, db_path=db_fp)
        click.echo("── 补齐完成 ──")
        click.echo(f"  synced : {summary.get('synced')}")
        click.echo(f"  skipped: {summary.get('skipped')}")
        click.echo(f"  totals : {summary.get('totals')}")
        return

    click.echo("请指定 --all（补齐所有历史）或 --date YYYY-MM-DD（单日同步），或 --ensure-schema（仅建表）。")


@cli.group()
def strategy() -> None:
    """高手跟随策略：策略文件 / 回测 / 数据体检。"""


@strategy.command("list")
def strategy_list() -> None:
    """列出策略文件与回测配置。"""
    from strategy import config as C

    click.echo("── 策略文件 ──")
    for s in C.list_strategies():
        if s.get("error"):
            click.echo(f"  [ERR] {s['id']}: {s['error']}")
        else:
            click.echo(f"  {s['id']:24s} {s.get('family'):6s} {C.describe(s)}")
    click.echo("── 回测配置 ──")
    for b in C.list_backtests():
        if b.get("error"):
            click.echo(f"  [ERR] {b['id']}: {b['error']}")
        else:
            click.echo(f"  {b['id']:24s} env={b['data_env']:4s} 策略数={b['n_strategies']}")


@strategy.command("run")
@click.option("--backtest", default="unified_sim", show_default=True, help="回测配置 id")
@click.option("--end", default="", help="数据截止日 YYYY-MM-DD，空=最新")
@click.option("--config", "config_path", default=None, help="配置文件路径")
def strategy_run(backtest: str, end: str, config_path: str | None) -> None:
    """运行一次统一回测（结果存 runs/<run_id>/summary.json）。"""
    from strategy import runner

    summary = runner.run(backtest, end or None)
    click.echo(f"run_id={summary['run_id']} status={summary['status']} passed={summary.get('passed')}")


@strategy.command("health")
def strategy_health() -> None:
    """数据体检（模拟盘/实盘数据可用性）。"""
    from strategy import data_health as DH

    h = DH.compute(refresh=True)
    for env in ("sim", "live"):
        x = h.get(env) or {}
        click.echo(f"  [{env}] ok={x.get('ok')} {x.get('reason') or x.get('source') or ''}")


@cli.command("ui")
@click.option("--port", default=8501, show_default=True, help="Streamlit 端口")
def ui(port: int) -> None:
    """启动 Streamlit UI（社区高手 + 高手跟随策略）。"""
    import subprocess
    import sys

    app = Path(__file__).resolve().parent / "ui" / "app.py"
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(app), "--server.port", str(port)], check=True)


if __name__ == "__main__":  # pragma: no cover
    cli()
