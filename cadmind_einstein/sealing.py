"""The agent key's secret, sealed to the miner that asked for it (DESIGN.md section 2).

The miner sends an ephemeral X25519 public key with its signed ``agent_key`` request. The validator
agrees a key with a fresh ephemeral key of its own, derives an AES-256 key with HKDF-SHA256 (salt:
both public keys; info: the protocol label and the hotkey), and encrypts the secret with AES-GCM,
the hotkey as associated data. Only the holder of the miner's private key opens it; the secret is
never in clear on the wire or in a log.

pycryptodome (also pulled in by bittensor's dependencies; declared in pyproject).
"""

from __future__ import annotations

import base64
import secrets
from collections.abc import Mapping

from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Protocol import DH
from Crypto.Protocol.KDF import HKDF
from Crypto.PublicKey import ECC

INFO = b"cadmind-agent-key/1"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _key(shared_from: object, salt: bytes, hotkey: str) -> bytes:
    def kdf(x: bytes) -> bytes:
        return bytes(HKDF(x, 32, salt, SHA256, context=INFO + hotkey.encode()))  # type: ignore[arg-type]

    return bytes(DH.key_agreement(**shared_from, kdf=kdf))  # type: ignore[arg-type]


def new_keypair() -> tuple[bytes, bytes]:
    """(private, public), 32 raw bytes each: the miner's ephemeral pair for one request."""
    k = ECC.generate(curve="curve25519")
    return bytes(k.seed), bytes(k.public_key().export_key(format="raw"))  # type: ignore[attr-defined]


def seal(secret: str, miner_public: bytes, hotkey: str) -> dict[str, str]:
    if len(miner_public) != 32:
        raise ValueError("the miner's X25519 public key is 32 bytes")
    peer = DH.import_x25519_public_key(miner_public)
    mine = ECC.generate(curve="curve25519")
    eph = bytes(mine.public_key().export_key(format="raw"))
    key = _key({"static_priv": mine, "static_pub": peer}, eph + miner_public, hotkey)
    nonce = secrets.token_bytes(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    cipher.update(hotkey.encode())
    ct, tag = cipher.encrypt_and_digest(secret.encode())
    return {
        "scheme": "x25519-hkdf-sha256-aes256gcm/1",
        "ephemeral": _b64(eph),
        "nonce": _b64(nonce),
        "ciphertext": _b64(ct + tag),
    }


def open_sealed(sealed: Mapping[str, str], miner_private: bytes, hotkey: str) -> str:
    mine = DH.import_x25519_private_key(miner_private)
    eph = base64.b64decode(sealed["ephemeral"])
    peer = DH.import_x25519_public_key(eph)
    pub = bytes(mine.public_key().export_key(format="raw"))
    key = _key({"static_priv": mine, "static_pub": peer}, eph + pub, hotkey)
    blob = base64.b64decode(sealed["ciphertext"])
    cipher = AES.new(key, AES.MODE_GCM, nonce=base64.b64decode(sealed["nonce"]))
    cipher.update(hotkey.encode())
    return cipher.decrypt_and_verify(blob[:-16], blob[-16:]).decode()
