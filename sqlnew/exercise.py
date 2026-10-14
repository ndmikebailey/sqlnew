"""Exercise this product only: synthetic correctness, latency, and file size."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import time

os.environ.setdefault("OPENBLAS_NUM_THREADS", "4")
os.environ.setdefault("OMP_NUM_THREADS", "4")
os.environ.setdefault("MKL_NUM_THREADS", "4")
import numpy as np

from db import Database, ROOT
from index import VectorIndex


def elapsed_ms(start):
    return (time.perf_counter() - start) * 1000


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=10000)
    parser.add_argument("--dimensions", type=int, default=128)
    parser.add_argument("--queries", type=int, default=25)
    parser.add_argument("--clusters", type=int, default=32)
    parser.add_argument("--probes", type=int, default=4)
    parser.add_argument("--output", type=Path, default=ROOT / "sqlnew/results/latest.json")
    parser.add_argument("--database", type=Path, default=ROOT / "build/demo.sqlnew")
    args = parser.parse_args()
    if args.rows < 10 or not 1 <= args.dimensions <= 4096 or args.queries < 1:
        parser.error("rows >= 10, dimensions 1..4096, and queries >= 1 required")
    if args.database.exists():
        parser.error("database already exists; choose a fresh --database path")
    args.database.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(20260905)
    centers = rng.normal(size=(64, args.dimensions)).astype(np.float32)
    centers /= np.linalg.norm(centers, axis=1)[:, None]
    matrix = centers[rng.integers(0, len(centers), args.rows)] + rng.normal(0, 0.06, size=(args.rows, args.dimensions)).astype(np.float32)
    matrix /= np.linalg.norm(matrix, axis=1)[:, None]
    queries = centers[rng.integers(0, len(centers), args.queries)] + rng.normal(0, 0.06, size=(args.queries, args.dimensions)).astype(np.float32)
    queries /= np.linalg.norm(queries, axis=1)[:, None]
    corpus = [f"Document {i}. Storage research topic {i % 64}. " + "Compressed local records and vector retrieval. " * 20 for i in range(args.rows)]
    result = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "product": "SQLNEW",
              "platform": platform.platform(), "processor": platform.processor(), "logical_cpus": os.cpu_count(),
              "workload": {"rows": args.rows, "dimensions": args.dimensions, "queries": args.queries,
                           "seed": 20260905, "data": "normalized synthetic 64-cluster Gaussian vectors; repeated text",
                           "clusters": args.clusters, "probes": args.probes, "k": 10, "rerank": 4},
              "scope": "This engine only. No upstream comparisons. Warm single-connection queries.",
              "limitations": ["Synthetic recall is not real-corpus relevance.", "Original vectors are retained in addition to quantized codes.",
                              "Timings include Python/SQL orchestration, not just native kernels.", "Rebuild is offline and holds the writer."]}
    with Database(args.database) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        result["backend"] = db.scalar("SELECT sqlnew_backend()")
        result["version"] = db.scalar("SELECT sqlnew_version()")
        result["engine_source_id"] = db.scalar("SELECT sqlite_source_id()")
        index = VectorIndex(db).create(args.dimensions, "synthetic/normalized-gaussian-v1")
        start = time.perf_counter()
        index.add((i, v, corpus[i], None) for i, v in enumerate(matrix))
        result["insert_ms"] = elapsed_ms(start)
        start = time.perf_counter()
        index.rebuild(clusters=args.clusters)
        result["rebuild_ms"] = elapsed_ms(start)
        result["stored_bytes"] = {
            "compressed_bodies": db.scalar("SELECT sum(length(body)) FROM sn_documents_rows"),
            "original_vectors": db.scalar("SELECT sum(length(vector)) FROM sn_documents_rows"),
            "sealed_code_frames": db.scalar("SELECT sum(length(codes)) FROM sn_documents_blocks"),
            "centroids": db.scalar("SELECT sum(length(vector)) FROM sn_documents_centroids"),
        }
        result["source_text_bytes"] = sum(len(s.encode()) for s in corpus)
        result["vector_blocks"] = db.scalar("SELECT count(*) FROM sn_documents_blocks")
        # Warm the same query path without contaminating the latency samples.
        index.search(queries[0], probes=args.probes)
        times, recall = [], []
        oracle_matrix = matrix.astype(np.float64)
        for query in queries:
            distances = np.sum((oracle_matrix - query) ** 2, axis=1)
            truth = set(np.lexsort((np.arange(args.rows), distances))[:10].tolist())
            start = time.perf_counter()
            hits = index.search(query, probes=args.probes)["hits"]
            times.append(elapsed_ms(start))
            recall.append(len(truth & {i for i, _ in hits}) / 10)
        # Exact-mode correctness is independent of the approximate query timing.
        for query in queries[:3]:
            truth = np.lexsort((np.arange(args.rows), np.sum((oracle_matrix - query) ** 2, axis=1)))[:10].tolist()
            assert [i for i, _ in index.search(query, exact=True)["hits"]] == truth
        result["query_ms"] = {"p50": float(np.percentile(times, 50)), "p95": float(np.percentile(times, 95)), "samples": times}
        result["recall_at_10"] = {"mean": float(np.mean(recall)), "minimum": min(recall), "samples": recall}
        result["integrity_check"] = db.scalar("PRAGMA integrity_check")
        assert result["integrity_check"] == "ok"
        assert index.document(0) == (corpus[0], None)
        result["checkpoint"] = db.execute("PRAGMA wal_checkpoint(TRUNCATE)")[0]
    with args.database.open("rb") as file:
        assert file.read(16) == b"SQLNEW format 2\0"
    result["database_bytes"] = args.database.stat().st_size
    result["database"] = str(args.database.relative_to(ROOT)) if args.database.is_relative_to(ROOT) else str(args.database)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key not in ("query_ms", "recall_at_10")}, indent=2))
    print(f"Query p50 {result['query_ms']['p50']:.3f} ms; p95 {result['query_ms']['p95']:.3f} ms; synthetic recall@10 {np.mean(recall):.3f}")


if __name__ == "__main__":
    main()
