import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from helpers import TempRepoMixin

import okr_tool
import okr_web

SAMPLE = """---
type: 季度OKR
---
# 2026-Q4 OKR

## O1 机器人 #工作

- [ ] KR1 项目 `2/4 个`
- [x] KR2 证书

## O2 健康 #健康

  * [ ] KR1 跑步 `9/36`

## 备注

- 普通列表
"""


def objs(lines):
    return okr_tool.parse_okr_text("\n".join(lines))


class EditTest(unittest.TestCase):
    def setUp(self):
        self.lines = SAMPLE.splitlines()
        self.o1, self.o2 = objs(self.lines)

    def test_parse_records_lines(self):
        self.assertEqual(self.lines[self.o1.line], "## O1 机器人 #工作")
        self.assertEqual(self.o1.krs[0].raw, "- [ ] KR1 项目 `2/4 个`")
        self.assertEqual(self.lines[self.o2.end_line], "## 备注")

    def test_save_kr_value_keeps_list_prefix(self):
        kr = self.o2.krs[0]
        out = okr_web.save_kr(self.lines, kr.line, kr.raw,
                              {"title": "KR1 跑步", "current": 10, "target": 36, "unit": "次"})
        self.assertEqual(out[kr.line], "  * [ ] KR1 跑步 `10/36 次`")

    def test_save_kr_checked_and_dropped(self):
        kr = self.o1.krs[0]
        out = okr_web.save_kr(self.lines, kr.line, kr.raw, {"title": "KR1 项目", "checked": True})
        self.assertEqual(out[kr.line], "- [x] KR1 项目")
        out = okr_web.save_kr(self.lines, kr.line, kr.raw, {"title": "KR1 项目", "dropped": True})
        self.assertEqual(out[kr.line], "- [-] KR1 项目")

    def test_stale_line_is_rejected(self):
        kr = self.o1.krs[0]
        with self.assertRaises(okr_web.ApiError) as ctx:
            okr_web.save_kr(self.lines, kr.line, "- [ ] 别处改过的内容", {"title": "x"})
        self.assertEqual(ctx.exception.status, 409)

    def test_invalid_input(self):
        kr = self.o1.krs[0]
        for bad in ({"title": ""}, {"title": "a`b"}, {"title": "x", "target": 0},
                    {"title": "x", "target": 5, "current": -1}, {"title": "x", "target": "abc"}):
            with self.assertRaises(okr_web.ApiError, msg=bad):
                okr_web.save_kr(self.lines, kr.line, kr.raw, bad)

    def test_save_kr_keeps_block_id(self):
        lines = ["## O1 a #工作", "- [ ] KR1 跑步 `9/36 次` ^kr-abc123"]
        out = okr_web.save_kr(lines, 1, lines[1], {"title": "KR1 慢跑", "current": 10, "target": 36, "unit": "次"})
        self.assertEqual(out[1], "- [ ] KR1 慢跑 `10/36 次` ^kr-abc123")

    def test_add_kr_auto_numbers(self):
        out = okr_web.add_kr(self.lines, self.o1.line, self.o1.raw, {"title": "模板库", "target": 20})
        o1 = objs(out)[0]
        self.assertEqual([k.title for k in o1.krs], ["KR1 项目", "KR2 证书", "KR3 模板库"])
        self.assertEqual(o1.krs[2].raw, "- [ ] KR3 模板库 `0/20`")

    def test_add_objective_then_kr(self):
        out = okr_web.add_objective(self.lines, {"title": "学 Python", "domain": "#学习"})
        o3 = objs(out)[2]
        self.assertEqual(o3.raw, "## O3 学 Python #学习")
        out = okr_web.add_kr(out, o3.line, o3.raw, {"title": "写脚本"})
        o3 = objs(out)[2]
        self.assertEqual([k.raw for k in o3.krs], ["- [ ] KR1 写脚本"])
        # 新目标插在“备注”之前，备注不受影响
        self.assertLess(out.index("## O3 学 Python #学习"), out.index("## 备注"))
        self.assertEqual(out[-1], "- 普通列表")

    def test_add_objective_to_empty_file(self):
        out = okr_web.add_objective(["# 2026-Q4 OKR", "", "## 备注", ""], {"title": "新", "domain": "工作"})
        self.assertEqual(out, ["# 2026-Q4 OKR", "", "## O1 新 #工作", "", "## 备注", ""])
        self.assertEqual([o.title for o in objs(out)], ["O1 新"])

    def test_save_objective_keeps_extra_tags(self):
        lines = ["## O1 旧 #工作 #长期", "- [ ] KR1 a"]
        out = okr_web.save_objective(lines, 0, lines[0], {"title": "O1 新", "domain": "学习"})
        self.assertEqual(out[0], "## O1 新 #学习 #长期")

    def test_delete_objective_and_kr(self):
        out = okr_web.delete_objective(self.lines, self.o1.line, self.o1.raw)
        self.assertEqual([o.title for o in objs(out)], ["O2 健康"])
        self.assertIn("## 备注", out)
        kr = self.o1.krs[1]
        out = okr_web.delete_kr(self.lines, kr.line, kr.raw)
        self.assertEqual(len(objs(out)[0].krs), 1)


class ServerTest(TempRepoMixin, unittest.TestCase):
    """真正起一个 HTTP 服务，走一遍界面会用到的接口。"""

    def setUp(self):
        super().setUp()
        self.qfile = self.write_quarter("2026-Q4", SAMPLE)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), okr_web.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def request(self, path, body=None, headers=None):
        hdrs = {"Content-Type": "application/json", "X-OKR": "1"} if body is not None else {}
        hdrs.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=hdrs)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def state(self):
        status, body = self.request("/api/state?quarter=2026-Q4")
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_index_page(self):
        status, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("我的 OKR".encode(), body)

    def test_state_and_update(self):
        st = self.state()
        q = st["quarter"]
        self.assertEqual(q["name"], "2026-Q4")
        self.assertEqual([o["title"] for o in q["objectives"]], ["O1 机器人", "O2 健康"])
        kr = q["objectives"][0]["krs"][0]
        payload = dict(kr, current=3)
        status, _ = self.request("/api/kr/save", {"quarter": "2026-Q4", "line": kr["line"],
                                                  "expect": kr["raw"], "kr": payload})
        self.assertEqual(status, 200)
        self.assertIn("- [ ] KR1 项目 `3/4 个`", self.qfile.read_text(encoding="utf-8"))
        self.assertTrue((self.root / "dashboard.md").exists())
        # 用旧的行内容再改一次，应被拒绝
        status, body = self.request("/api/kr/save", {"quarter": "2026-Q4", "line": kr["line"],
                                                     "expect": kr["raw"], "kr": payload})
        self.assertEqual(status, 409)
        self.assertIn("刷新", json.loads(body)["error"])

    def test_history_is_recorded_and_exposed(self):
        q = self.state()["quarter"]
        kr = q["objectives"][0]["krs"][0]
        self.assertTrue(kr["id"].startswith("kr-"))
        self.assertEqual(len(kr["spark"]), 1)
        self.assertIsNotNone(kr["forecast"])  # 数值型且未完成
        self.assertTrue(q["series"])
        self.request("/api/kr/save", {"quarter": "2026-Q4", "line": kr["line"], "expect": kr["raw"],
                                      "kr": dict(kr, current=3)})
        status, body = self.request(f"/api/history?id={kr['id']}")
        self.assertEqual(status, 200)
        rows = json.loads(body)["rows"]
        self.assertEqual([(r["current"], r["source"]) for r in rows], [(2.0, "file"), (3.0, "gui")])
        q = self.state()["quarter"]
        self.assertEqual(q["objectives"][0]["krs"][0]["spark"], [0.5, 0.75])
        self.assertEqual([c["text"] for c in q["week_changes"]], ["KR1 项目：2 → 3/4 个（+1）"])
        status, _ = self.request("/api/history?id=kr-nothere")
        self.assertEqual(status, 404)

    def test_obsidian_edit_before_gui_edit_is_not_labelled_gui(self):
        kr = self.state()["quarter"]["objectives"][1]["krs"][0]
        # 用户在 Obsidian 里改了跑步，界面还没刷新就去改了 O1 的 KR
        text = self.qfile.read_text(encoding="utf-8").replace("`9/36`", "`12/36`")
        self.qfile.write_text(text, encoding="utf-8")
        other = [l for l in text.splitlines() if "KR2 证书" in l][0]
        self.request("/api/kr/save", {"quarter": "2026-Q4", "line": text.splitlines().index(other),
                                      "expect": other, "kr": {"title": "KR2 证书", "checked": True}})
        status, body = self.request(f"/api/history?id={kr['id']}")
        self.assertEqual([r["source"] for r in json.loads(body)["rows"]], ["file", "file"])

    def test_post_requires_header_and_local_host(self):
        body = {"quarter": "2026-Q4", "title": "x", "domain": "工作"}
        status, _ = self.request("/api/objective/add", body, {"X-OKR": ""})
        self.assertEqual(status, 403)
        status, _ = self.request("/api/objective/add", body, {"Host": "evil.example.com"})
        self.assertEqual(status, 403)
        self.assertNotIn("x #工作", self.qfile.read_text(encoding="utf-8"))

    def test_bad_quarter_name(self):
        status, _ = self.request("/api/objective/add", {"quarter": "../../etc", "title": "x", "domain": "a"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
