import contextlib
import datetime as dt
import io
import os
import sqlite3
import unittest

from helpers import TempRepoMixin

import okr_db
import okr_tool

Q4 = """# 2026-Q4 OKR

## O1 机器人 #工作

- [ ] KR1 项目 `2/4 个`
- [ ] KR2 证书

## O2 健康 #健康

- [ ] KR1 跑步 `9/36 次`

## 备注
"""


def at(s):
    return dt.datetime.fromisoformat(s)


class DbTestCase(TempRepoMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.conn = okr_db.connect()

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def set_mtime(self, path, when):
        t = at(when).timestamp()
        os.utime(path, (t, t))

    def edit(self, path, old, new, when=None):
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        if when:
            self.set_mtime(path, when)

    def ids(self, path):
        return [kr.id for o in okr_tool.parse_okr_text(path.read_text(encoding="utf-8")) for kr in o.krs]

    def rows(self, sql, *args):
        return self.conn.execute(sql, args).fetchall()

    def quarter(self, name="2026-Q4"):
        return next(q for q in okr_tool.load_quarters() if q.name == name)


class MigrationTest(DbTestCase):
    def test_fresh_database_has_schema(self):
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], okr_db.MIGRATIONS[-1][0])
        names = {r["name"] for r in self.rows("SELECT name FROM sqlite_master")}
        self.assertTrue({"kr", "measurement", "kr_latest", "measurement_kr_ts"} <= names)

    def test_reconnect_is_idempotent(self):
        okr_db.connect().close()
        self.assertEqual(okr_db.migrate(self.conn), okr_db.MIGRATIONS[-1][0])

    def test_newer_database_is_rejected(self):
        self.conn.execute("PRAGMA user_version = 99")
        with self.assertRaises(RuntimeError):
            okr_db.migrate(self.conn)

    def test_constraints(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO measurement (kr_id, ts, checked, dropped, progress, source)"
                " VALUES ('nope', '2026-10-01T00:00:00', 0, 0, 0.5, 'gui')"
            )


class SyncTest(DbTestCase):
    def setUp(self):
        super().setUp()
        self.path = self.write_quarter("2026-Q4", Q4)
        self.set_mtime(self.path, "2026-10-05T09:00:00")

    def sync(self, source="file", now="2026-10-20T12:00:00"):
        return okr_db.sync(self.conn, source, now=at(now))

    def test_first_sync_assigns_ids_and_records_baseline(self):
        stats = self.sync()
        ids = self.ids(self.path)
        self.assertEqual(len(ids), 3)
        self.assertTrue(all(i.startswith("kr-") for i in ids))
        self.assertEqual(len(set(ids)), 3)
        self.assertIn("- [ ] KR1 项目 `2/4 个` ^", self.path.read_text(encoding="utf-8"))
        self.assertEqual(stats["krs_added"], 3)
        self.assertEqual(stats["measurements"], 3)
        # 基线时间用文件修改时间（补 ID 之前的），不是同步时间
        self.assertEqual({r["ts"] for r in self.rows("SELECT ts FROM measurement")}, {"2026-10-05T09:00:00"})

    def test_unchanged_file_records_nothing(self):
        self.sync()
        ids = self.ids(self.path)
        stats = self.sync(now="2026-10-21T12:00:00")
        self.assertEqual(stats.get("measurements", 0), 0)
        self.assertEqual(self.ids(self.path), ids)

    def test_only_changes_are_recorded_with_file_mtime(self):
        self.sync()
        self.edit(self.path, "`2/4 个`", "`3/4 个`", when="2026-10-12T20:30:00")
        stats = self.sync()
        self.assertEqual(stats["measurements"], 1)
        rows = self.rows("SELECT ts, current, source FROM measurement WHERE current IN (2, 3) ORDER BY ts")
        self.assertEqual([tuple(r) for r in rows],
                         [("2026-10-05T09:00:00", 2.0, "file"), ("2026-10-12T20:30:00", 3.0, "file")])

    def test_gui_source_uses_now_and_never_goes_back_in_time(self):
        self.sync()
        self.edit(self.path, "- [ ] KR2 证书", "- [x] KR2 证书")
        self.sync("gui", now="2026-10-20T15:00:00")
        row = self.rows("SELECT ts, source, checked FROM measurement WHERE checked = 1")[0]
        self.assertEqual(tuple(row), ("2026-10-20T15:00:00", "gui", 1))
        # 文件修改时间早于上一条记录（时钟异常）时，不能排到前面去
        self.edit(self.path, "- [x] KR2 证书", "- [ ] KR2 证书", when="2026-10-01T00:00:00")
        self.sync()
        last = self.rows("SELECT ts FROM measurement WHERE checked = 0 ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(last["ts"], "2026-10-20T15:00:00")

    def test_rapid_gui_clicks_are_merged(self):
        self.sync()
        kr_id = self.ids(self.path)[2]

        def click(old, new, when):
            self.edit(self.path, old, new)
            self.sync("gui", now=when)

        click("`9/36 次`", "`10/36 次`", "2026-10-20T20:00:00")
        click("`10/36 次`", "`11/36 次`", "2026-10-20T20:00:05")
        click("`11/36 次`", "`12/36 次`", "2026-10-20T20:00:09")
        rows = self.rows("SELECT ts, current FROM measurement WHERE kr_id = ? ORDER BY ts", kr_id)
        self.assertEqual([tuple(r) for r in rows],
                         [("2026-10-05T09:00:00", 9.0), ("2026-10-20T20:00:09", 12.0)])
        # 超过 10 分钟就是新的一条
        click("`12/36 次`", "`13/36 次`", "2026-10-20T20:30:00")
        self.assertEqual(len(self.rows("SELECT 1 FROM measurement WHERE kr_id = ?", kr_id)), 3)
        # +1 再 -1：改回上一条的值，这次修改直接消失
        click("`13/36 次`", "`14/36 次`", "2026-10-20T20:31:00")
        click("`14/36 次`", "`13/36 次`", "2026-10-20T20:31:30")
        currents = [r["current"] for r in self.rows("SELECT current FROM measurement WHERE kr_id = ? ORDER BY ts", kr_id)]
        self.assertEqual(currents, [9.0, 12.0, 13.0])

    def test_baseline_and_file_edits_are_never_merged(self):
        self.sync()
        kr_id = self.ids(self.path)[2]
        self.edit(self.path, "`9/36 次`", "`10/36 次`")
        self.sync("gui", now="2026-10-05T09:01:00")  # 离基线不到 10 分钟，基线也不能被改写
        self.edit(self.path, "`10/36 次`", "`11/36 次`", when="2026-10-05T09:02:00")
        self.sync("file", now="2026-10-05T09:03:00")
        self.assertEqual([r["current"] for r in self.rows(
            "SELECT current FROM measurement WHERE kr_id = ? ORDER BY ts, id", kr_id)], [9.0, 10.0, 11.0])

    def test_rename_and_move_keep_history(self):
        self.sync()
        kr_id = self.ids(self.path)[2]
        self.edit(self.path, "KR1 跑步 `9/36 次`", "KR1 慢跑 `12/36 次`", when="2026-10-15T08:00:00")
        self.sync()
        k = self.rows("SELECT title FROM kr WHERE id = ?", kr_id)[0]
        self.assertEqual(k["title"], "KR1 慢跑")
        self.assertEqual([r["current"] for r in self.rows(
            "SELECT current FROM measurement WHERE kr_id = ? ORDER BY ts", kr_id)], [9.0, 12.0])

    def test_removed_and_restored(self):
        self.sync()
        kr_id = self.ids(self.path)[1]
        line = next(l for l in self.path.read_text(encoding="utf-8").splitlines() if kr_id in l)
        self.edit(self.path, line + "\n", "")
        self.sync(now="2026-10-21T10:00:00")
        self.assertEqual(self.rows("SELECT removed_at FROM kr WHERE id = ?", kr_id)[0][0], "2026-10-21T10:00:00")
        self.edit(self.path, "## O2 健康", line + "\n\n## O2 健康")
        self.sync(now="2026-10-22T10:00:00")
        self.assertIsNone(self.rows("SELECT removed_at FROM kr WHERE id = ?", kr_id)[0][0])

    def test_duplicate_id_from_copy_paste_gets_new_id(self):
        self.sync()
        first = self.ids(self.path)[0]
        line = next(l for l in self.path.read_text(encoding="utf-8").splitlines() if first in l)
        self.edit(self.path, line, line + "\n" + line.replace("KR1 项目", "KR3 复制的"))
        self.sync()
        ids = self.ids(self.path)
        self.assertEqual(len(ids), 4)
        self.assertEqual(len(set(ids)), 4)
        self.assertEqual(ids[0], first)

    def test_kr_copied_into_next_quarter_starts_own_history(self):
        self.sync()
        q4_ids = self.ids(self.path)
        q1 = self.write_quarter("2027-Q1", self.path.read_text(encoding="utf-8").replace("2026-Q4", "2027-Q1"))
        self.sync()
        q1_ids = self.ids(q1)
        self.assertEqual(self.ids(self.path), q4_ids)
        self.assertFalse(set(q1_ids) & set(q4_ids))
        quarters = {r["quarter"] for r in self.rows("SELECT quarter FROM kr")}
        self.assertEqual(quarters, {"2026-Q4", "2027-Q1"})

    def test_file_changed_during_sync_is_not_overwritten(self):
        q = self.quarter()
        original = okr_tool.parse_okr_text

        def parse_while_obsidian_saves(text):
            # 模拟：程序读完文件后、写回之前，用户在 Obsidian 里保存了新内容
            okr_tool.parse_okr_text = original
            self.path.write_text(Q4 + "\n- 用户刚输入的内容\n", encoding="utf-8")
            self.set_mtime(self.path, "2026-10-06T00:00:00")
            return original(text)

        okr_tool.parse_okr_text = parse_while_obsidian_saves
        stats = okr_db.Counter()
        try:
            okr_db.assign_ids(self.conn, q, set(), stats)
        finally:
            okr_tool.parse_okr_text = original
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("用户刚输入的内容", text)
        self.assertNotIn("^kr-", text)
        self.assertEqual(stats["skipped_files"], 1)
        # 下一次同步正常补上 ID，用户内容保留
        okr_db.sync(self.conn, now=at("2026-10-20T12:00:00"))
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("用户刚输入的内容", text)
        self.assertEqual(len(self.ids(self.path)), 3)


class AnalysisTest(DbTestCase):
    """用一段“真实使用”的过程造历史数据，再验证各项统计。"""

    def setUp(self):
        super().setUp()
        self.path = self.write_quarter("2026-Q4", Q4)
        steps = [
            ("2026-10-01T09:00:00", None),
            ("2026-10-08T21:00:00", ("`9/36 次`", "`13/36 次`")),
            ("2026-10-15T21:00:00", ("`13/36 次`", "`17/36 次`")),
            ("2026-10-20T08:00:00", ("- [ ] KR2 证书", "- [x] KR2 证书")),
            ("2026-10-21T08:00:00", ("`2/4 个`", "`3/4 个`")),
        ]
        for when, change in steps:
            if change:
                self.edit(self.path, *change)
            self.set_mtime(self.path, when)
            okr_db.sync(self.conn, now=at(when))

    def test_quarter_series_replays_history(self):
        series = okr_db.quarter_series(self.conn, self.quarter(), dt.date(2026, 10, 21))
        by_day = {p["date"]: round(p["progress"], 4) for p in series}
        self.assertEqual(series[0]["date"], "2026-10-01")
        self.assertEqual(series[-1]["date"], "2026-10-21")
        self.assertEqual(len(series), 21)
        # 10-01：O1 = (0.5 + 0) / 2，O2 = 9/36
        self.assertEqual(by_day["2026-10-01"], round((0.25 + 9 / 36) / 2, 4))
        self.assertEqual(by_day["2026-10-07"], by_day["2026-10-01"])  # 中间没变化，沿用
        self.assertEqual(by_day["2026-10-08"], round((0.25 + 13 / 36) / 2, 4))
        self.assertEqual(by_day["2026-10-21"], round(((0.75 + 1) / 2 + 17 / 36) / 2, 4))

    def test_series_starts_when_tracking_began(self):
        later = self.write_quarter("2026-Q3", Q4.replace("2026-Q4", "2026-Q3"))
        self.set_mtime(later, "2026-09-20T00:00:00")
        okr_db.sync(self.conn, now=at("2026-10-21T09:00:00"))
        series = okr_db.quarter_series(self.conn, self.quarter("2026-Q3"), dt.date(2026, 10, 21))
        self.assertEqual(series[0]["date"], "2026-09-20")
        self.assertEqual(series[-1]["date"], "2026-09-30")  # 不超出季度末

    def test_changes_this_week(self):
        changes = okr_db.changes_since(self.conn, self.quarter(), okr_db.week_start(dt.date(2026, 10, 21)))
        texts = [okr_db.describe_change(c) for c in changes]
        self.assertEqual(texts, ["KR1 项目：2 → 3/4 个（+1）", "KR2 证书：✅ 完成"])
        md = okr_db.changes_markdown(changes)
        self.assertIn("- **O1 机器人** · KR1 项目：2 → 3/4 个（+1）", md)

    def test_changes_whole_quarter_start_from_baseline(self):
        changes = okr_db.changes_since(self.conn, self.quarter(), okr_db.quarter_start(self.quarter()))
        texts = [okr_db.describe_change(c) for c in changes]
        # 开始跟踪时的数值是起点，不算“新增”
        self.assertEqual(texts, ["KR1 项目：2 → 3/4 个（+1）", "KR2 证书：✅ 完成", "KR1 跑步：9 → 17/36 次（+8）"])

    def test_kr_added_later_counts_as_new(self):
        self.edit(self.path, "\n## 备注", "- [ ] KR2 体检 `0/1 次`\n\n## 备注")
        okr_db.sync(self.conn, "gui", now=at("2026-10-22T08:00:00"))
        changes = okr_db.changes_since(self.conn, self.quarter(), at("2026-10-22T00:00:00"))
        self.assertEqual([okr_db.describe_change(c) for c in changes], ["KR2 体检：新增，目前 0/1 次"])

    def test_change_reverted_within_period_is_hidden(self):
        self.edit(self.path, "`17/36 次`", "`18/36 次`")
        okr_db.sync(self.conn, "gui", now=at("2026-10-22T08:00:00"))
        self.edit(self.path, "`18/36 次`", "`17/36 次`")
        okr_db.sync(self.conn, "gui", now=at("2026-10-22T09:00:00"))
        changes = okr_db.changes_since(self.conn, self.quarter(), at("2026-10-22T00:00:00"))
        self.assertEqual(changes, [])

    def test_describe_other_changes(self):
        base = {"current": 5.0, "target": 10.0, "checked": 0, "dropped": 0}
        c = {"title": "跑步", "unit": "", "objective": "O", "before": base, "after": dict(base, target=12.0, current=4.0)}
        self.assertEqual(okr_db.describe_change(c), "跑步：5 → 4/12（-1），目标 10 → 12")
        c["after"] = dict(base, dropped=1)
        self.assertEqual(okr_db.describe_change(c), "跑步：已放弃")
        c["before"], c["after"] = dict(base, dropped=1), base
        self.assertEqual(okr_db.describe_change(c), "跑步：恢复跟踪")
        self.assertEqual(okr_db.changes_markdown([]), "- （这段时间没有记录到变化）")

    def test_forecast_uses_recent_pace(self):
        q = self.quarter()
        kr_id = self.ids(self.path)[2]
        rows = okr_db.measurements(self.conn, [kr_id])[kr_id]
        f = okr_db.forecast(rows, q, dt.date(2026, 10, 21))
        # 10-01 9 次 → 10-15 17 次：8 次 / 14.5 天
        rate = 8 / ((at("2026-10-15T21:00:00") - at("2026-10-01T09:00:00")).total_seconds() / 86400)
        self.assertAlmostEqual(f["rate_per_week"], rate * 7)
        self.assertEqual(f["days_left"], 71)
        self.assertAlmostEqual(f["projected"], 17 + rate * 71)
        self.assertTrue(f["on_track"])
        self.assertAlmostEqual(f["needed_per_week"], 19 / 71 * 7)
        eta = at("2026-10-15T21:00:00") + dt.timedelta(days=19 / rate)
        self.assertEqual(f["eta"], eta.date().isoformat())

    def test_forecast_edge_cases(self):
        q = self.quarter()
        data = okr_db.measurements(self.conn, self.ids(self.path))
        ids = self.ids(self.path)
        self.assertIsNone(okr_db.forecast(data[ids[1]], q, dt.date(2026, 10, 21)))  # 勾选型
        self.assertIsNone(okr_db.forecast([], q, dt.date(2026, 10, 21)))
        self.assertIsNone(okr_db.forecast(data[ids[2]], q, dt.date(2026, 12, 31)))  # 季度已结束
        one = okr_db.forecast(data[ids[0]][-1:], q, dt.date(2026, 10, 21))  # 只有一条记录
        self.assertNotIn("projected", one)
        self.assertAlmostEqual(one["needed_per_week"], 1 / 71 * 7)

    def test_week_report_gets_progress(self):
        path, content = okr_tool.plan_week(dt.date(2026, 10, 21))
        self.assertIn("KR1 项目：2 → 3/4 个（+1）", content)
        self.assertIn("KR2 证书：✅ 完成", content)
        self.assertNotIn("{{", content)

    def test_dashboard_has_week_section(self):
        okr_tool.write_dashboard(dt.date(2026, 10, 21))
        text = okr_tool.DASHBOARD.read_text(encoding="utf-8")
        self.assertIn("### 📈 本周进展", text)
        self.assertIn("KR2 证书：✅ 完成", text)


class CliTest(DbTestCase):
    def setUp(self):
        super().setUp()
        self.write_quarter("2026-Q4", Q4)

    def run_cmd(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = okr_tool.main(list(argv))
        return code, out.getvalue()

    def test_sync_and_history(self):
        code, out = self.run_cmd("sync")
        self.assertEqual(code, 0)
        self.assertIn("新 KR 3", out)
        code, out = self.run_cmd("history")
        self.assertIn("KR1 跑步", out)
        code, out = self.run_cmd("history", "跑步")
        self.assertEqual(code, 0)
        self.assertIn("9/36", out)
        self.assertEqual(self.run_cmd("history", "不存在的")[0], 1)

    def test_sql_is_read_only(self):
        self.run_cmd("sync")
        code, out = self.run_cmd("sql", "SELECT title, progress FROM kr_latest ORDER BY position")
        self.assertEqual(code, 0)
        self.assertIn("KR1 项目", out)
        self.assertIn("（3 行）", out)
        code, out = self.run_cmd("sql", "DELETE FROM measurement")
        self.assertEqual(code, 1)
        self.assertIn("readonly", out)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM measurement")[0][0], 3)

    def test_table_aligns_chinese(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            okr_db.print_table(["名字", "n"], [("跑步", 1), ("ab", 22)])
        lines = out.getvalue().splitlines()
        # 第二列在屏幕上的起始位置一致（中文按 2 个字符宽）
        self.assertEqual([okr_db._width(l[: l.rindex(" ") + 1]) for l in lines[2:]], [6, 6])


if __name__ == "__main__":
    unittest.main()
