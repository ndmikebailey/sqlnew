"""Small synthetic regressions from the independent core source review."""
import ctypes as C
import json
from pathlib import Path
import tempfile
import unittest

from db import Database, Error


class CoreReviewTests(unittest.TestCase):
    def command(self, db, name, mode="", keys=None):
        db.execute(f"INSERT {mode} INTO t(__sqlnew_command) VALUES(?)",
                   (json.dumps({"create_index": name, "keys": keys or ["x"]}),))

    def test_text_encodings(self):
        for encoding in ("UTF-8", "UTF-16le", "UTF-16be"):
            with self.subTest(encoding=encoding), Database() as db:
                db.execute(f"PRAGMA encoding='{encoding}'")
                db.execute("CREATE VIRTUAL TABLE t USING sqlnew_columnar(x TEXT,k INTEGER)")
                db.execute("CREATE TABLE reference(x TEXT,k INTEGER)")
                values = [None, "", "a", "\u00ff", "\u0100", "\ue000", "\U00010000", "a\0b", "a\0a"]
                for table in ("t", "reference"):
                    db.executemany(f"INSERT INTO {table} VALUES(?,?)", [(v, i % 2) for i, v in enumerate(values)])
                queries = [
                    ("SELECT min(x),max(x),count(x) FROM {table}", ()),
                    ("SELECT x FROM {table} WHERE x<? ORDER BY x", ("\u00ff",)),
                    ("SELECT x FROM {table} WHERE x>=? ORDER BY x", ("\ue000",)),
                    ("SELECT k,min(x),max(x) FROM {table} GROUP BY k ORDER BY k", ()),
                    ("SELECT x,count(*) FROM {table} GROUP BY x ORDER BY x", ()),
                    ("SELECT x FROM {table} ORDER BY x,rowid", ()),
                ]
                for indexed in (False, True):
                    if indexed:
                        self.command(db, "text_key")
                    for query, args in queries:
                        with self.subTest(indexed=indexed, query=query):
                            self.assertEqual(db.execute(query.format(table="t"), args),
                                             db.execute(query.format(table="reference"), args))
                db.execute("UPDATE t SET x=? WHERE rowid=2", ("\u0100",))
                db.execute("UPDATE reference SET x=? WHERE rowid=2", ("\u0100",))
                self.assertEqual(db.execute("SELECT min(x),max(x) FROM t"),
                                 db.execute("SELECT min(x),max(x) FROM reference"))
                self.assertEqual(db.scalar("PRAGMA integrity_check"), "ok")

    def test_duplicate_index_commands(self):
        for mode in ("OR IGNORE", "OR FAIL", "OR ABORT", "OR REPLACE"):
            with self.subTest(mode=mode), Database() as db:
                db.execute("CREATE VIRTUAL TABLE t USING sqlnew_columnar(x INTEGER)")
                db.execute("INSERT INTO t VALUES(1),(2),(3)")
                self.command(db, "ix")
                before = db.execute("SELECT name FROM sqlite_schema ORDER BY name")
                db.execute("BEGIN")
                if mode == "OR IGNORE":
                    self.command(db, "ix", mode)
                else:
                    with self.assertRaises(Error):
                        self.command(db, "ix", mode)
                self.assertEqual(db.execute("SELECT name FROM sqlite_schema ORDER BY name"), before)
                self.command(db, "iy")
                db.execute("COMMIT")
                self.assertEqual(db.scalar("PRAGMA integrity_check"), "ok")
                db.execute("DROP TABLE t")
                self.assertEqual(db.execute("SELECT name FROM sqlite_schema"), [])

    def memory_probe(self, pending):
        with Database() as db:
            db.lib.sqlite3_memory_used.argtypes = []
            db.lib.sqlite3_memory_used.restype = C.c_int64
            db.lib.sqlite3_memory_highwater.argtypes = [C.c_int]
            db.lib.sqlite3_memory_highwater.restype = C.c_int64
            db.execute("CREATE VIRTUAL TABLE t USING sqlnew_columnar(x BLOB,k INTEGER)")
            db.execute("BEGIN")
            value = bytes(range(256)) * 64
            db.executemany("INSERT INTO t VALUES(?,?)", [(value, i % 3) for i in range(256)])
            if not pending:
                db.execute("COMMIT")
            before = db.lib.sqlite3_memory_used()
            db.lib.sqlite3_memory_highwater(1)
            self.assertEqual(db.execute("SELECT length(x) FROM t"), [(len(value),)] * 256)
            peak = db.lib.sqlite3_memory_highwater(0) - before
            print(f"dictionary pending={pending} rows=256 value_bytes={len(value)} extra_peak_bytes={peak}")
            self.assertLess(peak, 512 * 1024)
            if pending:
                db.execute("ROLLBACK")

    def test_dictionary_read_memory(self):
        self.memory_probe(False)

    def test_pending_snapshot_memory(self):
        self.memory_probe(True)

    def test_payload_budget_and_shared_value_replacement(self):
        with Database() as db:
            db.execute("CREATE VIRTUAL TABLE t USING sqlnew_columnar(x BLOB,k INTEGER)")
            self.assertEqual(db.scalar("SELECT sqlnew_columnar_memory_limit(131072)"), 64 * 1024 * 1024)
            with self.assertRaises(Error):
                db.execute("INSERT INTO t VALUES(zeroblob(262144),1)")
            self.assertEqual(db.scalar("SELECT count(*) FROM t"), 0)
            self.assertEqual(json.loads(db.scalar("SELECT sqlnew_columnar_stats('t')"))["connection_payload_bytes"], 0)
            db.execute("INSERT INTO t VALUES(x'0102',1),(x'0102',1),(x'0102',2)")
            db.execute("BEGIN")
            db.execute("UPDATE t SET x=x'ffff' WHERE rowid=2")
            self.assertEqual(db.execute("SELECT x FROM t ORDER BY rowid"), [(b'\x01\x02',), (b'\xff\xff',), (b'\x01\x02',)])
            db.execute("ROLLBACK")
            self.assertEqual(db.execute("SELECT k,min(x),max(x) FROM t GROUP BY k ORDER BY k"),
                             [(1, b'\x01\x02', b'\x01\x02'), (2, b'\x01\x02', b'\x01\x02')])
            self.assertEqual(db.scalar("PRAGMA integrity_check"), "ok")
            db.execute("DROP TABLE t")

    def test_parameterized_text_count(self):
        with Database() as db:
            db.execute("CREATE VIRTUAL TABLE t USING sqlnew_columnar(s TEXT)")
            db.execute("INSERT INTO t VALUES(NULL),('a'),('b'),('z')")
            self.assertEqual(db.scalar("SELECT count(*) FROM t WHERE s<?", ("z",)), 2)

    def test_utf16_reopen_and_pending_text(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "utf16.sqlnew"
            with Database(path) as db:
                db.execute("PRAGMA encoding='UTF-16le'")
                db.execute("CREATE VIRTUAL TABLE t USING sqlnew_columnar(x TEXT)")
                db.execute("INSERT INTO t VALUES(char(255)),(char(256))")
                self.command(db, "ix")
            with Database(path) as db:
                self.assertEqual(db.execute("SELECT unicode(min(x)),unicode(max(x)) FROM t"), [(256, 255)])
                db.execute("BEGIN")
                db.execute("INSERT INTO t VALUES(char(257))")
                self.assertEqual(db.execute("SELECT unicode(x) FROM t WHERE x<? ORDER BY x", ("\u00ff",)), [(256,), (257,)])
                db.execute("ROLLBACK")
                self.assertEqual(db.scalar("PRAGMA integrity_check"), "ok")

    def test_index_conflict_with_pending_writes_and_reused_root(self):
        with Database() as db:
            db.execute("CREATE VIRTUAL TABLE t USING sqlnew_columnar(x INTEGER)")
            self.command(db, "ix")
            db.execute("INSERT INTO t(__sqlnew_command) VALUES(?)", (json.dumps({"drop_index": "ix"}),))
            self.command(db, "ix")
            db.execute("BEGIN")
            db.execute("INSERT INTO t VALUES(4),(5)")
            before = db.execute("SELECT name FROM sqlite_schema ORDER BY name")
            self.command(db, "ix", "OR IGNORE")
            self.assertEqual(db.execute("SELECT name FROM sqlite_schema ORDER BY name"), before)
            self.assertEqual(db.execute("SELECT x FROM t ORDER BY x"), [(4,), (5,)])
            db.execute("ROLLBACK")
            self.assertEqual(db.scalar("SELECT count(*) FROM t"), 0)
            self.assertEqual(db.scalar("PRAGMA integrity_check"), "ok")

    def test_summary_statements_observe_updates_rollback_and_rename(self):
        with Database() as db:
            db.execute("CREATE VIRTUAL TABLE t USING sqlnew_columnar(x INTEGER,y INTEGER)")
            db.execute("INSERT INTO t VALUES(1,11),(2,12),(3,13)")
            query = "SELECT count(*),sum(x),min(x),max(x),sum(y) FROM {}"
            self.assertEqual(db.execute(query.format('t')), [(3, 6, 1, 3, 36)])
            db.execute("BEGIN")
            db.execute("UPDATE t SET x=20,y=200 WHERE rowid=2")
            self.assertEqual(db.execute(query.format('t')), [(3, 24, 1, 20, 224)])
            db.execute("ROLLBACK")
            self.assertEqual(db.execute(query.format('t')), [(3, 6, 1, 3, 36)])
            db.execute("ALTER TABLE t RENAME TO renamed")
            self.assertEqual(db.execute(query.format('renamed')), [(3, 6, 1, 3, 36)])
            db.execute("DROP TABLE renamed")
            self.assertEqual(db.execute("SELECT name FROM sqlite_schema"), [])


if __name__ == "__main__":
    unittest.main()
