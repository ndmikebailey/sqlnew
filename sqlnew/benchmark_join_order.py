"""Read-only join-order diagnosis on restored common-task benchmark files."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import multiprocessing as mp
from pathlib import Path
import statistics
import subprocess
import time
import traceback

from benchmark_common import ARMS, FIELDS, fingerprint, queries, record
from db import BUILD, ROOT, Database


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def worker(pipe, label, setup, n, repetitions):
    try:
        path = Path(setup['database'])
        if not path.is_file():
            raise RuntimeError('rerun benchmark_common.py first: retained database missing')
        library = ARMS[label]
        assert digest(library)==setup['library_sha256'], 'benchmark library changed'
        file_hash = digest(path)
        expected = queries([record(i) for i in range(1,n+1)],[])['join_customer_segments'][2]
        result = {'library_sha256':digest(library),'database_sha256':file_hash,'queries':{}}
        with Database(path,library=library) as db:
            for setting in ('query_only=ON','cache_size=-65536','mmap_size=0',
                            'temp_store=MEMORY','threads=1'):
                db.execute('PRAGMA '+setting)
            assert fingerprint(db.execute('SELECT '+FIELDS+' FROM events ORDER BY rowid'))==setup['input_sha256']
            def counters():
                return json.loads(db.scalar("SELECT sqlnew_columnar_stats('events')")) if label.startswith('sqlnew_columns') else {}
            sql = queries([record(i) for i in range(1,n+1)],[])['join_customer_segments'][0]
            for name,statement_sql in (('default',sql),('events_outer',sql.replace(' JOIN customers',' CROSS JOIN customers'))):
                statement = db.prepare(statement_sql)
                before = counters()
                samples = []
                try:
                    for iteration in range(repetitions+2):
                        started = time.perf_counter_ns()
                        answer = list(db.rows(statement))
                        db.check(db.lib.sqlite3_reset(statement))
                        elapsed = (time.perf_counter_ns()-started)/1e6
                        assert answer==expected,(label,name)
                        if iteration>=2:
                            samples.append(elapsed)
                finally:
                    db.lib.sqlite3_finalize(statement)
                after = counters()
                delta = {k:v-before.get(k,0) for k,v in after.items()
                         if isinstance(v,int) and not k.startswith('connection_')}
                result['queries'][name] = {'sql':statement_sql,'median_ms':statistics.median(samples),
                    'samples_ms':samples,'plan':db.execute('EXPLAIN QUERY PLAN '+statement_sql),
                    'work_delta':delta,'calls_including_warmups':repetitions+2,
                    'expected_sha256':fingerprint(expected),'exact':True}
        assert digest(path)==file_hash,'read-only diagnosis changed database'
        result['database_unchanged'] = True
        pipe.send({'result':result})
    except BaseException:
        pipe.send({'error':traceback.format_exc()})
    finally:
        pipe.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--common-report',type=Path,default=BUILD/'common-tasks/benchmark.json')
    parser.add_argument('--repetitions',type=int,default=5)
    parser.add_argument('--output',type=Path,default=BUILD/'common-tasks/join-order.json')
    args = parser.parse_args()
    if args.repetitions<3:
        parser.error('at least three retained repetitions required')
    common = json.loads(args.common_report.read_text(encoding='utf-8'))
    assert common['all_exact']
    report = {'recorded_utc':datetime.now(timezone.utc).isoformat(),
        'git_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'script_sha256':digest(__file__),'common_report_sha256':digest(args.common_report),
        'protocol':'Separate sequential processes, same libraries and restored post-task files as common report. Read-only queries; full input hashes checked before timing and file hashes unchanged after. Warm prepared calls include result copying, two warmups and retained samples below. CROSS JOIN forces events outer for diagnosis only; original default scores remain unchanged. Counters include warmups and cached decoder inputs, not physical I/O. No native source change.',
        'repetitions':args.repetitions,'arms':{}}
    for label in ('sqlite_release','sqlnew_columns','sqlnew_columns_indexed'):
        parent,child = mp.Pipe()
        process = mp.Process(target=worker,args=(child,label,common['arms'][label]['setup'],common['rows'],args.repetitions))
        process.start()
        child.close()
        try:
            payload = parent.recv()
            if 'error' in payload:
                raise RuntimeError(payload['error'])
            report['arms'][label] = payload['result']
            print(label,{k:round(v['median_ms'],3) for k,v in payload['result']['queries'].items()},flush=True)
        finally:
            parent.close()
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join()
    report['all_exact'] = True
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8',newline='\n')
    print('Exact answers, input hashes and unchanged files:',args.output,flush=True)


if __name__=='__main__':
    mp.freeze_support()
    main()
