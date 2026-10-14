"""Small ctypes driver that always loads the SQLite built from this checkout."""
import ctypes as C
import os
from contextlib import contextmanager
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SUFFIX = ".dll" if sys.platform == "win32" else ".dylib" if sys.platform == "darwin" else ".so"
BUILD = Path(os.environ.get("SQLNEW_BUILD_DIR", str(ROOT / "build"))).resolve()


class Error(RuntimeError):
    pass


class Database:
    def __init__(self, path=":memory:", library=None):
        self.lib = C.CDLL(str(library or BUILD / "sqlnew" / ("sqlnewdb" + SUFFIX)))
        signatures = {
            "open_v2": ([C.c_char_p, C.POINTER(C.c_void_p), C.c_int, C.c_char_p], C.c_int),
            "close_v2": ([C.c_void_p], C.c_int),
            "errmsg": ([C.c_void_p], C.c_char_p),
            "prepare_v2": ([C.c_void_p, C.c_char_p, C.c_int, C.POINTER(C.c_void_p), C.POINTER(C.c_char_p)], C.c_int),
            "step": ([C.c_void_p], C.c_int),
            "reset": ([C.c_void_p], C.c_int),
            "clear_bindings": ([C.c_void_p], C.c_int),
            "finalize": ([C.c_void_p], C.c_int),
            "bind_parameter_count": ([C.c_void_p], C.c_int),
            "bind_null": ([C.c_void_p, C.c_int], C.c_int),
            "bind_int64": ([C.c_void_p, C.c_int, C.c_int64], C.c_int),
            "bind_double": ([C.c_void_p, C.c_int, C.c_double], C.c_int),
            "bind_text": ([C.c_void_p, C.c_int, C.c_char_p, C.c_int, C.c_void_p], C.c_int),
            "bind_blob": ([C.c_void_p, C.c_int, C.c_void_p, C.c_int, C.c_void_p], C.c_int),
            "column_count": ([C.c_void_p], C.c_int),
            "column_type": ([C.c_void_p, C.c_int], C.c_int),
            "column_int64": ([C.c_void_p, C.c_int], C.c_int64),
            "column_double": ([C.c_void_p, C.c_int], C.c_double),
            "column_blob": ([C.c_void_p, C.c_int], C.c_void_p),
            "column_text": ([C.c_void_p, C.c_int], C.c_void_p),
            "column_bytes": ([C.c_void_p, C.c_int], C.c_int),
            "busy_timeout": ([C.c_void_p, C.c_int], C.c_int),
            "interrupt": ([C.c_void_p], None),
            "libversion": ([], C.c_char_p),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.lib, "sqlite3_" + name)
            function.argtypes, function.restype = args, result
        self.handle = C.c_void_p()
        self._savepoint = 0
        rc = self.lib.sqlite3_open_v2(str(path).encode(), C.byref(self.handle), 2 | 4 | 0x10000, None)
        if rc:
            message = self.lib.sqlite3_errmsg(self.handle).decode()
            self.close()
            raise Error(message)
        self.check(self.lib.sqlite3_busy_timeout(self.handle, 5000))

    def check(self, rc):
        if rc != 0:
            raise Error(self.lib.sqlite3_errmsg(self.handle).decode("utf-8", "replace"))

    def prepare(self, sql):
        if not self.handle:
            raise Error("database is closed")
        text = sql.encode()
        statement, tail = C.c_void_p(), C.c_char_p()
        self.check(self.lib.sqlite3_prepare_v2(self.handle, text, len(text), C.byref(statement), C.byref(tail)))
        if not statement or (tail.value and tail.value.strip()):
            if statement:
                self.lib.sqlite3_finalize(statement)
            raise Error("exactly one SQL statement is required")
        return statement

    def bind(self, statement, parameters):
        if len(parameters) != self.lib.sqlite3_bind_parameter_count(statement):
            raise Error("wrong number of SQL parameters")
        for i, value in enumerate(parameters, 1):
            if value is None:
                rc = self.lib.sqlite3_bind_null(statement, i)
            elif isinstance(value, int):
                if not -(1 << 63) <= value < (1 << 63):
                    raise OverflowError("integer is outside SQLite int64 range")
                rc = self.lib.sqlite3_bind_int64(statement, i, value)
            elif isinstance(value, float):
                rc = self.lib.sqlite3_bind_double(statement, i, value)
            elif isinstance(value, str):
                encoded = value.encode()
                rc = self.lib.sqlite3_bind_text(statement, i, encoded, len(encoded), C.c_void_p(-1))
            else:
                encoded = bytes(value)
                rc = self.lib.sqlite3_bind_blob(statement, i, encoded, len(encoded), C.c_void_p(-1))
            self.check(rc)

    def rows(self, statement):
        while True:
            rc = self.lib.sqlite3_step(statement)
            if rc == 101:
                return
            if rc != 100:
                self.check(rc)
            row = []
            for i in range(self.lib.sqlite3_column_count(statement)):
                kind = self.lib.sqlite3_column_type(statement, i)
                if kind == 1:
                    value = self.lib.sqlite3_column_int64(statement, i)
                elif kind == 2:
                    value = self.lib.sqlite3_column_double(statement, i)
                elif kind in (3, 4):
                    ptr = (self.lib.sqlite3_column_text if kind == 3 else self.lib.sqlite3_column_blob)(statement, i)
                    size = self.lib.sqlite3_column_bytes(statement, i)
                    value = C.string_at(ptr, size) if size else b""
                    if kind == 3:
                        value = value.decode()
                else:
                    value = None
                row.append(value)
            yield tuple(row)

    def execute(self, sql, parameters=()):
        statement = self.prepare(sql)
        try:
            self.bind(statement, parameters)
            return list(self.rows(statement))
        finally:
            self.lib.sqlite3_finalize(statement)

    def scalar(self, sql, parameters=()):
        rows = self.execute(sql, parameters)
        return rows[0][0] if rows else None

    def executemany(self, sql, parameters):
        statement = self.prepare(sql)
        try:
            for values in parameters:
                self.bind(statement, values)
                for _ in self.rows(statement):
                    pass
                self.check(self.lib.sqlite3_reset(statement))
                self.check(self.lib.sqlite3_clear_bindings(statement))
        finally:
            self.lib.sqlite3_finalize(statement)

    @contextmanager
    def transaction(self):
        self._savepoint += 1
        name = f"sn_savepoint_{self._savepoint}"
        self.execute(f"SAVEPOINT {name}")
        try:
            yield self
            self.execute(f"RELEASE {name}")
        except BaseException:
            self.execute(f"ROLLBACK TO {name}")
            self.execute(f"RELEASE {name}")
            raise

    def close(self):
        if self.handle:
            self.check(self.lib.sqlite3_close_v2(self.handle))
            self.handle = C.c_void_p()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
