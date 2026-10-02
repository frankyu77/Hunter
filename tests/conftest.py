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
    # A developer's own HUNTER_PASSPHRASE, or Actions' GitHub variables, must
    # never leak into a test: they switch on the private layer and the inbox.
    for name in ("HUNTER_PASSPHRASE", "GITHUB_TOKEN", "GITHUB_REPOSITORY"):
        monkeypatch.delenv(name, raising=False)
