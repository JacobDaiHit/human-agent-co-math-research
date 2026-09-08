"""A deterministic local fixture, deliberately incapable of mathematical research."""

import hashlib
import json
from collections.abc import Mapping


class FakeProvider:
    """Produce a bounded, reproducible draft without tools, network, or model calls."""

    name = "fake"
    simulated = True
    supports_cancellation = False
    external_requests = False

    def generate(
        self,
        *,
        goal: str,
        instruction: str = "",
        read_set: Mapping[str, str] | None = None,
    ) -> str:
        inputs = {"goal": goal, "instruction": instruction, "read_set": dict(read_set or {})}
        canonical = json.dumps(inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        return (
            "# FakeProvider 测试草稿（模拟输出）\n\n"
            "这是确定性脚本生成的测试夹具，未调用真实模型；不构成证明、审查或研究结论。\n\n"
            f"输入指纹：`{fingerprint}`\n"
            f"固定输入版本数：{len(inputs['read_set'])}\n\n"
            f"## 当前目标（节选）\n{goal[:1200]}\n\n"
            f"## 执行指令（节选）\n{instruction[:800] or '无额外指令'}\n\n"
            "## 待办\n"
            "- 核对前提与目标的含义。\n"
            "- 补充可验证的证明或反例。\n"
            "- 通过独立审查后再决定是否采用。\n\n"
            "证据状态：草稿；尚未验证。\n"
        )
