"""Correctness checks for this fork. No upstream performance comparisons."""
import math
import json
import os
from pathlib import Path
import random
import struct
import tempfile
import unittest

import numpy as np

from db import Database, Error, ROOT
from index import VectorIndex, vector, id_blob


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()

    def tearDown(self):
        self.db.close()

    def pack(self, data):
        return self.db.scalar("SELECT sqlnew_pack(?)", (data,))

    def unpack(self, data):
        return self.db.scalar("SELECT sqlnew_unpack(?)", (data,))

    def block(self, rows, mode=0):
        self.db.execute("CREATE TEMP TABLE IF NOT EXISTS vectors(id INTEGER,v BLOB)")
        self.db.execute("DELETE FROM vectors")
        self.db.executemany("INSERT INTO vectors VALUES (?,?)", rows)
        return self.db.scalar("SELECT sqlnew_vector_block(id,v,? ORDER BY id) FROM vectors", (mode,))

    def test_checkout_version_and_builtin_registration(self):
        upstream = json.loads((ROOT/'sqlnew/upstream.json').read_text())
        self.assertEqual(self.db.scalar("SELECT sqlite_version()"), upstream['version'])
        timestamp = upstream['commit_time_utc'][:19].replace('T', ' ')
        self.assertTrue(self.db.scalar("SELECT sqlite_source_id()").startswith(timestamp))
        self.assertIn(self.db.scalar("SELECT sqlnew_backend()"), ("scalar-f64", "avx2-f64"))
        if os.environ.get("SQLNEW_DISABLE_SIMD") == "1":
            self.assertEqual(self.db.scalar("SELECT sqlnew_backend()"), "scalar-f64")

    def test_compression_roundtrip_and_size_bound(self):
        rng = random.Random(901)
        samples = [b"", b"x", b"abc" * 100000, bytes(range(256)) * 600, "hello \u0000世界".encode() * 2000]
        for length in [2, 3, 4, 127, 128, 129, 131, 255, 256, 4095, 4096, 65535, 65536, 65537, 1_000_000]:
            samples += [rng.randbytes(length), b"\xff" * length]
        for _ in range(100):
            motif = rng.randbytes(rng.randrange(1, 500))
            samples.append(motif * rng.randrange(1, 100) + rng.randbytes(37))
        for sample in samples:
            packed = self.pack(sample)
            self.assertEqual(self.unpack(packed), sample)
            self.assertLessEqual(len(packed), len(sample) + 16)
        self.assertLess(len(self.pack(b"abc" * 10000)), 2000)
        self.assertIsNone(self.pack(None))
        self.assertIsNone(self.unpack(None))

    def test_reject_corrupt_compression(self):
        packed = self.pack(b"abcdef" * 1000)
        for pos in range(len(packed)):
            corrupted = bytearray(packed)
            corrupted[pos] ^= 0x80
            with self.assertRaises(Error, msg=f"mutation offset {pos}"):
                self.unpack(corrupted)
        for end in range(len(packed)):
            with self.assertRaises(Error):
                self.unpack(packed[:end])
        with self.assertRaises(Error):
            self.unpack(packed + b"trailing")
        for blob in (b"SNZ1" + struct.pack("<III", 0xffffffff, 0, 1), b"SNZ1" + struct.pack("<III", 100, 0, 1) + b"\x80\0\0"):
            with self.assertRaises(Error):
                self.unpack(blob)

    def test_scalar_simd_distance_oracle(self):
        rng = np.random.default_rng(17)
        for dimensions in [1, 3, 4, 7, 8, 9, 31, 128, 768, 4096]:
            for _ in range(4):
                a, b = rng.normal(size=(2, dimensions)).astype("<f4")
                reference = math.fsum((float(x) - float(y)) ** 2 for x, y in zip(a, b))
                native, scalar = self.db.execute("SELECT sqlnew_l2(?,?),sqlnew_l2_scalar(?,?)", (a.tobytes(), b.tobytes()) * 2)[0]
                self.assertTrue(math.isclose(native, reference, rel_tol=1e-13))
                self.assertTrue(math.isclose(scalar, reference, rel_tol=1e-13))
        extreme = vector([np.finfo(np.float32).max])
        result = self.db.scalar("SELECT sqlnew_l2(?,?)", (extreme, vector([-np.finfo(np.float32).max])))
        self.assertTrue(math.isfinite(result))

    def test_vector_input_validation(self):
        valid = vector([1, 2])
        for invalid in [None, b"", b"a", "text", vector([1]), struct.pack("<ff", float("nan"), 2), struct.pack("<ff", float("inf"), 2)]:
            with self.assertRaises(Error):
                self.db.scalar("SELECT sqlnew_l2(?,?)", (invalid, valid))
        self.db.execute("CREATE TABLE mixed(id,v)")
        self.db.executemany("INSERT INTO mixed VALUES (?,?)", [(1, valid), (2, vector([1]))])
        with self.assertRaises(Error):
            self.db.scalar("SELECT sqlnew_vector_block(id,v,0) FROM mixed")
        self.assertIsNone(self.db.scalar("SELECT sqlnew_vector_block(id,v,0) FROM mixed WHERE 0"))
        for mode in (2, -1, 1 << 32, (1 << 32) + 1, -(1 << 32)):
            with self.assertRaises(Error):
                self.db.scalar("SELECT sqlnew_vector_block(1,?,?)", (valid, mode))
        with self.assertRaises(Error):
            self.db.execute("SELECT * FROM sqlnew_topk(zeroblob(67108865),?,1)", (valid,))

    def test_exact_topk_and_signed_ids(self):
        rng = np.random.default_rng(20)
        matrix = rng.normal(size=(200, 19)).astype("<f4")
        ids = list(range(-100, 98)) + [-(1 << 63), (1 << 63) - 1]
        block = self.block([(i, row.tobytes()) for i, row in zip(ids, matrix)])
        for query in rng.normal(size=(8, 19)).astype("<f4"):
            reference = sorted(zip(ids, np.sum((matrix.astype(np.float64) - query) ** 2, axis=1)), key=lambda x: (x[1], x[0]))
            for k in (1, 10, 199, 200, 500):
                actual = self.db.execute("SELECT id,distance FROM sqlnew_topk(?,?,?) ORDER BY distance,id", (block, query.tobytes(), k))
                self.assertEqual([i for i, _ in actual], [i for i, _ in reference[:k]])
                for (_, a), (_, b) in zip(actual, reference):
                    self.assertTrue(math.isclose(a, b, rel_tol=1e-13))
        ties = self.block([(i, vector([1, 0])) for i in (8, 2, -9, 1)])
        self.assertEqual(self.db.execute("SELECT id FROM sqlnew_topk(?,?,2)", (ties, vector([0, 0]))), [(-9,), (1,)])

    def test_quantized_topk_matches_decoded_codes(self):
        rng = np.random.default_rng(700)
        rows = [(i, vector(v)) for i, v in enumerate(rng.normal(size=(400, 65)))]
        rows += [(500, vector(np.zeros(65))), (501, vector(np.full(65, np.nextafter(np.float32(0), np.float32(1)))))]
        block = self.block(rows, 1)
        dimension, count, mode = struct.unpack_from("<III", block, 4)
        self.assertEqual((dimension, count, mode), (65, 402, 1))
        query = rng.normal(size=65).astype("<f4")
        reference = []
        for pos in range(count):
            offset = 16 + pos * (12 + dimension)
            rowid, scale = struct.unpack_from("<qf", block, offset)
            codes = np.frombuffer(block, dtype=np.int8, count=dimension, offset=offset + 12).astype(np.float64)
            reference.append((rowid, math.fsum((float(a) * scale - float(b)) ** 2 for a, b in zip(codes, query))))
        reference.sort(key=lambda x: (x[1], x[0]))
        actual = self.db.execute("SELECT id,distance FROM sqlnew_topk(?,?,17)", (block, query.tobytes()))
        self.assertEqual([i for i, _ in actual], [i for i, _ in reference[:17]])
        self.assertLess(len(block), sum(len(b) + 8 for _, b in rows))

    def test_filters_run_before_topk(self):
        block = self.block([(i, vector([i])) for i in range(100)])
        actual = self.db.execute("SELECT id FROM sqlnew_topk(?,?,3,?,?)", (block, vector([0]), id_blob([90]), id_blob([90, 91, 92, 93, 94])))
        self.assertEqual(actual, [(91,), (92,), (93,)])
        self.assertEqual(self.db.execute("SELECT id FROM sqlnew_topk(?,?,3,?,?)", (block, vector([0]), b"", b"")), [])
        with self.assertRaises(Error):
            self.db.execute("SELECT id FROM sqlnew_topk(?,?,3,?)", (block, vector([0]), struct.pack("<qq", 5, 2)))
        projected = self.db.execute("SELECT block,query,k,excluded,included FROM sqlnew_topk(?,?,3,?,?)", (block, vector([0]), b"", id_blob([2])))
        self.assertEqual(projected, [(block, vector([0]), 3, b"", id_blob([2]))])

    def test_hostile_vector_blocks(self):
        block = self.block([(1, vector([1, 2, 3]))])
        query = vector([0, 0, 0])
        for invalid in [b"", b"SNV1", block[:-1], block + b"x", b"BAD!" + block[4:], block[:8] + b"\xff" * 4 + block[12:], block[:12] + struct.pack("<I", 2) + block[16:]]:
            with self.assertRaises(Error):
                self.db.execute("SELECT * FROM sqlnew_topk(?,?,2)", (invalid, query))
        for k in (0, -1, 65537, 1 << 40, "3", 3.5):
            with self.assertRaises(Error):
                self.db.execute("SELECT * FROM sqlnew_topk(?,?,?)", (block, query, k))

    def test_random_malformed_frames_do_not_crash(self):
        rng = random.Random(912)
        for _ in range(2000):
            blob = rng.randbytes(rng.randrange(0, 400))
            try:
                self.unpack(blob)
            except Error:
                pass
            try:
                self.db.execute("SELECT * FROM sqlnew_topk(?,?,3)", (blob, vector([1, 2, 3])))
            except Error:
                pass
            try:
                self.db.execute("SELECT * FROM sqlnew_int_scan(?)", (blob,))
            except Error:
                pass

    def test_integer_blocks_preserve_nulls_order_and_full_int64_range(self):
        self.db.execute("CREATE TABLE integers(value)")
        rng = random.Random(801)
        cases = [[None], [None] * 1000, [42] * 1000, [None, 42, None, 42],
                 [-(1 << 63), None, (1 << 63) - 1, 0, -1, 1],
                 [rng.randrange(-100, 200) if i % 7 else None for i in range(1000)]]
        for width in range(1, 65):
            base = -(1 << 63)
            cases.append([base, base + (1 << width) - 1, None, base + 1, base])
        for values in cases:
            self.db.execute("DELETE FROM integers")
            self.db.executemany("INSERT INTO integers VALUES (?)", ((v,) for v in values))
            block = self.db.scalar("SELECT sqlnew_int_pack(value ORDER BY rowid) FROM integers")
            actual = self.db.execute("SELECT value,ordinal FROM sqlnew_int_scan(?) ORDER BY ordinal", (block,))
            self.assertEqual(actual, [(value, i + 1) for i, value in enumerate(values)])
        self.db.execute("DELETE FROM integers")
        self.assertIsNone(self.db.scalar("SELECT sqlnew_int_pack(value) FROM integers"))
        self.db.execute("INSERT INTO integers VALUES ('abc')")
        with self.assertRaises(Error):
            self.db.scalar("SELECT sqlnew_int_pack(value) FROM integers")

    def test_integer_filters_sql_affinity_and_corruption(self):
        self.db.execute("CREATE TABLE integers(value INTEGER)")
        values = [None, -(1 << 63), (1 << 63) - 1] + list(range(-100, 101))
        self.db.executemany("INSERT INTO integers VALUES (?)", ((v,) for v in values))
        block = self.db.scalar("SELECT sqlnew_int_pack(value ORDER BY rowid) FROM integers")
        for predicate, parameters in [
            ("value BETWEEN ? AND ?", (-7, 12)), ("value > ? AND value < ?", (-7, 12)),
            ("value = ?", (None,)), ("value IS NULL", ()), ("value = ?", ("12",)),
            ("value < ?", ("abc",)), ("value >= ?", (0.5,)),
            ("value > ?", ((1 << 63) - 1,)), ("value < ?", (-(1 << 63),)),
            ("value >= ? AND value <= ? AND value > ?", (5, 10, 5)),
            ("value = ? AND value = ?", (10, 11))]:
            expected = self.db.execute(f"SELECT value FROM integers WHERE {predicate} ORDER BY rowid", parameters)
            actual = self.db.execute(f"SELECT value FROM sqlnew_int_scan(?) WHERE {predicate} ORDER BY ordinal", (block, *parameters))
            self.assertEqual(actual, expected, (predicate, parameters))
        for i in range(len(block)):
            corrupt = bytearray(block); corrupt[i] ^= 1
            with self.assertRaises(Error):
                self.db.execute("SELECT * FROM sqlnew_int_scan(?)", (corrupt,))
        self.assertEqual(self.db.execute("SELECT * FROM sqlnew_int_scan(NULL)"), [])
        small = self.db.scalar("SELECT sqlnew_int_pack(100 + value%4) FROM integers WHERE value BETWEEN 0 AND 100")
        self.assertLess(len(small), 100)


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "test.db"
        self.db = Database(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.index = VectorIndex(self.db).create(8, "synthetic/test-v1")
        self.matrix = np.random.default_rng(90).normal(size=(300, 8)).astype("<f4")
        self.index.add((i, v, f"document {i} " + ("oranges" if i % 2 else "apples"), bytes([i % 256]) * 400) for i, v in enumerate(self.matrix))

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def test_delta_then_sealed_and_reopen(self):
        query = self.matrix[121]
        original = self.index.search(query, exact=True)
        self.assertEqual(original["hits"][0][0], 121)
        self.assertEqual(self.index.search(query)["hits"], original["hits"])
        self.index.rebuild(clusters=8, block_rows=32, iterations=3)
        self.assertEqual(self.index.search(query, exact=True)["hits"], original["hits"])
        self.assertEqual(self.index.search(query)["hits"], original["hits"])
        self.db.close()
        self.db = Database(self.path)
        self.index = VectorIndex(self.db)
        self.assertEqual(self.index.search(query)["hits"], original["hits"])
        self.assertEqual(self.index.document(121), ("document 121 oranges", bytes([121]) * 400))
        self.assertEqual(self.db.scalar("PRAGMA integrity_check"), "ok")

    def test_mutation_filter_and_rollback(self):
        query = self.matrix[121]
        self.index.rebuild(clusters=5, block_rows=64, iterations=2)
        self.index.delete([121])
        self.index.add([(122, query, "fresh oranges", b"updated"), (999, query, "new apples", None)])
        hits = self.index.search(query)["hits"]
        self.assertEqual([i for i, _ in hits[:2]], [122, 999])
        self.assertNotIn(121, [i for i, _ in hits])
        filtered = self.index.search(query, k=5, eligible_ids=[2, 12, 22, 32, 42])
        self.assertEqual(set(i for i, _ in filtered["hits"]), {2, 12, 22, 32, 42})
        generation = self.index.metadata()["generation"]
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with self.db.transaction():
                self.index.delete([122, 999])
                self.index.rebuild(clusters=3, iterations=1)
                raise RuntimeError("abort")
        self.assertEqual(self.index.metadata()["generation"], generation)
        self.assertEqual(self.index.search(query)["hits"], hits)
        self.assertEqual(self.index.document(122), ("fresh oranges", b"updated"))

    def test_wal_snapshot(self):
        with Database(self.path) as other:
            reader = VectorIndex(other)
            with other.transaction():
                self.assertIsNotNone(reader.document(10))
                self.index.delete([10])
                self.assertIsNotNone(reader.document(10))
                self.assertEqual(reader.search(self.matrix[10], exact=True, k=1)["hits"][0][0], 10)
            self.assertIsNone(reader.document(10))

    def test_hybrid_and_text_updates(self):
        self.index.rebuild(clusters=5, iterations=2)
        hits = self.index.hybrid(self.matrix[99], "oranges", k=10, eligible_ids=range(50, 100))
        self.assertTrue(hits)
        self.assertTrue(all(50 <= i < 100 for i, _ in hits))
        self.index.add([(99, self.matrix[99], "pineapples", b"new")])
        self.assertEqual(self.db.scalar("SELECT count(*) FROM sn_documents_text WHERE sn_documents_text MATCH 'oranges' AND rowid=99"), 0)
        self.assertEqual(self.db.scalar("SELECT count(*) FROM sn_documents_text WHERE sn_documents_text MATCH 'pineapples' AND rowid=99"), 1)
        self.index.delete([99])
        self.assertEqual(self.db.scalar("SELECT count(*) FROM sn_documents_text WHERE sn_documents_text MATCH 'pineapples' AND rowid=99"), 0)

    def test_empty_index_and_failed_upsert_are_atomic(self):
        empty = VectorIndex(self.db, "empty").create(2, "test")
        empty.rebuild(clusters=2)
        self.assertEqual(empty.search([0, 0])["hits"], [])
        with self.assertRaises(ValueError):
            empty.add([(1, [1, 2], "valid", b""), (2, [1], "invalid", b"")])
        self.assertIsNone(empty.document(1))

    def test_geometry_prunes_without_changing_exact_results(self):
        idx = VectorIndex(self.db, "separated").create(2, "geometry-test")
        rng = np.random.default_rng(821)
        points = np.vstack([rng.normal(0, 0.01, (40, 2)) + center for center in [[0, 0], [100, 0], [0, 100], [100, 100]]]).astype("<f4")
        idx.add((i, p, "", None) for i, p in enumerate(points))
        idx.rebuild(clusters=4, iterations=8)
        result = idx.search([0, 0], k=10, exact=True)
        self.assertGreater(result["clusters_pruned"], 0)
        self.assertEqual(result["hits"], idx.search([0, 0], k=10, exact=True, geometric=False)["hits"])
        idx.add([(500, [0, 0], "delta", None)])
        idx.delete([0, 1, 2])
        for query in ([0, 0], [50, 50], [100, 100], [1000, 1000]):
            for eligible in (None, [5, 41, 81, 121, 500]):
                actual = idx.search(query, k=5, exact=True, eligible_ids=eligible)
                expected = idx.search(query, k=5, exact=True, geometric=False, eligible_ids=eligible)
                self.assertEqual(actual["hits"], expected["hits"])

    def test_geometry_extreme_exponents_and_near_ties(self):
        rng = np.random.default_rng(119)
        for exponent in (-140, -50, 0, 50, 120):
            idx = VectorIndex(self.db, "exponent_" + str(exponent).replace("-", "m")).create(7, "geometry-test")
            points = np.ldexp(rng.normal(size=(60, 7)).astype(np.float32), exponent)
            idx.add((i, p, "", None) for i, p in enumerate(points))
            idx.rebuild(clusters=5, iterations=3)
            for query in list(points[:8]) + [np.zeros(7, dtype=np.float32)]:
                self.assertEqual(idx.search(query, k=7, exact=True)["hits"], idx.search(query, k=7, exact=True, geometric=False)["hits"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
