"""Direct reversible-representation experiments; no test framework required."""
import json
import math
from pathlib import Path
import random
import struct
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'sqlnew'))
from db import Database

MASK = (1 << 64) - 1
N = 1024


def transpose(raw):
    return b''.join(raw[j::8] for j in range(8))


def untranspose(raw):
    n = len(raw) // 8
    out = bytearray(len(raw))
    for j in range(8):
        out[j::8] = raw[j*n:(j+1)*n]
    return bytes(out)


def lift(values, mode):
    out = []
    previous = difference = 0
    for i, value in enumerate(values):
        if mode == 'xor':
            residual = value ^ previous
        elif mode == 'delta':
            residual = (value - previous) & MASK
        elif mode == 'second':
            residual = (value - previous - difference) & MASK
        else:
            residual = value
        if mode in ('delta', 'second'):
            residual = ((residual << 1) ^ (-(residual >> 63))) & MASK
        out.append(residual)
        difference = (value - previous) & MASK
        previous = value
    return out


def unlift(values, mode):
    out = []
    previous = difference = 0
    for residual in values:
        if mode in ('delta', 'second'):
            residual = ((residual >> 1) ^ (-(residual & 1))) & MASK
        if mode == 'xor':
            value = residual ^ previous
        elif mode == 'delta':
            value = (residual + previous) & MASK
        elif mode == 'second':
            value = (residual + previous + difference) & MASK
        else:
            value = residual
        out.append(value)
        difference = (value - previous) & MASK
        previous = value
    return out


def fixtures():
    rng = random.Random(61013)
    yield 'integer_quadratic', 'INTEGER', [1700000000000 + 11*i + 3*i*i for i in range(N)]
    yield 'integer_jittered_clock', 'INTEGER', [1700000000000 + 1000*i + rng.randrange(-3,4) for i in range(N)]
    yield 'integer_piecewise', 'INTEGER', [1000000000 + 7*i + 100000*(i//128) for i in range(N)]
    yield 'integer_random', 'INTEGER', [rng.randrange(-(1<<63),1<<63) for _ in range(N)]
    yield 'real_curve', 'REAL', [math.sin(i*.007) + i*.0001 for i in range(N)]
    yield 'real_decimal', 'REAL', [(123456 + rng.randrange(-250,251))/100 for _ in range(N)]
    yield 'real_gaussian', 'REAL', [rng.gauss(0,1) for _ in range(N)]
    yield 'real_wide', 'REAL', [math.ldexp(rng.uniform(-1,1),rng.randrange(-1000,1000)) for _ in range(N)]
    yield 'real_repeated', 'REAL', [3.141592653589793 if i%7 else -0.0 for i in range(N)]
    yield 'real_counter', 'REAL', [1700000000.0+i*.001 for i in range(N)]


def numeric(db):
    report = []
    for name, kind, values in fixtures():
        db.execute('CREATE VIRTUAL TABLE t USING sqlnew_columnar(x '+kind+')')
        with db.transaction():
            db.executemany('INSERT INTO t(rowid,x) VALUES(?,?)', enumerate(values))
        baseline = db.scalar('SELECT length(data) FROM t_data')
        stored = db.execute('SELECT x FROM t ORDER BY rowid')
        raw = b''.join(struct.pack('<q' if kind == 'INTEGER' else '<d', row[0]) for row in stored)
        words = struct.unpack('<'+str(N)+'Q',raw)
        candidates = []
        for mode in ('identity','xor','delta','second'):
            lifted = lift(words,mode)
            buf = struct.pack('<'+str(N)+'Q',*lifted)
            recovered = unlift(struct.unpack('<'+str(N)+'Q',untranspose(transpose(buf))),mode)
            if struct.pack('<'+str(N)+'Q',*recovered) != raw:
                raise RuntimeError('round-trip failed '+name+' '+mode)
            for planes in (False,True):
                payload = transpose(buf) if planes else buf
                # Same 32-byte CSC header footprint, plus outer SNZ frame.
                header=b'FLD1'+bytes([mode != 'identity',planes])+bytes(26)
                packed = db.scalar('SELECT sqlnew_pack(?)',(header+payload,))
                if db.scalar('SELECT sqlnew_unpack(?)',(packed,)) != header+payload:
                    raise RuntimeError('native packing round-trip failed')
                candidates.append({'mode':mode,'planes':planes,'bytes':len(packed)})
        row={'fixture':name,'kind':kind,'baseline_bytes':baseline,'candidates':candidates}
        report.append(row)
        print(name,'baseline',baseline,'best',min(candidates,key=lambda x:x['bytes']),flush=True)
        db.execute('DROP TABLE t')
    return report


def overlap_pool(strings):
    unique = sorted(set(strings))
    pool = b''
    locations = {}
    for word in unique:
        found = pool.find(word)
        if found < 0:
            overlap = min(len(pool),len(word))
            while overlap and pool[-overlap:] != word[:overlap]:
                overlap -= 1
            found = len(pool)-overlap
            pool += word[overlap:]
        locations[word]=(found,len(word))
    return struct.pack('<II',len(strings),len(pool))+b''.join(struct.pack('<II',*locations[s]) for s in strings)+pool


def unpack_pool(frame):
    n,size=struct.unpack_from('<II',frame)
    pool=frame[8+8*n:]
    if len(pool)!=size:raise RuntimeError('pool length')
    return [pool[a:a+b] for a,b in struct.iter_unpack('<II',frame[8:8+8*n])]


def strings(db):
    rng=random.Random(71)
    source=rng.randbytes(8192)
    fixtures={
        'overlap_windows': [source[i:i+256] for i in range(0,4096,4)],
        'common_logs': [f'host=server-{i%31:02d} event={i:06d} operation=measurement status=okay'.encode() for i in range(N)],
        'random_strings': [rng.randbytes(64) for _ in range(N)],
    }
    report=[]
    for name,values in fixtures.items():
        raw=b''.join(struct.pack('<I',len(s))+s for s in values)
        start=time.perf_counter();frame=overlap_pool(values);elapsed=time.perf_counter()-start
        if unpack_pool(frame)!=values:raise RuntimeError('pool round-trip')
        sizes={key:len(db.scalar('SELECT sqlnew_pack(?)',(data,))) for key,data in [('raw',raw),('pooled',frame)]}
        row={'fixture':name,**sizes,'pool_build_seconds_python':elapsed,'roundtrip':True}
        report.append(row);print(row,flush=True)
    return report


if __name__=='__main__':
    with Database(library=ROOT/'build/math-exploration/baseline/sqlnewdb.dll') as db:
        result={'numeric':numeric(db),'strings':strings(db),'note':'Size scout; Python transform timings are not native engine performance.'}
    path=ROOT/'build/math-exploration/scout.json'
    path.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
