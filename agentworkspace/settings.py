"""人能改的那一个设置：**aaas-service 地址**。

为什么单独一个文件：这是全系统**唯一**一处「人改一下就改变内核之外那个东西」的配置。
它有三段，各自可断言：

    normalize(raw)   一句话能不能当地址用 —— 纯函数，所以不必起服务就能验
    save_url(root)   落盘（原子写），不动别的键
    hub_cfg(root)    「配置」到「Hub 要的形状」的唯一翻译

**边界**：这里只碰 ``config.json``。``workspace/dev.json`` 与它同级，同为
``toolbox.RESERVED`` —— 一个能写文件的程序如果能改这两份，就等于能给自己或别人
拨权限。所以设置这条路**只属于人**（``server.py`` 里的路由，没有对应的工具）。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

DEFAULT_URL = "ws://localhost:8888/"
SCHEMES = ("ws", "wss", "http", "https")
_SHAPE = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*)://(?P<rest>.+)$")
DEFAULT_REVISION = "workspace-v1"


class BadURL(ValueError):
    """这个地址不能用。消息是**给人看的**，直接上界面。"""


def normalize(raw) -> str:
    """把一行输入变成一个能用的地址；不能就抛 :class:`BadURL`（消息给人看）。

    只做三件事：去空白、校验协议与主机、保留其余原样。
    **不**替人补端口、**不**改路径 —— 猜错的地址比报错更难查。
    """
    s = str(raw if raw is not None else "").strip()
    if not s:
        raise BadURL("地址不能为空")
    if any(ch.isspace() for ch in s):
        raise BadURL("地址里不能有空格")
    m = _SHAPE.match(s)
    if not m:
        raise BadURL("地址要带协议，比如 ws://localhost:8888/")
    scheme = m.group("scheme").lower()
    if scheme not in SCHEMES:
        raise BadURL(f"只支持 {' / '.join(SCHEMES)}，收到的是 {scheme}://")
    rest = m.group("rest")
    hostport = re.split(r"[/?#]", rest, maxsplit=1)[0]
    if not hostport:
        raise BadURL("地址里没有主机名")
    if hostport.startswith("["):                      # [::1]:8888
        end = hostport.find("]")
        if end < 0:
            raise BadURL("IPv6 地址缺少 ]")
        tail = hostport[end + 1:]
        if tail and (not tail.startswith(":") or not tail[1:].isdigit()):
            raise BadURL(f"IPv6 地址后面的端口不对：{tail.lstrip(':')}")
    elif ":" in hostport:
        host, _, port = hostport.rpartition(":")
        if not host:                                  # ":8888" —— 没有主机
            raise BadURL("地址里没有主机名")
        if port and not port.isdigit():
            raise BadURL(f"端口不是数字：{port}")
    return f"{scheme}://{rest}"


def load(root: Path) -> dict:
    """读 ``config.json``；坏掉/不存在都当空 —— 一个坏文件不该让宿主起不来。"""
    try:
        d = json.loads((Path(root) / "config.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_url(root: Path, url: str) -> str:
    """把校验过的地址写进 ``config.json``（原子写，其它键原样保留）。"""
    url = normalize(url)
    root = Path(root)
    cfg = load(root)
    aaas = cfg.get("aaas")
    if not isinstance(aaas, dict):
        aaas = {}
    aaas["url"] = url
    cfg["aaas"] = aaas
    root.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(root), prefix=".config-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, root / "config.json")      # 同盘原子替换：不会读到半个文件
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return url


def hub_cfg(root: Path) -> dict:
    """``config.json`` → :class:`link.Hub` 要的那一份（扁平）。唯一翻译点。"""
    cfg = load(root)
    aaas = cfg.get("aaas") if isinstance(cfg.get("aaas"), dict) else {}
    agent = cfg.get("agent") if isinstance(cfg.get("agent"), dict) else {}
    out = dict(aaas)
    out.setdefault("url", DEFAULT_URL)
    out["revision"] = agent.get("revision", DEFAULT_REVISION)
    return out
