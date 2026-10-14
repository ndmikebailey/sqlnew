"""Build and run the native checks under AddressSanitizer (local toolchain)."""
import os
import sys

from build import ROOT, BUILD, msvc_env, run


def main():
    out = BUILD / "native-check"
    out.mkdir(parents=True, exist_ok=True)
    gen = BUILD / "upstream"
    timeout = float(os.environ.get("SQLNEW_CHECK_TIMEOUT", "300"))
    if sys.platform == "win32":
        env = msvc_env()
        sources = [*sorted((ROOT / "ext/sqlnew").glob("*.c")), ROOT / "sqlnew/native_check.c"]
        run(["cl", "/nologo", "/MD", "/O1", "/Zi", "/fsanitize=address", "/DSQLITE_CORE", f"/I{gen}",
             *sources, BUILD / "sqlnew/sqlite3_core.obj", "/Fe:native_check.exe"], out, env)
        run([out / "native_check.exe"], out, env, timeout=timeout)
    else:
        sources = [gen / "sqlite3.c", *sorted((ROOT / "ext/sqlnew").glob("*.c")), ROOT / "sqlnew/native_check.c"]
        # This host also reproduces ASan signal loops in an empty PIE program.
        # A fixed-address test executable avoids that runtime problem without
        # disabling instrumentation or signal diagnostics. Product flags stay unchanged.
        layout = ["-fno-pie", "-no-pie"] if sys.platform.startswith("linux") else []
        run([os.environ.get("CC", "cc"), "-O1", "-g", "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
             *layout,
             "-DSQLITE_CORE", "-DSQLITE_ENABLE_SQLNEW", "-DSQLITE_EXTRA_INIT=sqlnew_bootstrap", "-include", ROOT / "sqlnew/config.h", "-I", gen,
             *sources, "-o", out / "native_check", "-lm", "-lpthread", "-ldl"], out)
        run([out / "native_check"], out, timeout=timeout)


if __name__ == "__main__":
    main()
