"""Agent Workspace —— 道 = Agent（aaas-service），一 = 内核（本包）。

只有四样东西：
    kernel.py  一：目录约定 / 唯一 dispatch / 事件流 / 能力面（不含一克智能）
    link.py    道：aaas 接线（connect / declare / send / 转发被点名的那一个）
    server.py  二之一：人的门（本地 HTTP + SSE）
    web/       前端：工作区视图 + 对话 + 自动能力面板
"""
