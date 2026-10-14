Historical 0.1 validation document. Its results apply to the earlier source hashes,
not the current 0.2 implementation.

# Validation of the experimental engine

Checks use mathematical or SQL contracts. The initial exercises measured this
engine alone; [controlled public-workload comparisons](../../../sqlnew/BENCHMARKS.md) now also
use a pristine revision-matched SQLite build and an indexed configuration.
Results are development evidence, not a release certification or proof of
optimal performance.

## Reproduce locally

```console
python sqlnew/build.py
python sqlnew/test_columnar.py
python sqlnew/test_sqlnew.py
python sqlnew/check_native.py
python sqlnew/exercise_columnar.py --database build/events-new.sqlnew
python sqlnew/exercise_sql.py --database build/columns-new.sqlnew
python sqlnew/exercise.py --database build/vectors-new.sqlnew
```

Each exercise requires a fresh file. Correctness tests use temporary files.
The first build uses the installed C toolchain; no database server is needed.
NumPy is required for vector tests and training. Column tables and integer SQL
work through the standalone native CLI and C library without NumPy.

For an optional Linux portability check from a Docker-enabled host:

```console
docker run --rm --mount "type=bind,source=ABSOLUTE_REPO_PATH,target=/work,readonly" -e SQLNEW_BUILD_DIR=/tmp/sqlnew-build -w /work debian:bookworm-slim sh sqlnew/check_linux.sh
```

This downloads a disposable compiler environment and development dependencies;
it does not run a database server. The source mount is read-only. The script
builds and tests the native engine, compiles the complete core and extensions
under address/undefined-behavior sanitizers, then rebuilds with SIMD disabled
and repeats correctness checks. The local Windows build is independent of it.

## Coverage

The 32 Python test methods include repeated and randomized cases for:

- Writable typed columns, automatic affine/constant/dictionary encodings,
  NULLs, embedded NUL bytes, and full-range signed integer round trips.
- Lazy column decoding, range pruning, comparison affinity, quoted names,
  attached schemas, joins, grouping, and aggregate fallback paths.
- Summary aggregates without payload reads, intermediate integer overflow
  within and across groups, and cancellation whose final result fits.
- INSERT/UPDATE/DELETE, moved rowids, conflict policies, last inserted rowid,
  changed-row counts, nested savepoints, failed-statement rollback, and
  transactional DROP rollback.
- Columnar WAL snapshots, pending-write visibility, reopen, and corruption
  detection through reads and module integrity checking.
- Abrupt process exit during a multi-group uncommitted write, followed by
  recovery of the last committed state in rollback-journal and WAL modes.

- Lossless bytes, output size bounds, corrupt/truncated frames.
- Nullable integer blocks, all 64 widths, signed endpoints, ordinal order,
  SQL affinity and range predicates, constants, and malformed inputs.
- Float32 and quantized scoring against mathematical reference calculations;
  signed-ID ties, hidden columns, filters before top-k, and argument limits.
- Exact geometric results against exhaustive originals, including edits,
  eligible-ID restrictions, near ties, and extreme exponents.
- Rebuild and reopen, rollback, failed upsert atomicity, WAL snapshots,
  delta visibility, and text-index maintenance.

The native harness tries 1,800 allocation-failure positions; only positions
actually reached inject a fault. It also supplies 20,000 malformed inputs to
the byte, integer-block, and vector-block interfaces and checks zero live
SQLite allocations at shutdown. Allocation injection exercises columnar
inserts, updates, deletes, summaries, reads, and savepoints as well. Selected
CSC1 corruption cases are covered by the Python tests; the native random
malformed-input loop does not exhaust the columnar decoder.
This does not exhaust parser states, concurrency interleavings, or VFS failures.

On Windows, AddressSanitizer instruments the new extension modules and harness;
the linked SQLite core object is the regular build. The Linux sanitizer build
instruments the complete core, extensions, and harness, including the new SQL
compiler path. The change in `src/select.c` is enabled by
`SQLITE_ENABLE_SQLNEW`; feature tests use that product configuration.

The core `make devtest` matrix is also run from the modified source tree in a
separate Linux build directory. It uses its own build configurations without
SQLNEW registration, so it checks the core configurations; the feature tests
above exercise the enabled columnar optimization. Run file-permission tests
as an ordinary user: root bypasses permissions those tests need to enforce.
The complete release suite and comprehensive power-loss injection remain
outside the recorded coverage.

The completed core matrix reports **0 errors out of 977,772 test cases**:
10 build jobs, 2,486 Tcl test jobs, and 54 fuzzing jobs. The sanitizer runtime
correction and replay described below completed all pending jobs successfully.

The Linux sanitizer harness and core ASan fuzzing executable are linked without
PIE. On this Docker/WSL host an
empty C program reproduced the default sanitizer runtime's fatal-signal loop
in 6 of 20 launches; a fixed-address sanitizer executable passed all 20. This
setting affects only the diagnostic executables. Address and undefined-behavior
instrumentation and default signal diagnostics remain enabled. Harness execution
has a 60-second timeout, configurable with `SQLNEW_CHECK_TIMEOUT`.

The core matrix's default PIE sanitizer executable also entered that fatal-signal
loop. Its affected and pending fuzzing jobs were rerun with
`CFLAGS.fuzzcheck-asan.fsanitize=-fsanitize=address -fno-pie -no-pie` passed to
make. This preserves address instrumentation and default signal diagnostics.

## Recorded resource exercises

[columnar.json](../../../sqlnew/results/columnar.json) records SQL ingestion of 1,000,000
five-column events, with varying readings and unique messages. The insertion
includes encoding and commit under WAL with FULL synchronization. The complete
checkpointed file is 28,262,400 bytes for 79,777,786 logical value bytes.
Logical bytes count raw typed values, not another database's file size.

Eleven warm samples per query gave a 1.70 ms median for COUNT, SUM, MIN, and MAX
together, without data-column decoding; 0.50 ms for a 100-row time slice,
reading one of 977 groups; and 0.17 ms for a rowid lookup with three columns.
The time slice read 88 compressed payload bytes and additional metadata.
Counters measure payload bytes, not total filesystem or pager I/O. Independent
integer calculations and expected rows checked the answers; integrity and
reopen checks passed. These are single-connection local measurements, not
cold-cache or multi-user throughput guarantees.

The following smaller experiments exercise the earlier explicit block APIs;
their frames are distinct from the automatic column-table encoding.

[columns.json](../../../sqlnew/results/columns.json) contains 131,072 rows for each distribution,
in 1,024-row blocks. Frame sizes include headers and NULL maps:

| Distribution | Frame bits per row |
|---|---:|
| Constant integer | 0.3125 |
| All NULL | 1.3125 |
| 16-value range with NULLs | 5.3125 |
| Sequential integer | 10.3125 |
| Uniform full-range int64 | 64.3125 |

All checked counts, extrema, supported sums, and range results matched Python
integer calculations; database integrity returned `ok`. The frame sizes exclude
table keys, B-tree pages, and free space; the complete exercise file is 1,880,064
bytes. Full-range random values illustrate why lossless encoding cannot shrink
every input. Whole-block validation remains part of scan cost.

[latest.json](../../../sqlnew/results/latest.json) records the separate 10,000-row, 128-dimensional
synthetic vector/text exercise. Its recall is measured against exhaustive
mathematical neighbors, not human semantic judgments. Original vectors remain
stored alongside quantized codes. Latencies include Python orchestration and
reflect one warm local run; background compilation was active on the same host.

The latest platform check outcomes and checked source hashes are recorded in
[validation.json](../../../sqlnew/results/validation.json). Windows and Linux x86-64 are the
tested platforms. ARM, 32-bit targets, macOS, and other floating-point
environments still require validation.
