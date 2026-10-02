"""Tool-free DeepSeek baselines: one shot or a fixed, gold-blind self-refinement chain."""

import asyncio
import re
import time

import httpx
from mathagent.evaluation.outcomes import submission, usage_summary
from mathagent.providers.mathematical_guidance import SOLUTION_SET_GUIDANCE
from mathagent.providers.observability import RequestObservation
from mathagent.runtime.completion import boxed_answers

INDEPENDENT_SELECTION_RULE = "independent-single-box-whitespace-vote-earliest-v1"

DIRECT_INSTRUCTION = r"""Solve the given Olympiad problem. No internet, tools, external
references, answer key or human hints are available. Explain your reasoning and use
LaTeX for all mathematics. Submit your full mathematical argument. If there is a
separate short answer, you may put it in \boxed{...}; a proof-only submission is
valid. If the proof is incomplete, state what remains unresolved honestly rather
than inventing an answer. A submission does not certify the proof.
""" + "\n" + SOLUTION_SET_GUIDANCE
REFINE_INSTRUCTION = "Independently check your previous reasoning for errors, correct any you find, and submit your best final answer. State unresolved proof gaps. Do not assume the previous answer was correct."


def output_occupancy(calls):
    total = 0
    for call in calls:
        used = (call.get("usage") or {}).get("completion_tokens")
        total += used if call["state"] == "spent" and type(used) is int and used >= 0 else call["reserved_output_tokens"]
    return total


async def run_direct_case(case, directory, config, limits, *, transport_factory=None):
    if limits.solver == "independent_samples":
        return await run_independent_samples_case(case, directory, config, limits,
                                                   transport_factory=transport_factory)
    from mathagent.evaluation.answerbench import CompletionOnlyTransport, stamp, write_json

    directory.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    calls, messages = [], [{"role": "system", "content": DIRECT_INSTRUCTION},
                          {"role": "user", "content": case["problem"]}]
    transport = CompletionOnlyTransport(config.base_url + "/chat/completions", directory,
        inner=transport_factory(case) if transport_factory else None)
    final_body, terminal, unknown_retries = None, "completed", 0
    ledger = directory / "baseline-calls.json"
    output_budget = limits.cumulative_output_token_budget
    occupied_output = 0
    async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False) as client:
        for index in range(limits.request_budget):
            remaining_seconds = limits.case_timeout_seconds - (time.monotonic() - started)
            if remaining_seconds <= 0:
                terminal = "case_timeout"
                break
            maximum = min(limits.max_output_tokens, output_budget - occupied_output) if output_budget is not None else limits.max_output_tokens
            if maximum < 1:
                terminal = "output_token_budget_exhausted"
                break
            call = {"number": index + 1, "state": "dispatched", "usage": {},
                    "reserved_output_tokens": maximum, "created_at": stamp()}
            calls.append(call)
            occupied_output += maximum
            # Durable occupancy precedes dispatch; a crash is never a free retry.
            write_json(ledger, {"calls": calls, "occupied_output_tokens": occupied_output})
            final_body = None
            payload = {"model": config.model, "messages": messages, "stream": False,
                       "max_tokens": maximum, "thinking": {"type": "enabled"},
                       "reasoning_effort": limits.reasoning_effort}
            try:
                async with asyncio.timeout(min(remaining_seconds, limits.request_timeout_seconds)):
                    response = await client.post(config.base_url + "/chat/completions", json=payload,
                        headers={"Authorization": "Bearer " + config.api_key}, timeout=limits.request_timeout_seconds)
                    response.raise_for_status()
                    value = response.json()
                if not isinstance(value, dict):
                    raise ValueError("Invalid completion object")
                usage = value.get("usage") or {}
                if not isinstance(usage, dict):
                    raise ValueError("Invalid usage object")
                observation = RequestObservation(config)
                observation.metadata(value)
                call.update(state="spent", usage=usage, provider_request_id=value.get("id"),
                            response_metadata=observation.snapshot()["response_metadata"])
                used = usage.get("completion_tokens")
                if type(used) is int and 0 <= used <= maximum:
                    occupied_output -= maximum - used
                choices = value.get("choices") or []
                if not isinstance(choices, list) or (choices and not isinstance(choices[0], dict)):
                    raise ValueError("Invalid completion choice")
                choice = choices[0] if choices else {}
                call["finish_reason"] = choice.get("finish_reason")
                message = choice.get("message") or {}
                if not isinstance(message, dict):
                    raise ValueError("Invalid completion message")
                content = message.get("content")
                if choice.get("finish_reason") != "stop" or not isinstance(content, str) or not content.strip():
                    terminal = "incomplete_output"
                    break
                final_body = content
                write_json(directory / f"response-{index + 1}.json", {"body": content, "usage": usage,
                    "finish_reason": choice["finish_reason"], "provider_request_id": value.get("id"),
                    "response_metadata": call["response_metadata"]})
                if limits.solver == "self_refine" and index + 1 < limits.request_budget:
                    messages.extend([{"role": "assistant", "content": content},
                                     {"role": "user", "content": REFINE_INSTRUCTION}])
            except (httpx.HTTPError, TimeoutError, ValueError, TypeError, KeyError) as error:
                # No vendor error body or credentials are persisted. Unknown cost stays reserved.
                call.update(state="unknown", error_type=type(error).__name__)
                terminal = "reconciliation_required"
                transport_unknown = isinstance(error, (httpx.ReadError, httpx.RemoteProtocolError,
                    httpx.TimeoutException, TimeoutError))
                if transport_unknown and limits.unknown_recovery == "once" and unknown_retries == 0 and index + 1 < limits.request_budget:
                    unknown_retries += 1
                    terminal = "completed"
                    continue
                break
            finally:
                occupied_output = output_occupancy(calls)
                write_json(ledger, {"calls": calls, "occupied_output_tokens": occupied_output})
    final = submission(final_body, declared=final_body is not None, source="last_baseline_response",
                       source_id=calls[-1]["number"] if calls else None)
    unknown = sum(c["state"] == "unknown" for c in calls)
    report = {"report_schema_version": "3.0", "case_id": case["id"], "problem_id": case["id"],
        "evaluation_mode": "answer", "solver": limits.solver, "state": terminal, "terminal_reason": terminal,
        "answer_submission": final, "final_answer": final["answer"], "final_body": final_body,
        "final_answer_present": final["answer"] is not None,
        "completed": terminal == "completed" and final["status"] == "submitted",
        "submission_present": final["status"] == "submitted", "researcher_outcome": None,
        "calls": calls, "usage_summary": usage_summary(calls), "requests": len(calls),
        "occupied_output_tokens": occupied_output,
        "budget": {"request_budget": limits.request_budget, "occupied": len(calls),
                   "spent": sum(c["state"] == "spent" for c in calls), "unknown": unknown},
        "human_interventions": 0, "interventions": [], "unattended_eligible": True,
        "financial_reconciliation_pending": unknown > 0, "unknown_retries_authorized": unknown_retries,
        "network_dispatches": transport.number, "model_web_tools": False, "answer_key_loaded": False,
        "elapsed_seconds": time.monotonic() - started, "finished_at": stamp()}
    write_json(directory / "report.json", report)
    return report


async def run_independent_samples_case(case, directory, config, limits, *, transport_factory=None):
    """Run independent closed-book samples and select by conservative local vote."""
    from mathagent.evaluation.answerbench import CompletionOnlyTransport, stamp, write_json

    directory.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    calls, candidates, unknown_retries = [], [], 0
    runtime_terminal = "completed"
    ledger = directory / "baseline-calls.json"
    occupied_output = 0
    transport = CompletionOnlyTransport(config.base_url + "/chat/completions", directory,
        inner=transport_factory(case) if transport_factory else None)
    async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False) as client:
        for index in range(limits.request_budget):
            remaining_seconds = limits.case_timeout_seconds - (time.monotonic() - started)
            if remaining_seconds <= 0:
                runtime_terminal = "case_timeout"
                break
            maximum = min(limits.max_output_tokens,
                          (limits.cumulative_output_token_budget - occupied_output)
                          if limits.cumulative_output_token_budget is not None else limits.max_output_tokens)
            if maximum < 1:
                runtime_terminal = "output_token_budget_exhausted"
                break
            call = {"number": index + 1, "state": "dispatched", "usage": {},
                    "reserved_output_tokens": maximum, "created_at": stamp()}
            calls.append(call)
            occupied_output += maximum
            write_json(ledger, {"calls": calls, "candidates": candidates,
                                "occupied_output_tokens": occupied_output})
            payload = {"model": config.model,
                       "messages": [{"role": "system", "content": DIRECT_INSTRUCTION},
                                    {"role": "user", "content": case["problem"]}],
                       "stream": False, "max_tokens": maximum,
                       "thinking": {"type": "enabled"}, "reasoning_effort": limits.reasoning_effort}
            try:
                async with asyncio.timeout(min(remaining_seconds, limits.request_timeout_seconds)):
                    response = await client.post(config.base_url + "/chat/completions", json=payload,
                        headers={"Authorization": "Bearer " + config.api_key}, timeout=limits.request_timeout_seconds)
                    response.raise_for_status()
                    value = response.json()
                if not isinstance(value, dict) or not isinstance(value.get("usage") or {}, dict):
                    raise ValueError("Invalid completion object")
                usage = value.get("usage") or {}
                observation = RequestObservation(config)
                observation.metadata(value)
                call.update(state="spent", usage=usage, provider_request_id=value.get("id"),
                            response_metadata=observation.snapshot()["response_metadata"])
                used = usage.get("completion_tokens")
                if type(used) is int and 0 <= used <= maximum:
                    occupied_output -= maximum - used
                choices = value.get("choices")
                if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                    raise ValueError("Invalid completion choice")
                choice = choices[0]
                if not isinstance(choice, dict):
                    raise ValueError("Invalid completion choice")
                call["finish_reason"] = choice.get("finish_reason")
                message = choice.get("message")
                if not isinstance(message, dict):
                    raise ValueError("Invalid completion message")
                content = message.get("content")
                if choice.get("finish_reason") != "stop" or not isinstance(content, str) or not content.strip():
                    runtime_terminal = "incomplete_output"
                    continue
                write_json(directory / f"response-{index + 1}.json", {"body": content, "usage": usage,
                    "finish_reason": choice["finish_reason"], "provider_request_id": value.get("id"),
                    "response_metadata": call["response_metadata"]})
                answers = boxed_answers(content)
                if len(answers) == 1:
                    candidates.append({"number": index + 1, "body": content,
                                       "answer": answers[0], "normalized": re.sub(r"\s+", "", answers[0])})
            except (httpx.HTTPError, TimeoutError, ValueError, TypeError, KeyError) as error:
                if call["state"] != "spent":
                    call.update(state="unknown", error_type=type(error).__name__)
                else:
                    call["error_type"] = type(error).__name__
                    runtime_terminal = "malformed_output"
                transport_unknown = isinstance(error, (httpx.ReadError, httpx.RemoteProtocolError,
                    httpx.TimeoutException, TimeoutError))
                if call["state"] == "unknown" and transport_unknown and limits.unknown_recovery == "once" and unknown_retries == 0 and index + 1 < limits.request_budget:
                    unknown_retries += 1
                    continue
                if call["state"] == "unknown":
                    runtime_terminal = "reconciliation_required"
                    break
                continue
            finally:
                occupied_output = output_occupancy(calls)
                write_json(ledger, {"calls": calls, "candidates": candidates,
                                    "occupied_output_tokens": occupied_output})
    counts = {}
    for candidate in candidates:
        counts[candidate["normalized"]] = counts.get(candidate["normalized"], 0) + 1
    selected = None
    if counts:
        best = max(counts.values())
        selected = next(candidate for candidate in candidates if counts[candidate["normalized"]] == best)
    final_body = selected["body"] if selected else None
    final = submission(final_body, declared=selected is not None, source="independent_samples_vote",
                       source_id=selected["number"] if selected else None)
    unknown = sum(c["state"] == "unknown" for c in calls)
    terminal = runtime_terminal
    if unknown and terminal == "completed":
        terminal = "reconciliation_required"
    report = {"report_schema_version": "3.0", "case_id": case["id"], "problem_id": case["id"],
        "evaluation_mode": "answer", "solver": limits.solver, "state": terminal,
        "terminal_reason": terminal, "selection_rule": INDEPENDENT_SELECTION_RULE,
        "answer_submission": final, "final_answer": final["answer"], "final_body": final_body,
        "final_answer_present": final["answer"] is not None, "answer_independently_submitted": selected is not None,
        "completed": terminal == "completed" and selected is not None,
        "submission_present": final["status"] == "submitted", "researcher_outcome": None,
        "candidates": candidates, "vote_counts": counts, "calls": calls,
        "usage_summary": usage_summary(calls), "requests": len(calls),
        "occupied_output_tokens": occupied_output,
        "budget": {"request_budget": limits.request_budget, "occupied": len(calls),
                   "spent": sum(c["state"] == "spent" for c in calls), "unknown": unknown},
        "human_interventions": 0, "interventions": [], "unattended_eligible": True,
        "financial_reconciliation_pending": unknown > 0, "unknown_retries_authorized": unknown_retries,
        "network_dispatches": transport.number, "model_web_tools": False, "answer_key_loaded": False,
        "elapsed_seconds": time.monotonic() - started, "finished_at": stamp()}
    write_json(directory / "report.json", report)
    return report
