"""Pinned public workloads, independent Arrow answers, and typed input hashes."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
import urllib.request

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv

from build import BUILD

BASE = BUILD / "comparison"
H2O = "c7421051af7530951d16b1505158371aebc0d2c1"
DSQ = "c3ae0bafb0c3283e3c98cb250ada5a19e79ad58e"
INPUT_SQL = "SELECT * FROM t ORDER BY rowid"
RESULT_SQL = "SELECT * FROM temp.bench_result ORDER BY rowid"


def digest(path):
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def fetch(url, path):
    path = Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".partial")
        with urllib.request.urlopen(url, timeout=60) as src, temporary.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        temporary.replace(path)
    return {"url": url, "bytes": path.stat().st_size, "sha256": digest(path)}


def typed_hash(table, sql):
    """Independent implementation of SQLite shathree.c's documented framing."""
    text = sql.encode()
    h = hashlib.sha3_256(f"S{len(text)}:".encode() + text)
    kinds = ["I" if pa.types.is_integer(f.type) else "F" if pa.types.is_floating(f.type) else "T"
             for f in table.schema]
    for batch in table.to_batches(max_chunksize=32768):
        columns = [c.to_pylist() for c in batch.columns]
        output = bytearray()
        for row in zip(*columns):
            output += b"R"
            for kind, value in zip(kinds, row):
                if value is None:
                    output += b"N"
                elif kind == "I":
                    output += b"I" + struct.pack(">q", value)
                elif kind == "F":
                    output += b"F" + struct.pack(">d", value + 0.0)
                else:
                    encoded = value.encode()
                    output += f"T{len(encoded)}:".encode() + encoded
        h.update(output)
    return h.hexdigest().upper()


def rows(table):
    return list(zip(*(c.to_pylist() for c in table.columns)))


def sorted_rows(table, key_columns):
    return sorted(rows(table), key=lambda row: tuple((v is not None, v) for v in row[:key_columns]))


def grouped(table, keys, aggregates):
    return table.group_by(keys, use_threads=False).aggregate(aggregates)


def answer(name, sql, expected, source="SQLNEW diagnostic"):
    return {"name": name, "sql": sql, "source": source, "expected": expected}


def h2o_dataset(n):
    k = 100
    rng = np.random.Generator(np.random.PCG64(108))
    data = {}
    for name, cardinality, width in (("id1", k, 3), ("id2", k, 3), ("id3", n // k, 10)):
        labels = np.array([f"id{i:0{width}d}" for i in range(1, cardinality + 1)])
        data[name] = labels[rng.integers(0, cardinality, n)]
    for name, cardinality in (("id4", k), ("id5", k), ("id6", n // k), ("v1", 5), ("v2", 15)):
        data[name] = rng.integers(1, cardinality + 1, n, dtype=np.int64)
    data["v3"] = np.round(rng.uniform(0, 100, n), 6)
    table = pa.table(data)
    queries = []
    source = "H2O.ai db-benchmark groupby (adapted)"
    definitions = [
        ("h2o_q1", ["id1"], [("v1", "sum")], "id1,sum(v1) AS v1"),
        ("h2o_q2", ["id1", "id2"], [("v1", "sum")], "id1,id2,sum(v1) AS v1"),
        ("h2o_q3", ["id3"], [("v1", "sum"), ("v3", "mean")], "id3,sum(v1) AS v1,avg(v3) AS v3"),
        ("h2o_q4", ["id4"], [("v1", "mean"), ("v2", "mean"), ("v3", "mean")],
         "id4,avg(v1) AS v1,avg(v2) AS v2,avg(v3) AS v3"),
        ("h2o_q5", ["id6"], [("v1", "sum"), ("v2", "sum"), ("v3", "sum")],
         "id6,sum(v1) AS v1,sum(v2) AS v2,sum(v3) AS v3"),
    ]
    for name, keys, aggs, projection in definitions:
        keys_sql = ",".join(keys)
        sql = f"SELECT {projection} FROM t GROUP BY {keys_sql} ORDER BY {keys_sql}"
        queries.append(answer(name, sql, sorted_rows(grouped(table, keys, aggs), len(keys)), source))
    g = grouped(table, ["id3"], [("v1", "max"), ("v2", "min")])
    expected = sorted((key, hi - lo) for key, hi, lo in rows(g))
    queries.append(answer("h2o_q7", "SELECT id3,max(v1)-min(v2) AS value FROM t GROUP BY id3 ORDER BY id3", expected, source))
    order = np.lexsort((-data["v3"], data["id6"]))
    ids = data["id6"][order]
    starts = np.r_[True, ids[1:] != ids[:-1]]
    rank = np.arange(n) - np.maximum.accumulate(np.where(starts, np.arange(n), 0))
    chosen = order[rank < 2]
    expected = list(zip(data["id6"][chosen].tolist(), data["v3"][chosen].tolist()))
    queries.append(answer("h2o_q8", "SELECT id6,v3 FROM (SELECT id6,v3,row_number() OVER "
                          "(PARTITION BY id6 ORDER BY v3 DESC) AS rank FROM t WHERE v3 IS NOT NULL) "
                          "WHERE rank<=2 ORDER BY id6,v3 DESC", expected, source))
    keys = ["id1", "id2", "id3", "id4", "id5", "id6"]
    g = grouped(table, keys, [("v3", "sum"), ("v3", "count")]).sort_by([(key, "ascending") for key in keys])
    # With these independent keys, each generated row should have a unique key.
    # Refuse a hash-only float check if summation order could become relevant.
    assert pc.max(g["v3_count"]).as_py() == 1
    queries.append({"name": "h2o_q10", "source": source,
                    "sql": "SELECT id1,id2,id3,id4,id5,id6,sum(v3) AS v3,count(*) AS n FROM t "
                           "GROUP BY id1,id2,id3,id4,id5,id6 ORDER BY id1,id2,id3,id4,id5,id6",
                    "expected_hash": typed_hash(g, RESULT_SQL), "expected_rows": n})
    queries.append(answer("summary", "SELECT count(*) AS n,sum(v1) AS s,min(id4) AS lo,max(id4) AS hi FROM t",
                          [(n, int(data["v1"].sum()), int(data["id4"].min()), int(data["id4"].max()))]))
    return table, queries, {
        "description": "H2O group cardinalities and value distributions; NumPy PCG64 seed 108, not R-identical rows; K=100, no NULLs, unsorted.",
        "adaptations": ["1,000,000-row default instead of the original minimum 10,000,000; configurable.",
                        "Portable SELECTs materialized into a temporary table; deterministic result ordering added.",
                        "q6 (median/stddev) and q9 (correlation) are reported unsupported by both SQLite configurations and SQLNEW."],
        "unsupported": {"h2o_q6": "quantile_cont and stddev are unavailable", "h2o_q9": "corr is unavailable"},
        "indexes": ["CREATE INDEX tuned_id12 ON t(id1,id2,v1)",
                    "CREATE INDEX tuned_id3 ON t(id3,v1,v2,v3)",
                    "CREATE INDEX tuned_id4 ON t(id4,v1,v2,v3)",
                    "CREATE INDEX tuned_id6 ON t(id6,v1,v2,v3)",
                    "CREATE INDEX tuned_top2 ON t(id6,v3 DESC)"],
        "point_columns": ["id1", "v1", "v3"], "update_column": "v1"}


def taxi_dataset(archive):
    raw = BASE / "data/testdata/taxi.csv"
    if not raw.exists():
        members = subprocess.check_output(["tar", "-tf", str(archive)], text=True).splitlines()
        if members != ["testdata/taxi.csv"]:
            raise RuntimeError("unexpected taxi archive members")
        subprocess.run(["tar", "-xf", str(archive), "-C", str(BASE / "data"), "testdata/taxi.csv"], check=True)
    table = pacsv.read_csv(raw)
    fields = []
    for column, field in zip(table.columns, table.schema):
        if pa.types.is_timestamp(field.type):
            column = pc.cast(column, pa.int64())  # The pinned CSV has second resolution.
        if pa.types.is_floating(column.type):
            if pc.any(pc.invert(pc.is_finite(column))).as_py():
                raise ValueError("nonfinite taxi value requires an explicit conversion policy")
            column = pc.add(column, 0.0)  # Canonicalize negative zero for all engines.
        fields.append(column)
    table = pa.Table.from_arrays(fields, names=table.column_names)
    g = grouped(table, ["passenger_count"], [([], "count_all"), ("total_amount", "mean")])
    queries = [answer("dsq_taxi_groupby", "SELECT passenger_count,count(*) AS n,avg(total_amount) AS avg_total "
                      "FROM t GROUP BY passenger_count ORDER BY passenger_count", sorted_rows(g, 1),
                      "multiprocessio/dsq scripts/benchmark.sh")]
    queries.append(answer("summary", "SELECT count(*) AS n,sum(passenger_count) AS passengers,"
                          "min(tpep_pickup_datetime) AS lo,max(tpep_pickup_datetime) AS hi FROM t",
                          [(len(table), pc.sum(table["passenger_count"]).as_py(),
                            pc.min(table["tpep_pickup_datetime"]).as_py(), pc.max(table["tpep_pickup_datetime"]).as_py())]))
    low = int(datetime(2021, 4, 15, 12, tzinfo=timezone.utc).timestamp())
    high = low + 7200
    selected = table.filter(pc.and_(pc.greater_equal(table["tpep_pickup_datetime"], low),
                                   pc.less(table["tpep_pickup_datetime"], high)))
    queries.append(answer("time_range", f"SELECT count(*) AS n,sum(total_amount) AS total FROM t "
                          f"WHERE tpep_pickup_datetime>={low} AND tpep_pickup_datetime<{high}",
                          [(len(selected), pc.sum(selected["total_amount"]).as_py())]))
    g = grouped(table, ["payment_type"], [([], "count_all"), ("fare_amount", "sum"), ("trip_distance", "mean")])
    queries.append(answer("payment_groupby", "SELECT payment_type,count(*) AS n,sum(fare_amount) AS fare,"
                          "avg(trip_distance) AS distance FROM t GROUP BY payment_type ORDER BY payment_type", sorted_rows(g, 1)))
    return table, queries, {
        "description": "Complete pinned GitHub taxi fixture: April 2021 NYC yellow taxi, all 18 columns and 2,171,187 rows in source order.",
        "raw_csv_sha256": digest(raw),
        "adaptations": ["Timestamps become integer Unix seconds; numeric and text values and NULLs retained; signed zero canonicalized.",
                        "Separate persistent load and warm query phases instead of the source's mixed CSV-import/end-to-end timings."],
        "unsupported": {},
        "indexes": ["CREATE INDEX tuned_passengers ON t(passenger_count,total_amount)",
                    "CREATE INDEX tuned_time ON t(tpep_pickup_datetime,total_amount)",
                    "CREATE INDEX tuned_payment ON t(payment_type,fare_amount,trip_distance)"],
        "point_columns": ["passenger_count", "total_amount", "tpep_pickup_datetime"], "update_column": "fare_amount"}


def save_dataset(name, table, queries, info):
    csv_path = BASE / "data" / (name + "-typed.csv")
    null_marker = chr(92) + "N"
    encoded = []
    for field, column in zip(table.schema, table.columns):
        if pa.types.is_string(field.type) and pc.any(pc.equal(column, null_marker)).as_py():
            raise ValueError("text collides with the CSV NULL marker")
        encoded.append(pc.fill_null(pc.cast(column, pa.string()), null_marker))
    # A dedicated marker preserves both empty TEXT and SQL NULL. All engines
    # read the same quoted CSV fields and perform the same declared casts.
    pacsv.write_csv(pa.Table.from_arrays(encoded, names=table.column_names), csv_path)
    schema = [(field.name, "INTEGER" if pa.types.is_integer(field.type) else "REAL" if pa.types.is_floating(field.type) else "TEXT")
              for field in table.schema]
    rng = np.random.Generator(np.random.PCG64(20260905))
    indices = rng.choice(len(table), 100, replace=False)
    selected = table.take(pa.array(indices))
    info.update(name=name, rows=len(table), schema=schema, null_marker=null_marker,
                csv=str(csv_path.resolve()), csv_sha256=digest(csv_path),
                logical_arrow_bytes=table.nbytes, input_hash=typed_hash(table, INPUT_SQL), queries=queries,
                points=[{"rowid": int(i) + 1,
                         "values": [selected[c][j].as_py() for c in info["point_columns"]],
                         "original_update": selected[info["update_column"]][j].as_py()}
                        for j, i in enumerate(indices)])
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--groupby-rows", type=int, default=1000000)
    args = parser.parse_args()
    if args.groupby_rows < 10000 or args.groupby_rows % 100:
        parser.error("groupby rows must be a multiple of 100 and at least 10000")
    (BASE / "data").mkdir(parents=True, exist_ok=True)
    sources = []
    for repo, commit, path in [("h2oai/db-benchmark", H2O, "_data/groupby-datagen.R"),
                               ("h2oai/db-benchmark", H2O, "duckdb/groupby-duckdb.R"),
                               ("multiprocessio/dsq", DSQ, "scripts/benchmark.sh")]:
        sources.append(fetch(f"https://raw.githubusercontent.com/{repo}/{commit}/{path}", BASE / "sources" / Path(path).name))
    archive = BASE / "data/taxi.csv.7z"
    sources.append(fetch(f"https://raw.githubusercontent.com/multiprocessio/dsq/{DSQ}/testdata/taxi.csv.7z", archive))
    datasets = []
    for name, create in (("groupby", lambda: h2o_dataset(args.groupby_rows)), ("taxi", lambda: taxi_dataset(archive))):
        print("Preparing", name, flush=True)
        table, queries, info = create()
        datasets.append(save_dataset(name, table, queries, info))
        print(name, len(table), "rows; independent answers and full typed hash prepared", flush=True)
        del table
    manifest = {"sources": sources, "datasets": datasets, "numpy": np.__version__, "pyarrow": pa.__version__,
                "oracle": "Independent Arrow/NumPy aggregates and Python SHA3 framing over every typed input value."}
    (BASE / "data.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
