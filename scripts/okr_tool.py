#!/usr/bin/env python3
"""个人 OKR 工具：解析 Markdown 中的 OKR，生成仪表盘、周报、季度文件。

只依赖 Python 标准库（3.9+）。

用法：
    python scripts/okr_tool.py dashboard          # 生成 dashboard.md
    python scripts/okr_tool.py week               # 新建本周周报
    python scripts/okr_tool.py quarter            # 新建本季度 OKR 文件
    python scripts/okr_tool.py review             # 新建本季度复盘
    python scripts/okr_tool.py year               # 新建本年度目标

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
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
OKR_DIR = ROOT / "okr"
TEMPLATE_DIR = ROOT / "templates"
DASHBOARD = ROOT / "dashboard.md"

QUARTER_FILE_RE = re.compile(r"^(\d{4})-Q([1-4])\.md$")
OBJECTIVE_RE = re.compile(r"^##\s+(.+?)\s*$")
KR_RE = re.compile(r"^\s*[-*]\s+\[( |x|X|-)\]\s+(.+?)\s*$")
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
    m = VALUE_RE.search(text)
    if m:
        kr.current, kr.target = float(m.group(1)), float(m.group(2))
        kr.unit = m.group(3)
        kr.title = (text[: m.start()] + text[m.end() :]).strip()
    kr.title = re.sub(r"\s{2,}", " ", kr.title)
    return kr


def parse_okr_text(text: str) -> List[Objective]:
    objectives: List[Objective] = []
    current: Optional[Objective] = None
    in_frontmatter = False
    in_code = False
    for i, line in enumerate(text.splitlines()):
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
        if m:
            current = parse_objective_heading(m.group(1))
            objectives.append(current)
            continue
        if line.startswith("# "):
            current = None
            continue
        m = KR_RE.match(line)
        if m and current is not None:
            current.krs.append(parse_kr(m.group(1), m.group(2)))
    # 没有任何 KR 的二级标题（如“备注”）不算目标
    return [o for o in objectives if o.krs]


def load_quarters(okr_dir: Path = OKR_DIR) -> List[Quarter]:
    quarters = []
    for path in sorted(okr_dir.glob("*/*.md")):
        m = QUARTER_FILE_RE.match(path.name)
        if not m:
            continue
        q = Quarter(int(m.group(1)), int(m.group(2)), path)
        q.objectives = parse_okr_text(path.read_text(encoding="utf-8"))
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


def render_dashboard(quarters: List[Quarter], today: dt.date) -> str:
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
    text = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
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


def cmd_dashboard(today: dt.date) -> int:
    DASHBOARD.write_text(render_dashboard(load_quarters(), today), encoding="utf-8")
    print(f"已生成：{rel(DASHBOARD)}")
    return 0


def cmd_week(today: dt.date) -> int:
    iso_year, week, _ = today.isocalendar()
    monday = today - dt.timedelta(days=today.weekday())
    sunday = monday + dt.timedelta(days=6)
    q = quarter_of(today)
    current = next(
        (x for x in load_quarters() if x.year == today.year and x.q == q), None
    )
    content = fill_template(
        "周报.md",
        {
            "week": f"{iso_year}-W{week:02d}",
            "start": monday.isoformat(),
            "end": sunday.isoformat(),
            "quarter": f"{today.year}-Q{q}",
            "snapshot": kr_snapshot(current),
        },
    )
    path = OKR_DIR / str(today.year) / "weekly" / f"{iso_year}-W{week:02d}.md"
    return write_new(path, content)


def cmd_quarter(today: dt.date) -> int:
    q = quarter_of(today)
    name = f"{today.year}-Q{q}"
    content = fill_template("季度OKR.md", {"quarter": name, "year": str(today.year)})
    return write_new(OKR_DIR / str(today.year) / f"{name}.md", content)


def cmd_review(today: dt.date) -> int:
    q = quarter_of(today)
    name = f"{today.year}-Q{q}"
    current = next(
        (x for x in load_quarters() if x.year == today.year and x.q == q), None
    )
    content = fill_template(
        "季度复盘.md",
        {
            "quarter": name,
            "score": pct(current.progress) if current else "—",
            "snapshot": kr_snapshot(current),
        },
    )
    return write_new(OKR_DIR / str(today.year) / f"{name}-复盘.md", content)


def cmd_year(today: dt.date) -> int:
    year = str(today.year)
    content = fill_template("年度目标.md", {"year": year})
    return write_new(OKR_DIR / year / f"{year}-年度目标.md", content)


COMMANDS = {
    "dashboard": cmd_dashboard,
    "week": cmd_week,
    "quarter": cmd_quarter,
    "review": cmd_review,
    "year": cmd_year,
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="个人 OKR 工具")
    parser.add_argument("command", choices=COMMANDS, help="要执行的命令")
    parser.add_argument("--date", help="指定日期 YYYY-MM-DD（默认今天）")
    args = parser.parse_args(argv)
    today = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    return COMMANDS[args.command](today)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台中文输出
    sys.exit(main())
