"""Product regressions for inherited SQLite statement-rollback behavior."""
import unittest

from db import Database, Error


class StatementRollbackTests(unittest.TestCase):
    def test_failed_replace_insert_is_atomic(self):
        with Database() as db:
            db.execute('CREATE TABLE t(a INTEGER PRIMARY KEY)')
            db.execute('BEGIN')
            with self.assertRaisesRegex(Error, 'datatype mismatch'):
                db.execute("INSERT OR REPLACE INTO t VALUES(1),('a')")
            db.execute('COMMIT')
            self.assertEqual(db.execute('SELECT a FROM t'), [])

    def test_failed_replace_update_restores_original_rows(self):
        with Database() as db:
            db.execute('CREATE TABLE t(a INTEGER PRIMARY KEY,b)')
            db.execute("INSERT INTO t VALUES(1,'x'),(2,'y')")
            db.execute('BEGIN')
            with self.assertRaisesRegex(Error, 'datatype mismatch'):
                db.execute("UPDATE OR REPLACE t SET a=CASE a WHEN 1 THEN 10 ELSE 'z' END")
            db.execute('COMMIT')
            self.assertEqual(db.execute('SELECT a,b FROM t ORDER BY a'), [(1, 'x'), (2, 'y')])


if __name__ == '__main__':
    unittest.main()
