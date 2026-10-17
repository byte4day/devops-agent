from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

from devops_agent.core.config import Config


def test_documented_schema_covers_every_runtime_config_key() -> None:
    schema_path = Path(__file__).resolve().parents[1] / "schemas/devops.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert set(schema["properties"]) == {item.name for item in fields(Config)}
    assert schema["$id"].endswith("byte4day/devops-agent/main/schemas/devops.schema.json")
