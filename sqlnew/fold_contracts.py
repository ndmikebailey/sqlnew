"""Exact recovery and canonical ordered-merge contracts for the native fold API."""
import random
import struct


def canonical(db):
    rng = random.Random(107128)
    checks = 0
    for n in (0, 1, 2, 3, 127, 128, 129, 255, 256, 257, 1024, 4097):
        for origin in (0, 1, 55, 127, 128, 100001):
            values = [rng.randrange(1 << 64) for _ in range(n)]
            if n > 255:
                values = [(13*i*i + 1710000000000) & ((1 << 64)-1) for i in range(n)]
            raw = struct.pack('<' + str(n) + 'Q', *values)
            whole = db.scalar('SELECT sqlnew_fold(?,?)', (raw, origin))
            if db.scalar('SELECT sqlnew_unfold(?)', (whole,)) != raw:
                raise AssertionError('whole recovery')
            empty_left = db.scalar('SELECT sqlnew_fold(?,?)', (b'', origin))
            empty_right = db.scalar('SELECT sqlnew_fold(?,?)', (b'', origin+n))
            for left, right in ((empty_left, whole), (whole, empty_right)):
                if db.scalar('SELECT sqlnew_fold_merge(?,?)', (left, right)) != whole:
                    raise AssertionError('empty merge identity')
                checks += 1
            cuts = sorted(set([0, n] + [rng.randrange(n+1) for _ in range(13)]))
            leaves = [db.scalar('SELECT sqlnew_fold(?,?)', (raw[8*a:8*b], origin+a))
                      for a, b in zip(cuts, cuts[1:])] or [whole]
            for policy in ('left', 'right', 'random'):
                work = leaves[:]
                while len(work) > 1:
                    pos = (0 if policy == 'left' else len(work)-2 if policy == 'right'
                           else rng.randrange(len(work)-1))
                    work[pos:pos+2] = [db.scalar('SELECT sqlnew_fold_merge(?,?)',
                                                (work[pos], work[pos+1]))]
                if work[0] != whole:
                    raise AssertionError(f'canonical mismatch {n} {origin} {policy}')
                checks += 1
    return checks
