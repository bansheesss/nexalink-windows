"""Key exchange, password authentication and AES-GCM session encryption.

Design: ephemeral X25519 (forward secrecy) + password-derived key (scrypt)
mixed into HKDF. Without the password, a man-in-the-middle cannot derive
the session keys or produce a valid proof.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class AuthError(Exception):
    pass


def generate_machine_id() -> str:
    """Random 9-digit ID (first digit non-zero)."""
    return str(secrets.randbelow(9) + 1) + "".join(
        str(secrets.randbelow(10)) for _ in range(8))


def generate_password(length: int = 10) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def new_ephemeral() -> X25519PrivateKey:
    return X25519PrivateKey.generate()


def public_bytes(key: X25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def derive_password_key(password: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(
        password.encode("utf-8"))


class SessionCipher:
    """AES-256-GCM with implicit counter nonces (never sent on the wire).

    TCP delivers in order, so a counter gives replay/reorder protection:
    any dropped, duplicated or reordered frame fails authentication.
    """

    def __init__(self, send_key: bytes, recv_key: bytes):
        self._send = AESGCM(send_key)
        self._recv = AESGCM(recv_key)
        self._send_ctr = 0
        self._recv_ctr = 0

    @staticmethod
    def _nonce(counter: int) -> bytes:
        return b"\x00\x00\x00\x00" + counter.to_bytes(8, "big")

    def encrypt(self, plaintext: bytes, aad: bytes = b"") -> bytes:
        ct = self._send.encrypt(self._nonce(self._send_ctr), plaintext, aad)
        self._send_ctr += 1
        return ct

    def decrypt(self, ciphertext: bytes, aad: bytes = b"") -> bytes:
        try:
            pt = self._recv.decrypt(self._nonce(self._recv_ctr), ciphertext, aad)
        except InvalidTag as e:
            raise AuthError("frame authentication failed") from e
        self._recv_ctr += 1
        return pt


@dataclass
class SessionKeys:
    cipher: SessionCipher
    auth_key: bytes

    def proof(self, label: bytes) -> bytes:
        return hmac.new(self.auth_key, label, hashlib.sha256).digest()


def derive_session(private: X25519PrivateKey, peer_pub: bytes, password: str,
                   salt: bytes, transcript: bytes, is_client: bool) -> SessionKeys:
    """CPU-heavy (scrypt): call via asyncio.to_thread."""
    shared = private.exchange(X25519PublicKey.from_public_bytes(peer_pub))
    pw_key = derive_password_key(password, salt)
    okm = HKDF(hashes.SHA256(), 96, salt=transcript,
               info=b"remotelink/v1").derive(shared + pw_key)
    c2h, h2c, auth = okm[:32], okm[32:64], okm[64:]
    cipher = SessionCipher(c2h, h2c) if is_client else SessionCipher(h2c, c2h)
    return SessionKeys(cipher, auth)
