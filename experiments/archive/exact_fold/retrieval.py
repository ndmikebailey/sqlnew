"""Sparse exact change retrieval over immutable shared region packets.

This is an isolated version comparison. Narrative results: ATTEMPTS.txt.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import statistics
import struct
import time

import numpy as np

from regions import ROOT, HEAD, RECORD, EDGE, MASK64, Builder, Reader, Codecs, Database
from overlays import HEADER, OverlayWriter, OverlayReader, exact

OUT = ROOT / 'build/math-exploration/retrieval'
OLD = ROOT / 'build/math-exploration/overlays'
U16, U64 = struct.Struct('<H'), struct.Struct('<Q')


class SparseBuilder:
    def __init__(self, shape, changes, codecs):
        self.shape, self.codecs = shape, codecs
        self.data, self.intern = bytearray(HEAD.size), {}
        self.leaves = {}
        for coordinate, value in changes.items():
            origin = tuple(i // 8 * 8 for i in coordinate)
            z, y, x = (i % 8 for i in coordinate)
            self.leaves.setdefault(origin, []).append((z*64+y*8+x, value))

    def record(self, kind, flags, aux, body):
        raw = RECORD.pack(kind, flags, aux, len(body)) + body
        if raw not in self.intern:
            self.intern[raw] = len(self.data)
            self.data.extend(raw)
        return self.intern[raw]

    def leaf(self, entries):
        entries = sorted(entries)
        live = [v for _, v in entries if v is not None]
        anchor = live[0] if live else 0
        # The first normalized live value is always zero and needs no bytes.
        tail = b''.join(U64.pack((v-anchor) & MASK64) for v in live[1:])
        codec, payload = self.codecs.best(tail) if tail else (0, b'')
        codes = b''.join(U16.pack(slot | (0x8000 if value is None else 0))
                         for slot, value in entries)
        changed = sum(1 << slot for slot, _ in entries)
        deleted = sum(1 << slot for slot, value in entries if value is None)
        bitmaps = changed.to_bytes(64, 'little')
        if deleted:
            bitmaps += deleted.to_bytes(64, 'little')
        mode = int(len(bitmaps) < len(codes))
        metadata = bitmaps if mode else codes
        flags = mode | (codec << 1) | (0x80 if deleted else 0)
        return self.record(2, flags, len(entries), metadata + payload), anchor

    def region(self, origin, span, leaves):
        if span == 8:
            return self.leaf(leaves[0][1])
        half = span // 2
        buckets = {}
        for coordinate, entries in leaves:
            slot = sum((int(coordinate[i] >= origin[i]+half) << (2-i)) for i in range(3))
            buckets.setdefault(slot, []).append((coordinate, entries))
        children = []
        child_mask = 0
        for slot in sorted(buckets):
            child_origin = tuple(origin[i]+half*((slot >> (2-i)) & 1) for i in range(3))
            children.append(self.region(child_origin, half, buckets[slot]))
            child_mask |= 1 << slot
        anchor = children[0][1]
        body = bytes((child_mask,)) + b''.join(EDGE.pack(offset, (base-anchor) & MASK64)
                                               for offset, base in children)
        return self.record(0, 0, 0, body), anchor

    def finish(self, count):
        span = 8
        while span < max(self.shape):
            span *= 2
        offset, anchor = self.region((0, 0, 0), span, list(self.leaves.items())) if self.leaves else (0, 0)
        self.data[:HEAD.size] = HEAD.pack(b'SPC1', 1, 1, 3, 0, *self.shape,
                                         span, offset, anchor, count)
        return bytes(self.data)


class SparseWriter(OverlayWriter):
    def packet(self):
        patch = SparseBuilder(self.reader.shape, self.changes, self.codecs).finish(len(self.changes))
        return HEADER.pack(b'SOV2', 1, self.identity, self.generation,
                           *self.reader.shape, len(patch), 0, len(self.changes)) + patch


class SparsePatchReader:
    def __init__(self, data, codecs):
        magic, version, normalized, log, reserved, z, y, x, span, root, anchor, count = HEAD.unpack_from(data)
        assert magic == b'SPC1' and version == 1 and log == 3 and normalized == 1 and reserved == 0
        self.data, self.codecs = data, codecs
        self.shape, self.span, self.root, self.anchor = (z, y, x), span, root, anchor

    def selected(self, box):
        stats = {'encoded_bytes_touched': HEAD.size, 'decoded_words': 0,
                 'record_visits': 0, 'metadata_entries_examined': 0, 'changes_matched': 0}
        seen, words, decoded = set(), {}, {}
        results = []
        is_point = all(hi-lo == 1 for lo, hi in box)

        def charge(key, size):
            if key not in seen:
                seen.add(key)
                stats['encoded_bytes_touched'] += size

        def walk(offset, anchor, origin, span):
            if any(o >= hi or o+span <= lo for o, (lo, hi) in zip(origin, box)):
                return
            kind, flags, count, size = RECORD.unpack_from(self.data, offset)
            stats['record_visits'] += 1
            start = offset + RECORD.size
            if kind == 0:
                charge(('branch', offset), RECORD.size+size)
                mask, pos, half = self.data[start], start+1, span//2
                for slot in range(8):
                    if mask & (1 << slot):
                        child, delta = EDGE.unpack_from(self.data, pos)
                        pos += EDGE.size
                        child_origin = tuple(origin[i]+half*((slot >> (2-i)) & 1) for i in range(3))
                        walk(child, (anchor+delta) & MASK64, child_origin, half)
                return
            assert kind == 2 and span == 8
            codec = (flags >> 1) & 7
            meta_size = (128 if flags & 0x80 else 64) if flags & 1 else 2*count
            charge(('meta', offset), RECORD.size+meta_size)
            hits = []
            if flags & 1:
                changed = int.from_bytes(self.data[start:start+64], 'little')
                deleted = int.from_bytes(self.data[start+64:start+128], 'little') if flags & 0x80 else 0
                live = changed & ~deleted
                nlive = live.bit_count()
                if is_point:
                    z, y, x = (b[0]-o for b, o in zip(box, origin))
                    requested = 1 << (z*64+y*8+x)
                else:
                    lo = tuple(max(0, b[0]-o) for b, o in zip(box, origin))
                    hi = tuple(min(8, b[1]-o) for b, o in zip(box, origin))
                    line = ((1 << (hi[2]-lo[2]))-1) << lo[2]
                    requested = 0
                    for z in range(lo[0], hi[0]):
                        for y in range(lo[1], hi[1]):
                            requested |= line << (z*64+y*8)
                matched = changed & requested
                while matched:
                    bit = matched & -matched
                    slot = bit.bit_length()-1
                    hits.append((slot, None if deleted & bit else (live & (bit-1)).bit_count()))
                    matched ^= bit
                stats['metadata_entries_examined'] += len(hits)
            else:
                # At most 32 entries without deletions, or 64 with deletions.
                # Metadata is read before any value payload is requested.
                nlive = 0
                for i in range(count):
                    code = U16.unpack_from(self.data, start+2*i)[0]
                    slot, deleted = code & 511, bool(code & 0x8000)
                    coordinate = (origin[0]+slot//64, origin[1]+(slot//8)%8, origin[2]+slot%8)
                    if all(lo <= c < hi for c, (lo, hi) in zip(coordinate, box)):
                        hits.append((slot, None if deleted else nlive))
                    if not deleted:
                        nlive += 1
                stats['metadata_entries_examined'] += count
            if not hits:
                return
            payload_start = start+meta_size
            for slot, rank in hits:
                coordinate = (origin[0]+slot//64, origin[1]+(slot//8)%8, origin[2]+slot%8)
                if rank is None:
                    value = None
                else:
                    key = (offset, rank)
                    if key not in words:
                        if rank == 0:
                            words[key] = 0
                            stats['decoded_words'] += 1
                        elif codec == 0:
                            address = payload_start+8*(rank-1)
                            words[key] = U64.unpack_from(self.data, address)[0]
                            charge(('word', address), 8)
                            stats['decoded_words'] += 1
                        else:
                            if offset not in decoded:
                                body = self.data[payload_start:start+size]
                                raw = self.codecs.decode(codec, body)
                                assert len(raw) == 8*(nlive-1)
                                decoded[offset] = raw
                                charge(('payload', offset), len(body))
                                stats['decoded_words'] += nlive-1
                            words[key] = U64.unpack_from(decoded[offset], 8*(rank-1))[0]
                    value = (words[key]+anchor) & MASK64
                results.append((coordinate, value))

        if self.root:
            walk(self.root, self.anchor, (0, 0, 0), self.span)
        stats['changes_matched'] = len(results)
        return results, stats


class SparseReader:
    def __init__(self, base, overlay, codecs):
        magic, version, identity, generation, z, y, x, size, reserved, count = HEADER.unpack_from(overlay)
        assert magic == b'SOV2' and version == 1 and reserved == 0
        if hashlib.sha256(base).digest() != identity:
            raise ValueError('overlay belongs to another base')
        assert len(overlay) == HEADER.size+size
        self.base, self.patch = Reader(base, codecs), SparsePatchReader(overlay[HEADER.size:], codecs)
        self.shape, self.generation, self.count = (z, y, x), generation, count
        assert self.base.shape == self.patch.shape == self.shape

    def select(self, box=None):
        box = box or tuple((0, n) for n in self.shape)
        changes, ps = self.patch.selected(box)
        shape = tuple(hi-lo for lo, hi in box)
        total = int(np.prod(shape))
        covered = len(changes) == total
        if covered:
            values, present = np.zeros(shape, dtype='<u8'), np.zeros(shape, dtype=np.bool_)
            bs = {'encoded_bytes_touched': 0, 'decoded_words': 0, 'record_visits': 0}
        else:
            values, present, bs = self.base.select(box)
        for coordinate, value in changes:
            index = tuple(c-b[0] for c, b in zip(coordinate, box))
            values[index] = 0 if value is None else value
            present[index] = value is not None
        return values, present, {
            'requested_cells': int(present.sum()), 'changes_matched': len(changes),
            'base_bytes_touched': bs['encoded_bytes_touched'], 'base_decoded_words': bs['decoded_words'],
            'overlay_bytes_touched': HEADER.size+ps['encoded_bytes_touched'],
            'overlay_decoded_words': ps['decoded_words'],
            'encoded_bytes_touched': HEADER.size+ps['encoded_bytes_touched']+bs['encoded_bytes_touched'],
            'decoded_words': ps['decoded_words']+bs['decoded_words'],
            'record_visits': ps['record_visits']+bs['record_visits'],
            'metadata_entries_examined': ps['metadata_entries_examined'], 'base_skipped': covered,
        }

    def get(self, coordinate):
        coordinate = tuple(int(i) for i in coordinate)
        if len(coordinate) != 3 or any(i < 0 or i >= n for i, n in zip(coordinate, self.shape)):
            raise ValueError('coordinate outside this root extent')
        box = tuple((i, i+1) for i in coordinate)
        changes, _ = self.patch.selected(box)
        if changes:
            return changes[0][1]
        values, present, _ = self.base.select(box)
        return int(values[0, 0, 0]) if present[0, 0, 0] else None


def timings(readers, boxes):
    samples = {version: [] for version in readers}
    names = list(readers)
    for repetition in range(22):
        for version in names if repetition % 2 == 0 else names[::-1]:
            started = time.perf_counter_ns()
            for box in boxes:
                readers[version].select(box)
            elapsed = (time.perf_counter_ns()-started)/1e6
            if repetition >= 3:
                samples[version].append(elapsed)
    return {version: {'median_ms': statistics.median(s), 'samples_ms': s} for version, s in samples.items()}


def measure(codecs):
    prior = json.loads((ROOT/'experiments/archive/exact_fold/overlays-results.json').read_text())
    rows = []
    names = ('same_512_edits_as_previous_failure', 'one_random_position_per_region',
             'random_replacements_1pct', 'random_replacements_10pct', 'random_replacements_50pct')
    for name in names:
        record = next(row for row in prior['measurements'] if row['fixture'] == name)
        base = (OLD/(name+'.srg')).read_bytes()
        old = (OLD/(name+'.sov')).read_bytes()
        assert hashlib.sha256(base).hexdigest() == record['base_sha256']
        assert hashlib.sha256(old).hexdigest() == record['overlay_sha256']
        previous = OverlayReader(base, old, codecs)
        expected, occupied, _ = previous.select()
        assert hashlib.sha256(expected[occupied].tobytes()).hexdigest() == record['source_sha256']
        writer = SparseWriter(base, codecs)
        changed = (occupied != writer.present) | (occupied & (expected != writer.original))
        coordinates = [tuple(int(i) for i in c) for c in np.argwhere(changed)]
        writer.apply((c, int(expected[c]) if occupied[c] else None) for c in coordinates)
        packet = writer.packet()
        path = OUT/(name+'.sov2')
        path.write_bytes(packet)
        current = SparseReader(base, path.read_bytes(), codecs)
        full = {'before': exact(previous, expected, occupied), 'after': exact(current, expected, occupied)}
        first = coordinates[0]
        unchanged = next(c for c in np.ndindex(expected.shape) if not changed[c])
        boxes = {
            'edited_point': tuple((i, i+1) for i in first),
            'unchanged_point': tuple((i, i+1) for i in unchanged),
        }
        for axis in range(3):
            box = [(0, n) for n in expected.shape]
            box[axis] = (first[axis], first[axis]+1)
            boxes['edited_plane_'+'zyx'[axis]] = tuple(box)
        query_rows = {}
        for query, box in boxes.items():
            a = exact(previous, expected, occupied, box)
            b = exact(current, expected, occupied, box)
            query_rows[query] = {'before': a, 'after': b,
                                 'timings': timings({'before': previous, 'after': current}, [box])}
        # Use distinct positions: a repeated single point could flatter cache behavior.
        points = coordinates[:min(64, len(coordinates))]
        point_boxes = [tuple((i, i+1) for i in c) for c in points]
        for c in points:
            assert current.get(c) == int(expected[c])
        batch_times = timings({'before': previous, 'after': current}, point_boxes)
        row = {'fixture': name, 'changes': len(coordinates), 'base_bytes': len(base),
               'before_overlay_bytes': len(old), 'after_overlay_bytes': len(packet),
               'before_total_bytes': len(base)+len(old), 'after_total_bytes': len(base)+len(packet),
               'linear_comparison_bytes': record['best_linear_bytes'],
               'full_read': full, 'queries': query_rows,
               'distinct_edited_point_batch': {'count': len(points), 'timings': batch_times},
               'base_sha256': hashlib.sha256(base).hexdigest(),
               'overlay_sha256': hashlib.sha256(packet).hexdigest(), 'exact': True}
        rows.append(row)
        print(name, 'overlay bytes', len(old), '->', len(packet),
              'full overlay words', full['before']['overlay_decoded_words'], '->', full['after']['overlay_decoded_words'],
              '64 points ms', round(batch_times['before']['median_ms'], 3), '->',
              round(batch_times['after']['median_ms'], 3), flush=True)
    return rows


def lifecycle(codecs):
    values = np.arange(17*25*33, dtype='<u8').reshape((17, 25, 33))
    occupied = np.ones(values.shape, dtype=np.bool_)
    occupied[1, 2, 3] = False
    base = Builder(values, occupied, codecs, 'regions_shared').finish()
    writer = SparseWriter(base, codecs)
    empty = writer.packet()
    changes = [((0, 0, 0), MASK64), ((1, 2, 3), 0), ((4, 5, 6), None),
               ((8, 8, 8), 1 << 63), ((16, 24, 32), 0)]
    writer.apply(changes)
    expected, mask = values.copy(), occupied.copy()
    for c, value in changes:
        expected[c], mask[c] = 0 if value is None else value, value is not None
    pinned = SparseReader(base, writer.packet(), codecs)
    exact(pinned, expected, mask)
    for c, value in changes:
        assert pinned.get(c) == value
    lengths = []
    for i in range(100):
        writer.apply([((0, 0, 0), 900000+i)])
        snapshot = writer.packet()
        reader = SparseReader(base, snapshot, codecs)
        now = expected.copy()
        now[0, 0, 0] = 900000+i
        exact(reader, now, mask)
        lengths.append(len(snapshot))
    exact(pinned, expected, mask)
    writer.apply([(c, int(values[c]) if occupied[c] else None) for c, _ in changes])
    assert not writer.changes
    assert len(writer.packet()) == len(empty) == 192
    exact(SparseReader(base, writer.packet(), codecs), values, occupied)
    # Exercise both metadata forms, tombstone ranks and compressed live tails.
    rng = np.random.default_rng(6101426)
    mode_cases = []
    for count in (1, 31, 32, 33, 63, 64, 65, 127, 256, 512):
        for deletion_stride in (0, 3):
            work = SparseWriter(base, codecs)
            state, live = values.copy(), occupied.copy()
            slots = rng.permutation(512)[:count]
            edits = []
            for i, slot in enumerate(slots):
                c = (int(slot)//64, (int(slot)//8)%8, int(slot)%8)
                value = None if deletion_stride and i % deletion_stride == 0 else 700000+int(slot)*13
                edits.append((c, value))
                state[c], live[c] = 0 if value is None else value, value is not None
            work.apply(edits)
            reader = SparseReader(base, work.packet(), codecs)
            exact(reader, state, live)
            for c, value in edits:
                assert reader.get(c) == value
            for axis in range(3):
                box = [(0, n) for n in values.shape]
                box[axis] = (3, 4)
                exact(reader, state, live, box)
            mode_cases.append({'count': count, 'deletion_stride': deletion_stride, 'exact': True})
    return {'pinned_snapshot_exact': True, 'latest_snapshots_exact': 100,
            'snapshot_bytes_min': min(lengths), 'snapshot_bytes_max': max(lengths),
            'restored_overlay_bytes': len(empty), 'metadata_boundary_cases': mode_cases,
            'updates_deletes_absence_zero_extreme_bits_exact': True}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with Database() as db:
        codecs = Codecs(db)
        checks = lifecycle(codecs)
        print('lifecycle and metadata boundaries exact', flush=True)
        rows = measure(codecs)
    report = {'recorded_utc': datetime.now(timezone.utc).isoformat(), 'platform': platform.platform(),
              'measurements': rows, 'checks': checks,
              'protocol': 'Isolated Python reader comparison over identical immutable bases and current values. Select returns the same arrays and occupancy. Fresh per-query caches, three warmups and 19 retained timing samples, version order alternated each repetition. Resident packets; reader opening/base hashing excluded. Timings include traversal, native decompression when needed, and result assembly. No native SQL or disk-I/O claim.',
              'source_sha256': {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in (
                  'experiments/archive/exact_fold/retrieval.py', 'experiments/archive/exact_fold/overlays.py',
                  'experiments/archive/exact_fold/regions.py', 'ext/sqlnew/fold.c',
                  'ext/sqlnew/sqlnew.c', 'ext/sqlnew/columnar.c')},
              'library_sha256': hashlib.sha256((ROOT/'build/sqlnew/sqlnewdb.dll').read_bytes()).hexdigest()}
    path = OUT/'results.json'
    path.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print('saved', path, flush=True)


if __name__ == '__main__':
    main()
