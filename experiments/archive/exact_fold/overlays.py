"""Immutable shared region bases with one consolidated sparse edit overlay.

Isolated representation experiment. Narrative results belong in ATTEMPTS.txt.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import struct
import time

import numpy as np

from regions import (ROOT, Builder, Codecs, Database, Reader, ensure_exact,
                     fixtures, linear_baseline, MASK64)

OUT = ROOT / 'build/math-exploration/overlays'
# Base SHA-256, generation, shape and child packet lengths. 128 bytes total.
HEADER = struct.Struct('<4sB3x32sQIII4xQQQ40x')
assert HEADER.size == 128


class OverlayWriter:
    def __init__(self, base, codecs):
        self.base = base
        self.codecs = codecs
        self.identity = hashlib.sha256(base).digest()
        self.reader = Reader(base, codecs)
        # Scout implementation: materialize the original once for exact
        # no-op/revert detection. This is not an optimized mutation path.
        self.original, self.present, _ = self.reader.select()
        self.changes = {}
        self.generation = 0

    def apply(self, edits):
        """Apply in order; None removes a cell, zero is an occupied value."""
        for coordinate, value in edits:
            coordinate = tuple(int(i) for i in coordinate)
            if len(coordinate) != 3 or any(i < 0 or i >= n for i, n in zip(coordinate, self.reader.shape)):
                raise ValueError('coordinate outside this root extent')
            if value is not None:
                value = int(value)
                if not 0 <= value <= MASK64:
                    raise ValueError('expected exact unsigned 64-bit bits')
            original = int(self.original[coordinate]) if self.present[coordinate] else None
            if value == original:
                self.changes.pop(coordinate, None)
            else:
                self.changes[coordinate] = value
        self.generation += 1

    def packet(self):
        values = np.zeros(self.reader.shape, dtype='<u8')
        changed = np.zeros(self.reader.shape, dtype=np.bool_)
        deleted = np.zeros(self.reader.shape, dtype=np.bool_)
        for coordinate, value in self.changes.items():
            if value is None:
                deleted[coordinate] = True
            else:
                values[coordinate] = value
                changed[coordinate] = True
        updates = Builder(values, changed, self.codecs, 'regions_shared').finish()
        removals = Builder(np.zeros_like(values), deleted, self.codecs, 'regions_shared').finish()
        header = HEADER.pack(b'SOV1', 1, self.identity, self.generation,
                             *self.reader.shape, len(updates), len(removals), len(self.changes))
        return header + updates + removals


class OverlayReader:
    def __init__(self, base, overlay, codecs):
        magic, version, identity, generation, z, y, x, update_size, delete_size, count = HEADER.unpack_from(overlay)
        if magic != b'SOV1' or version != 1:
            raise ValueError('unknown overlay format')
        if hashlib.sha256(base).digest() != identity:
            raise ValueError('overlay belongs to another base')
        if len(overlay) != HEADER.size + update_size + delete_size:
            raise ValueError('overlay size mismatch')
        self.base = Reader(base, codecs)
        self.updates = Reader(overlay[HEADER.size:HEADER.size+update_size], codecs)
        self.deletes = Reader(overlay[HEADER.size+update_size:], codecs)
        self.shape = (z, y, x)
        assert self.base.shape == self.updates.shape == self.deletes.shape == self.shape
        self.generation, self.count = generation, count

    def select(self, box=None):
        updates, changed, us = self.updates.select(box)
        _, deleted, ds = self.deletes.select(box)
        assert not np.any(changed & deleted)
        covered = changed | deleted
        if covered.all():
            values, present = np.zeros_like(updates), np.zeros_like(changed)
            bs = {'encoded_bytes_touched': 0, 'decoded_words': 0, 'record_visits': 0}
        else:
            values, present, bs = self.base.select(box)
        values[changed] = updates[changed]
        present[changed] = True
        values[deleted] = 0
        present[deleted] = False
        return values, present, {
            'requested_cells': int(present.sum()), 'changes_matched': int(covered.sum()),
            'base_bytes_touched': bs['encoded_bytes_touched'],
            'base_decoded_words': bs['decoded_words'],
            'overlay_bytes_touched': HEADER.size + us['encoded_bytes_touched'] + ds['encoded_bytes_touched'],
            'overlay_decoded_words': us['decoded_words'] + ds['decoded_words'],
            'encoded_bytes_touched': HEADER.size + sum(s['encoded_bytes_touched'] for s in (bs, us, ds)),
            'decoded_words': sum(s['decoded_words'] for s in (bs, us, ds)),
            'record_visits': sum(s['record_visits'] for s in (bs, us, ds)),
            'base_skipped': bool(covered.all()),
        }


def exact(reader, values, occupied, box=None):
    actual, present, stats = reader.select(box)
    slices = tuple(slice(a, b) for a, b in box) if box else (...,)
    assert np.array_equal(present, occupied[slices]), 'occupancy mismatch'
    assert np.array_equal(actual[present], values[slices][present]), 'exact bits mismatch'
    return stats


def queries(reader, values, occupied, changed_point, unchanged_point):
    result = {'full': exact(reader, values, occupied)}
    for axis in range(3):
        box = [(0, n) for n in values.shape]
        middle = values.shape[axis] // 2
        box[axis] = (middle, middle + 1)
        result['plane_' + 'zyx'[axis]] = exact(reader, values, occupied, box)
        box[axis] = (changed_point[axis], changed_point[axis]+1)
        result['changed_plane_' + 'zyx'[axis]] = exact(reader, values, occupied, box)
    for name, coordinate in [('changed_point', changed_point), ('unchanged_point', unchanged_point)]:
        if coordinate is None:
            continue
        box = tuple((i, i+1) for i in coordinate)
        result[name] = exact(reader, values, occupied, box)
    return result


def measure_case(name, base, before, present, after, after_mask, edits, codecs):
    writer = OverlayWriter(base, codecs)
    started = time.perf_counter()
    writer.apply(edits)
    overlay = writer.packet()
    elapsed = time.perf_counter() - started
    base_path = OUT / (name + '.srg')
    overlay_path = OUT / (name + '.sov')
    base_path.write_bytes(base)
    overlay_path.write_bytes(overlay)
    reopened_base = base_path.read_bytes()
    assert reopened_base == base
    reader = OverlayReader(reopened_base, overlay_path.read_bytes(), codecs)
    changed_point = next(iter(writer.changes), (0, 0, 0))
    unchanged_point = next((c for c in np.ndindex(before.shape) if c not in writer.changes), None)
    work = queries(reader, after, after_mask, changed_point, unchanged_point)
    rebuilt = Builder(after, after_mask, codecs, 'regions_shared').finish()
    ensure_exact(Reader(rebuilt, codecs), after, after_mask)
    linear, _ = linear_baseline(after, after_mask, codecs)
    total = len(base) + len(overlay)
    row = {'fixture': name, 'shape': list(before.shape), 'changes': len(writer.changes),
           'base_bytes': len(base), 'overlay_bytes': len(overlay), 'total_bytes': total,
           'rebuilt_shared_bytes': len(rebuilt), 'best_linear_bytes': linear['bytes'],
           'linear_over_overlay': linear['bytes']/total,
           'rebuilt_over_overlay': len(rebuilt)/total,
           'overlay_encode_seconds': elapsed,
           'base_unchanged': hashlib.sha256(base_path.read_bytes()).digest() == writer.identity,
           'base_sha256': writer.identity.hex(),
           'overlay_sha256': hashlib.sha256(overlay).hexdigest(),
           'source_sha256': hashlib.sha256(after[after_mask].tobytes()).hexdigest(),
           'queries': work, 'exact': True}
    print(name, 'changes', row['changes'], 'base+overlay', total,
          'rebuilt', len(rebuilt), 'linear', linear['bytes'],
          'multiplier', round(row['linear_over_overlay'], 3), flush=True)
    return row


def measure(codecs):
    fixture_rows = {name: (v, m) for name, v, m in fixtures()}
    before = fixture_rows['shared_irregular_motif'][0]
    present = np.ones(before.shape, dtype=np.bool_)
    base = Builder(before, present, codecs, 'regions_shared').finish()
    rng = np.random.default_rng(6101423)
    rows = []
    after = fixture_rows['motif_one_edit_per_region'][0]
    changes = [tuple(int(i) for i in coordinate) for coordinate in np.argwhere(before != after)]
    edits = [(c, int(after[c])) for c in changes]
    rows.append(measure_case('same_512_edits_as_previous_failure', base, before, present,
                             after, present, edits, codecs))
    # Avoid relying on identical patch positions or values between regions.
    after = before.copy()
    edits = []
    for region in np.ndindex((8, 8, 8)):
        position = rng.integers(0, 8, size=3)
        coordinate = tuple(int(r*8+p) for r, p in zip(region, position))
        value = int(rng.integers(0, 1 << 64, dtype=np.uint64))
        after[coordinate] = value
        edits.append((coordinate, value))
    rows.append(measure_case('one_random_position_per_region', base, before, present,
                             after, present, edits, codecs))
    # Independent new values, nested uniformly selected coordinate sets.
    order = rng.permutation(before.size)
    replacement = rng.integers(0, 1 << 64, size=before.size, dtype=np.uint64)
    for fraction in (.01, .1, .5, 1.0):
        count = int(before.size * fraction)
        ids = order[:count]
        after = before.copy()
        after.ravel()[ids] = replacement[:count]
        edits = [(tuple(int(i) for i in np.unravel_index(int(j), before.shape)), int(v))
                 for j, v in zip(ids, replacement[:count])]
        rows.append(measure_case('random_replacements_' + str(int(fraction*100)) + 'pct',
                                 base, before, present, after, present, edits, codecs))
    return rows


def lifecycle(codecs):
    before = np.arange(17*25*33, dtype='<u8').reshape((17, 25, 33))
    present = np.ones(before.shape, dtype=np.bool_)
    present[1, 2, 3] = False
    base = Builder(before, present, codecs, 'regions_shared').finish()
    identity = hashlib.sha256(base).hexdigest()
    writer = OverlayWriter(base, codecs)
    pristine = writer.packet()
    expected, occupied = before.copy(), present.copy()
    edits = [((0, 0, 0), MASK64), ((1, 2, 3), 0), ((4, 5, 6), None),
             ((8, 8, 8), 1 << 63), ((16, 24, 32), 0)]
    writer.apply(edits)
    for c, value in edits:
        expected[c] = 0 if value is None else value
        occupied[c] = value is not None
    first = writer.packet()
    pinned = OverlayReader(base, first, codecs)
    work = queries(pinned, expected, occupied, (1, 2, 3), (2, 2, 2))
    old_expected, old_mask = expected.copy(), occupied.copy()
    latest_sizes = []
    retained_bytes = len(first)
    # The history list is NOT searched by reads. Each snapshot has one overlay.
    for iteration in range(100):
        value = 500000 + iteration
        writer.apply([((0, 0, 0), value), ((4, 5, 6), value+1)])
        expected[0, 0, 0] = value
        expected[4, 5, 6] = value+1
        occupied[4, 5, 6] = True
        packet = writer.packet()
        retained_bytes += len(packet)
        latest_sizes.append(len(packet))
        exact(OverlayReader(base, packet, codecs), expected, occupied)
        assert len(writer.changes) == 5
    exact(pinned, old_expected, old_mask)
    # Restore the immutable base's original states, including absence.
    writer.apply([(c, int(before[c]) if present[c] else None) for c, _ in edits])
    restored = writer.packet()
    assert not writer.changes
    exact(OverlayReader(base, restored, codecs), before, present)
    assert len(restored) == len(pristine) == 256
    assert hashlib.sha256(base).hexdigest() == identity
    # Same latest states reached through different update histories have
    # identical encoded children. Generation belongs to the outer snapshot.
    a, b = OverlayWriter(base, codecs), OverlayWriter(base, codecs)
    a.apply(edits)
    b.apply([((0, 0, 0), 123), ((4, 5, 6), 456)])
    b.apply(list(reversed(edits)))
    assert a.packet()[HEADER.size:] == b.packet()[HEADER.size:]
    # Bind a patch to the precise root so coordinates cannot silently resolve
    # against different content. This is a functional identity check.
    another = before.copy()
    another[0, 0, 0] = 991
    alternate = Builder(another, present, codecs, 'regions_shared').finish()
    try:
        OverlayReader(alternate, first, codecs)
    except ValueError as error:
        assert str(error) == 'overlay belongs to another base'
    else:
        raise AssertionError('accepted an overlay for a different base')
    return {'exact_latest_snapshots': 100, 'pinned_snapshot_unchanged': True,
            'repeat_edits_keep_five_latest_entries': True,
            'latest_overlay_bytes_min': min(latest_sizes), 'latest_overlay_bytes_max': max(latest_sizes),
            'if_all_101_overlays_retained_bytes': retained_bytes,
            'retained_history_base_bytes': len(base),
            'restored_empty_overlay_bytes': len(restored), 'base_unchanged': True,
            'current_overlay_read_depth': 1, 'updates_deletes_insertions_zero_extremes_exact': True,
            'different_histories_same_child_packets': True, 'different_base_rejected': True,
            'first_snapshot_queries': work}


def replication(codecs):
    rows = []
    z, y, x = np.indices((64, 64, 64), dtype=np.uint64)
    for seed in (107, 128):
        rng = np.random.default_rng(seed)
        motif = rng.integers(0, 1 << 64, size=(8, 8, 8), dtype=np.uint64)
        anchors = rng.integers(0, 1 << 64, size=(8, 8, 8), dtype=np.uint64)
        before = motif[z % 8, y % 8, x % 8] + anchors[z // 8, y // 8, x // 8]
        present = np.ones(before.shape, dtype=np.bool_)
        base = Builder(before, present, codecs, 'regions_shared').finish()
        after = before.copy()
        after[3::8, 4::8, 5::8] += rng.integers(1, 1 << 32, size=(8, 8, 8), dtype=np.uint64)
        writer = OverlayWriter(base, codecs)
        writer.apply((tuple(c), int(after[tuple(c)])) for c in np.argwhere(before != after))
        packet = writer.packet()
        exact(OverlayReader(base, packet, codecs), after, present)
        rows.append({'seed': seed, 'base_bytes': len(base), 'overlay_bytes': len(packet),
                     'total_bytes': len(base)+len(packet), 'exact': True})
    return rows


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with Database() as db:
        codecs = Codecs(db)
        rows = measure(codecs)
        history = lifecycle(codecs)
        repeats = replication(codecs)
        print('lifecycle', history, flush=True)
    report = {
        'recorded_utc': datetime.now(timezone.utc).isoformat(), 'platform': platform.platform(),
        'measurements': rows, 'lifecycle': history, 'replication': repeats,
        'protocol': 'Complete immutable base plus current overlay bytes. Same native codecs as the prior structural experiment. Best linear comparison tries six axis orders. Base and overlay reopened from disk. Queries use fresh per-packet caches; counters are logical work in memory, not disk I/O or SQL latency. Writer materializes the base and constructs sparse arrays; update timing excludes writer initialization and is not native engine performance.',
        'source_sha256': {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in (
            'experiments/archive/exact_fold/overlays.py', 'experiments/archive/exact_fold/regions.py',
            'ext/sqlnew/fold.c', 'ext/sqlnew/sqlnew.c', 'ext/sqlnew/columnar.c')},
        'library_sha256': hashlib.sha256((ROOT/'build/sqlnew/sqlnewdb.dll').read_bytes()).hexdigest(),
    }
    path = OUT/'results.json'
    path.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print('saved', path, flush=True)


if __name__ == '__main__':
    main()
