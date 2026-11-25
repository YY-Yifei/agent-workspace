r"""先红后绿：故意把框架词塞回去，看门是不是真的会红。

用法：python _tmp\mutate_prompt.py [mutate|revert]
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(r"D:\projects\claw\open-agents\openagent-aaas")
DECODE = ROOT / "aaas_service" / "decode.py"
TOOLS = ROOT / "aaas_service" / "tools.py"

MUTATIONS = [
    # (文件, 好文字, 坏文字, 该红的那条门)
    (
        DECODE,
        "            \"这一次没有任何可用工具 —— 你查不到外部事实，也做不了任何动作。\",",
        "            \"这一次没有任何可用工具 —— 调用方声明本轮没有给你能力。\",",
        "framework（prompt 里出现「调用方」）",
    ),
    (
        DECODE,
        "        spec.instructions.strip() or \"（没有收到职责描述）\",",
        "        spec.instructions.strip() or \"（服务端未收到职责描述）\",",
        "framework（prompt 里出现「服务端」）",
    ),
    (
        TOOLS,
        '            f"\\n这次 {name} 没有被执行，不是参数的问题。"',
        '            f"\\n调用方没有执行 {name}，这不是参数问题。"',
        "framework（渲染文本里出现「调用方」）",
    ),
]


def apply(revert: bool) -> int:
    for path, good, bad, _ in MUTATIONS:
        src = path.read_text(encoding="utf-8")
        old, new = (bad, good) if revert else (good, bad)
        if src.count(old) != 1:
            print(f"!! {path.name}: 命中 {src.count(old)} 次，放弃")
            return 1
        path.write_text(src.replace(old, new), encoding="utf-8", newline="")
    print("[ok] 已" + ("退回" if revert else "弄坏"))
    return 0


def run_gates() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-k", "framework or stays_short", "--no-header"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    tail = [ln for ln in proc.stdout.splitlines() if ln.strip()][-6:]
    print("\n".join(tail))


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(errors="replace")
    mode = argv[1] if len(argv) > 1 else "mutate"
    if apply(revert=(mode == "revert")):
        return 1
    run_gates()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
