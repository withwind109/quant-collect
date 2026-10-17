"""批量拉取高手组合调仓历史 (2026-09-24; 2026-10-16 迁移到 expert-platform)。

与 history.py(单组合) 的区别:
  * 名单 = 最近每日爬取产出(data/crawl/<日期>/expert_buys.json)里出现过的全部东财组合(模拟盘长号);
  * 落盘到 data/history_bulk/<zjzh>.json（跟随策略回测的数据基础, 见 strategy/pit_nav.py）;
  * 可断点续跑: 已有文件且 since 覆盖得住的跳过; 连续失败过多自动停(疑似被限流)。

用法:
  python -m bulk_history --since 2025-12-01
  python -m bulk_history --incremental            # 夜间增量: 已有文件只补最近几天

since 取得比回补窗口早半年: 逐日重建组合持仓/净值需要窗口开始前的持仓,
而持仓只能从调仓记录里推出来(见 strategy/pit_nav.py)。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

from paths import CRAWL_DATA_DIR, HISTORY_BULK_DIR
from pipeline import build_sources
from settings import DEFAULT_CONFIG, load_settings

OUT_DIR = HISTORY_BULK_DIR


def roster() -> dict:
    """{zjzh: 组合名}: 每日产出里出现过的全部东财组合。"""
    out: dict = {}
    for fp in sorted(CRAWL_DATA_DIR.glob("20*/expert_buys.json")):
        try:
            rows = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for e in rows:
            if e.get("source") == "eastmoney" and e.get("expert_id"):
                out[str(e["expert_id"])] = e.get("expert_name") or e.get("owner") or ""
    return out


def main(since: str = "2025-12-01", limit: int = 0, max_fail: int = 25,
         incremental: bool = False, config_path: Path | None = None) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    names = roster()
    for fp in OUT_DIR.glob("*.json"):              # 名单只增不减: 以前拉过的组合继续跟
        names.setdefault(fp.stem, "")
    ids = sorted(names)
    if limit:
        ids = ids[:limit]
    if not ids:
        logger.warning("[bulk-history] 名单为空: 先跑每日爬取(crawl)产生 expert_buys.json")
        return
    # force_http=True: 批量回补是 N 组合 × M 页的重任务，浏览器限速跑不完，强制 curl 轻量后端
    client = build_sources(load_settings(config_path or DEFAULT_CONFIG), force_http=True)["eastmoney"]
    done = skipped = failed = consec = 0
    t0 = time.time()
    try:
        for i, zjzh in enumerate(ids, 1):
            fp = OUT_DIR / f"{zjzh}.json"
            old = None
            if fp.exists():
                try:
                    old = json.loads(fp.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    old = None
                if old and not incremental and old.get("since", "9") <= since:
                    skipped += 1
                    continue
            use_since = since
            if incremental and old:
                last = max([r.get("trade_date") or "" for r in old.get("records") or []] + [old.get("since") or since])
                use_since = (datetime.strptime(last[:10], "%Y-%m-%d") - timedelta(days=5)).strftime("%Y-%m-%d")
            try:
                recs = client.portfolio_history(zjzh, since=use_since)
                consec = 0
            except Exception as e:  # noqa: BLE001
                failed += 1
                consec += 1
                logger.warning(f"[bulk-history] {zjzh} 失败({consec}连): {e}")
                if consec >= max_fail:
                    logger.error("[bulk-history] 连续失败过多, 停止(稍后重跑会从断点继续)")
                    break
                time.sleep(min(60, 2 ** min(consec, 6)))
                continue
            rows = [r.model_dump(exclude={"raw"}) for r in recs]
            if incremental and old:
                def key(r):
                    return (str(r.get("trade_time")), r.get("stock_code"), r.get("action"),  # noqa: E731
                            r.get("weight_before"), r.get("weight_after"))
                seen = {key(r) for r in rows}
                rows = rows + [r for r in old.get("records") or [] if key(r) not in seen]
                rows.sort(key=lambda r: str(r.get("trade_time")), reverse=True)
            fp.write_text(json.dumps({
                "zjzh": zjzh, "name": names.get(zjzh, "") or (old or {}).get("name", ""),
                "since": (old or {}).get("since") or since,
                "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "count": len(rows), "records": rows,
            }, ensure_ascii=False, default=str), encoding="utf-8")
            done += 1
            if i % 100 == 0:
                el = time.time() - t0
                logger.info(f"[bulk-history] {i}/{len(ids)} 新拉{done} 跳过{skipped} 失败{failed} "
                            f"已用{el / 60:.0f}分, 预计剩余{el / max(done, 1) * (len(ids) - i) / 60:.0f}分")
    finally:
        try:
            client.http.close()
        except Exception:  # noqa: BLE001
            pass
    logger.info(f"[bulk-history] 结束: 共{len(ids)} 新拉{done} 跳过{skipped} 失败{failed}")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="批量回补高手组合调仓历史")
    ap.add_argument("--since", default="2025-12-01")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个(调试)")
    ap.add_argument("--max-fail", type=int, default=25, help="连续失败这么多次就停(疑似限流)")
    ap.add_argument("--incremental", action="store_true", help="已有文件只补最近几天")
    a = ap.parse_args()
    main(since=a.since, limit=a.limit, max_fail=a.max_fail, incremental=a.incremental)
