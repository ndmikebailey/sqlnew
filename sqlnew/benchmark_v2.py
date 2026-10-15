"""Compare both product versions, SQLite, and optional covering indexes."""
import argparse
import copy
import ctypes as C
from datetime import datetime, timezone
import hashlib
import json
import multiprocessing as mp
from pathlib import Path
import platform
import statistics
import time

import benchmark_compare as bench
from db import Database, Error, ROOT, BUILD, SUFFIX

ARMS = ('sqlite', 'sqlite_indexed', 'sqlnew_previous', 'sqlnew_columnar', 'sqlnew_indexed')


class AdvancedEngine(bench.Engine):
    def __init__(self, arm, folder, spec):
        self.label, self.spec = arm, spec
        self.arm = 'sqlnew_columnar' if arm.startswith('sqlnew_') else arm
        self.can_compact = arm in ('sqlnew_columnar', 'sqlnew_indexed')
        self.path = Path(folder) / (arm + ('.sqlnew' if arm.startswith('sqlnew_') else '.sqlite'))
        if self.path.exists():
            raise RuntimeError('comparison requires fresh files')
        self.library = (bench.BASE / 'before-v2/build/sqlnew' / ('sqlnewdb' + SUFFIX) if arm == 'sqlnew_previous'
                        else BUILD / 'sqlnew' / ('sqlnewdb' + SUFFIX) if arm.startswith('sqlnew_')
                        else bench.BASE / ('sqlite-baseline' + SUFFIX))
        self.db = Database(self.path, library=self.library)
        enable = self.db.lib.sqlite3_enable_load_extension
        enable.argtypes, enable.restype = [C.c_void_p, C.c_int], C.c_int
        self.db.check(enable(self.db.handle, 1))
        for name in ('csv', 'shathree'):
            self.db.execute('SELECT load_extension(?,?)', (str(bench.BASE / (name + SUFFIX)), 'sqlite3_' + name + '_init'))
        self.db.check(enable(self.db.handle, 0))
        for pragma in ('page_size=4096', 'journal_mode=WAL', 'synchronous=FULL', 'wal_autocheckpoint=0',
                       'cache_size=-65536', 'mmap_size=0', 'temp_store=MEMORY', 'threads=1'):
            self.db.execute('PRAGMA ' + pragma)

    def load(self):
        db, spec = self.db, self.spec
        declaration = ','.join('"' + name + '" ' + kind for name, kind in spec['schema'])
        if self.arm == 'sqlnew_columnar':
            db.execute('CREATE VIRTUAL TABLE t USING sqlnew_columnar(' + declaration + ')')
        else:
            db.execute('CREATE TABLE t(' + declaration + ')')
        csv = spec['csv'].replace("'", "''").replace('\\', '/')
        db.execute(f"CREATE VIRTUAL TABLE temp.bench_input USING csv(filename='{csv}',header=YES)")
        marker = spec['null_marker'].replace("'", "''")
        projection = ','.join(f'CAST(NULLIF("{name}",\'{marker}\') AS {kind})' for name, kind in spec['schema'])
        start = time.perf_counter()
        db.execute('INSERT INTO t SELECT ' + projection + ' FROM temp.bench_input')
        ingestion = time.perf_counter() - start
        db.execute('DROP TABLE temp.bench_input')
        actual_hash = bench.fingerprint(db, bench.INPUT_SQL)
        if actual_hash != spec['input_hash']:
            raise AssertionError('full typed input hash differs')
        indexes = spec['indexes'] if self.label == 'sqlite_indexed' else []
        commands = spec['columnar_indexes'] if self.label == 'sqlnew_indexed' else []
        index_seconds = 0.0
        if indexes or commands:
            start = time.perf_counter()
            db.execute('BEGIN')
            for sql in indexes:
                db.execute(sql)
            for command in commands:
                db.execute('INSERT INTO t(__sqlnew_command) VALUES(?)', (json.dumps(command),))
            db.execute('COMMIT')
            index_seconds = time.perf_counter() - start
        start = time.perf_counter()
        db.execute('ANALYZE')
        analyze_seconds = time.perf_counter() - start
        wal = Path(str(self.path) + '-wal')
        wal_bytes = wal.stat().st_size if wal.exists() else 0
        start = time.perf_counter()
        if db.execute('PRAGMA wal_checkpoint(TRUNCATE)') != [(0, 0, 0)]:
            raise AssertionError('checkpoint busy')
        checkpoint_seconds = time.perf_counter() - start
        unsupported = {}
        for name, sql in (('h2o_q6', 'SELECT quantile_cont(v3,0.5),stddev(v3) FROM t'), ('h2o_q9', 'SELECT corr(v1,v2) FROM t')):
            if name in spec['unsupported']:
                try:
                    statement = db.prepare(sql)
                except Error as exc:
                    unsupported[name] = str(exc)
                else:
                    db.lib.sqlite3_finalize(statement)
                    raise AssertionError('extend the benchmark: previously unsupported function is available')
        return {'ingest_seconds': ingestion, 'index_seconds': index_seconds, 'analyze_seconds': analyze_seconds,
                'checkpoint_seconds': checkpoint_seconds, 'ready_seconds': ingestion + index_seconds + analyze_seconds + checkpoint_seconds,
                'database_bytes': self.path.stat().st_size, 'wal_bytes_before_checkpoint': wal_bytes,
                'input_hash': actual_hash, 'unsupported': unsupported, 'index_sql': indexes, 'index_commands': commands,
                'sqlite_version': db.scalar('SELECT sqlite_version()'), 'source_id': db.scalar('SELECT sqlite_source_id()'),
                'compile_options': [row[0] for row in db.execute('PRAGMA compile_options')],
                'library_sha256': hashlib.sha256(self.library.read_bytes()).hexdigest(),
                'settings': {name: db.scalar('PRAGMA ' + name) for name in ('page_size', 'journal_mode', 'synchronous',
                             'cache_size', 'mmap_size', 'temp_store', 'threads', 'wal_autocheckpoint')}}

    def query(self, index):
        if self.spec['queries'][index].get('kind') == 'maintenance_cycle':
            return self.maintenance_cycle()
        if self.spec['queries'][index].get('kind') == 'indexed_updates':
            original = self.spec
            self.spec = dict(original, update_column=original['indexed_update_column'],
                             points=original['indexed_update_points'])
            try:
                return super().updates()
            finally:
                self.spec = original
        return super().query(index)

    def maintenance_cycle(self):
        db, spec = self.db, self.spec
        if self.can_compact:
            db.execute("INSERT INTO t(__sqlnew_command) VALUES('compact')")
        if db.execute('PRAGMA wal_checkpoint(TRUNCATE)') != [(0, 0, 0)]:
            raise AssertionError('checkpoint busy')
        statement = db.prepare(f'UPDATE t SET "{spec["update_column"]}"="{spec["update_column"]}"+1 WHERE rowid=?')
        before = self.stats()
        start = time.perf_counter()
        try:
            db.execute('BEGIN')
            for point in spec['points']:
                db.bind(statement, (point['rowid'],))
                list(db.rows(statement))
                db.check(db.lib.sqlite3_reset(statement))
            db.execute('COMMIT')
        finally:
            update_ms = (time.perf_counter() - start) * 1000
            db.lib.sqlite3_finalize(statement)
        for point in spec['points']:
            old = point['original_update']
            expected = None if old is None else old + 1
            actual = db.scalar(f'SELECT "{spec["update_column"]}" FROM t WHERE rowid=?', (point['rowid'],))
            if type(actual) is not type(expected) or actual != expected:
                raise AssertionError('updated value differs')
        # This independent answer reflects the changed values, not just the base fixture.
        spec['queries'].append(spec['post_update_query'])
        try:
            read = super().query(len(spec['queries']) - 1)
        finally:
            spec['queries'].pop()
        compact_ms = 0.0
        if self.can_compact:
            start = time.perf_counter()
            db.execute("INSERT INTO t(__sqlnew_command) VALUES('compact')")
            compact_ms = (time.perf_counter() - start) * 1000
            if db.scalar('SELECT count(*) FROM t_patch') != 0:
                raise AssertionError('compaction left patches')
        start = time.perf_counter()
        if db.execute('PRAGMA wal_checkpoint(TRUNCATE)') != [(0, 0, 0)]:
            raise AssertionError('checkpoint busy')
        checkpoint_ms = (time.perf_counter() - start) * 1000
        after = self.stats()
        # Check the merged answer too. Restore and merge the fixture separately;
        # these reverse mutations are test setup, not application work.
        spec['queries'].append(spec['post_update_query'])
        try:
            super().query(len(spec['queries']) - 1)
        finally:
            spec['queries'].pop()
        db.execute('BEGIN')
        db.executemany(f'UPDATE t SET "{spec["update_column"]}"=? WHERE rowid=?',
                       [(p['original_update'], p['rowid']) for p in spec['points']])
        db.execute('COMMIT')
        if self.can_compact:
            db.execute("INSERT INTO t(__sqlnew_command) VALUES('compact')")
        return {'ms': update_ms + read['ms'] + compact_ms + checkpoint_ms,
                'update_commit_ms': update_ms, 'read_ms': read['ms'], 'compact_commit_ms': compact_ms,
                'checkpoint_ms': checkpoint_ms, 'operations': 100, 'validated': True,
                'max_float_abs_error': read['max_float_abs_error'], 'read_result_hash': read['result_hash'],
                'work': {key: after[key] - before[key] for key in before}}


original_summary = bench.summarize


def summarize(samples):
    result = original_summary(samples)
    for key in ('update_commit_ms', 'read_ms', 'compact_commit_ms', 'checkpoint_ms'):
        if key in samples[0]:
            result[key] = {'median': statistics.median(s[key] for s in samples), 'samples': [s[key] for s in samples]}
    if 'read_result_hash' in samples[0]:
        result['read_result_hash'] = samples[0]['read_result_hash']
    return result


bench.ARMS = ARMS
bench.Engine = AdvancedEngine
bench.summarize = summarize


def prepare_scenario(spec):
    column = 'v3' if spec['name'] == 'groupby' else 'total_amount'
    spec['indexed_update_column'] = column
    spec['indexed_update_points'] = [dict(p, original_update=p['values'][spec['point_columns'].index(column)])
                                     for p in spec['points']]
    if spec['name'] == 'groupby':
        spec['columnar_indexes'] = [{'create_index': 'top_two', 'keys': ['id6', {'column': 'v3', 'desc': True}]}]
        query = copy.deepcopy(next(q for q in spec['queries'] if q['name'] == 'h2o_q1'))
        changed = {}
        for point in spec['points']:
            key = point['values'][spec['point_columns'].index('id1')]
            changed[key] = changed.get(key, 0) + 1
        for row in query['expected']:
            row[1] += changed.get(row[0], 0)
    else:
        import pyarrow as pa
        from benchmark_data import taxi_dataset
        table, _, _ = taxi_dataset(bench.BASE / 'data/taxi.csv.7z')
        payment = table['payment_type'].take(pa.array([p['rowid'] - 1 for p in spec['points']])).to_pylist()
        spec['columnar_indexes'] = [{'create_index': 'pickup_time', 'keys': ['tpep_pickup_datetime'], 'include': ['total_amount']}]
        query = copy.deepcopy(next(q for q in spec['queries'] if q['name'] == 'payment_groupby'))
        changed = {}
        for point, key in zip(spec['points'], payment):
            if point['original_update'] is not None:
                changed[key] = changed.get(key, 0) + 1
        for row in query['expected']:
            row[2] += changed.get(row[0], 0)
    spec['post_update_query'] = query
    spec['queries'].append({'name': 'update_read_compact_100', 'kind': 'maintenance_cycle', 'sql': None,
                            'source': '100 random updates, affected grouping query, complete patch merge and WAL checkpoint'})
    spec['queries'].append({'name': 'indexed_updates_100', 'kind': 'indexed_updates', 'sql': None,
                            'source': f'100 random updates to {column} and FULL commit; maintained by the optional covering index'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repetitions', type=int, default=10)
    parser.add_argument('--dataset', choices=('groupby', 'taxi', 'all'), default='all')
    parser.add_argument('--output', type=Path, default=ROOT / 'sqlnew/results/comparison-v2.json')
    args = parser.parse_args()
    if args.repetitions < 5 or args.repetitions % 5:
        parser.error('repetitions must be a positive multiple of five')
    data = json.loads((bench.BASE / 'data.json').read_text())
    stamp = datetime.now(timezone.utc)
    folder = bench.BASE / 'runs-v2' / stamp.strftime('%Y%m%dT%H%M%S%fZ')
    folder.mkdir(parents=True)
    report = {'timestamp_utc': stamp.isoformat(), 'run_directory': str(folder), 'platform': platform.platform(),
              'python': platform.python_version(), 'sources': data['sources'], 'oracle': data['oracle'],
              'oracle_versions': {key: data[key] for key in ('numpy', 'pyarrow')},
              'protocol': {'arms': ARMS, 'repetitions': args.repetitions, 'warmups': 1,
                           'execution': 'sequential balanced rotation, isolated persistent worker processes',
                           'settings': '4096-byte pages, WAL, FULL, 64 MiB connection cache, memory temp tables, mmap off, one SQL worker',
                           'query_timing': 'prepare and CREATE TEMP TABLE AS SELECT; validation and hashing excluded',
                           'maintenance_cycle': '100 committed updates + affected grouping query + complete patch merge/commit + truncating WAL checkpoint; exact fixture restoration excluded',
                           'index_policy': 'SQLite retains all original workload indexes; SQLNEW adds only the time or top-two index, because native grouping does not require a persistent grouping index',
                           'limits': ['One Windows machine; warm single-connection results.', 'Setup timings are single observations.',
                                      'Temporary SQL sorting follows the connection temp-store setting; native hash state is capped.',
                                      'No combined winner score.']}, 'datasets': []}
    names = ['sqlnew/benchmark_v2.py', 'sqlnew/benchmark_compare.py', 'sqlnew/benchmark_data.py', 'sqlnew/db.py',
             'sqlnew/build.py', 'sqlnew/config.h', 'engine/sqlite/src/select.c']
    names += [p.relative_to(ROOT).as_posix() for p in sorted((ROOT / 'ext/sqlnew').iterdir()) if p.suffix in ('.c', '.inc')]
    report['source_sha256'] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}
    report['historical_baseline_build'] = json.loads((bench.BASE / 'build.json').read_text())
    report['generated_amalgamation_sha256'] = hashlib.sha256((BUILD / 'upstream/sqlite3.c').read_bytes()).hexdigest()
    for spec in data['datasets']:
        if args.dataset != 'all' and spec['name'] != args.dataset:
            continue
        with Path(spec['csv']).open('rb') as f:
            if hashlib.file_digest(f, 'sha256').hexdigest() != spec['csv_sha256']:
                raise RuntimeError('prepared CSV changed')
        prepare_scenario(spec)
        report['datasets'].append(bench.run_dataset(spec, folder / spec['name'], args.repetitions))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print('All answers, update/merge scenarios, full restored hashes, integrity and reopen checks passed.', flush=True)


if __name__ == '__main__':
    mp.freeze_support()
    main()
