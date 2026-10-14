"""Compare Dev 0.2.1 native region retrieval with retained native baselines."""
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path
import platform
import statistics
import struct
import time

import numpy as np

from db import Database, ROOT, BUILD, SUFFIX

ARMS = {
    'v021_columnar': (BUILD/'sqlnew'/('sqlnewdb'+SUFFIX),'columnar'),
    'v021_columnar_fold': (BUILD/'sqlnew'/('sqlnewdb'+SUFFIX),'fold'),
    'v021_region': (BUILD/'sqlnew'/('sqlnewdb'+SUFFIX),'region'),
    'sqlite_indexed': (BUILD/'comparison'/('sqlite-baseline'+SUFFIX),'row'),
}


def datasets(text_source=None):
    rng = np.random.default_rng(6101430)
    z,y,x = np.indices((64,64,64),dtype=np.int64)
    motif = rng.integers(0,1<<64,size=(8,8,8),dtype=np.uint64)
    anchors = rng.integers(0,1<<64,size=(8,8,8),dtype=np.uint64)
    values = (motif[z%8,y%8,x%8]+anchors[z//8,y//8,x//8]).view('<i8')
    yield 'translated_motif','INTEGER',(64,64,64),values.ravel().tolist()
    i = np.arange(z.size,dtype=np.int64)
    yield 'quadratic_clock','INTEGER',(64,64,64),(1700000000000+1000*i+3*i*i).tolist()
    yield 'independent_bits','INTEGER',(64,64,64),rng.integers(0,1<<64,size=z.shape,dtype=np.uint64).view('<i8').ravel().tolist()
    source = text_source or ROOT/'ext/sqlnew'
    paths = sorted(p for p in source.rglob('*') if p.suffix in ('.py', '.c', '.inc') and p.is_file())
    text = '\n'.join(p.read_text(encoding='utf-8') for p in paths)
    if len(text) <= 8192:
        raise ValueError('text fixture requires more than 8192 characters of source text')
    passages = [f'passage {j}\n'+text[(j*317)%(len(text)-8192):][:8192] for j in range(2048)]
    yield 'source_passages','TEXT',(16,16,8),passages


def counters(db, kind):
    if kind == 'region':
        return json.loads(db.scalar("SELECT sqlnew_region_stats('t')"))
    if kind in ('columnar','fold'):
        return json.loads(db.scalar("SELECT sqlnew_columnar_stats('t')"))
    return {}


def timed(db, sql, parameters, reps=13):
    statement = db.prepare(sql)
    samples = []
    answer = None
    try:
        for i in range(reps+2):
            started = time.perf_counter_ns()
            rows = []
            for p in parameters:
                db.bind(statement,p)
                rows.extend(db.rows(statement))
                db.check(db.lib.sqlite3_reset(statement))
                db.check(db.lib.sqlite3_clear_bindings(statement))
            elapsed = (time.perf_counter_ns()-started)/1e6
            if answer is None:
                answer = rows
            assert rows == answer
            if i>=2:
                samples.append(elapsed)
    finally:
        db.lib.sqlite3_finalize(statement)
    ordered = sorted(samples)
    return {'median_ms':statistics.median(samples),'p95_ms':ordered[int((len(ordered)-1)*.95)],
            'samples_ms':samples,'result_sha256':hashlib.sha256(repr(answer).encode()).hexdigest()}, answer


def run_arm(folder,name,kind,dims,values,arm,library,storage):
    nx,ny,nz = dims
    path = folder/(name+'-'+arm+('.sqlite' if storage=='row' else '.sqlnew'))
    with Database(path,library=library) as db:
        for p in ('page_size=4096','journal_mode=WAL','synchronous=FULL','wal_autocheckpoint=0',
                  'cache_size=-65536','mmap_size=0','temp_store=MEMORY','threads=1'):
            db.execute('PRAGMA '+p)
        db.execute('CREATE TABLE source(x INTEGER,y INTEGER,z INTEGER,value '+kind+')')
        with db.transaction():
            db.executemany('INSERT INTO source(rowid,x,y,z,value) VALUES(?,?,?,?,?)',
                          ((j,j%nx,(j//nx)%ny,j//(nx*ny),v) for j,v in enumerate(values)))
        if storage == 'region':
            db.execute(f'CREATE VIRTUAL TABLE t USING sqlnew_region({nx},{ny},{nz},{kind})')
        elif storage == 'row':
            db.execute('CREATE TABLE t(x INTEGER,y INTEGER,z INTEGER,value '+kind+')')
        else:
            if storage=='fold':
                db.execute('SELECT sqlnew_columnar_folding(1)')
            db.execute('CREATE VIRTUAL TABLE t USING sqlnew_columnar(x INTEGER,y INTEGER,z INTEGER,value '+kind+')')
        started = time.perf_counter()
        db.execute('INSERT INTO t(rowid,x,y,z,value) SELECT rowid,x,y,z,value FROM source')
        ingest = time.perf_counter()-started
        seal = 0.0
        if storage=='region':
            started=time.perf_counter()
            db.execute("INSERT INTO t(__sqlnew_command) VALUES('compact')")
            seal=time.perf_counter()-started
        index = 0.0
        if storage=='row':
            started=time.perf_counter()
            for axis in ('x','y','z'):
                db.execute('CREATE INDEX t_'+axis+' ON t('+axis+')')
            index=time.perf_counter()-started
        got = [r[0] for r in db.execute('SELECT value FROM t ORDER BY rowid')]
        assert got == values, (name,arm,'full source')
        db.execute('DROP TABLE source')
        # Free fixture pages are reclaimed before complete database sizing.
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        db.execute('VACUUM')
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        # VACUUM reconnects virtual tables lazily; establish the stats owner.
        assert db.scalar('SELECT count(*) FROM t') == len(values)
        if storage=='region':
            payload = db.scalar('SELECT coalesce(sum(length(data)),0) FROM t_bases')+db.scalar('SELECT coalesce(sum(length(data)),0) FROM t_values')
            base_count = db.scalar('SELECT count(*) FROM t_bases')
        elif storage=='row':
            payload,base_count = None,None
        else:
            payload,base_count = db.scalar('SELECT sum(length(data)) FROM t_data'),None
        size = path.stat().st_size
        rng=np.random.default_rng(6101431)
        ids=rng.choice(len(values),size=min(64,len(values)),replace=False).tolist()
        selected = [j for j in range(len(values)) if j//(nx*ny)==nz//2]
        queries = {}
        cases = [('point_64','SELECT value FROM t WHERE rowid=?',[(j,) for j in ids]),
                 ('plane_z','SELECT rowid,value FROM t WHERE z=? ORDER BY rowid',[(nz//2,)]),
                 ('plane_x','SELECT rowid,value FROM t WHERE x=? ORDER BY rowid',[(nx//2,)]),
                 ('summary','SELECT count(*),min(value),max(value) FROM t',[()])]
        expected = {
            'point_64':[(values[j],) for j in ids],
            'plane_z':[(j,values[j]) for j in selected],
            'plane_x':[(j,values[j]) for j in range(nx//2,len(values),nx)],
            'summary':[(len(values),min(values),max(values))],
        }
        for label,sql,args in cases:
            before=counters(db,storage)
            timing,answer=timed(db,sql,args)
            assert answer == expected[label], (name,arm,label)
            after=counters(db,storage)
            timing['work_delta']={k:after[k]-before.get(k,0) for k in after if isinstance(after[k],int)}
            queries[label]=timing
        mutated=list(values)
        changed={j:(int(v)^0x15555 if kind=='INTEGER' else v+'\nedited') for j,v in ((j,values[j]) for j in ids)}
        saved_bases = db.execute('SELECT id,data FROM t_bases ORDER BY id') if storage=='region' else None
        started=time.perf_counter()
        with db.transaction():
            db.executemany('UPDATE t SET value=? WHERE rowid=?',((v,j) for j,v in changed.items()))
        update=time.perf_counter()-started
        if storage=='region':
            assert db.execute('SELECT id,data FROM t_bases ORDER BY id')==saved_bases
        post,_=timed(db,'SELECT value FROM t WHERE rowid=?',[(j,) for j in ids])
        assert [r[0] for r in db.execute('SELECT value FROM t WHERE rowid IN('+','.join('?' for _ in ids)+') ORDER BY rowid',sorted(ids))] == [changed[j] for j in sorted(ids)]
        with db.transaction():
            db.executemany('UPDATE t SET value=? WHERE rowid=?',((values[j],j) for j in ids))
        if storage=='region':
            assert db.scalar('SELECT count(*) FROM t_patch')==0
            db.execute("INSERT INTO t(__sqlnew_command) VALUES('compact')")
        assert [r[0] for r in db.execute('SELECT value FROM t ORDER BY rowid')]==values
        assert db.execute('PRAGMA integrity_check')==[('ok',)]
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        result = {'database_bytes':size,'compressed_payload_bytes':payload,'distinct_base_payloads':base_count,
                  'ingest_seconds':ingest,'seal_seconds':seal,'index_seconds':index,
                  'ready_seconds':ingest+seal+index,'queries':queries,
                  'update_64_commit_seconds':update,'edited_point_64':post,
                  'exact':True,'library_sha256':hashlib.sha256(library.read_bytes()).hexdigest(),
                  'product_version':db.scalar('SELECT sqlnew_version()') if storage!='row' else None,
                  'database':str(path),'sqlite_source_id':db.scalar('SELECT sqlite_source_id()')}
    with Database(path,library=library) as db:
        assert db.scalar('SELECT count(*) FROM t')==len(values)
    print(name,arm,'bytes',size,'ready_s',round(result['ready_seconds'],3),
          'points_ms',round(queries['point_64']['median_ms'],3),flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--previous-library', type=Path,
                        help='optional retained older SQLNEW library; adds a fifth arm')
    parser.add_argument('--text-source', type=Path,
                        help='source directory for constructed text passages; defaults to ext/sqlnew')
    parser.add_argument('--output', type=Path, default=BUILD/'dev021/benchmark.json')
    args = parser.parse_args()
    arms = dict(ARMS)
    if args.previous_library:
        arms['previous_columnar'] = (args.previous_library.resolve(), 'columnar')
    for name, (library, _) in arms.items():
        if not library.is_file():
            parser.error(f'{name}: missing {library}; build SQLNEW and run sqlnew/benchmark_build.py first')
    text_source = (args.text_source or ROOT/'ext/sqlnew').resolve()
    folder=BUILD/'dev021'/('benchmark-'+str(time.time_ns()))
    folder.mkdir(parents=True)
    report={'recorded_utc':datetime.now(timezone.utc).isoformat(),'platform':platform.platform(),
            'machine':platform.machine(),'processor':platform.processor(),
            'python_version':platform.python_version(),'numpy_version':np.__version__,
            'version':json.loads((BUILD/'build.json').read_text())['version'],'measurements':[],
            'text_fixture':{'description':'Overlapping source windows with unique labels; not a representative document corpus.',
                            'files':{str(p.relative_to(text_source)):hashlib.sha256(p.read_bytes()).hexdigest()
                                     for p in sorted(text_source.rglob('*'))
                                     if p.is_file() and p.suffix in ('.py','.c','.inc')}},
            'protocol':'Fresh file-backed databases; common 4 KiB pages, WAL FULL, 64 MiB cache, mmap off. Input fixture preparation excluded. Ingest/seal/index costs included separately. Fixture table removed and VACUUM/checkpoint before size. Warm prepared SQL calls include ctypes result copying; two warmups, 13 retained samples. Updates include one FULL commit. Indexed SQLite has three coordinate indexes. Single host, sequential arms; no cold-cache claim.'}
    for index,(name,kind,dims,values) in enumerate(datasets(text_source)):
        row={'fixture':name,'type':kind,'dimensions_xyz':dims,'rows':len(values),
             'logical_value_bytes':sum(len(v.encode()) for v in values) if kind=='TEXT' else len(values)*8,
             'source_sha256':hashlib.sha256(repr(values).encode()).hexdigest(),'arms':{}}
        ordered_arms=list(arms.items())
        if index%2:
            ordered_arms.reverse()
        for arm,(library,storage) in ordered_arms:
            row['arms'][arm]=run_arm(folder,name,kind,dims,values,arm,library,storage)
        report['measurements'].append(row)
        (folder/'partial-results.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    report['build']=json.loads((BUILD/'build.json').read_text())
    report['baseline_build']=json.loads((BUILD/'comparison/build.json').read_text())
    report['script_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    out=args.output
    out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print('saved',out,flush=True)


if __name__=='__main__':
    main()
