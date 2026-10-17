"""推荐台账: 每个策略每天一批推荐, 生效日开盘(09:30)前可覆盖, 之后锁定不可修改。

valid_forward=1 表示"在生效日开盘前生成"——只有这样的记录才计入前向证据 (防事后回填/改写)。
backfilled=1 的记录只作参考。

迁移自 scripts/recs/ledger.py (2026-10-16)。存储 MySQL -> 本地 SQLite:
  * 源表字段比 core.db 预建的 recs_ledger (id/rec_date/strategy_id/code/action/target_weight/
    price/status/meta_json/created_at) 多 (content_hash/valid_forward/backfilled/...),
    按迁移约定用 core.db.ensure_table 建适配表 xcq_recommendations / xcq_rec_runs
    (列同源表, 见 recs_store), recs_ledger 预留表本模块未使用;
  * "删旧+写新" 保持同一事务 (strategy.recs_store.tx(), 对应源 get_engine().begin());
  * ON DUPLICATE KEY UPDATE -> ON CONFLICT(pred_date,strategy_id) DO UPDATE (只更指定列,
    与源语义一致; 简单的整行 upsert 才用 INSERT OR REPLACE);
  * NOW() -> datetime.now() 参数; IF(...) -> CASE WHEN; date/datetime 参数转 ISO 字符串。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Dict, List, Optional, Sequence

from core.logger import logger
from strategy import recs_store as store

LOCK_TIME = time(9, 30)  # 生效日开盘: 之后不得改写已成立的前向记录


@dataclass
class Rec:
    code: str
    action: str                       # 买入/卖出/持有/减仓/观望
    reason_code: str
    reason_text: str = ""
    name: str = ""
    score: Optional[float] = None
    rank_no: Optional[int] = None
    target_weight: Optional[float] = None
    stop_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    pool_ref: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def normalized_code(self) -> str:
        return str(self.code).split(".")[0].strip()


def _r(v: Any, n: int = 4) -> Any:
    return None if v is None else round(float(v), n)


def rec_hash(r: Rec) -> str:
    payload = [r.normalized_code(), r.action, r.reason_code, _r(r.score), r.rank_no,
               _r(r.target_weight), _r(r.stop_pct, 2), _r(r.take_profit_pct, 2),
               json.dumps(r.meta, sort_keys=True, ensure_ascii=False, default=str)]
    return hashlib.sha1(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()[:16]


def batch_hash(hashes: Sequence[str]) -> str:
    return hashlib.sha1("|".join(sorted(hashes)).encode()).hexdigest()[:16]


def _d(v: Any) -> date:
    return v if isinstance(v, date) and not isinstance(v, datetime) else datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def write_batch(strategy_id: str, strategy_version: int, data_date: Any, pred_date: Any,
                recs: List[Rec], *, generated_at: Optional[datetime] = None,
                backfilled: bool = False, note: str = "", invalid: bool = False) -> Dict[str, Any]:
    """写入一个策略在 pred_date 的整批推荐。返回 {status, n, valid_forward, hash}。

    status: written / unchanged / locked(已锁定且内容不同, 拒绝改写) / empty(0 条也记一次运行)
    """
    store.ensure_schema()
    now = generated_at or datetime.now()
    dd, pd_ = _d(data_date), _d(pred_date)
    # invalid=True: 生成时行情数据还没同步到信息截止日(如收盘后手动跑、数据还是旧的), 推荐照常展示但不计前向证据;
    # 之后盘前的正式运行会覆盖它(未锁定前允许覆盖)
    valid = 0 if (backfilled or invalid) else int(now < datetime.combine(pd_, LOCK_TIME))

    # 同 (code, action) 只留一条(分数高者优先), 满足唯一键
    dedup: Dict[tuple, Rec] = {}
    for r in recs:
        k = (r.normalized_code(), r.action)
        if k not in dedup or (r.score or 0) > (dedup[k].score or 0):
            dedup[k] = r
    items = list(dedup.values())
    hashes = [rec_hash(r) for r in items]
    bh = batch_hash(hashes) if hashes else None

    existing = store.fetch(
        "SELECT content_hash, valid_forward, backfilled FROM xcq_recommendations WHERE pred_date=:p AND strategy_id=:s",
        {"p": pd_, "s": strategy_id})
    if existing:
        old_bh = batch_hash([e["content_hash"] for e in existing])
        old_forward = all(int(e["valid_forward"]) == 1 and int(e["backfilled"]) == 0 for e in existing)
        # 内容相同: 只有在"不会让证据资格变差/也没有升级机会"时才算无变化。
        # 已有记录是回补/迟到的, 而这次是盘前的正式生成 -> 需要重写以升级为可计入前向证据。
        if old_bh == bh and (old_forward or not valid):
            return {"status": "unchanged", "n": len(items), "valid_forward": int(old_forward), "hash": bh}
        locked = any(int(e["valid_forward"]) == 1 for e in existing) and now >= datetime.combine(pd_, LOCK_TIME)
        if locked:
            logger.warning(f"[recs] {strategy_id}@{pd_} 已锁定, 拒绝改写 (旧 {old_bh} → 新 {bh})")
            store.run("INSERT INTO xcq_rec_runs (pred_date,strategy_id,status,n_recs,valid_forward,batch_hash,note,generated_at) "
                      "VALUES (:p,:s,'locked',:n,1,:h,:note,:g) "
                      "ON CONFLICT(pred_date,strategy_id) DO UPDATE SET note=excluded.note",
                      {"p": pd_, "s": strategy_id, "n": len(existing), "h": old_bh,
                       "note": f"锁定后尝试改写被拒绝(新哈希{bh})", "g": now})
            return {"status": "locked", "n": len(existing), "valid_forward": 1, "hash": old_bh}

    rows = [{
        "dd": dd.isoformat(), "pd": pd_.isoformat(), "sid": strategy_id, "sv": strategy_version, "code": r.normalized_code(),
        "name": (r.name or "")[:40], "action": r.action, "rc": r.reason_code, "rt": (r.reason_text or "")[:300],
        "score": r.score, "rank": r.rank_no, "tw": r.target_weight, "sl": r.stop_pct, "tp": r.take_profit_pct,
        "pool": r.pool_ref, "meta": json.dumps(r.meta, ensure_ascii=False, default=str),
        "gen": now.isoformat(), "h": h, "vf": valid, "bf": int(backfilled),
    } for r, h in zip(items, hashes)]

    with store.tx() as conn:   # 删旧+写新 同一事务
        conn.execute("DELETE FROM xcq_recommendations WHERE pred_date=? AND strategy_id=?",
                     (pd_.isoformat(), strategy_id))
        if rows:
            cols = ["data_date", "pred_date", "strategy_id", "strategy_version", "code", "name", "action",
                    "reason_code", "reason_text", "score", "rank_no", "target_weight", "stop_pct",
                    "take_profit_pct", "pool_ref", "meta_json", "generated_at", "content_hash",
                    "valid_forward", "backfilled"]
            keys = ["dd", "pd", "sid", "sv", "code", "name", "action", "rc", "rt", "score", "rank",
                    "tw", "sl", "tp", "pool", "meta", "gen", "h", "vf", "bf"]
            conn.executemany(
                f"INSERT INTO xcq_recommendations ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [tuple(r[k] for k in keys) for r in rows])
        conn.execute(
            "INSERT INTO xcq_rec_runs (pred_date,strategy_id,status,n_recs,valid_forward,batch_hash,note,generated_at) "
            "VALUES (?,?,'ok',?,?,?,?,?) "
            "ON CONFLICT(pred_date,strategy_id) DO UPDATE SET status='ok',n_recs=excluded.n_recs,"
            "valid_forward=excluded.valid_forward,batch_hash=excluded.batch_hash,note=excluded.note,"
            "generated_at=excluded.generated_at",
            (pd_.isoformat(), strategy_id, len(items), valid, bh, note[:300], now.isoformat()))
    return {"status": "written" if items else "empty", "n": len(items), "valid_forward": valid, "hash": bh}


def mark_missing(strategy_id: str, pred_date: Any, note: str, status: str = "missing") -> None:
    """当天该策略没能产出(数据缺失/异常): 显式记一笔, 证据与联赛据此标注缺失日而不是补 0。"""
    store.ensure_schema()
    store.run(
        "INSERT INTO xcq_rec_runs (pred_date,strategy_id,status,n_recs,valid_forward,note,generated_at) "
        "VALUES (:p,:s,:st,0,0,:n,:g) "
        "ON CONFLICT(pred_date,strategy_id) DO UPDATE SET "
        "status=CASE WHEN status='ok' AND n_recs>0 THEN status ELSE excluded.status END, "
        "note=CASE WHEN status='ok' AND n_recs>0 THEN note ELSE excluded.note END",
        {"p": _d(pred_date), "s": strategy_id, "st": status, "n": note[:300], "g": datetime.now()})


def get_recs(pred_date: Any, strategy_ids: Optional[List[str]] = None,
             actions: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM xcq_recommendations WHERE pred_date=:p"
    params: Dict[str, Any] = {"p": _d(pred_date)}
    if strategy_ids:
        sql += " AND strategy_id IN (" + ",".join(f":s{i}" for i in range(len(strategy_ids))) + ")"
        params.update({f"s{i}": s for i, s in enumerate(strategy_ids)})
    if actions:
        sql += " AND action IN (" + ",".join(f":a{i}" for i in range(len(actions))) + ")"
        params.update({f"a{i}": a for i, a in enumerate(actions)})
    return store.fetch(sql + " ORDER BY strategy_id, rank_no, code", params)
