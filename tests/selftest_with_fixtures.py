"""在**临时工作区**里补齐 selftest 需要的两个 fixture，然后跑一遍。

为什么需要它：`agentworkspace/selftest.py` 有一组断言验的是「内核 × 真实程序」的对接
（pomodoro 的 4 个能力、memo 的原生窗口）。那两个程序是**工作区数据**，不是代码 ——
被删掉之后（`workspace/plugins/.trash-*` 就是删除残留），那 16 条断言必然失败。

`selftest._fixture_note()` 已经把「数据缺失」与「代码坏了」分开说了。这个脚本走得更远
一步：**在临时目录里造出 fixture 再跑**，于是「补上数据之后是不是全绿」变成一条可重复的
运行，而不是一句需要相信的话。

用法：

    python tests/selftest_with_fixtures.py

它不会碰 `workspace/` 里的任何东西（只读复制），临时工作区地点会打印出来。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

POMO_BACKEND = '''"""番茄钟 —— selftest 用的最小 fixture（4 个公开能力 + 1 个私有辅助）。"""

from __future__ import annotations

from typing import Annotated

Minutes = Annotated[int, {"minimum": 1, "maximum": 180, "default": 25,
                          "description": "这一轮多少分钟"}]


def _bump_done(n: int = 1) -> int:
    """私有：完成计数（不该出现在能力面上）。"""
    done = int(ctx.get("done", 0)) + n
    ctx.set("done", done)
    return done


def start(minutes: Minutes = 25) -> dict:
    """开始一轮番茄钟。

    minutes: 多少分钟
    """
    ctx.set("minutes", int(minutes))
    ctx.set("running", True)
    return {"text": f"开始 {minutes} 分钟", "minutes": int(minutes)}


def status() -> dict:
    """看当前这一轮还剩多久。"""
    minutes = int(ctx.get("minutes", 0))
    return {"text": f"还有 {minutes} 分钟", "minutes": minutes,
            "running": bool(ctx.get("running", False))}


def done() -> dict:
    """标记这一轮做完了。"""
    return {"text": f"今天已完成 {_bump_done()} 轮"}


def stop() -> dict:
    """停掉这一轮。"""
    ctx.set("running", False)
    return {"text": "已停下"}
'''

MEMO_BACKEND = '''"""记事 —— selftest 用的原生程序 fixture。"""

from __future__ import annotations

from typing import Annotated

Text = Annotated[str, {"description": "要记下的一句话"}]


def add(text: Text) -> dict:
    """记一条。

    text: 内容
    """
    items = list(ctx.get("items", []))
    items.append({"text": text})
    ctx.set("items", items)
    return {"text": f"已记下（共 {len(items)} 条）", "count": len(items)}


def notes() -> dict:
    """看记下的全部。"""
    items = list(ctx.get("items", []))
    return {"text": f"共 {len(items)} 条", "items": items}
'''

MEMO_UI = '''"""原生窗口 fixture（selftest 只做静态体检，不会真的拉起它）。"""

import tkinter as tk

root = tk.Tk()
root.title("记事")
root.mainloop()
'''


def build(tmp: Path) -> None:
    """把真 workspace 只读复制到 tmp，再补上 pomodoro / memo。"""
    src = ROOT / "workspace"
    if src.is_dir():
        shutil.copytree(src, tmp / "workspace",
                        ignore=shutil.ignore_patterns(".backup", ".trash-*",
                                                      "__pycache__", "*.pyc"))
    ws = tmp / "workspace"
    (ws / "prompts").mkdir(parents=True, exist_ok=True)
    if not (ws / "prompts" / "assistant.md").is_file():
        (ws / "prompts" / "assistant.md").write_text(
            "# 你是谁\n\n你是小助理。\n", encoding="utf-8")

    for pid, meta, backend in (
        ("pomodoro", {"id": "pomodoro", "title": "番茄钟",
                      "description": "专注一小段时间"}, POMO_BACKEND),
        ("memo", {"id": "memo", "title": "记事", "description": "随手记一句",
                  "ui": "ui/app.py"}, MEMO_BACKEND),
    ):
        d = ws / "plugins" / pid
        (d / "ui").mkdir(parents=True, exist_ok=True)
        (d / "plugin.json").write_text(json.dumps(meta, ensure_ascii=False),
                                       encoding="utf-8")
        (d / "backend.py").write_text(backend, encoding="utf-8")
        if pid == "memo":
            (d / "ui" / "app.py").write_text(MEMO_UI, encoding="utf-8")
        else:
            (d / "ui" / "index.html").write_text(
                "<!doctype html><html><body><h1>番茄钟</h1></body></html>",
                encoding="utf-8")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="aw-selftest-"))
    build(tmp)
    from agentworkspace import selftest

    selftest.WS = tmp / "workspace"          # 全部断言改在临时工作区上跑
    print(f"[临时工作区] {selftest.WS}", flush=True)
    rc = selftest.main()
    print(f"\n（临时工作区保留在 {tmp}，方便复查；确认后可删）", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
