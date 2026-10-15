"""Run validated, interleaved file-backed comparisons in isolated engine processes."""
import argparse
import ctypes as C
from datetime import datetime, timezone
import hashlib
import json
import math
import multiprocessing as mp
import os
from pathlib import Path
import platform
import random
import statistics
import time
import traceback

from db import Database, Error, ROOT, BUILD, SUFFIX

BASE = BUILD / "comparison"
ARMS = ("sqlite", "sqlite_indexed", "sqlnew_columnar")
INPUT_SQL = "SELECT * FROM t ORDER BY rowid"
RESULT_SQL = "SELECT * FROM temp.bench_result ORDER BY rowid"


def check_rows(actual, expected):
    if len(actual) != len(expected):
        raise AssertionError(f"row count mismatch: {len(actual)} != {len(expected)}")
    worst = 0.0
    for row_index, (left, right) in enumerate(zip(actual, expected)):
        if len(left) != len(right):
            raise AssertionError("column count mismatch")
        for column, (a, e) in enumerate(zip(left, right)):
            if isinstance(e, float):
                valid = isinstance(a, float) and math.isclose(a, e, rel_tol=1e-10, abs_tol=1e-7)
                if valid:
                    worst = max(worst, abs(a - e))
            else:
                valid = type(a) is type(e) and a == e
            if not valid:
                raise AssertionError(f"row {row_index}, column {column}: {a!r} != {e!r}")
    return worst


def fingerprint(db, sql):
    return db.scalar("SELECT hex(sha3_query(?))", (sql,))


class Engine:
    def __init__(self, arm, folder, spec):
        self.arm, self.spec = arm, spec
        self.path = Path(folder) / (arm + (".sqlnew" if arm == "sqlnew_columnar" else ".sqlite"))
        if self.path.exists():
            raise RuntimeError("benchmark requires a fresh database: " + str(self.path))
        library = BUILD / "sqlnew" / ("sqlnewdb" + SUFFIX) if arm == "sqlnew_columnar" else BASE / ("sqlite-baseline" + SUFFIX)
        self.db = Database(self.path, library=library)
        self.library = library
        # Each engine has its own process, including extension API pointers,
        # connection caches, allocator state and prepared statements.
        enable = self.db.lib.sqlite3_enable_load_extension
        enable.argtypes, enable.restype = [C.c_void_p, C.c_int], C.c_int
        self.db.check(enable(self.db.handle, 1))
        for name in ("csv", "shathree"):
            self.db.execute("SELECT load_extension(?,?)", (str(BASE / (name + SUFFIX)), "sqlite3_" + name + "_init"))
        self.db.check(enable(self.db.handle, 0))
        for pragma in ("page_size=4096", "journal_mode=WAL", "synchronous=FULL", "wal_autocheckpoint=0",
                       "cache_size=-65536", "mmap_size=0", "temp_store=MEMORY", "threads=1"):
            self.db.execute("PRAGMA " + pragma)

    def stats(self):
        return json.loads(self.db.scalar("SELECT sqlnew_columnar_stats('t')")) if self.arm == "sqlnew_columnar" else {}

    def load(self):
        db, spec = self.db, self.spec
        declaration = ",".join('"' + name + '" ' + kind for name, kind in spec["schema"])
        if self.arm == "sqlnew_columnar":
            db.execute("CREATE VIRTUAL TABLE t USING sqlnew_columnar(" + declaration + ")")
        else:
            db.execute("CREATE TABLE t(" + declaration + ")")
        csv = spec["csv"].replace("'", "''").replace("\\", "/")
        db.execute(f"CREATE VIRTUAL TABLE temp.bench_input USING csv(filename='{csv}',header=YES)")
        marker = spec["null_marker"].replace("'", "''")
        projection = ",".join(f'CAST(NULLIF("{name}",\'{marker}\') AS {kind})' for name, kind in spec["schema"])
        started = time.perf_counter()
        db.execute("INSERT INTO t SELECT " + projection + " FROM temp.bench_input")
        ingestion = time.perf_counter() - started
        db.execute("DROP TABLE temp.bench_input")
        actual_hash = fingerprint(db, INPUT_SQL)
        if actual_hash != spec["input_hash"]:
            raise AssertionError(f"{self.arm} {spec['name']} full typed input hash differs: {actual_hash} != {spec['input_hash']}")
        index_seconds = 0.0
        if self.arm == "sqlite_indexed":
            started = time.perf_counter()
            db.execute("BEGIN")
            for sql in spec["indexes"]:
                db.execute(sql)
            db.execute("COMMIT")
            index_seconds = time.perf_counter() - started
        started = time.perf_counter()
        db.execute("ANALYZE")
        analyze_seconds = time.perf_counter() - started
        wal = Path(str(self.path) + "-wal")
        wal_bytes = wal.stat().st_size if wal.exists() else 0
        started = time.perf_counter()
        checkpoint = db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if checkpoint != [(0, 0, 0)]:
            raise AssertionError(checkpoint)
        checkpoint_seconds = time.perf_counter() - started
        unsupported = {}
        for name, probe in (("h2o_q6", "SELECT quantile_cont(v3,0.5),stddev(v3) FROM t"),
                            ("h2o_q9", "SELECT corr(v1,v2) FROM t")):
            if name in spec["unsupported"]:
                try:
                    statement = db.prepare(probe)
                except Error as exc:
                    unsupported[name] = str(exc)
                else:
                    db.lib.sqlite3_finalize(statement)
                    raise AssertionError(name + " unexpectedly supported; extend the benchmark")
        return {"ingest_seconds": ingestion, "index_seconds": index_seconds, "analyze_seconds": analyze_seconds,
                "checkpoint_seconds": checkpoint_seconds,
                "ready_seconds": ingestion + index_seconds + analyze_seconds + checkpoint_seconds,
                "database_bytes": self.path.stat().st_size, "wal_bytes_before_checkpoint": wal_bytes,
                "input_hash": actual_hash, "unsupported": unsupported,
                "sqlite_version": db.scalar("SELECT sqlite_version()"), "source_id": db.scalar("SELECT sqlite_source_id()"),
                "compile_options": [row[0] for row in db.execute("PRAGMA compile_options")],
                "library_sha256": hashlib.sha256(self.library.read_bytes()).hexdigest(),
                "settings": {name: db.scalar("PRAGMA " + name) for name in ("page_size", "journal_mode", "synchronous",
                            "cache_size", "mmap_size", "temp_store", "threads", "wal_autocheckpoint")}}

    def query(self, index):
        db, query = self.db, self.spec["queries"][index]
        db.execute("DROP TABLE IF EXISTS temp.bench_result")
        before = self.stats()
        started = time.perf_counter()
        db.execute("CREATE TEMP TABLE bench_result AS " + query["sql"])
        elapsed = (time.perf_counter() - started) * 1000
        after = self.stats()
        count = db.scalar("SELECT count(*) FROM temp.bench_result")
        if "expected_hash" in query:
            actual_hash = fingerprint(db, RESULT_SQL)
            if count != query["expected_rows"] or actual_hash != query["expected_hash"]:
                raise AssertionError(query["name"] + " full result hash mismatch")
            error = 0.0
        else:
            error = check_rows(db.execute(RESULT_SQL), query["expected"])
            actual_hash = fingerprint(db, RESULT_SQL)
        return {"ms": elapsed, "rows": count, "result_hash": actual_hash, "max_float_abs_error": error,
                "work": {key: after[key] - before[key] for key in before},
                "plan": db.execute("EXPLAIN QUERY PLAN " + query["sql"]), "validated": True}

    def points(self):
        db, spec = self.db, self.spec
        columns = ",".join('"' + name + '"' for name in spec["point_columns"])
        statement = db.prepare("SELECT " + columns + " FROM t WHERE rowid=?")
        actual = []
        before = self.stats()
        started = time.perf_counter()
        try:
            for point in spec["points"]:
                db.bind(statement, (point["rowid"],))
                actual.extend(db.rows(statement))
                db.check(db.lib.sqlite3_reset(statement))
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            db.lib.sqlite3_finalize(statement)
        after = self.stats()
        error = check_rows(actual, [p["values"] for p in spec["points"]])
        return {"ms": elapsed, "operations": len(spec["points"]), "validated": True, "max_float_abs_error": error,
                "work": {key: after[key] - before[key] for key in before}}

    def updates(self):
        db, spec = self.db, self.spec
        column = '"' + spec["update_column"] + '"'
        statement = db.prepare(f"UPDATE t SET {column}={column}+1 WHERE rowid=?")
        before = self.stats()
        started = time.perf_counter()
        try:
            db.execute("BEGIN")
            for point in spec["points"]:
                db.bind(statement, (point["rowid"],))
                list(db.rows(statement))
                db.check(db.lib.sqlite3_reset(statement))
            db.execute("COMMIT")
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            db.lib.sqlite3_finalize(statement)
        after = self.stats()
        for point in spec["points"]:
            old = point["original_update"]
            expected = None if old is None else old + 1
            actual = db.scalar(f"SELECT {column} FROM t WHERE rowid=?", (point["rowid"],))
            if actual != expected:
                raise AssertionError(f"update row {point['rowid']}: {actual!r} != {expected!r}")
        # Restore the exact originals outside the timed region, avoiding float
        # drift from repeatedly adding and subtracting the same number.
        db.execute("BEGIN")
        db.executemany(f"UPDATE t SET {column}=? WHERE rowid=?",
                       [(p["original_update"], p["rowid"]) for p in spec["points"]])
        db.execute("COMMIT")
        return {"ms": elapsed, "operations": len(spec["points"]), "validated": True,
                "work": {key: after[key] - before[key] for key in before}}

    def finish(self):
        db = self.db
        restored_hash = fingerprint(db, INPUT_SQL)
        if restored_hash != self.spec["input_hash"]:
            raise AssertionError("full dataset changed after restored updates")
        integrity = db.execute("PRAGMA integrity_check")
        if integrity != [("ok",)]:
            raise AssertionError(integrity)
        if db.execute("PRAGMA wal_checkpoint(TRUNCATE)") != [(0, 0, 0)]:
            raise AssertionError("checkpoint busy")
        db.close()
        self.db = Database(self.path, library=self.library)
        if self.db.scalar("SELECT count(*) FROM t") != self.spec["rows"]:
            raise AssertionError("reopen row count changed")
        self.db.close()
        return {"restored_full_input_hash": restored_hash, "integrity": "ok", "reopen": True,
                "final_database_bytes": self.path.stat().st_size}


def worker(pipe, arm, folder, spec):
    try:
        engine = Engine(arm, folder, spec)
        pipe.send({"ok": True})
        while True:
            method, argument = pipe.recv()
            result = getattr(engine, method)(*argument)
            pipe.send({"ok": True, "result": result})
            if method == "finish":
                return
    except BaseException:
        pipe.send({"ok": False, "error": traceback.format_exc()})
    finally:
        pipe.close()


def receive(pipe, timeout=300):
    if not pipe.poll(timeout):
        raise TimeoutError("benchmark worker exceeded 300 seconds")
    result = pipe.recv()
    if not result["ok"]:
        raise RuntimeError(result["error"])
    return result.get("result")


def call(pipe, method, *args):
    pipe.send((method, args))
    return receive(pipe)


def summarize(samples):
    timings = [x["ms"] for x in samples]
    q1, _, q3 = statistics.quantiles(timings, n=4, method="inclusive")
    return {"median_ms": statistics.median(timings), "q1_ms": q1, "q3_ms": q3,
            "samples_ms": timings, "validated": all(x["validated"] for x in samples),
            "max_float_abs_error": max(x.get("max_float_abs_error", 0.0) for x in samples),
            "work_per_sample": [x["work"] for x in samples],
            **{key: samples[0][key] for key in ("rows", "operations", "result_hash", "plan") if key in samples[0]}}


def run_dataset(spec, folder, repetitions):
    folder.mkdir()
    pipes, processes = {}, []
    result = {key: value for key, value in spec.items() if key not in ("queries", "points", "csv")}
    result["engines"], result["queries"] = {}, {}
    try:
        for arm in ARMS:
            parent, child = mp.Pipe()
            process = mp.Process(target=worker, args=(child, arm, str(folder), spec))
            process.start()
            child.close()
            pipes[arm] = parent
            processes.append(process)
            receive(parent)
        order = list(ARMS)
        random.Random(20260905).shuffle(order)
        result["load_order"] = order
        for arm in order:
            result["engines"][arm] = call(pipes[arm], "load")
            r = result["engines"][arm]
            print(spec["name"], arm, f"load {r['ingest_seconds']:.2f}s; file {r['database_bytes']:,}; input hash verified", flush=True)
        work = [(q["name"], "query", [i], q) for i, q in enumerate(spec["queries"])]
        work += [("point_reads_100", "points", [], {}), ("random_updates_100", "updates", [], {})]
        for number, (name, method, args, query) in enumerate(work):
            for arm in ARMS:
                call(pipes[arm], method, *args)  # Validated warm-up, not a recorded sample.
            samples = {arm: [] for arm in ARMS}
            orders = []
            for repetition in range(repetitions):
                offset = (repetition + number) % len(ARMS)
                order = ARMS[offset:] + ARMS[:offset]
                orders.append(list(order))
                for arm in order:
                    samples[arm].append(call(pipes[arm], method, *args))
            measurements = {arm: summarize(samples[arm]) for arm in ARMS}
            result["queries"][name] = {"sql": query.get("sql"), "source": query.get("source", "SQLNEW access/update diagnostic"),
                                       "sample_orders": orders, "measurements": measurements}
            print(spec["name"], name, " | ".join(f"{arm} {measurements[arm]['median_ms']:.3f} ms" for arm in ARMS), flush=True)
        for arm in ARMS:
            result["engines"][arm]["final_validation"] = call(pipes[arm], "finish")
    finally:
        for pipe in pipes.values():
            pipe.close()
        for process in processes:
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=6)
    parser.add_argument("--dataset", choices=("groupby", "taxi", "all"), default="all")
    parser.add_argument("--output", type=Path, default=ROOT / "sqlnew/results/comparison.json")
    args = parser.parse_args()
    if args.repetitions < 3 or args.repetitions % 3:
        parser.error("repetitions must be a positive multiple of three, at least three")
    data = json.loads((BASE / "data.json").read_text())
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder = BASE / "runs" / stamp
    folder.mkdir(parents=True)
    report = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "platform": platform.platform(),
              "processor": platform.processor(), "logical_cpus": os.cpu_count(), "python": platform.python_version(),
              "sources": data["sources"], "build": json.loads((BASE / "build.json").read_text()),
              "oracle": data["oracle"], "oracle_versions": {key: data[key] for key in ("numpy", "pyarrow")},
              "protocol": {"repetitions": args.repetitions, "warmups": 1, "execution": "sequential balanced rotation across isolated worker processes",
                           "query_timing": "CREATE TEMP TABLE AS SELECT, including preparation and in-memory result materialization; validation excluded",
                           "point_timing": "100 prepared point reads, including shared Python/ctypes binding and result conversion",
                           "update_timing": "100 prepared random updates in one FULL-synchronous committed transaction; validation/restoration excluded",
                           "load_timing": "single observation per arm: same native CSV reader, casts, INSERT SELECT and commit; preprocessing excluded",
                           "comparison": "pristine revision-matched SQLite, same SQLite with declared workload indexes, SQLNEW columnar",
                           "float_tolerance": {"relative": 1e-10, "absolute": 1e-7},
                           "limits": ["Warm local single-connection workload; not a cold-cache or concurrent-service benchmark.",
                                      "Public workloads adapted to supported SQL and available machine scale; not published-suite scores.",
                                      "Load and index-build times are single observations, not sampled estimates.",
                                      "No composite winner score; workload tradeoffs reported separately."]}, "datasets": []}
    names = ("sqlnew/benchmark_build.py", "sqlnew/benchmark_data.py", "sqlnew/benchmark_compare.py",
             "sqlnew/db.py", "sqlnew/build.py", "sqlnew/config.h", "engine/sqlite/src/select.c",
             "ext/sqlnew/columnar.c", "ext/sqlnew/sqlnew.c", "ext/sqlnew/intblock.c")
    report["source_sha256"] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}
    for spec in data["datasets"]:
        if args.dataset != "all" and spec["name"] != args.dataset:
            continue
        with Path(spec["csv"]).open("rb") as f:
            if hashlib.file_digest(f, "sha256").hexdigest() != spec["csv_sha256"]:
                raise RuntimeError("prepared CSV changed")
        report["datasets"].append(run_dataset(spec, folder / spec["name"], args.repetitions))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("All measured answers, full input hashes, restored data, integrity and reopen checks passed", flush=True)


if __name__ == "__main__":
    main()
