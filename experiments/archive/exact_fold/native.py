"""Build and measure exact folds, including canonical boundary-only merges."""
import ctypes as C
import hashlib
import json
from pathlib import Path
import random
import statistics
import struct
import sys
import time

import numpy as np

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'sqlnew'))
from build import msvc_env, run
from db import Database

OUT=ROOT/'build/math-exploration'


def compile_native():
    out=OUT/'native';out.mkdir(parents=True,exist_ok=True)
    run(['cl','/nologo','/O2','/MD','/W4','/WX',f'/I{ROOT / "build/upstream"}',
         '/LD',Path(__file__).with_name('fold.c'),'/link',f'/OUT:{out / "fold.dll"}'],out,msvc_env())
    return out/'fold.dll'


def load(db,path):
    enable=db.lib.sqlite3_enable_load_extension
    enable.argtypes,enable.restype=[C.c_void_p,C.c_int],C.c_int
    db.check(enable(db.handle,1))
    db.execute('SELECT load_extension(?,?)',(str(path),'sqlite3_fold_init'))
    db.check(enable(db.handle,0))


def timed(db,sql,args,repetitions=11):
    statement=db.prepare(sql)
    db.bind(statement,args)
    samples=[]
    try:
        for i in range(repetitions+2):
            start=time.perf_counter_ns()
            rc=db.lib.sqlite3_step(statement)
            if rc!=100:db.check(rc)
            db.check(db.lib.sqlite3_reset(statement))
            elapsed=(time.perf_counter_ns()-start)/1e6
            if i>=2:samples.append(elapsed)
    finally:db.lib.sqlite3_finalize(statement)
    return {'median_ms':statistics.median(samples),'samples_ms':samples}


from fold_contracts import canonical


def fixtures(n):
    rng=np.random.default_rng(61013)
    i=np.arange(n,dtype=np.int64)
    yield 'quadratic', (1700000000000+11*i+3*i*i).astype('<i8').tobytes()
    yield 'jittered_clock',(1700000000000+1000*i+rng.integers(-3,4,n)).astype('<i8').tobytes()
    yield 'piecewise',(1000000000+7*i+100000*(i//128)).astype('<i8').tobytes()
    yield 'float_clock',(1700000000.0+i*.001).astype('<f8').tobytes()
    yield 'float_curve',(np.sin(i*.007)+i*.0001).astype('<f8').tobytes()
    yield 'float_decimal',((123456+rng.integers(-250,251,n))/100).astype('<f8').tobytes()
    yield 'gaussian',rng.normal(size=n).astype('<f8').tobytes()
    yield 'random_bits',rng.bytes(n*8)


def measure(db,n):
    rows=[]
    for name,raw in fixtures(n):
        pack=db.scalar('SELECT sqlnew_pack(?)',(raw,))
        fold=db.scalar('SELECT sqlnew_fold(?)',(raw,))
        foldpack=db.scalar('SELECT sqlnew_pack(?)',(fold,))
        for sql,data in [('SELECT sqlnew_unpack(?)',pack),('SELECT sqlnew_unfold(?)',fold),
                         ('SELECT sqlnew_unfold(sqlnew_unpack(?))',foldpack)]:
            if db.scalar(sql,(data,))!=raw:raise RuntimeError(name+' recovery')
        cut=n//2+19
        left=db.scalar('SELECT sqlnew_fold(?,0)',(raw[:cut*8],))
        right=db.scalar('SELECT sqlnew_fold(?,?)',(raw[cut*8:],cut))
        merged=db.scalar('SELECT sqlnew_fold_merge(?,?)',(left,right))
        if merged!=fold:raise RuntimeError(name+' merge bytes')
        row={'fixture':name,'words':n,'raw_bytes':len(raw),'snz_bytes':len(pack),'fold_bytes':len(fold),
             'fold_snz_bytes':len(foldpack),'sha256':hashlib.sha256(raw).hexdigest(),'exact':True,
             'canonical_merge':True,'timings':{}}
        cases=[('snz_encode','SELECT sqlnew_pack(?)',(raw,)),('snz_decode','SELECT sqlnew_unpack(?)',(pack,)),
               ('fold_encode','SELECT sqlnew_fold(?)',(raw,)),('fold_decode','SELECT sqlnew_unfold(?)',(fold,)),
               ('fold_snz_encode','SELECT sqlnew_pack(sqlnew_fold(?))',(raw,)),
               ('fold_snz_decode','SELECT sqlnew_unfold(sqlnew_unpack(?))',(foldpack,)),
               ('fold_merge','SELECT sqlnew_fold_merge(?,?)',(left,right))]
        # Each operation has its own warmups; outputs are checked before timing.
        for key,sql,args in cases:
            row['timings'][key]=timed(db,sql,args)
        rows.append(row)
        print(name,{k:row[k] for k in ['snz_bytes','fold_bytes','fold_snz_bytes']},
              {k:round(v['median_ms'],3) for k,v in row['timings'].items()},flush=True)
    return rows


if __name__=='__main__':
    lib=compile_native()
    with Database(library=OUT/'baseline/sqlnewdb.dll') as db:
        load(db,lib)
        report={'canonical_checks':canonical(db),'measurements':measure(db,1<<18),
                'protocol':'Native SQL functions, prepared/prebound statements; step plus reset timed; output copying and Python input generation excluded; 2 warmups and 11 repetitions. Single Windows host.',
                'source_sha256':hashlib.sha256((ROOT/'ext/sqlnew/fold.c').read_bytes()).hexdigest()}
    (OUT/'native.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
