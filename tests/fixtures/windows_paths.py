"""Owned-fixture 8.3 aliases; never change the volume's short-name settings."""
import ctypes
import os
from pathlib import Path
import unittest


def short_path(path):
    if os.name != "nt":
        raise unittest.SkipTest("Windows short path API")
    api = ctypes.windll.kernel32.GetShortPathNameW
    api.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
    api.restype = ctypes.c_uint32
    required = api(str(path), None, 0)
    if not required:
        raise unittest.SkipTest("Short paths are unavailable on this volume")
    output = ctypes.create_unicode_buffer(required)
    count = api(str(path), output, required)
    if not count or count >= required:
        raise unittest.SkipTest("Short path query failed")
    result = Path(output.value)
    if os.path.normcase(str(result)) == os.path.normcase(str(Path(path).resolve())):
        raise unittest.SkipTest("This fixture has no distinct short-name alias")
    return result
