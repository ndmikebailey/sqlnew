"""Build this SQLite fork and its extension with a local compiler. No downloads."""
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine/sqlite"
BUILD = Path(os.environ.get("SQLNEW_BUILD_DIR", str(ROOT / "build"))).resolve()


def run(args, cwd, env=None, timeout=None):
    print("+", subprocess.list2cmdline([str(a) for a in args]), flush=True)
    command = [str(a) for a in args]
    if env is not None:
        command[0] = shutil.which(command[0], path=env.get("PATH", env.get("Path"))) or command[0]
    subprocess.run(command, cwd=cwd, env=env, check=True, timeout=timeout)


def msvc_env():
    if shutil.which("cl"):
        return dict(os.environ)
    vswhere = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft Visual Studio/Installer/vswhere.exe"
    install = subprocess.check_output([str(vswhere), "-latest", "-products", "*", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"], text=True).strip()
    if not install:
        raise RuntimeError("Visual Studio C++ Build Tools are required")
    batch = Path(install) / "VC/Auxiliary/Build/vcvars64.bat"
    result = subprocess.check_output(f'cmd /d /s /c ""{batch}" >nul && set"', text=True)
    return dict(line.split("=", 1) for line in result.splitlines() if "=" in line and not line.startswith("="))


def generate(env=None):
    gen = BUILD / "upstream"
    gen.mkdir(parents=True, exist_ok=True)
    marker = gen / ".sqlnew-source-root"
    same_source = marker.exists() and marker.read_text() == str(ENGINE)
    if sys.platform == "win32":
        run(["nmake", "/nologo", "/f", ENGINE / "Makefile.msc", f"TOP={ENGINE}", "DEBUG=0", "sqlite3.c", "shell.c"], gen, env or msvc_env())
    else:
        if not (gen / "Makefile").exists() or not same_source:
            run([ENGINE / "configure", "--disable-tcl"], gen)
        run(["make", "-j2", "sqlite3.c", "shell.c"], gen)
    marker.write_text(str(ENGINE))
    return gen


def main():
    win = sys.platform == "win32"
    env = msvc_env() if win else None
    gen = generate(env)
    out = BUILD / "sqlnew"
    out.mkdir(exist_ok=True)
    defines = ["SQLITE_ENABLE_FTS5", "SQLITE_ENABLE_RTREE", "SQLITE_ENABLE_MATH_FUNCTIONS", "SQLITE_ENABLE_EXPLAIN_COMMENTS", "SQLITE_ENABLE_SQLNEW"]
    if os.environ.get("SQLNEW_DISABLE_SIMD") == "1":
        defines.append("SQLNEW_DISABLE_SIMD")
    sources = [gen / "sqlite3.c", *sorted((ROOT / "ext/sqlnew").glob("*.c"))]
    includes = b"".join(p.read_bytes() for p in sorted((ROOT / "ext/sqlnew").glob("*.inc")))
    if win:
        runtime = "/MT" if os.environ.get("SQLNEW_STATIC_CRT") == "1" else "/MD"
        cc = ["cl", "/nologo", "/O2", runtime, "/utf-8"]
        flags = ["/D" + d for d in defines] + [f"/FI{ROOT / 'sqlnew/config.h'}", "/DSQLITE_API=__declspec(dllexport)", "/DSQLITE_CORE", "/DSQLITE_EXTRA_INIT=sqlnew_bootstrap", f"/I{gen}"]
        objects = []
        for source in [*sources, gen / "shell.c"]:
            obj = out / (source.stem + "_core.obj")
            stamp = out / (source.stem + ".sha256")
            digest = hashlib.sha256(source.read_bytes() + (includes if source.name == "columnar.c" else b"") + (ROOT / 'sqlnew/config.h').read_bytes() + (gen / 'sqlite3.h').read_bytes() + repr([cc, flags]).encode()).hexdigest()
            if not obj.exists() or not stamp.exists() or stamp.read_text() != digest:
                run([*cc, *flags, "/c", source, f"/Fo:{obj}"], out, env)
                stamp.write_text(digest)
            objects.append(obj)
        run(["link", "/nologo", "/DLL", *objects[:-1], f"/OUT:{out / 'sqlnewdb.dll'}"], out, env)
        run(["link", "/nologo", *objects, f"/OUT:{out / 'sqlnew.exe'}", f"/IMPLIB:{out / 'sqlnew_shell.lib'}"], out, env)
        extension_flags = ['/DSQLNEW_DISABLE_SIMD'] if 'SQLNEW_DISABLE_SIMD' in defines else []
        run([*cc, "/W4", "/WX", *extension_flags, f"/I{gen}", "/LD", *sources[1:], "/link", f"/OUT:{out / 'sqlnew.dll'}"], out, env)
        (out / "python").mkdir(exist_ok=True)
        shutil.copy2(out / "sqlnewdb.dll", out / "python/sqlite3.dll")
    else:
        cc = [os.environ.get("CC", "cc"), "-O3", "-fPIC"]
        flags = ["-D" + d for d in defines] + ["-include", ROOT / "sqlnew/config.h", "-DSQLITE_CORE", "-DSQLITE_EXTRA_INIT=sqlnew_bootstrap", "-I", gen]
        ext = ".dylib" if sys.platform == "darwin" else ".so"
        shared = "-dynamiclib" if sys.platform == "darwin" else "-shared"
        libs = ["-lm", "-lpthread"] + ([] if sys.platform == "darwin" else ["-ldl"])
        objects = []
        for source in [*sources, gen / "shell.c"]:
            obj = out / (source.stem + "_core.o")
            stamp = out / (source.stem + ".sha256")
            digest = hashlib.sha256(source.read_bytes() + (includes if source.name == "columnar.c" else b"") + (ROOT / "sqlnew/config.h").read_bytes() + (gen / "sqlite3.h").read_bytes() + repr([cc, flags]).encode()).hexdigest()
            if not obj.exists() or not stamp.exists() or stamp.read_text() != digest:
                run([*cc, *flags, "-c", source, "-o", obj], out)
                stamp.write_text(digest)
            objects.append(obj)
        run([*cc, shared, *objects[:-1], "-o", out / ("sqlnewdb" + ext), *libs], out)
        run([*cc, *objects, "-o", out / "sqlnew", *libs], out)
        extension_flags = ['-DSQLNEW_DISABLE_SIMD'] if 'SQLNEW_DISABLE_SIMD' in defines else []
        run([*cc, "-Wall", "-Wextra", "-Werror", *extension_flags, "-I", gen, shared, *sources[1:], "-o", out / ("sqlnew" + ext), "-lm"], out)
    source_files = [*sorted((ROOT / "ext/sqlnew").glob("*.c")),
                    *sorted((ROOT / "ext/sqlnew").glob("*.inc")),
                    ROOT / "sqlnew/config.h", ROOT / "sqlnew/build.py",
                    ENGINE / "src/select.c", ENGINE / "VERSION", ENGINE / "manifest.uuid"]
    hashes = {str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in source_files}
    version = (ROOT / "sqlnew/config.h").read_text().split('#define SQLNEW_VERSION "')[1].split('"')[0]
    library = out / ("sqlnewdb.dll" if win else "sqlnewdb" + ext)
    (BUILD / "build.json").write_text(json.dumps({"version": version, "compiler": cc,
        "defines": defines, "platform": sys.platform, "source_sha256": hashes,
        "generated_sha256": {name: hashlib.sha256((gen/name).read_bytes()).hexdigest()
                             for name in ('sqlite3.c', 'sqlite3.h', 'shell.c')},
        "library_sha256": hashlib.sha256(library.read_bytes()).hexdigest(),
        "upstream": json.loads((ROOT / "sqlnew/upstream.json").read_text())}, indent=2) + "\n")


if __name__ == "__main__":
    main()
