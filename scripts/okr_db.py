#!/usr/bin/env python3
"""OKR 历史数据库（SQLite，Python 自带，无需安装）。

分工：
- Markdown 是“当前状态”的唯一来源，界面和 Obsidian 都改它；
- 数据库只追加记录每个 KR 的数值变化（只在变了的时候记），用来画趋势、
  统计本周进展、预测季末结果。数据库丢了也不影响 OKR 本身，下次同步会从当前值重新开始记。

KR 的身份靠行尾的 Obsidian 块 ID（如 ``^kr-3fa9c1``）识别：改标题、挪位置、
换目标都不会断开历史。没有 ID 的 KR 在同步时自动补上。
"""

from __future__ import annotations

import bisect
import datetime as dt
import secrets
import sqlite3
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

sys.path.insert(0, str(Path(__file__).resolve().parent))

import okr_tool as okr  # noqa: E402

# 每个元素是 (版本号, SQL)。只能往后追加，不能改已发布的版本。
MIGRATIONS = [
    (
        1,
        """
        CREATE TABLE kr (
            id          TEXT PRIMARY KEY,      -- 块 ID，如 kr-3fa9c1
            quarter     TEXT NOT NULL,         -- 2026-Q4
            objective   TEXT NOT NULL,         -- 所属目标（最近一次同步时）
            domain      TEXT NOT NULL,         -- 领域标签
            title       TEXT NOT NULL,
            position    INTEGER NOT NULL,      -- 在季度文件中的顺序
            first_seen  TEXT NOT NULL,
            removed_at  TEXT                   -- 从文件中删除的时间；恢复后清空
        );
        CREATE TABLE measurement (
            id       INTEGER PRIMARY KEY,
            kr_id    TEXT NOT NULL REFERENCES kr(id) ON DELETE CASCADE,
            ts       TEXT NOT NULL,            -- 本地时间 YYYY-MM-DDTHH:MM:SS
            current  REAL,
            target   REAL,
            checked  INTEGER NOT NULL CHECK (checked IN (0, 1)),
            dropped  INTEGER NOT NULL CHECK (dropped IN (0, 1)),
            progress REAL NOT NULL CHECK (progress BETWEEN 0 AND 1),
            source   TEXT NOT NULL CHECK (source IN ('gui', 'file'))
        );
        CREATE INDEX measurement_kr_ts ON measurement (kr_id, ts);
        -- 每个 KR 的最新一条记录，方便手写 SQL 查询
        CREATE VIEW kr_latest AS
            SELECT k.*, m.current, m.target, m.checked, m.dropped, m.progress,
                   m.ts AS updated_at
            FROM kr k
            JOIN measurement m ON m.id = (
                SELECT id FROM measurement WHERE kr_id = k.id ORDER BY ts DESC, id DESC LIMIT 1
            );
        """,
    ),
]

SOURCES = ("gui", "file")
TS_FMT = "%Y-%m-%dT%H:%M:%S"
FORECAST_WINDOW_DAYS = 28  # 预测速度只看最近 4 周，反映“现在”的节奏
MERGE_GUI_MINUTES = 10  # 界面上连续点 +/- 时，10 分钟内的修改合并成一条记录


# ---------------------------------------------------------------- 连接与迁移


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    path = path or okr.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    latest = MIGRATIONS[-1][0]
    if version > latest:
        raise RuntimeError(f"数据库版本 {version} 比程序支持的 {latest} 新，请先更新程序（git pull）。")
    for v, sql in MIGRATIONS:
        if v > version:
            # executescript 会先提交已有事务；整段放进一个事务里，失败就整体回滚
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {v};\nCOMMIT;")
            version = v
    return version


def ts_str(t: dt.datetime) -> str:
    return t.strftime(TS_FMT)


def parse_ts(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, TS_FMT)


# ---------------------------------------------------------------- 同步：Markdown -> 数据库


def _new_id(conn: sqlite3.Connection, taken: Set[str]) -> str:
    while True:
        kr_id = "kr-" + secrets.token_hex(3)
        if kr_id not in taken and not conn.execute("SELECT 1 FROM kr WHERE id = ?", (kr_id,)).fetchone():
            return kr_id


def assign_ids(conn: sqlite3.Connection, q: okr.Quarter, seen: Set[str], stats: Counter) -> None:
    """给缺 ID 的 KR 补上块 ID，并修正两类冲突：
    - 同一 ID 出现两次（Obsidian 里复制粘贴了一行）：后出现的换新 ID；
    - ID 属于别的季度（从上季度复制 KR 过来）：新季度单独开一份历史。
    """
    path = q.path
    before = path.stat().st_mtime_ns
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    changed = False
    for o in okr.parse_okr_text("\n".join(lines)):
        for kr in o.krs:
            owner = None
            if kr.id:
                owner = conn.execute("SELECT quarter FROM kr WHERE id = ?", (kr.id,)).fetchone()
            if kr.id and kr.id not in seen and (owner is None or owner["quarter"] == q.name):
                seen.add(kr.id)
                continue
            new = _new_id(conn, seen)
            line = okr.BLOCK_ID_RE.sub("", lines[kr.line]) if kr.id else lines[kr.line]
            lines[kr.line] = f"{line.rstrip()} ^{new}"
            seen.add(new)
            changed = True
            stats["ids_assigned"] += 1
    if not changed:
        return
    if path.stat().st_mtime_ns != before:
        # 读取之后文件又被别的程序改了：这次不写，下次同步再补
        stats["skipped_files"] += 1
        return
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _values(kr: Any) -> tuple:
    """用来比较“有没有变化”的字段。kr 可以是 KeyResult 或数据库行。"""
    if isinstance(kr, okr.KeyResult):
        return (kr.current, kr.target, int(kr.checked), int(kr.dropped))
    return (kr["current"], kr["target"], kr["checked"], kr["dropped"])


def _same(a: tuple, b: tuple) -> bool:
    def eq(x, y):
        if x is None or y is None:
            return x is y
        return abs(float(x) - float(y)) < 1e-9

    return all(eq(x, y) for x, y in zip(a, b))


def record(conn: sqlite3.Connection, q: okr.Quarter, source: str, ts: dt.datetime,
           now: dt.datetime, stats: Counter) -> None:
    present = []
    position = 0
    for o in q.objectives:
        for kr in o.krs:
            if not kr.id:  # 本次没能写入 ID 的文件，等下次
                continue
            present.append(kr.id)
            position += 1
            row = conn.execute("SELECT id FROM kr WHERE id = ?", (kr.id,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO kr (id, quarter, objective, domain, title, position, first_seen)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (kr.id, q.name, o.title, o.domain, kr.title, position, ts_str(ts)),
                )
                stats["krs_added"] += 1
            else:
                conn.execute(
                    "UPDATE kr SET objective = ?, domain = ?, title = ?, position = ?, removed_at = NULL"
                    " WHERE id = ?",
                    (o.title, o.domain, kr.title, position, kr.id),
                )
            recent = conn.execute(
                "SELECT * FROM measurement WHERE kr_id = ? ORDER BY ts DESC, id DESC LIMIT 2", (kr.id,)
            ).fetchall()
            last = recent[0] if recent else None
            if last is not None and _same(_values(kr), _values(last)):
                continue
            # 时间不能早于该 KR 上一条记录，也不能晚于现在（防止系统时间异常导致顺序错乱）
            at = min(ts, now)
            if last is not None:
                at = max(at, parse_ts(last["ts"]))
            if (source == "gui" and len(recent) == 2 and last["source"] == "gui"
                    and at - parse_ts(last["ts"]) <= dt.timedelta(minutes=MERGE_GUI_MINUTES)):
                # 连续点击：改写上一条，而不是再加一条；改回到更早的值就直接删掉。
                # 只合并“界面”记录，且至少保留一条更早的记录作为对比基准。
                if _same(_values(kr), _values(recent[1])):
                    conn.execute("DELETE FROM measurement WHERE id = ?", (last["id"],))
                else:
                    conn.execute(
                        "UPDATE measurement SET ts = ?, current = ?, target = ?, checked = ?, dropped = ?,"
                        " progress = ? WHERE id = ?",
                        (ts_str(at), kr.current, kr.target, int(kr.checked), int(kr.dropped),
                         kr.progress, last["id"]),
                    )
                stats["measurements_merged"] += 1
                continue
            conn.execute(
                "INSERT INTO measurement (kr_id, ts, current, target, checked, dropped, progress, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (kr.id, ts_str(at), kr.current, kr.target, int(kr.checked), int(kr.dropped),
                 kr.progress, source),
            )
            stats["measurements"] += 1
    marks = ",".join("?" * len(present))
    cur = conn.execute(
        f"UPDATE kr SET removed_at = ? WHERE quarter = ? AND removed_at IS NULL"
        f"{f' AND id NOT IN ({marks})' if present else ''}",
        (ts_str(now), q.name, *present),
    )
    stats["krs_removed"] += cur.rowcount


def sync(conn: sqlite3.Connection, source: str = "file", now: Optional[dt.datetime] = None) -> Dict[str, int]:
    """把所有季度文件的当前状态同步进数据库，返回统计。

    source="gui"：刚由界面写入，记录时间用“现在”；
    source="file"：在别处（Obsidian）改的，记录时间用文件的修改时间。
    """
    if source not in SOURCES:
        raise ValueError(source)
    now = (now or dt.datetime.now()).replace(microsecond=0)
    stats: Counter = Counter()
    # 先记下修改时间：补 ID 会改动文件，但那不是用户的编辑时间
    quarters = okr.load_quarters()
    mtimes = {q.name: dt.datetime.fromtimestamp(q.path.stat().st_mtime).replace(microsecond=0) for q in quarters}
    seen: Set[str] = set()
    with conn:
        for q in quarters:  # 按时间顺序，老季度优先保留原 ID
            assign_ids(conn, q, seen, stats)
        for q in okr.load_quarters():
            ts = now if source == "gui" else mtimes.get(q.name, now)
            record(conn, q, source, ts, now, stats)
    return dict(stats)


# ---------------------------------------------------------------- 查询与计算


def measurements(conn: sqlite3.Connection, ids: Iterable[str]) -> Dict[str, List[sqlite3.Row]]:
    ids = list(ids)
    out: Dict[str, List[sqlite3.Row]] = {i: [] for i in ids}
    if not ids:
        return out
    marks = ",".join("?" * len(ids))
    for row in conn.execute(f"SELECT * FROM measurement WHERE kr_id IN ({marks}) ORDER BY ts, id", ids):
        out[row["kr_id"]].append(row)
    return out


def _as_of(rows: Sequence[sqlite3.Row], ts: str, inclusive: bool = True) -> Optional[sqlite3.Row]:
    """rows 按时间升序；返回 ts 时刻（含/不含）之前的最后一条。"""
    keys = [r["ts"] for r in rows]
    i = bisect.bisect_right(keys, ts) if inclusive else bisect.bisect_left(keys, ts)
    return rows[i - 1] if i else None


def _kr_ids(q: okr.Quarter) -> List[str]:
    return [kr.id for o in q.objectives for kr in o.krs if kr.id]


def quarter_series(conn: sqlite3.Connection, q: okr.Quarter, today: dt.date) -> List[Dict[str, Any]]:
    """按天回放历史，算出每天结束时的季度完成度。

    目标分组以当前文件为准；某天还没有记录的 KR 不计入（那时还没开始跟踪）。
    """
    data = measurements(conn, _kr_ids(q))
    points = []
    day, end = q.start, min(today, q.end)
    while day <= end:
        cutoff = f"{day.isoformat()}T23:59:59"
        obj_values = []
        for o in q.objectives:
            vals = []
            for kr in o.krs:
                row = _as_of(data.get(kr.id, []), cutoff) if kr.id else None
                if row is not None and not row["dropped"]:
                    vals.append(row["progress"])
            if vals:
                obj_values.append(sum(vals) / len(vals))
        if obj_values:
            points.append({"date": day.isoformat(), "progress": sum(obj_values) / len(obj_values)})
        day += dt.timedelta(days=1)
    return points


def changes_since(conn: sqlite3.Connection, q: okr.Quarter, since: dt.datetime) -> List[Dict[str, Any]]:
    """since 之后有变化的 KR：对比 since 之前最后一条记录和现在的最新记录。

    本季度开始跟踪时（第一次同步）记下的是起点，不算“新增”；之后才加的 KR 才算新增。
    """
    since_s = ts_str(since)
    data = measurements(conn, _kr_ids(q))
    tracking_start = min((rows[0]["ts"] for rows in data.values() if rows), default=None)
    out = []
    for o in q.objectives:
        for kr in o.krs:
            rows = data.get(kr.id, [])
            if not rows or rows[-1]["ts"] < since_s:
                continue
            before = _as_of(rows, since_s, inclusive=False)
            if before is None and rows[0]["ts"] == tracking_start:
                before = rows[0]
            after = rows[-1]
            if before is not None and _same(_values(before), _values(after)):
                continue  # 期间改过又改回来
            out.append({
                "id": kr.id, "objective": o.title, "title": kr.title, "unit": kr.unit,
                "before": dict(before) if before is not None else None, "after": dict(after),
            })
    return out


def describe_change(c: Dict[str, Any]) -> str:
    b, a, unit = c["before"], c["after"], f" {c['unit']}" if c["unit"] else ""
    name = c["title"]
    if a["dropped"] and not (b and b["dropped"]):
        return f"{name}：已放弃"
    if b and b["dropped"] and not a["dropped"]:
        return f"{name}：恢复跟踪"
    if a["checked"] and not (b and b["checked"]):
        return f"{name}：✅ 完成"
    if b and b["checked"] and not a["checked"]:
        return f"{name}：取消完成"
    if a["target"] is None:
        return f"{name}：新增" if b is None else f"{name}：有更新"
    now_text = f"{okr._num(a['current'])}/{okr._num(a['target'])}{unit}"
    if b is None or b["current"] is None:
        return f"{name}：新增，目前 {now_text}"
    delta = a["current"] - b["current"]
    text = f"{name}：{okr._num(b['current'])} → {now_text}（{'+' if delta >= 0 else ''}{okr._num(delta)}）"
    if b["target"] is not None and abs(b["target"] - a["target"]) > 1e-9:
        text += f"，目标 {okr._num(b['target'])} → {okr._num(a['target'])}"
    return text


def changes_markdown(changes: List[Dict[str, Any]]) -> str:
    if not changes:
        return "- （这段时间没有记录到变化）"
    return "\n".join(f"- **{c['objective']}** · {describe_change(c)}" for c in changes)


def forecast(rows: Sequence[sqlite3.Row], q: okr.Quarter, today: dt.date) -> Optional[Dict[str, Any]]:
    """数值型 KR 按最近 4 周的速度线性外推到季末。数据不够（不足 1 天）时只给“还需要的速度”。"""
    if not rows:
        return None
    last = rows[-1]
    if last["target"] is None or last["checked"] or last["dropped"] or last["current"] >= last["target"]:
        return None
    days_left = (q.end - today).days
    if days_left <= 0:
        return None
    remaining = last["target"] - last["current"]
    result: Dict[str, Any] = {"days_left": days_left, "needed_per_week": remaining / days_left * 7}
    last_t = parse_ts(last["ts"])
    window_start = ts_str(last_t - dt.timedelta(days=FORECAST_WINDOW_DAYS))
    base = _as_of(rows, window_start) or rows[0]
    span = (last_t - parse_ts(base["ts"])).total_seconds() / 86400
    if span >= 1 and base["current"] is not None:
        rate = (last["current"] - base["current"]) / span
        projected = last["current"] + rate * days_left
        result.update(rate_per_week=rate * 7, projected=projected, on_track=projected >= last["target"])
        if rate > 0:
            # 预计完成日：从最后一次记录起，按这个速度还要多少天
            eta = last_t + dt.timedelta(days=remaining / rate)
            result["eta"] = eta.date().isoformat()
    return result


def week_start(today: dt.date) -> dt.datetime:
    return dt.datetime.combine(today - dt.timedelta(days=today.weekday()), dt.time())


def quarter_start(q: okr.Quarter) -> dt.datetime:
    return dt.datetime.combine(q.start, dt.time())


def safe_changes(q: Optional[okr.Quarter], since_fn) -> str:
    """给模板用：同步后统计变化。数据库出问题不影响生成文件。"""
    if q is None:
        return changes_markdown([])
    try:
        conn = connect()
        try:
            sync(conn)
            q = next((x for x in okr.load_quarters() if x.name == q.name), q)
            return changes_markdown(changes_since(conn, q, since_fn(q)))
        finally:
            conn.close()
    except (sqlite3.Error, OSError, RuntimeError) as e:
        return f"- （读取历史数据失败：{e}）"


# ---------------------------------------------------------------- 命令行


def _width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def print_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    cells = [[("" if v is None else str(v)) for v in r] for r in rows]
    widths = [max([_width(h)] + [_width(r[i]) for r in cells]) for i, h in enumerate(headers)]

    def line(vals):
        return "  ".join(v + " " * (w - _width(v)) for v, w in zip(vals, widths)).rstrip()

    print(line(headers))
    print("  ".join("-" * w for w in widths))
    for r in cells:
        print(line(r))


def cmd_sync(today: dt.date, args: List[str]) -> int:
    conn = connect()
    try:
        stats = sync(conn)
    finally:
        conn.close()
    names = {"ids_assigned": "补 ID", "krs_added": "新 KR", "measurements": "新记录",
             "measurements_merged": "合并的连续修改",
             "krs_removed": "已删除 KR", "skipped_files": "跳过的文件（正被修改）"}
    summary = "，".join(f"{names[k]} {v}" for k, v in stats.items() if v) or "没有变化"
    print(f"已同步到 {okr.rel(okr.DB_PATH)}：{summary}")
    return 0


def cmd_history(today: dt.date, args: List[str]) -> int:
    """不带参数：列出所有 KR；带参数：按 ID 或标题关键词显示完整历史。"""
    conn = connect()
    try:
        sync(conn)
        if not args:
            rows = conn.execute(
                "SELECT k.quarter, k.id, k.objective, k.title, COUNT(m.id) AS n, MAX(m.ts) AS last,"
                " k.removed_at FROM kr k LEFT JOIN measurement m ON m.kr_id = k.id"
                " GROUP BY k.id ORDER BY k.quarter DESC, k.position"
            ).fetchall()
            print_table(["季度", "ID", "目标", "KR", "记录数", "最后更新", "删除于"], rows)
            return 0
        key = args[0]
        krs = conn.execute(
            "SELECT * FROM kr WHERE id = ? OR title LIKE ? ORDER BY quarter DESC, position",
            (key, f"%{key}%"),
        ).fetchall()
        if not krs:
            print(f"没有找到：{key}")
            return 1
        for k in krs:
            print(f"\n{k['quarter']} · {k['objective']} · {k['title']}（{k['id']}）")
            rows = conn.execute(
                "SELECT ts, current, target, checked, dropped, progress, source FROM measurement"
                " WHERE kr_id = ? ORDER BY ts, id", (k["id"],)
            ).fetchall()
            print_table(
                ["时间", "数值", "进度", "状态", "来源"],
                [(r["ts"].replace("T", " "),
                  "" if r["target"] is None else f"{okr._num(r['current'])}/{okr._num(r['target'])}",
                  okr.pct(r["progress"]),
                  "放弃" if r["dropped"] else "完成" if r["checked"] else "",
                  "界面" if r["source"] == "gui" else "文件") for r in rows],
            )
        return 0
    finally:
        conn.close()


def cmd_sql(today: dt.date, args: List[str]) -> int:
    """只读执行一条 SQL，例如：okr sql "SELECT * FROM kr_latest" """
    if not args:
        print('用法：okr sql "SELECT * FROM kr_latest"\n表：kr、measurement；视图：kr_latest')
        return 1
    if not okr.DB_PATH.exists():
        print("还没有数据库，先运行 okr sync")
        return 1
    # mode=ro：以只读方式打开，任何写入语句都会被 SQLite 拒绝
    conn = sqlite3.connect(okr.DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        cur = conn.execute(" ".join(args))
        rows = cur.fetchall()
        if cur.description:
            print_table([d[0] for d in cur.description], rows)
            print(f"（{len(rows)} 行）")
        return 0
    except sqlite3.Error as e:
        print(f"SQL 错误：{e}")
        return 1
    finally:
        conn.close()
