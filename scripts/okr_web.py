#!/usr/bin/env python3
"""OKR 本地网页界面：启动后自动打开浏览器。

数据仍然是 okr/ 下的 Markdown 文件：界面里的每次修改都直接写回文件，
并顺带刷新 dashboard.md，所以和 Obsidian 可以混着用。

用法：
    python scripts/okr_web.py            # 启动并打开浏览器
    python scripts/okr_web.py --no-open  # 只启动服务
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
import threading
import webbrowser
from http import HTTPStatus
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import okr_db  # noqa: E402
import okr_tool as okr  # noqa: E402

HOST = "127.0.0.1"
PORT = 8765
INDEX_HTML = Path(__file__).resolve().parent / "web" / "index.html"
QUARTER_NAME_RE = re.compile(r"^(\d{4})-Q([1-4])$")
LIST_PREFIX_RE = re.compile(r"^(\s*[-*]\s+)")
NUMBERED_RE = {"O": re.compile(r"^O(\d+)\b"), "KR": re.compile(r"^KR(\d+)\b")}

# 同一时间只允许一个写操作，避免两个请求交叉改同一个文件
_write_lock = threading.Lock()


class ApiError(Exception):
    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- 文件读写


def quarter_path(name: str) -> Path:
    m = QUARTER_NAME_RE.match(name or "")
    if not m:
        raise ApiError(f"季度名称不对：{name}")
    return okr.OKR_DIR / m.group(1) / f"{name}.md"


def read_lines(path: Path) -> List[str]:
    if not path.exists():
        raise ApiError(f"文件不存在：{okr.rel(path)}", HTTPStatus.NOT_FOUND)
    return path.read_text(encoding="utf-8-sig").splitlines()


def write_lines(path: Path, lines: List[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def check_line(lines: List[str], line: int, expect: str) -> None:
    """确认要改的那一行没被别处（比如 Obsidian）改过，否则拒绝修改。"""
    if not (0 <= line < len(lines)) or lines[line] != expect:
        raise ApiError("文件已在别处被修改，请刷新页面后再操作。", HTTPStatus.CONFLICT)


def find_objective(lines: List[str], line: int) -> okr.Objective:
    for o in okr.parse_okr_text("\n".join(lines)):
        if o.line == line:
            return o
    raise ApiError("找不到这个目标，请刷新页面。", HTTPStatus.CONFLICT)


# ---------------------------------------------------------------- 输入校验


def clean_text(value: Any, what: str) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise ApiError(f"{what}不能为空")
    if "`" in text:
        raise ApiError(f"{what}里不能有反引号 `")
    return text


def clean_tag(value: Any) -> str:
    tag = str(value or "").strip().lstrip("#")
    if not tag or re.search(r"[\s#]", tag):
        raise ApiError("领域标签不能为空，也不能有空格或 #")
    return tag


def to_number(value: Any, what: str) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ApiError(f"{what}必须是数字") from None
    if num < 0 or num != num or num == float("inf"):
        raise ApiError(f"{what}不能小于 0")
    return num


def kr_from_payload(data: Dict[str, Any]) -> okr.KeyResult:
    kr = okr.KeyResult(
        title=clean_text(data.get("title"), "KR 标题"),
        checked=bool(data.get("checked")),
        dropped=bool(data.get("dropped")) and not data.get("checked"),
    )
    if data.get("target") not in (None, ""):
        kr.target = to_number(data["target"], "目标值")
        if kr.target == 0:
            raise ApiError("目标值必须大于 0")
        kr.current = to_number(data.get("current") or 0, "当前值")
        kr.unit = str(data.get("unit") or "").strip().replace("`", "")
    return kr


def auto_number(title: str, siblings: List[str], prefix: str) -> str:
    """同级都用 O1/KR1 这种编号时，新条目自动接着编号。"""
    pattern = NUMBERED_RE[prefix]
    if pattern.match(title):
        return title
    numbers = [pattern.match(t) for t in siblings]
    if not all(numbers):
        return title
    nxt = max((int(m.group(1)) for m in numbers), default=0) + 1
    return f"{prefix}{nxt} {title}"


def insert_point(lines: List[str], pos: int) -> int:
    """在 pos 之前插入时，跳过前面的空行，让新内容紧贴上一段。"""
    while pos > 0 and not lines[pos - 1].strip():
        pos -= 1
    return pos


# ---------------------------------------------------------------- 修改操作（纯函数，便于测试）


def save_kr(lines: List[str], line: int, expect: str, data: Dict[str, Any]) -> List[str]:
    check_line(lines, line, expect)
    m = LIST_PREFIX_RE.match(lines[line])
    prefix = m.group(1) if m else "- "
    kr = kr_from_payload(data)
    old_id = okr.BLOCK_ID_RE.search(lines[line])
    kr.id = old_id.group(1) if old_id else ""  # 保留块 ID，历史记录才连得上
    lines = list(lines)
    lines[line] = okr.format_kr(kr, prefix)
    return lines


def delete_kr(lines: List[str], line: int, expect: str) -> List[str]:
    check_line(lines, line, expect)
    return lines[:line] + lines[line + 1 :]


def add_kr(lines: List[str], obj_line: int, expect: str, data: Dict[str, Any]) -> List[str]:
    check_line(lines, obj_line, expect)
    obj = find_objective(lines, obj_line)
    kr = kr_from_payload(data)
    kr.title = auto_number(kr.title, [k.title for k in obj.krs], "KR")
    new_line = okr.format_kr(kr)
    if obj.krs:
        pos = obj.krs[-1].line + 1
        return lines[:pos] + [new_line] + lines[pos:]
    pos = obj_line + 1
    return lines[:pos] + ["", new_line] + lines[pos:]


def add_objective(lines: List[str], data: Dict[str, Any]) -> List[str]:
    objectives = okr.parse_okr_text("\n".join(lines))
    title = auto_number(clean_text(data.get("title"), "目标标题"), [o.title for o in objectives], "O")
    heading = okr.format_objective_heading(title, [clean_tag(data.get("domain"))])
    if objectives:
        pos = objectives[-1].end_line
    else:
        # 放在第一个二级标题（如“备注”）之前，没有就放到文件末尾
        pos = next((i for i, l in enumerate(lines) if l.startswith("## ")), len(lines))
    pos = insert_point(lines, pos)
    return lines[:pos] + ["", heading] + lines[pos:]


def save_objective(lines: List[str], line: int, expect: str, data: Dict[str, Any]) -> List[str]:
    check_line(lines, line, expect)
    obj = find_objective(lines, line)
    tags = [clean_tag(data.get("domain"))] + obj.tags[1:]
    lines = list(lines)
    lines[line] = okr.format_objective_heading(clean_text(data.get("title"), "目标标题"), tags)
    return lines


def delete_objective(lines: List[str], line: int, expect: str) -> List[str]:
    check_line(lines, line, expect)
    obj = find_objective(lines, line)
    return lines[:line] + lines[obj.end_line :]


EDITS = {
    "/api/kr/save": lambda lines, d: save_kr(lines, d["line"], d["expect"], d.get("kr", {})),
    "/api/kr/delete": lambda lines, d: delete_kr(lines, d["line"], d["expect"]),
    "/api/kr/add": lambda lines, d: add_kr(lines, d["line"], d["expect"], d.get("kr", {})),
    "/api/objective/add": lambda lines, d: add_objective(lines, d),
    "/api/objective/save": lambda lines, d: save_objective(lines, d["line"], d["expect"], d),
    "/api/objective/delete": lambda lines, d: delete_objective(lines, d["line"], d["expect"]),
}


# ---------------------------------------------------------------- 状态输出


def file_info(path: Path) -> Dict[str, Any]:
    return {"path": okr.rel(path), "abs": str(path), "exists": path.exists()}


def kr_json(kr: okr.KeyResult, rows: List[Any], q: okr.Quarter, today: dt.date) -> Dict[str, Any]:
    return {
        "id": kr.id,
        # 迷你趋势：最近 20 条记录的进度
        "spark": [r["progress"] for r in rows[-20:]],
        "forecast": okr_db.forecast(rows, q, today),
        "line": kr.line,
        "raw": kr.raw,
        "title": kr.title,
        "checked": kr.checked,
        "dropped": kr.dropped,
        "current": kr.current,
        "target": kr.target,
        "unit": kr.unit,
        "progress": kr.progress,
    }


def quarter_json(q: okr.Quarter, today: dt.date, conn: Any) -> Dict[str, Any]:
    tp = q.time_progress(today)
    data = okr_db.measurements(conn, [kr.id for o in q.objectives for kr in o.krs if kr.id])
    week = okr_db.changes_since(conn, q, okr_db.week_start(today)) if q.start <= today <= q.end else []
    return {
        "name": q.name,
        "file": file_info(q.path),
        "start": q.start.isoformat(),
        "end": q.end.isoformat(),
        "time_progress": tp,
        "days_left": max((q.end - max(today, q.start)).days, 0),
        "progress": q.progress,
        "status": okr.status(q.progress, tp),
        "domains": [
            {"name": k, "progress": v, "status": okr.status(v, tp)}
            for k, v in sorted(okr.domain_progress(q.objectives).items(), key=lambda kv: -kv[1])
        ],
        "series": okr_db.quarter_series(conn, q, today),
        "week_changes": [dict(c, text=okr_db.describe_change(c)) for c in week],
        "objectives": [
            {
                "line": o.line,
                "raw": o.raw,
                "title": o.title,
                "domain": o.domain,
                "tags": o.tags,
                "progress": o.progress,
                "status": okr.status(o.progress, tp) if o.active_krs else "",
                "krs": [kr_json(kr, data.get(kr.id, []), q, today) for kr in o.krs],
            }
            for o in q.objectives
        ],
    }


def next_quarter_start(today: dt.date) -> dt.date:
    q = okr.quarter_of(today)
    return dt.date(today.year + 1, 1, 1) if q == 4 else dt.date(today.year, 3 * q + 1, 1)


def build_state(today: dt.date, selected: Optional[str], conn: Any) -> Dict[str, Any]:
    quarters = okr.load_quarters()
    this_name = f"{today.year}-Q{okr.quarter_of(today)}"
    names = [q.name for q in quarters]
    if not selected or selected not in names:
        selected = this_name if this_name in names else (names[-1] if names else None)
    chosen = next((q for q in quarters if q.name == selected), None)
    nxt = next_quarter_start(today)
    return {
        "today": today.isoformat(),
        "this_quarter": this_name,
        "next_quarter": {"name": f"{nxt.year}-Q{okr.quarter_of(nxt)}", "date": nxt.isoformat()},
        "quarters": [{"name": q.name, "progress": q.progress} for q in reversed(quarters)],
        "quarter": quarter_json(chosen, today, conn) if chosen else None,
        "files": {kind: file_info(path(today)) for kind, path in okr.PATHS.items()},
        "dashboard": file_info(okr.DASHBOARD),
    }


def kr_history(conn: Any, kr_id: str) -> Dict[str, Any]:
    kr = conn.execute("SELECT * FROM kr WHERE id = ?", (kr_id,)).fetchone()
    if kr is None:
        raise ApiError("找不到这个 KR 的历史记录", HTTPStatus.NOT_FOUND)
    rows = conn.execute(
        "SELECT ts, current, target, checked, dropped, progress, source FROM measurement"
        " WHERE kr_id = ? ORDER BY ts, id", (kr_id,)
    ).fetchall()
    return {"kr": dict(kr), "rows": [dict(r) for r in rows]}


def create_file(kind: str, date: Optional[str]) -> Dict[str, Any]:
    if kind not in okr.PLANS:
        raise ApiError(f"不支持的类型：{kind}")
    try:
        day = dt.date.fromisoformat(date) if date else dt.date.today()
    except ValueError:
        raise ApiError(f"日期格式不对：{date}") from None
    path, content = okr.PLANS[kind](day)
    created = not path.exists()
    if created:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return {"created": created, **file_info(path)}


# ---------------------------------------------------------------- HTTP 服务


class Handler(BaseHTTPRequestHandler):
    server_version = "OKR"

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静一点，只打印错误
        pass

    def _host_ok(self) -> bool:
        # 防 DNS 重绑定：只接受用本机地址访问
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost")

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: Any, status: int = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path in ("/", "/index.html"):
            return self._send(HTTPStatus.OK, INDEX_HTML.read_bytes(), "text/html; charset=utf-8")
        try:
            if url.path == "/api/state":
                with _write_lock, closing(okr_db.connect()) as conn:
                    okr_db.sync(conn, "file")  # 收进 Obsidian 里的改动
                    return self._json(build_state(dt.date.today(), query.get("quarter", [None])[0], conn))
            if url.path == "/api/history":
                with closing(okr_db.connect()) as conn:
                    return self._json(kr_history(conn, query.get("id", [""])[0]))
        except ApiError as e:
            return self._json({"error": str(e)}, e.status)
        except Exception as e:  # 文件格式、数据库等问题，交给页面显示
            return self._json({"error": f"读取失败：{e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
        self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        # 自定义请求头会触发浏览器跨域预检（本服务不响应），防止别的网页偷偷提交
        if not self._host_ok() or self.headers.get("X-OKR") != "1":
            return self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
            with _write_lock:
                with closing(okr_db.connect()) as conn:
                    # 先按“文件”收掉别处的改动，再改，最后按“界面”记录这次修改，
                    # 这样 Obsidian 里的改动不会被误记成界面操作
                    okr_db.sync(conn, "file")
                    result = self._handle(urlparse(self.path).path, data)
                    okr_db.sync(conn, "gui")
                okr.write_dashboard(dt.date.today())
            self._json({"ok": True, **result})
        except ApiError as e:
            self._json({"error": str(e)}, e.status)
        except (KeyError, TypeError, ValueError) as e:
            self._json({"error": f"请求参数不对：{e}"}, HTTPStatus.BAD_REQUEST)
        except sqlite3.Error as e:
            self._json({"error": f"历史数据库出错：{e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _handle(self, path: str, data: Dict[str, Any]) -> Dict[str, Any]:
        if path == "/api/create":
            return create_file(data.get("kind", ""), data.get("date"))
        if path not in EDITS:
            raise ApiError("未知操作", HTTPStatus.NOT_FOUND)
        file = quarter_path(data.get("quarter", ""))
        write_lines(file, EDITS[path](read_lines(file), data))
        return {}


def serve(port: int, open_browser: bool) -> None:
    server = None
    for p in range(port, port + 10):
        try:
            server = ThreadingHTTPServer((HOST, p), Handler)
            break
        except OSError:
            continue
    if server is None:
        sys.exit(f"端口 {port}~{port + 9} 都被占用了")
    url = f"http://{HOST}:{server.server_address[1]}/"
    print(f"OKR 界面已启动：{url}")
    print("关闭这个窗口（或按 Ctrl+C）即退出。")
    if open_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="OKR 本地网页界面")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)
    serve(args.port, not args.no_open)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
