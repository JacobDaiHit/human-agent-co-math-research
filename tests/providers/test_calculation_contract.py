"""The model sees a complete calculator schema without expression execution."""

import json

import pytest
from mathagent.providers.actions import (
    Calculate,
    calculation_validation_feedback,
    operation_schemas,
)
from pydantic import ValidationError


def test_calculation_schema_selects_exact_required_inputs_per_tool():
    schema = operation_schemas()["calculate"]
    assert schema["discriminator"]["propertyName"] == "tool"
    assert set(schema["discriminator"]["mapping"]) == {
        "rational_arithmetic",
        "polynomial_identity",
        "hermitian_crossing",
    }
    definitions = schema["$defs"]
    assert definitions["RationalInputs"]["required"] == ["expression"]
    assert definitions["PolynomialInputs"]["required"] == ["left", "right", "variables"]
    assert definitions["HermitianInputs"]["properties"] == {}
    for name in ("RationalInputs", "PolynomialInputs", "HermitianInputs"):
        assert definitions[name]["additionalProperties"] is False
    for name in ("RationalCalculation", "PolynomialCalculation", "HermitianCalculation"):
        assert definitions[name]["required"] == ["tool", "inputs"]
    description = definitions["PolynomialInputs"]["properties"]["left"]["description"]
    assert "explicit *" in description and "**" in description and "never ^" in description
    assert "integer literals" in description and "no functions" in description
    assert "lhs/rhs are not aliases" in description


@pytest.mark.parametrize(
    "values",
    [
        {"tool": "rational_arithmetic", "inputs": {"expression": "1/3+2/5"}},
        {"tool": "rational_arithmetic", "inputs": {"expression": "(3/2)**-2"}},
        {
            "tool": "polynomial_identity",
            "inputs": {"left": "(x+y)**2", "right": "x**2+2*x*y+y**2", "variables": ["x", "y"]},
        },
        {"tool": "hermitian_crossing", "inputs": {}},
    ],
)
def test_calculation_discriminated_model_preserves_existing_wire_shape(values):
    assert Calculate.model_validate(values).model_dump(exclude_none=True) == values


@pytest.mark.parametrize(
    "tool,inputs",
    [
        ("rational_arithmetic", {}),
        ("rational_arithmetic", {"expression": "x+1"}),
        ("rational_arithmetic", {"expression": "2^3"}),
        ("rational_arithmetic", {"expression": "0.5"}),
        ("rational_arithmetic", {"expression": "2(3)"}),
        ("rational_arithmetic", {"expression": " "}),
        ("polynomial_identity", {"lhs": "x^2", "rhs": "x*x"}),
        ("polynomial_identity", {"left": "x**2", "right": "x*x"}),
        ("polynomial_identity", {"left": "x^2", "right": "x*x", "variables": ["x"]}),
        ("polynomial_identity", {"left": "sin(x)", "right": "x", "variables": ["x"]}),
        ("polynomial_identity", {"left": "2(x)", "right": "x", "variables": ["x"]}),
        ("polynomial_identity", {"left": "__import__('os')", "right": "x", "variables": ["x"]}),
        ("polynomial_identity", {"left": "x", "right": "x", "variables": ["x", "x"]}),
        ("polynomial_identity", {"left": "x", "right": "x", "variables": ["if"]}),
        ("hermitian_crossing", {"matrix": [[1, 0], [0, 1]]}),
    ],
)
def test_calculation_fields_and_syntax_are_not_silently_aliased(tool, inputs):
    with pytest.raises(ValidationError):
        Calculate.model_validate({"tool": tool, "inputs": inputs})


def test_parameter_feedback_reports_missing_paths_without_values_or_arbitrary_keys():
    values = {
        "tool": "polynomial_identity",
        "inputs": {
            "lhs": "SYNTHETIC_PRIVATE_EXPRESSION",
            "rhs": "PRIVATE_RHS",
            "SYNTHETIC_KEY_IN_FIELD_NAME": "SYNTHETIC_SECRET_VALUE",
        },
    }
    with pytest.raises(ValidationError) as raised:
        Calculate.model_validate(values)
    feedback = calculation_validation_feedback(raised.value, values["tool"])
    assert feedback["expected_inputs"] == ["inputs.left", "inputs.right", "inputs.variables"]
    missing = {
        tuple(item["path"]) for item in feedback["validation_errors"] if item["code"] == "missing"
    }
    assert missing == {("inputs", "left"), ("inputs", "right"), ("inputs", "variables")}
    assert all(set(item) == {"path", "code"} for item in feedback["validation_errors"])
    encoded = json.dumps(feedback)
    assert "PRIVATE" not in encoded and "SYNTHETIC" not in encoded
    assert "<extra_field>" in encoded


def test_parameter_feedback_handles_invalid_tool_types_without_echoing_them():
    values = {"tool": {"synthetic-secret": "value"}, "inputs": {}}
    with pytest.raises(ValidationError) as raised:
        Calculate.model_validate(values)
    feedback = calculation_validation_feedback(raised.value, values["tool"])
    assert feedback["expected_inputs"] == ["tool", "inputs"]
    assert "synthetic-secret" not in json.dumps(feedback)
