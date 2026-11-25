"""真链路探针（含 HTTP 那一层）：界面那一句话 → 模型真的看见图。

live_images.py 走的是 Link（产品代码）；这一条再往上盖一层 —— 真的 HTTP 门、
真的 Hub、真的 JSON body。合起来就是「人粘一张图，模型看见它」的全程。

跑法：python _tmp/live_http.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

AAAS = Path(r"D:\projects\claw\open-agents\openagent-aaas")
WORK = Path(r"D:\projects\claw\open-agents\openagent-workspace")
for p in (str(AAAS), str(WORK)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ["AAAS_AUDIT"] = "off"

from aaas_service.app import build_server                              # noqa: E402
from tests.scripted import VisionModel, image_blocks, text, PNG_1X1    # noqa: E402

from agentworkspace import assistant, link as link_mod, server as door  # noqa: E402
from agentworkspace.kernel import Runtime                                # noqa: E402


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
    model = VisionModel([text("我看到了这张图。")])
    svc = build_server(model)
    ready, stop = threading.Event(), threading.Event()

    def serve_aaas() -> None:
        async def run():
            await svc.start(host="127.0.0.1", port=port)
            ready.set()
            while not stop.is_set():
                await asyncio.sleep(0.05)
            await svc.close()

        asyncio.run(run())

    th = threading.Thread(target=serve_aaas, daemon=True)
    th.start()
    ready.wait(15)

    tmp = Path(tempfile.mkdtemp(prefix="aw-live-http-"))
    rt = Runtime(tmp)
    assistant.install(rt)
    rt.scan()
    hub = link_mod.Hub(rt, tmp, {"url": f"ws://127.0.0.1:{port}"})
    httpd = door.serve(rt, hub, WORK / "agentworkspace" / "web", port=0)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    b64 = base64.b64encode(PNG_1X1).decode("ascii")
    body = json.dumps({"text": "这是什么",
                       "images": [{"name": "a.png", "mime": "image/png", "data": b64}]}).encode()
    req = urllib.request.Request(f"{base}/api/p/assistant/chat", data=body,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        out = json.loads(resp.read().decode())
    check("P1 HTTP 门收下了这句话（回执是 sent）", out.get("status") == "sent", str(out))

    end = time.time() + 30
    while time.time() < end and not model.requests:
        time.sleep(0.05)
    blocks = image_blocks(model.requests[0]["messages"]) if model.requests else []
    check("P2 模型看见了一张 image/png", len(blocks) == 1
          and blocks[0].source.media_type == "image/png", f"{len(blocks)} 块")
    check("P3 字节一路原样（base64 → JSON → Part.file → 模型块）",
          bool(blocks) and blocks[0].source.data == b64, "")

    # 第二件事实：图也进了会话记录，刷新页面还画得出来
    l = hub.link("assistant")
    rows = [r for r in l.store.messages("assistant", l.sid) if r["role"] == "user"] if l else []
    ref = (rows[-1].get("files") or [None])[0] if rows else None
    check("P4 会话记录里有引用（刷新后气泡里还画得出来）", bool(ref) and "name" in ref, str(ref))
    if ref:
        with urllib.request.urlopen(f"{base}/api/p/assistant/session/{l.sid}/img/{ref['name']}",
                                    timeout=10) as r2:
            got = r2.read()
            ctype = r2.headers.get("content-type")
        check("P5 图端点把那张图原样发回来", got == PNG_1X1 and ctype == "image/png",
              f"{len(got)} 字节 / {ctype}")

    httpd.shutdown()
    if hub is not None:
        hub.stop()
    stop.set()
    th.join(5)
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'全绿' if bad == 0 else '有失败'}：{bad} 条失败")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
