"""Package a built SQLNEW preview and run its CLI from an isolated extraction."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

from build import ROOT, BUILD


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--output-dir', type=Path, default=BUILD/'dist')
    parser.add_argument('--allow-dirty', action='store_true',
                        help='local packaging rehearsal; marks the manifest as dirty')
    args = parser.parse_args()
    build = json.loads((BUILD/'build.json').read_text())
    dirty = bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT))
    if dirty and not args.allow_dirty:
        parser.error('commit source changes before packaging, or use --allow-dirty for a rehearsal')
    commit = subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    required = {'sqlnew/build.py','sqlnew/config.h','engine/sqlite/src/select.c'}
    if not required.issubset(build['source_sha256']):
        parser.error('rebuild with the current builder before packaging')
    for name, expected in build['source_sha256'].items():
        if sha(ROOT/name) != expected:
            parser.error('source changed since build: '+name)
    for name, expected in build['generated_sha256'].items():
        if sha(BUILD/'upstream'/name) != expected:
            parser.error('generated input changed since build: '+name)
    win = sys.platform == 'win32'
    if win and '/MT' not in build['compiler']:
        parser.error('Windows downloads require a build with SQLNEW_STATIC_CRT=1')
    suffix = '.dll' if win else '.dylib' if sys.platform == 'darwin' else '.so'
    native = BUILD/'sqlnew'
    if sha(native/('sqlnewdb'+suffix)) != build['library_sha256']:
        parser.error('native library differs from build manifest')
    backend = 'scalar' if 'SQLNEW_DISABLE_SIMD' in build['defines'] else 'optimized'
    system = 'windows' if win else 'macos' if sys.platform == 'darwin' else 'linux'
    machine = platform.machine().lower()
    machine = {'amd64':'x86_64','aarch64':'arm64'}.get(machine,machine)
    name = f"sqlnew-{build['version']}-{system}-{machine}-{backend}-{commit[:9]}"
    output = args.output_dir.resolve()
    output.mkdir(parents=True,exist_ok=True)
    files = {'sqlnew.exe' if win else 'sqlnew':native/('sqlnew.exe' if win else 'sqlnew'),
             'lib/sqlnewdb'+suffix:native/('sqlnewdb'+suffix),
             'lib/sqlnew'+suffix:native/('sqlnew'+suffix),
             'include/sqlite3.h':BUILD/'upstream/sqlite3.h',
             'include/sqlnew_config.h':ROOT/'sqlnew/config.h',
             'examples/quickstart.sql':ROOT/'sqlnew/examples/quickstart.sql',
             'LICENSE.md':ROOT/'LICENSE.md',
             'UPSTREAM.json':ROOT/'sqlnew/upstream.json',
             'BUILD.json':BUILD/'build.json'}
    if win:
        files['lib/sqlnewdb.lib'] = native/'sqlnewdb.lib'
    for path in files.values():
        if not path.is_file():
            parser.error('missing package input: '+str(path))
    with tempfile.TemporaryDirectory(prefix='package-',dir=output) as temporary:
        staging = Path(temporary)/name
        staging.mkdir()
        for destination, source in files.items():
            target = staging/destination
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,target)
        cli_name = 'sqlnew.exe' if win else 'sqlnew'
        (staging/cli_name).chmod(0o755)
        command = '.\\sqlnew.exe' if win else './sqlnew'
        (staging/'README.txt').write_text(
            f"SQLNEW {build['version']} experimental preview ({backend})\n\n"
            f"Run from this extracted folder:\n  {command} :memory: \".read examples/quickstart.sql\"\n"
            f"Open a fresh file:\n  {command} my-data.sqlnew\n\n"
            "No Python, compiler or database server is needed for the CLI.\n"
            "Windows binaries include the C runtime. Linux binaries target the hosted\n"
            "Ubuntu runner's glibc baseline; see BUILD.json for build configuration.\n"
            "Optimized builds choose AVX2 at runtime when supported; scalar builds\n"
            "disable it. lib/ contains the embedded core and optional loadable module.\n"
            "Use the embedded core for the complete SQLNEW compiler optimizations.\n"
            "The public C API uses sqlite3_* names in include/sqlite3.h.\n\n"
            "Files use SQLNEW format 2. Use fresh databases; SQLite and earlier\n"
            "experimental files are not interchangeable. This preview makes no\n"
            "production or future format-compatibility commitment.\n\n"
            f"Source: https://github.com/ndmikebailey/sqlnew/tree/{commit}\n"
            "Guide: https://github.com/ndmikebailey/sqlnew/blob/main/sqlnew/README.md\n",
            encoding='utf-8')
        manifest = {'source_commit':commit,'working_tree_dirty':dirty,
                    'version':build['version'],'configuration':backend,
                    'platform':platform.platform(),'architecture':machine,
                    'files':{p.relative_to(staging).as_posix():sha(p)
                             for p in sorted(staging.rglob('*')) if p.is_file()}}
        (staging/'MANIFEST.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
        archive = output/(name+('.zip' if win else '.tar.gz'))
        if win:
            with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as bundle:
                for path in sorted(staging.rglob('*')):
                    if path.is_file():
                        entry = zipfile.ZipInfo(name+'/'+path.relative_to(staging).as_posix())
                        entry.compress_type = zipfile.ZIP_DEFLATED
                        entry.external_attr = (0o100755 if path.name==cli_name else 0o100644)<<16
                        bundle.writestr(entry,path.read_bytes())
        else:
            with archive.open('wb') as raw, gzip.GzipFile(filename='',fileobj=raw,mode='wb',mtime=0) as zipped:
                with tarfile.open(fileobj=zipped,mode='w') as bundle:
                    for path in sorted(staging.rglob('*')):
                        if path.is_file():
                            content = path.read_bytes()
                            entry = tarfile.TarInfo(name+'/'+path.relative_to(staging).as_posix())
                            entry.size = len(content)
                            entry.mode = 0o755 if path.name==cli_name else 0o644
                            bundle.addfile(entry,io.BytesIO(content))
        extraction = Path(temporary)/'extracted'
        extraction.mkdir()
        if win:
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(extraction)
        else:
            with tarfile.open(archive) as bundle:
                bundle.extractall(extraction,filter='data')
        extracted = extraction/name
        for path, expected in manifest['files'].items():
            assert sha(extracted/path)==expected,path
        run = subprocess.run([str(extracted/cli_name),':memory:',
                              '.read examples/quickstart.sql'],cwd=extracted,
                             text=True,capture_output=True,check=True)
        assert build['version'] in run.stdout and 'restored_passage' in run.stdout and 'ok' in run.stdout
    (output/'SHA256SUMS').write_text(''.join(
        f'{sha(p)}  {p.name}\n' for p in sorted(output.iterdir())
        if p.is_file() and (p.name.endswith('.zip') or p.name.endswith('.tar.gz'))),encoding='utf-8')
    print('Packaged and exercised extracted CLI:',archive,flush=True)


if __name__ == '__main__':
    main()
