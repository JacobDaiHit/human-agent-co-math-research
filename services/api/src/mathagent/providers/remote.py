"""Small chat-completion adapters, gated before any real network dispatch.

One generate call means one HTTP request: retry/accounting belongs to the worker.
No provider response body or credentials are included in errors.
"""

import asyncio
import codecs
import contextlib
import copy
import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

import httpx
from mathagent.providers import protocol
from mathagent.providers.observability import MAX_WIRE_BYTES, RequestObservation, empty_observation
from mathagent.providers.options import MAX_OUTPUT_TOKENS

DEFAULT_URLS = {
    "deepseek": "https://api.deepseek.com",
    "glm": "https://open.bigmodel.cn/api/paas/v4",
}


@dataclass(frozen=True)
class Capabilities:
    streaming: bool = True
    json_output: bool = True
    native_json_schema: bool = False
    executable_tools: bool = False
    cancellation: bool = False
    request_retrieval: bool = False
    server_idempotency: bool = False


@dataclass(frozen=True, repr=False)
class ProviderConfig:
    name: str
    api_key: str
    model: str
    base_url: str
    enabled: bool

    @classmethod
    def from_env(cls, name):
        if name not in DEFAULT_URLS:
            raise ValueError("Unknown provider")
        prefix = "MATHAGENT_" + name.upper()
        return cls(
            name,
            os.getenv(prefix + "_API_KEY", ""),
            os.getenv(prefix + "_MODEL", ""),
            os.getenv(prefix + "_BASE_URL", DEFAULT_URLS[name]).rstrip("/"),
            os.getenv("MATHAGENT_ENABLE_REAL_API") == "1",
        )

    def ready(self):
        return self.enabled and bool(self.api_key.strip() and self.model.strip())


def provider_status():
    providers = [
        {
            "provider": "fake",
            "configured": True,
            "enabled": True,
            "simulated": True,
            "key_configured": False,
            "model_configured": True,
            "capabilities": asdict(Capabilities(streaming=False, json_output=True)),
        }
    ]
    for name in DEFAULT_URLS:
        config = ProviderConfig.from_env(name)
        providers.append(
            {
                "provider": name,
                "configured": bool(config.api_key and config.model),
                "key_configured": bool(config.api_key),
                "model_configured": bool(config.model),
                "enabled": config.ready(),
                "simulated": False,
                "capabilities": asdict(Capabilities()),
            }
        )
    return {"real_api_gate": os.getenv("MATHAGENT_ENABLE_REAL_API") == "1", "providers": providers}


class ProviderFailure(Exception):
    def __init__(self, code, *, outcome="unknown", retryable=False, observation=None):
        super().__init__(code)
        self.code = code
        self.outcome = outcome
        self.retryable = retryable
        self.observation = (
            copy.deepcopy(observation) if observation is not None else empty_observation()
        )


def transport_failure_code(error):
    """Allowlisted categories, never exception text, URLs, headers or OS messages."""
    for error_type, code in (
        (httpx.ReadTimeout, "transport_read_timeout"),
        (httpx.WriteTimeout, "transport_write_timeout"),
        (httpx.ConnectTimeout, "transport_connect_timeout"),
        (httpx.PoolTimeout, "transport_pool_timeout"),
        (httpx.RemoteProtocolError, "transport_remote_protocol_error"),
        (httpx.LocalProtocolError, "transport_local_protocol_error"),
        (httpx.ReadError, "transport_read_error"),
        (httpx.WriteError, "transport_write_error"),
        (httpx.ConnectError, "transport_connect_error"),
        (httpx.ProxyError, "transport_proxy_error"),
    ):
        if isinstance(error, error_type):
            return code
    return "transport_outcome_unknown"


class RemoteProvider:
    capabilities = Capabilities()
    simulated = False

    def __init__(self, config, client=None):
        self.config = config
        self.client = client

    @staticmethod
    def _prepare(task, config):
        task = copy.deepcopy(task)
        options = task.get("provider_options", {})
        if not isinstance(options, dict) or set(options) - {
            "max_output_tokens",
            "request_timeout_seconds",
            "thinking_mode",
            "reasoning_effort",
        }:
            raise ProviderFailure("invalid_provider_options", outcome="unaccepted")
        max_tokens = options.get("max_output_tokens", task.get("max_output_tokens", 4096))
        deadline = options.get("request_timeout_seconds", task.get("request_timeout_seconds", 180))
        thinking = options.get("thinking_mode", task.get("thinking_mode", "provider_default"))
        effort = options.get("reasoning_effort", task.get("reasoning_effort", "provider_default"))
        if (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or not 256 <= max_tokens <= MAX_OUTPUT_TOKENS
        ):
            raise ProviderFailure("invalid_max_output_tokens", outcome="unaccepted")
        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)
            or not 1 <= deadline <= 600
        ):
            raise ProviderFailure("invalid_request_timeout", outcome="unaccepted")
        if thinking not in ("provider_default", "enabled", "disabled"):
            raise ProviderFailure("invalid_thinking_mode", outcome="unaccepted")
        if effort not in ("provider_default", "low", "high", "max"):
            raise ProviderFailure("invalid_reasoning_effort", outcome="unaccepted")
        if thinking == "disabled" and effort != "provider_default":
            raise ProviderFailure("incompatible_thinking_options", outcome="unaccepted")
        if config.name != "deepseek" and (thinking != "provider_default" or effort != "provider_default"):
            raise ProviderFailure("unsupported_thinking_options", outcome="unaccepted")
        messages = protocol.messages_for(task)
        parameters = {
            "stream": True,
            "response_format": {"type": "json_object"},
            "max_tokens": max_tokens,
        }
        if thinking != "provider_default":
            parameters["thinking"] = {"type": thinking}
        if effort != "provider_default":
            # An explicit effort always requests thinking, independent of server defaults.
            parameters["thinking"] = {"type": "enabled"}
            parameters["reasoning_effort"] = effort
        canonical = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        template = "\n".join(m["content"] for m in messages if m["role"] == "system")
        call_config = {
            "provider": config.name,
            "model": config.model,
            "parameters": copy.deepcopy(parameters),
            "request_timeout_seconds": deadline,
            "transport_timeout_seconds": {
                "connect": min(10, deadline),
                "read": min(120, deadline),
                "write": min(120, deadline),
                "pool": min(120, deadline),
            },
            "prompt_template_version": protocol.PROMPT_VERSION,
            "prompt_template_sha256": hashlib.sha256(template.encode("utf-8")).hexdigest(),
            "prompt_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        }
        payload = {"model": config.model, "messages": messages, **parameters}
        return task, payload, call_config

    def describe(self, task):
        """Describe the effective request without credentials, URLs, or prompt text."""
        _, _, call_config = self._prepare(task, self.config)
        return RequestObservation(self.config, call_config).snapshot()["call_config"]

    async def generate(self, task):
        config = self.config
        observation = RequestObservation(config, {"provider": config.name, "model": config.model})
        own_client = self.client is None
        client = self.client
        response_started = False
        try:
            task, payload, call_config = self._prepare(task, config)
            observation.call_config = call_config
            if not config.ready():
                raise ProviderFailure("provider_disabled", outcome="unaccepted")
            try:
                parsed = urlparse(config.base_url)
            except ValueError:
                raise ProviderFailure("invalid_provider_url", outcome="unaccepted") from None
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ProviderFailure("invalid_provider_url", outcome="unaccepted")
            if sum(len(m["content"]) for m in payload["messages"]) > 200_000:
                raise ProviderFailure("input_too_large", outcome="unaccepted")
            deadline = call_config["request_timeout_seconds"]
            timeout = httpx.Timeout(min(120, deadline), connect=min(10, deadline))
            client = client or httpx.AsyncClient(
                timeout=timeout, follow_redirects=False, trust_env=False
            )
            async with asyncio.timeout(deadline):
                async with client.stream(
                    "POST",
                    config.base_url + "/chat/completions",
                    json=payload,
                    headers={"Authorization": "Bearer " + config.api_key},
                    timeout=timeout,
                    follow_redirects=False,
                ) as response:
                    response_started = True
                    if response.status_code != 200:
                        status = response.status_code
                        raw, _ = await self._read_body(response, observation)
                        observation.set_raw(raw.decode("utf-8", errors="replace"))
                        with contextlib.suppress(ValueError, TypeError):
                            observation.metadata(json.loads(raw))
                        if status in {400, 401, 402, 403, 404, 422, 429}:
                            raise ProviderFailure(
                                "rate_limit" if status == 429 else f"http_{status}",
                                outcome="unaccepted",
                                retryable=status == 429,
                            )
                        raise ProviderFailure(f"http_{status}")
                    content = await self._decode(
                        response, observation,
                        wire_limit=max(MAX_WIRE_BYTES, payload["max_tokens"] * 1024),
                    )
                try:
                    result = protocol.validate_result(
                        json.loads(observation.redact(content)),
                        mode=task["mode"],
                        read_set=task["read_set"],
                        context_revision_ids=task.get("context_revision_ids", []),
                        autonomous=bool(task.get("autonomous")),
                    )
                except (ValueError, TypeError):
                    raise ProviderFailure("invalid_structured_output", outcome="spent") from None
                return {
                    "result": result.model_dump(),
                    "usage": copy.deepcopy(observation.usage),
                    "provider_request_id": observation.provider_request_id,
                    "observation": observation.snapshot(),
                }
        except ProviderFailure as error:
            error.observation = observation.snapshot()
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as error:
            raise ProviderFailure(
                transport_failure_code(error) if response_started else "connection_not_established",
                outcome="unknown" if response_started else "unaccepted",
                retryable=not response_started,
                observation=observation.snapshot(),
            ) from None
        except TimeoutError:
            raise ProviderFailure(
                "request_deadline_exceeded", observation=observation.snapshot()
            ) from None
        except httpx.TransportError as error:
            raise ProviderFailure(
                transport_failure_code(error), observation=observation.snapshot()
            ) from None
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ProviderFailure(
                "invalid_provider_response", observation=observation.snapshot()
            ) from None
        finally:
            if own_client and client is not None:
                with contextlib.suppress(httpx.TransportError, TimeoutError):
                    await asyncio.wait_for(client.aclose(), timeout=5)

    @staticmethod
    async def _read_body(response, observation, *, wire_limit=MAX_WIRE_BYTES):
        body = bytearray()
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        async for chunk in response.aiter_bytes():
            available = wire_limit - len(body)
            body.extend(chunk[:available])
            observation.append_raw(decoder.decode(chunk[:available]))
            if len(chunk) > available:
                observation.raw_text_truncated = True
                return bytes(body), True
        observation.append_raw(decoder.decode(b"", final=True))
        return bytes(body), False

    @staticmethod
    async def _lines(response, observation, *, wire_limit=MAX_WIRE_BYTES):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buffer, size = "", 0
        async for chunk in response.aiter_bytes():
            available = wire_limit - size
            size += len(chunk)
            buffer += decoder.decode(chunk[:available])
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                yield line.rstrip("\r")
            if size > wire_limit:
                if not observation.raw_text:
                    observation.set_raw(buffer)
                observation.raw_text_truncated = True
                raise ProviderFailure("response_too_large", outcome="spent")
        buffer += decoder.decode(b"", final=True)
        if buffer:
            yield buffer.rstrip("\r")

    @staticmethod
    async def _decode(response, observation, *, wire_limit=MAX_WIRE_BYTES):
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" not in content_type:
            raw, overflow = await RemoteProvider._read_body(response, observation, wire_limit=wire_limit)
            observation.set_raw(raw.decode("utf-8", errors="replace"))
            if overflow:
                observation.raw_text_truncated = True
                raise ProviderFailure("response_too_large", outcome="spent")
            try:
                data = json.loads(raw)
                observation.metadata(data)
                choice = data["choices"][0]
                reason = choice.get("finish_reason")
                observation.finish_reason = reason if isinstance(reason, str) else None
                observation.complete = reason == "stop"
                content = choice["message"].get("content")
                if isinstance(content, str):
                    observation.set_raw(content)
                if choice["message"].get("tool_calls"):
                    raise ProviderFailure("tool_calls_disallowed", outcome="spent")
                if reason != "stop":
                    raise ProviderFailure("incomplete_output", outcome="spent")
                if not isinstance(content, str):
                    raise ProviderFailure("invalid_provider_response", outcome="spent")
                return content
            except (ValueError, KeyError, TypeError, IndexError, AttributeError):
                raise ProviderFailure("invalid_provider_response", outcome="spent") from None
        parts, finished, done = [], False, False
        try:
            async for line in RemoteProvider._lines(response, observation, wire_limit=wire_limit):
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    done = True
                    break
                try:
                    data = json.loads(raw)
                except ValueError:
                    if not observation.raw_text:
                        observation.set_raw(raw)
                    raise
                observation.metadata(data)
                if data.get("error"):
                    raise ProviderFailure("stream_provider_error")
                for choice in data.get("choices", []):
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if content is not None and not isinstance(content, str):
                        raise ProviderFailure("invalid_stream")
                    if content:
                        parts.append(content)
                        observation.append_raw(content)
                    reason = choice.get("finish_reason")
                    if isinstance(reason, str):
                        observation.finish_reason = reason
                    if delta.get("tool_calls"):
                        raise ProviderFailure("tool_calls_disallowed", outcome="spent")
                    finished = finished or reason == "stop"
        except (ValueError, TypeError, AttributeError):
            raise ProviderFailure("invalid_stream", outcome="unknown") from None
        if observation.finish_reason and observation.finish_reason != "stop":
            raise ProviderFailure("incomplete_output", outcome="spent")
        if not done or not finished:
            raise ProviderFailure("stream_interrupted")
        observation.complete = True
        return "".join(parts)


class DeepSeekProvider(RemoteProvider):
    def __init__(self, config=None, client=None):
        super().__init__(config or ProviderConfig.from_env("deepseek"), client)


class GLMProvider(RemoteProvider):
    def __init__(self, config=None, client=None):
        super().__init__(config or ProviderConfig.from_env("glm"), client)
