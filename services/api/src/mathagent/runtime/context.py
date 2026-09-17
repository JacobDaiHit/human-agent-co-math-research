"""Bounded, source-addressable context; excerpts never stand in for full proofs."""

import copy
import hashlib
import json

PROMPT_LIMIT = 190_000
WARNING = (
    "这是带来源的局部上下文。excerpted/omitted 表示内容不完整，片段可能跨越公式。"
    "必要前提或证明必须用 read_object 按 read_ref 的版本、section 和 offset 续读；"
    "不得把未读完的论证视为已经验证。record 可回读正文、元数据、证明计划和操作回执。"
    "续页必须核对 body_sha256 一致；若元数据期间发生变化，应从 offset=0 重新读取。"
    "历史回执只描述当时的操作；current=false 或 stale=true 的内容不是当前版本。"
)


def serialized(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def excerpt(body, limit, offset=0):
    if limit <= 0 or offset < 0 or offset > len(body):
        raise ValueError("Page offset must be within the source and page size positive")
    end = min(len(body), offset + limit)
    if end < len(body):
        boundary = body.rfind("\n\n", offset + limit // 2, end)
        if boundary >= 0:
            end = boundary + 2
    return {
        "body": body[offset:end],
        "body_sha256": digest(body),
        "total_chars": len(body),
        "offset": offset,
        "next_offset": end if end < len(body) else None,
        "excerpted": offset > 0 or end < len(body),
    }


def read_ref(item, section="record"):
    return {
        **{key: item[key] for key in ("object_id", "revision_id", "branch_id") if item.get(key)},
        "section": section,
        "offset": 0,
    }


def omitted(value, ref):
    source = serialized(value)
    return {"omitted": True, "total_chars": len(source), "sha256": digest(source), "read_ref": ref}


def _json_page(value, limit, offset):
    """Hash the entire JSON while retaining only the requested page in memory."""
    if limit <= 0 or offset < 0:
        raise ValueError("Page offset must be nonnegative and page size positive")
    total, parts, source_hash = 0, [], hashlib.sha256()
    for chunk in json.JSONEncoder(ensure_ascii=False, sort_keys=True).iterencode(value):
        source_hash.update(chunk.encode())
        end = total + len(chunk)
        if end > offset and total < offset + limit:
            parts.append(chunk[max(0, offset - total) : min(len(chunk), offset + limit - total)])
        total = end
    if offset > total:
        raise ValueError("Page offset must be within the source")
    end = min(total, offset + limit)
    return {
        "body": "".join(parts),
        "body_sha256": source_hash.hexdigest(),
        "total_chars": total,
        "offset": offset,
        "next_offset": end if end < total else None,
        "excerpted": offset > 0 or end < total,
    }


def read_page(item, values):
    section = values.get("section", "body")
    if section not in {"body", "payload", "record"}:
        raise ValueError("Unknown record section")
    keys = (
        "object_id",
        "revision_id",
        "branch_id",
        "kind",
        "current",
        "stale",
        "current_revision_id",
        "adoption_state",
    )
    page = {key: item[key] for key in keys if key in item}
    limit, offset = values.get("max_chars", 12000), values.get("offset", 0)
    page.update(
        excerpt(item["body"], limit, offset)
        if section == "body"
        else _json_page(item["payload"] if section == "payload" else item, limit, offset)
    )
    page.update(section=section, read_ref=read_ref(item, section))
    if section == "body":
        for key in ("payload", "proof_plan", "evidence", "support", "operation_receipts"):
            if key in item:
                value = item[key]
                page[key] = (
                    value
                    if len(serialized(value)) <= 2400
                    else omitted(value, read_ref(item, "payload" if key == "payload" else "record"))
                )
    return page


def _short_page(item, limit):
    """Shorten a previously made page without losing its original cursor/hash."""
    if "body_sha256" not in item:
        return read_page(item, {"max_chars": limit})
    page = copy.deepcopy(item)
    body = page.get("body", "")
    if len(body) > limit:
        page["body"] = body[:limit]
        page["next_offset"] = page.get("offset", 0) + limit
        page["excerpted"] = True
    return page


def _condense(value, budget, ref):
    """Keep small records intact; retain IDs and explicit source pointers otherwise."""
    if len(serialized(value)) <= budget:
        return value
    if isinstance(value, dict):
        source_ref = value.get("read_ref") or (read_ref(value) if value.get("revision_id") else ref)
        keep = {
            k: value[k]
            for k in (
                "object_id",
                "revision_id",
                "branch_id",
                "output_revision_id",
                "number",
                "run_id",
                "type",
                "status",
                "error",
                "state",
                "current",
                "stale",
                "current_revision_id",
            )
            if k in value
        }
        keep.update(omitted(value, source_ref))
        for key in ("result", "items", "actions"):
            if key in value:
                keep[key] = _condense(value[key], max(300, budget // 2), source_ref)
        if "body" in value and isinstance(value["body"], str):
            if "body_sha256" in value:
                page = _short_page(value, min(1000, max(100, budget // 4)))
                keep.update(
                    {
                        k: page[k]
                        for k in (
                            "body",
                            "body_sha256",
                            "offset",
                            "next_offset",
                            "total_chars",
                            "excerpted",
                        )
                    }
                )
            else:
                keep.update(excerpt(value["body"], min(1000, max(100, budget // 4))))
        if len(serialized(keep)) > max(1000, budget):
            keep = {
                k: v for k, v in keep.items() if k not in {"result", "items", "actions", "body"}
            }
        return keep
    if isinstance(value, list):
        slots = min(len(value), 12)
        kept = [_condense(v, max(400, budget // max(1, slots)), ref) for v in value[-slots:]]
        if slots < len(value):
            kept.insert(0, omitted(value[:-slots], ref))
        return kept
    return omitted(value, ref)


def _run_context_ref(goal_ref, section):
    return {**goal_ref, "section": "record", "offset": 0}


def _memory_ref(goal_ref, limit=200):
    return {"type": "request_memory", "arguments": {"offset": 0, "limit": limit}}


def _compact_search_context(value, budget, goal_ref):
    """Keep controller identity while making route details rereadable."""
    if not isinstance(value, dict):
        return omitted(value, _run_context_ref(goal_ref, "run_context.search_context"))
    ref = _run_context_ref(goal_ref, "run_context.search_context")
    core = ("work_id", "kind", "route_id", "controller", "budget",
            "original_goal_revision_id", "selected_route_id")
    result = {key: value[key] for key in core if key in value}
    for key, item in value.items():
        if key in core:
            continue
        result[key] = item if len(serialized(item)) <= max(400, budget // 3) else omitted(item, ref)
    if len(serialized(result)) > budget:
        # Preserve the controller identity even when route progress/gaps are huge.
        result = {key: result[key] for key in core if key in result}
        result["omitted"] = True
        result["read_ref"] = ref
    return result


def _compact_memory_packet(value, budget, goal_ref):
    if not isinstance(value, dict):
        return omitted(value, _memory_ref(goal_ref))
    result = dict(value)
    entries = result.get("entries")
    if entries is not None and len(serialized(entries)) > max(600, budget // 2):
        result["entries"] = omitted(entries, _memory_ref(goal_ref))
    if len(serialized(result)) > budget:
        result = {key: result[key] for key in (
            "metadata", "entries", "offset", "limit", "total", "next_offset", "selection"
        ) if key in result}
        result["read_refs"] = omitted(value.get("read_refs", []), _memory_ref(goal_ref))
        if "entries" in result and len(serialized(result["entries"])) > max(400, budget // 2):
            result["entries"] = omitted(result["entries"], _memory_ref(goal_ref))
    return result


def compact_task(task):
    from mathagent.providers.protocol import messages_for

    task = copy.deepcopy(task)
    old_summary = task.get("context_summary", {})
    all_inputs = task["inputs"]
    goal = next((i for i in all_inputs if i["object_id"] == task["goal_object_id"]), {})
    goal_ref = read_ref(goal)
    goal_ref["context_attempt_id"] = task.get("attempt_id")
    ordered = sorted(
        all_inputs,
        key=lambda i: (
            i["object_id"] != task["goal_object_id"],
            {"problem": 0, "argument": 1, "context": 2}.get(i["kind"], 3),
        ),
    )
    inputs, missing, used = [], list(old_summary.get("omitted_revisions", [])), 0
    reviewing = task.get("mode") == "review"
    for item in ordered:
        page = _short_page(item, max(1, len(item["body"])) if reviewing else 12000)
        if reviewing:
            for field in ("payload", "proof_plan"):
                if field in item:
                    page[field] = copy.deepcopy(item[field])
        if (inputs and used + len(serialized(page)) > (150000 if reviewing else 85000)) or len(inputs) >= 48:
            missing.append(read_ref(item))
            continue
        used += len(serialized(page))
        inputs.append(page)
    task["inputs"] = inputs
    summary = {
        "input_count": old_summary.get("input_count", len(all_inputs)),
        "included_count": len(inputs),
        "omitted_count": old_summary.get("omitted_count", 0)
        + len(missing)
        - len(old_summary.get("omitted_revisions", [])),
        "omitted_revisions": missing[:40],
        "warning": WARNING,
        "run_context_read_ref": goal_ref,
        "condensed_sections": dict(old_summary.get("condensed_sections", {})),
    }
    task["context_summary"] = summary
    for field in (
        "previous_steps",
        "child_results",
        "operation_results",
        "discussions",
        "proof_plans",
    ):
        original = task.get(field, [])
        value = _condense(original, 10000, goal_ref)
        task[field] = value
        if value != original:
            summary["condensed_sections"][field] = omitted(original, goal_ref)
    if task.get("search_context") is not None:
        original = task["search_context"]
        task["search_context"] = _compact_search_context(original, 10000, goal_ref)
        if task["search_context"] != original:
            summary["condensed_sections"]["search_context"] = omitted(
                original, _run_context_ref(goal_ref, "run_context.search_context"))
    if task.get("memory_packet") is not None:
        original = task["memory_packet"]
        task["memory_packet"] = _compact_memory_packet(original, 10000, goal_ref)
        if task["memory_packet"] != original:
            summary["condensed_sections"]["memory_packet"] = omitted(
                original, _memory_ref(goal_ref))
    instruction = task.get("instruction", "")
    if len(instruction) > 8000:
        summary["condensed_sections"]["instruction"] = {
            "sha256": digest(instruction),
            "total_chars": len(instruction),
            "read_ref": goal_ref,
            "record_field": "run_context.instruction",
            "omitted": True,
        }
        task["instruction"] = (
            instruction[:8000] + "\n[指令未读完；必须回读 run_context.instruction。]"
        )
    # Count the actual provider strings, including schemas and JSON escaping.
    while sum(len(m["content"]) for m in messages_for(task)) >= PROMPT_LIMIT:
        candidates = [
            (len(serialized(task.get(k))), k)
            for k in (
                "inputs",
                "previous_steps",
                "child_results",
                "operation_results",
                "discussions",
                "proof_plans",
                "repair_output",
                "instruction",
                "search_context",
                "memory_packet",
            )
            if task.get(k)
        ]
        if not candidates:
            raise ValueError("Fixed prompt metadata exceeds context limit")
        _, key = max(candidates)
        value = task[key]
        summary["condensed_sections"].setdefault(key, omitted(value, goal_ref))
        if key == "inputs":
            if len(value) > 1:
                removed = value.pop()
                summary["omitted_count"] += 1
                if len(summary["omitted_revisions"]) < 40:
                    summary["omitted_revisions"].append(read_ref(removed))
            else:
                value[0] = _short_page(value[0], max(100, len(value[0]["body"]) // 2))
                for field in ("payload", "proof_plan", "evidence", "support", "operation_receipts"):
                    if field in value[0]:
                        value[0][field] = omitted(value[0][field], read_ref(value[0]))
        elif isinstance(value, str):
            task[key] = value[: len(value) // 2]
        elif isinstance(value, list):
            task[key] = value[len(value) // 2 :] if len(value) > 1 else []
        elif key == "search_context":
            task[key] = _compact_search_context(value, max(1200, len(serialized(value)) // 2), goal_ref)
        elif key == "memory_packet":
            task[key] = _compact_memory_packet(value, max(1200, len(serialized(value)) // 2), goal_ref)
        else:
            task[key] = []
    summary["included_count"] = len(task["inputs"])
    return task
