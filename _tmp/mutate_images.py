"""变异验证：把图片这条链上的三处各拆一次，对应的门必须红。

一条门只有在「它守着的那行代码被拆掉时会红」才算门 —— 否则它只是恰好绿着。
"""
from __future__ import annotations

import io
import re
import subprocess
import sys
from pathlib import Path

WORK = Path(r"D:\projects\claw\open-agents\openagent-workspace")
LINK = WORK / "agentworkspace" / "link.py"
OUT = WORK / "_tmp" / "mut.txt"

MUTATIONS = [
    ("A 图不拼进 Message（退回死参数）",
     "msg: str | aaas.Message = media.message(text, list(pics)) if pics else text",
     "msg: str | aaas.Message = text",
     ("H7", "H8", "H9")),
    ("B 不判 part.file（未协商也照发）",
     '        if not cr.has("part.file"):',
     "        if False:",
     ("H10",)),
    ("C history 不读回图（第二轮就忘）",
     "            pics = [im for im in (self._image_of(f) for f in keep) if im is not None]",
     "            pics = []",
     ("H8", "H11b")),
]


def run_selftest() -> str:
    with open(OUT, "wb") as fh:
        subprocess.run([sys.executable, "-m", "agentworkspace.selftest"],
                       cwd=str(WORK), stdout=fh, stderr=subprocess.STDOUT, timeout=900)
    return io.open(OUT, encoding="gbk", errors="replace").read()


def h_section(text: str) -> str:
    i = text.find("[H]")
    return text[i:] if i >= 0 else text[-800:]


def main() -> int:
    original = LINK.read_text(encoding="utf-8")
    bad = 0
    try:
        for name, old, new, expect in MUTATIONS:
            assert old in original, f"变异点没命中：{name}"
            LINK.write_text(original.replace(old, new, 1), encoding="utf-8")
            out = run_selftest()
            sec = h_section(out)
            reds = set(re.findall(r"!!\s+(H\d+[a-z]?)", sec))
            hit = [e for e in expect if e in reds]
            ok = bool(hit)
            print(f"{'ok  ' if ok else '!!  '}{name} → 红的是 {sorted(reds) or '（一条都没红）'}")
            if not ok:
                bad += 1
                print(sec[-1200:])
    finally:
        LINK.write_text(original, encoding="utf-8")

    out = run_selftest()
    tail = out.strip().splitlines()[-1] if out.strip() else ""
    green = "0 失败" in tail
    print(f"{'ok  ' if green else '!!  '}恢复后全绿：{tail}")
    if not green:
        bad += 1
    print(f"\n{'变异全部被抓住' if bad == 0 else f'{bad} 处没被抓住'}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
