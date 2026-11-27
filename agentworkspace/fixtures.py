"""selftest 用的两个 fixture 程序（pomodoro / memo）—— 随包发行，不进 workspace。

为什么它们在包里：``python -m agentworkspace.selftest`` 默认在一个**自带 fixture
的临时工作台**里跑，才能对任何一台机器给出同一份答案。这两个程序原来是工作台
数据（放在 ``workspace/plugins/``），谁把它们删了（``.trash-*-data`` 就是删除残留），
「内核 × 真实程序」那 16 条断言就红着 —— 而红着不解释的质量门等于没有门。

只读复制源工作台：``.backup`` / ``.trash-*`` / ``__pycache__`` 一律不拷（历史归档与
删除残留会把临时目录撑大，还可能把「程序已删」的旧状态混进来）。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

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


def build(tmp: Path, src: Path | None = None) -> Path:
    """把 ``workspace/`` **只读复制**到 ``tmp``，补上两个 fixture，返回新工作台路径。

    Args:
        tmp: 临时目录（不存在会建）。
        src: 源工作台；默认是本包旁边的 ``workspace/``。

    Returns:
        新工作台 ``tmp / "workspace"``。
    """
    tmp = Path(tmp)
    if src is None:
        src = Path(__file__).resolve().parent.parent / "workspace"
    src, dst = Path(src), tmp / "workspace"
    if src.is_dir():
        shutil.copytree(src, dst,
                        ignore=shutil.ignore_patterns(".backup", ".trash-*",
                                                      "__pycache__", "*.pyc"))
    else:                                   # 源都没有：从空工作台起步，也别炸
        dst.mkdir(parents=True, exist_ok=True)
    (dst / "prompts").mkdir(parents=True, exist_ok=True)
    if not (dst / "prompts" / "assistant.md").is_file():
        (dst / "prompts" / "assistant.md").write_text(
            "# 你是谁\n\n你是小助理。\n", encoding="utf-8")

    for pid, meta, backend in (
        ("pomodoro", {"id": "pomodoro", "title": "番茄钟",
                      "description": "专注一小段时间"}, POMO_BACKEND),
        ("memo", {"id": "memo", "title": "记事", "description": "随手记一句",
                  "ui": "ui/app.py"}, MEMO_BACKEND),
    ):
        d = dst / "plugins" / pid
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
    return dst
