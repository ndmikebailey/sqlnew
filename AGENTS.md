# Working on SQLNEW

SQLNEW is an independent experimental embedded SQL engine maintained in this
Git repository: <https://github.com/ndmikebailey/sqlnew>. Use its issues and pull
requests for SQLNEW work. The product is currently `0.2.1-dev`, with its own
`SQLNEW format 2` database signature and no promised migration compatibility.

## Start here

- `README.md`: purpose, supported features and recorded comparisons.
- `sqlnew/README.md`: product build and SQL usage.
- `CONTRIBUTING.md`: contribution scope and validation expectations.
- `sqlnew/FORMAT.md`: native storage contracts and current limits.
- `sqlnew/upstream.json`: exact inherited SQLite Git/Fossil provenance.

## Repository layout

- `ext/sqlnew/`: SQLNEW native C modules, codecs, columns and regions.
- `sqlnew/`: build/release tools, Python driver, workflows and product checks.
- `engine/sqlite/`: inherited SQL compiler, pager, VFS, build tools and tests.
  Its `src/select.c` contains SQLNEW compiler hooks behind `SQLITE_ENABLE_SQLNEW`.
- `experiments/archive/`: preserved prototypes, outside the product build.
- `experiments/exact_fold/ATTEMPTS.txt`: the single narrative attempt journal.
- `build/`: ignored generated sources, binaries, databases, corpora and logs.

Keep the inherited engine tree internally intact. Its generic build targets
build the inherited core; the SQLNEW builder enables and links product modules.
SQLite's own contribution rules and Fossil workflow apply to that separate
upstream project. They are not this repository's contribution instructions.

## Build and check

Python 3.12+ and a C compiler are required. Windows discovers Visual Studio C++
Build Tools; Linux uses cc and make. Run from the repository root:

```console
python sqlnew/build.py
python -m pip install -r requirements-dev.txt
python -m unittest discover -s sqlnew -p "test_*.py" -v
python sqlnew/exercise_dev021.py
```

`SQLNEW_BUILD_DIR` selects an alternate output root. Set the same value for the
builder, driver and validation processes. `SQLNEW_DISABLE_SIMD=1` builds the
scalar backend; keep that variable set during its product checks to assert it.

After any change to `engine/sqlite/src/`, also run at least core `devtest`.
For Linux, from `build/core-devtest/`:

```sh
../../engine/sqlite/configure --dev
make tclextension-install
make -j2 devtest
```

This needs Tcl development libraries and a writable Tcl package directory.
The GitHub workflow shows the hosted installation commands. On Windows use
`engine/sqlite/Makefile.msc` with `TOP` pointing to that engine directory and a
working Tcl toolchain, from a separate build directory. Core devtest does not
register SQLNEW; product checks cover the enabled modules and compiler hooks.

Use checks appropriate to the change. Do not delegate work, introduce pytest,
or start a separate security/sanitizer campaign unless the owner requests it.

## Editing rules

- Preserve upstream blessing comments unchanged. Do not add copyright or
  license headers. Preserve notices for bundled build tools in `LICENSE.md`.
- Use C89/C99-compatible C, SQLite allocators (`sqlite3Malloc`,
  `sqlite3_malloc64`, `sqlite3MallocZero`), and `SQLITE_*` error codes.
- Use `i64`/`sqlite3_int64`, `u32` and `u64` for stored widths; avoid bare `long`
  or `int` for values that may exceed 2 GiB. Keep invariant assertions.
- Edit source templates, not generated amalgamations, parser files, opcode
  tables or headers. Generators live under `engine/sqlite/tool/`.
- Grammar changes belong in `engine/sqlite/src/parse.y`; new PRAGMAs belong in
  `engine/sqlite/tool/mkpragmatab.tcl`; opcodes start in `src/vdbe.c` there.
- The internal core header is `engine/sqlite/src/sqliteInt.h`; the public API
  template is `engine/sqlite/src/sqlite.h.in`. SQLNEW configuration is in
  `sqlnew/config.h`.

## Experiments and evidence

Record hypotheses, unsuccessful attempts, corrections and decisions in the
single attempt journal. Keep prototypes reproducible and archive superseded
ones. Do not make the native product depend on archived experiments.

Benchmark exact results before timing. Disclose setup, complete file sizes,
index costs, changes/compaction, hardware and regressions. Native payload
counters are not measurements of operating-system I/O. Keep fixture identities
and source/library hashes with small reports in `sqlnew/results/`; preserve
historical reports unchanged. Generated output belongs in ignored `build/`.

Pin coherent upstream intakes in `sqlnew/upstream.json`, preserve SQLNEW hooks,
and rerun product and core checks. Document format changes explicitly. SQLNEW
does not imply upstream SQLite authorship or acceptance of fork changes.
