"""Small synthetic before/after checks; no external datasets or upstream build."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import tempfile
import time

from db import Database


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fixture = [(i % 17, i % 501 - 250, (i % 31) / 4.0, f"key-{i % 7}", bytes([i % 5]))
               for i in range(2048)]
    queries = {
        "summary": "SELECT count(v),sum(v),min(v),max(v) FROM t",
        "grouping": "SELECT k,count(*),sum(v),sum(r),min(s) FROM t GROUP BY k ORDER BY k",
        "range": "SELECT count(*),sum(r) FROM t WHERE v>=20 AND v<40",
        "point": "SELECT * FROM t WHERE rowid=1010",
    }
    report = {"rows": len(fixture), "repetitions": 11, "queries_per_repetition": 20,
              "source": "deterministic synthetic fixture", "units": "milliseconds",
              "scope": "Warm single-process microchecks; no claim about large datasets or other platforms.",
              "implementations": {}}
    with tempfile.TemporaryDirectory(prefix="core-samples-", dir=args.output.parent) as root:
        handles = {}
        for name, library in (("before", args.before), ("after", args.after)):
            db = Database(Path(root) / (name + ".sqlnew"), library=library.resolve())
            handles[name] = db
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE VIRTUAL TABLE t USING sqlnew_columnar(k INTEGER,v INTEGER,r REAL,s TEXT,b BLOB)")
            db.execute("CREATE TABLE reference(k INTEGER,v INTEGER,r REAL,s TEXT,b BLOB)")
            for table in ("t", "reference"):
                with db.transaction():
                    db.executemany(f"INSERT INTO {table} VALUES(?,?,?,?,?)", fixture)
            db.execute("INSERT INTO t(__sqlnew_command) VALUES(?)",
                       (json.dumps({"create_index": "range", "keys": ["v"], "include": ["r"]}),))
            for query in queries.values():
                assert db.execute(query) == db.execute(query.replace(" FROM t", " FROM reference")), query
            frames = db.execute("SELECT block,col,data FROM t_data ORDER BY block,col")
            digest = hashlib.sha256()
            for block, col, frame in frames:
                digest.update(f"{block},{col},{len(frame)}:".encode())
                digest.update(frame)
            report["implementations"][name] = {
                "library_sha256": hashlib.sha256(library.read_bytes()).hexdigest(),
                "column_frames_sha256": digest.hexdigest(),
                "timings": {key: [] for key in (*queries, "eight_updates_commit")},
            }
        try:
            for repeat in range(11):
                for name in (("before", "after") if repeat % 2 == 0 else ("after", "before")):
                    db = handles[name]
                    timings = report["implementations"][name]["timings"]
                    for key, query in queries.items():
                        start = time.perf_counter_ns()
                        for _ in range(20):
                            db.execute(query)
                        timings[key].append((time.perf_counter_ns() - start) / 1e6 / 20)
                    start = time.perf_counter_ns()
                    db.execute("BEGIN")
                    db.executemany("UPDATE t SET k=k+1 WHERE rowid=?", [(i,) for i in range(1, 9)])
                    db.execute("COMMIT")
                    timings["eight_updates_commit"].append((time.perf_counter_ns() - start) / 1e6)
                    assert db.execute("SELECT k FROM t WHERE rowid<=8 ORDER BY rowid") == [(i % 17 + 1,) for i in range(8)]
                    db.execute("BEGIN")
                    db.executemany("UPDATE t SET k=k-1 WHERE rowid=?", [(i,) for i in range(1, 9)])
                    db.execute("COMMIT")
            for name, db in handles.items():
                assert db.scalar("PRAGMA integrity_check") == "ok"
                assert db.execute("SELECT * FROM t ORDER BY rowid") == fixture
                item = report["implementations"][name]
                item["medians"] = {key: statistics.median(values) for key, values in item["timings"].items()}
            assert report["implementations"]["before"]["column_frames_sha256"] == report["implementations"]["after"]["column_frames_sha256"]
        finally:
            for db in handles.values():
                db.close()
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({name: item["medians"] for name, item in report["implementations"].items()}, indent=2))


if __name__ == "__main__":
    main()
