"""Validated, interleaved common SQL tasks against released and matched SQLite."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import multiprocessing as mp
from pathlib import Path
import platform
import random
import statistics
import subprocess
import time
import traceback

from db import Database, ROOT, BUILD, SUFFIX

SCHEMA = 'customer INTEGER,created INTEGER,amount INTEGER,qty INTEGER,status TEXT,memo TEXT'
FIELDS = 'rowid,customer,created,amount,qty,status,memo'
INSERT = 'INSERT INTO events('+FIELDS+') VALUES(?,?,?,?,?,?,?)'
EPOCH = 1700000000
INDEXES = [('customer',['customer','created','amount','qty','status']),
           ('time',['created','customer','amount','qty','status'])]
ARMS = {
    'sqlite_release':BUILD/'comparison/sqlite-release'/('sqlite-release'+SUFFIX),
    'sqlite_release_unindexed':BUILD/'comparison/sqlite-release'/('sqlite-release'+SUFFIX),
    'sqlite_matched':BUILD/'comparison'/('sqlite-baseline'+SUFFIX),
    'sqlnew_rows':BUILD/'sqlnew'/('sqlnewdb'+SUFFIX),
    'sqlnew_columns':BUILD/'sqlnew'/('sqlnewdb'+SUFFIX),
    'sqlnew_columns_indexed':BUILD/'sqlnew'/('sqlnewdb'+SUFFIX)}
WRITES = ('insert_1000_commit','update_memo_100_commit','update_indexed_amount_100_commit',
          'delete_100_commit','replace_100_commit','single_insert_20_commits','rollback_100_updates')


def record(i):
    status = 'cancelled' if i%10==0 else 'pending' if i%10==1 else 'paid'
    return (i,(i*17)%1000+1,EPOCH+i,(i*7919)%50000,None if i%37==0 else i%5+1,
            status,f"order={i} channel={('web','app','api')[i%3]} item={i%97:03d}")


def fingerprint(rows):
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps(row,ensure_ascii=False,separators=(',',':')).encode())
        digest.update(b'\n')
    return digest.hexdigest()


def queries(rows, ids):
    n = len(rows)
    groups, segments = {}, {}
    for _,customer,_,amount,qty,_,_ in rows:
        group = groups.setdefault(customer,[0,0,0])
        group[0]+=1; group[1]+=amount; group[2]+=qty or 0
        segment = segments.setdefault('segment '+str((customer-1)%5),[0,0])
        segment[0]+=1; segment[1]+=amount
    recent = sorted((r for r in rows if r[1]==42),key=lambda r:r[2],reverse=True)[:20]
    start = max(1,n//2)
    return {
        'point_100':('SELECT '+FIELDS+' FROM events WHERE rowid=?',[(i,) for i in ids],[rows[i-1] for i in ids]),
        'time_range_100':('SELECT rowid,created,customer,amount,qty,status FROM events WHERE created BETWEEN ? AND ? ORDER BY created',
                          [(EPOCH+start,EPOCH+start+99)],[(r[0],r[2],r[1],r[3],r[4],r[5]) for r in rows[start-1:start+99]]),
        'customer_recent_20':('SELECT rowid,created,amount,qty,status FROM events WHERE customer=? ORDER BY created DESC LIMIT 20',
                              [(42,)],[(r[0],r[2],r[3],r[4],r[5]) for r in recent]),
        'whole_summary':('SELECT count(*),sum(amount),min(created),max(created) FROM events',[()],
                          [(n,sum(r[3] for r in rows),rows[0][2],rows[-1][2])]),
        'nullable_summary':('SELECT count(*),count(qty),sum(amount),min(created),max(created) FROM events',[()],
                          [(n,sum(r[4] is not None for r in rows),sum(r[3] for r in rows),rows[0][2],rows[-1][2])]),
        'group_customers':('SELECT customer,count(*),sum(amount),sum(qty) FROM events GROUP BY customer ORDER BY customer',
                            [()],[(k,*v) for k,v in sorted(groups.items())]),
        'join_customer_segments':('SELECT c.segment,count(*),sum(e.amount) FROM events e JOIN customers c ON c.id=e.customer GROUP BY c.segment ORDER BY c.segment',
                                  [()],[(k,*v) for k,v in sorted(segments.items())]),
        'sort_top_20':('SELECT rowid,amount FROM events ORDER BY amount DESC,rowid LIMIT 20',[()],
                        [(r[0],r[3]) for r in sorted(rows,key=lambda r:(-r[3],r[0]))[:20]])}


class Engine:
    def __init__(self, label, folder, n):
        self.label, self.n = label,n
        self.columnar = label.startswith('sqlnew_columns')
        self.indexed = label not in ('sqlnew_columns','sqlite_release_unindexed')
        self.rows = [record(i) for i in range(1,n+1)]
        self.input_hash = fingerprint(self.rows)
        self.ids = random.Random(6101502).sample(range(1,n+1),100)
        self.cases = queries(self.rows,self.ids)
        self.library = ARMS[label]
        self.path = Path(folder)/(label+('.sqlnew' if label.startswith('sqlnew') else '.sqlite'))
        if self.path.exists():
            raise RuntimeError('benchmark requires fresh database files')
        self.db = db = Database(self.path,library=self.library)
        for p in ('page_size=4096','journal_mode=WAL','synchronous=FULL','wal_autocheckpoint=0',
                  'cache_size=-65536','mmap_size=0','temp_store=MEMORY','threads=1'):
            db.execute('PRAGMA '+p)
        db.execute('CREATE TABLE customers(id INTEGER PRIMARY KEY,segment TEXT)')
        db.executemany('INSERT INTO customers VALUES(?,?)',((i,'segment '+str((i-1)%5)) for i in range(1,1001)))
        db.execute('CREATE TEMP TABLE seed(r INTEGER,'+SCHEMA+')')
        db.execute("""WITH RECURSIVE seq(i) AS (VALUES(1) UNION ALL SELECT i+1 FROM seq WHERE i<?)
          INSERT INTO seed SELECT i,(i*17)%1000+1,1700000000+i,(i*7919)%50000,
          CASE WHEN i%37=0 THEN NULL ELSE i%5+1 END,
          CASE WHEN i%10=0 THEN 'cancelled' WHEN i%10=1 THEN 'pending' ELSE 'paid' END,
          printf('order=%d channel=%s item=%03d',i,CASE i%3 WHEN 0 THEN 'web' WHEN 1 THEN 'app' ELSE 'api' END,i%97)
          FROM seq""",(n+1020,))
        db.execute(('CREATE VIRTUAL TABLE events USING sqlnew_columnar('+SCHEMA+')') if self.columnar else
                   'CREATE TABLE events('+SCHEMA+')')
        started = time.perf_counter_ns()
        db.execute('BEGIN')
        db.execute('INSERT INTO events('+FIELDS+') SELECT * FROM seed WHERE r<=?',(n,))
        db.execute('COMMIT')
        ingest = (time.perf_counter_ns()-started)/1e6
        started = time.perf_counter_ns()
        if self.indexed:
            db.execute('BEGIN')
            for name,keys in INDEXES:
                if self.columnar:
                    db.execute('INSERT INTO events(__sqlnew_command) VALUES(?)',
                               (json.dumps({'create_index':name,'keys':keys}),))
                else:
                    db.execute('CREATE INDEX events_'+name+' ON events('+','.join(keys)+')')
            db.execute('COMMIT')
        indexing = (time.perf_counter_ns()-started)/1e6
        started = time.perf_counter_ns()
        db.execute('ANALYZE')
        analysis = (time.perf_counter_ns()-started)/1e6
        assert self.full_hash()==self.input_hash
        self.checkpoint()
        # Initial sizing removes allocation slack equally; later growth is retained.
        db.execute('VACUUM')
        self.checkpoint()
        assert db.scalar('SELECT count(*) FROM events')==n
        self.statements = {k:db.prepare(v[0]) for k,v in self.cases.items()}
        self.setup = {'rows':n,'customers':1000,'input_sha256':self.input_hash,
                      'ingest_commit_ms':ingest,'index_commit_ms':indexing,'analyze_ms':analysis,
                      'database_bytes':self.path.stat().st_size,
                      'freelist_pages':db.scalar('PRAGMA freelist_count'),
                      'indexes':INDEXES if self.indexed else [],
                      'sqlite_version':db.scalar('SELECT sqlite_version()'),
                      'sqlite_source_id':db.scalar('SELECT sqlite_source_id()'),
                      'sqlnew_version':db.scalar('SELECT sqlnew_version()') if label.startswith('sqlnew') else None,
                      'library_sha256':hashlib.sha256(self.library.read_bytes()).hexdigest(),
                      'compile_options':[r[0] for r in db.execute('PRAGMA compile_options')],
                      'settings':{p:db.scalar('PRAGMA '+p) for p in ('page_size','journal_mode','synchronous',
                                  'cache_size','mmap_size','temp_store','threads','wal_autocheckpoint')},
                      'plans':{k:db.execute('EXPLAIN QUERY PLAN '+v[0],v[1][0]) for k,v in self.cases.items()},
                      'database':str(self.path)}

    def full_hash(self):
        db = self.db
        statement = db.prepare('SELECT '+FIELDS+' FROM events ORDER BY rowid')
        try:
            return fingerprint(db.rows(statement))
        finally:
            db.lib.sqlite3_finalize(statement)

    def checkpoint(self):
        assert self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')==[(0,0,0)]

    def selected(self, ids):
        return self.db.execute('SELECT '+FIELDS+' FROM events WHERE rowid IN ('+
                               ','.join('?' for _ in ids)+') ORDER BY rowid',ids)

    def measure(self, task):
        db = self.db
        if task in self.cases:
            _,parameters,expected = self.cases[task]
            statement = self.statements[task]
            result = []
            started = time.perf_counter_ns()
            for args in parameters:
                db.bind(statement,args)
                result.extend(db.rows(statement))
                db.check(db.lib.sqlite3_reset(statement))
                db.check(db.lib.sqlite3_clear_bindings(statement))
            elapsed = (time.perf_counter_ns()-started)/1e6
            assert result==expected,(self.label,task)
            return {'ms':elapsed,'exact':True}
        ids = self.ids
        original = [self.rows[i-1] for i in ids]
        changed = [(*r[:-1],r[-1]+' updated') for r in original]
        started = time.perf_counter_ns()
        if task!='single_insert_20_commits':
            db.execute('BEGIN')
        if task=='insert_1000_commit':
            db.execute('INSERT INTO events('+FIELDS+') SELECT * FROM seed WHERE r>? AND r<=?',(self.n,self.n+1000))
        elif task in ('update_memo_100_commit','rollback_100_updates'):
            db.executemany('UPDATE events SET memo=? WHERE rowid=?',((r[-1],r[0]) for r in changed))
        elif task=='update_indexed_amount_100_commit':
            db.executemany('UPDATE events SET amount=amount+7 WHERE rowid=?',((i,) for i in ids))
        elif task=='delete_100_commit':
            db.executemany('DELETE FROM events WHERE rowid=?',((i,) for i in ids))
        elif task=='replace_100_commit':
            db.executemany(INSERT.replace('INSERT INTO','INSERT OR REPLACE INTO'),changed)
        elif task=='single_insert_20_commits':
            for i in range(self.n+1001,self.n+1021):
                db.execute('BEGIN')
                db.execute(INSERT,record(i))
                db.execute('COMMIT')
        else:
            raise ValueError(task)
        if task!='single_insert_20_commits':
            db.execute('ROLLBACK' if task=='rollback_100_updates' else 'COMMIT')
        elapsed = (time.perf_counter_ns()-started)/1e6
        expected_count = self.n
        if task in ('insert_1000_commit','single_insert_20_commits'):
            inserted = list(range(self.n+1,self.n+1001)) if task=='insert_1000_commit' else list(range(self.n+1001,self.n+1021))
            assert self.selected(inserted)==[record(i) for i in inserted]
            expected_count+=len(inserted)
        elif task=='delete_100_commit':
            assert self.selected(ids)==[]
            expected_count-=100
        elif task=='update_indexed_amount_100_commit':
            assert self.selected(ids)==sorted([(*r[:3],r[3]+7,*r[4:]) for r in original])
        else:
            assert self.selected(ids)==sorted(original if task=='rollback_100_updates' else changed)
        assert db.scalar('SELECT count(*) FROM events')==expected_count
        started = time.perf_counter_ns()
        db.execute('BEGIN')
        if task in ('insert_1000_commit','single_insert_20_commits'):
            db.execute('DELETE FROM events WHERE rowid>?',(self.n,))
        elif task=='delete_100_commit':
            db.executemany(INSERT,original)
        elif task in ('update_memo_100_commit','replace_100_commit'):
            db.executemany('UPDATE events SET memo=? WHERE rowid=?',((r[-1],r[0]) for r in original))
        elif task=='update_indexed_amount_100_commit':
            db.executemany('UPDATE events SET amount=? WHERE rowid=?',((r[3],r[0]) for r in original))
        db.execute('COMMIT')
        restore = (time.perf_counter_ns()-started)/1e6
        started = time.perf_counter_ns()
        if self.columnar:
            db.execute("INSERT INTO events(__sqlnew_command) VALUES('compact')")
        self.checkpoint()
        maintenance = (time.perf_counter_ns()-started)/1e6
        assert self.selected(ids)==sorted(original)
        assert db.scalar('SELECT count(*) FROM events')==self.n
        return {'ms':elapsed,'restore_ms':restore,'compact_checkpoint_ms':maintenance,
                'benchmark_cycle_ms':elapsed+restore+maintenance,'exact':True}

    def finish(self):
        assert self.full_hash()==self.input_hash
        for task in self.cases:
            self.measure(task)
        assert self.db.execute('PRAGMA integrity_check')==[('ok',)]
        self.checkpoint()
        result = {'exact':True,'restored_input_sha256':self.input_hash,
                  'database_after_tasks_bytes':self.path.stat().st_size,
                  'freelist_pages':self.db.scalar('PRAGMA freelist_count')}
        for statement in self.statements.values():
            self.db.lib.sqlite3_finalize(statement)
        self.statements.clear()
        self.db.close()
        with Database(self.path,library=self.library) as reopened:
            assert reopened.scalar('SELECT count(*) FROM events')==self.n
            statement = reopened.prepare('SELECT '+FIELDS+' FROM events ORDER BY rowid')
            try:
                assert fingerprint(reopened.rows(statement))==self.input_hash
            finally:
                reopened.lib.sqlite3_finalize(statement)
            assert reopened.execute('PRAGMA integrity_check')==[('ok',)]
        result['reopened_exact'] = True
        return result

    def close(self):
        for statement in getattr(self,'statements',{}).values():
            self.db.lib.sqlite3_finalize(statement)
        self.statements = {}
        self.db.close()


def worker(pipe, label, folder, n):
    engine = None
    try:
        engine = Engine(label,folder,n)
        pipe.send({'ok':True,'result':engine.setup})
        while True:
            message = pipe.recv()
            if message=='close':
                return
            result = engine.finish() if message=='finish' else engine.measure(message)
            pipe.send({'ok':True,'result':result})
    except BaseException:
        pipe.send({'ok':False,'error':traceback.format_exc()})
    finally:
        if engine is not None:
            engine.close()
        pipe.close()


def receive(pipe):
    if not pipe.poll(300):
        raise TimeoutError('worker exceeded five minutes; retain its partial run for inspection')
    answer = pipe.recv()
    if not answer['ok']:
        raise RuntimeError(answer['error'])
    return answer['result']


def summarize(samples, field='ms'):
    values = [s[field] for s in samples]
    return {'median_ms':statistics.median(values),
            'p95_ms':sorted(values)[int((len(values)-1)*.95)],'samples_ms':values}


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--rows',type=int,default=100000)
    parser.add_argument('--repetitions',type=int,default=13)
    parser.add_argument('--output',type=Path,default=BUILD/'common-tasks/benchmark.json')
    args = parser.parse_args()
    if args.rows<1000 or args.repetitions<3:
        parser.error('use at least 1000 rows and 3 repetitions')
    for name,path in ARMS.items():
        if not path.is_file():
            parser.error(name+': missing '+str(path)+'; build product, matched baseline and released baseline first')
    folder = BUILD/'common-tasks'/('run-'+str(time.time_ns()))
    folder.mkdir(parents=True)
    rows = [record(i) for i in range(1,args.rows+1)]
    ids = random.Random(6101502).sample(range(1,args.rows+1),100)
    cases = queries(rows,ids)
    report = {'recorded_utc':datetime.now(timezone.utc).isoformat(),
              'git_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
              'working_tree_dirty':bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT)),
              'platform':platform.platform(),'processor':platform.processor(),'python':platform.python_version(),
              'rows':args.rows,'customers':1000,'schema':SCHEMA,'input_sha256':fingerprint(rows),
              'fixture':'Deterministic commerce-style records: ascending seconds, 1000 permuted customer IDs, integer cents, nullable quantity, three statuses and unique short memos. Synthetic; no artificial repeated body padding.',
              'protocol':'One isolated persistent worker per arm. All timing sequential; arm order rotates each repetition. Two warmups, retained repetitions below; lower empirical p95. Same ctypes driver, 4 KiB pages, WAL FULL, 64 MiB cache, mmap off, memory temp data. Reads use prepared statements and include binding/result copying. Writes include BEGIN and FULL COMMIT; exact checks excluded. Fixture preparation excluded; native bulk ingestion, index build and ANALYZE reported separately. Both covering indexes use identical five key fields. Initial VACUUM/checkpoint for size; later file growth retained. Restoration and native compaction/checkpoint are separately timed; benchmark cycle includes undo, not a normal application transaction. No concurrent writers or cold-cache claim.',
              'repetitions':args.repetitions,'warmups':2,'arms':{},'tasks':{},
              'build':json.loads((BUILD/'build.json').read_text()),
              'matched_baseline':json.loads((BUILD/'comparison/build.json').read_text()),
              'released_baseline':json.loads((BUILD/'comparison/sqlite-release/build.json').read_text()),
              'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    context = mp.get_context('spawn')
    workers = {}
    try:
        for label in ARMS:
            parent,child = context.Pipe()
            process = context.Process(target=worker,args=(child,label,folder,args.rows))
            process.start(); child.close()
            workers[label] = (parent,process)
            setup = receive(parent)
            assert setup['input_sha256']==report['input_sha256']
            report['arms'][label] = {'setup':setup}
            print('Ready',label,'bytes',setup['database_bytes'],flush=True)
        tasks = [*cases,*WRITES]
        for task_index,task in enumerate(tasks):
            samples = {label:[] for label in ARMS}
            labels = list(ARMS)
            for repeat in range(args.repetitions+2):
                offset = (repeat+task_index)%len(labels)
                for label in labels[offset:]+labels[:offset]:
                    pipe,_ = workers[label]
                    pipe.send(task)
                    measured = receive(pipe)
                    assert measured['exact']
                    if repeat>=2:
                        samples[label].append(measured)
            task_result = {'measurements':{}}
            if task in cases:
                sql,parameters,expected = cases[task]
                task_result.update(sql=sql,parameters=parameters,expected_result_sha256=fingerprint(expected),result_rows=len(expected))
            for label,values in samples.items():
                result = summarize(values)
                if task in WRITES:
                    result.update(restore=summarize(values,'restore_ms'),
                                  compact_checkpoint=summarize(values,'compact_checkpoint_ms'),
                                  benchmark_cycle=summarize(values,'benchmark_cycle_ms'))
                task_result['measurements'][label] = result
            report['tasks'][task] = task_result
            (folder/'partial.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
            print(task,{k:round(v['median_ms'],3) for k,v in task_result['measurements'].items()},flush=True)
        for label,(pipe,_) in workers.items():
            pipe.send('finish')
            report['arms'][label]['validation'] = receive(pipe)
        report['all_exact'] = True
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
        print('All common tasks, full restored input and reopened files exact:',args.output,flush=True)
    finally:
        for pipe,process in workers.values():
            if process.is_alive():
                try: pipe.send('close')
                except (BrokenPipeError,EOFError,OSError): pass
            process.join(5)
            if process.is_alive(): process.terminate();process.join()
            pipe.close()


if __name__=='__main__':
    mp.freeze_support()
    main()
