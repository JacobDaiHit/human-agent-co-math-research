"""All remote calls use mock transports; no credentials or external sockets needed."""

import asyncio
import json

import httpx
import pytest
from mathagent.providers.protocol import messages_for, validate_result
from mathagent.providers.remote import (
    DeepSeekProvider,
    GLMProvider,
    ProviderConfig,
    ProviderFailure,
    provider_status,
)


def task(mode="research"):
    return {
        "mode": mode,
        "goal_object_id": "goal",
        "target_revision_id": "version",
        "instruction": "Check the actual statement",
        "read_set": {"goal": "version"},
        "inputs": [{"object_id": "goal", "revision_id": "version", "body": "x=x"}],
        "proof_plans": [{"revision_id": "version", "dependencies": []}],
    }


def result(mode="research"):
    return {
        "mode": mode,
        "body": "Candidate argument only",
        "findings": ["Needs human review"],
        "cited_revision_ids": ["version"],
        "verdict": "inconclusive" if mode == "review" else None,
        "scope": "Only the displayed equation; no formal verification"
        if mode == "review"
        else None,
    }


def response(content=None, *, finish="stop"):
    return {
        "id": "vendor-request-1",
        "usage": {"total_tokens": 12},
        "choices": [
            {
                "finish_reason": finish,
                "message": {
                    "content": json.dumps(content or result()),
                },
            }
        ],
    }


@pytest.mark.parametrize("adapter,name", [(DeepSeekProvider, "deepseek"), (GLMProvider, "glm")])
@pytest.mark.parametrize("streaming", [True, False])
def test_structured_output_and_fixed_review_context(adapter, name, streaming):
    seen = []

    def transport(request):
        seen.append(request)
        if not streaming:
            return httpx.Response(200, json=response(result("review")))
        chunks = [
            {
                "id": "vendor-request-1",
                "choices": [
                    {"delta": {"content": json.dumps(result("review"))}, "finish_reason": None}
                ],
            },
            {"usage": {"total_tokens": 12}, "choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        wire = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
        return httpx.Response(
            200, content=wire + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"}
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            config = ProviderConfig(
                name, "synthetic-secret", "synthetic-model", "https://mock.invalid", True
            )
            output = await adapter(config, client).generate(task("review"))
            assert output["result"]["verdict"] == "inconclusive"
            assert output["usage"] == {"total_tokens": 12}

    asyncio.run(run())
    assert len(seen) == 1
    sent = json.loads(seen[0].content)
    assert sent["stream"] is True
    assert sent["response_format"] == {"type": "json_object"}
    assert "tools" not in sent
    context = json.loads(sent["messages"][1]["content"])
    assert context["target_revision_id"] == "version"
    assert context["inputs"][0]["body"] == "x=x"
    assert context["proof_plans"]


@pytest.mark.parametrize(
    "status,outcome,retryable",
    [
        (400, "unaccepted", False),
        (401, "unaccepted", False),
        (429, "unaccepted", True),
        (500, "unknown", False),
        (503, "unknown", False),
    ],
)
def test_http_errors_are_conservative_and_never_expose_body(status, outcome, retryable):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(status, text="synthetic-secret must not leak")
            )
        ) as client:
            provider = DeepSeekProvider(
                ProviderConfig("deepseek", "key", "model", "https://mock.invalid", True), client
            )
            with pytest.raises(ProviderFailure) as caught:
                await provider.generate(task())
            assert caught.value.outcome == outcome
            assert caught.value.retryable is retryable
            assert "synthetic-secret" not in str(caught.value)

    asyncio.run(run())


@pytest.mark.parametrize(
    "error,outcome,retryable",
    [
        (httpx.ConnectError, "unaccepted", True),
        (httpx.ReadTimeout, "unknown", False),
        (httpx.WriteError, "unknown", False),
    ],
)
def test_transport_failures_never_blindly_retry(error, outcome, retryable):
    calls = []

    def transport(request):
        calls.append(request)
        raise error("synthetic-secret")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            provider = DeepSeekProvider(
                ProviderConfig("deepseek", "key", "model", "https://mock.invalid", True), client
            )
            with pytest.raises(ProviderFailure) as caught:
                await provider.generate(task())
            assert (caught.value.outcome, caught.value.retryable) == (outcome, retryable)
            assert "synthetic-secret" not in str(caught.value)

    asyncio.run(run())
    assert len(calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {**result(), "cited_revision_ids": ["invented-version"]},
        {**result(), "execute_tool": "danger"},
        {**result(), "body": " "},
        {**result("review"), "mode": "review"},
    ],
)
def test_invalid_or_invented_structured_output_is_spent(payload):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response(payload)))
        ) as client:
            provider = DeepSeekProvider(
                ProviderConfig("deepseek", "key", "model", "https://mock.invalid", True), client
            )
            with pytest.raises(ProviderFailure) as caught:
                await provider.generate(task())
            assert caught.value.outcome == "spent"
            assert not caught.value.retryable

    asyncio.run(run())


@pytest.mark.parametrize(
    "wire",
    [
        'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
        "data: [DONE]\n\n",
        "data: not-json\n\n",
    ],
)
def test_interrupted_stream_is_unknown(wire):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, content=wire, headers={"content-type": "text/event-stream"}
                )
            )
        ) as client:
            provider = DeepSeekProvider(
                ProviderConfig("deepseek", "key", "model", "https://mock.invalid", True), client
            )
            with pytest.raises(ProviderFailure) as caught:
                await provider.generate(task())
            assert caught.value.outcome == "unknown"

    asyncio.run(run())


def test_disabled_provider_never_dispatches_and_status_is_boolean(monkeypatch):
    monkeypatch.setenv("MATHAGENT_ENABLE_REAL_API", "0")
    monkeypatch.setenv("MATHAGENT_DEEPSEEK_API_KEY", "synthetic-secret")
    monkeypatch.setenv("MATHAGENT_DEEPSEEK_MODEL", "synthetic-model")
    monkeypatch.setenv("MATHAGENT_GLM_API_KEY", "")
    monkeypatch.setenv("MATHAGENT_GLM_MODEL", "")
    status = provider_status()
    assert status["real_api_gate"] is False
    assert "synthetic-secret" not in json.dumps(status)
    assert "synthetic-model" not in json.dumps(status)

    async def run():
        def transport(_):
            pytest.fail("Disabled provider must not make a request")

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            with pytest.raises(ProviderFailure, match="provider_disabled"):
                await DeepSeekProvider(client=client).generate(task())

    asyncio.run(run())


def test_review_requires_scope_and_pinned_citations_are_explicit():
    with pytest.raises(ValueError):
        validate_result(
            {**result("review"), "scope": None}, mode="review", read_set={"goal": "version"}
        )
    with pytest.raises(ValueError):
        validate_result(
            {**result(), "cited_revision_ids": ["historical"]},
            mode="research",
            read_set={"goal": "version"},
        )
    accepted = validate_result(
        {**result(), "cited_revision_ids": ["historical"]},
        mode="research",
        read_set={"goal": "version"},
        context_revision_ids=["historical"],
    )
    assert accepted.cited_revision_ids == ["historical"]
    assert "scope" in messages_for(task("review"))[0]["content"]
