"""Record real SQL work, storage and avoided decoding on native column tables."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import time

from db import Database, ROOT


def stats(db):
    return json.loads(db.scalar("SELECT sqlnew_columnar_stats('events')"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=1000000)
    parser.add_argument("--database", type=Path, default=ROOT / "build/events-columnar.sqlnew")
    parser.add_argument("--output", type=Path, default=ROOT / "sqlnew/results/columnar.json")
    args = parser.parse_args()
    if args.rows < 10000:
        parser.error("at least 10000 rows required")
    if args.database.exists():
        parser.error("choose a fresh --database path")
    args.database.parent.mkdir(parents=True, exist_ok=True)
    result = {"timestamp_utc": datetime.now(timezone.utc).isoformat(),
              "platform": platform.platform(), "rows": args.rows,
              "machine": platform.machine(), "processor": platform.processor(),
              "logical_cpus": os.cpu_count(),
              "scope": "This engine only. SQL ingestion, query latency, physical file and execution counters.",
              "workload": "Deterministic event IDs and times, 257 sensors, varying real readings, unique formatted messages.",
              "limitations": ["Warm single-connection queries; no throughput or cold-cache guarantee.",
                             "Logical bytes are raw typed values, not another database's file size.",
                             "Range pruning benefits from correlation between values and row groups.",
                             "This fixture measures summaries, a range, and a point read; grouped paths are measured by benchmark_v2.py."]}
    result["source_sha256"] = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in ("ext/sqlnew/columnar.c", "ext/sqlnew/columnar_batch.inc", "ext/sqlnew/columnar_index.inc",
                     "ext/sqlnew/columnar_write.inc", "ext/sqlnew/sqlnew.c", "engine/sqlite/src/select.c",
                     "sqlnew/config.h", "sqlnew/build.py", "sqlnew/db.py", "sqlnew/exercise_columnar.py")}
    with Database(args.database) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("""CREATE VIRTUAL TABLE events USING sqlnew_columnar(
            seq INTEGER,ts INTEGER,sensor INTEGER,reading REAL,message TEXT)""")
        started = time.perf_counter()
        db.execute("""WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<?)
            INSERT INTO events
            SELECT x,1700000000000+x*1000,(x*13)%257,((x*7919)%1000000)/100.0,
              printf('device=%03d event=%d reading=%d status=ok',(x*13)%257,x,(x*7919)%1000000)
            FROM n""", (args.rows,))
        result["ingest_seconds"] = time.perf_counter() - started
        result["product_version"] = db.scalar("SELECT sqlnew_version()")
        result["cpu_backend"] = db.scalar("SELECT sqlnew_backend()")
        result["groups"] = db.scalar("SELECT count(*) FROM events_blocks")
        result["compressed_column_bytes"] = db.scalar("SELECT sum(length(data)) FROM events_data")
        # Independent size calculation of the generated typed values.
        result["logical_value_bytes"] = args.rows * 32 + sum(
            len(f"device={(x * 13) % 257:03d} event={x} reading={(x * 7919) % 1000000} status=ok".encode())
            for x in range(1, args.rows + 1))
        queries = [
            ("summary", "SELECT count(*),sum(seq),min(ts),max(ts) FROM events", (),
             [(args.rows, args.rows * (args.rows + 1) // 2,
               1700000001000, 1700000000000 + args.rows * 1000)]),
            ("time_slice", "SELECT seq FROM events WHERE ts BETWEEN ? AND ? ORDER BY seq",
             (1700000000000 + (args.rows // 2) * 1000, 1700000000000 + (args.rows // 2 + 99) * 1000),
             [(x,) for x in range(args.rows // 2, args.rows // 2 + 100)]),
            ("point", "SELECT seq,sensor,message FROM events WHERE rowid=?", (args.rows - 17,),
             [(args.rows - 17, ((args.rows - 17) * 13) % 257,
               f"device={((args.rows - 17) * 13) % 257:03d} event={args.rows - 17} reading={((args.rows - 17) * 7919) % 1000000} status=ok")]),
        ]
        result["queries"] = {}
        for name, sql, parameters, expected in queries:
            before = stats(db)
            assert db.execute(sql, parameters) == expected
            after = stats(db)
            counters = {key: after[key] - before[key] for key in before}
            samples = []
            for _ in range(11):
                started = time.perf_counter()
                assert db.execute(sql, parameters) == expected
                samples.append((time.perf_counter() - started) * 1000)
            result["queries"][name] = {
                "sql": sql, "parameters": parameters,
                "median_ms": statistics.median(samples), "samples_ms": samples,
                "work_per_query": counters, "plan": db.execute("EXPLAIN QUERY PLAN " + sql, parameters)}
        assert result["queries"]["summary"]["work_per_query"]["columns_decoded"] == 0
        assert result["queries"]["summary"]["work_per_query"]["rows_emitted"] == 0
        assert result["queries"]["time_slice"]["work_per_query"]["blocks_read"] <= 2
        result["integrity_check"] = db.execute("PRAGMA integrity_check")
        assert result["integrity_check"] == [("ok",)]
        result["checkpoint"] = db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    result["database_bytes"] = args.database.stat().st_size
    with Database(args.database) as db:
        assert db.scalar("SELECT sum(seq) FROM events") == args.rows * (args.rows + 1) // 2
        result["reopen_verified"] = True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"{args.rows:,} rows; ingest {result['ingest_seconds']:.3f} seconds")
    print(f"Logical values {result['logical_value_bytes']:,} bytes; full file {result['database_bytes']:,} bytes")
    for name, query in result["queries"].items():
        print(f"{name}: {query['median_ms']:.3f} ms; {query['work_per_query']}")
    print("Integrity and reopened aggregate verified")


if __name__ == "__main__":
    main()
