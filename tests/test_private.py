"""Private layer: personal data reaches the page only as ciphertext."""

import json
import re

import pytest
from cryptography.exceptions import InvalidTag

from scraper import dashboard, feedback, private
from tests.test_dashboard import NOW, inserted, make_job, store_with

PHRASE = "correct horse battery staple"


def sealed_from(page) -> dict | None:
    text = page.read_text(encoding="utf-8")
    raw = re.search(r'<script id="private" type="application/json">(.*?)</script>', text, re.S)
    return json.loads(raw.group(1))


def voted_store(tmp_path):
    job = make_job(1, title="Quant Developer")
    store = store_with(tmp_path, {job.id: NOW})
    feedback.remember_sent([job], store, NOW)
    store.feedback["votes"] = {
        "tok": {"id": job.id, "title": "SECRET-VOTED-TITLE", "company": "acme",
                "url": job.url, "vote": "up", "starred": True, "applied": True,
                "applied_at": "2026-09-29T10:00:00+00:00",
                "updated_at": "2026-09-29T10:00:00+00:00"},
    }
    return job, store


def test_seal_round_trips_and_a_wrong_passphrase_fails_loudly():
    envelope = private.seal({"mine": [{"t": "x"}]}, PHRASE)

    assert private.unseal(envelope, PHRASE) == {"mine": [{"t": "x"}]}
    with pytest.raises(InvalidTag):  # GCM: wrong key never decrypts to garbage
        private.unseal(envelope, PHRASE + "!")


def test_each_seal_uses_a_fresh_iv_but_the_same_salt():
    a, b = private.seal({}, PHRASE), private.seal({}, PHRASE)
    assert a["iv"] != b["iv"]
    assert a["kdf"] == b["kdf"]  # a stable key, so browsers can remember it


def test_passphrase_is_trimmed_and_normalized_like_the_page_does():
    envelope = private.seal({"ok": True}, "  café au lait, deux sucres\n")
    assert private.unseal(envelope, "café au lait, deux sucres") == {"ok": True}


def test_no_or_short_passphrase_means_no_private_layer(monkeypatch):
    assert private.passphrase() is None
    monkeypatch.setenv(private.PASSPHRASE_ENV, "too short")
    assert private.passphrase() is None
    monkeypatch.setenv(private.PASSPHRASE_ENV, PHRASE)
    assert private.passphrase() == PHRASE


def test_page_carries_votes_only_encrypted(tmp_path, monkeypatch):
    monkeypatch.setenv(private.PASSPHRASE_ENV, PHRASE)
    job, store = voted_store(tmp_path)

    page = dashboard.build(store, str(tmp_path / "site"), [job], {}, NOW)
    text = inserted(page)

    assert "Quant Developer" in text
    for secret in ("SECRET-VOTED-TITLE", '"vote"', '"star"', '"applied"'):
        assert secret not in text
    mine = private.unseal(sealed_from(page), PHRASE)["mine"]
    assert mine == [{"t": "SECRET-VOTED-TITLE", "c": "Acme", "l": "", "u": job.url, "k": "",
                     "s": "", "d": "2026-09-29", "vote": "up", "star": True,
                     "applied": "2026-09-29"}]


def test_without_a_passphrase_the_page_has_no_private_layer(tmp_path):
    job, store = voted_store(tmp_path)
    page = dashboard.build(store, str(tmp_path / "site"), [job], {}, NOW)
    assert sealed_from(page) is None
    assert "__PRIVATE__" not in page.read_text(encoding="utf-8")


def test_a_sealing_failure_publishes_the_public_page_without_it(tmp_path, monkeypatch):
    monkeypatch.setenv(private.PASSPHRASE_ENV, PHRASE)

    def boom(*args):
        raise RuntimeError("no entropy")

    monkeypatch.setattr(private, "seal", boom)
    job, store = voted_store(tmp_path)
    page = dashboard.build(store, str(tmp_path / "site"), [job], {}, NOW)
    assert sealed_from(page) is None
    assert "Quant Developer" in page.read_text(encoding="utf-8")
