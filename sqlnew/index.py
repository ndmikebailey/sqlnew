"""Transactional block/cluster vector index and compressed hybrid document store.

NumPy is used only for vector input and offline centroid training. Query scoring,
block packing, payload compression, and top-k run in the fork's native extension.
"""
import json
import math
import re
import struct
import numpy as np


def vector(value, dimensions=None):
    result = np.asarray(value, dtype="<f4")
    if result.ndim != 1 or not 1 <= result.size <= 4096 or not np.isfinite(result).all():
        raise ValueError("vector must have 1..4096 finite float32 coordinates")
    if dimensions is not None and result.size != dimensions:
        raise ValueError("vector dimension does not match the index")
    return result.tobytes()


def id_blob(ids):
    return b"".join(struct.pack("<q", int(i)) for i in sorted(set(ids)))


class VectorIndex:
    def __init__(self, db, name="documents"):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", name):
            raise ValueError("index name must be a simple ASCII identifier")
        self.db, self.name = db, "sn_" + name

    def create(self, dimensions, model, quantized=True):
        if not isinstance(dimensions, int) or not 1 <= dimensions <= 4096:
            raise ValueError("dimensions must be 1..4096")
        if not isinstance(model, str) or not model:
            raise ValueError("an embedding model/revision identifier is required")
        p, db = self.name, self.db
        with db.transaction():
            db.execute(f"CREATE TABLE {p}_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID")
            db.executemany(f"INSERT INTO {p}_meta VALUES (?,?)", [(k, json.dumps(v)) for k, v in {
                "dimensions": dimensions, "model": model, "quantized": bool(quantized),
                "metric": "squared_l2", "format": 1, "generation": 0,
            }.items()])
            db.execute(f"CREATE TABLE {p}_rows(id INTEGER PRIMARY KEY,vector BLOB NOT NULL,body BLOB NOT NULL,payload BLOB)")
            db.execute(f"CREATE TABLE {p}_delta(id INTEGER PRIMARY KEY)")
            db.execute(f"CREATE TABLE {p}_centroids(cluster INTEGER PRIMARY KEY,vector BLOB NOT NULL,radius REAL NOT NULL)")
            db.execute(f"CREATE TABLE {p}_membership(id INTEGER PRIMARY KEY,cluster INTEGER NOT NULL,blockno INTEGER NOT NULL)")
            db.execute(f"CREATE INDEX {p}_membership_cluster ON {p}_membership(cluster,blockno,id)")
            db.execute(f"CREATE TABLE {p}_blocks(cluster INTEGER NOT NULL,blockno INTEGER NOT NULL,codes BLOB NOT NULL,PRIMARY KEY(cluster,blockno)) WITHOUT ROWID")
            db.execute(f"CREATE VIRTUAL TABLE {p}_text USING fts5(body,content='')")
            db.execute(f"""CREATE TRIGGER {p}_insert AFTER INSERT ON {p}_rows BEGIN
                INSERT INTO {p}_delta SELECT new.id WHERE NOT EXISTS(SELECT 1 FROM {p}_delta WHERE id=new.id);
                INSERT INTO {p}_text(rowid,body) VALUES(new.id,CAST(sqlnew_unpack(new.body) AS TEXT));
                END""")
            db.execute(f"""CREATE TRIGGER {p}_delete AFTER DELETE ON {p}_rows BEGIN
                INSERT INTO {p}_delta SELECT old.id WHERE NOT EXISTS(SELECT 1 FROM {p}_delta WHERE id=old.id);
                INSERT INTO {p}_text({p}_text,rowid,body) VALUES('delete',old.id,CAST(sqlnew_unpack(old.body) AS TEXT));
                END""")
            db.execute(f"""CREATE TRIGGER {p}_update AFTER UPDATE ON {p}_rows BEGIN
                INSERT INTO {p}_delta SELECT old.id WHERE NOT EXISTS(SELECT 1 FROM {p}_delta WHERE id=old.id);
                INSERT INTO {p}_delta SELECT new.id WHERE NOT EXISTS(SELECT 1 FROM {p}_delta WHERE id=new.id);
                INSERT INTO {p}_text({p}_text,rowid,body) VALUES('delete',old.id,CAST(sqlnew_unpack(old.body) AS TEXT));
                INSERT INTO {p}_text(rowid,body) VALUES(new.id,CAST(sqlnew_unpack(new.body) AS TEXT));
                END""")
        return self

    def metadata(self):
        return {k: json.loads(v) for k, v in self.db.execute(f"SELECT key,value FROM {self.name}_meta")}

    def add(self, rows):
        """Upsert (integer ID, vector, body text, optional payload bytes) rows."""
        with self.db.transaction():
            dimensions = self.metadata()["dimensions"]
            def values():
                for rowid, values, body, payload in rows:
                    if not isinstance(rowid, int) or not isinstance(body, str):
                        raise ValueError("ID must be an integer and body must be text")
                    yield rowid, vector(values, dimensions), body, payload
            self.db.executemany(f"""INSERT INTO {self.name}_rows VALUES(?,?,sqlnew_pack(?),sqlnew_pack(?))
                ON CONFLICT(id) DO UPDATE SET vector=excluded.vector,body=excluded.body,payload=excluded.payload""", values())

    def delete(self, ids):
        with self.db.transaction():
            self.db.executemany(f"DELETE FROM {self.name}_rows WHERE id=?", ((int(i),) for i in ids))

    def document(self, rowid):
        rows = self.db.execute(f"SELECT CAST(sqlnew_unpack(body) AS TEXT),sqlnew_unpack(payload) FROM {self.name}_rows WHERE id=?", (rowid,))
        return rows[0] if rows else None

    @staticmethod
    def _assign(matrix, centers):
        assignments = np.empty(len(matrix), dtype=np.int32)
        c = centers.astype(np.float64)
        norms = (c * c).sum(axis=1)
        for start in range(0, len(matrix), 1024):
            batch = matrix[start:start + 1024].astype(np.float64)
            distances = (batch * batch).sum(axis=1)[:, None] + norms[None, :] - 2 * (batch @ c.T)
            assignments[start:start + len(batch)] = distances.argmin(axis=1)
        return assignments

    def rebuild(self, clusters=32, block_rows=256, iterations=8, seed=20260905):
        """Atomically seal all rows; bounded training sample, full dataset in RAM.

        This is an offline rebuild that holds the writer for its transaction.
        Search visibility remains correct before and after it, including rollback.
        """
        if not 1 <= clusters <= 4096 or not 1 <= block_rows <= 2048 or not 1 <= iterations <= 100:
            raise ValueError("invalid clustering/block parameters")
        p, db = self.name, self.db
        with db.transaction():
            meta = self.metadata()
            # Acquire the writer before taking a training snapshot.
            db.execute(f"UPDATE {p}_meta SET value=value WHERE key='generation'")
            rows = db.execute(f"SELECT id,vector FROM {p}_rows ORDER BY id")
            db.execute(f"DELETE FROM {p}_blocks")
            db.execute(f"DELETE FROM {p}_centroids")
            db.execute(f"DELETE FROM {p}_membership")
            if rows:
                matrix = np.stack([np.frombuffer(b, dtype="<f4") for _, b in rows])
                if matrix.shape[1] != meta["dimensions"] or not np.isfinite(matrix).all():
                    raise ValueError("invalid stored vector")
                rng = np.random.default_rng(seed)
                sample = matrix[rng.choice(len(matrix), min(len(matrix), 4096), replace=False)]
                clusters = min(clusters, len(sample))
                centers = sample[rng.choice(len(sample), clusters, replace=False)].copy()
                for _ in range(iterations):
                    labels = self._assign(sample, centers)
                    for cluster in range(clusters):
                        selected = sample[labels == cluster]
                        if len(selected):
                            centers[cluster] = selected.astype(np.float64).mean(axis=0)
                labels = self._assign(matrix, centers)
                mapping, centroid_rows = [], []
                # Inflate radii conservatively for finite binary32 coordinates
                # evaluated with binary64 arithmetic, with no fast-math.
                guard = 64 * meta["dimensions"] * np.finfo(np.float64).eps
                for cluster in range(clusters):
                    members = np.flatnonzero(labels == cluster)
                    differences = matrix[members].astype(np.float64) - centers[cluster].astype(np.float64)
                    squared = np.sum(differences * differences, axis=1)
                    radius = math.nextafter(math.sqrt(float(squared.max())) * (1 + guard), math.inf) if len(members) else 0.0
                    centroid_rows.append((cluster, vector(centers[cluster]), radius))
                    mapping.extend((rows[int(idx)][0], cluster, pos // block_rows) for pos, idx in enumerate(members))
                db.executemany(f"INSERT INTO {p}_centroids VALUES(?,?,?)", centroid_rows)
                db.executemany(f"INSERT INTO {p}_membership VALUES(?,?,?)", mapping)
                db.execute(f"""INSERT INTO {p}_blocks
                    SELECT m.cluster,m.blockno,sqlnew_pack(sqlnew_vector_block(r.id,r.vector,? ORDER BY r.id))
                    FROM {p}_rows r JOIN {p}_membership m ON m.id=r.id GROUP BY m.cluster,m.blockno""", (int(meta["quantized"]),))
            db.execute(f"DELETE FROM {p}_delta")
            db.execute(f"UPDATE {p}_meta SET value=? WHERE key='generation'", (json.dumps(meta["generation"] + 1),))

    def _exact(self, q, k, included, meta, geometric):
        p, db = self.name, self.db
        optional_sql = ",?" if included is not None else ""
        optional_args = (included,) if included is not None else ()
        selected = []

        def scan(source, source_args=(), grouping="(r.id >> 8)"):
            sql = f"""WITH blocks AS NOT MATERIALIZED (
                SELECT sqlnew_vector_block(r.id,r.vector,0 ORDER BY r.id) codes FROM {source} GROUP BY {grouping})
                SELECT t.id,t.distance FROM blocks b CROSS JOIN sqlnew_topk(b.codes,?,?,?{optional_sql}) t
                ORDER BY t.distance,t.id LIMIT ?"""
            selected.extend(db.execute(sql, (*source_args, q, k, b"", *optional_args, k)))
            selected.sort(key=lambda h: (h[1], h[0]))
            del selected[k:]

        if not geometric:
            scan(f"{p}_rows r")
            return selected, 0, 0
        # Dirty rows use originals and never depend on a stale cluster radius.
        scan(f"{p}_rows r JOIN {p}_delta d ON r.id=d.id")
        guard = 64 * meta["dimensions"] * np.finfo(np.float64).eps
        bounds = []
        for cluster, distance, radius in db.execute(f"SELECT cluster,sqlnew_l2(vector,?),radius FROM {p}_centroids", (q,)):
            lower = max(0.0, math.nextafter(math.sqrt(distance) * (1 - guard) - radius, -math.inf))
            bounds.append((lower, cluster))
        bounds.sort()
        visited = 0
        for lower, cluster in bounds:
            if len(selected) == k:
                upper = math.nextafter(math.sqrt(selected[-1][1]) * (1 + guard), math.inf)
                if lower > upper:
                    break
            scan(f"""{p}_membership m JOIN {p}_rows r ON r.id=m.id
                WHERE m.cluster=? AND NOT EXISTS(SELECT 1 FROM {p}_delta d WHERE d.id=r.id)""", (cluster,), "m.blockno")
            visited += 1
        return selected, visited, len(bounds) - visited

    def search(self, query, k=10, probes=None, rerank=4, exact=False, eligible_ids=None, geometric=True):
        """Exact searches scan originals; approximate searches use sealed codes.

        probes=None scans all clusters but quantized codes remain approximate.
        Reranking improves scores inside the candidate set; it cannot restore
        candidates missed by clustering or quantization. Filters precede top-k.
        """
        if not isinstance(k, int) or not 1 <= k <= 65536 or not isinstance(rerank, int) or rerank < 1 or (not exact and k * rerank > 65536):
            raise ValueError("invalid k or rerank budget")
        if probes is not None and (not isinstance(probes, int) or probes < 1):
            raise ValueError("probes must be a positive integer or None")
        with self.db.transaction():
            p, db, meta = self.name, self.db, self.metadata()
            q = vector(query, meta["dimensions"])
            included = id_blob(eligible_ids) if eligible_ids is not None else None
            if exact:
                hits, visited, pruned = self._exact(q, k, included, meta, geometric)
                return {"hits": hits, "exact": True, "clusters_probed": visited, "clusters_pruned": pruned,
                        "generation": meta["generation"], "candidate_budget": k, "model": meta["model"]}
            optional_sql = ",?" if included is not None else ""
            optional_args = (included,) if included is not None else ()
            budget = k if exact else k * rerank
            delta = b"" if exact else id_blob(i for (i,) in db.execute(f"SELECT id FROM {p}_delta ORDER BY id"))
            candidates = []
            block_count = 0
            if not exact:
                nclusters = db.scalar(f"SELECT count(*) FROM {p}_centroids")
                selected = nclusters if probes is None else min(nclusters, probes)
                sql = f"""WITH chosen AS MATERIALIZED (
                    SELECT cluster FROM {p}_centroids ORDER BY sqlnew_l2(vector,?),cluster LIMIT ?)
                    SELECT t.id,t.distance FROM {p}_blocks b JOIN chosen c USING(cluster)
                    CROSS JOIN sqlnew_topk(sqlnew_unpack(b.codes),?,?,?{optional_sql}) t
                    ORDER BY t.distance,t.id LIMIT ?"""
                candidates = db.execute(sql, (q, selected, q, budget, delta, *optional_args, budget))
                block_count = selected
            source = f"{p}_rows r" if exact else f"{p}_rows r JOIN {p}_delta d ON r.id=d.id"
            sql = f"""WITH blocks AS NOT MATERIALIZED (
                SELECT sqlnew_vector_block(r.id,r.vector,0 ORDER BY r.id) AS codes FROM {source} GROUP BY (r.id >> 8))
                SELECT t.id,t.distance FROM blocks b CROSS JOIN sqlnew_topk(b.codes,?,?,?{optional_sql}) t
                ORDER BY t.distance,t.id LIMIT ?"""
            candidates += db.execute(sql, (q, budget, b"", *optional_args, budget))
            candidates.sort(key=lambda hit: (hit[1], hit[0]))
            candidates = candidates[:budget]
            if not exact and candidates:
                # Reuse one prepared statement to load and score only candidates.
                # JSON avoids a variable-count SQL statement and bind limit.
                ids = json.dumps([i for i, _ in candidates])
                candidates = db.execute(f"""SELECT id,sqlnew_l2(vector,?) AS distance FROM {p}_rows
                    WHERE id IN (SELECT value FROM json_each(?)) ORDER BY distance,id LIMIT ?""", (q, ids, k))
            return {"hits": candidates[:k], "exact": bool(exact), "clusters_probed": block_count,
                    "generation": meta["generation"], "candidate_budget": budget, "model": meta["model"]}

    def hybrid(self, query, text, k=10, probes=None, eligible_ids=None):
        """Reciprocal-rank fusion of eligible text and vector candidates."""
        if not isinstance(k, int) or not 1 <= k <= 4096:
            raise ValueError("hybrid k must be 1..4096")
        with self.db.transaction():
            eligible = None if eligible_ids is None else sorted(set(eligible_ids))
            semantic = self.search(query, k=k * 4, probes=probes, eligible_ids=eligible)
            condition = "" if eligible is None else "AND rowid IN (SELECT value FROM json_each(?))"
            parameters = (text,) + (() if eligible is None else (json.dumps(eligible),)) + (k * 4,)
            lexical = self.db.execute(f"SELECT rowid FROM {self.name}_text WHERE {self.name}_text MATCH ? {condition} ORDER BY rank,rowid LIMIT ?", parameters)
            scores = {}
            for stream in ([i for i, _ in semantic["hits"]], [i for (i,) in lexical]):
                for rank, rowid in enumerate(stream, 1):
                    scores[rowid] = scores.get(rowid, 0.0) + 1.0 / (60 + rank)
            return sorted(scores.items(), key=lambda hit: (-hit[1], hit[0]))[:k]
