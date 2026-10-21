import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import okr_tool  # noqa: E402

SAMPLE = """---
type: 季度OKR
---
# 2026-Q4 OKR

## O1 机器人 #工作

- [ ] KR1 项目 `2/4`
- [x] KR2 证书
- [ ] KR3 模板 `30/20 个`

## O2 健康 #健康 #长期

- [ ] KR1 跑步 `9/36 次`
- [-] KR2 早睡
- [ ] KR3 体检

```
## 代码块里的不算
- [x] 不算
```

## 备注

- 普通列表
"""


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.objs = okr_tool.parse_okr_text(SAMPLE)

    def test_objectives_without_krs_are_skipped(self):
        self.assertEqual([o.title for o in self.objs], ["O1 机器人", "O2 健康"])

    def test_tags(self):
        self.assertEqual(self.objs[0].domain, "工作")
        self.assertEqual(self.objs[1].tags, ["健康", "长期"])

    def test_numeric_kr(self):
        kr = self.objs[0].krs[0]
        self.assertEqual(kr.title, "KR1 项目")
        self.assertAlmostEqual(kr.progress, 0.5)

    def test_checked_kr(self):
        self.assertEqual(self.objs[0].krs[1].progress, 1.0)

    def test_overachieved_kr_is_capped_and_keeps_unit(self):
        kr = self.objs[0].krs[2]
        self.assertEqual(kr.progress, 1.0)
        self.assertEqual(kr.value_text, "30/20 个")

    def test_dropped_kr_excluded(self):
        o2 = self.objs[1]
        self.assertEqual(len(o2.krs), 3)
        self.assertEqual(len(o2.active_krs), 2)
        self.assertAlmostEqual(o2.progress, 0.125)

    def test_objective_progress(self):
        self.assertAlmostEqual(self.objs[0].progress, (0.5 + 1 + 1) / 3)


class QuarterTest(unittest.TestCase):
    def test_dates(self):
        q = okr_tool.Quarter(2026, 1, Path("x"))
        self.assertEqual(q.end, dt.date(2026, 3, 31))
        q4 = okr_tool.Quarter(2026, 4, Path("x"))
        self.assertEqual(q4.start, dt.date(2026, 10, 1))
        self.assertEqual(q4.time_progress(dt.date(2026, 12, 31)), 1.0)
        self.assertEqual(q4.time_progress(dt.date(2026, 9, 1)), 0.0)

    def test_status(self):
        self.assertIn("正常", okr_tool.status(0.45, 0.5))
        self.assertIn("预警", okr_tool.status(0.30, 0.5))
        self.assertIn("落后", okr_tool.status(0.10, 0.5))
        self.assertIn("完成", okr_tool.status(1.0, 0.5))


class DashboardTest(unittest.TestCase):
    def test_render(self):
        with tempfile.TemporaryDirectory() as d:
            year = Path(d) / "2026"
            year.mkdir()
            (year / "2026-Q4.md").write_text(SAMPLE, encoding="utf-8")
            (year / "2026-年度目标.md").write_text("## 不是季度", encoding="utf-8")
            quarters = okr_tool.load_quarters(Path(d))
            self.assertEqual([q.name for q in quarters], ["2026-Q4"])
            text = okr_tool.render_dashboard(quarters, dt.date(2026, 11, 20))
        self.assertIn("当前季度", text)
        self.assertIn("#工作", text)
        self.assertIn("~~KR2 早睡~~", text)
        self.assertIn("还没动的 KR", text)
        self.assertIn("O2 健康 → KR3 体检", text)

    def test_render_empty(self):
        self.assertIn("还没有季度 OKR", okr_tool.render_dashboard([], dt.date.today()))


if __name__ == "__main__":
    unittest.main()
