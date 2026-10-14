"""Role signatures: Ed25519 keys per role+project, the signed letter envelope.

The key binds to the (project, role) pair: key_id = sha256(project_root + role).
A CLI restart never changes the key — no expiry by construction. Key loss means
regenesis: the old key leaves for the revoke list, verification fail-opens with
a transcript record (signed-roles-push-channel card, p.1).

One dependency — PyNaCl (already installed, zero new). No back-references to
PairSession: server imports from here, not the reverse.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey


def key_id_for(project_root: Path | str, role: str) -> str:
    """Stable key ID: project + role + install at the key place."""
    root = str(Path(project_root).resolve())
    return hashlib.sha256(f"{root}\x00{role}".encode("utf-8")).hexdigest()[:16]


def keys_dir(state_dir: Path | str) -> Path:
    return Path(state_dir) / "keys"


def _priv_path(state_dir: Path | str, role: str) -> Path:
    return keys_dir(state_dir) / f"{role}.priv"


def _pub_path(state_dir: Path | str, role: str) -> Path:
    return keys_dir(state_dir) / f"{role}.pub"


def generate(state_dir: Path | str, project_root: Path | str, role: str) -> dict[str, str]:
    """Generate the role pair. The private key is 0600, else refuse."""
    key = SigningKey.generate()
    raw = bytes(key)
    d = keys_dir(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    priv = _priv_path(state_dir, role)
    priv.write_bytes(raw)
    os.chmod(priv, 0o600)
    pub = _pub_path(state_dir, role)
    pub.write_bytes(bytes(key.verify_key))
    key_id = key_id_for(project_root, role)
    pub_b64 = base64.b64encode(bytes(key.verify_key)).decode("ascii")
    return {"key_id": key_id, "public": pub_b64}


def load_signing_key(state_dir: Path | str, role: str) -> SigningKey | None:
    try:
        return SigningKey(_priv_path(state_dir, role).read_bytes())
    except OSError:
        return None


def load_verify_key(state_dir: Path | str, role: str) -> VerifyKey | None:
    try:
        return VerifyKey(_pub_path(state_dir, role).read_bytes())
    except OSError:
        return None


def sign_envelope(signing_key: SigningKey, key_id: str,
                  seq: int, frm: str, to: str, ts: str, text: str) -> dict[str, str]:
    """Sign the letter envelope. The signature covers canonical JSON minus sig."""
    body: dict[str, Any] = {
        "seq": seq, "from": frm, "to": to, "ts": ts,
        "body_sha": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "key_id": key_id,
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = signing_key.sign(canonical).signature
    body["sig"] = base64.b64encode(sig).decode("ascii")
    return body


def verify_envelope(verify_key: VerifyKey, envelope: dict[str, Any], text: str) -> bool:
    """Verify the envelope signature and the text body_sha match."""
    try:
        body = {k: envelope[k] for k in ("seq", "from", "to", "ts", "body_sha", "key_id")}
    except KeyError:
        return False
    if body["body_sha"] != hashlib.sha256(text.encode("utf-8")).hexdigest():
        return False
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    try:
        sig = base64.b64decode(envelope.get("sig") or "")
    except (ValueError, TypeError):
        return False
    try:
        verify_key.verify(canonical, sig)
    except BadSignatureError:
        return False
    return True
