"""SQL semantics, pruning, summaries and transactions for native column tables."""
import json
from pathlib import Path
import random
import struct
import subprocess
import sys
import tempfile
import unittest

from db import Database, Error


class ColumnarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "columns.sqlnew"
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def stats(self):
        return json.loads(self.db.scalar("SELECT sqlnew_columnar_stats('t')"))

    def sequence(self, count=8192):
        self.db.execute("CREATE VIRTUAL TABLE t USING sqlnew_columnar(a INTEGER,b INTEGER,c TEXT)")
        self.db.execute("""WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<?)
            INSERT INTO t SELECT x,x%16,'repeated text '||(x%8) FROM n""", (count,))

    def test_types_codecs_nulls_and_reopen(self):
        self.db.execute("CREATE VIRTUAL TABLE t USING sqlnew_columnar(a INTEGER,b REAL,c TEXT,d BLOB)")
        rng = random.Random(412)
        values = [(None if i % 17 == 0 else rng.randrange(-(1 << 63), 1 << 63),
                   None if i % 19 == 0 else rng.uniform(-1e100, 1e100),
                   None if i % 13 == 0 else ("same\0世界" if i % 3 else str(i) * 20),
                   None if i % 11 == 0 else rng.randbytes(i % 39)) for i in range(2300)]
        with self.db.transaction():
            self.db.executemany("INSERT INTO t VALUES(?,?,?,?)", values)
        self.assertEqual(self.db.execute("SELECT * FROM t ORDER BY rowid"), values)
        self.db.close()
        self.db = Database(self.path)
        self.assertEqual(self.db.execute("SELECT * FROM t ORDER BY rowid"), values)
        self.assertEqual(self.db.scalar("PRAGMA integrity_check"), "ok")

    def test_affine_constant_dictionary_and_all_null(self):
        self.db.execute("CREATE VIRTUAL TABLE t USING sqlnew_columnar(a INTEGER,b INTEGER,c TEXT,d INTEGER)")
        self.db.execute("""WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<2047)
            INSERT INTO t(rowid,a,b,c,d) SELECT x,1000000+x*7,42,'dictionary-'||(x%4),NULL FROM n""")
        frames = self.db.execute("SELECT col,sqlnew_unpack(data) FROM t_data WHERE block=0 ORDER BY col")
        self.assertEqual(frames[0][1][5], 1)  # Affine sequence.
        self.assertEqual(len(frames[0][1]), 32)
        self.assertEqual(len(frames[1][1]), 32)  # Constant.
        self.assertEqual(frames[2][1][5], 1)  # Dictionary.
        self.assertEqual(len(frames[3][1]), 32)  # All NULL.
        self.assertEqual(self.db.execute("SELECT a,b,c,d FROM t WHERE rowid=1025"),
                         [(1000000 + 1025 * 7, 42, "dictionary-1", None)])

    def test_summary_uses_no_payload_and_handles_expressions(self):
        self.sequence()
        before = self.stats()
        result = self.db.execute("SELECT count(*),count(a),sum(a),min(a),max(a),min(c),sum(a)+count(*) FROM t")[0]
        total = 8192 * 8193 // 2
        self.assertEqual(result, (8192, 8192, total, 1, 8192, "repeated text 0", total + 8192))
        after = self.stats()
        self.assertEqual(after["columns_decoded"], before["columns_decoded"])
        self.assertEqual(after["rows_emitted"], before["rows_emitted"])
        self.assertGreater(after["summary_blocks"], before["summary_blocks"])
        self.assertIn("COLUMNAR SUMMARY", str(self.db.execute("EXPLAIN QUERY PLAN SELECT sum(a) FROM t")))
        self.assertEqual(self.db.execute("SELECT count(*),sum(a),min(a),max(a) FROM t WHERE 0"),
                         [(0, None, None, None)])
        self.db.execute("DELETE FROM t")
        self.assertEqual(self.db.execute("SELECT count(*),count(a),sum(a),min(a),max(a) FROM t"),
                         [(0, 0, None, None, None)])

    def test_range_pruning_projection_and_sql_affinity(self):
        self.sequence(16384)
        before = self.stats()
        self.assertEqual(self.db.execute("SELECT a FROM t WHERE a BETWEEN 12000 AND 12002 ORDER BY a"),
                         [(12000,), (12001,), (12002,)])
        after = self.stats()
        self.assertEqual(after["blocks_read"] - before["blocks_read"], 1)
        self.assertEqual(after["columns_decoded"] - before["columns_decoded"], 1)
        for bound, expected in [(12000, [(12000,)]), (12000.0, [(12000,)]),
                                (12000.25, []), ("12000", [(12000,)]), ("no", []), (None, [])]:
            self.assertEqual(self.db.execute("SELECT a FROM t WHERE a=?", (bound,)), expected)
            self.assertEqual(self.db.execute("SELECT a FROM t WHERE rowid=?", (bound,)), expected)
        self.assertEqual(self.db.execute("SELECT a FROM t WHERE rowid IN (8,2,4000) ORDER BY rowid"),
                         [(2,), (8,), (4000,)])
        self.assertEqual(self.db.scalar("SELECT count(*) FROM t WHERE c='REPEATED TEXT 0' COLLATE NOCASE"), 2048)

    def test_updates_deletes_moved_rowids_and_conflicts(self):
        self.sequence(2600)
        self.db.execute("UPDATE t SET a=-a WHERE rowid IN (2,1024,2049)")
        self.db.execute("DELETE FROM t WHERE rowid=3")
        self.db.execute("UPDATE t SET rowid=-5000 WHERE rowid=5")
        self.assertEqual(self.db.execute("SELECT rowid,a FROM t WHERE rowid IN (-5000,2,3,5,1024,2049) ORDER BY rowid"),
                         [(-5000, 5), (2, -2), (1024, -1024), (2049, -2049)])
        with self.assertRaises(Error):
            self.db.execute("INSERT INTO t(rowid,a,b,c) VALUES(2,99,99,'x')")
        self.db.execute("INSERT OR IGNORE INTO t(rowid,a,b,c) VALUES(2,99,99,'x')")
        self.assertEqual(self.db.scalar("SELECT a FROM t WHERE rowid=2"), -2)
        self.db.execute("INSERT OR REPLACE INTO t(rowid,a,b,c) VALUES(2,99,99,'x')")
        self.assertEqual(self.db.scalar("SELECT a FROM t WHERE rowid=2"), 99)
        with self.assertRaises(Error):
            self.db.execute("UPDATE t SET rowid=2 WHERE rowid=4")
        self.assertEqual(self.db.scalar("SELECT a FROM t WHERE rowid=4"), 4)

    def test_insert_identity_and_ignored_change_counts(self):
        self.db.execute("CREATE VIRTUAL TABLE t USING sqlnew_columnar(a INTEGER,b TEXT)")
        self.assertEqual(self.db.scalar("SELECT last_insert_rowid()"), 0)
        self.db.execute("INSERT INTO t(rowid,a,b) VALUES(5000,1,'first')")
        self.assertEqual(self.db.execute("SELECT last_insert_rowid(),changes()"), [(5000, 1)])
        self.db.execute("BEGIN")
        self.db.execute("INSERT INTO t(rowid,a,b) VALUES(8000,2,'second')")
        self.db.execute("SAVEPOINT s")
        self.assertEqual(self.db.scalar("SELECT last_insert_rowid()"), 8000)
        self.db.execute("INSERT OR IGNORE INTO t(rowid,a,b) VALUES(5000,3,'ignored')")
        self.assertEqual(self.db.execute("SELECT last_insert_rowid(),changes()"), [(8000, 0)])
        self.db.execute("UPDATE OR IGNORE t SET rowid=5000 WHERE rowid=8000")
        self.assertEqual(self.db.execute("SELECT last_insert_rowid(),changes()"), [(8000, 0)])
        self.db.execute("RELEASE s")
        self.db.execute("COMMIT")
        self.assertEqual(self.db.scalar("SELECT last_insert_rowid()"), 8000)
        self.db.execute("INSERT OR IGNORE INTO t(rowid,a,b) VALUES(5000,3,'ignored'),(9000,4,'third')")
        self.assertEqual(self.db.execute("SELECT last_insert_rowid(),changes()"), [(9000, 1)])

    def test_savepoint_statement_rollback_and_transactional_drop(self):
        self.sequence(1200)
        self.db.execute("BEGIN")
        self.db.execute("INSERT INTO t VALUES(9000,0,'pending')")
        self.db.execute("SAVEPOINT s")
        self.db.execute("UPDATE t SET a=-1")
        self.db.execute("DELETE FROM t WHERE rowid=2")
        self.db.execute("ROLLBACK TO s")
        self.assertEqual(self.db.scalar("SELECT sum(a) FROM t"), 1200 * 1201 // 2 + 9000)
        with self.assertRaises(Error):
            self.db.execute("""WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<2400)
                INSERT INTO t SELECT CASE WHEN x=2399 THEN 'bad' ELSE x END,0,'new' FROM n""")
        self.assertEqual(self.db.scalar("SELECT count(*) FROM t"), 1201)
        self.db.execute("SAVEPOINT d")
        self.db.execute("DROP TABLE t")
        self.db.execute("ROLLBACK TO d")
        self.assertEqual(self.db.scalar("SELECT count(*) FROM t"), 1201)
        self.db.execute("RELEASE d")
        self.db.execute("RELEASE s")
        self.db.execute("ROLLBACK")
        self.assertEqual(self.db.scalar("SELECT count(*) FROM t"), 1200)

    def test_wal_snapshot_and_pending_summaries(self):
        self.db.execute("PRAGMA journal_mode=WAL")
        self.sequence(2100)
        with Database(self.path) as other:
            other.execute("BEGIN")
            self.assertEqual(other.scalar("SELECT count(*) FROM t"), 2100)
            self.db.execute("BEGIN")
            self.db.execute("DELETE FROM t WHERE rowid=2")
            self.db.execute("INSERT INTO t VALUES(99999,0,'new')")
            self.assertEqual(self.db.scalar("SELECT sum(a) FROM t"), 2100 * 2101 // 2 - 2 + 99999)
            self.db.execute("COMMIT")
            self.assertEqual(other.scalar("SELECT sum(a) FROM t"), 2100 * 2101 // 2)
            other.execute("COMMIT")
            self.assertEqual(other.scalar("SELECT sum(a) FROM t"), 2100 * 2101 // 2 - 2 + 99999)

    def test_process_exit_recovers_last_commit(self):
        self.db.close()
        child = """
import os,sys
from db import Database
d=Database(sys.argv[1])
d.execute('CREATE VIRTUAL TABLE t USING sqlnew_columnar(a INTEGER,b TEXT)')
d.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<2200) INSERT INTO t SELECT x,printf(\"event-%d\",x) FROM n')
d.execute('BEGIN')
d.execute('UPDATE t SET a=-a')
d.execute('DELETE FROM t WHERE rowid<1200')
d.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<6000) INSERT INTO t SELECT x,printf(\"pending-%d\",x) FROM n')
os._exit(23)
"""
        for mode in ("DELETE", "WAL"):
            path = Path(self.temp.name) / (mode + ".sqlnew")
            with Database(path) as d:
                self.assertEqual(d.scalar("PRAGMA journal_mode=" + mode), mode.lower())
            result = subprocess.run([sys.executable, "-c", child, str(path)],
                                    cwd=Path(__file__).resolve().parent, timeout=30,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 23, result.stderr)
            with Database(path) as d:
                self.assertEqual(d.execute("SELECT count(*),sum(a),min(a),max(a) FROM t"),
                                 [(2200, 2200 * 2201 // 2, 1, 2200)])
                self.assertEqual(d.scalar("SELECT b FROM t WHERE rowid=2199"), "event-2199")
                self.assertEqual(d.scalar("PRAGMA integrity_check"), "ok")

    def test_aggregate_fallbacks_grouping_distinct_filter_and_collation(self):
        self.sequence(2100)
        cases = [
            ("SELECT sum(a+0),count(DISTINCT b) FROM t", [(2100 * 2101 // 2, 16)]),
            ("SELECT count(*) FILTER (WHERE b=0) FROM t", [(2100 // 16,)]),
            ("SELECT sum(a) FROM t WHERE a<4", [(6,)]),
            ("SELECT min(c COLLATE NOCASE) FROM t", [("repeated text 0",)]),
            ("SELECT count(*) FROM t HAVING count(*)>100", [(2100,)]),
        ]
        for sql, expected in cases:
            self.assertEqual(self.db.execute(sql), expected)
            self.assertNotIn("COLUMNAR SUMMARY", str(self.db.execute("EXPLAIN QUERY PLAN " + sql)))
        self.assertEqual(self.db.execute("SELECT b,count(*) FROM t GROUP BY b ORDER BY b"),
                         [(i, sum(x % 16 == i for x in range(1, 2101))) for i in range(16)])

    def test_full_int64_and_overflow_prefixes(self):
        self.db.execute("CREATE VIRTUAL TABLE t USING sqlnew_columnar(a INTEGER)")
        values = [-(1 << 63), (1 << 63) - 1, 1, None, -5, 7]
        with self.db.transaction():
            self.db.executemany("INSERT INTO t VALUES(?)", ((v,) for v in values))
        self.assertEqual(self.db.scalar("SELECT sum(a) FROM t"), 2)
        self.assertEqual(self.db.scalar("SELECT sum(a+0) FROM t"), 2)
        self.db.execute("DELETE FROM t")
        self.db.execute("INSERT INTO t VALUES(?),(1),(-1)", ((1 << 63) - 1,))
        with self.assertRaises(Error):
            self.db.scalar("SELECT sum(a) FROM t")
        with self.assertRaises(Error):
            self.db.scalar("SELECT sum(a+0) FROM t")
        # The same final sum can fit while an intermediate prefix in a later
        # group overflows. The summary must check the preceding group's total.
        for rows, expected in [([(1, (1 << 63) - 1), (1024, 1), (1025, -1)], None),
                               ([(1, -(1 << 63)), (1024, -1), (1025, 1)], None),
                               ([(1, -1), (1024, (1 << 63) - 1), (1025, 1)], (1 << 63) - 1)]:
            self.db.execute("DELETE FROM t")
            self.db.executemany("INSERT INTO t(rowid,a) VALUES(?,?)", rows)
            for expr in ("a", "a+0"):
                if expected is None:
                    with self.assertRaises(Error):
                        self.db.scalar("SELECT sum(" + expr + ") FROM t")
                else:
                    self.assertEqual(self.db.scalar("SELECT sum(" + expr + ") FROM t"), expected)
        self.db.execute("DELETE FROM t")
        self.db.executemany("INSERT INTO t(rowid,a) VALUES(?,?)",
                            [(-(1 << 63), 10), (-1, 20), (0, 30), ((1 << 63) - 1, 40)])
        self.assertEqual(self.db.execute("SELECT rowid,a FROM t ORDER BY rowid"),
                         [(-(1 << 63), 10), (-1, 20), (0, 30), ((1 << 63) - 1, 40)])
        self.assertEqual(self.db.scalar("SELECT sum(a) FROM t"), 100)

    def test_rename_attach_join_and_quotes(self):
        self.db.execute('CREATE VIRTUAL TABLE "odd table" USING sqlnew_columnar("a value" INTEGER,b TEXT)')
        self.db.execute('INSERT INTO "odd table" VALUES(7,\'hi\')')
        self.db.execute('ALTER TABLE "odd table" RENAME TO t')
        self.assertEqual(self.db.scalar('SELECT sum("a value") FROM t'), 7)
        self.db.execute('CREATE VIRTUAL TABLE "odd table" USING sqlnew_columnar(a INTEGER)')
        self.db.execute('BEGIN')
        self.db.execute('ALTER TABLE t RENAME TO transient_name')
        self.db.execute('ROLLBACK')
        self.assertEqual(self.db.scalar('SELECT sum("a value") FROM t'), 7)
        self.assertEqual(self.db.scalar("PRAGMA integrity_check"), "ok")
        self.db.execute("ATTACH DATABASE ? AS extra", (str(Path(self.temp.name) / "extra.sqlnew"),))
        self.db.execute("CREATE VIRTUAL TABLE extra.t USING sqlnew_columnar(a INTEGER)")
        self.db.execute("INSERT INTO extra.t VALUES(9)")
        self.assertEqual(self.db.execute('SELECT t.b,e.a FROM t CROSS JOIN extra.t e'), [("hi", 9)])
        self.assertEqual(self.db.scalar("SELECT sum(a) FROM extra.t"), 9)

    def test_corrupt_payload_rejected_on_read(self):
        self.sequence(2048)
        frame = self.db.scalar("SELECT sqlnew_unpack(data) FROM t_data WHERE block=0 AND col=0")
        for index in (0, 4, 5, 6, 7, 24, 28):
            damaged = bytearray(frame)
            damaged[index] ^= 0x7f
            self.db.execute("UPDATE t_data SET data=sqlnew_pack(?) WHERE block=0 AND col=0", (damaged,))
            with self.assertRaises(Error):
                self.db.execute("SELECT a FROM t WHERE rowid=1")
        self.db.execute("UPDATE t_data SET data=sqlnew_pack(?) WHERE block=0 AND col=0", (frame,))
        self.assertEqual(self.db.scalar("SELECT a FROM t WHERE rowid=1"), 1)
        self.db.execute("UPDATE t_columns SET maximum=0 WHERE block=0 AND col=0")
        self.assertIn("invalid block", str(self.db.execute("PRAGMA integrity_check")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
