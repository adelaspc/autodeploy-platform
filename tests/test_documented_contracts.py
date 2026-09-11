"""Keep documented API examples aligned with request validation."""

import json
import re
from pathlib import Path

from control_plane.application.projects.validation import validate_deployment_request_payload


API_REFERENCE = Path(__file__).resolve().parents[1] / "docs" / "api-reference.md"


def json_examples():
    text = API_REFERENCE.read_text(encoding="utf-8")
    return [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)]


def test_api_reference_json_examples_are_valid():
    assert len(json_examples()) >= 2


def test_documented_manual_deployment_payload_passes_validation():
    payload = next(example for example in json_examples() if example.get("message") == "Manual verification record")

    assert validate_deployment_request_payload(payload) is None
