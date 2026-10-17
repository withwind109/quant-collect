"""高手跟随策略页面（对应原 xcq-platform #/expert-strategy / routes_expert_follow.py）。

功能（对齐原 expert-follow 工作台的 API 面，无 FastAPI 中间层，Streamlit 直接调业务模块）：
  * 策略看板：模拟账户前向表现（paper_sim 按日重放）＋ 证据等级 ＋ 晋级状态 ＋ 前向对账
  * 策略文件：内置/用户策略列表、原文与解析、中文描述、指纹、另存为用户策略、参数预览
  * 回测配置：统一回测矩阵（策略 + 扫描 + 闸门 + 随机对照）、自定义回测保存
  * 运行回测：后台运行（strategy.runner 子进程）＋ 运行记录 ＋ 方案明细/净值 ＋ 发布模拟盘 ＋ 晋级管理
  * 数据体检：模拟盘/实盘数据可用性

与原版的差异：v3_tiers 引擎（模型训练流水线 expert_imitation/LightGBM）尚未迁移，
统一回测配置里已停用该策略；页面选择该方案时会给出明确提示。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import streamlit as st

from ui import components as X
from paths import LOG_DIR


def _sys_executable() -> str:
    import sys
    return sys.executable


# ── 策略看板 ────────────────────────────────────────────────────────────────

def _acct_perf(acct: dict) -> Dict[str, Any]:
    from core.db import q
    nav = q("SELECT trade_date, total_nav FROM xcq_paper_nav_daily WHERE account_id=? ORDER BY trade_date",
            (acct["id"],))
    initial = float(acct.get("initial_capital") or 1_000_000)
    pts = [(str(r["trade_date"]), float(r["total_nav"])) for r in nav if r.get("total_nav") is not None]
    out: Dict[str, Any] = {"n_days": len(pts), "first_date": pts[0][0] if pts else None,
                           "last_date": pts[-1][0] if pts else None, "nav": pts}
    if not pts:
        return {**out, "cum_return_pct": 0.0, "drawdown_pct": 0.0, "max_drawdown_pct": 0.0}
    vs = [v for _, v in pts]
    peak, mdd = initial, 0.0
    for v in vs:
        peak = max(peak, v)
        mdd = max(mdd, 1 - v / peak)
    out.update(cum_return_pct=round((vs[-1] / initial - 1) * 100, 2),
               drawdown_pct=round((1 - vs[-1] / peak) * 100, 2), max_drawdown_pct=round(mdd * 100, 2))
    try:
        from strategy import alpha as A
        ds = [d for d, _ in pts]
        b = A.bench_returns(ds[0], ds[-1])
        bd = [d for d in ds if d in b]
        if len(bd) >= 2:
            br = 1.0
            for d in bd[1:]:
                br *= (1 + b[d])
            out["bench_return_pct"] = round((br - 1) * 100, 2)
            out["excess_pct"] = round(out["cum_return_pct"] - out["bench_return_pct"], 2)
        if len(pts) >= 10:
            rets = [vs[i] / vs[i - 1] - 1 for i in range(1, len(vs))]
            al = A.regress(ds[1:], rets, b)
            if al:
                out.update(alpha_pct=round(al["alpha"], 2), alpha_t=round(al["alpha_t"], 2))
    except Exception:  # noqa: BLE001
        pass
    return out


def _overview_tab() -> None:
    from core.db import q
    from strategy import config as C
    from strategy import promotion, recon
    from strategy import paper_sim

    st.subheader("模拟账户看板")
    st.caption("前向净值由 paper_sim 按日重放（与回测同一套选人/目标仓位函数，PIT 口径）；"
               "先点「更新前向净值」再刷新看板。")
    c1, c2 = st.columns([1, 3])
    with c1:
        if st.button("🔄 更新前向净值", key="ov_update", width="stretch"):
            with st.spinner("按日重放中（数据量大时可能耗时几分钟）…"):
                try:
                    res = paper_sim.update_all()
                    ok = [r for r in res if not r.get("error")]
                    st.success(f"已更新 {len(ok)} 个账户" + (f"；{len(res) - len(ok)} 个失败" if len(res) != len(ok) else ""))
                    for r in res:
                        if r.get("error"):
                            st.warning(f"{r['account_id']}: {r['error']}")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"更新失败: {exc}")
    with c2:
        st.caption("发布到模拟盘的账户每天按 T 日信号 / T+1 开盘执行记账；满 60 个交易日可与回测对账。")

    cfgs = {c["id"]: c for c in C.list_strategies() if not c.get("error")}
    bind = {c["bind_strategy_id"]: c for c in cfgs.values() if c.get("bind_strategy_id")}
    promos = {p["strategy_id"]: p for p in promotion.list_all()}
    rows = q("SELECT a.id, a.name, a.initial_capital, a.enabled, a.created_at, s.strategy_id, s.tier, s.spec_json "
             "FROM xcq_paper_accounts a JOIN xcq_strategies s ON s.strategy_id=a.strategy_id "
             "WHERE s.strategy_id LIKE 'expert_follow%' ORDER BY a.created_at")
    if not rows:
        st.info("还没有模拟账户 —— 在「运行回测」里跑一次统一回测并发布方案。")
    accts = []
    for r in rows:
        try:
            spec = json.loads(r.get("spec_json") or "{}")
        except ValueError:
            spec = {}
        cfg = bind.get(r["strategy_id"]) or cfgs.get(spec.get("config_id") or "")
        perf = _acct_perf(r)
        p = promos.get(r["strategy_id"])
        tier = r.get("tier") or "E0"
        stage = ("live" if p and p.get("state") == "approved" else
                 "requested" if p and p.get("state") == "requested" else
                 "halted" if p and p.get("state") == "halted" else
                 "ready" if tier in ("E2", "E3") else "observing")
        accts.append({**r, "spec": spec, "cfg": cfg, "perf": perf, "promotion": p, "stage": stage})
    if accts:
        rows_out = [{
            "账户": a["id"], "名称": a["name"], "策略文件": (a["cfg"] or {}).get("id") or a["spec"].get("config_id") or "—",
            "家族": (a["cfg"] or {}).get("family") or "—", "等级": a["tier"] or "E0", "阶段": a["stage"],
            "天数": a["perf"]["n_days"], "累计%": a["perf"]["cum_return_pct"],
            "回撤%": a["perf"]["drawdown_pct"], "最大回撤%": a["perf"]["max_drawdown_pct"],
            "超额%": a["perf"].get("excess_pct"), "晋级": (a["promotion"] or {}).get("state") or "—",
            "60日进度": f"{min(1.0, a['perf']['n_days'] / 60):.0%}",
        } for a in accts]
        X.show_df(rows_out, height=260)
        # 净值曲线
        curves = {}
        for a in accts:
            if a["perf"]["nav"]:
                curves[a["name"][:20] or a["id"]] = {d: round(v / float(a["initial_capital"]), 4) for d, v in a["perf"]["nav"]}
        if curves:
            import pandas as pd
            df = pd.DataFrame(curves)
            df.index.name = "日期"
            st.caption("净值曲线（归一化到 1）")
            st.line_chart(df, height=320)
        # 晋级操作
        st.subheader("晋级管理（正式交易）")
        st.caption("晋级只生成待确认单（原平台口径）；approve 后 state=approved。")
        opts = [a["strategy_id"] for a in accts]
        sel = st.selectbox("策略", opts, key="promo_sid") if opts else None
        pc1, pc2, pc3, pc4 = st.columns([1, 1, 1, 2])
        with pc1:
            cap = st.number_input("资金", min_value=10000.0, value=1_000_000.0, step=100000.0, key="promo_cap")
        with pc2:
            dd = st.number_input("最大回撤%", min_value=1.0, max_value=50.0, value=10.0, step=1.0, key="promo_dd")
        with pc3:
            st.write("")
            if st.button("申请晋级", key="promo_req", width="stretch") and sel:
                try:
                    promotion.request(sel, cap, dd / 100.0)
                    st.success("已提交申请"); st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
        with pc4:
            b1, b2 = st.columns(2)
            with b1:
                if st.button("批准", key="promo_ok", width="stretch") and sel:
                    try:
                        promotion.approve(sel); st.success("已批准"); st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))
            with b2:
                if st.button("撤销", key="promo_no", width="stretch") and sel:
                    try:
                        promotion.revoke(sel); st.success("已撤销"); st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))
        # 前向对账汇总
        try:
            rs = recon.summaries()
            if rs:
                st.subheader("前向 vs 回测 对账")
                X.show_df([{"账户": k, **v} for k, v in rs.items()], height=200)
        except Exception as exc:  # noqa: BLE001
            st.caption(f"对账汇总不可用: {exc}")


# ── 策略文件 ────────────────────────────────────────────────────────────────

def _strategies_tab() -> None:
    from strategy import config as C
    st.subheader("策略文件（configs/expert_follow/strategies）")
    st.caption("内置策略页面只读；「另存为」写到 strategies/user/。同一份文件既用于回测也用于模拟盘，改文件 = 新版本（指纹变）。")
    try:
        strategies = C.list_strategies()
    except Exception as exc:  # noqa: BLE001
        st.error(f"读取策略文件失败: {exc}")
        return
    rows = [{"id": s["id"], "家族": s.get("family"), "内置": "✓" if s.get("_builtin") else "用户",
             "描述": s.get("label") or s.get("error") or "", "指纹": s.get("hash") or "—"} for s in strategies]
    X.show_df(rows, height=200)
    ok_ids = [s["id"] for s in strategies if not s.get("error")]
    if not ok_ids:
        return
    sel = st.selectbox("查看策略", ok_ids, key="strat_sel")
    try:
        s = C.load_strategy(sel)
    except C.ConfigError as exc:
        st.error(str(exc))
        return
    fp = C.strategy_path(sel)
    c1, c2 = st.columns(2)
    with c1:
        st.caption(f"原文（{fp}）")
        st.code(fp.read_text(encoding="utf-8"), language="json")
    with c2:
        st.caption("解析 + 中文描述")
        st.json(s)
        st.markdown(f"**描述**：{C.describe(s)}")
        st.markdown(f"**指纹**：`{C.strategy_hash(s)}`")
    st.subheader("另存为用户策略 / 参数预览")
    st.caption("在左侧原文基础上改 spec（JSON），先预览（校验+描述+指纹），再另存为。")
    edited = st.text_area("spec（JSON）", value=json.dumps({k: v for k, v in s.items() if not k.startswith("_")},
                                                          ensure_ascii=False, indent=2), height=220, key="strat_edit")
    pc1, pc2 = st.columns(2)
    with pc1:
        if st.button("预览", key="strat_preview"):
            try:
                ns = C.normalize_strategy(json.loads(edited))
                st.success(f"校验通过 · {C.describe(ns)} · 指纹 {C.strategy_hash(ns)}")
            except Exception as exc:  # noqa: BLE001
                st.error(f"校验失败: {exc}")
    with pc2:
        if st.button("另存为", key="strat_save"):
            try:
                ns = C.save_user_strategy(json.loads(edited))
                st.success(f"已保存 strategies/user/{ns['id']}.json5"); st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(f"保存失败: {exc}")


# ── 回测配置 ────────────────────────────────────────────────────────────────

def _backtests_tab() -> None:
    from strategy import config as C
    st.subheader("回测配置（configs/expert_follow/backtests）")
    st.caption("统一回测：所有版本同一回测期、同一套闸门（不放宽）；随机对照从同一合格池抽同样人数。")
    try:
        bts = C.list_backtests()
    except Exception as exc:  # noqa: BLE001
        st.error(f"读取回测配置失败: {exc}")
        return
    rows = [{"id": b["id"], "名称": b.get("name") or b["id"], "环境": b.get("data_env"),
             "策略数": b.get("n_strategies"), "内置": "✓" if b.get("_builtin") else "用户",
             "错误": b.get("error") or ""} for b in bts]
    X.show_df(rows, height=180)
    ok_ids = [b["id"] for b in bts if not b.get("error")]
    if not ok_ids:
        return
    sel = st.selectbox("查看回测配置", ok_ids, key="bt_sel")
    b = C.load_backtest(sel)
    c1, c2 = st.columns(2)
    with c1:
        st.caption("策略清单（含参数扫描展开数）")
        rows = []
        for e in b.get("strategies") or []:
            try:
                n = len(C.expand(e, b["data_env"])) if e.get("enabled", True) else 0
            except Exception:  # noqa: BLE001
                n = -1
            rows.append({"ref": e["ref"], "启用": "✓" if e.get("enabled", True) else "✗", "变体数": n})
        X.show_df(rows, height=200)
    with c2:
        st.caption("闸门 / 随机对照 / 显示")
        st.json({"period": b["period"], "capital": b["capital"], "baseline": b["baseline"],
                 "gate": b["gate"], "null": b["null"], "display": b["display"]})
    st.subheader("自定义回测")
    st.caption("闸门/基准/随机对照分位固定沿用内置 unified_<env>（不放宽）。")
    all_refs = [s["id"] for s in C.list_strategies() if not s.get("error")]
    c1, c2, c3 = st.columns([1, 2, 1])
    with c1:
        bid = st.text_input("回测 id（字母/数字/下划线）", key="ubt_id")
    with c2:
        refs = st.multiselect("选择策略（可多选）", all_refs, key="ubt_refs")
    with c3:
        env = st.selectbox("数据环境", ["sim", "live"], key="ubt_env")
        if st.button("保存", key="ubt_save", width="stretch"):
            if not bid.strip() or not refs:
                st.error("id 与策略都不能为空")
            else:
                try:
                    b2 = C.save_user_backtest({"id": bid.strip(), "name": bid.strip(), "data_env": env,
                                               "strategies": [{"ref": r} for r in refs]})
                    st.success(f"已保存 backtests/user/{b2['id']}.json5"); st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(f"保存失败: {exc}")


# ── 运行回测 ────────────────────────────────────────────────────────────────

def _run_tab() -> None:
    from strategy import config as C
    from strategy import live as L
    from strategy import store

    st.subheader("运行统一回测")
    st.caption("后台运行（strategy.runner 子进程）；一次只跑一个。运行结束后再到「策略看板」发布。")
    bts = [b["id"] for b in C.list_backtests() if not b.get("error")]
    c1, c2, c3 = st.columns([2, 2, 1])
    with c1:
        bid = st.selectbox("回测配置", bts, key="run_bid")
    with c2:
        end = st.text_input("数据截止日（空=最新）", key="run_end")
    with c3:
        st.write("")
        st.write("")
        if st.button("▶ 运行", type="primary", key="run_start", width="stretch"):
            run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
            status_fp = LOG_DIR / f"ef_status_{run_id}.json"
            argv = [_sys_executable(), "-W", "ignore", "-m", "strategy.runner",
                    "--backtest", bid, "--status-file", str(status_fp), "--run-id", run_id]
            if end.strip():
                argv += ["--end", end.strip()]
            fp = X.start_job("backtest", argv)
            st.session_state["run_id"] = run_id
            st.session_state["run_status_fp"] = str(status_fp)
            st.session_state["run_job_fp"] = str(fp)
            st.rerun()
    run_id = st.session_state.get("run_id")
    if run_id:
        status_fp = Path(st.session_state["run_status_fp"])
        st_ = X.job_status(status_fp)
        if st_.get("state") in ("running", "starting"):
            st.info(f"运行 {run_id} 进行中…（{st_.get('stage') or st_.get('progress') or ''}）")
            st.markdown('<meta http-equiv="refresh" content="5">', unsafe_allow_html=True)
        elif st_.get("state") == "done":
            st.success(f"运行 {run_id} 完成")
        elif st_.get("state") == "error":
            st.error(f"运行 {run_id} 失败: {st_.get('error') or st_.get('msg') or '见日志'}")
        X.job_panel(Path(st.session_state["run_job_fp"]), "回测任务")

    st.subheader("运行记录")
    try:
        runs = store.list_runs(archived=False)
    except Exception as exc:  # noqa: BLE001
        st.error(f"读取运行记录失败: {exc}")
        return
    if not runs:
        st.info("还没有运行记录")
        return
    rows = [{"run_id": r["run_id"], "回测": r.get("backtest_id"), "环境": r.get("data_env"),
             "状态": r.get("status"), "开始": X.fmt_ts(r.get("started_at")),
             "完成": X.fmt_ts(r.get("finished_at")), "过闸门": "、".join(r.get("passed") or []) or "—"}
            for r in runs]
    X.show_df(rows, height=200)
    sel = st.selectbox("查看运行", [r["run_id"] for r in runs], key="run_detail_sel")
    if not sel:
        return
    s = L.load_summary(sel)
    if not s:
        st.warning("找不到该运行的 summary.json")
        return
    if s.get("status") == "blocked":
        st.error(f"运行被数据体检拦截: {s.get('reason') or s.get('msg')}")
        return
    st.caption(f"回测 {s.get('backtest_id')} · 环境 {s.get('data_env')} · "
               f"{s.get('period', {}).get('start')} ~ {s.get('period', {}).get('end')}")
    variants = s.get("variants") or {}
    vrows = []
    for k, v in variants.items():
        m = v.get("metrics") or {}
        g = v.get("gate") or {}
        vrows.append({"方案": k, "策略": v.get("ref"), "引擎": v.get("engine") or "",
                      "总收益%": m.get("total_return_pct"), "年化%": m.get("annualized_pct"),
                      "最大回撤%": m.get("max_drawdown_pct"), "夏普": m.get("sharpe"),
                      "α%": m.get("alpha_pct"), "α t": m.get("alpha_t"), "买入笔数": m.get("n_buys"),
                      "过闸门": "✓" if g.get("passed") else "✗",
                      "未过项": "、".join(c for c, ok in (g.get("checks") or {}).items() if not ok)})
    vrows.sort(key=lambda r: (r["过闸门"] == "✗", -(r["总收益%"] or -1e9)))
    X.show_df(vrows[:60], height=380)
    # 净值曲线（前 N 个方案）
    import pandas as pd
    curves = {}
    for k, v in list(variants.items())[:8]:
        nd = v.get("nav") or []
        if nd:
            curves[k] = {d: round(float(x), 4) for d, x in nd}
    if curves:
        st.caption("方案净值（归一化到 1，最多 8 个）")
        st.line_chart(pd.DataFrame(curves), height=320)
    # 发布 / 晋级
    st.subheader("发布到模拟盘 / 晋级")
    vkeys = list(variants.keys())
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        vkey = st.selectbox("方案", vkeys, key="pub_vkey")
    with c2:
        cap = st.number_input("初始资金", min_value=10000.0, value=1_000_000.0, step=100000.0, key="pub_cap")
    with c3:
        observe = st.checkbox("前向观察（未过闸门）", key="pub_obs")
        if st.button("发布", key="pub_go", width="stretch"):
            try:
                r = L.publish(sel, vkey, observe=observe, capital=cap)
                st.success(f"已发布账户 {r['account_id']}（策略 {r['strategy_id']}）"); st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    st.caption("发布后到「策略看板」点「更新前向净值」累计前向证据；晋级在「策略看板 → 晋级管理」操作。")


# ── 数据体检 ────────────────────────────────────────────────────────────────

def _health_tab() -> None:
    from strategy import data_health as DH
    st.subheader("数据体检")
    st.caption("模拟盘（history_bulk）/ 实盘（rtV3 流水）数据可用性；实盘回测前会先过体检。")
    if st.button("🔄 重新体检", key="health_refresh"):
        with st.spinner("体检中…"):
            try:
                DH.compute(refresh=True)
                st.success("体检完成")
            except Exception as exc:  # noqa: BLE001
                st.error(f"体检失败: {exc}")
    try:
        h = DH.compute()
    except Exception as exc:  # noqa: BLE001
        st.error(f"读取体检结果失败: {exc}")
        return
    if not h:
        st.info("还没有体检结果，点「重新体检」")
        return
    st.caption(f"生成于 {h.get('generated_at') or '—'}")
    for env in ("sim", "live"):
        x = h.get(env) or {}
        if not x:
            continue
        st.markdown(f"**{'模拟盘' if env == 'sim' else '实盘'}**")
        X.kv_cards([
            ("可用", "✓" if x.get("ok") else "✗"), ("说明", x.get("reason") or x.get("source") or "—"),
            ("组合数", x.get("n_experts") or x.get("portfolios") and len(x["portfolios"]) or "—"),
        ])


def render() -> None:
    X.page_header("高手跟随策略", "策略文件化 · 统一回测（同回测期/同闸门/随机对照）· 模拟盘前向验证 · 晋级管理")
    tabs = st.tabs(["策略看板", "策略文件", "回测配置", "运行回测", "数据体检"])
    with tabs[0]:
        _overview_tab()
    with tabs[1]:
        _strategies_tab()
    with tabs[2]:
        _backtests_tab()
    with tabs[3]:
        _run_tab()
    with tabs[4]:
        _health_tab()
