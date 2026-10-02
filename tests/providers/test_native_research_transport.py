"""Native transport conversations; all transports are local engineering fixtures."""

import asyncio
import json

import httpx
import pytest
from mathagent.providers import research
from mathagent.providers.remote import (
    DeepSeekProvider,
    GLMProvider,
    ProviderConfig,
    ProviderFailure,
)


def task(conversation=None):
    return {"research_protocol": True, "mode": "research", "max_output_tokens": 4096,
            "conversation": conversation or [{"role": "user", "content": "研究局部问题。"}],
            "tools": research.tools_for(compute=True)}


def tool_message(arguments='{"code":"print(1 + 1)"}'):
    return {"role": "assistant", "content": "先计算，再推导。",
            "reasoning_content": "provider-supplied continuation fixture",
            "tool_calls": [{"id": "call-1", "type": "function", "function": {
                "name": "compute", "arguments": arguments}}]}


@pytest.mark.parametrize("adapter,name", [(DeepSeekProvider, "deepseek"), (GLMProvider, "glm")])
@pytest.mark.parametrize("streaming", [False, True])
def test_plain_mathematics_and_fragmented_native_tools(adapter, name, streaming):
    requests = []
    message = tool_message()

    def transport(request):
        requests.append(json.loads(request.content))
        if not streaming:
            return httpx.Response(200, json={"id": "fixture", "usage": {"completion_tokens": 8},
                "choices": [{"message": message, "finish_reason": "tool_calls"}]})
        fragments = [
            {"reasoning_content": message["reasoning_content"][:10]},
            {"reasoning_content": message["reasoning_content"][10:], "content": message["content"]},
            {"tool_calls": [{"index": 0, "id": "call-1", "type": "function",
                "function": {"name": "compute", "arguments": '{"code":'}}]},
            {"tool_calls": [{"index": 0, "function": {"arguments": '"print(1 + 1)"}'}}]},
        ]
        events = [{"choices": [{"delta": value}]} for value in fragments]
        events.append({"id": "fixture", "usage": {"completion_tokens": 8},
                       "choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
        wire = "".join("data: " + json.dumps(value) + "\n\n" for value in events)
        return httpx.Response(200, text=wire + "data: [DONE]\n\n",
                              headers={"content-type": "text/event-stream"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            provider = adapter(ProviderConfig(name, "synthetic-secret", "fixture-model",
                                              "https://fixture.invalid", True), client)
            output = await provider.generate(task())
            assert output["result"] == {"message": message}
            assert output["observation"]["complete"] is True
            assert output["observation"]["finish_reason"] == "tool_calls"
            assert output["usage"]["completion_tokens"] == 8
    asyncio.run(run())
    assert "response_format" not in requests[0]
    assert requests[0]["tools"] == research.tools_for(compute=True)
    assert requests[0]["messages"][1]["content"] == "研究局部问题。"


def test_provider_continuation_and_tool_results_are_replayed_without_reformatting():
    message = tool_message()
    history = [{"role": "user", "content": "研究原题。"}, message,
               {"role": "tool", "tool_call_id": "call-1", "content": "2"}]
    sent = []

    def transport(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": "计算得到 $2$。证明没有缺口；正文可包含多个展示公式。"}}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            provider = DeepSeekProvider(ProviderConfig("deepseek", "synthetic-secret", "fixture",
                                                       "https://fixture.invalid", True), client)
            output = await provider.generate(task(history))
            assert output["result"]["message"]["content"].startswith("计算得到")
    asyncio.run(run())
    assert sent[0]["messages"][1:] == history
    assert history[1]["reasoning_content"] == message["reasoning_content"]


def test_bad_tool_arguments_do_not_erase_mathematical_body():
    message = tool_message("not valid JSON")
    assert research.parse_message(message) == message


def test_disabled_capabilities_are_absent_not_described_as_forbidden_roles():
    tools = research.tools_for(compute=False, discussion=False)
    names = {tool["function"]["name"] for tool in tools}
    assert "compute" not in names and "send_message" not in names
    assert "assign_work" not in names
    assert "continue_research" in names
    assert "verdict" not in research.INSTRUCTION
    assert "completion_requirements" not in research.INSTRUCTION


def test_partial_output_is_saved_but_not_used_as_a_completed_tool_request():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200,
            json={"choices": [{"finish_reason": "length", "message": tool_message()}]}))) as client:
            provider = DeepSeekProvider(ProviderConfig("deepseek", "synthetic-secret", "fixture",
                                                       "https://fixture.invalid", True), client)
            with pytest.raises(ProviderFailure) as caught:
                await provider.generate(task())
            assert caught.value.code == "incomplete_output"
            assert caught.value.observation["raw_text"]
            assert caught.value.observation["complete"] is False
    asyncio.run(run())


def test_streamed_reasoning_is_retained_when_the_provider_hits_its_output_limit():
    events = [
        {"choices": [{"delta": {"reasoning_content": "unfinished mathematical exploration"}}]},
        {"id": "length-fixture", "usage": {"completion_tokens": 32},
         "choices": [{"delta": {}, "finish_reason": "length"}]},
    ]
    wire = "".join("data: " + json.dumps(value) + "\n\n" for value in events) + "data: [DONE]\n\n"

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, text=wire, headers={"content-type": "text/event-stream"}))) as client:
            provider = DeepSeekProvider(ProviderConfig("deepseek", "synthetic-secret", "fixture",
                                                       "https://fixture.invalid", True), client)
            with pytest.raises(ProviderFailure) as caught:
                await provider.generate(task())
            observed = caught.value.observation
            assert caught.value.code == "incomplete_output"
            assert observed["complete"] is False
            assert observed["usage"]["completion_tokens"] == 32
            assert json.loads(observed["raw_text"])["reasoning_content"] == "unfinished mathematical exploration"
    asyncio.run(run())


def test_context_capacity_only_uses_explicit_or_known_official_values():
    def config(model="unknown-model", url="https://fixture.invalid", capacity=None):
        return ProviderConfig("deepseek", "synthetic-secret", model, url, True, capacity)
    assert config().context_length() is None
    assert config("deepseek-v4-flash").context_length() is None
    assert config(url="https://api.deepseek.com").context_length() is None
    assert config("deepseek-v4-flash", "https://api.deepseek.com").context_length() == 1000000
    assert config(capacity=123456).context_length() == 123456
    with pytest.raises(ValueError, match="positive"):
        config(capacity=0)
