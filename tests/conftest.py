import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture():
    def load(name: str):
        with open(FIXTURES / name, encoding="utf-8") as f:
            return json.load(f)

    return load


@pytest.fixture(autouse=True)
def no_passphrase(monkeypatch):
    # A developer's own HUNTER_PASSPHRASE must never leak into a test build.
    monkeypatch.delenv("HUNTER_PASSPHRASE", raising=False)
