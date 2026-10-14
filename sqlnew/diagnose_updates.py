"""Split taxi UPDATE execution from COMMIT using copies of a completed comparison."""
import argparse
import ctypes as C
from datetime import datetime, timezone
import hashlib
import json
import multiprocessing as mp
from pathlib import Path
import shutil
import statistics
import time

import benchmark_compare as bench
from db import Database, ROOT, BUILD, SUFFIX


class PhaseEngine(bench.Engine):
    def __init__(self, arm, folder, spec):
        self.arm, self.spec = arm, spec
        self.path = Path(folder) / (arm + (".sqlnew" if arm == "sqlnew_columnar" else ".sqlite"))
        if self.path.exists():
            raise RuntimeError("diagnostic requires a fresh destination")
        shutil.copyfile(Path(spec["source_folder"]) / self.path.name, self.path)
        self.library = BUILD / "sqlnew/sqlnewdb.dll" if arm == "sqlnew_columnar" else bench.BASE / "sqlite-baseline.dll"
        self.library = self.library.with_suffix(SUFFIX)
        self.db = Database(self.path, library=self.library)
        for pragma in ("journal_mode=WAL", "synchronous=FULL", "wal_autocheckpoint=0",
                       "cache_size=-65536", "mmap_size=0", "temp_store=MEMORY", "threads=1"):
            self.db.execute("PRAGMA " + pragma)
        enable = self.db.lib.sqlite3_enable_load_extension
        enable.argtypes, enable.restype = [C.c_void_p, C.c_int], C.c_int
        self.db.check(enable(self.db.handle, 1))
        self.db.execute("SELECT load_extension(?,?)", (str(bench.BASE / ("shathree" + SUFFIX)), "sqlite3_shathree_init"))
        self.db.check(enable(self.db.handle, 0))
        if bench.fingerprint(self.db, bench.INPUT_SQL) != spec["input_hash"]:
            raise AssertionError("source database does not match the prepared fixture")

    def updates(self):
        db, spec = self.db, self.spec
        if db.execute("PRAGMA wal_checkpoint(TRUNCATE)") != [(0, 0, 0)]:
            raise AssertionError("checkpoint busy")
        statement = db.prepare('UPDATE t SET "fare_amount"="fare_amount"+1 WHERE rowid=?')
        before = self.stats()
        started = time.perf_counter()
        try:
            db.execute("BEGIN")
            for point in spec["points"]:
                db.bind(statement, (point["rowid"],))
                list(db.rows(statement))
                db.check(db.lib.sqlite3_reset(statement))
            after_updates = time.perf_counter()
            db.execute("COMMIT")
            after_commit = time.perf_counter()
        finally:
            db.lib.sqlite3_finalize(statement)
        after = self.stats()
        wal_bytes = Path(str(self.path) + "-wal").stat().st_size
        for point in spec["points"]:
            old = point["original_update"]
            expected = None if old is None else old + 1
            actual = db.scalar("SELECT fare_amount FROM t WHERE rowid=?", (point["rowid"],))
            if type(actual) is not type(expected) or actual != expected:
                raise AssertionError("changed value differs")
        db.execute("BEGIN")
        db.executemany("UPDATE t SET fare_amount=? WHERE rowid=?",
                       [(p["original_update"], p["rowid"]) for p in spec["points"]])
        db.execute("COMMIT")
        return {"updates_ms": (after_updates - started) * 1000,
                "commit_ms": (after_commit - after_updates) * 1000,
                "total_ms": (after_commit - started) * 1000,
                "wal_bytes": wal_bytes, "validated": True,
                "work": {k: after[k] - before[k] for k in before}}


# The spawned worker uses the same implementation in each isolated process.
bench.Engine = PhaseEngine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True, help="Completed comparison run directory")
    args = parser.parse_args()
    spec = next(d for d in json.loads((bench.BASE / "data.json").read_text())["datasets"] if d["name"] == "taxi")
    spec["source_folder"] = str((args.run / "taxi").resolve())
    for arm in bench.ARMS:
        source = Path(spec["source_folder"]) / (arm + (".sqlnew" if arm == "sqlnew_columnar" else ".sqlite"))
        if not source.is_file() or (Path(str(source) + "-wal").exists() and Path(str(source) + "-wal").stat().st_size):
            parser.error("source files must exist and be checkpointed and closed")
    stamp = datetime.now(timezone.utc)
    folder = bench.BASE / "update-phases" / stamp.strftime("%Y%m%dT%H%M%S%fZ")
    folder.mkdir(parents=True)
    pipes, processes = {}, []
    result = {"timestamp_utc": stamp.isoformat(), "source_run": str(args.run.resolve()),
              "input_hash": spec["input_hash"], "operations_per_sample": 100,
              "protocol": "Copies of validated taxi files; same 100 points and FULL/WAL settings; one checked warmup, six sequential balanced repetitions. WAL truncated before each sample outside timer. Commit includes module flush and pager work, not just fsync. Exact originals restored outside timer.",
              "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
                                ("sqlnew/diagnose_updates.py", "sqlnew/benchmark_compare.py", "sqlnew/db.py", "ext/sqlnew/columnar.c")},
              "measurements": {}, "final_validation": {}}
    try:
        for arm in bench.ARMS:
            parent, child = mp.Pipe()
            p = mp.Process(target=bench.worker, args=(child, arm, str(folder), spec))
            p.start()
            child.close()
            pipes[arm] = parent
            processes.append(p)
            bench.receive(parent)
            bench.call(parent, "updates")
            result["measurements"][arm] = {"samples": []}
        for repeat in range(6):
            offset = repeat % 3
            for arm in bench.ARMS[offset:] + bench.ARMS[:offset]:
                result["measurements"][arm]["samples"].append(bench.call(pipes[arm], "updates"))
        for arm, data in result["measurements"].items():
            for key in ("updates_ms", "commit_ms", "total_ms", "wal_bytes"):
                data["median_" + key] = statistics.median(s[key] for s in data["samples"])
            result["final_validation"][arm] = bench.call(pipes[arm], "finish")
            print(arm, {k: v for k, v in data.items() if k != "samples"}, flush=True)
    finally:
        for pipe in pipes.values():
            pipe.close()
        for p in processes:
            p.join(timeout=2)
            if p.is_alive():
                p.terminate()
                p.join(timeout=5)
    output = ROOT / "sqlnew/results/update-phases.json"
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(output, flush=True)


if __name__ == "__main__":
    mp.freeze_support()
    main()
