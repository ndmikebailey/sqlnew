"""Build a pristine, revision-matched SQLite baseline and common CSV/hash helpers."""
import hashlib
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tarfile

from build import ROOT, BUILD, msvc_env, run


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--fetch-upstream', action='store_true',
                        help='fetch the pinned public SQLite commit if absent from local Git history')
    args = parser.parse_args()
    out = BUILD / "comparison"
    out.mkdir(parents=True, exist_ok=True)
    commit = json.loads((ROOT / "sqlnew/upstream.json").read_text())["git_commit"]
    present = subprocess.run(['git', 'cat-file', '-e', commit+'^{commit}'], cwd=ROOT,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if not present:
        source_url = json.loads((ROOT/'sqlnew/upstream.json').read_text())['source']
        if not args.fetch_upstream:
            parser.error('Pinned SQLite commit is absent from local Git history. '
                         'Run again with --fetch-upstream to download the official baseline.')
        run(['git', 'fetch', '--no-tags', source_url, commit], ROOT)
    source = out / ("sqlite-" + commit[:12])
    if not (source / ".source-commit").exists():
        if source.exists():
            raise RuntimeError("incomplete baseline checkout: inspect " + str(source))
        archive = out / "pristine-sqlite.tar"
        run(["git", "archive", "--format=tar", "-o", archive, commit], ROOT)
        source.mkdir()
        with tarfile.open(archive) as files:
            for member in files.getmembers():
                target = (source / member.name).resolve()
                if not target.is_relative_to(source.resolve()):
                    raise RuntimeError("archive path escapes baseline directory")
            files.extractall(source, filter="data")
        (source / ".source-commit").write_text(commit)
    if (source / ".source-commit").read_text() != commit:
        raise RuntimeError("baseline source revision mismatch")
    gen = out / ("baseline-build-" + commit[:12])
    gen.mkdir(exist_ok=True)
    win = sys.platform == "win32"
    env = msvc_env() if win else None
    if win:
        run(["nmake", "/nologo", "/f", source / "Makefile.msc",
             f"TOP={source}", "DEBUG=0", "sqlite3.c"], gen, env)
    else:
        if not (gen / "Makefile").exists():
            run([source / "configure", "--disable-tcl"], gen)
        run(["make", "-j2", "sqlite3.c"], gen)
    settings = json.loads((BUILD / "build.json").read_text())
    defines = [x for x in settings["defines"] if x not in ("SQLITE_ENABLE_SQLNEW", "SQLNEW_DISABLE_SIMD")]
    if win:
        cc = ["cl", "/nologo", "/O2", "/MD", "/utf-8"]
        command = [*cc, *["/D" + x for x in defines], "/DSQLITE_CORE",
                   "/DSQLITE_API=__declspec(dllexport)", f"/I{gen}", "/LD",
                   gen / "sqlite3.c", "/link", f"/OUT:{out / 'sqlite-baseline.dll'}"]
    else:
        cc = settings["compiler"]
        suffix = '.dylib' if sys.platform == 'darwin' else '.so'
        shared = '-dynamiclib' if sys.platform == 'darwin' else '-shared'
        libs = ['-lm', '-lpthread'] + ([] if sys.platform == 'darwin' else ['-ldl'])
        command = [*cc, *["-D" + x for x in defines], "-DSQLITE_CORE", shared,
                   gen / "sqlite3.c", "-o", out / ('sqlite-baseline' + suffix), *libs]
    stamp = out / "baseline.sha256"
    digest = hashlib.sha256((gen / "sqlite3.c").read_bytes() + repr(command).encode()).hexdigest()
    if not stamp.exists() or stamp.read_text() != digest:
        run(command, out, env)
        stamp.write_text(digest)
    for name in ("csv", "shathree"):
        src = source / "ext/misc" / (name + ".c")
        if win:
            run([*cc, f"/I{gen}", "/LD", src, "/link", f"/OUT:{out / (name + '.dll')}"], out, env)
        else:
            run([*cc, "-I", gen, shared, src, "-o", out / (name + suffix)], out)
    report = {"baseline_commit": commit, "source": "Pristine git archive of the pinned upstream SQLite revision",
              "compiler": cc, "defines": defines, "baseline_command": [str(x) for x in command],
              "baseline_amalgamation_sha256": hashlib.sha256((gen / "sqlite3.c").read_bytes()).hexdigest(),
              "sqlnew_amalgamation_sha256": hashlib.sha256((BUILD / "upstream/sqlite3.c").read_bytes()).hexdigest(),
              "helpers": {name: hashlib.sha256((source / "ext/misc" / (name + ".c")).read_bytes()).hexdigest()
                          for name in ("csv", "shathree")}}
    (out / "build.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("Pristine baseline and common ingestion/validation helpers built")


if __name__ == "__main__":
    main()
