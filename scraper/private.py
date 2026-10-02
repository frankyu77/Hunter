"""Private layer: personal data published encrypted next to the public dashboard.

The repo and its Pages site are public, so anything personal (votes,
applications) can only ship as ciphertext. The run encrypts it with a key
derived from the HUNTER_PASSPHRASE secret; the page asks for the same
passphrase once per browser and decrypts in place with WebCrypto. Nobody
else gets past the lock, and the dashboard stays a free static page.

Why these primitives:

- PBKDF2-HMAC-SHA256 is the only password KDF that every browser's
  WebCrypto implements natively (no Argon2 or scrypt), so it is the one both
  ends can share without shipping a crypto library to the page. The
  ciphertext is public and can be attacked offline, so the iteration count
  follows OWASP's current figure and short passphrases are refused outright.
- AES-256-GCM is authenticated: a wrong passphrase fails loudly instead of
  decrypting to garbage, and a tampered payload is rejected. WebCrypto
  expects the 16-byte tag appended to the ciphertext, which is also what
  ``AESGCM.encrypt`` returns, so the bytes cross over unchanged.
- The salt is a fixed, app-specific constant rather than random per build.
  It is published either way; what it buys is that the derived key stays
  the same from one hourly build to the next, so a browser can keep the
  derived (non-extractable) key and never ask again. A random salt would
  force re-typing the passphrase after every rebuild. A fresh random IV per
  build is what GCM needs, and it gets one.

Changing SALT or ITERATIONS locks every browser out once (they re-prompt);
it never loses data, because the plaintext lives in the state file.

The same key runs the other way for dashboard actions (scraper.inbox): the
page encrypts a vote, the run opens it. GCM doubles as authentication
there - only someone holding the passphrase can produce a message that
opens. Opening always uses this module's KDF settings, never ones named in
the message, or a forged message could ask for a billion iterations.

Fails closed: no passphrase, or one that is too short, means no private
layer at all - never a plaintext fallback.
"""

import base64
import functools
import hashlib
import json
import logging
import os
import unicodedata

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger(__name__)

PASSPHRASE_ENV = "HUNTER_PASSPHRASE"
MIN_PASSPHRASE = 16
ITERATIONS = 600_000
SALT = b"hunter/private-layer/v1"
FORMAT = 1


def passphrase() -> str | None:
    """The configured passphrase, or None if the private layer is off."""
    phrase = _normalize(os.environ.get(PASSPHRASE_ENV, ""))
    if not phrase:
        return None
    if len(phrase) < MIN_PASSPHRASE:
        log.warning("%s is under %d characters; publishing without the private layer.",
                    PASSPHRASE_ENV, MIN_PASSPHRASE)
        return None
    return phrase


def seal(data: dict, phrase: str) -> dict:
    """Encrypt ``data`` into the JSON envelope the page knows how to open."""
    iv = os.urandom(12)
    plaintext = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return {
        "v": FORMAT,
        "kdf": {"name": "PBKDF2", "hash": "SHA-256", "iterations": ITERATIONS,
                "salt": _b64(SALT)},
        "iv": _b64(iv),
        "ct": _b64(AESGCM(_key(phrase)).encrypt(iv, plaintext, None)),
    }


def unseal(envelope: dict, phrase: str) -> dict:
    """The inverse of seal, and how the run opens the page's messages.
    Raises ``cryptography.exceptions.InvalidTag`` on a wrong passphrase or a
    tampered message, and ValueError on KDF settings other than ours."""
    kdf = envelope.get("kdf")
    if kdf is not None and (kdf.get("iterations") != ITERATIONS
                            or base64.b64decode(kdf.get("salt", "")) != SALT):
        raise ValueError("sealed with different KDF settings")
    plaintext = AESGCM(_key(phrase)).decrypt(base64.b64decode(envelope["iv"]),
                                             base64.b64decode(envelope["ct"]), None)
    return json.loads(plaintext)


@functools.lru_cache(maxsize=4)  # deliberately slow; a run opens many messages
def _key(phrase: str) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", _normalize(phrase).encode("utf-8"), SALT,
                               ITERATIONS, dklen=32)


def _normalize(phrase: str) -> str:
    # Must match the page: trimmed (a pasted secret often carries a trailing
    # newline) and NFC, so an accented character typed on a phone and on a
    # laptop derives the same key.
    return unicodedata.normalize("NFC", phrase.strip())


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")
