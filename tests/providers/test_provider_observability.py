"""Diagnostic retention and frozen requests, entirely on synthetic transports."""

import asyncio
import hashlib
import json
import time

import httpx
import pytest
from mathagent.providers import protocol, remote
from mathagent.providers.observability import MAX_RAW_TEXT, MAX_WIRE_BYTES, RequestObservation
from mathagent.providers.remote import DeepSeekProvider, ProviderConfig, ProviderFailure

SECRET = "synthetic-provider-secret"


def task(**changes):
    return {
        "mode": "research",
        "goal_object_id": "goal",
        "target_revision_id": "version",
        "read_set": {"goal": "version"},
        "instruction": "Inspect this exact equation",
        "inputs": [{"object_id": "goal", "revision_id": "version", "body": "x=x"}],
        **changes,
    }


def result(**changes):
    return {
        "mode": "research",
        "body": "Candidate equality argument",
        "findings": ["Check the definition of equality"],
        "cited_revision_ids": ["version"],
        **changes,
    }


def envelope(text, *, finish="stop", **changes):
    return {
        "id": "synthetic-request-id",
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        "choices": [{"message": {"content": text}, "finish_reason": finish}],
        **changes,
    }


def frame(content=None, *, finish=None, **changes):
    return (
        "data: "
        + json.dumps(
            {
                "id": "synthetic-request-id",
                "choices": [
                    {
                        "delta": {"content": content} if content is not None else {},
                        "finish_reason": finish,
                    }
                ],
                **changes,
            }
        )
        + "\n\n"
    ).encode()


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


def call(response_factory, assigned=None, *, url="https://mock.invalid"):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response_factory)) as client:
            provider = DeepSeekProvider(
                ProviderConfig("deepseek", SECRET, "synthetic-model", url, True), client
            )
            return await provider.generate(assigned or task())

    return asyncio.run(run())


def test_success_observation_matches_describe_and_exact_frozen_wire_request():
    assigned = task(provider_options={"max_output_tokens": 1024, "request_timeout_seconds": 45})
    captured = {}

    async def run():
        def transport(request):
            captured["payload"] = json.loads(request.content)
            assigned["provider_options"]["max_output_tokens"] = 4096
            assigned["read_set"]["goal"] = "mutated-version"
            assigned["inputs"][0]["body"] = "Mutated after dispatch"
            return httpx.Response(200, json=envelope(json.dumps(result())))

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            provider = DeepSeekProvider(
                ProviderConfig("deepseek", SECRET, "synthetic-model", "https://mock.invalid", True),
                client,
            )
            described = provider.describe(assigned)
            output = await provider.generate(assigned)
        observation = output["observation"]
        assert observation["call_config"] == described
        assert observation["raw_text"] == json.dumps(result())
        assert observation["finish_reason"] == "stop" and observation["complete"] is True
        assert (
            observation["usage"] == output["usage"] == {"prompt_tokens": 11, "completion_tokens": 7}
        )
        assert (
            observation["provider_request_id"]
            == output["provider_request_id"]
            == "synthetic-request-id"
        )
        assert captured["payload"]["max_tokens"] == 1024
        assert (
            json.loads(captured["payload"]["messages"][1]["content"])["inputs"][0]["body"] == "x=x"
        )
        assert described["request_timeout_seconds"] == 45
        assert described["parameters"]["max_tokens"] == 1024
        assert described["model"] == captured["payload"]["model"]
        canonical = json.dumps(
            captured["payload"]["messages"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        assert described["prompt_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
        template = captured["payload"]["messages"][0]["content"]
        assert described["prompt_template_sha256"] == hashlib.sha256(template.encode()).hexdigest()
        assert described["prompt_template_version"] == protocol.PROMPT_VERSION
        assert SECRET not in json.dumps(described)
        assert "mock.invalid" not in json.dumps(described)

    asyncio.run(run())


def test_default_parameters_remain_4096_tokens_with_finite_180_second_deadline():
    provider = DeepSeekProvider(
        ProviderConfig("deepseek", SECRET, "synthetic-model", "https://mock.invalid", True)
    )
    described = provider.describe(task())
    assert described["parameters"] == {
        "stream": True,
        "response_format": {"type": "json_object"},
        "max_tokens": 4096,
    }
    assert described["request_timeout_seconds"] == 180
    assert described["transport_timeout_seconds"] == {
        "connect": 10,
        "read": 120,
        "write": 120,
        "pool": 120,
    }


def test_response_model_is_separate_from_immutable_requested_model_and_redacted():
    response = call(lambda request: httpx.Response(200, json=envelope(
        json.dumps(result()), model="provider-actual-model")))
    observation = response["observation"]
    assert observation["call_config"]["model"] == "synthetic-model"
    assert observation["response_metadata"] == {"model": "provider-actual-model"}
    missing = call(lambda request: httpx.Response(200, json=envelope(json.dumps(result()))))
    assert missing["observation"]["response_metadata"] == {}
    private = call(lambda request: httpx.Response(200, json=envelope(json.dumps(result()), model=SECRET)))
    assert private["observation"]["response_metadata"] == {"model": "[redacted]"}


def test_streamed_model_label_survives_a_later_transport_failure():
    with pytest.raises(ProviderFailure) as error:
        call(lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks([
            frame("A partial argument", model="provider-streamed-model"), httpx.ReadError("interrupted")
        ])))
    assert error.value.observation["response_metadata"] == {"model": "provider-streamed-model"}


@pytest.mark.parametrize("streaming", [False, True])
def test_invalid_json_retains_complete_response_usage_id_and_configuration(streaming):
    raw = '{"mode":"research","body":not valid JSON'

    def transport(_):
        if not streaming:
            return httpx.Response(200, json=envelope(raw))
        return httpx.Response(
            200,
            stream=Chunks(
                [
                    frame(raw),
                    frame(finish="stop", usage={"total_tokens": 18}),
                    b"data: [DONE]\n\n",
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )

    with pytest.raises(ProviderFailure) as caught:
        call(transport)
    error = caught.value
    assert str(error) == "invalid_structured_output"
    assert error.outcome == "spent" and not error.retryable
    assert error.observation["raw_text"] == raw
    assert error.observation["complete"] is True
    assert error.observation["finish_reason"] == "stop"
    assert error.observation["usage"]
    assert error.observation["provider_request_id"] == "synthetic-request-id"
    assert error.observation["call_config"]["model"] == "synthetic-model"


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps(result()) + '\n{"unrelated": true}',
        json.dumps(result(body="first line\nsecond line")).replace("\\n", "\n"),
    ],
    ids=["second_top_level_value", "unescaped_control_character"],
)
def test_completed_malformed_json_is_spent_without_local_repair(raw):
    with pytest.raises(ProviderFailure) as caught:
        call(lambda _: httpx.Response(200, json=envelope(raw)))
    error = caught.value
    assert str(error) == "invalid_structured_output"
    assert error.outcome == "spent" and not error.retryable
    assert error.observation["raw_text"] == raw
    assert error.observation["complete"] is True
    assert error.observation["finish_reason"] == "stop"


def test_one_json_object_with_escaped_newline_is_accepted_without_rewriting():
    raw = json.dumps(result(body="first line\nsecond line"))
    output = call(lambda _: httpx.Response(200, json=envelope(raw)))
    assert output["result"]["body"] == "first line\nsecond line"
    assert output["observation"]["raw_text"] == raw


@pytest.mark.parametrize("streaming", [False, True])
def test_length_truncation_retains_prefix_and_trailing_usage(streaming):
    raw = '{"mode":"research","body":"partial'

    def transport(_):
        if not streaming:
            return httpx.Response(200, json=envelope(raw, finish="length"))
        return httpx.Response(
            200,
            stream=Chunks(
                [
                    frame(raw),
                    frame(finish="length"),
                    frame(usage={"total_tokens": 99}, choices=[]),
                    b"data: [DONE]\n\n",
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )

    with pytest.raises(ProviderFailure) as caught:
        call(transport)
    observation = caught.value.observation
    assert str(caught.value) == "incomplete_output"
    assert caught.value.outcome == "spent"
    assert observation["raw_text"] == raw
    assert observation["finish_reason"] == "length" and observation["complete"] is False
    assert observation["usage"] == (
        {"total_tokens": 99} if streaming else {"prompt_tokens": 11, "completion_tokens": 7}
    )


@pytest.mark.parametrize("ending", [None, httpx.ReadTimeout("synthetic transport secret")])
def test_interrupted_sse_preserves_accumulated_text_metadata_and_safe_failure(ending):
    chunks = [frame("prefix", usage={"total_tokens": 4})]
    if ending:
        chunks.append(ending)
    with pytest.raises(ProviderFailure) as caught:
        call(
            lambda _: httpx.Response(
                200, stream=Chunks(chunks), headers={"content-type": "text/event-stream"}
            )
        )
    error = caught.value
    assert error.outcome == "unknown" and not error.retryable
    assert error.observation["raw_text"] == "prefix"
    assert error.observation["usage"] == {"total_tokens": 4}
    assert error.observation["provider_request_id"] == "synthetic-request-id"
    assert error.observation["complete"] is False
    assert "synthetic transport secret" not in str(error)


def test_partial_nonstream_response_is_retained_after_read_failure():
    prefix = b'{"id":"partial-response", "choices":'
    with pytest.raises(ProviderFailure) as caught:
        call(
            lambda _: httpx.Response(
                200,
                stream=Chunks([prefix, httpx.ReadError("synthetic")]),
                headers={"content-type": "application/json"},
            )
        )
    assert caught.value.observation["raw_text"] == prefix.decode()
    assert caught.value.outcome == "unknown"


@pytest.mark.parametrize("error_type,code", [
    (httpx.ReadTimeout, "transport_read_timeout"),
    (httpx.WriteTimeout, "transport_write_timeout"),
    (httpx.RemoteProtocolError, "transport_remote_protocol_error"),
    (httpx.ReadError, "transport_read_error"),
    (httpx.WriteError, "transport_write_error"),
    (httpx.TransportError, "transport_outcome_unknown"),
    (httpx.ConnectError, "transport_connect_error"),
])
def test_post_response_transport_categories_are_safe_and_never_retryable(error_type, code):
    error = error_type(f"Private URL https://user:{SECRET}@mock.invalid/?credential={SECRET}")
    with pytest.raises(ProviderFailure) as caught:
        call(lambda _: httpx.Response(200,
            stream=Chunks([frame(), error]), headers={"content-type": "text/event-stream"}))
    failure = caught.value
    assert failure.code == code
    assert failure.outcome == "unknown" and not failure.retryable
    assert failure.observation["provider_request_id"] == "synthetic-request-id"
    assert SECRET not in str(failure) + json.dumps(failure.observation)
    assert "Private URL" not in str(failure) + json.dumps(failure.observation)


def test_connection_failure_before_response_remains_confirmed_unaccepted():
    def transport(_):
        raise httpx.ConnectError("Synthetic connection failure")
    with pytest.raises(ProviderFailure) as caught:
        call(transport)
    assert caught.value.code == "connection_not_established"
    assert caught.value.outcome == "unaccepted" and caught.value.retryable


def test_total_deadline_interrupts_a_stream_that_keeps_sending_heartbeats():
    class Endless(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield frame("prefix before deadline", usage={"total_tokens": 2})
            while True:
                await asyncio.sleep(0.03)
                yield b": keepalive\n\n"

    started = time.monotonic()
    with pytest.raises(ProviderFailure) as caught:
        call(
            lambda _: httpx.Response(
                200, stream=Endless(), headers={"content-type": "text/event-stream"}
            ),
            task(provider_options={"request_timeout_seconds": 1}),
        )
    assert time.monotonic() - started < 3
    assert str(caught.value) == "request_deadline_exceeded"
    assert caught.value.outcome == "unknown"
    assert caught.value.observation["raw_text"] == "prefix before deadline"
    assert caught.value.observation["usage"] == {"total_tokens": 2}
    assert caught.value.observation["complete"] is False


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"max_output_tokens": 0}, "invalid_max_output_tokens"),
        ({"max_output_tokens": 393217}, "invalid_max_output_tokens"),
        ({"max_output_tokens": True}, "invalid_max_output_tokens"),
        ({"request_timeout_seconds": 0}, "invalid_request_timeout"),
        ({"request_timeout_seconds": 3601}, "invalid_request_timeout"),
        ({"request_timeout_seconds": float("nan")}, "invalid_request_timeout"),
        ({"provider_options": {"model": "untrusted-override"}}, "invalid_provider_options"),
        ({"thinking_mode": "auto"}, "invalid_thinking_mode"),
        ({"thinking_mode": {}}, "invalid_thinking_mode"),
        ({"reasoning_effort": "xhigh"}, "invalid_reasoning_effort"),
        ({"reasoning_effort": True}, "invalid_reasoning_effort"),
        ({"thinking_mode": "disabled", "reasoning_effort": "max"}, "incompatible_thinking_options"),
    ],
)
def test_invalid_options_never_dispatch(changes, code):
    def transport(_):
        pytest.fail("Invalid configuration must be rejected before network dispatch")

    with pytest.raises(ProviderFailure) as caught:
        call(transport, task(**changes))
    assert str(caught.value) == code
    assert caught.value.outcome == "unaccepted"
    assert caught.value.observation["complete"] is False


@pytest.mark.parametrize("cap", [1, 131072, 393216])
def test_full_provider_output_range_is_sent_without_a_hidden_clamp(cap):
    captured = {}

    def transport(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=envelope(json.dumps(result())))

    call(transport, task(max_output_tokens=cap, request_timeout_seconds=3600,
                         thinking_mode="enabled", reasoning_effort="max"))
    assert captured["max_tokens"] == cap
    assert captured["thinking"] == {"type": "enabled"} and captured["reasoning_effort"] == "max"


def test_http_diagnostics_redact_known_key_and_url_credentials():
    body = {
        "error": {"message": SECRET + " https://synthetic-user:synthetic-password@mock.invalid"},
        "id": "receipt-" + SECRET,
        "usage": {"diagnostic": SECRET, "total_tokens": 3},
    }
    with pytest.raises(ProviderFailure) as caught:
        call(lambda _: httpx.Response(401, json=body))
    diagnostic = json.dumps(caught.value.observation)
    assert SECRET not in diagnostic
    assert "synthetic-user" not in diagnostic
    assert "synthetic-password" not in diagnostic
    assert "[redacted]" in diagnostic
    assert str(caught.value) == "http_401"


@pytest.mark.parametrize(
    "url", ["http://mock.invalid", "https://user:password@mock.invalid", "https://[broken"]
)
def test_invalid_urls_remain_unaccepted_without_leaking_credentials(url):
    def transport(_):
        pytest.fail("Unsafe URL must never be dispatched")

    with pytest.raises(ProviderFailure) as caught:
        call(transport, url=url)
    assert caught.value.outcome == "unaccepted"
    assert str(caught.value) == "invalid_provider_url"
    assert "password" not in json.dumps(caught.value.observation)


def test_raw_observation_and_wire_buffer_are_bounded():
    with pytest.raises(ProviderFailure) as caught:
        call(
            lambda _: httpx.Response(200, stream=Chunks([b"x" * (MAX_WIRE_BYTES + 100)])),
            task(max_output_tokens=256),
        )
    assert str(caught.value) == "response_too_large"
    assert len(caught.value.observation["raw_text"]) == min(MAX_RAW_TEXT, MAX_WIRE_BYTES)
    assert caught.value.observation["raw_text_truncated"] is True
    assert caught.value.observation["complete"] is False


@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_explicit_thinking_effort_is_frozen_on_wire_without_changing_model(effort):
    assigned = task(provider_options={
        "thinking_mode": "enabled", "reasoning_effort": effort,
        "max_output_tokens": 65536, "request_timeout_seconds": 600,
    })

    async def run():
        captured = {}

        def transport(request):
            captured.update(json.loads(request.content))
            assigned["provider_options"]["reasoning_effort"] = "low"
            return httpx.Response(200, json=envelope(json.dumps(result())))

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            provider = DeepSeekProvider(ProviderConfig(
                "deepseek", SECRET, "deepseek-v4-flash-vision-exp", "https://mock.invalid", True,
            ), client)
            description = provider.describe(assigned)
            output = await provider.generate(assigned)
        assert output["observation"]["call_config"] == description
        assert captured["thinking"] == description["parameters"]["thinking"] == {"type": "enabled"}
        assert captured["reasoning_effort"] == description["parameters"]["reasoning_effort"] == effort
        assert captured["max_tokens"] == 65536
        assert captured["model"] == description["model"] == "deepseek-v4-flash-vision-exp"
        assert description["request_timeout_seconds"] == 600

    asyncio.run(run())


def test_effort_enables_thinking_and_explicit_disabled_omits_effort():
    provider = DeepSeekProvider(ProviderConfig(
        "deepseek", SECRET, "deepseek-v4-flash", "https://mock.invalid", True,
    ))
    parameters = provider.describe(task(reasoning_effort="max"))["parameters"]
    assert parameters["thinking"] == {"type": "enabled"}
    assert parameters["reasoning_effort"] == "max"
    parameters = provider.describe(task(thinking_mode="disabled"))["parameters"]
    assert parameters["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in parameters


def test_deepseek_thinking_options_are_not_silently_ignored_by_other_providers():
    provider = remote.GLMProvider(ProviderConfig(
        "glm", SECRET, "synthetic-model", "https://mock.invalid", True,
    ))
    with pytest.raises(ProviderFailure, match="unsupported_thinking_options"):
        provider.describe(task(thinking_mode="enabled", reasoning_effort="high"))


def test_large_thinking_stream_uses_explicit_budget_and_does_not_save_reasoning():
    reasoning = ("data: " + json.dumps({"choices": [{
        "delta": {"reasoning_content": "synthetic-private-reasoning-" * 64},
        "finish_reason": None,
    }]}) + "\n\n").encode()
    reasoning_chunks = [reasoning] * (MAX_WIRE_BYTES // len(reasoning) + 1)
    assert sum(map(len, reasoning_chunks)) > MAX_WIRE_BYTES
    output = call(
        lambda _: httpx.Response(200, stream=Chunks([
            *reasoning_chunks, frame(json.dumps(result())),
            frame(finish="stop", usage={"completion_tokens": 20000}), b"data: [DONE]\n\n",
        ]), headers={"content-type": "text/event-stream"}),
        task(max_output_tokens=65536, thinking_mode="enabled", reasoning_effort="max"),
    )
    observation = output["observation"]
    assert observation["complete"] is True
    assert observation["raw_text"] == json.dumps(result())
    assert observation["usage"]["completion_tokens"] == 20000
    assert "synthetic-private-reasoning" not in json.dumps(output)


def test_tool_calls_are_rejected_after_retaining_available_text_and_usage():
    wire = envelope("partial explanation", finish="tool_calls")
    wire["choices"][0]["message"]["tool_calls"] = [{"function": {"name": "never_execute"}}]
    with pytest.raises(ProviderFailure) as caught:
        call(lambda _: httpx.Response(200, json=wire))
    assert str(caught.value) == "tool_calls_disallowed"
    assert caught.value.observation["raw_text"] == "partial explanation"
    assert caught.value.observation["finish_reason"] == "tool_calls"
    assert caught.value.observation["usage"]["prompt_tokens"] == 11


def test_injected_client_cannot_enable_redirect_following():
    seen = []

    async def run():
        def transport(request):
            seen.append(str(request.url))
            return httpx.Response(307, headers={"Location": "https://other.invalid/receive"})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport), follow_redirects=True
        ) as client:
            provider = DeepSeekProvider(
                ProviderConfig("deepseek", SECRET, "synthetic-model", "https://mock.invalid", True),
                client,
            )
            with pytest.raises(ProviderFailure, match="http_307"):
                await provider.generate(task())

    asyncio.run(run())
    assert seen == ["https://mock.invalid/chat/completions"]


def test_failure_keeps_the_same_configuration_as_predispatch_description():
    async def run():
        transport = httpx.MockTransport(
            lambda _: httpx.Response(200, json=envelope("unfinished model JSON", finish="length"))
        )
        async with httpx.AsyncClient(transport=transport) as client:
            provider = DeepSeekProvider(
                ProviderConfig("deepseek", SECRET, "synthetic-model", "https://mock.invalid", True),
                client,
            )
            assigned = task(
                provider_options={"max_output_tokens": 2048, "request_timeout_seconds": 90}
            )
            described = provider.describe(assigned)
            with pytest.raises(ProviderFailure) as caught:
                await provider.generate(assigned)
            assert caught.value.observation["call_config"] == described

    asyncio.run(run())


def test_default_client_disables_proxy_inheritance_and_redirects(monkeypatch):
    original_client = httpx.AsyncClient
    observed = {}

    def factory(**kwargs):
        observed.update(kwargs)
        transport = httpx.MockTransport(
            lambda _: httpx.Response(200, json=envelope(json.dumps(result())))
        )
        return original_client(transport=transport, **kwargs)

    monkeypatch.setattr(remote.httpx, "AsyncClient", factory)

    async def run():
        provider = DeepSeekProvider(
            ProviderConfig("deepseek", SECRET, "synthetic-model", "https://mock.invalid", True)
        )
        await provider.generate(task())

    asyncio.run(run())
    assert observed["trust_env"] is False
    assert observed["follow_redirects"] is False


def test_success_and_split_stream_raw_text_cannot_echo_credentials():
    raw = json.dumps(
        result(body=SECRET + " https://synthetic-user:synthetic-password@mock.invalid/path")
    )
    split = raw.index(SECRET) + 8
    output = call(
        lambda _: httpx.Response(
            200,
            stream=Chunks(
                [
                    frame(raw[:split]),
                    frame(raw[split:]),
                    frame(finish="stop"),
                    b"data: [DONE]\n\n",
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )
    )
    serialized = json.dumps(output)
    assert SECRET not in serialized
    assert "synthetic-password" not in serialized
    assert "synthetic-user" not in serialized
    assert "[redacted]" in output["result"]["body"]


def test_redaction_and_usage_growth_are_also_bounded():
    observation = RequestObservation(
        ProviderConfig("deepseek", "x", "synthetic-model", "https://mock.invalid", True)
    )
    observation.set_raw("x" * MAX_RAW_TEXT)
    for number in range(100):
        observation.metadata({"usage": {str(number): "x" * 1000}})
    snapshot = observation.snapshot()
    assert len(snapshot["raw_text"]) <= MAX_RAW_TEXT
    assert snapshot["raw_text_truncated"] is True
    assert len(json.dumps(snapshot["usage"])) <= 16_000
