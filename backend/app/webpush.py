"""Web Push: telling an installed mobile app about an incident while it is closed.

Implemented on the `cryptography` package that is already installed, rather than adding pywebpush
and its dependencies. Two standards are involved, and both are small:

* **VAPID (RFC 8292)** identifies this server to the browser's push service: a short JWT signed
  with an ECDSA P-256 key, sent with every message. The key pair is generated once and stored in
  the data directory (never in the repository); its public half is what the app subscribes with.
* **Message encryption (RFC 8291, aes128gcm)**: the push service relays the message but cannot read
  it. Each message is encrypted for one subscription with an ephemeral key pair, the subscription's
  public key and its auth secret.

Push is an extra, never a dependency: the app works fully without it, every failure here is logged
and swallowed, and a subscription the push service reports as gone (404/410) is deleted.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import time
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import appdata, config

log = logging.getLogger("aira.webpush")

KEY_FILE = config.DATA_DIR / "vapid_private.pem"
# The contact a push service may use about this sender. Not personal data: an operator can set
# their own with AIRA_VAPID_SUBJECT.
SUBJECT = os.getenv("AIRA_VAPID_SUBJECT", "mailto:aira-admin@localhost")


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---------------------------------------------------------------- the server's VAPID key

_key: ec.EllipticCurvePrivateKey | None = None


def private_key() -> ec.EllipticCurvePrivateKey:
    """This server's VAPID key, created on first use and kept in the data directory."""
    global _key
    if _key is None:
        if KEY_FILE.exists():
            _key = serialization.load_pem_private_key(KEY_FILE.read_bytes(), password=None)
        else:
            _key = ec.generate_private_key(ec.SECP256R1())
            KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
            KEY_FILE.write_bytes(_key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()))
            log.info("created a VAPID key for Web Push at %s", KEY_FILE)
    return _key


def _raw_public(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962,
                            serialization.PublicFormat.UncompressedPoint)


def public_key_b64() -> str:
    """The applicationServerKey the app subscribes with (65-byte uncompressed point, base64url)."""
    return b64u(_raw_public(private_key().public_key()))


def vapid_jwt(endpoint: str, ttl_s: int = 12 * 3600) -> str:
    """The signed token for one push service (its origin is the audience)."""
    parts = urlsplit(endpoint)
    header = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64u(json.dumps({"aud": f"{parts.scheme}://{parts.netloc}",
                              "exp": int(time.time()) + ttl_s, "sub": SUBJECT},
                             separators=(",", ":")).encode())
    signing_input = f"{header}.{claims}".encode()
    der = private_key().sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return f"{header}.{claims}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


# ---------------------------------------------------------------- RFC 8291 encryption

def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def encrypt(payload: bytes, p256dh_b64: str, auth_b64: str,
            salt: bytes | None = None, server_key: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """One aes128gcm-encoded message for one subscription (RFC 8291 + RFC 8188)."""
    ua_public = b64u_decode(p256dh_b64)
    auth_secret = b64u_decode(auth_b64)
    salt = salt or os.urandom(16)
    as_key = server_key or ec.generate_private_key(ec.SECP256R1())
    as_public = _raw_public(as_key.public_key())
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    ecdh = as_key.exchange(ec.ECDH(), ua_key)

    prk_key = hmac.new(auth_secret, ecdh, hashlib.sha256).digest()
    ikm = _hkdf_expand(prk_key, b"WebPush: info\x00" + ua_public + as_public, 32)
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    cek = _hkdf_expand(prk, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf_expand(prk, b"Content-Encoding: nonce\x00", 12)

    record_size = 4096
    if len(payload) + 17 > record_size:
        raise ValueError("push payload too large for one record")
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack("!IB", record_size, len(as_public)) + as_public + ciphertext


# ---------------------------------------------------------------- sending

async def send(sub: dict, message: dict, ttl_s: int = 600, urgency: str = "normal") -> int:
    """Deliver one message to one subscription. Returns the push service's HTTP status (0 = error)."""
    from . import http_util  # noqa: PLC0415 - the project's client: verified TLS, trace headers

    body = encrypt(json.dumps(message, separators=(",", ":")).encode(), sub["p256dh"], sub["auth"])
    headers = {
        "TTL": str(ttl_s), "Urgency": urgency, "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "Authorization": f"vapid t={vapid_jwt(sub['endpoint'])}, k={public_key_b64()}",
    }
    try:
        async with http_util.async_client(timeout=10) as client:
            r = await client.post(sub["endpoint"], content=body, headers=headers)
        status = r.status_code
    except Exception as e:  # noqa: BLE001 - push is never allowed to break anything else
        log.warning("push to %s failed: %s", urlsplit(sub["endpoint"]).netloc, e)
        appdata.mark_subscription(sub["endpoint"], ok=False)
        return 0
    if status in (404, 410):
        # The subscription no longer exists (app uninstalled, permission revoked).
        appdata.delete_subscription(sub["endpoint"])
        log.info("removed an expired push subscription (%s)", status)
    elif 200 <= status < 300:
        appdata.mark_subscription(sub["endpoint"], ok=True)
    else:
        appdata.mark_subscription(sub["endpoint"], ok=False)
        log.warning("push service %s answered %s", urlsplit(sub["endpoint"]).netloc, status)
    return status


async def broadcast(message: dict, urgency: str = "normal") -> dict:
    """Send one message to every subscribed device. Returns {endpoint host: status}."""
    out = {}
    for sub in appdata.subscriptions():
        out[urlsplit(sub["endpoint"]).netloc] = await send(sub, message, urgency=urgency)
    return out
