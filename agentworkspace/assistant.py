"""内置程序插件：**小助理**（pid = ``assistant``）。

整个内核里只内置这一个程序，而且它在数据结构上和别的插件**没有区别**：
有 pid、有标题、有能力面、有自己的智能体上下文（自己的 aaas 会话 / 自己的指令）。

它独有的只有两件事：

    1. **手**  —— tree / read / write / search / shell（toolbox 里那些动词）
    2. **造程序** —— plugin_write / plugin_verify / plugin_delete / plugin_list / plugin_call

于是「谁能造程序」不是提示词里劝出来的规矩，而是**结构**：
别的程序的智能体拿不到 ``assistant_plugin_write`` 这个能力 —— 它们的能力面里
压根没有这一项（见 ``Runtime.capabilities_of``）。
"""

from __future__ import annotations

PID = "assistant"

PROGRAM = {
    "title": "小助理",
    "icon": "💬",
    "description": "什么都能聊；也能给你造新程序",
}


def install(rt) -> str:
    """把小助理挂上内核（调用一次，装配期）。"""
    from . import devtools, toolbox

    toolbox._install(rt)
    devtools._install(rt)
    dev = rt.mount_devtools(devtools)          # 开发者模式借到的那双手（不属于任何程序）
    return f"{rt.mount(toolbox, PID, program=PROGRAM)}；{dev}"
