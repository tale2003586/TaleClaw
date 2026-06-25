import json
from pathlib import Path


def test_user_schema_requires_email():
    schema = json.loads(Path("schemas/user.schema.json").read_text(encoding="utf-8"))
    assert "email" in schema["required"]
    assert schema["properties"]["email"]["type"] == "string"
