"""Write-only model credentials protected by the current Windows account.

The catalog owns persistence and transactions; this module only protects bytes.
Unsupported platforms fail closed and retain the environment-variable workflow.
No portable plaintext fallback or application-owned encryption key is created.
"""

import base64
import ctypes
from ctypes import wintypes
import os

from .model_binding import ModelProfileError

PREFIX = "dpapi-user-v1:"


def available():
    return os.name == "nt"


def validate_key(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 4096
            or any(ord(char) < 33 or ord(char) > 126 for char in value)):
        raise ModelProfileError("model_credentials_invalid")
    return value


def _crypt(data, identifier, *, decrypt=False):
    if not available():
        raise ModelProfileError("model_key_storage_unavailable")

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    entropy_buffer = ctypes.create_string_buffer(("astra.model-key.v1:" + identifier).encode("ascii"))
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    entropy = Blob(len(entropy_buffer) - 1, ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
        operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                              ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        operation.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        try:
            # UI_FORBIDDEN; deliberately omit LOCAL_MACHINE so other Windows
            # accounts cannot decrypt a copied database with their own identity.
            if not operation(ctypes.byref(source), None, ctypes.byref(entropy), None,
                             None, 1, ctypes.byref(output)):
                raise OSError
            if not output.data or not 1 <= output.size <= 16384:
                raise ValueError
            return ctypes.string_at(output.data, output.size)
        finally:
            if output.data:
                ctypes.memset(output.data, 0, output.size)
                kernel32.LocalFree(output.data)
    except (OSError, ValueError, AttributeError):
        raise ModelProfileError("model_key_storage_unavailable") from None
    finally:
        ctypes.memset(buffer, 0, len(buffer))


def protect_key(value, identifier):
    value = validate_key(value)
    return PREFIX + base64.b64encode(_crypt(value.encode("ascii"), identifier)).decode("ascii")


def unprotect_key(value, identifier):
    if value is None:
        raise ModelProfileError("model_credentials_missing")
    try:
        if not isinstance(value, str) or not value.startswith(PREFIX) or len(value) > 24000:
            raise ValueError
        data = base64.b64decode(value[len(PREFIX):], validate=True)
        if not data:
            raise ValueError
        return validate_key(_crypt(data, identifier, decrypt=True).decode("ascii"))
    except (ValueError, UnicodeError):
        raise ModelProfileError("model_key_storage_unavailable") from None
