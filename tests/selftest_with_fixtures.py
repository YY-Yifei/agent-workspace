"""兼容入口：fixture 已经搬进包里，selftest 默认就在临时工作区跑。

（在此之前，这个脚本是「补 fixture 再跑一遍」的唯一途径。现在那是
``python -m agentworkspace.selftest`` 的**默认行为**，所以这里只剩一句转接 ——
保留它，是因为 CI 与肌肉记忆还在调这个路径。）

    python tests/selftest_with_fixtures.py          # == python -m agentworkspace.selftest
    python tests/selftest_with_fixtures.py --here   # 就在真工作区里跑
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agentworkspace import selftest  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(selftest.main(sys.argv[1:]))
