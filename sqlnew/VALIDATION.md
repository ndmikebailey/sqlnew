# Validation of SQLNEW development builds

## Common-task and released-SQLite comparison

The [100,000-row common-task matrix](results/dev021-common-tasks.json) passed
all **15 tasks x six configurations**, with **1,170 retained samples** plus two
warmups per combination. An independent Python oracle checks typed answers,
including NULLs and a SUM exceeding 32 bits. Committed insert/update/delete/
replace and rollback cases verify affected data. Complete ordered input hashes
match before/after restoration and after reopen in all six files.

The baseline includes unmodified released SQLite 3.54.0 and a separate pristine
3.54.1 snapshot matching SQLNEW's inherited core. Release archive, amalgamation,
runtime source ID, compiler flags and library hashes are recorded. The
[join-order diagnosis](results/dev021-join-order.json) checks six query/engine
combinations against the same restored inputs and confirms database-file hashes
are unchanged. Its measurements retain the original slow default plan.

A fresh [public-document run](results/dev021-documents-sqlite-release.json)
against released 3.54.0 passed all three storage arms: original reconstruction,
FTS answers and edits, native maintenance, restoration, integrity and reopen.
These checks validate the measured fixtures; they are not a concurrent-writer
or crash-recovery stress test. No native engine or format change was made, so
the existing product/core validation below remains applicable. See
[methods and limitations](BENCHMARKS.md#common-application-tasks-against-released-sqlite).

The [direct version audit](results/dev021-version-reads.json) additionally passed
four identical read tasks on retained 0.1.0, saved 0.2.0 and current 0.2.1 column
libraries: **12 combinations / 156 retained samples**, with independent answers,
matching full input hashes, integrity and reopen checks in all three files.
The frozen 0.1 library hash matches the historical comparison. This checks whole
version bundles; it does not isolate the cost of one source change.

## Source layout and preview packages

The inherited tree now lives under `engine/sqlite/`, with its internal layout
and native source bytes preserved. A fresh Windows build generated identical
amalgamation, header and shell hashes to the previous candidate. Required core
`devtest` passed **881,414 checks with zero errors** in 7:55, across six builds,
2,580 Tcl jobs and 16 corpus jobs.

The fresh relocated default build and both Windows `/MT` download builds passed
all **56 product methods**, **10 direct workflow groups** and **360 canonical
folding cases**. Both preview archives passed their SQL walkthrough from an
isolated extraction. [dev021-layout-validation.json](results/dev021-layout-validation.json)
records configurations and hashes. Hosted preview publication additionally
requires Windows/Linux product jobs and Linux core devtest on its tagged commit.

That [tagged run](https://github.com/ndmikebailey/sqlnew/actions/runs/38666524200)
has passed: both platforms completed 56 product methods and 360 folding cases
per configuration, all four archives passed their extracted walkthroughs, and
Linux core devtest passed **979,196 checks with zero errors** in 7:05. The
[preview](https://github.com/ndmikebailey/sqlnew/releases/tag/v0.2.1-dev.1) is
published. [dev021-preview-hosted.json](results/dev021-preview-hosted.json)
retains jobs, runtime hashes, archive identities and download verification.

Historical source paths and measurements below describe their original runs.
No native codec, engine behavior or database format changed in this relocation.

## Public-document retrieval

The public-document comparison passed all nine final file-backed arms: three
storage methods at three passage sizes. Seven originals reconstruct their
UTF-8 hashes exactly, FTS candidates match pinned pristine SQLite, edited values
and FTS updates commit together, region bases stay unchanged under edits,
and compaction, full restoration, integrity and reopen checks pass.
The [benchmark notes](BENCHMARKS.md#public-documents-and-passage-retrieval) link
raw measurements and two-pass variation. The [corpus archive](corpora/public-documents-archive.json)
also passed an independent restore into a fresh cache, with no downloads or
engine-source edits. This is a bounded lexical retrieval workload.

## Current 0.2.1 maintenance preparation — October 14

The inherited core now matches the pinned `branch-3.54` snapshot
`23500fbf19d6d412f3326614ce3bac2fbab7564f`, with only the existing 231-line
SQLNEW addition to `engine/sqlite/src/select.c` (originally `src/select.c`). The upstream development version is 3.54.1;
this is an unreleased maintenance snapshot. Optional upstream extension sources
are retained without registering additional modules in the default SQLNEW build.

[dev021-maintenance-validation.json](results/dev021-maintenance-validation.json)
records the current evidence and hashes:

- All **56 product test methods passed** on the Windows optimized build, a
  clean-directory build, and the scalar build. The two new statement-rollback
  regressions reproduce partial writes on the saved previous library and pass
  on the integrated core. A failed REPLACE statement now restores its changes.
- Direct region/column workflows and **360 canonical folding cases passed**
  with optimized and scalar native libraries. The active check no longer
  imports archived prototypes or needs NumPy.
- Required Windows **`nmake devtest` passed 881,423 checks with zero errors**
  in 10:46: six builds, 2,580 Tcl jobs and 16 corpus jobs. This is SQLite's core
  matrix without SQLNEW registration; product checks exercise enabled hooks.
- Optimized and scalar loadable extensions passed fold/region smoke checks;
  the scalar build flag now also applies to the loadable extension. The isolated
  Windows CPython bridge reports SQLNEW 0.2.1-dev over SQLite 3.54.1.
- Fresh and reused output directories generated identical amalgamation, header
  and shell hashes. Build manifests now retain those generated-input hashes.

The [current benchmark](results/dev021-maintenance-benchmark.json) uses the
self-contained fixture protocol in [BENCHMARKS.md](BENCHMARKS.md), with an
optional previous-library arm to isolate the maintenance intake. Its constructed
source passages differ from the historical doc2sql fixture. Per-arm core and
library identities accompany exact answer checks and timings.

The [first hosted CI run](https://github.com/ndmikebailey/sqlnew/actions/runs/38656971916)
has since completed successfully on the initial public commit
`502945b45794f43b636807ebe94bf973df357b65`. Both Windows x64 and Linux x64 passed
all **56 product methods**, **10 direct workflow groups** and **360 canonical
folding cases** in each of their optimized and scalar configurations. Runtime
reports identify AVX2 and scalar backends respectively. Linux core `devtest`
passed **979,196 checks with zero errors** in 24:31, without SQLNEW registration.

[dev021-hosted-ci.json](results/dev021-hosted-ci.json) records the run, jobs,
commit, counts and runtime library hashes. It is separate from the original local
report, whose pending-work list describes the state before publication. This CI
run establishes build/correctness coverage; the Windows benchmark timings above
were not rerun on Linux. ARM/macOS execution, broader crash scheduling,
multiwriter throughput and cold-storage behavior remain outside these checks.
No additional sanitizer or security-audit campaign was run during preparation.

## Pre-maintenance Dev 0.2.1 native integration

This earlier integration pass retained the folding codec and added an optional native `sqlnew_region` module
compile into the CLI, core library and loadable extension. Windows extension
compilation passes `/W4 /WX`. The imported core sources were not changed in this
pass; the existing `src/select.c` compiler optimizations remain in place.

[dev021-contracts.json](results/dev021-contracts.json) records direct SQL workflows:
ragged and sparse extents, nullable int64, immutable bases under edits, exact
restoration, nested savepoints, rollback of compaction, concurrent WAL snapshots,
all axes and fractional predicates, aggregates, coordinate conflict behavior,
relocation, REAL bit recovery, large TEXT/BLOB values, rename/recreation, attached
joins, checkpoint/reopen, both column folding modes and 360 canonical fold cases.

[dev021-density.json](results/dev021-density.json) adds 1%, 50% and 100% edit
density over 32,768 cells, all-axis planes and independent integer sums, 100
repeated edits with one current change entry, UTF-16 comparison parity, and
the retained vector/compressed-text index API. Base payload bytes remain unchanged
until explicit compaction. Pinned readers keep their previous values during it.

[dev021-doc2sql.json](results/dev021-doc2sql.json) records native ingestion of
the current doc2sql README through the isolated CPython bridge. The library has
the SQLNEW format-2 signature; source text, native region copy and original FTS
results remain exact. This validates single-process native ingestion, not the
consumer's isolated worker bootstrap or an automatic replacement of its schema.

The four-arm file-backed comparison in
[dev021-benchmark.json](results/dev021-benchmark.json) checks every restored
input, each timed query answer, changes, unchanged bases, checkpoint and reopen.
It compares the saved 0.2 column engine, 0.2.1 folded columns, 0.2.1 regions and
revision-matched indexed SQLite. Results include full files and setup costs.
The passage fixture uses actual source text with constructed overlapping windows;
the translated-motif fixture is deliberately favorable synthetic structure.
Prepared calls include result copying, with two warmups and 13 retained samples.
Reported p95 is the lower empirical quantile. These are warm single-host results.

This pass used direct workflow scripts and benchmarks, without pytest, sanitizer
or security suites. Full crash/failure scheduling, multiwriter throughput, cold
storage behavior and new cross-platform validation remain for a later stress pass.
Shared region value predicates and aggregates use the general SQL path; existing
column summaries remain much faster for whole-table analytics.

```console
python sqlnew/build.py
python sqlnew/exercise_dev021.py
python sqlnew/benchmark_dev021.py
```

## Focused review fixes, September 6

Small synthetic examples reproduced all three independent source-review findings:
incorrect UTF-16 text results, orphan index storage after duplicate commands,
and dictionary/snapshot memory amplification. Each correction passed its focused
regressions before broader checks. That build passed **54 product test
methods**, including nine new review regressions. No downloaded datasets or
previous benchmark results were used for this validation.

UTF-16 text comparisons now retain the engine's collation path, including after
reopening a file and with pending writes. Duplicate index commands under IGNORE,
FAIL, ABORT, and REPLACE preserve schema consistency. Dictionary rows and pending
snapshots share immutable payloads, with a configurable connection payload
budget and checked allocation failure. Flush encoding also reuses its already
computed statistics instead of scanning every column twice.

With **256 rows containing one repeated 16 KiB blob**, additional peak SQLite
heap usage for a read fell from **4,230,640 to 50,704 bytes**. The pending-write
snapshot sample fell from **4,246,712 to 50,360 bytes**. These are extra SQLite
heap allocations during the query, not total process memory.

A separate **2,048-row** fixture checked summaries, grouping, indexed ranges,
point reads, eight updates plus a FULL-synchronous WAL commit, integrity, and
unchanged stored column bytes. Three runs showed improvements in grouping and
point reads; summary and commit differences changed direction between runs.
These tiny timings do not establish general throughput or a persistent commit
regression. A prepared-summary-statement experiment did
not improve the samples and was removed. Aggregate fusion and statistical
planner estimates remain unimplemented optimization candidates.

The final AddressSanitizer run covered **6,000 allocation-failure positions**,
**3,159 injected faults**, and **20,000 malformed frames**, finishing with
**zero live SQLite allocations**. SQLNEW modules and the harness were instrumented;
the linked core object was the regular build. This rerun used the retained
implementation after removing the prepared-statement experiment.

Fresh results and hashes are recorded in
[core-review-validation.json](results/core-review-validation.json). The tests ran
on Windows x64 with the local compiler; they do not establish Linux or ARM
behavior. `src/` was unchanged in this pass. The earlier results below describe
the implementation before these fixes and are retained as historical records.

```console
python sqlnew/build.py
python -m unittest discover -s sqlnew -p test_core_review.py -v
python -m unittest discover -s sqlnew -p 'test_*.py' -v
```

## Earlier development pass

The earlier pass reran correctness and development checks on Windows. Historical
0.1 Linux results are preserved in [validation-0.1.json](results/validation-0.1.json);
they do not validate the new batch, patch, or covering-index code on Linux.
This is development evidence, not release certification.

## Completed correctness checks

- **45 product test methods passed** with the normal AVX2 build and again with
  SIMD disabled. Tests include all 32 previous methods and 13 new methods.
- **6,000 allocation-failure positions**, **3,767 injected faults**, and
  **20,000 malformed inputs** passed under Windows AddressSanitizer. Shutdown
  reported **zero live SQLite allocations**. Only reached positions inject faults.
- The native Windows core `nmake devtest` matrix passed **880,666 test cases,
  zero errors**: 6 builds, 2,486 Tcl jobs, and 16 fuzzing jobs, in 8:02.

New contracts cover unchanged payload bytes after sparse updates, exact patch
restoration, automatic and explicit compaction, native grouping and overflow,
NULL/affinity behavior, bounded-state fallback, mixed index directions, all
mutation types, old prepared plans after index removal, index-root reuse,
savepoints, WAL snapshots, and recovery after abrupt process exit. Corrupted
index payloads fail integrity checking. Upper-bound indexed aggregate tests
explicitly exclude NULL keys and exercise all five comparison operators. Covered
scalar aggregates preserve types, NULLs, and overflow errors; empty middle groups
skip aggregate payload decoding.

Fault injection found and fixed a nested rollback use-after-free and a lost
allocation-error status in filter construction. The successful sanitizer run
includes both fixes and the nullable-range correction. Final checks were repeated
after range-path tuning; exact source and library hashes are recorded in JSON.

Windows AddressSanitizer instruments the SQLNEW modules and harness; the linked
SQLite core object is the regular build. The core development matrix uses its
own configurations without SQLNEW registration. Product tests exercise the
enabled compiler path. Forced scalar testing demonstrates operation without
AVX2 on this Windows toolchain; it is not a substitute for a new Linux/ARM run.
No Docker was used in this pass. A local Tcl 8.6.18 toolchain was built from the
official source inside the ignored workspace for the core tests.

## Benchmark and exercise rerun

The final five-arm comparison passed **525 timed samples**, full restored input
hashes, integrity checks, and reopening for all ten databases. The initial
implementation passed 1,050 samples; the final matrix was rerun after fixing
the measured range regression. Both reports are retained. The million-row event,
655,360-value integer, and 10,000-vector exercises also passed; vector recall@10
remained 1.0 on the synthetic fixture. Timings and storage changes are reported
in [BENCHMARKS.md](BENCHMARKS.md), including slower point reads and summaries.

## Reproduce

```console
python sqlnew/build.py
python -m unittest discover -s sqlnew -p 'test_*.py' -v
python sqlnew/check_native.py
python sqlnew/benchmark_v2.py
python sqlnew/exercise_columnar.py --database build/new-events.sqlnew --output build/new-events.json
python sqlnew/exercise_sql.py --database build/new-columns.sqlnew --output build/new-columns.json
python sqlnew/exercise.py --database build/new-vectors.sqlnew --output build/new-vectors.json
```

Each exercise requires a fresh filename. The five-arm benchmark also needs the
pinned fixtures and pristine baseline prepared by `benchmark_build.py` and
`benchmark_data.py`, plus the frozen 0.1 library at
`build/comparison/before-v2/build/sqlnew/sqlnewdb.dll`. The snapshot's sources and
library are retained in this workspace; old source hashes are in the historical
comparison report. Timed workload protocols and results are in
[BENCHMARKS.md](BENCHMARKS.md). `SQLNEW_CHECK_TIMEOUT` overrides the native
harness's 300-second default.

The native malformed-input loop targets byte, integer, and vector frames;
selected columnar corruption is tested separately. Comprehensive VFS failures,
power-loss injection, exhaustive concurrency schedules, the complete release
suite, and cross-platform validation of 0.2 remain outside this pass.
