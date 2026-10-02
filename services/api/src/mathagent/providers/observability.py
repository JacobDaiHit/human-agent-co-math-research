"""Bounded, credential-redacted observations for one frozen provider request."""

import copy
import json
import math
import re
from urllib.parse import unquote, urlparse

MAX_RAW_TEXT = 16_000_000
MAX_WIRE_BYTES = 2_000_000
_URL_CREDENTIALS = re.compile(r"(https?://)[^\s/@]+@", re.IGNORECASE)


class RequestObservation:
    def __init__(self, config, call_config=None):
        try:
            parsed = urlparse(config.base_url)
        except ValueError:
            parsed = urlparse("")
        self._secrets = tuple(
            sorted(
                {
                    value
                    for value in (
                        config.api_key,
                        parsed.username,
                        parsed.password,
                        unquote(parsed.username or ""),
                        unquote(parsed.password or ""),
                    )
                    if value
                },
                key=len,
                reverse=True,
            )
        )
        self.call_config = copy.deepcopy(call_config or {})
        self.raw_text = ""
        self.raw_text_truncated = False
        self.finish_reason = None
        self.complete = False
        self.usage = {}
        self.provider_request_id = None
        self.response_metadata = {}

    def redact(self, text):
        for secret in self._secrets:
            text = text.replace(secret, "[redacted]")
        return _URL_CREDENTIALS.sub(r"\1[redacted]@", text)

    def _safe_value(self, value, depth=0):
        if depth > 4:
            return None
        if isinstance(value, str):
            return self.redact(value[:1000])
        if isinstance(value, dict):
            return {
                self.redact(str(k)[:100]): self._safe_value(v, depth + 1)
                for k, v in list(value.items())[:64]
            }
        if isinstance(value, list):
            return [self._safe_value(v, depth + 1) for v in value[:64]]
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if value is None or isinstance(value, (int, float, bool)):
            return value
        return None

    def set_raw(self, value):
        self.raw_text = value[:MAX_RAW_TEXT]
        self.raw_text_truncated = len(value) > MAX_RAW_TEXT

    def append_raw(self, value):
        available = MAX_RAW_TEXT - len(self.raw_text)
        self.raw_text += value[:available]
        self.raw_text_truncated |= len(value) > available

    def metadata(self, data):
        if not isinstance(data, dict):
            return
        if isinstance(data.get("usage"), dict):
            self.usage.update(self._safe_value(data["usage"]))
            self.usage = dict(list(self.usage.items())[:64])
            while len(json.dumps(self.usage, ensure_ascii=False)) > 16_000:
                self.usage.pop(next(reversed(self.usage)))
        if isinstance(data.get("id"), str):
            self.provider_request_id = self.redact(data["id"])[:300]
        if isinstance(data.get("model"), str):
            self.response_metadata["model"] = self.redact(data["model"])[:300]

    def snapshot(self):
        raw_text = self.raw_text
        if self.raw_text_truncated:
            # A retained prefix may end halfway through a known credential.
            # Remove that suffix too; chunk boundaries cannot defeat redaction.
            for secret in self._secrets:
                for length in range(min(len(secret) - 1, len(raw_text)), 0, -1):
                    if raw_text.endswith(secret[:length]):
                        raw_text = raw_text[:-length] + "[redacted]"
                        break
        raw_text = self.redact(raw_text)
        return {
            "raw_text": raw_text[:MAX_RAW_TEXT],
            "raw_text_truncated": self.raw_text_truncated or len(raw_text) > MAX_RAW_TEXT,
            "finish_reason": self.redact(self.finish_reason)[:100] if self.finish_reason else None,
            "complete": self.complete,
            "usage": copy.deepcopy(self.usage),
            "provider_request_id": self.provider_request_id,
            "response_metadata": copy.deepcopy(self.response_metadata),
            "call_config": self._safe_value(self.call_config),
        }


def empty_observation():
    return {
        "raw_text": "",
        "raw_text_truncated": False,
        "finish_reason": None,
        "complete": False,
        "usage": {},
        "provider_request_id": None,
        "response_metadata": {},
        "call_config": {},
    }


def cache_usage(calls):
    known = [call.get("usage") or {} for call in calls if all(
        type((call.get("usage") or {}).get(key)) is int and call["usage"][key] >= 0
        for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"))]
    hit = sum(value["prompt_cache_hit_tokens"] for value in known) if known else None
    miss = sum(value["prompt_cache_miss_tokens"] for value in known) if known else None
    denominator = hit + miss if known else 0
    return {"calls_with_cache_usage": len(known), "calls_without_cache_usage": len(calls) - len(known),
            "hit_tokens": hit, "miss_tokens": miss,
            "hit_fraction": hit / denominator if denominator else None}


def usage_summary(calls):
    keys = ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens")
    totals = {key: 0 for key in keys[:3]}
    missing = 0
    for call in calls:
        usage = call.get("usage") or {}
        if not all(type(usage.get(k)) is int and usage[k] >= 0 for k in keys[:3]):
            missing += 1
        for key in keys[:3]:
            if type(usage.get(key)) is int and usage[key] >= 0:
                totals[key] += usage[key]
    cache = cache_usage(calls)
    totals.update(prompt_cache_hit_tokens=cache["hit_tokens"], prompt_cache_miss_tokens=cache["miss_tokens"])
    members, dialogues = {}, {}
    for call in calls:
        context = (call.get("call_config") or {}).get("research_context") or {}
        name = context.get("member") or "unassigned"
        members.setdefault(name, []).append(call)
        if context.get("dialogue_id"):
            dialogues.setdefault(context["dialogue_id"], []).append(call)
    return {"reported_tokens": totals, "calls_with_incomplete_usage": missing,
            "all_usage_known": missing == 0, "includes_all_members_and_recovery": True,
            "cache": cache,
            "by_researcher": {name: {"requests": len(group), "cache": cache_usage(group),
                "input_tokens": sum((call.get("usage") or {}).get("prompt_tokens", 0) for call in group
                                    if type((call.get("usage") or {}).get("prompt_tokens")) is int),
                "output_tokens": sum((call.get("usage") or {}).get("completion_tokens", 0) for call in group
                                     if type((call.get("usage") or {}).get("completion_tokens")) is int)}
                for name, group in members.items()},
            "conversations": [{"dialogue_id": key,
                "member": group[0]["call_config"]["research_context"].get("member"),
                "reset_reason": group[0]["call_config"]["research_context"].get("reset_reason"),
                "requests": len(group), "cache": cache_usage(group),
                "prefix_changes": sum(call["call_config"]["research_context"].get("previous_input_preserved") is False
                                      for call in group)} for key, group in dialogues.items()]}
