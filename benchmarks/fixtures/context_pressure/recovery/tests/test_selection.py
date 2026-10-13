import hashlib
import json
from pathlib import Path

def test_selected_code():
    value = json.loads(Path("src/selection.json").read_text())
    assert set(value) == {"key", "recovery_code"}
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(canonical.encode()).hexdigest() == "bff968fe2c06c0be733c4628bef610f9c7ea716f2398b028908394b89f2782d0"
