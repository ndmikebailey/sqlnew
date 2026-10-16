"""Compare retained 0.1/0.2 binaries and current columns on identical reads."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import multiprocessing as mp
from pathlib import Path
import platform
import subprocess
import time
import traceback

import benchmark_common as common
from db import BUILD, ROOT, SUFFIX, Database

TASKS = ('point_100','time_range_100','whole_summary','group_customers')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def worker(pipe, library, folder, n):
    engine = None
    try:
        # The same label deliberately selects unindexed columns for every version.
        common.ARMS['sqlnew_columns'] = Path(library)
        engine = common.Engine('sqlnew_columns',folder,n)
        pipe.send({'result':engine.setup})
        while True:
            task = pipe.recv()
            if task=='finish':
                assert engine.full_hash()==engine.input_hash
                assert engine.db.execute('PRAGMA integrity_check')==[('ok',)]
                for statement in engine.statements.values():
                    engine.db.lib.sqlite3_finalize(statement)
                engine.db.close()
                with Database(engine.path,library=library) as db:
                    assert common.fingerprint(db.execute('SELECT '+common.FIELDS+' FROM events ORDER BY rowid'))==engine.input_hash
                pipe.send({'result':{'restored_input_sha256':engine.input_hash,'reopened_exact':True}})
                break
            before = json.loads(engine.db.scalar("SELECT sqlnew_columnar_stats('events')"))
            measured = engine.measure(task)
            after = json.loads(engine.db.scalar("SELECT sqlnew_columnar_stats('events')"))
            measured['work'] = {k:v-before.get(k,0) for k,v in after.items()
                                if isinstance(v,int) and not k.startswith('connection_')}
            pipe.send({'result':measured})
    except BaseException:
        pipe.send({'error':traceback.format_exc()})
    finally:
        if engine is not None:
            engine.db.close()
        pipe.close()


def receive(pipe):
    payload = pipe.recv()
    if 'error' in payload:
        raise RuntimeError(payload['error'])
    return payload['result']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--v01-library',type=Path,default=BUILD/'comparison/before-v2/build/sqlnew'/('sqlnewdb'+SUFFIX))
    parser.add_argument('--v02-library',type=Path,default=BUILD/'dev021/before'/('sqlnewdb'+SUFFIX))
    parser.add_argument('--rows',type=int,default=100000)
    parser.add_argument('--repetitions',type=int,default=13)
    parser.add_argument('--output',type=Path,default=BUILD/'version-reads/benchmark.json')
    args = parser.parse_args()
    if args.rows<1000 or args.repetitions<3:
        parser.error('at least 1000 rows and three retained repetitions required')
    libraries = {'v01':args.v01_library.resolve(),'v02':args.v02_library.resolve(),
                 'current':common.ARMS['sqlnew_columns']}
    for path in libraries.values():
        if not path.is_file():
            parser.error('retained library missing; supply matching --v01-library/--v02-library: '+str(path))
    folder = BUILD/'version-reads'/('run-'+str(time.time_ns()))
    folder.mkdir(parents=True)
    report = {'recorded_utc':datetime.now(timezone.utc).isoformat(),
        'git_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        'platform':platform.platform(),'processor':platform.processor(),
        'rows':args.rows,'repetitions':args.repetitions,'warmups':2,
        'protocol':'Three isolated persistent version processes; all unindexed sqlnew_columnar tables, same 100000-row commerce fixture by default, schema, driver, SQL and parameters. Sequential timings with rotating version order, two warmups and retained repetitions below. Reads include binding and result copying; independent Python oracle and full input/reopened hashes. No writes timed. Existing native binaries, with different historical core revisions; this measures version bundles, not a single change. Frozen binaries are local artifacts, not rebuilt or shipped by this script.',
        'source_sha256':{name:digest(ROOT/name) for name in
                         ('sqlnew/benchmark_versions.py','sqlnew/benchmark_common.py','sqlnew/db.py')},
        'arms':{},'tasks':{}}
    workers = {}
    try:
        for name,library in libraries.items():
            arm_folder = folder/name
            arm_folder.mkdir()
            parent,child = mp.Pipe()
            process = mp.Process(target=worker,args=(child,library,arm_folder,args.rows))
            process.start()
            child.close()
            workers[name] = parent,process
            setup = receive(parent)
            report['arms'][name] = {'setup':setup}
            print('Ready',name,setup['sqlnew_version'],setup['database_bytes'],flush=True)
        hashes = {arm['setup']['input_sha256'] for arm in report['arms'].values()}
        assert len(hashes)==1
        for index,task in enumerate(TASKS):
            samples = {name:[] for name in libraries}
            names = list(libraries)
            for iteration in range(args.repetitions+2):
                offset = (iteration+index)%len(names)
                for name in names[offset:]+names[:offset]:
                    pipe,_ = workers[name]
                    pipe.send(task)
                    measured = receive(pipe)
                    assert measured['exact']
                    if iteration>=2:
                        samples[name].append(measured)
            result = {}
            for name,values in samples.items():
                result[name] = common.summarize(values)
                result[name]['work_per_sample'] = [value['work'] for value in values]
            report['tasks'][task] = {'measurements':result}
            print(task,{name:round(v['median_ms'],3) for name,v in result.items()},flush=True)
        for name,(pipe,_) in workers.items():
            pipe.send('finish')
            report['arms'][name]['validation'] = receive(pipe)
        report['all_exact'] = True
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8',newline='\n')
        print('All version answers and reopened inputs exact:',args.output,flush=True)
    finally:
        for pipe,process in workers.values():
            pipe.close()
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join()


if __name__=='__main__':
    mp.freeze_support()
    main()
