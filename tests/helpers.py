"""测试共用：把仓库路径整体指向临时目录，避免测试写到真实数据。"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import okr_tool  # noqa: E402

PATCHED = ("ROOT", "OKR_DIR", "DASHBOARD", "DB_PATH")


class TempRepoMixin:
    """setUp 后 self.root 是临时仓库根目录，okr/ 已创建。"""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "okr").mkdir()
        self._saved = {k: getattr(okr_tool, k) for k in PATCHED}
        okr_tool.ROOT = self.root
        okr_tool.OKR_DIR = self.root / "okr"
        okr_tool.DASHBOARD = self.root / "dashboard.md"
        okr_tool.DB_PATH = self.root / "data" / "okr.db"

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(okr_tool, k, v)
        self._tmp.cleanup()
        super().tearDown()

    def write_quarter(self, name, text):
        path = self.root / "okr" / name[:4] / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path
