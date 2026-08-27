# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Xaloqi
"""
_security.py — AES-128-CMAC key derivation for UDS SecurityAccess.

Pure-Python path implements FIPS 197 AES-128 and RFC 4493 AES-CMAC,
producing identical output to pycryptodome and the EDS C firmware.
pycryptodome is preferred when available (faster). The pure-Python
fallback works correctly without any native dependencies.

Validated against:
  - cryptography.hazmat AES-ECB + CMAC (ground truth)
  - RFC 4493 Appendix D Examples 1-3
  - FIPS 197 Appendix B
"""

# ---------------------------------------------------------------------------
# Master keys per security level
#
# Production ECUs: set XALOQI_SA_KEY_LEVEL_1 and XALOQI_SA_KEY_LEVEL_2 to
# 32-char hex strings (16 bytes each) matching your ECU's key material.
#
# Simulator / EDS dev firmware: the fallback values below match the placeholder
# keys in uds_security_algo.c (CONFIG_DIAG_PLACEHOLDER_KEYS_ONLY=y).
# EDS production builds reject those placeholder keys at boot — do not use
# these fallback values against a production ECU.
# ---------------------------------------------------------------------------

import os as _os
import warnings as _warnings


def _load_sa_key(level: int, fallback: bytes) -> bytes:
    env_var = f"XALOQI_SA_KEY_LEVEL_{level}"
    hex_val = _os.environ.get(env_var, "").strip()
    if not hex_val:
        return fallback
    try:
        key = bytes.fromhex(hex_val)
    except ValueError as exc:
        raise ValueError(f"{env_var}: invalid hex string — {exc}") from exc
    if len(key) != 16:
        raise ValueError(f"{env_var}: expected 16 bytes, got {len(key)}")
    return key


_SA_KEY_FALLBACK = {
    1: bytes([
        0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07,
        0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F,
    ]),
    2: bytes([
        0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17,
        0x18, 0x19, 0x1A, 0x1B, 0x1C, 0x1D, 0x1E, 0x1F,
    ]),
}

_LEVEL_MASTER_KEYS = {
    level: _load_sa_key(level, fallback)
    for level, fallback in _SA_KEY_FALLBACK.items()
}

# ---------------------------------------------------------------------------
# FIPS 197 AES-128 S-box and round constants
# ---------------------------------------------------------------------------

_SBOX = bytes([
    0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
    0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
    0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
    0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
    0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
    0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
    0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
    0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
    0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
    0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
    0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
    0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
    0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
    0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
    0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
    0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16,
])

_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36]


def _xtime(b: int) -> int:
    return ((b << 1) ^ 0x1B) & 0xFF if b & 0x80 else (b << 1) & 0xFF


def _gmul(a: int, b: int) -> int:
    r = 0
    for _ in range(8):
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r


def _aes_enc(key: bytes, block: bytes) -> bytes:
    """
    FIPS 197 AES-128 single-block encryption.

    State stored flat, column-major: s[r + 4*c] = row r, col c.
    Validated against FIPS 197 Appendix B and cryptography.hazmat reference.
    """
    assert len(key) == 16 and len(block) == 16

    # Key expansion
    w = [key[i * 4: i * 4 + 4] for i in range(4)]
    for i in range(4, 44):
        t = w[i - 1]
        if i % 4 == 0:
            t = bytes([
                _SBOX[t[1]] ^ _RCON[i // 4 - 1],
                _SBOX[t[2]],
                _SBOX[t[3]],
                _SBOX[t[0]],
            ])
        w.append(bytes(a ^ b for a, b in zip(w[i - 4], t)))

    rks = [b"".join(w[r * 4: r * 4 + 4]) for r in range(11)]

    s = bytearray(block)  # column-major flat: s[r + 4*c]

    def add_round_key(rk):
        for i in range(16):
            s[i] ^= rk[i]

    def sub_bytes():
        for i in range(16):
            s[i] = _SBOX[s[i]]

    def shift_rows():
        # Row 1 (indices 1,5,9,13): shift left 1
        s[1], s[5], s[9], s[13] = s[5], s[9], s[13], s[1]
        # Row 2 (indices 2,6,10,14): shift left 2
        s[2], s[6], s[10], s[14] = s[10], s[14], s[2], s[6]
        # Row 3 (indices 3,7,11,15): shift left 3
        s[3], s[7], s[11], s[15] = s[15], s[3], s[7], s[11]

    def mix_columns():
        # Column c: bytes at s[4c], s[4c+1], s[4c+2], s[4c+3]
        for c in range(4):
            i = 4 * c
            s0, s1, s2, s3 = s[i], s[i + 1], s[i + 2], s[i + 3]
            s[i]     = _gmul(0x02, s0) ^ _gmul(0x03, s1) ^ s2              ^ s3
            s[i + 1] = s0              ^ _gmul(0x02, s1) ^ _gmul(0x03, s2) ^ s3
            s[i + 2] = s0              ^ s1              ^ _gmul(0x02, s2) ^ _gmul(0x03, s3)
            s[i + 3] = _gmul(0x03, s0) ^ s1              ^ s2              ^ _gmul(0x02, s3)

    add_round_key(rks[0])
    for rd in range(1, 10):
        sub_bytes()
        shift_rows()
        mix_columns()
        add_round_key(rks[rd])
    sub_bytes()
    shift_rows()
    add_round_key(rks[10])

    return bytes(s)


# ---------------------------------------------------------------------------
# RFC 4493 AES-128-CMAC
# ---------------------------------------------------------------------------

def _cmac_pure_python(key: bytes, msg: bytes) -> bytes:
    """
    AES-128-CMAC per RFC 4493. Returns 16-byte tag.
    Handles messages of any length including empty.
    """
    def _xor(a: bytes, b: bytes) -> bytes:
        return bytes(x ^ y for x, y in zip(a, b))

    def _gen_subkeys(k: bytes):
        L = _aes_enc(k, b"\x00" * 16)
        K1 = (int.from_bytes(L, "big") << 1) & 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF
        if L[0] & 0x80:
            K1 ^= 0x87
        K1 = K1.to_bytes(16, "big")
        K2 = (int.from_bytes(K1, "big") << 1) & 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF
        if K1[0] & 0x80:
            K2 ^= 0x87
        K2 = K2.to_bytes(16, "big")
        return K1, K2

    K1, K2 = _gen_subkeys(key)

    if len(msg) == 0:
        last = _xor(b"\x80" + b"\x00" * 15, K2)
        return _aes_enc(key, last)

    blocks = [msg[i: i + 16] for i in range(0, len(msg), 16)]
    last = blocks[-1]

    if len(last) == 16:
        blocks[-1] = _xor(last, K1)
    else:
        padded = last + b"\x80" + b"\x00" * (15 - len(last))
        blocks[-1] = _xor(padded, K2)

    X = b"\x00" * 16
    for blk in blocks:
        X = _aes_enc(key, _xor(X, blk))
    return X


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def aes_cmac(key: bytes, message: bytes) -> bytes:
    """
    Compute AES-128-CMAC of message using key.

    Uses pycryptodome if available (faster). Falls back to pure-Python
    FIPS 197 / RFC 4493 implementation. Both paths produce identical output.

    Args:
        key:     16-byte AES key.
        message: Input bytes (any length).

    Returns:
        16-byte CMAC.
    """
    try:
        from Crypto.Cipher import AES
        from Crypto.Hash import CMAC
        c = CMAC.new(key, ciphermod=AES)
        c.update(message)
        return c.digest()
    except ImportError:
        return _cmac_pure_python(key, message)


def derive_key(seed: bytes, level: int) -> bytes:
    """
    Derive SecurityAccess key from seed using AES-128-CMAC.

    Args:
        seed:  Seed bytes from ECU 0x67 response. Passed as-is to aes_cmac.
        level: Security level integer (1, 2, ...).

    Returns:
        4-byte key to send in the 0x27 key request.
    """
    if level not in _LEVEL_MASTER_KEYS:
        raise ValueError(
            f"Unknown security level {level}. "
            f"Supported: {list(_LEVEL_MASTER_KEYS.keys())}"
        )
    if _LEVEL_MASTER_KEYS[level] == _SA_KEY_FALLBACK.get(level):
        _warnings.warn(
            f"SecurityAccess level {level}: using simulator placeholder key. "
            f"Set XALOQI_SA_KEY_LEVEL_{level}=<32-hex-chars> for production ECUs.",
            UserWarning,
            stacklevel=2,
        )
    mac = aes_cmac(_LEVEL_MASTER_KEYS[level], seed)
    return mac[:4]
