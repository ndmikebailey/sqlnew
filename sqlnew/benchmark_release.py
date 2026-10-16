"""Build the pinned public SQLite release with the product benchmark toolchain."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import urllib.request
import zipfile

from build import ROOT, BUILD, msvc_env, run
from db import Database, SUFFIX


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--fetch-release',action='store_true',help='permit downloading the pinned official source archive')
    args = parser.parse_args()
    pin = json.loads(Path(__file__).with_name('sqlite-release.json').read_text())
    out = BUILD/'comparison/sqlite-release'
    out.mkdir(parents=True,exist_ok=True)
    archive = out/('sqlite-amalgamation-'+pin['version']+'.zip')
    if not archive.exists():
        if not args.fetch_release:
            parser.error('source archive missing; run with --fetch-release')
        req = urllib.request.Request(pin['url'],headers={'User-Agent':'SQLNEW-release-comparison'})
        with urllib.request.urlopen(req,timeout=30) as response:
            content = response.read()
        if hashlib.sha3_256(content).hexdigest()!=pin['archive_sha3_256']:
            raise RuntimeError('official archive differs from the recorded release pin')
        archive.write_bytes(content)
    assert hashlib.sha3_256(archive.read_bytes()).hexdigest()==pin['archive_sha3_256']
    source = out/'source'
    source.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as files:
        for name in ('sqlite3.c','sqlite3.h'):
            matches = [p for p in files.namelist() if p.endswith('/'+name)]
            assert len(matches)==1
            (source/name).write_bytes(files.read(matches[0]))
    assert hashlib.sha3_256((source/'sqlite3.c').read_bytes()).hexdigest()==pin['amalgamation_sha3_256']
    product = json.loads((BUILD/'build.json').read_text())
    defines = [d for d in product['defines'] if d not in ('SQLITE_ENABLE_SQLNEW','SQLNEW_DISABLE_SIMD')]
    library = out/('sqlite-release'+SUFFIX)
    cc = product['compiler']
    win = sys.platform=='win32'
    if win:
        command = [*cc,*['/D'+d for d in defines],'/DSQLITE_CORE','/DSQLITE_API=__declspec(dllexport)',
                   '/LD',source/'sqlite3.c','/link','/OUT:'+str(library)]
    else:
        command = [*cc,*['-D'+d for d in defines],'-DSQLITE_CORE',
                   '-dynamiclib' if sys.platform=='darwin' else '-shared',source/'sqlite3.c',
                   '-o',library,'-lm','-lpthread',*([] if sys.platform=='darwin' else ['-ldl'])]
    stamp = out/'build.sha256'
    key = hashlib.sha256((source/'sqlite3.c').read_bytes()+repr(command).encode()).hexdigest()
    if not library.exists() or not stamp.exists() or stamp.read_text()!=key:
        run(command,out,msvc_env() if win else None)
        stamp.write_text(key)
    with Database(library=library) as db:
        assert db.scalar('SELECT sqlite_version()')==pin['version']
        assert db.scalar('SELECT sqlite_source_id()')==pin['source_id']
        options = [r[0] for r in db.execute('PRAGMA compile_options')]
    report = {'release':pin,'compiler':cc,'defines':defines,'command':[str(x) for x in command],
              'library_sha256':hashlib.sha256(library.read_bytes()).hexdigest(),
              'compile_options':options,'unmodified_official_amalgamation':True}
    (out/'build.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print('Released SQLite baseline:',pin['version'],library,flush=True)


if __name__=='__main__':
    main()
