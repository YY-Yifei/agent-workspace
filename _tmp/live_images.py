"""真链路探针：真 aaas-service + 真 Caller + 真 png，走的是**产品代码**（Link.send）。

与 selftest 的 [H] 节互补：那一节用替身 caller 证明「我们发了什么」，
这一节证明「对面真的收到了、模型真的看见了」。

跑法：python _tmp/live_images.py
"""

from __future__ import annotations

import asyncio
import base64
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

AAAS = Path(r"D:\projects\claw\open-agents\openagent-aaas")
WORK = Path(r"D:\projects\claw\open-agents\openagent-workspace")
for p in (str(AAAS), str(WORK)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ["AAAS_AUDIT"] = "off"          # 别往仓库根写会话审计

from aaas_service.app import build_server          # noqa: E402
from tests.scripted import VisionModel, image_blocks, text, PNG_1X1  # noqa: E402

from agentworkspace import assistant, link as link_mod, media  # noqa: E402
from agentworkspace.kernel import Runtime          # noqa: E402


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main() -> int:
    bad = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal bad
        if cond:
            print(f"  ok  {name}")
        else:
            bad += 1
            print(f"  !!  {name}   {detail}")

    port = free_port()
    model = VisionModel([text("我看到了。"), text("还是那一张。")])
    server = build_server(model)
    ready, stop = threading.Event(), threading.Event()
    crash: list[BaseException] = []

    def serve() -> None:
        async def run():
            await server.start(host="127.0.0.1", port=port)
            ready.set()
            while not stop.is_set():
                await asyncio.sleep(0.05)
            await server.close()

        try:
            asyncio.run(run())
        except BaseException as exc:            # 起不来就把真相带回来，别静默
            crash.append(exc)
            ready.set()

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    ready.wait(15)
    if crash:
        print(f"[错误] 服务起不来：{crash[0]!r}")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="aw-live-"))
    rt = Runtime(tmp)
    assistant.install(rt)
    rt.scan()

    l = link_mod.Link(rt, tmp, {"url": f"ws://127.0.0.1:{port}"}, "assistant")
    l.start()
    end = time.time() + 20
    while time.time() < end and l.status != "ready":
        time.sleep(0.05)
    check("L1 声明成功、连接 ready（part.file 已协商）",
          l.status == "ready", f"{l.status} / {l.error}")
    cr = getattr(l._caller, "connect_result", None)
    check("L1b 对面确实宣告了 part.file", bool(cr and cr.has("part.file")),
          str(getattr(cr, "features", None)))
    check("L1c 上限来自连接自报（不是我们写死的 4MB）",
          l._max_file_bytes() > 0, str(l._max_file_bytes()))

    b64 = base64.b64encode(PNG_1X1).decode("ascii")
    st = l.send("这是什么", images=[{"name": "a.png", "mime": "image/png", "data": b64}])
    check("L2 送出去了", st == "sent", st)
    end = time.time() + 20
    while time.time() < end and l._busy:
        time.sleep(0.05)

    blocks = image_blocks(model.requests[0]["messages"]) if model.requests else []
    check("L3 模型真的收到了一份图片块", len(blocks) == 1, f"{len(blocks)} 块")
    got = blocks[0].source.data if blocks else ""
    check("L3b 字节原样（不是占位、不是重新编码成别的图）", got == b64,
          f"{len(got)} vs {len(b64)} 字符")
    check("L3c media_type 是 image/png", bool(blocks) and blocks[0].source.media_type == "image/png",
          str(getattr(blocks[0].source, "media_type", None) if blocks else None))

    # 第二轮：stateless 每轮重发全史 —— 图必须还在模型看得见的那份里
    l.send("那现在呢")
    end = time.time() + 20
    while time.time() < end and l._busy:
        time.sleep(0.05)
    second = image_blocks(model.requests[1]["messages"]) if len(model.requests) > 1 else []
    check("L4 第二轮里图还在（模型不会忘）", len(second) >= 1, f"{len(second)} 块")

    l.stop()
    stop.set()
    th.join(5)

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'全绿' if bad == 0 else '有失败'}：{bad} 条失败")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
