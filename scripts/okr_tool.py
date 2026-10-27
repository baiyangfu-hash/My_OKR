#!/usr/bin/env python3
"""个人 OKR 工具：解析 Markdown 中的 OKR，生成仪表盘、周报、季度文件。

只依赖 Python 标准库（3.9+）。

用法：
    python scripts/okr_tool.py dashboard          # 生成 dashboard.md
    python scripts/okr_tool.py week               # 新建本周周报
    python scripts/okr_tool.py quarter            # 新建本季度 OKR 文件
    python scripts/okr_tool.py review             # 新建本季度复盘
    python scripts/okr_tool.py year               # 新建本年度目标
    python scripts/okr_tool.py sync               # 把当前进度同步进历史数据库
    python scripts/okr_tool.py history [关键词]   # 查看 KR 的历史记录
    python scripts/okr_tool.py sql "SELECT ..."   # 只读执行 SQL

KR 写法（在季度 OKR 文件中）：
    ## O1 目标标题 #工作
    - [ ] KR1 数值型 `2/3`       -> 66.7%（可带单位：`8/20 个`）
    - [x] KR2 勾选型              -> 100%
    - [ ] KR3 未开始              -> 0%
    - [-] KR4 已放弃              -> 不计入
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
OKR_DIR = ROOT / "okr"
TEMPLATE_DIR = ROOT / "templates"
DASHBOARD = ROOT / "dashboard.md"
DB_PATH = ROOT / "data" / "okr.db"

QUARTER_FILE_RE = re.compile(r"^(\d{4})-Q([1-4])\.md$")
OBJECTIVE_RE = re.compile(r"^##\s+(.+?)\s*$")
KR_RE = re.compile(r"^\s*[-*]\s+\[( |x|X|-)\]\s+(.+?)\s*$")
BLOCK_ID_RE = re.compile(r"\s+\^([A-Za-z0-9-]+)\s*$")  # Obsidian 块 ID，如 ^kr-3fa9c1
VALUE_RE = re.compile(r"`\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*([^`]*?)\s*`")
TAG_RE = re.compile(r"(?:^|\s)#([^\s#]+)")

UNTAGGED = "未分类"


# ---------------------------------------------------------------- 数据模型


@dataclass
class KeyResult:
    title: str
    checked: bool = False
    dropped: bool = False
    current: Optional[float] = None
    target: Optional[float] = None
    unit: str = ""
    id: str = ""  # 行尾的块 ID，用来在数据库里跨编辑识别同一个 KR
    line: int = -1  # 在文件中的行号（从 0 开始），供界面修改时定位
    raw: str = ""

    @property
    def progress(self) -> float:
        """0.0 ~ 1.0；勾选即 100%，有数值按数值，否则 0。"""
        if self.checked:
            return 1.0
        if self.current is not None and self.target:
            return max(0.0, min(self.current / self.target, 1.0))
        return 0.0

    @property
    def value_text(self) -> str:
        if self.current is None or self.target is None:
            return "✔" if self.checked else "—"
        text = f"{_num(self.current)}/{_num(self.target)}"
        return f"{text} {self.unit}" if self.unit else text


@dataclass
class Objective:
    title: str
    tags: List[str] = field(default_factory=list)
    krs: List[KeyResult] = field(default_factory=list)
    line: int = -1
    end_line: int = -1  # 下一个标题所在行（不含）
    raw: str = ""

    @property
    def active_krs(self) -> List[KeyResult]:
        return [kr for kr in self.krs if not kr.dropped]

    @property
    def progress(self) -> float:
        krs = self.active_krs
        return sum(kr.progress for kr in krs) / len(krs) if krs else 0.0

    @property
    def domain(self) -> str:
        return self.tags[0] if self.tags else UNTAGGED


@dataclass
class Quarter:
    year: int
    q: int
    path: Path
    objectives: List[Objective] = field(default_factory=list)

    @property
    def name(self) -> str:
        return f"{self.year}-Q{self.q}"

    @property
    def start(self) -> dt.date:
        return dt.date(self.year, 3 * self.q - 2, 1)

    @property
    def end(self) -> dt.date:
        if self.q == 4:
            return dt.date(self.year, 12, 31)
        return dt.date(self.year, 3 * self.q + 1, 1) - dt.timedelta(days=1)

    @property
    def progress(self) -> float:
        objs = [o for o in self.objectives if o.active_krs]
        return sum(o.progress for o in objs) / len(objs) if objs else 0.0

    def time_progress(self, today: dt.date) -> float:
        if today <= self.start:
            return 0.0
        if today >= self.end:
            return 1.0
        total = (self.end - self.start).days + 1
        return ((today - self.start).days + 1) / total


# ---------------------------------------------------------------- 解析


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def parse_objective_heading(text: str) -> Objective:
    tags = TAG_RE.findall(text)
    title = TAG_RE.sub("", text).strip()
    return Objective(title=title, tags=tags)


def parse_kr(mark: str, text: str) -> KeyResult:
    kr = KeyResult(title=text, checked=mark in "xX", dropped=mark == "-")
    m = BLOCK_ID_RE.search(text)
    if m:
        kr.id = m.group(1)
        text = kr.title = text[: m.start()]
    m = VALUE_RE.search(text)
    if m:
        kr.current, kr.target = float(m.group(1)), float(m.group(2))
        kr.unit = m.group(3)
        kr.title = (text[: m.start()] + text[m.end() :]).strip()
    kr.title = re.sub(r"\s{2,}", " ", kr.title)
    return kr


def format_kr(kr: KeyResult, prefix: str = "- ") -> str:
    """把 KR 写回成一行 Markdown 任务。"""
    mark = "x" if kr.checked else "-" if kr.dropped else " "
    text = kr.title
    if kr.target is not None:
        value = f"{_num(kr.current or 0)}/{_num(kr.target)}"
        if kr.unit:
            value += f" {kr.unit}"
        text += f" `{value}`"
    if kr.id:
        text += f" ^{kr.id}"
    return f"{prefix}[{mark}] {text}"


def format_objective_heading(title: str, tags: List[str]) -> str:
    return " ".join(["##", title] + [f"#{t}" for t in tags])


def parse_okr_text(text: str) -> List[Objective]:
    objectives: List[Objective] = []
    current: Optional[Objective] = None
    in_frontmatter = False
    in_code = False
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if i == 0 and line.strip() == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if line.strip() == "---":
                in_frontmatter = False
            continue
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = OBJECTIVE_RE.match(line)
        if m or line.startswith("# "):
            if current is not None:
                current.end_line = i
            current = None
        if m:
            current = parse_objective_heading(m.group(1))
            current.line, current.raw = i, line
            objectives.append(current)
            continue
        m = KR_RE.match(line)
        if m and current is not None:
            kr = parse_kr(m.group(1), m.group(2))
            kr.line, kr.raw = i, line
            current.krs.append(kr)
    if current is not None:
        current.end_line = len(lines)
    # 既没有 KR 也没有标签的二级标题（如“备注”）不算目标
    return [o for o in objectives if o.krs or o.tags]


def load_quarters(okr_dir: Optional[Path] = None) -> List[Quarter]:
    quarters = []
    for path in sorted((okr_dir or OKR_DIR).glob("*/*.md")):
        m = QUARTER_FILE_RE.match(path.name)
        if not m:
            continue
        q = Quarter(int(m.group(1)), int(m.group(2)), path)
        q.objectives = parse_okr_text(path.read_text(encoding="utf-8-sig"))
        quarters.append(q)
    return sorted(quarters, key=lambda x: (x.year, x.q))


# ---------------------------------------------------------------- 仪表盘


def bar(p: float, width: int = 10) -> str:
    filled = int(round(p * width))
    return "█" * filled + "░" * (width - filled)


def pct(p: float) -> str:
    return f"{p * 100:.0f}%"


def status(progress: float, time_progress: float) -> str:
    """进度与时间对比：落后 ≤10% 为正常，≤25% 为预警，否则为落后。"""
    gap = time_progress - progress
    if progress >= 1.0:
        return "✅ 完成"
    if gap <= 0.10:
        return "🟢 正常"
    if gap <= 0.25:
        return "🟡 预警"
    return "🔴 落后"


def domain_progress(objectives: List[Objective]) -> Dict[str, float]:
    groups: Dict[str, List[float]] = {}
    for o in objectives:
        if o.active_krs:
            groups.setdefault(o.domain, []).append(o.progress)
    return {k: sum(v) / len(v) for k, v in groups.items()}


def rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def render_dashboard(quarters: List[Quarter], today: dt.date, week_changes: Optional[str] = None) -> str:
    lines = [
        "# 📊 OKR 仪表盘",
        "",
        f"> 自动生成于 {today.isoformat()}，请勿手动编辑。"
        "运行 `python scripts/okr_tool.py dashboard` 刷新。",
        "",
    ]
    if not quarters:
        lines += ["还没有季度 OKR，运行 `python scripts/okr_tool.py quarter` 新建。", ""]
        return "\n".join(lines)

    cur = next((q for q in quarters if q.start <= today <= q.end), quarters[-1])
    tp = cur.time_progress(today)
    days_left = max((cur.end - today).days, 0)

    lines += [
        f"## 当前季度：[{cur.name}]({rel(cur.path)})",
        "",
        "| 项目 | 进度 | |",
        "|---|---|---|",
        f"| ⏱️ 时间进度 | `{bar(tp)}` {pct(tp)} | 剩余 {days_left} 天 |",
        f"| 🎯 整体完成 | `{bar(cur.progress)}` {pct(cur.progress)} "
        f"| {status(cur.progress, tp)} |",
        "",
    ]

    domains = domain_progress(cur.objectives)
    if domains:
        lines += ["### 各领域", "", "| 领域 | 进度 | 状态 |", "|---|---|---|"]
        for name, p in sorted(domains.items(), key=lambda kv: -kv[1]):
            lines.append(f"| #{name} | `{bar(p)}` {pct(p)} | {status(p, tp)} |")
        lines.append("")

    if week_changes:
        lines += ["### 📈 本周进展", "", week_changes, ""]

    lines += ["### 目标明细", ""]
    for o in cur.objectives:
        tag = f" `#{o.domain}`" if o.tags else ""
        lines += [
            f"#### {o.title}{tag}",
            "",
            f"`{bar(o.progress)}` **{pct(o.progress)}** · {status(o.progress, tp)}",
            "",
            "| KR | 数值 | 进度 |",
            "|---|---|---|",
        ]
        if not o.krs:
            lines.append("| （还没有 KR） | | |")
        for kr in o.krs:
            if kr.dropped:
                lines.append(f"| ~~{kr.title}~~ | 已放弃 | — |")
            else:
                lines.append(
                    f"| {kr.title} | {kr.value_text} | `{bar(kr.progress, 5)}` {pct(kr.progress)} |"
                )
        lines.append("")

    # 落后提醒：时间过半但仍为 0 的 KR
    if tp >= 0.5:
        stuck = [
            (o, kr) for o in cur.objectives for kr in o.active_krs if kr.progress == 0
        ]
        if stuck:
            lines += ["### ⚠️ 还没动的 KR", ""]
            lines += [f"- {o.title} → {kr.title}" for o, kr in stuck]
            lines.append("")

    lines += ["## 历史季度", "", "| 季度 | 目标数 | 完成度 | |", "|---|---|---|---|"]
    for q in reversed(quarters):
        lines.append(
            f"| [{q.name}]({rel(q.path)}) | {len(q.objectives)} "
            f"| {pct(q.progress)} | `{bar(q.progress)}` |"
        )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- 文件生成


def quarter_of(day: dt.date) -> int:
    return (day.month - 1) // 3 + 1


def fill_template(name: str, values: Dict[str, str]) -> str:
    text = (TEMPLATE_DIR / name).read_text(encoding="utf-8-sig")
    for key, val in values.items():
        text = text.replace("{{" + key + "}}", val)
    return text


def write_new(path: Path, content: str) -> int:
    if path.exists():
        print(f"已存在，未覆盖：{rel(path)}")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(f"已创建：{rel(path)}")
    return 0


def kr_snapshot(quarter: Optional[Quarter]) -> str:
    if quarter is None or not quarter.objectives:
        return "- （本季度还没有 OKR）"
    out = []
    for o in quarter.objectives:
        out.append(f"- **{o.title}** — {pct(o.progress)}")
        for kr in o.active_krs:
            out.append(f"    - {kr.title}：{kr.value_text}（{pct(kr.progress)}）")
    return "\n".join(out)


def find_quarter(today: dt.date) -> Optional[Quarter]:
    q = quarter_of(today)
    return next((x for x in load_quarters() if x.year == today.year and x.q == q), None)


def write_dashboard(today: dt.date) -> Path:
    import okr_db  # 延迟导入：okr_db 依赖本模块

    quarters = load_quarters()
    current = next((q for q in quarters if q.start <= today <= q.end), None)
    week = okr_db.safe_changes(current, lambda q: okr_db.week_start(today)) if current else None
    DASHBOARD.write_text(render_dashboard(load_quarters(), today, week), encoding="utf-8")
    return DASHBOARD


# *_path 只算文件路径；plan_* 再算出内容，由调用方决定是否写入


def week_path(today: dt.date) -> Path:
    iso_year, week, _ = today.isocalendar()
    return OKR_DIR / str(today.year) / "weekly" / f"{iso_year}-W{week:02d}.md"


def quarter_path(today: dt.date) -> Path:
    return OKR_DIR / str(today.year) / f"{today.year}-Q{quarter_of(today)}.md"


def review_path(today: dt.date) -> Path:
    return OKR_DIR / str(today.year) / f"{today.year}-Q{quarter_of(today)}-复盘.md"


def year_path(today: dt.date) -> Path:
    return OKR_DIR / str(today.year) / f"{today.year}-年度目标.md"


def plan_week(today: dt.date) -> Tuple[Path, str]:
    import okr_db

    iso_year, week, _ = today.isocalendar()
    monday = today - dt.timedelta(days=today.weekday())
    current = find_quarter(today)
    changes = okr_db.safe_changes(current, lambda q: okr_db.week_start(today))
    content = fill_template(
        "周报.md",
        {
            "week": f"{iso_year}-W{week:02d}",
            "start": monday.isoformat(),
            "end": (monday + dt.timedelta(days=6)).isoformat(),
            "quarter": f"{today.year}-Q{quarter_of(today)}",
            "changes": changes,
            "snapshot": kr_snapshot(find_quarter(today)),
        },
    )
    return week_path(today), content


def plan_quarter(today: dt.date) -> Tuple[Path, str]:
    name = f"{today.year}-Q{quarter_of(today)}"
    content = fill_template("季度OKR.md", {"quarter": name, "year": str(today.year)})
    return quarter_path(today), content


def plan_review(today: dt.date) -> Tuple[Path, str]:
    import okr_db

    current = find_quarter(today)
    changes = okr_db.safe_changes(current, okr_db.quarter_start)
    current = find_quarter(today)  # 同步可能补了 ID，重新读取
    content = fill_template(
        "季度复盘.md",
        {
            "quarter": f"{today.year}-Q{quarter_of(today)}",
            "score": pct(current.progress) if current else "—",
            "changes": changes,
            "snapshot": kr_snapshot(current),
        },
    )
    return review_path(today), content


def plan_year(today: dt.date) -> Tuple[Path, str]:
    content = fill_template("年度目标.md", {"year": str(today.year)})
    return year_path(today), content


PATHS = {"week": week_path, "quarter": quarter_path, "review": review_path, "year": year_path}
PLANS = {"week": plan_week, "quarter": plan_quarter, "review": plan_review, "year": plan_year}


def cmd_dashboard(today: dt.date, args: List[str]) -> int:
    print(f"已生成：{rel(write_dashboard(today))}")
    return 0


def _plan_cmd(plan):
    def cmd(today: dt.date, args: List[str]) -> int:
        return write_new(*plan(today))

    return cmd


def _db_cmd(name: str):
    def cmd(today: dt.date, args: List[str]) -> int:
        import okr_db

        return getattr(okr_db, name)(today, args)

    return cmd


COMMANDS = {
    "dashboard": cmd_dashboard,
    **{n: _plan_cmd(p) for n, p in PLANS.items()},
    "sync": _db_cmd("cmd_sync"),
    "history": _db_cmd("cmd_history"),
    "sql": _db_cmd("cmd_sql"),
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="个人 OKR 工具")
    parser.add_argument("command", choices=COMMANDS, help="要执行的命令")
    parser.add_argument("args", nargs="*", help="history 的关键词 / sql 的语句")
    parser.add_argument("--date", help="指定日期 YYYY-MM-DD（默认今天）")
    args = parser.parse_args(argv)
    today = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    return COMMANDS[args.command](today, args.args)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台中文输出
    sys.exit(main())
