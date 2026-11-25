r"""给 aaas 仓的 test_bridge.py 补门：框架词不许出现在模型读到的文字里。"""
from __future__ import annotations

from pathlib import Path

PATH = Path(r"D:\projects\claw\open-agents\openagent-aaas\tests\test_bridge.py")

OLD_ASSERT = '''        assert "本轮**没有任何可用工具**" in prompt
        assert "不要推测" in prompt
'''

NEW_ASSERT = '''        assert "没有任何可用工具" in prompt
        assert "不要推测" in prompt

    #: 这套接线上的词：一个都不许出现在**模型读到的文字**里。模型不需要知道
    #: AaaS 怎么把一份声明变成 Agent —— 它知道了也只能去猜「调用方是谁」，
    #: 而这个问题它无从回答；猜错的代价是一次编造，或者一句「我没法知道」。
    FRAMEWORK_WORDS = (
        "AaaS",
        "服务端",
        "调用方",
        "本次连接",
        "下发",
        "协议",
        "声明",
        "function",
    )

    def test_prompt_never_names_the_framework(self) -> None:
        """四种组合（有/无工具 × 看/不看图）里都不许出现框架词。

        这是本仓最容易复发的一类改写：加一段说明时顺手写「调用方声明的…」，
        读起来毫无问题，但模型读到的是一个它无法解析的主语。
        """
        for functions in ((), (decl("prices"),)):
            for vision in (False, True):
                prompt = build_system_prompt(
                    decode_definition(definition(*functions, instructions="")),
                    None,
                    vision=vision,
                )
                hits = [word for word in self.FRAMEWORK_WORDS if word in prompt]
                assert hits == [], (functions, vision, hits, prompt)

    def test_prompt_stays_short(self) -> None:
        """基底 prompt 有长度上限 —— 防它再胖回去。

        夹带「服务端不背书副作用」这类免责声明时，长度是唯一会报警的指标：
        它不改变任何行为，只会让每一轮都多付一次 token。当前约 250 字符。
        """
        base = build_system_prompt(
            decode_definition(definition(decl("prices"), instructions="")), None
        )
        assert len(base) <= 360, base
'''

OLD_THREE = '''        assert "未执行" in rendered
        assert "原样重发" in rendered
        assert "busy" in rendered
'''

NEW_THREE = '''        assert "未执行" in rendered
        assert "原样重发" in rendered
        assert "busy" in rendered

    def test_render_never_names_the_framework(self) -> None:
        """渲染文本是模型**直接读到**的原文 —— 这里同样不许出现框架词。

        ``调用方没有执行 X`` 里的「调用方」对模型是个悬空主语：它看不到协议、
        也没有「调用方」这个概念，只会在下一句里把这个词复述给用户。
        """
        for result in (
            aaas.FunctionResult.ok("102.33"),
            aaas.FunctionResult.error("BAD_CODE", "找不到该债券"),
            aaas.FunctionResult.rejected("busy"),
        ):
            rendered, _ = self._render(result)
            for word in ("调用方", "服务端", "AaaS", "协议"):
                assert word not in rendered, (word, rendered)
'''

PATCHES = [
    (OLD_ASSERT, NEW_ASSERT),
    (OLD_THREE, NEW_THREE),
]


def main() -> int:
    src = PATH.read_text(encoding="utf-8")
    for old, new in PATCHES:
        if src.count(old) != 1:
            print(f"!! 期望命中 1 次，实际 {src.count(old)} 次 —— 未落盘")
            print(old)
            return 1
        src = src.replace(old, new)
    PATH.write_text(src, encoding="utf-8", newline="")
    print("[ok] test_bridge.py 已更新")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
