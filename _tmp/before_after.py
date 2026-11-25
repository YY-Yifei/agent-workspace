r"""改前 / 改后对照：从 git 取出 HEAD 版 decode.py，与工作区版各拼一次 prompt。

同一个假 spec、两条判据（同一个 spec 喂给新旧两版），所以差值就是这次改动。
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(r"D:\projects\claw\open-agents\openagent-aaas")
sys.path.insert(0, str(ROOT))
from srcpath import ensure_aaas_on_path  # noqa: E402

ensure_aaas_on_path(ROOT)

import aaas  # noqa: E402


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    head = subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(ROOT), "show", "HEAD:aaas_service/decode.py"],
        capture_output=True,
        check=True,
    ).stdout
    tmp = Path(tempfile.mkdtemp(prefix="old-decode-")) / "decode_old.py"
    tmp.write_bytes(head)
    old = _load(tmp, "_decode_old")
    new = _load(ROOT / "aaas_service" / "decode.py", "_decode_new")

    decl = aaas.FunctionDeclaration(
        name="prices",
        description="查价格",
        parameters={"type": "object", "properties": {}},
        timeout_ms=5000,
        metadata=None,
    )
    instructions = "你是小助理。用户让你做什么，你就用你手上的能力做完它。"  # 27 字符，兜底身份
    for label, functions, extra in (
        ("10 项工具 + 兜底身份（≈ 装机首用）", (decl,) * 10, None),
        ("零工具（= 调用方没声明任何能力）", (), None),
    ):
        for vision in (False, True):
            for name, mod in (("改前", old), ("改后", new)):
                spec = mod.decode_definition(
                    aaas.AgentDefinition(
                        revision="v1", instructions=instructions, functions=functions
                    )
                )
                prompt = mod.build_system_prompt(spec, extra, vision=vision)
                banned = [w for w in ("AaaS", "服务端", "调用方", "声明", "function") if w in prompt]
                print(
                    f"{label:<34} vision={vision!s:<5} {name}：{len(prompt):>4} 字符"
                    f"  框架词 {banned or '无'}"
                )
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
