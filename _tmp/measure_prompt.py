r"""量 build_system_prompt 的实际输出（改前 / 改后对照），并做框架词体检。

跑法：python _tmp\measure_prompt.py
"""
from __future__ import annotations

import sys
from pathlib import Path

AAAS_ROOT = Path(r"D:\projects\claw\open-agents\openagent-aaas")
sys.path.insert(0, str(AAAS_ROOT))
from srcpath import ensure_aaas_on_path  # noqa: E402

ensure_aaas_on_path(AAAS_ROOT)

import aaas  # noqa: E402
from aaas_service.decode import build_system_prompt, decode_definition  # noqa: E402

BANNED = ("AaaS", "服务端", "调用方", "本次连接", "下发", "协议", "声明", "function")


def decl(name: str = "prices") -> aaas.FunctionDeclaration:
    return aaas.FunctionDeclaration(
        name=name,
        description="一个测试用能力",
        parameters={"type": "object", "properties": {}},
        timeout_ms=5000,
        metadata=None,
    )


def main() -> int:
    for label, functions, instructions in (
        ("全新安装（零工具、兜底身份）", (), ""),
        ("有工具、调用方给了职责", (decl(),), "你是一个测试助手"),
    ):
        for vision in (False, True):
            spec = decode_definition(
                aaas.AgentDefinition(
                    revision="v1", instructions=instructions, functions=tuple(functions)
                )
            )
            prompt = build_system_prompt(
                spec, "补一句" if functions else None, vision=vision
            )
            hits = [w for w in BANNED if w in prompt]
            print(
                f"\n== {label} / vision={vision}：{len(prompt)} 字符 / "
                f"{prompt.count(chr(10)) + 1} 行 / 框架词 {hits or '无'} =="
            )
            print(prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
