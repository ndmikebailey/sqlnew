"""Measure this engine's integer blocks against their mathematical contract."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import random
import statistics
import time

from db import Database, ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=131072)
    parser.add_argument("--block-rows", type=int, default=1024)
    parser.add_argument("--database", type=Path, default=ROOT / "build/columns.sqlnew")
    parser.add_argument("--output", type=Path, default=ROOT / "sqlnew/results/columns.json")
    args = parser.parse_args()
    if args.rows < 1 or not 1 <= args.block_rows <= 65536:
        parser.error("positive row count and block-rows 1..65536 required")
    if args.database.exists():
        parser.error("database already exists; choose a fresh --database path")
    args.database.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(20260905)
    workloads = {
        "constant": ([123456789] * args.rows, (123456789, 123456789)),
        "all_null": ([None] * args.rows, (-10, 10)),
        "narrow_nullable": ([None if i % 13 == 0 else 1000000 + rng.randrange(16)
                             for i in range(args.rows)], (1000004, 1000007)),
        "sequential": (list(range(args.rows)), (args.rows // 3, args.rows // 3 + 99)),
        "full_int64": ([rng.getrandbits(64) - (1 << 63) for _ in range(args.rows)],
                       (-(1 << 61), (1 << 61) - 1)),
    }
    report = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(), "product": "SQLNEW", "seed": 20260905,
        "rows_per_workload": args.rows, "block_rows": args.block_rows,
        "scope": "This engine only; warm SQL scans; no upstream build comparison.",
        "limitations": ["Explicit blocks, not automatic columnar table storage.",
                        "Packing time includes Python JSON serialization and SQL ingestion.",
                        "Every scan validates the whole block before applying predicates.",
                        "Logical int64 bytes are an encoding reference, not another database's size."],
        "workloads": {},
    }
    with Database(args.database) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        report["version"] = db.scalar("SELECT sqlnew_version()")
        report["backend"] = db.scalar("SELECT sqlnew_backend()")
        report["engine_source_id"] = db.scalar("SELECT sqlite_source_id()")
        db.execute("CREATE TABLE sealed(kind TEXT,block_id INTEGER,data BLOB NOT NULL,PRIMARY KEY(kind,block_id)) WITHOUT ROWID")
        for kind, (values, bounds) in workloads.items():
            started = time.perf_counter()
            with db.transaction():
                db.executemany("""INSERT INTO sealed
                    SELECT ?,?,sqlnew_int_pack(value ORDER BY key) FROM json_each(?)""",
                    ((kind, start // args.block_rows, json.dumps(values[start:start + args.block_rows]))
                     for start in range(0, len(values), args.block_rows)))
            packing_ms = (time.perf_counter() - started) * 1000
            nonnull = [value for value in values if value is not None]
            expected = (len(values), len(nonnull), min(nonnull) if nonnull else None,
                        max(nonnull) if nonnull else None)
            actual = db.execute("""SELECT count(*),count(i.value),min(i.value),max(i.value)
                FROM sealed s CROSS JOIN sqlnew_int_scan(s.data) i WHERE s.kind=?""", (kind,))[0]
            assert actual == expected, (kind, actual, expected)
            expected_count = sum(bounds[0] <= value <= bounds[1] for value in nonnull)
            query = """SELECT count(*) FROM sealed s CROSS JOIN sqlnew_int_scan(s.data) i
                WHERE s.kind=? AND i.value BETWEEN ? AND ?"""
            parameters = (kind, *bounds)
            assert db.scalar(query, parameters) == expected_count
            samples = []
            for _ in range(9):
                started = time.perf_counter()
                assert db.scalar(query, parameters) == expected_count
                samples.append((time.perf_counter() - started) * 1000)
            # Only check SUM when the mathematical result fits SQL's int64 result.
            total = sum(nonnull) if nonnull else None
            if kind != "full_int64" and (total is None or -(1 << 63) <= total < (1 << 63)):
                assert db.scalar("""SELECT sum(i.value) FROM sealed s
                    CROSS JOIN sqlnew_int_scan(s.data) i WHERE s.kind=?""", (kind,)) == total
            stored = db.scalar("SELECT sum(length(data)) FROM sealed WHERE kind=?", (kind,))
            report["workloads"][kind] = {
                "non_null_rows": len(nonnull), "frame_bytes": stored,
                "logical_non_null_int64_bytes": len(nonnull) * 8,
                "frame_bits_per_row": stored * 8 / len(values), "packing_ms": packing_ms,
                "predicate": {"lower": bounds[0], "upper": bounds[1], "matched": expected_count},
                "query_ms": {"median": statistics.median(samples), "samples": samples},
            }
        report["integrity_check"] = db.scalar("PRAGMA integrity_check")
        assert report["integrity_check"] == "ok"
        report["checkpoint"] = db.execute("PRAGMA wal_checkpoint(TRUNCATE)")[0]
    with args.database.open("rb") as file:
        assert file.read(16) == b"SQLNEW format 2\0"
    report["database_bytes"] = args.database.stat().st_size
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for kind, value in report["workloads"].items():
        print(f"{kind}: {value['frame_bits_per_row']:.4f} bits/row including headers; "
              f"range count {value['query_ms']['median']:.3f} ms")
    print(f"Integrity {report['integrity_check']}; database {report['database_bytes']} bytes")


if __name__ == "__main__":
    main()
