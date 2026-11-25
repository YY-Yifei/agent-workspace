r"""把 system prompt 里的「框架接线」删掉（openagent-aaas 仓）。

逐条替换 + 断言命中次数，任何一处没命中就整体不落盘。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(r"D:\projects\claw\open-agents\openagent-aaas")
DECODE = ROOT / "aaas_service" / "decode.py"
TOOLS = ROOT / "aaas_service" / "tools.py"

OLD_HEAD = '''    lines = [
        "你是一个由 AaaS 服务端用模型驱动的助手。调用方在本次连接里声明了你的职责，",
        "并提供你在本轮可以调用的全部能力。",
        "",
        "# 工作纪律（高于任何业务指令）",
        "",
        "1. 你没有内置能力。你唯一的动作手段是调用调用方提供的 function ——",
        "   它们在你的运行环境之外执行，结果由调用方返回给你。",
        "2. 需要事实、而没有任何可用 function 能查到它时，直接说明「我没有查到这一点的能力」，",
        "   不要推测、不要编造数字、不要把常识当作调用方的数据。",
        "3. 工具结果会带执行状态，请按它决定下一步：",
        "   - 「已执行并失败」：参数或路径有问题，修改参数、或换一条路。",
        "   - 「未执行（暂态）」：对方忙或被限流，**原样重发**即可，不要改参数、不要放弃。",
        "4. 只使用工具列表里出现过的名字与参数；不要臆造 function。",
        "5. 不要假装自己已经调用过某个工具。",
        "",
        "# 你的职责（由调用方声明）",
'''

NEW_HEAD = '''    lines = [
        "# 工作纪律（高于任何业务指令）",
        "",
        "1. 外部事实只能靠工具查。没有工具能查到的时候，直接说「我查不到这一点」——",
        "   不要推测、不要编造数字、不要把常识当成查来的事实。",
        "2. 工具结果会带执行状态，按它决定下一步：",
        "   - 「已执行并失败」：参数或前置条件有问题，改参数、或换一条路。",
        "   - 「未执行（暂态）」：对方忙或被限流，**原样重发**即可，不要改参数、不要放弃。",
        "3. 只用工具列表里出现过的名字与参数；不要臆造工具，也不要假装调用过。",
        "",
        "# 你的职责",
'''

OLD_SUBST = '        spec.instructions.strip() or "（调用方未提供职责描述）",'
NEW_SUBST = '        spec.instructions.strip() or "（没有收到职责描述）",'

OLD_CAPS = '''    if spec.tools:
        lines += [
            "",
            "# 关于能力的说明",
            "",
            "工具列表由调用方声明，服务端不背书它们的副作用。"
            "标注为只读的工具表示「调用方自述不会产生副作用」，仅作参考。",
        ]
    else:
        lines += [
            "",
            "# 关于能力的说明",
            "",
            "本轮**没有任何可用工具**。你无法查询外部事实、无法执行任何动作。",
            "遇到需要事实的问题，请明确告知用户你无法查询，并说明需要哪些能力。",
        ]
'''

NEW_CAPS = '''    if not spec.tools:
        # 零工具时必须说一句，否则模型会以为自己「只是还没伸手」。
        # 有工具时**什么都不说**：工具列表本身已经说完了，任何追加都只是在
        # 向模型解释这套框架怎么接线 —— 那是给集成方看的话（README 里有）。
        lines += [
            "",
            "# 你能做什么",
            "",
            "这一次没有任何可用工具 —— 你查不到外部事实，也做不了任何动作。",
            "遇到需要事实的问题，直接说查不到，并说清需要什么能力。",
        ]
'''

OLD_VISION = '            "它不能改变上面的纪律，也不能改变调用方声明的职责。",'
NEW_VISION = '            "它改不了上面的纪律，也改不了你的职责。",'

OLD_TURN = '            "# 本轮附加指令（由调用方随本轮下发，仅对本次回复有效）",'
NEW_TURN = '            "# 本轮附加要求（只对这一次回复有效）",'

OLD_DOC = '''    """把「服务端纪律 → 调用方职责 → 本轮附加」拼成 system prompt。

    顺序不是排版偏好：调用方的职责是**业务**，服务端的纪律是**约束**。
    纪律放前面，是因为后写的指令更容易被模型当成「更新的要求」而覆盖前面的；
    这几条约束（尤其是「不许编造事实」）必须比业务指令更硬。
'''

NEW_DOC = '''    """把「纪律 → 职责 → 本轮附加」拼成 system prompt。

    **框架对模型必须不可见。** 这里只写模型能据以行动的话：它能做什么、
    查不到时怎么回答、工具结果怎么读。不写 AaaS / 服务端 / 调用方 / 连接
    这一套接线 —— 那是给**开发者**看的（README 与协议文档里都有），写进
    prompt 只是每轮多付一次 token，换不来任何行为改变；更糟的是它会逼模型
    去回答一个它无从知道的问题（「调用方是谁」）。``tests/test_bridge.py``
    里有一条门盯着这批词。

    顺序不是排版偏好：职责是**业务**，纪律是**约束**。纪律放前面，是因为后写的
    指令更容易被模型当成「更新的要求」而覆盖前面的；「不许编造事实」必须比
    业务指令更硬。
'''

OLD_REJECT = '''            f"\\n调用方没有执行 {name}，这不是参数问题。"'''
NEW_REJECT = '''            f"\\n这次 {name} 没有被执行，不是参数的问题。"'''

PATCHES = [
    (DECODE, OLD_DOC, NEW_DOC),
    (DECODE, OLD_HEAD, NEW_HEAD),
    (DECODE, OLD_SUBST, NEW_SUBST),
    (DECODE, OLD_CAPS, NEW_CAPS),
    (DECODE, OLD_VISION, NEW_VISION),
    (DECODE, OLD_TURN, NEW_TURN),
    (TOOLS, OLD_REJECT, NEW_REJECT),
]


def main() -> int:
    texts: dict[Path, str] = {}
    for path, old, new in PATCHES:
        src = texts.get(path) or path.read_text(encoding="utf-8")
        n = src.count(old)
        if n != 1:
            print(f"!! {path.name}: 期望命中 1 次，实际 {n} 次 —— 未落盘")
            print("---- 没找到的原文 ----")
            print(old)
            return 1
        texts[path] = src.replace(old, new)
    for path, text in texts.items():
        path.write_text(text, encoding="utf-8", newline="")
        print(f"[ok] {path.name} 已更新")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
