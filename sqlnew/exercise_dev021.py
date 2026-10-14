"""Direct SQLNEW 0.2.1 workflow checks, without a test framework."""
import hashlib
import argparse
import json
from pathlib import Path
import random
import struct
import time

from db import Database, Error, ROOT, BUILD, SUFFIX


def snapshot(db, table='t'):
    return db.execute('SELECT rowid,x,y,z,value FROM '+table+' ORDER BY rowid')


def exercise():
    folder = BUILD/'dev021'/('contracts-'+str(time.time_ns()))
    folder.mkdir(parents=True)
    path = folder/'regions.sqlnew'
    checks = []
    with Database(path) as a:
        assert a.scalar('SELECT sqlnew_version()') == '0.2.1-dev'
        assert json.loads(a.scalar('SELECT sqlnew_capabilities()'))['region'] == 1
        a.execute('PRAGMA journal_mode=WAL')
        a.execute('PRAGMA synchronous=FULL')
        a.execute('CREATE VIRTUAL TABLE t USING sqlnew_region(17,25,33,INTEGER)')
        rng = random.Random(6101428)
        rows = [(x,y,z,rng.randrange(-(1<<63),1<<63))
                for z in range(0,33,3) for y in range(0,25,2) for x in range(0,17,2)]
        rows += [(1,1,1,None),(3,3,3,0)]
        with a.transaction():
            a.executemany('INSERT INTO t(x,y,z,value) VALUES(?,?,?,?)', rows)
        original = snapshot(a)
        expected_input = sorted((x+17*(y+25*z),x,y,z,v) for x,y,z,v in rows)
        assert original == expected_input
        a.execute("INSERT INTO t(__sqlnew_command) VALUES('compact')")
        assert snapshot(a) == original
        checks.append('ragged sparse nullable int64 and compaction')
        bases = a.execute('SELECT id,data FROM t_bases ORDER BY id')
        target = original[len(original)//2][0]
        a.execute('UPDATE t SET value=42 WHERE rowid=?',(target,))
        assert a.execute('SELECT id,data FROM t_bases ORDER BY id') == bases
        assert a.scalar('SELECT value FROM t WHERE rowid=?',(target,)) == 42
        assert a.scalar('SELECT count(*) FROM t_patch') == 1
        a.execute('UPDATE t SET value=? WHERE rowid=?',(original[len(original)//2][-1],target))
        assert a.scalar('SELECT count(*) FROM t_patch') == 0
        checks.append('private patch and exact restoration without base rewrite')
        a.execute('SAVEPOINT outer_edit')
        a.execute('UPDATE t SET value=42 WHERE x=0')
        a.execute('SAVEPOINT inner_edit')
        a.execute('DELETE FROM t WHERE y=0')
        a.execute("INSERT INTO t(__sqlnew_command) VALUES('compact')")
        a.execute('ROLLBACK TO inner_edit')
        a.execute('RELEASE inner_edit')
        a.execute('ROLLBACK TO outer_edit')
        a.execute('RELEASE outer_edit')
        assert snapshot(a) == original
        assert a.execute('SELECT id,data FROM t_bases ORDER BY id') == bases
        checks.append('nested savepoints and rollback of compaction')
        # An old reader continues to see its snapshot during edit+compaction.
        with Database(path) as b:
            b.execute('BEGIN')
            assert snapshot(b) == original
            a.execute('UPDATE t SET value=27 WHERE rowid=?',(target,))
            a.execute("INSERT INTO t(__sqlnew_command) VALUES('compact')")
            assert snapshot(b) == original
            b.execute('COMMIT')
            assert b.scalar('SELECT value FROM t WHERE rowid=?',(target,)) == 27
        checks.append('WAL reader snapshot across published replacement regions')
        reference = snapshot(a)
        a.execute('CREATE TABLE oracle(id INTEGER PRIMARY KEY,x INTEGER,y INTEGER,z INTEGER,value INTEGER)')
        a.executemany('INSERT INTO oracle VALUES(?,?,?,?,?)', reference)
        predicates = ['x=0','y=4','z=9','x BETWEEN 2 AND 10','y>3 AND z<19',
                      'rowid BETWEEN 100 AND 5000','x>=1.5 AND y<=7.5','x<1.0','x=NULL','x=1.5']
        for where in predicates:
            want = a.execute('SELECT id,x,y,z,value FROM oracle WHERE '+where.replace('rowid','id')+' ORDER BY id')
            assert a.execute('SELECT rowid,x,y,z,value FROM t WHERE '+where+' ORDER BY rowid') == want, where
        for query in ['SELECT count(*),min(value),max(value) FROM {}',
                      'SELECT y,count(*),min(value),max(value) FROM {} GROUP BY y ORDER BY y']:
            assert a.execute(query.format('t')) == a.execute(query.format('oracle'))
        checks.append('all axes, ranges, fractional predicates and aggregate SQL semantics')
        try:
            a.execute('INSERT INTO t(x,y,z,value) VALUES(1,1,1,45)')
        except Error:
            pass
        else:
            raise AssertionError('duplicate coordinate accepted')
        count = a.scalar('SELECT count(*) FROM t')
        a.execute('INSERT OR IGNORE INTO t(x,y,z,value) VALUES(1,1,1,45)')
        assert a.scalar('SELECT count(*) FROM t') == count
        a.execute('INSERT OR REPLACE INTO t(x,y,z,value) VALUES(1,1,1,45)')
        assert a.scalar('SELECT value FROM t WHERE x=1 AND y=1 AND z=1') == 45
        a.execute('UPDATE t SET x=2,y=1,z=1 WHERE x=1 AND y=1 AND z=1')
        assert a.scalar('SELECT value FROM t WHERE x=2 AND y=1 AND z=1') == 45
        checks.append('coordinate uniqueness, IGNORE, REPLACE and relocation')
        for kind, values in [('REAL',[None,0.0,-0.0,1e-300,1.2345,1.7976931348623157e308]),
                             ('TEXT',[None,'','page\x00tail','\u0100\u00e9\u96ea','many pages\n'*10000]),
                             ('BLOB',[None,b'',b'\x00\xff'*100,b'large payload\n'*20000])]:
            a.execute('CREATE VIRTUAL TABLE typed USING sqlnew_region(8,8,8,'+kind+')')
            a.executemany('INSERT INTO typed(x,y,z,value) VALUES(?,0,0,?)',enumerate(values))
            a.execute("INSERT INTO typed(__sqlnew_command) VALUES('compact')")
            got = [r[0] for r in a.execute('SELECT value FROM typed ORDER BY rowid')]
            if kind == 'REAL':
                assert [None if v is None else struct.pack('<d',v) for v in got] == [None if v is None else struct.pack('<d',v) for v in values]
            else:
                assert got == values
            a.execute('UPDATE typed SET value=NULL WHERE rowid=1')
            a.execute('UPDATE typed SET value=? WHERE rowid=1',(values[1],))
            assert a.scalar('SELECT count(*) FROM typed_patch') == 0
            a.execute('ALTER TABLE typed RENAME TO renamed')
            assert a.scalar('SELECT count(*) FROM renamed') == len(values)
            a.execute('CREATE VIRTUAL TABLE typed USING sqlnew_region(8,8,8,'+kind+')')
            a.execute('DROP TABLE typed')
            a.execute("INSERT INTO renamed(__sqlnew_command) VALUES('compact')")
            a.execute('DROP TABLE renamed')
        checks.append('REAL bit recovery and individually compressed TEXT/BLOB, rename/drop')
        extra=folder/'attached.sqlnew'
        a.execute('ATTACH DATABASE ? AS extra',(str(extra),))
        a.execute('CREATE VIRTUAL TABLE extra.t USING sqlnew_region(8,8,8,INTEGER)')
        a.execute('INSERT INTO extra.t(x,y,z,value) VALUES(0,0,0,77)')
        assert a.scalar('SELECT value FROM extra.t WHERE rowid=0') == 77
        assert a.scalar("SELECT json_extract(sqlnew_region_stats('extra.t'),'$.rows_emitted')") >= 1
        assert a.execute('SELECT a.value,b.value FROM t a JOIN extra.t b ON a.rowid=b.rowid') == [(reference[0][-1],77)]
        checks.append('attached database joins and schema-qualified counters')
        assert a.execute('PRAGMA integrity_check') == [('ok',)]
        expected = snapshot(a)
        a.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    with Database(path) as db:
        assert snapshot(db) == expected
        assert db.execute('PRAGMA integrity_check') == [('ok',)]
    checks.append('checkpoint and reopen')
    # Fold contracts and existing columnar modes remain directly exercised.
    with Database() as db:
        for enabled in (0,1):
            db.execute('SELECT sqlnew_columnar_folding(?)',(enabled,))
            db.execute('CREATE VIRTUAL TABLE c USING sqlnew_columnar(x INTEGER,s TEXT)')
            data = [(i,1700000000000+3*i*i,'value '+str(i)) for i in range(4097)]
            db.executemany('INSERT INTO c(rowid,x,s) VALUES(?,?,?)', data)
            assert db.execute('SELECT rowid,x,s FROM c ORDER BY rowid') == data
            db.execute('UPDATE c SET x=42 WHERE rowid=1024')
            assert db.scalar('SELECT x FROM c WHERE rowid=1024') == 42
            db.execute("INSERT INTO c(__sqlnew_command) VALUES('compact')")
            db.execute('DROP TABLE c')
        from fold_contracts import canonical
        folds = canonical(db)
        runtime = {'version':db.scalar('SELECT sqlnew_version()'),
                   'sqlite_source_id':db.scalar('SELECT sqlite_source_id()'),
                   'backend':db.scalar('SELECT sqlnew_backend()'),
                   'library_sha256':hashlib.sha256((BUILD/'sqlnew'/('sqlnewdb'+SUFFIX)).read_bytes()).hexdigest()}
    checks.append('existing columnar folding modes and canonical fold merges')
    result = {'checks': checks, 'canonical_fold_checks': folds, 'exact': True,
              'runtime': runtime,
              'database': str(path), 'source_sha256': hashlib.sha256((ROOT/'ext/sqlnew/region.c').read_bytes()).hexdigest()}
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--output', type=Path, default=BUILD/'dev021/contracts.json')
    args = parser.parse_args()
    result = exercise()
    out = args.output
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2),flush=True)
