"""Measure exact passage retrieval over pinned, non-overlapping public documents."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import random
import re
import statistics
import subprocess
import time
import urllib.request

from db import Database, ROOT, BUILD, SUFFIX

PLANE = 64*16384
SPEC = Path(__file__).with_name('corpora')/'public-documents.json'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def passages(text, size):
    """Keep all characters once, preferring paragraph boundaries to hard cuts."""
    result, pending = [], ''
    for part in re.findall(r'.*?(?:\r?\n[ \t]*\r?\n|$)',text,re.S):
        if not part:
            continue
        if pending and len(pending)+len(part)>size:
            result.append(pending)
            pending = ''
        while len(part)>size:
            result.append(part[:size])
            part = part[size:]
        pending += part
    if pending:
        result.append(pending)
    assert ''.join(result)==text
    return result


def corpus(fetch=False, size=4096):
    spec = json.loads(SPEC.read_text(encoding='utf-8'))
    cache = BUILD/'document-corpus'
    documents, rows = [], []
    for doc, source in enumerate(spec['sources']):
        cached = cache/source.get('cache_file',Path(source.get('path','')).name)
        if cached.exists():
            path = cached
        elif 'path' in source:
            path = ROOT/source['path']
        else:
            path = cache/source['cache_file']
            if not path.exists():
                if not fetch:
                    raise RuntimeError('missing corpus original; use --fetch-corpus: '+str(path))
                path.parent.mkdir(parents=True,exist_ok=True)
                req = urllib.request.Request(source['url'],headers={'User-Agent':'SQLNEW-corpus-benchmark/0.2.1'})
                with urllib.request.urlopen(req,timeout=30) as response:
                    content = response.read()
                if digest(content)!=source['sha256']:
                    raise RuntimeError('remote source differs from pinned original: '+source['id'])
                path.write_bytes(content)
        raw = path.read_bytes()
        if digest(raw)!=source['sha256'] or len(raw)!=source['bytes']:
            raise RuntimeError('corpus source differs from pin: '+source['id'])
        parts = passages(raw.decode('utf-8'),size)
        if len(parts)>=PLANE:
            raise RuntimeError('document exceeds coordinate extent')
        rows.extend((doc*PLANE+i,doc,i,body) for i,body in enumerate(parts))
        documents.append(dict(source,document_id=doc,passages=len(parts)))
    return spec, documents, rows


def counters(db, kind):
    if kind=='row':
        return {}
    function = 'sqlnew_region_stats' if kind=='region' else 'sqlnew_columnar_stats'
    return json.loads(db.scalar("SELECT "+function+"('t')"))


def timed(db, sql, args, expected, repetitions, kind):
    before = counters(db,kind)
    samples = []
    statement = db.prepare(sql)
    try:
        for index in range(repetitions+2):
            result = []
            start = time.perf_counter_ns()
            for parameters in args:
                db.bind(statement,parameters)
                result.extend(db.rows(statement))
                db.check(db.lib.sqlite3_reset(statement))
                db.check(db.lib.sqlite3_clear_bindings(statement))
            elapsed = (time.perf_counter_ns()-start)/1e6
            assert result==expected,sql
            if index>=2:
                samples.append(elapsed)
    finally:
        db.lib.sqlite3_finalize(statement)
    after = counters(db,kind)
    delta = {k:after[k]-before.get(k,0) for k in after
             if isinstance(after[k],int) and not k.startswith('connection_')}
    return {'median_ms':statistics.median(samples),
            'p95_ms':sorted(samples)[int((len(samples)-1)*.95)],'samples_ms':samples,
            'result_rows_per_sample':len(expected),
            'returned_text_bytes_per_sample':sum(len(v.encode('utf-8')) for row in expected for v in row if isinstance(v,str)),
            'result_sha256':digest(repr(expected).encode()),'work_delta':delta,
            'connection_gauges_after':{k:v for k,v in after.items() if k.startswith('connection_')},
            'work_samples_including_warmups':repetitions+2,
            'plan':db.execute('EXPLAIN QUERY PLAN '+sql,args[0])}


def fts_oracle(rows, terms, library):
    """Use pristine FTS5 for candidate identities; bodies come from original text."""
    with Database(library=library) as db:
        db.execute("CREATE VIRTUAL TABLE fts USING fts5(body,content='')")
        with db.transaction():
            db.executemany('INSERT INTO fts(rowid,body) VALUES(?,?)',((r[0],r[3]) for r in rows))
        return {term:[r[0] for r in db.execute('SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY rowid LIMIT 16',(term,))] for term in terms}


def run_arm(folder, kind, library, documents, rows, terms, oracle, repetitions):
    path = folder/(kind+('.sqlite' if kind=='row' else '.sqlnew'))
    reference = {r[0]:r[3] for r in rows}
    ids = random.Random(6101501).sample(sorted(reference),min(64,len(rows)))
    with Database(path,library=library) as db:
        for setting in ('page_size=4096','journal_mode=WAL','synchronous=FULL','wal_autocheckpoint=0',
                        'cache_size=-65536','mmap_size=0','temp_store=MEMORY','threads=1'):
            db.execute('PRAGMA '+setting)
        db.execute('CREATE TABLE documents(id INTEGER PRIMARY KEY,title TEXT,source_sha256 TEXT)')
        db.executemany('INSERT INTO documents VALUES(?,?,?)',((d['document_id'],d['title'],d['sha256']) for d in documents))
        if kind=='region':
            db.execute(f'CREATE VIRTUAL TABLE t USING sqlnew_region(64,16384,{len(documents)},TEXT)')
            insert = 'INSERT INTO t(rowid,x,y,z,value) VALUES(?,?,?,?,?)'
            payload = [(r,i%64,i//64,d,body) for r,d,i,body in rows]
            body, doccol, order = 'value','z','y,x'
        else:
            schema = 'CREATE TABLE t(doc INTEGER,ordinal INTEGER,body TEXT)' if kind=='row' else \
                     'CREATE VIRTUAL TABLE t USING sqlnew_columnar(doc INTEGER,ordinal INTEGER,body TEXT)'
            db.execute(schema)
            insert, payload = 'INSERT INTO t(rowid,doc,ordinal,body) VALUES(?,?,?,?)',rows
            body, doccol, order = 'body','doc','ordinal'
        start = time.perf_counter()
        with db.transaction():
            db.executemany(insert,payload)
        ingest = time.perf_counter()-start
        start = time.perf_counter()
        if kind=='region':
            db.execute("INSERT INTO t(__sqlnew_command) VALUES('compact')")
        seal = time.perf_counter()-start
        start = time.perf_counter()
        if kind=='row':
            db.execute('CREATE INDEX t_document ON t(doc,ordinal)')
        db.execute("CREATE VIRTUAL TABLE fts USING fts5(body,content='')")
        with db.transaction():
            db.executemany('INSERT INTO fts(rowid,body) VALUES(?,?)',((r[0],r[3]) for r in rows))
        db.execute("INSERT INTO fts(fts) VALUES('optimize')")
        index = time.perf_counter()-start
        expected_all = [(r[0],r[3]) for r in rows]
        assert db.execute(f'SELECT rowid,{body} FROM t ORDER BY rowid')==expected_all
        for d in documents:
            originals = [r[3] for r in rows if r[1]==d['document_id']]
            assert digest(''.join(originals).encode('utf-8'))==d['sha256']
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        db.execute('VACUUM')
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        assert db.scalar('SELECT count(*) FROM t')==len(rows)
        initial_size = path.stat().st_size
        fts_bytes = db.scalar('SELECT coalesce(sum(length(block)),0) FROM fts_data')
        point_sql = f'SELECT {body} FROM t WHERE rowid=?'
        search_sql = f'SELECT fts.rowid,p.{body} FROM fts JOIN t AS p ON p.rowid=fts.rowid WHERE fts MATCH ? ORDER BY fts.rowid LIMIT 16'
        cases = {
            'point_64':(point_sql,[(i,) for i in ids],[(reference[i],) for i in ids]),
            'keyword_context':(search_sql,[(term,) for term in terms],[(i,reference[i]) for term in terms for i in oracle[term]]),
            'whole_document':(f'SELECT {body} FROM t WHERE {doccol}=? ORDER BY {order}',[(0,)],[(r[3],) for r in rows if r[1]==0])}
        measurements = {name:timed(db,sql,args,expected,repetitions,kind)
                        for name,(sql,args,expected) in cases.items()}
        saved_bases = db.execute('SELECT id,data FROM t_bases ORDER BY id') if kind=='region' else None
        changed = {i:reference[i]+'\nSQLNEWEDITSAMPLE\n' for i in ids}

        def edit(values, old):
            with db.transaction():
                for i,value in values.items():
                    db.execute(f'UPDATE t SET {body}=? WHERE rowid=?',(value,i))
                    db.execute("INSERT INTO fts(fts,rowid,body) VALUES('delete',?,?)",(i,old[i]))
                    db.execute('INSERT INTO fts(rowid,body) VALUES(?,?)',(i,value))

        start = time.perf_counter()
        edit(changed,reference)
        update = time.perf_counter()-start
        if kind=='region':
            assert db.execute('SELECT id,data FROM t_bases ORDER BY id')==saved_bases
        edited = timed(db,point_sql,[(i,) for i in ids],[(changed[i],) for i in ids],repetitions,kind)
        assert [r[0] for r in db.execute("SELECT rowid FROM fts WHERE fts MATCH 'sqlneweditsample' ORDER BY rowid")]==sorted(ids)
        start = time.perf_counter()
        with db.transaction():
            if kind!='row':
                db.execute("INSERT INTO t(__sqlnew_command) VALUES('compact')")
            db.execute("INSERT INTO fts(fts) VALUES('optimize')")
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        maintenance = time.perf_counter()-start
        edited_size = path.stat().st_size
        assert db.execute(point_sql,(ids[0],))==[(changed[ids[0]],)]
        edit({i:reference[i] for i in ids},changed)
        if kind!='row':
            db.execute("INSERT INTO t(__sqlnew_command) VALUES('compact')")
        assert db.execute(f'SELECT rowid,{body} FROM t ORDER BY rowid')==expected_all
        assert db.execute('PRAGMA integrity_check')==[('ok',)]
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        result = {'database_bytes':initial_size,'fts_segment_payload_bytes':fts_bytes,
                  'database_after_edit_compaction_bytes':edited_size,
                  'restored_database_bytes':path.stat().st_size,
                  'ingest_seconds':ingest,'seal_seconds':seal,'index_seconds':index,
                  'ready_seconds':ingest+seal+index,'queries':measurements,
                  'update_64_and_fts_commit_seconds':update,'edited_point_64':edited,
                  'compact_fts_optimize_checkpoint_seconds':maintenance,
                  'region_bases_unchanged_under_edits':True if kind=='region' else None,
                  'exact':True,'library_sha256':digest(library.read_bytes()),
                  'sqlite_source_id':db.scalar('SELECT sqlite_source_id()'),'database':str(path)}
    with Database(path,library=library) as db:
        assert db.execute(f'SELECT rowid,{body} FROM t ORDER BY rowid')==expected_all
        for term in terms:
            assert db.execute(search_sql,(term,))==[(i,reference[i]) for i in oracle[term]]
        assert db.execute("SELECT rowid FROM fts WHERE fts MATCH 'sqlneweditsample'")==[]
        assert db.execute('PRAGMA integrity_check')==[('ok',)]
    print(kind,'bytes',initial_size,'points_ms',round(measurements['point_64']['median_ms'],3),
          'context_ms',round(measurements['keyword_context']['median_ms'],3),flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--fetch-corpus',action='store_true')
    parser.add_argument('--passage-chars',type=int,default=4096)
    parser.add_argument('--repetitions',type=int,default=13)
    parser.add_argument('--sqlite-library',type=Path,
                        help='override the pristine SQLite baseline, for example the compiled public release')
    parser.add_argument('--output',type=Path,default=BUILD/'documents/benchmark.json')
    args = parser.parse_args()
    if args.repetitions<3 or args.passage_chars<256:
        parser.error('use at least 3 repetitions and 256 passage characters')
    spec, documents, rows = corpus(args.fetch_corpus,args.passage_chars)
    current = BUILD/'sqlnew'/('sqlnewdb'+SUFFIX)
    baseline = args.sqlite_library.resolve() if args.sqlite_library else BUILD/'comparison'/('sqlite-baseline'+SUFFIX)
    if not current.is_file() or not baseline.is_file():
        parser.error('build SQLNEW and run sqlnew/benchmark_build.py --fetch-upstream first')
    oracle = fts_oracle(rows,spec['fts_terms'],baseline)
    folder = BUILD/'documents'/('run-'+str(time.time_ns()))
    folder.mkdir(parents=True)
    report = {'recorded_utc':datetime.now(timezone.utc).isoformat(),
              'git_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
              'working_tree_dirty':bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT)),
              'platform':platform.platform(),'processor':platform.processor(),
              'python_version':platform.python_version(),'corpus':spec['name'],
              'sources':documents,'passage_characters_max':args.passage_chars,'rows':len(rows),
              'logical_text_bytes':sum(d['bytes'] for d in documents),
              'fts_terms':spec['fts_terms'],'fts_candidates_per_term':{k:len(v) for k,v in oracle.items()},
              'oracle':'Pristine pinned SQLite FTS5 candidate IDs; full body bytes and reconstructed documents independently match original corpus hashes.',
              'protocol':'Non-overlapping passages preserve every original UTF-8 byte. All arms have the same contentless FTS5 candidate index. Complete sizes include catalog and indexes. SQLite adds (doc,ordinal) index. Warm prepared calls include Python/ctypes result copying; 2 warmups, 13 retained samples by default. WAL FULL, 4 KiB pages, 64 MiB cache, mmap off. One sequential host run. Sparse edits update FTS in the same transaction; compaction, FTS optimization and checkpoint measured separately. No cold-cache or semantic-relevance claim.',
              'counter_scope':'Native compressed payload bytes, decodes and rows only; excludes FTS index and physical/page-cache I/O. Row-store counters unavailable. Work deltas include warmups.',
              'repetitions':args.repetitions,'arms':{},
              'build':json.loads((BUILD/'build.json').read_text()),
              'baseline_build':json.loads((baseline.parent/'build.json').read_text())
                               if (baseline.parent/'build.json').exists() else {'library':str(baseline),'build_metadata_available':False},
              'script_sha256':digest(Path(__file__).read_bytes()),'corpus_manifest_sha256':digest(SPEC.read_bytes())}
    for kind,library in [('row',baseline),('region',current),('columnar',current)]:
        report['arms'][kind] = run_arm(folder,kind,library,documents,rows,spec['fts_terms'],oracle,args.repetitions)
        (folder/'partial.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print('Saved exact document benchmark:',args.output,flush=True)


if __name__=='__main__':
    main()
