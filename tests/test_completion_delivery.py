"""Only exact review text actually present in prompts counts as delivered."""

from mathagent.runtime.completion import boxed_answers, delivered_review_ranges


def test_single_box_handles_nested_braces_and_rejects_malformed_or_multiple_boxes():
    assert boxed_answers(r"$\boxed{\frac{1}{2}}$") == [r"\frac{1}{2}"]
    assert len(boxed_answers(r"$\boxed{1}$ and $\boxed{2}$")) == 2
    assert boxed_answers(r"$\boxed{1}$ and \boxed{") == []
    assert boxed_answers(r"\\boxed{1}") == []
    assert boxed_answers(r"\boxed{" * 20000) == []


def test_reference_or_excerpt_does_not_count_as_delivered_review():
    text = "Important review prefix. " * 1000 + "Critical concluding limitation."
    ranges, complete = delivered_review_ranges({"context_revision_ids": ["r"],
        "child_results": [{"output_revision_id": "r", "body": text[:1000], "excerpted": True}],
        "schema": {"revision_id": {"type": "string"}}}, {"r": text})
    assert ranges == {"r": []} and complete == []


def test_exact_continuation_pages_require_complete_coverage_without_gaps():
    text = "0123456789"
    first, complete = delivered_review_ranges({"items": [
        {"revision_id": "r", "body": text[:4], "section": "body", "offset": 0},
        {"revision_id": "r", "body": text[6:], "section": "body", "offset": 6},
    ]}, {"r": text})
    assert complete == []
    final, complete = delivered_review_ranges({"revision_id": "r", "body": text[4:6],
        "section": "body", "offset": 4}, {"r": text}, first)
    assert final == {"r": [[0, 10]]} and complete == ["r"]
    _, forged = delivered_review_ranges({"revision_id": "r", "body": "other text",
        "section": "body", "offset": 0}, {"r": text})
    assert forged == []
