"""The design and usage documents describe session start recall as the hook launcher does it (vault-graph request S7).

vault_hooks.py session-start prints vg output when `vg hook session-start` exits 0, and recall.sh
output otherwise. Without hooks nothing is added at session start.
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RecallInjectionDocTests(unittest.TestCase):
    def test_usage_describes_the_three_cases(self):
        text = (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
        self.assertNotIn("세션을 시작할 때 요약을 자동으로 넣지는 않습니다.", text)
        for needle in ("hook 없음", "`vg hook session-start`가 exit 0", "vg가 없거나 0이 아닌 코드로 끝나거나"):
            self.assertIn(needle, text)

    def test_design_page_does_not_say_the_hook_always_injects_the_summary(self):
        text = (ROOT / "docs" / "html" / "project-memory-structure.html").read_text(encoding="utf-8")
        self.assertNotIn("hook이 프로젝트 요약을 넣음", text)
        self.assertNotIn("<td>hook이 세션 시작 때 주입</td>", text)
        self.assertIn("vg가 없거나 실패하면 프로젝트 요약을 넣음", text)


if __name__ == "__main__":
    unittest.main()
