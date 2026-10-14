"""Use SQLNEW through CPython's native sqlite3 binding in an isolated process.

Windows CPython dynamically links _sqlite3.pyd to sqlite3.dll. Preload the local
product build under that dependency name before importing the binding. Nothing
is installed into or changed in the Python installation. Fail closed if the
wrong backend has already been loaded. Unix needs a binding linked to SQLNEW;
this bootstrap intentionally does not pretend LD_LIBRARY_PATH can repair a
running process.
"""
import ctypes
from pathlib import Path
import re
import sys

_library = None


def activate():
    global _library
    if sys.platform != "win32":
        raise RuntimeError("this CPython bootstrap currently supports Windows only")
    if "_sqlite3" in sys.modules and _library is None:
        raise RuntimeError("activate SQLNEW before importing sqlite3 or doc2sql")
    library = Path(__file__).resolve().parents[1] / "build/sqlnew/python/sqlite3.dll"
    if _library is None:
        if not library.is_file():
            raise RuntimeError("run python sqlnew/build.py first")
        _library = ctypes.WinDLL(str(library))
    import sqlite3
    connection = sqlite3.connect(":memory:")
    try:
        config = Path(__file__).with_name("config.h").read_text(encoding="utf-8")
        expected = re.search(r'#define SQLNEW_VERSION "([^"]+)"', config).group(1)
        if connection.execute("SELECT sqlnew_version()").fetchone()[0] != expected:
            raise RuntimeError("unexpected SQLNEW product version")
    finally:
        connection.close()
    return sqlite3
