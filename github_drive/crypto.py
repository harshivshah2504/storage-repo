"""Optional AES-128-GCM encryption layer applied to file bytes before upload.

Format on disk for an encrypted asset:
    magic (4 bytes)  = b"GDRV"
    version (1 byte) = 0x01
    nonce (12 bytes)
    tag   (16 bytes)
    ciphertext (...)

Encryption is opt-in. When disabled the file is uploaded as-is.
"""
from __future__ import annotations

import os
from typing import Optional

MAGIC = b"GDRV"
VERSION = 0x01
NONCE_BYTES = 12
TAG_BYTES = 16
HEADER_LEN = len(MAGIC) + 1 + NONCE_BYTES + TAG_BYTES


def encrypt_file(source_path: str, output_path: str, key: bytes) -> None:
    _validate_key(key)
    from Crypto.Cipher import AES

    nonce = os.urandom(NONCE_BYTES)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    with open(source_path, "rb") as src:
        plaintext = src.read()
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    with open(output_path, "wb") as dst:
        dst.write(MAGIC)
        dst.write(bytes([VERSION]))
        dst.write(nonce)
        dst.write(tag)
        dst.write(ciphertext)


def decrypt_file(source_path: str, output_path: str, key: bytes) -> None:
    _validate_key(key)
    from Crypto.Cipher import AES

    with open(source_path, "rb") as src:
        header = src.read(HEADER_LEN)
        if len(header) < HEADER_LEN or header[: len(MAGIC)] != MAGIC:
            raise RuntimeError(f"{source_path} is not a github-drive encrypted file.")
        version = header[len(MAGIC)]
        if version != VERSION:
            raise RuntimeError(f"Unsupported encryption version {version} in {source_path}")
        nonce = header[len(MAGIC) + 1 : len(MAGIC) + 1 + NONCE_BYTES]
        tag = header[len(MAGIC) + 1 + NONCE_BYTES : HEADER_LEN]
        ciphertext = src.read()
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    plaintext = cipher.decrypt_and_verify(ciphertext, tag)
    with open(output_path, "wb") as dst:
        dst.write(plaintext)


def _validate_key(key: Optional[bytes]) -> None:
    if not isinstance(key, (bytes, bytearray)) or len(key) not in (16, 24, 32):
        raise RuntimeError("AES key must be 16, 24, or 32 bytes.")
