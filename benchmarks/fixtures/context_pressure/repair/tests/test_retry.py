import hashlib
import json
from src.retry import retry_delay_seconds
from src.limits import RETRY_DELAY_MS

def test_contract_and_units():
    actual = json.dumps([RETRY_DELAY_MS, retry_delay_seconds()], separators=(",", ":"))
    assert hashlib.sha256(actual.encode()).hexdigest() == "6cf6d00261cd45794fb822471771e9ffbf35b980d5043f405609e8198436d773"
