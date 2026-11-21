"""**开发者工具集** —— 一个程序「改自己」时借到的那双手。

它不是程序：没有 pid、没有界面、不在左侧栏；它是一套**能力**。
某个程序的人打开了开发者模式（``workspace/dev.json``），那个程序的智能体
就能看见它；关掉立刻收回（见 ``Runtime.capabilities_of``）。

能力面是**故意小**的，用户的原话就是判据：

    看：``dev_tree`` ``dev_read`` ``dev_search``
    写：``dev_write``         —— 工作区内的普通文件（不碰程序目录、不碰开关）
    改：``dev_plugin_write``  —— 改**自己**：**没有 pid 参数**，改别人在签名上就不可能

没有 ``plugin_list`` / ``plugin_call`` / ``plugin_delete`` / ``plugin_verify`` /
``shell`` —— 开发者模式是「给你一把改自己的螺丝刀」，不是「给你一整套造别人
程序的能力」，也不是「给你一个终端」。边界写在**能力面**上，比写在提示词里
劝模型「别乱来」可靠得多：模型看不见的能力，它连试都不会试。

归属也不变：这些手写的是 ``dev_*``，工具卡上写「🛠 开发者 · write」——
用户一眼能分清「这是它自己的手」还是「小助理的手」。
"""

from __future__ import annotations

_RT = None                                     # 由 _install() 注入


def _install(rt) -> None:
    global _RT
    _RT = rt


def _rt():
    if _RT is None:                            # 理论上不可达：装配先于使用
        raise RuntimeError("内核未装配")
    return _RT


def _me() -> str:
    """此刻动手的是**哪个程序**。内核的 caller 栈是唯一来源。

    为什么不用参数传 pid：一旦 pid 是参数，模型就能填别人的名字 ——
    那时「只能改自己」只能靠「拒绝」来守；而「改自己」这件事是这一层
    唯一的用途，它根本不需要知道名字。少一个参数，就少一条能走错的路。
    """
    me = _rt().caller()
    if not me or me not in _rt().plugins:
        raise ValueError("这条路只给打开了开发者模式的程序用：此刻不知道是哪个程序在动手")
    return me


# ------------------------------------------------------------------ 看

def tree(max_depth: int = 3) -> dict:
    """看工作区结构（只读）。你自己的文件在 plugins/<你的 id>/ 下。

    max_depth: 递归深度，默认 3
    """
    from . import toolbox

    out = toolbox.tree(max_depth)
    me = _rt().caller()
    if me in _rt().plugins:
        out["text"] += f"\n\n（你是 {me}：改自己用 dev_plugin_write）"
    return out


def read(path: str) -> dict:
    """读一个文件（工作区内）。改自己之前，先把当前那一版读出来。

    path: 相对工作区的路径，如 plugins/<你的 id>/backend.py
    """
    from . import toolbox

    return toolbox.read(path)


def search(pattern: str, path: str = ".", limit: int = 30) -> dict:
    """在工作区里按文本检索，返回「文件:行号: 内容」。

    pattern: 要找的文本
    path: 检索起点（相对工作区），默认整个工作区
    limit: 最多返回多少条
    """
    from . import toolbox

    return toolbox.search(pattern, path, limit)


# ------------------------------------------------------------------ 写

def write(path: str, content: str) -> dict:
    """写一个普通文本文件（工作区内）。**不要用来改自己** —— 改自己用 dev_plugin_write。

    path: 相对工作区的路径（plugins/**、dev.json、config.json 会被拒）
    content: 完整内容
    """
    from . import toolbox

    return toolbox.write(path, content)


def plugin_write(files: dict) -> dict:
    """提交**你自己**的新版本：一次提交整份文件，四道校验后才落盘。

    提交的是整份文件，不是补丁：backend.py 里原有的函数、注解、docstring
    都得带上，否则那个能力（和用户用的那个按钮）就没了。

    files: {文件名: 内容}，只接受 plugin.json / backend.py / ui/**；
           data/（用户攒下的数据）永远不许碰

    校验不过会**整份回滚**到提交前那一版，你什么也没改成。
    上一版留在 workspace/.backup/<你的 id>/，要退回去就把它再提交一遍。
    """
    me = _me()
    from . import toolbox

    caps, kind = toolbox._commit(me, files)      # 与小助理的 plugin_write 同一条落盘路径
    how = ("你的界面是原生窗口（ui/app.py）—— 用户点开就弹那个窗口。"
           if kind == "native" else
           "你的界面是网页（ui/index.html）—— 用户刷新一下就是新的。")
    return {"text": f"**改的是你自己**：{me} 现在是这一版，能力 {caps}。\n"
                    f"{how}\n下一轮你的上下文里就是这套能力。\n"
                    f"（上一版留在 workspace/.backup/{me}/，要退回去用 plugin_write 提交它。）",
            "plugin": me, "functions": caps, "self": True}
