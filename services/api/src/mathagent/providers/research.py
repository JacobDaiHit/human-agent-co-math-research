"""Text-first mathematical research with ordinary, provider-native tool calls.

Only executable requests have a schema. Mathematical prose is never parsed as an
approval, a proof-completeness score, or a workbench-management packet.
"""

import copy
import json
from typing import Literal

from pydantic import BaseModel, Field

PROMPT_VERSION = "continuous-research-v3"


class ReadMaterial(BaseModel):
    ref: str
    offset: int = Field(default=0, ge=0)
    length: int = Field(default=50000, ge=1, le=50000)


class SaveNote(BaseModel):
    scope: Literal["personal", "shared", "material"]
    body: str = Field(min_length=1)
    title: str = ""


class AssignWork(BaseModel):
    member: Literal["self", "peer"]
    goal: str = Field(min_length=1)
    independent: bool = False
    materials: list[str] = Field(default_factory=list)


class SendMessage(BaseModel):
    recipient: Literal["lead", "peer"]
    topic: str
    body: str = Field(min_length=1)
    wait: bool = False


class FinishWork(BaseModel):
    summary: str = ""


class SubmitSolution(BaseModel):
    outcome: Literal["solved", "unresolved"]
    answer: str | None = None
    body_ref: str | None = None


class Compute(BaseModel):
    code: str
    timeout_seconds: int = Field(default=5, ge=1, le=10)


ARGUMENT_MODELS = {"read_material": ReadMaterial, "save_note": SaveNote,
    "assign_work": AssignWork, "send_message": SendMessage, "finish_work": FinishWork,
    "submit_solution": SubmitSolution, "compute": Compute}


def arguments_for(name, value):
    if name not in ARGUMENT_MODELS:
        raise ValueError("Unknown research tool")
    return ARGUMENT_MODELS[name].model_validate(value).model_dump(exclude_none=True)

INSTRUCTION = """研究给定的数学问题。当前任务可以是原题，也可以是一个局部问题。
这是持续研究，不是要求每次调用都独自想完整题并交一份最终报告。
每次聚焦一个能推进的小目标；得到关系式、失败原因或新的困难后，及时保存阶段成果。
若一个方向仍需较长探索，可以先保存已经得到的推导，再调整自己的局部任务继续。
起步时先理解题目、选择一个具体的小目标；需要独立的不同思路时邀请同伴。
选择目标时直接调用工具落实安排，不要先独自探索整题。目标简短、具体，推导留给选定的研究任务。
正文中的公式使用 LaTeX 显示分隔符；这只是显示方式，不是完成条件。
把推导写成普通数学正文；需要材料或计算时调用工具，拿到结果后接着研究。
记录重要结论、失败原因、当前困难和下一步；长证明与计算可以另存后按需读取。
保留题目的全部条件和量词。计算结果的适用范围由你判断，程序不认证数学证明。
研究安排由你决定：继续、换方法、拆出困难的局部任务或组合已有成果。
同伴是另一位研究者，不是审批者。围绕具体推导交流，不因身份、信心或意见一致
而接受结论；也不为了反对而反对。改变判断时记录起作用的推导、计算或反例。
独立探索任务先形成自己的推导，再交换对方材料。不要求每次合作重新独立解题。
完成局部任务用 finish_work；主研究者完成原题用 submit_solution 明确提交。
若还要继续研究，用工具安排下一任务或继续操作，不要只结束正文让整个研究空等。
已有解答随正文提交，不必另写收尾摘要。简单题可以直接提交，无需固定的起步报告。
未解出时可以提交进展，但不要把猜测描述为已完成的证明。无需固定审查或收尾。
工具返回的材料名称可直接用于后续读取；版本和归属由后台维护，不要编造编号。
所有成员共享总请求和输出额度。不要把一项简单推导拆成一串管理任务。
"""


def _tool(name, description, properties, required=()):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": list(required), "additionalProperties": False},
    }}


def tools_for(*, compute=False, discussion=True, lead=True):
    text = {"type": "string"}
    tools = [
        _tool("read_material", "按名称读取原题、工作稿或保存的完整材料。", {
            "ref": text, "offset": {"type": "integer", "minimum": 0},
            "length": {"type": "integer", "minimum": 1, "maximum": 50000},
        }, ("ref",)),
        _tool("save_note", "保存个人或公共工作稿，或另存完整推导。不是数学认证。", {
            "scope": {"type": "string", "enum": ["personal", "shared", "material"]},
            "body": text, "title": text,
        }, ("scope", "body")),
        _tool("assign_work", "继续、替换自己的研究任务，或请同伴研究一个具体问题。", {
            "member": {"type": "string", "enum": ["self", "peer"]},
            "goal": text, "independent": {"type": "boolean"},
            "materials": {"type": "array", "items": text},
        }, ("member", "goal")),
        _tool("finish_work", "结束当前局部任务。阶段成果会保存并送回主研究者。", {
            "summary": text,
        }),
        _tool("submit_solution", "主研究者提交原题的解答或未解决进展。无需审查通过。", {
            "answer": text, "body_ref": text,
            "outcome": {"type": "string", "enum": ["solved", "unresolved"]},
        }, ("outcome",)),
    ]
    if compute:
        tools.append(_tool("compute", "在离线隔离环境中运行 Python；可使用 SymPy。", {
            "code": text, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 10},
        }, ("code",)))
    if discussion:
        tools.append(_tool("send_message", "围绕具体数学话题给另一成员发消息，可提问或回复。", {
            "recipient": {"type": "string", "enum": ["lead", "peer"]},
            "topic": text, "body": text, "wait": {"type": "boolean"},
        }, ("recipient", "topic", "body")))
    else:
        tools[2]["function"]["parameters"]["properties"]["member"]["enum"] = ["self"]
    if not lead:
        tools = [tool for tool in tools if tool["function"]["name"] != "submit_solution"]
        note = next(tool for tool in tools if tool["function"]["name"] == "save_note")
        note["function"]["parameters"]["properties"]["scope"]["enum"] = ["personal", "material"]
    return tools


def messages_for(task):
    """The runtime owns task selection; the adapter preserves its conversation."""
    budget = task.get("request_budget_status", {})
    remaining = budget.get("remaining")
    instruction = INSTRUCTION
    if remaining is not None:
        instruction += "\n本次回答之后，整个研究可用的后续模型调用最多还有 " + str(max(0, remaining - 1)) + " 次。"
    return [{"role": "system", "content": instruction},
            *copy.deepcopy(task["conversation"])]


def parse_message(value, *, allow_reasoning_only=False):
    """Validate transport shape, not mathematical content or tool arguments.

    Invalid arguments remain a normal tool error, returned to the same research
    conversation. They do not discard the mathematical body or restart it.
    """
    if not isinstance(value, dict):
        raise ValueError("Expected an assistant message")
    content = value.get("content") or ""
    reasoning = value.get("reasoning_content")
    calls = value.get("tool_calls") or []
    if not isinstance(content, str) or not isinstance(calls, list):
        raise ValueError("Invalid assistant message")
    message = {"role": "assistant", "content": content}
    if reasoning is not None:
        if not isinstance(reasoning, str):
            raise ValueError("Invalid provider continuation")
        message["reasoning_content"] = reasoning
    if calls:
        normalized = []
        for call in calls:
            function = call.get("function", {})
            if not isinstance(call.get("id"), str) or not call["id"]:
                raise ValueError("Tool call has no identity")
            if not isinstance(function.get("name"), str):
                raise ValueError("Tool call has no name")
            arguments = function.get("arguments", "")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            normalized.append({"id": call["id"], "type": "function", "function": {
                "name": function["name"], "arguments": arguments,
            }})
        message["tool_calls"] = normalized
    if not content and not calls and not (allow_reasoning_only and reasoning):
        raise ValueError("Assistant produced no visible output or tool request")
    return message


def partial_message(observation):
    """A known length stop can retain research, but cannot execute tool fragments."""
    if observation.get("finish_reason") != "length" or observation.get("raw_text_truncated"):
        return None
    try:
        value = json.loads(observation.get("raw_text", ""))
        message = {"role": "assistant", "content": value.get("content") or ""}
        if value.get("reasoning_content") is not None:
            message["reasoning_content"] = value["reasoning_content"]
        return parse_message(message, allow_reasoning_only=True)
    except (ValueError, TypeError, AttributeError):
        return None
