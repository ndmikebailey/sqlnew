"""Contracts for patches, native batch execution and transactional covering indexes."""
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

from benchmark_compare import check_rows
from db import Database, Error


class ColumnarV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "v2.sqlnew"
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def stats(self):
        return json.loads(self.db.scalar("SELECT sqlnew_columnar_stats('t')"))

    def command(self, command, table="t"):
        text = command if isinstance(command, str) else json.dumps(command)
        self.db.execute(f"INSERT INTO {table}(__sqlnew_command) VALUES(?)", (text,))

    def data(self, n=3200):
        self.db.execute("CREATE VIRTUAL TABLE t USING sqlnew_columnar(k INTEGER,v INTEGER,r REAL,s TEXT,b BLOB)")
        self.db.execute("CREATE TABLE reference(k INTEGER,v INTEGER,r REAL,s TEXT,b BLOB)")
        rng = random.Random(2941)
        rows = [(None if i % 19 == 0 else i % 13, None if i % 11 == 0 else rng.randrange(-1000, 1000),
                 None if i % 23 == 0 else rng.randrange(-10000, 10000) / 10.0,
                 None if i % 7 == 0 else ('key\0' + str(i % 9)),
                 None if i % 17 == 0 else bytes([i % 5, 0, 255])) for i in range(n)]
        for table in ("t", "reference"):
            self.db.execute("BEGIN")
            self.db.executemany(f"INSERT INTO {table} VALUES(?,?,?,?,?)", rows)
            self.db.execute("COMMIT")
        return rows

    def same(self, query, args=()):
        actual = self.db.execute(query.format(table="t"), args)
        expected = self.db.execute(query.format(table="reference"), args)
        check_rows(actual, expected)
        return actual

    def mutate(self, query, args=()):
        for table in ("t", "reference"):
            self.db.execute(query.format(table=table), args)

    def test_sparse_updates_restore_and_dense_compaction(self):
        self.data()
        frames = self.db.execute("SELECT col,data FROM t_data WHERE block=0 ORDER BY col")
        before = self.stats()
        self.mutate("UPDATE {table} SET r=999.0 WHERE rowid=2")
        self.assertEqual(self.stats()["columns_written"], before["columns_written"])
        self.assertEqual(self.db.scalar("SELECT count(*) FROM t_patch"), 1)
        self.assertEqual(self.db.execute("SELECT col,data FROM t_data WHERE block=0 ORDER BY col"), frames)
        self.same("SELECT count(r),min(r),max(r) FROM {table}")
        self.same("SELECT rowid,r FROM {table} WHERE r>=998.0 ORDER BY rowid")
        self.mutate("UPDATE {table} SET v=NULL,s='changed' WHERE rowid IN (3,1050,2090)")
        self.same("SELECT count(v),sum(v),min(s),max(s) FROM {table}")
        self.assertEqual(self.db.scalar("PRAGMA integrity_check"), "ok")
        self.command("compact")
        self.assertEqual(self.db.scalar("SELECT count(*) FROM t_patch"), 0)
        self.same("SELECT * FROM {table} ORDER BY rowid")
        before = self.stats()
        self.mutate("UPDATE {table} SET r=3.5 WHERE rowid BETWEEN 10 AND 210")
        after = self.stats()
        self.assertGreater(after["compactions"], before["compactions"])
        self.assertEqual(after["columns_written"] - before["columns_written"], after["compactions"] - before["compactions"])
        self.same("SELECT * FROM {table} ORDER BY rowid")
        self.command("compact")
        self.assertEqual(self.db.scalar("PRAGMA integrity_check"), "ok")

    def test_exact_restoration_removes_patch(self):
        rows = self.data(1200)
        self.db.execute("UPDATE t SET r=6.0 WHERE rowid=2")
        self.db.execute("UPDATE t SET r=? WHERE rowid=2", (rows[1][2],))
        self.assertEqual(self.db.scalar("SELECT count(*) FROM t_patch"), 0)
        self.same("SELECT * FROM {table} ORDER BY rowid")

    def test_batch_grouping_order_limit_nulls_and_affinities(self):
        self.data()
        queries = [
            "SELECT k,count(*),sum(v),avg(r),min(s),max(b) FROM {table} GROUP BY k ORDER BY k",
            "SELECT k,s,count(v),sum(r),avg(v) FROM {table} GROUP BY k,s ORDER BY k DESC,s DESC LIMIT 23 OFFSET 3",
            "SELECT s,sum(v),max(r)-min(r) AS spread FROM {table} GROUP BY s ORDER BY spread DESC,s",
            "SELECT k,sum(v),avg(r) FROM {table} WHERE v>=-400 AND v<200 GROUP BY k ORDER BY sum(v),k",
            "SELECT k,s FROM {table} GROUP BY k,s ORDER BY k,s",
            "SELECT count(*),sum(r),avg(v),min(s) FROM {table} WHERE k=99",
            "SELECT count(__sqlnew_command) FROM t",
        ]
        for query in queries[:-1]:
            with self.subTest(query=query):
                self.same(query)
        self.assertEqual(self.db.scalar(queries[-1]), 0)
        self.assertGreater(self.stats()["batch_rows"], 0)
        for bound in (4, 4.0, 4.5, '4', 'x', None, b'4'):
            self.same("SELECT count(*),sum(v),avg(r) FROM {table} WHERE k>=?", (bound,))
        self.assertGreater(self.stats()["batch_fallbacks"], 0)
        self.same("SELECT count(*),sum(v) FROM {table} WHERE rowid>=? AND rowid<?", (1010, 1030))
        self.same("SELECT s,count(*) FROM {table} GROUP BY s COLLATE NOCASE ORDER BY s COLLATE NOCASE")

    def test_batch_real_zero_and_integer_overflow(self):
        self.db.execute("CREATE VIRTUAL TABLE t USING sqlnew_columnar(k INTEGER,v INTEGER,r REAL)")
        self.db.execute("INSERT INTO t VALUES(1,1,1.0),(1,-1,-1.0),(2,NULL,NULL)")
        self.assertEqual(self.db.execute("SELECT k,typeof(sum(r)),sum(r),avg(v) FROM t GROUP BY k ORDER BY k"),
                         [(1, 'real', 0.0, 0.0), (2, 'null', None, None)])
        self.db.execute("DELETE FROM t")
        self.db.execute("INSERT INTO t VALUES(1,?,0.0),(1,1,0.0),(1,-1,0.0)", ((1 << 63) - 1,))
        with self.assertRaises(Error):
            self.db.execute("SELECT k,sum(v) FROM t GROUP BY k")
        self.assertIsInstance(self.db.scalar("SELECT avg(v) FROM t WHERE k=1"), float)

    def test_bounded_hash_falls_back_without_losing_rows(self):
        self.db.execute("CREATE VIRTUAL TABLE t USING sqlnew_columnar(k INTEGER,v INTEGER)")
        self.db.execute("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<18000) INSERT INTO t SELECT x,x%19 FROM n")
        rows = self.db.execute("SELECT k,sum(v) FROM t GROUP BY k ORDER BY k DESC LIMIT 30 OFFSET 5")
        self.assertEqual(rows, [(i, i % 19) for i in range(17995, 17965, -1)])
        self.assertGreater(self.stats()["batch_fallbacks"], 0)

    def test_covering_indexes_maintain_all_mutations(self):
        self.data()
        self.command({'create_index': 'keys', 'keys': ['k', 's'], 'include': ['v', 'r']})
        self.command({'create_index': 'bytes', 'keys': ['b'], 'include': ['v']})
        query = "SELECT k,s,v,r FROM {table} WHERE k>=3 AND k<6 ORDER BY k,s,rowid"
        self.same(query)
        self.assertIn('INDEX 2:', str(self.db.execute('EXPLAIN QUERY PLAN ' + query.format(table='t'))))
        before = self.stats()
        self.same("SELECT count(*),sum(r) FROM {table} WHERE k>=3 AND k<6")
        self.assertGreater(self.stats()["index_rows_read"], before["index_rows_read"])
        for sql in (
            "UPDATE {table} SET k=3,s=NULL,r=23.5 WHERE rowid IN (7,1029,2095)",
            "UPDATE {table} SET v=v+5 WHERE rowid IN (11,1000,2017)",
            "UPDATE {table} SET rowid=-4000 WHERE rowid=15",
            "DELETE FROM {table} WHERE rowid IN (3,8,18)",
            "INSERT OR REPLACE INTO {table}(rowid,k,v,r,s,b) VALUES(2,7,42,2.0,'replaced',x'00')",
            "INSERT INTO {table} VALUES(9,12,8.0,'inserted',x'00')",
        ):
            self.mutate(sql)
            self.same(query)
            self.same("SELECT b,v FROM {table} WHERE b>=x'00' ORDER BY b,rowid")
            self.assertEqual(self.db.scalar("PRAGMA integrity_check"), "ok")
        for bound in (4, 4.5, '4', 'x', None):
            self.same("SELECT k,v,r FROM {table} WHERE k>=? ORDER BY k,rowid", (bound,))
        self.command({'drop_index': 'keys'})
        self.same(query)
        self.assertEqual(self.db.scalar("PRAGMA integrity_check"), "ok")

    def test_index_snapshot_savepoint_compaction_rename_and_drop(self):
        self.db.execute("PRAGMA journal_mode=WAL")
        self.data(1200)
        self.command({'create_index': 'range', 'keys': ['v'], 'include': ['r']})
        with Database(self.path) as reader:
            reader.execute('BEGIN')
            sql = 'SELECT count(*),sum(r) FROM t WHERE v>=0'
            old = reader.execute(sql)
            self.db.execute('BEGIN')
            self.db.execute('SAVEPOINT before_updates')
            self.db.execute('UPDATE t SET v=12345,r=99.0 WHERE rowid<300')
            self.command('compact')
            self.assertEqual(reader.execute(sql), old)
            self.db.execute('ROLLBACK TO before_updates')
            self.assertEqual(self.db.execute(sql), old)
            self.db.execute('RELEASE before_updates')
            self.db.execute('COMMIT')
            reader.execute('COMMIT')
        self.db.execute('BEGIN')
        self.db.execute('SAVEPOINT s')
        self.command({'drop_index': 'range'})
        self.db.execute('ROLLBACK TO s')
        self.assertEqual(self.db.scalar('PRAGMA integrity_check'), 'ok')
        self.db.execute('RELEASE s')
        self.db.execute('COMMIT')
        self.db.execute('ALTER TABLE t RENAME TO renamed')
        self.db.execute('UPDATE renamed SET r=2.0 WHERE rowid=10')
        self.assertEqual(self.db.scalar('PRAGMA integrity_check'), 'ok')
        self.db.execute('BEGIN')
        self.db.execute('SAVEPOINT d')
        self.db.execute('DROP TABLE renamed')
        self.db.execute('ROLLBACK TO d')
        self.assertEqual(self.db.scalar('PRAGMA integrity_check'), 'ok')
        self.db.execute('ROLLBACK')

    def test_invalid_index_commands_are_atomic_and_corruption_detected(self):
        self.data(1100)
        for command in ({'create_index': 'bad', 'keys': ['missing']},
                        {'create_index': 'bad', 'keys': ['k', 'k']},
                        {'create_index': 'bad', 'keys': []}):
            with self.assertRaises(Error):
                self.command(command)
            self.assertEqual(self.db.scalar('SELECT count(*) FROM t_indexes'), 0)
        self.command({'create_index': 'good', 'keys': ['k'], 'include': ['v']})
        with self.assertRaises(Error):
            self.command({'create_index': 'good', 'keys': ['v']})
        self.assertEqual(self.db.scalar('SELECT count(*) FROM t_indexes'), 1)
        self.assertEqual(self.db.scalar('PRAGMA integrity_check'), 'ok')
        self.db.execute('UPDATE t_index_1 SET v1=99999 WHERE rid=1')
        self.assertIn('invalid block', str(self.db.execute('PRAGMA integrity_check')))

    def test_index_direction_and_prepared_statement_after_removal(self):
        self.data(1300)
        definition = {'create_index': 'mixed', 'keys': ['k', {'column': 'r', 'desc': True}], 'include': ['v']}
        self.command(definition)
        query = 'SELECT k,r,v FROM {table} ORDER BY k,r DESC,rowid'
        self.same(query)
        self.same('SELECT k,r,v FROM {table} ORDER BY k DESC,r,rowid DESC')
        self.same('SELECT k,r,v FROM {table} ORDER BY k NULLS LAST,r DESC,rowid')
        statement = self.db.prepare(query.format(table='t'))
        try:
            self.command({'drop_index': 'mixed'})
            check_rows(list(self.db.rows(statement)), self.db.execute(query.format(table='reference')))
        finally:
            self.db.lib.sqlite3_finalize(statement)
        self.command(definition)
        self.assertEqual(self.db.scalar('SELECT count(*) FROM t_indexes'), 1)  # Reuses the empty root.
        self.same(query)
        self.assertEqual(self.db.scalar('PRAGMA integrity_check'), 'ok')

    def test_indexed_batch_excludes_nulls_from_upper_ranges(self):
        self.data(1300)
        self.command({'create_index': 'nullable', 'keys': ['k'], 'include': ['r']})
        for op in ('<', '<=', '=', '>=', '>'):
            for bound in (0, 6, 13, None, '6', 6.5):
                self.same(f'SELECT count(*),sum(r),avg(r) FROM {{table}} WHERE k{op}?', (bound,))
                self.same(f'SELECT k,count(*),sum(r) FROM {{table}} WHERE k{op}? GROUP BY k', (bound,))

    def test_covered_scalar_aggregates_preserve_types_and_empty_results(self):
        self.data(1300)
        self.command({'create_index': 'all_values', 'keys': ['k'], 'include': ['v', 'r', 's', 'b']})
        for bound in (-1, 0, 8, 99, None, 8.5):
            self.same('SELECT count(*),count(v),sum(v),avg(v),sum(r),avg(r),min(s),max(b) '
                      'FROM {table} WHERE k<=?', (bound,))
        before = self.stats()
        answer = self.same('SELECT count(*),sum(r) FROM {table} WHERE k<=8')
        after = self.stats()
        self.assertEqual(after['rows_emitted'] - before['rows_emitted'], 0)
        self.assertEqual(after['columns_decoded'] - before['columns_decoded'], 0)
        self.assertEqual(after['index_rows_read'] - before['index_rows_read'], answer[0][0])
        self.mutate('INSERT INTO {table}(k,v) VALUES(999,9223372036854775807),(999,1)')
        for table in ('t', 'reference'):
            with self.assertRaisesRegex(Error, 'integer overflow'):
                self.db.execute(f'SELECT sum(v) FROM {table} WHERE k=999')

    def test_filter_skips_payload_in_nonmatching_middle_group(self):
        self.db.execute('CREATE VIRTUAL TABLE t USING sqlnew_columnar(k INTEGER,v INTEGER)')
        self.db.execute('BEGIN')
        self.db.executemany('INSERT INTO t(rowid,k,v) VALUES(?,?,?)',
                            [(i, (100 if i % 2 else 0) if 1024 <= i < 2048 else 50, i)
                             for i in range(3072)])
        self.db.execute('COMMIT')
        before = self.stats()
        self.assertEqual(self.db.execute('SELECT count(*),sum(v) FROM t WHERE k>=40 AND k<=60'),
                         [(2048, sum(range(1024)) + sum(range(2048, 3072)))])
        after = self.stats()
        self.assertEqual(after['columns_decoded'] - before['columns_decoded'], 5)

    def test_committed_patches_and_index_survive_process_exit(self):
        self.db.close()
        child = r'''
import os,sys,json
from db import Database
d=Database(sys.argv[1])
d.execute('PRAGMA journal_mode=WAL')
d.execute('CREATE VIRTUAL TABLE t USING sqlnew_columnar(k INTEGER,r REAL)')
d.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<2200) INSERT INTO t SELECT x,x*1.5 FROM n')
d.execute('INSERT INTO t(__sqlnew_command) VALUES(?)',(json.dumps({'create_index':'range','keys':['k'],'include':['r']}),))
d.execute('UPDATE t SET k=9999,r=7.5 WHERE rowid=2')
d.execute('BEGIN')
d.execute('UPDATE t SET k=-k,r=1.0 WHERE rowid<1000')
d.execute("INSERT INTO t(__sqlnew_command) VALUES('compact')")
os._exit(27)
'''
        path = Path(self.temp.name) / 'crash.sqlnew'
        result = subprocess.run([sys.executable, '-c', child, str(path)], cwd=Path(__file__).parent,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 27, result.stderr)
        with Database(path) as recovered:
            self.assertEqual(recovered.execute('SELECT count(*),sum(r) FROM t WHERE k>=9999'), [(1, 7.5)])
            self.assertEqual(recovered.scalar('SELECT count(*) FROM t'), 2200)
            self.assertEqual(recovered.scalar('PRAGMA integrity_check'), 'ok')
        self.db = Database(self.path)


if __name__ == '__main__':
    unittest.main(verbosity=2)
