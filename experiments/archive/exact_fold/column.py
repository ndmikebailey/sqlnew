"""Compare actual column storage and SQL operations against the saved engine."""
import ctypes as C
import hashlib
import json
from pathlib import Path
import statistics
import struct
import sys
import time

import numpy as np

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'sqlnew'))
from db import Database
from native import canonical, timed

OUT=ROOT/'build/math-exploration'
LIBRARIES={'before':OUT/'baseline/sqlnewdb.dll','after':ROOT/'build/sqlnew/sqlnewdb.dll'}


def datasets(n):
    i=np.arange(n,dtype=np.int64);rng=np.random.default_rng(103128)
    yield 'quadratic','INTEGER',(1700000000000+11*i+3*i*i).tolist()
    yield 'jittered_clock','INTEGER',(1700000000000+1000*i+rng.integers(-3,4,n)).tolist()
    yield 'float_clock','REAL',(1700000000.0+i*.001).tolist()
    yield 'float_curve','REAL',(np.sin(i*.007)+i*.0001).tolist()
    yield 'decimal','REAL',((123456+rng.integers(-250,251,n))/100).tolist()
    yield 'random_integer','INTEGER',np.frombuffer(rng.bytes(n*8),dtype='<i8').tolist()


def exact(values,rows,kind):
    if len(values)!=len(rows):raise RuntimeError('row count')
    fmt='<q' if kind=='INTEGER' else '<d'
    for i,(want,row) in enumerate(zip(values,rows)):
        got=row[0]
        if want is None or got is None:
            if want is not got:raise RuntimeError('NULL mismatch at '+str(i))
        elif struct.pack(fmt,want)!=struct.pack(fmt,got):
            raise RuntimeError('value bits mismatch at '+str(i))


def exercise(name,kind,values,label):
    with Database(library=LIBRARIES[label]) as db:
        if label=='after':db.execute('SELECT sqlnew_columnar_folding(1)')
        db.execute('CREATE TABLE source(x '+kind+')')
        with db.transaction():db.executemany('INSERT INTO source VALUES(?)',((x,) for x in values))
        ingestion=[]
        for iteration in range(5):
            db.execute('CREATE VIRTUAL TABLE t USING sqlnew_columnar(x '+kind+')')
            start=time.perf_counter_ns()
            db.execute('INSERT INTO t(rowid,x) SELECT rowid-1,x FROM source')
            ingestion.append((time.perf_counter_ns()-start)/1e6)
            if iteration<4:db.execute('DROP TABLE t')
        exact(values,db.execute('SELECT x FROM t ORDER BY rowid'),kind)
        # Force value decoding; whole-table MIN/MAX would only read summaries.
        scan=timed(db,'SELECT min(x+0),max(x+0),count(x+0) FROM t',(),17)
        point=timed(db,'SELECT x FROM t WHERE rowid=?',(len(values)//2+19,),31)
        codecs=db.execute('SELECT hex(substr(sqlnew_unpack(data),6,1)),count(*) FROM t_data GROUP BY 1')
        frames=db.execute('SELECT data FROM t_data ORDER BY block,col')
        size=sum(len(x[0]) for x in frames)
        return {'ingest_ms':statistics.median(ingestion[1:]),'ingest_samples_ms':ingestion,
                'scan':scan,'point':point,'column_bytes':size,'codecs':codecs,
                'frame_lengths':[len(x[0]) for x in frames],
                'frame_sha256':hashlib.sha256(b''.join(x[0] for x in frames)).hexdigest(),'exact':True}


def mutations():
    path=OUT/'fold-contracts.sqlnew'
    if path.exists():
        path=OUT/('fold-contracts-'+str(time.time_ns())+'.sqlnew')
    values=[None if i%13==0 else 1000000+3*i*i for i in range(4097)]
    with Database(path,library=LIBRARIES['after']) as db:
        db.execute('SELECT sqlnew_columnar_folding(1)')
        db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=FULL')
        db.execute('CREATE VIRTUAL TABLE t USING sqlnew_columnar(x INTEGER)')
        with db.transaction():db.executemany('INSERT INTO t(rowid,x) VALUES(?,?)',enumerate(values))
        exact(values,db.execute('SELECT x FROM t ORDER BY rowid'),'INTEGER')
        before=db.execute('SELECT block,col,data FROM t_data ORDER BY block,col')
        db.execute('SAVEPOINT s');db.execute('UPDATE t SET x=-x WHERE rowid%101=0')
        db.execute("INSERT INTO t(__sqlnew_command) VALUES('compact')")
        db.execute('ROLLBACK TO s');db.execute('RELEASE s')
        if db.execute('SELECT block,col,data FROM t_data ORDER BY block,col')!=before:
            raise RuntimeError('rollback changed stored bytes')
        db.execute("INSERT INTO t(__sqlnew_command) VALUES(?)",('{"create_index":"x_key","keys":["x"]}',))
        with db.transaction():
            db.execute('UPDATE t SET x=99 WHERE rowid=333');values[333]=99
            db.execute('UPDATE t SET x=NULL WHERE rowid=334');values[334]=None
            db.execute('DELETE FROM t WHERE rowid=335')
            db.execute('INSERT INTO t(rowid,x) VALUES(335,88)');values[335]=88
            db.execute("INSERT INTO t(__sqlnew_command) VALUES('compact')")
        exact(values,db.execute('SELECT x FROM t ORDER BY rowid'),'INTEGER')
        result=db.execute('SELECT count(*),sum(x),min(x),max(x) FROM t')[0]
        live=[x for x in values if x is not None]
        if result!=(len(values),sum(live),min(live),max(live)):raise RuntimeError('summary')
        if db.scalar('SELECT count(*) FROM t WHERE x>=1000000')!=sum(x>=1000000 for x in live):
            raise RuntimeError('indexed range')
    with Database(path,library=LIBRARIES['after']) as db:
        exact(values,db.execute('SELECT x FROM t ORDER BY rowid'),'INTEGER')
    print('nullable values, edits, compaction, rollback bytes, summaries, index, reopen: exact',flush=True)
    return str(path)


def unchanged_default():
    rng=np.random.default_rng(109)
    samples=[b'',b'abcdefgh'*1024,rng.bytes(16384),bytes(range(256))*41]
    outputs=[]
    for label in ('before','after'):
        with Database(library=LIBRARIES[label]) as db:
            if label=='after' and db.scalar('SELECT sqlnew_columnar_folding()')!=0:
                raise RuntimeError('folding default')
            packed=[db.scalar('SELECT sqlnew_pack(?)',(x,)) for x in samples]
            db.execute('CREATE VIRTUAL TABLE t USING sqlnew_columnar(x INTEGER,r REAL,s TEXT,b BLOB)')
            values=[(None if i%7==0 else i*i,1.7e9+i*.001,str(i%31),samples[i%4][:17]) for i in range(3073)]
            with db.transaction():db.executemany('INSERT INTO t VALUES(?,?,?,?)',values)
            frames=db.execute('SELECT data FROM t_data ORDER BY block,col')
            outputs.append((packed,frames,db.execute('SELECT * FROM t ORDER BY rowid')))
    if outputs[0]!=outputs[1]:raise RuntimeError('default output changed')
    print('Default-off engine: byte-identical pack outputs and mixed-type stored frames',flush=True)
    return True


if __name__=='__main__':
    n=int(sys.argv[1]) if len(sys.argv)>1 else 65536
    report={'rows_per_fixture':n,'results':[],'protocol':'Same typed source values in both engines; source setup excluded; in-memory column tables; four retained ingestion samples, 17 scans, 31 points; no output conversion in query timer.'}
    with Database(library=LIBRARIES['after']) as db:report['canonical_checks']=canonical(db)
    report['default_bytes_unchanged']=unchanged_default()
    report['mutation_database']=mutations()
    for index,(name,kind,values) in enumerate(datasets(n)):
        row={'fixture':name,'kind':kind,'engines':{}}
        for label in (('before','after') if index%2==0 else ('after','before')):
            row['engines'][label]=exercise(name,kind,values,label)
        before=row['engines']['before'];after=row['engines']['after']
        if any(new>old for old,new in zip(before['frame_lengths'],after['frame_lengths'])):
            raise RuntimeError(name+' frame size increased')
        if all(mode=='00' for mode,count in after['codecs']) and before['frame_sha256']!=after['frame_sha256']:
            raise RuntimeError(name+' fallback bytes changed')
        report['results'].append(row)
        print(name,{label:{'bytes':r['column_bytes'],'ingest_ms':round(r['ingest_ms'],3),
              'scan_ms':round(r['scan']['median_ms'],3),'point_ms':round(r['point']['median_ms'],3),
              'codecs':r['codecs']} for label,r in row['engines'].items()},flush=True)
        (OUT/'column.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
