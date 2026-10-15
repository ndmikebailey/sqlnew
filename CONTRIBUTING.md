# Contributing to SQLNEW

SQLNEW is an independent experimental fork. Use this repository's issues and
pull requests for SQLNEW changes. SQLite's own contribution policies apply to
its upstream project; this fork does not speak for SQLite's developers.

Start with [the build and SQL guide](sqlnew/README.md). Use Python 3.12 or later
for development and install `requirements-dev.txt` for vector APIs and benchmarks.
Keep changes focused and describe the observable behavior, a reproduction, and
the checks run. Reports with small SQL examples are especially useful.

## Source and checks

- Native SQLNEW modules are in `ext/sqlnew/`; Python helpers and product checks
  are in `sqlnew/`. Keep public documentation aligned with actual behavior.
- Use SQLite allocators and C89/C99-compatible C. Preserve upstream blessing
  comments. Do not add copyright or license headers to source files.
- Edit source templates, then regenerate through the build. Do not hand-edit
  amalgamations, parser output, opcode tables, or generated headers.
- Build with `python sqlnew/build.py`, run
  `python -m unittest discover -s sqlnew -p "test_*.py" -v`, and run
  `python sqlnew/exercise_dev021.py`. These commands use unittest/direct SQL;
  pytest is not required.
- After a change to `engine/sqlite/src/`, also run the upstream `devtest` target.
  On Linux, create `build/core-devtest`, run `../../engine/sqlite/configure --dev` there, then
  `make tclextension-install` and `make -j2 devtest`. Tcl development libraries
  and a writable Tcl package directory are required. Windows can use
  `nmake /f <engine-source>/Makefile.msc TOP=<engine-source> DEBUG=0 TCLDIR=<tcl> devtest`
  from a separate build directory with a working Tcl SQLite extension.
- Measure performance changes with exact answer checks and disclose setup,
  complete file sizes, hardware, and query costs. Preserve regressions as well
  as favorable results. See [benchmark methodology](sqlnew/BENCHMARKS.md).

## Experiments and upstream intake

Add narrative attempt summaries to the single
[attempt journal](experiments/exact_fold/ATTEMPTS.txt). Archive superseded
prototypes under `experiments/archive/`; the native build must not depend on
them. Keep generated databases, binaries, downloaded corpora and local snapshots
under ignored `build/`. Retain small machine-readable measurement reports with
their source hashes in `sqlnew/results/`.

For an upstream update, pin its Git and Fossil identities in
`sqlnew/upstream.json`, preserve the SQLNEW compiler hooks and configuration,
and include dependent fixes together. Rebuild generated files and compare SQLNEW
contracts and benchmarks. Changes to the encoded file formats need explicit
format documentation; no automatic migration promise currently exists.

## Licensing

Keep additions consistent with the repository's public-domain policy and
preserve the separate notices for bundled build tools described in
[LICENSE.md](LICENSE.md). Identify any proposed third-party dependency and its
provenance. Do not assert SQLite authorship or upstream acceptance for fork code.
