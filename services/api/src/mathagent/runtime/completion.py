"""Mechanical delivery checks; these do not establish mathematical correctness."""

import re


def review_material_hash(item):
    """Fingerprint mathematical input, excluding prior judgments and UI metadata."""
    import hashlib
    import json

    material = {key: item[key] for key in ("body", "payload", "proof_plan") if key in item}
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def review_input_receipt(task):
    return {item["revision_id"]: review_material_hash(item) for item in task["inputs"]}


def boxed_answers(body):
    body = body or ""
    answers, offset = [], 0
    pattern = re.compile(r"(?<!\\)\\boxed\s*\{")
    while match := pattern.search(body, offset):
        depth = 1
        for index in range(match.end(), len(body)):
            if body[index] in "{}":
                before, slashes = index - 1, 0
                while before >= 0 and body[before] == "\\":
                    slashes += 1
                    before -= 1
                if slashes % 2:
                    continue
                depth += 1 if body[index] == "{" else -1
            if depth == 0:
                value = body[match.end():index].strip()
                if value and not pattern.search(value):
                    answers.append(value)
                else:
                    return []
                offset = index + 1
                break
        else:
            return []
    return answers


def delivered_review_ranges(task, bodies, previous=None):
    """Record exact body ranges present in the actual, compacted provider input."""
    import json

    ranges = {rid: list((previous or {}).get(rid, [])) for rid in bodies}
    pending = [task]
    while pending:
        item = pending.pop()
        if isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, dict):
            pending.extend(v for v in item.values() if isinstance(v, (dict, list)))
            rid = item.get("revision_id") or item.get("output_revision_id")
            body = item.get("body")
            if not isinstance(rid, str) or rid not in bodies or not isinstance(body, str):
                continue
            expected = bodies[rid]
            if body == expected:
                ranges[rid].append([0, len(expected)])
            elif item.get("section") == "body":
                offset = item.get("offset", 0)
                if isinstance(offset, int) and offset >= 0 and body and expected[offset:offset + len(body)] == body:
                    ranges[rid].append([offset, offset + len(body)])
            elif item.get("section") == "record" and not item.get("excerpted"):
                try:
                    record = json.loads(body)
                except (ValueError, TypeError):
                    continue
                if isinstance(record, dict) and record.get("body") == expected:
                    ranges[rid].append([0, len(expected)])
    merged, complete = {}, []
    for rid, intervals in ranges.items():
        merged[rid] = []
        for start, end in sorted(intervals):
            if merged[rid] and start <= merged[rid][-1][1]:
                merged[rid][-1][1] = max(merged[rid][-1][1], end)
            else:
                merged[rid].append([start, end])
        if merged[rid] == [[0, len(bodies[rid])]]:
            complete.append(rid)
    return merged, complete
