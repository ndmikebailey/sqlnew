"""Preserve or restore the exact benchmark originals, without network access."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

from benchmark_documents import corpus, SPEC
from db import ROOT, BUILD


def filename(source):
    return source.get('cache_file',Path(source.get('path','')).name)


def main():
    parser = argparse.ArgumentParser(__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--create',type=Path,help='archive already-prepared pinned originals')
    mode.add_argument('--extract',type=Path,help='restore pinned originals into the ignored corpus cache')
    args = parser.parse_args()
    spec = json.loads(SPEC.read_text(encoding='utf-8'))
    cache = BUILD/'document-corpus'
    if args.extract:
        with zipfile.ZipFile(args.extract) as archive:
            originals = {filename(s):archive.read(filename(s)) for s in spec['sources']}
        for source in spec['sources']:
            raw = originals[filename(source)]
            if len(raw)!=source['bytes'] or hashlib.sha256(raw).hexdigest()!=source['sha256']:
                raise RuntimeError('original differs from corpus pin: '+source['id'])
        cache.mkdir(parents=True,exist_ok=True)
        for name, raw in originals.items():
            (cache/name).write_bytes(raw)
        _, documents, rows = corpus()
        print('Restored exact corpus:',len(documents),'documents,',len(rows),'default passages')
        return
    corpus()  # Verify every original before collecting it.
    files = {}
    for source in spec['sources']:
        name = filename(source)
        path = cache/name
        if not path.exists():
            path = ROOT/source['path']
        files[name] = path.read_bytes()
    files['SOURCE_MANIFEST.json'] = SPEC.read_bytes()
    files['README.txt'] = (
        'SQLNEW public-documents-v1 benchmark originals\n\n'
        'Four unmodified Project Gutenberg UTF-8 ebooks and three SQLite technical\n'
        'documents. The ebooks retain their complete original notices and license\n'
        'text. SOURCE_MANIFEST.json identifies every original and its source.\n\n'
        'From a SQLNEW source checkout, restore without downloading anything:\n'
        '  python sqlnew/corpus_archive.py --extract PATH/TO/public-documents-v1.zip\n'
        '  python sqlnew/benchmark_documents.py\n\n'
        'The restore checks all original byte counts and SHA-256 identities.\n'
        'Build the native engine and pinned baseline before running the benchmark.\n'
        'Corpus coverage is bounded English prose and technical text.\n').encode()
    output = args.create.resolve()
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'w') as archive:
        for name,raw in sorted(files.items()):
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = 0o100644<<16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info,raw,compresslevel=9)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix+'.sha256').write_text(digest+'  '+output.name+'\n',encoding='utf-8')
    print('Corpus archive:',output,'bytes',output.stat().st_size,'sha256',digest)


if __name__=='__main__':
    main()
