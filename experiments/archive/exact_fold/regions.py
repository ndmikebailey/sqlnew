"""Explore direct region storage using SQLNEW's existing exact codecs.

This is an isolated, single-process representation experiment, not an engine
format or a SQL module. All narrative findings belong in ATTEMPTS.txt.
"""
import argparse
from collections import Counter
import hashlib
import itertools
import json
from pathlib import Path
import platform
import struct
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'sqlnew'))
from db import Database

OUT = ROOT / 'build/math-exploration/regions'
MASK64 = (1 << 64) - 1
HEAD = struct.Struct('<4sBBBBIIIIQQQ16x')
RECORD = struct.Struct('<BBHI')
EDGE = struct.Struct('<QQ')
VARIANTS = {
    'regions_existing': (False, False, False),
    'regions_delta': (False, False, True),
    'regions_shared': (True, True, True),
    'regions_shared_no_delta': (True, True, False),
}


class Codecs:
    def __init__(self, db):
        self.db = db

    def best(self, raw):
        packed = self.db.scalar('SELECT sqlnew_pack(?)', (raw,))
        folded = self.db.scalar('SELECT sqlnew_fold(?)', (raw,))
        both = self.db.scalar('SELECT sqlnew_pack(?)', (folded,))
        return min(enumerate((raw, packed, folded, both)),
                   key=lambda pair: (len(pair[1]), pair[0]))

    def decode(self, codec, body):
        if codec == 0:
            return body
        sql = {1: 'SELECT sqlnew_unpack(?)', 2: 'SELECT sqlnew_unfold(?)',
               3: 'SELECT sqlnew_unfold(sqlnew_unpack(?))'}[codec]
        return self.db.scalar(sql, (body,))


def difference(values, axes, inverse=False):
    result = values.copy()
    for bit, axis in enumerate((2, 1, 0)):
        if not axes & (1 << bit):
            continue
        if inverse:
            result = np.cumsum(result, axis=axis, dtype=np.uint64)
        else:
            before = [slice(None)] * 3
            after = [slice(None)] * 3
            before[axis], after[axis] = slice(None, -1), slice(1, None)
            result[tuple(after)] = (result[tuple(after)] - result[tuple(before)])
    return result


class Builder:
    def __init__(self, values, occupied, codecs, variant, leaf_log=3):
        self.values = np.asarray(values, dtype='<u8')
        self.occupied = occupied
        self.codecs = codecs
        self.normalize, self.share, self.transforms = VARIANTS[variant]
        self.edge = 1 << leaf_log
        self.leaf_log = leaf_log
        self.data = bytearray(HEAD.size)
        self.intern = {}
        self.leaf_cache = {}
        self.logical = Counter()
        self.physical = Counter()
        self.modes = Counter()

    def record(self, kind, flags, aux, body):
        record = RECORD.pack(kind, flags, aux, len(body)) + body
        self.logical['records'] += 1
        self.logical['bytes'] += len(record)
        # Exact byte equality is authoritative, including after hash collisions.
        if self.share and record in self.intern:
            return self.intern[record]
        offset = len(self.data)
        self.data.extend(record)
        if self.share:
            self.intern[record] = offset
        self.physical['records'] += 1
        self.physical['directory_bytes' if kind == 0 else 'leaf_bytes'] += len(record)
        return offset

    def leaf(self, origin):
        slices = tuple(slice(o, min(o + self.edge, n))
                       for o, n in zip(origin, self.values.shape))
        source = self.values[slices]
        present = self.occupied[slices]
        if not present.any():
            return None
        local = tuple(slice(0, n) for n in source.shape)
        cube = np.zeros((self.edge,) * 3, dtype='<u8')
        mask = np.zeros(cube.shape, dtype=np.bool_)
        mask[local] = present
        anchor = int(source[present][0]) if self.normalize else 0
        cube[local] = np.where(present, source - np.uint64(anchor), np.uint64(0))
        flags = int(mask.all())
        bitmap = b'' if flags else np.packbits(mask.ravel(), bitorder='little').tobytes()
        cache_key = bitmap, flags, cube.tobytes()
        selected = self.leaf_cache.get(cache_key)
        if selected is None:
            candidates = []
            for axes in range(8) if self.transforms else (0,):
                raw = difference(cube, axes).tobytes() if axes else cube.tobytes()
                codec, body = self.codecs.best(raw)
                candidates.append((len(body), axes, codec, body))
            _, axes, codec, body = min(candidates)
            selected = axes, codec, body
            self.leaf_cache[cache_key] = selected
        axes, codec, body = selected
        self.modes[f'axes={axes},codec={codec}'] += 1
        return self.record(1, flags, axes | (codec << 8), bitmap + body), anchor

    def region(self, origin, span):
        if any(o >= n for o, n in zip(origin, self.values.shape)):
            return None
        if span == self.edge:
            return self.leaf(origin)
        half = span // 2
        children = []
        child_mask = 0
        for slot in range(8):
            child_origin = tuple(origin[i] + half * ((slot >> (2-i)) & 1)
                                 for i in range(3))
            child = self.region(child_origin, half)
            if child is not None:
                children.append(child)
                child_mask |= 1 << slot
        if not children:
            return None
        anchor = children[0][1] if self.normalize else 0
        body = bytes((child_mask,)) + b''.join(
            EDGE.pack(offset, (base - anchor) & MASK64) for offset, base in children)
        return self.record(0, 0, 0, body), anchor

    def finish(self):
        span = self.edge
        while span < max(self.values.shape):
            span *= 2
        root = self.region((0, 0, 0), span)
        offset, anchor = root or (0, 0)
        header = HEAD.pack(b'SRG1', 1, int(self.normalize), self.leaf_log, 0,
                           *self.values.shape, span, offset, anchor,
                           int(self.occupied.sum()))
        self.data[:HEAD.size] = header
        return bytes(self.data)


class Reader:
    def __init__(self, data, codecs):
        self.data, self.codecs = data, codecs
        magic, version, normalized, log, reserved, z, y, x, span, root, anchor, count = HEAD.unpack_from(data)
        assert magic == b'SRG1' and version == 1 and reserved == 0
        self.shape, self.span, self.root = (z, y, x), span, root
        self.edge, self.anchor, self.count = 1 << log, anchor, count

    def tiles(self, box, stats):
        seen = set()
        decoded = {}
        stats.update(header_bytes=HEAD.size, record_bytes=0, record_visits=0,
                     records_read=0, decoded_words=0, leaf_visits=0)

        def walk(offset, anchor, origin, span):
            if any(o >= hi or o + span <= lo
                   for o, (lo, hi) in zip(origin, box)):
                return
            kind, flags, aux, size = RECORD.unpack_from(self.data, offset)
            stats['record_visits'] += 1
            if offset not in seen:
                seen.add(offset)
                stats['records_read'] += 1
                stats['record_bytes'] += RECORD.size + size
            start = offset + RECORD.size
            if kind == 0:
                child_mask = self.data[start]
                pos, half = start + 1, span // 2
                for slot in range(8):
                    if child_mask & (1 << slot):
                        child_offset, delta = EDGE.unpack_from(self.data, pos)
                        pos += EDGE.size
                        child_origin = tuple(origin[i] + half * ((slot >> (2-i)) & 1)
                                             for i in range(3))
                        yield from walk(child_offset, (anchor + delta) & MASK64,
                                        child_origin, half)
            else:
                assert kind == 1 and span == self.edge
                stats['leaf_visits'] += 1
                if offset not in decoded:
                    words = self.edge ** 3
                    mask_size = 0 if flags else (words + 7) // 8
                    mask = (np.ones(words, dtype=np.bool_) if flags else
                            np.unpackbits(np.frombuffer(self.data[start:start+mask_size], dtype=np.uint8),
                                          bitorder='little')[:words].astype(np.bool_))
                    raw = self.codecs.decode(aux >> 8, self.data[start+mask_size:start+size])
                    assert len(raw) == words * 8
                    values = np.frombuffer(raw, dtype='<u8').reshape((self.edge,) * 3)
                    values = difference(values, aux & 7, inverse=True)
                    decoded[offset] = values, mask.reshape(values.shape)
                    stats['decoded_words'] += words
                values, mask = decoded[offset]
                yield origin, values + np.uint64(anchor), mask

        if self.root:
            yield from walk(self.root, self.anchor, (0, 0, 0), self.span)

    def select(self, box=None):
        box = box or tuple((0, n) for n in self.shape)
        shape = tuple(hi-lo for lo, hi in box)
        result, present = np.zeros(shape, dtype='<u8'), np.zeros(shape, dtype=np.bool_)
        stats = {}
        for origin, values, mask in self.tiles(box, stats):
            lo = tuple(max(o, b[0]) for o, b in zip(origin, box))
            hi = tuple(min(o+self.edge, b[1], n)
                       for o, b, n in zip(origin, box, self.shape))
            if any(a >= b for a, b in zip(lo, hi)):
                continue
            dest = tuple(slice(a-b[0], h-b[0]) for a, h, b in zip(lo, hi, box))
            source = tuple(slice(a-o, h-o) for a, h, o in zip(lo, hi, origin))
            result[dest] = np.where(mask[source], values[source], np.uint64(0))
            present[dest] = mask[source]
        stats['requested_cells'] = int(present.sum())
        stats['encoded_bytes_touched'] = stats['header_bytes'] + stats['record_bytes']
        return result, present, stats


def ensure_exact(reader, values, occupied, box=None):
    got, present, stats = reader.select(box)
    slices = tuple(slice(lo, hi) for lo, hi in box) if box else (...,)
    want, mask = values[slices], occupied[slices]
    assert np.array_equal(mask, present), 'occupancy differs'
    assert np.array_equal(want[mask], got[mask]), 'value bits differ'
    return stats


def fixtures():
    rng = np.random.default_rng(6101421)
    z, y, x = np.indices((64, 64, 64), dtype=np.uint64)
    row = z * np.uint64(64) + y
    yield 'quadratic_columns', (1700000000000 + 3*row*row + 1000*row + 13*x).astype('<u8'), None
    motif = rng.integers(0, 1 << 64, size=(8, 8, 8), dtype=np.uint64)
    anchors = rng.integers(0, 1 << 64, size=(8, 8, 8), dtype=np.uint64)
    shared = motif[z % 8, y % 8, x % 8] + anchors[z // 8, y // 8, x // 8]
    yield 'shared_irregular_motif', shared, None
    perturbed = shared.copy()
    # One changed occupied value in each 512-word leaf, with independent changes.
    perturbed[3::8, 4::8, 5::8] += rng.integers(1, 1 << 32, size=(8, 8, 8), dtype=np.uint64)
    yield 'motif_one_edit_per_region', perturbed, None
    yield 'independent_bits', rng.integers(0, 1 << 64, size=z.shape, dtype=np.uint64), None
    # A real source-byte control with an explicitly arbitrary geometric layout.
    paths = sorted((ROOT.parent/'doc2sql/src/doc2sql/codecs').glob('*.py'))
    raw = b''.join(path.read_bytes() for path in paths)[:2*1024*1024]
    count = len(raw) // 8
    raw = raw[:count*8]
    shape = ((count + 2047) // 2048, 64, 32)
    values = np.zeros(shape, dtype='<u8')
    occupied = np.zeros(shape, dtype=np.bool_)
    values.ravel()[:count] = np.frombuffer(raw, dtype='<u8')
    occupied.ravel()[:count] = True
    yield 'doc2sql_source_byte_window', values, occupied


def linear_baseline(values, occupied, codecs):
    mask = b'' if occupied.all() else np.packbits(occupied.ravel(), bitorder='little').tobytes()
    padded_mask = mask + b'\0' * (-len(mask) % 8)
    mask_codec, packed_mask = codecs.best(padded_mask) if mask else (0, b'')
    assert codecs.decode(mask_codec, packed_mask) == padded_mask
    choices = []
    for perm_id, perm in enumerate(itertools.permutations(range(3))):
        raw = values.transpose(perm).copy().tobytes()
        codec, packed = codecs.best(raw)
        assert codecs.decode(codec, packed) == raw
        header = HEAD.pack(b'LIN1', 1, codec, mask_codec, 0, *values.shape,
                           perm_id, len(packed), len(packed_mask), int(occupied.sum()))
        packet = header + packed + packed_mask
        choices.append((len(packet), perm_id, codec, packet))
    size, perm_id, codec, packet = min(choices)
    return {'bytes': size, 'row_major_bytes': choices[0][0], 'axis_permutation': perm_id,
            'six_axis_order_bytes': [choice[0] for choice in choices],
            'codec': codec, 'mask_codec': mask_codec, 'mask_bytes': len(packed_mask),
            'exact': True}, packet


def check_linear_packet(packet, values, occupied, codecs):
    magic, version, codec, mask_codec, reserved, z, y, x, perm_id, size, mask_size, count = HEAD.unpack_from(packet)
    assert magic == b'LIN1' and version == 1 and reserved == 0
    shape = (z, y, x)
    perm = list(itertools.permutations(range(3)))[perm_id]
    raw = codecs.decode(codec, packet[HEAD.size:HEAD.size+size])
    got = np.frombuffer(raw, dtype='<u8').reshape(tuple(shape[i] for i in perm))
    got = got.transpose(tuple(np.argsort(perm)))
    assert np.array_equal(got, values)
    assert count == int(occupied.sum())
    if mask_size:
        mask = codecs.decode(mask_codec, packet[HEAD.size+size:])
        mask = np.unpackbits(np.frombuffer(mask, dtype=np.uint8), bitorder='little')[:values.size]
        assert np.array_equal(mask.reshape(shape), occupied)
    else:
        assert occupied.all()


def measure(codecs, selected=None):
    rows = []
    for name, values, occupied in fixtures():
        if selected and name not in selected:
            continue
        occupied = np.ones(values.shape, dtype=np.bool_) if occupied is None else occupied
        baseline, linear_packet = linear_baseline(values, occupied, codecs)
        linear_path = OUT / (name + '-linear-best.lin')
        linear_path.write_bytes(linear_packet)
        check_linear_packet(linear_path.read_bytes(), values, occupied, codecs)
        row = {'fixture': name, 'shape_zyx': list(values.shape),
               'occupied_cells': int(occupied.sum()), 'logical_value_bytes': int(occupied.sum())*8,
               'source_sha256': hashlib.sha256(values[occupied].tobytes()).hexdigest(),
               'linear_best': baseline, 'variants': {}}
        for variant in VARIANTS:
            started = time.perf_counter()
            builder = Builder(values, occupied, codecs, variant)
            packet = builder.finish()
            elapsed = time.perf_counter() - started
            path = OUT / (name + '-' + variant + '.srg')
            path.write_bytes(packet)
            # Reopen the bytes from disk, without the builder or its intern map.
            reader = Reader(path.read_bytes(), codecs)
            full = ensure_exact(reader, values, occupied)
            queries = {}
            for axis in range(3):
                box = [(0, n) for n in values.shape]
                point = values.shape[axis] // 2
                box[axis] = (point, point + 1)
                queries['plane_' + 'zyx'[axis]] = ensure_exact(reader, values, occupied, box)
            point = tuple((n//2, n//2+1) for n in values.shape)
            queries['point'] = ensure_exact(reader, values, occupied, point)
            detail = {'bytes': len(packet), 'exact': True, 'encode_seconds': elapsed,
                      'full_read': full, 'queries': queries, 'physical': dict(builder.physical),
                      'logical': dict(builder.logical), 'modes': dict(builder.modes),
                      'packet_sha256': hashlib.sha256(packet).hexdigest()}
            row['variants'][variant] = detail
            print(name, variant, 'bytes', len(packet), 'linear', row['linear_best']['bytes'],
                  'encode_s', round(elapsed, 3), 'point_bytes', queries['point']['encoded_bytes_touched'],
                  flush=True)
        rows.append(row)
        (OUT/'partial-results.json').write_text(json.dumps(rows, indent=2)+'\n', encoding='utf-8')
    return rows


def verify_finding(codecs):
    """Replication and a locality control, without adding a new codec."""
    z, y, x = np.indices((64, 64, 64), dtype=np.uint64)
    rows = []
    for seed in (107, 128):
        rng = np.random.default_rng(seed)
        motif = rng.integers(0, 1 << 64, size=(8, 8, 8), dtype=np.uint64)
        anchors = rng.integers(0, 1 << 64, size=(8, 8, 8), dtype=np.uint64)
        values = motif[z % 8, y % 8, x % 8] + anchors[z // 8, y // 8, x // 8]
        occupied = np.ones(values.shape, dtype=np.bool_)
        for edits in ('none', 'one_global', 'one_per_region'):
            source = values.copy()
            if edits == 'one_global':
                source[3, 4, 5] += np.uint64(17)
            elif edits == 'one_per_region':
                source[3::8, 4::8, 5::8] += rng.integers(1, 1 << 32, size=(8, 8, 8), dtype=np.uint64)
            baseline, _ = linear_baseline(source, occupied, codecs)
            packet = Builder(source, occupied, codecs, 'regions_shared').finish()
            ensure_exact(Reader(packet, codecs), source, occupied)
            rows.append({'seed': seed, 'edits': edits, 'region_bytes': len(packet),
                         'linear_best_bytes': baseline['bytes'], 'exact': True})
    return rows


def structural_checks(codecs):
    rng = np.random.default_rng(6101422)
    values = rng.integers(0, 1 << 64, size=(17, 25, 33), dtype=np.uint64)
    occupied = rng.random(values.shape) > .21
    values[0, 0, :4] = [0, (1 << 64)-1, 1 << 63, 17]
    occupied[0, 0, :4] = True
    checks = 0
    sizes = []
    for perm in itertools.permutations(range(3)):
        source, mask = values.transpose(perm).copy(), occupied.transpose(perm).copy()
        builder = Builder(source, mask, codecs, 'regions_shared')
        packet = builder.finish()
        reader = Reader(packet, codecs)
        ensure_exact(reader, source, mask)
        sizes.append(len(packet))
        checks += 1
        # Each ordering of fixed independent axis selections must identify
        # the same region. Bounds, not linear row offsets, select the cells.
        for order in itertools.permutations(range(3)):
            bounds = [(0, n) for n in source.shape]
            for axis in order:
                bounds[axis] = (2, source.shape[axis] - 3)
                ensure_exact(reader, source, mask, bounds)
                checks += 1
    # Splitting outside the internal tile grid and recomposing exact values
    # checks semantics; it does NOT claim zero-copy persistent region merging.
    cut = 11
    pieces = []
    for a, b in ((0, cut), (cut, values.shape[2])):
        source, mask = values[:, :, a:b], occupied[:, :, a:b]
        reader = Reader(Builder(source, mask, codecs, 'regions_shared').finish(), codecs)
        got, present, _ = reader.select()
        pieces.append((got, present))
        checks += 1
    joined = np.concatenate([p[0] for p in pieces], axis=2)
    joint_mask = np.concatenate([p[1] for p in pieces], axis=2)
    assert np.array_equal(joint_mask, occupied)
    assert np.array_equal(joined[occupied], values[occupied])
    whole = Builder(values, occupied, codecs, 'regions_shared').finish()
    rebuilt = Builder(joined, joint_mask, codecs, 'regions_shared').finish()
    assert rebuilt == whole
    checks += 1
    # Empty occupancy is distinct from an occupied cell whose bits are zero.
    for mask in (np.zeros((1, 1, 1), dtype=np.bool_), np.ones((1, 1, 1), dtype=np.bool_)):
        source = np.zeros(mask.shape, dtype='<u8')
        ensure_exact(Reader(Builder(source, mask, codecs, 'regions_shared').finish(), codecs), source, mask)
        checks += 1
    return {'exact_checks': checks, 'axis_permutation_packet_bytes': sizes,
            'split_recompose_canonical_bytes': True,
            'scope': 'Exact numeric bits, sparse occupancy, unequal dimensions, all six axis permutations, all axis selection orders, split/reencode/recompose; no engine or security suite.'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixtures', nargs='*')
    parser.add_argument('--checks-only', action='store_true')
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    with Database() as db:
        codecs = Codecs(db)
        checks = structural_checks(codecs)
        print('structural checks', checks, flush=True)
        rows = [] if args.checks_only else measure(codecs, args.fixtures)
        replication = [] if args.checks_only else verify_finding(codecs)
    report = {'platform': platform.platform(), 'checks': checks, 'measurements': rows,
              'finding_replication': replication,
              'protocol': 'Python structural prototype calling existing native SQLNEW codecs. Full packets include headers, sparse occupancy, region directories, edge biases and compressed payloads. Read counters count distinct encoded records and unique decoded leaves with a fresh per-query cache; they are logical work, not physical disk I/O or native latency.',
              'source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'library_sha256': hashlib.sha256((ROOT/'build/sqlnew/sqlnewdb.dll').read_bytes()).hexdigest()}
    source_paths = sorted((ROOT.parent/'doc2sql/src/doc2sql/codecs').glob('*.py'))
    report['source_byte_control'] = {
        'paths': [{'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                  for path in source_paths],
        'description': 'Sorted codec Python source files concatenated, capped at 2 MiB and truncated to whole 64-bit words. Arbitrary z,y,x layout; this is source text, not extracted document evidence.'}
    path = OUT / ('checks.json' if args.checks_only else 'results.json')
    path.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print('saved', path, flush=True)


if __name__ == '__main__':
    main()
