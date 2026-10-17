#!/usr/bin/env python3
"""无头浏览器端到端验证：渲染两个页面，收集 console 错误 / 请求失败 / 关键 DOM。"""
import sys
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8601"
errors, failed = [], []
name_key = {"社区高手": "experts", "高手跟随策略": "strategy"}


def e_is_visible(page, el):
    return page.evaluate("(el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)", el)


def count_visible(page, sel):
    return sum(1 for el in page.query_selector_all(sel) if e_is_visible(page, el))


def check_page(page, path, name, wait_ms=6000):
    msgs = []
    page.on("console", lambda m: msgs.append(f"[console.{m.type}] {m.text}") if m.type in ("error",) else None)
    page.on("pageerror", lambda e: msgs.append(f"[pageerror] {e}"))
    page.on("requestfailed", lambda r: failed.append(f"{name}: {r.url}"))
    page.goto(BASE + "/", wait_until="networkidle")
    # 用点击导航（真实用户行为）切页面，避免 hash-only goto 的浏览器差异
    nav_sel = {"experts": "#nav-experts", "strategy": "#nav-strategy"}
    page.click(nav_sel[name_key[name]])
    page.wait_for_timeout(wait_ms)
    h3s, subs = [], []
    for e in page.query_selector_all("h3"):
        if e_is_visible(page, e):
            h3s.append(e.inner_text())
    for e in page.query_selector_all(".subtabs a"):
        if e_is_visible(page, e):
            subs.append(e.inner_text())
    info = {
        "hash": page.evaluate("() => location.hash"),
        "title": page.title(),
        "h3": h3s[:6],
        "kv_cards": count_visible(page, ".kv"),
        "tables": count_visible(page, "table.grid"),
        "rows": count_visible(page, "table.grid tbody tr"),
        "subtabs": subs,
        "jobs": [e.inner_text().replace("\n", " ") for e in page.query_selector_all(".job")][:3],
        "charts": count_visible(page, "canvas"),
        "sysinfo": (page.query_selector_all("#sysinfo")[0].inner_text() if page.query_selector_all("#sysinfo") else ""),
    }
    return info, msgs


with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True, args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1440, "height": 900})

    for path, name in [("/#/experts", "社区高手"), ("/#/expert-strategy", "高手跟随策略")]:
        info, msgs = check_page(pg, path, name)
        print(f"═══ {name} ═══")
        for k, v in info.items():
            print(f"  {k}: {v}")
        print(f"  JS 错误: {msgs if msgs else '无'}")
        errors += [f"{name}: {m}" for m in msgs]
        pg.screenshot(path=f"logs/shot_{'experts' if 'experts' in path else 'strategy'}.png", full_page=False)

    # 交互：切子 tab（高手跟随策略页）
    page_ = pg
    page_.click("#nav-strategy")
    page_.wait_for_timeout(4000)
    for i in (1, 2, 3):
        tabs = page_.query_selector_all("#strategy-root .subtabs a")
        if len(tabs) > i:
            tabs[i].click()
            page_.wait_for_timeout(2500)
            print(f"  [子tab {i}] {tabs[i].inner_text()} → tables={count_visible(page_, 'table.grid')} "
                  f"empty={count_visible(page_, '.empty')} jobs={count_visible(page_, '.job')} "
                  f"h3={[e.inner_text() for e in page_.query_selector_all('#strategy-root h3')][:2]}")

    # 交互：社区高手页切子 tab
    page_.click("#nav-experts")
    page_.wait_for_timeout(3000)
    tabs = page_.query_selector_all("#experts-root .subtabs a")
    for i in range(1, len(tabs)):
        tabs[i].click()
        page_.wait_for_timeout(1200)
        print(f"  [子tab {i}] {tabs[i].inner_text()} → rows={count_visible(page_, '#experts-root table.grid tbody tr')}")

    b.close()

print()
print("═══ 汇总 ═══")
print("JS 错误:", len(errors))
for e in errors[:10]:
    print("  ", e)
print("失败请求:", len(failed))
for f in failed[:10]:
    print("  ", f)
sys.exit(1 if errors or failed else 0)
