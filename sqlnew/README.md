# SQLNEW

An experimental embedded SQL engine with a pinned SQLite `branch-3.54`
maintenance snapshot. [upstream.json](upstream.json) identifies the exact Git and
Fossil sources and original import. Its upstream 3.54.1 version is unreleased.

Dev **0.2.1** adds an optional native region store and exact numeric folding.
The column store remains available for workloads that benefit from its summaries,
batch aggregates and compression across long columns. Both use ordinary SQL and
the same transactional pager.

Ordinary indexed rows remain available alongside both native storage modules.
The [first architecture plans](../experiments/archive/early-sqlnew/) are historical
notes; they are not requirements or a roadmap.

## Build and run

### Download a preview

[GitHub Releases](https://github.com/ndmikebailey/sqlnew/releases) provides
experimental Windows x64 ZIP and Linux x64 tar.gz archives, in optimized and
scalar configurations. Extract the archive and run the CLI from that directory:

```powershell
.\sqlnew.exe :memory: ".read examples/quickstart.sql"
```

Linux uses `./sqlnew` instead. The CLI needs no Python, compiler or server.
Windows archives include the C runtime; Linux builds target the hosted Ubuntu
24.04 glibc baseline. These are portable development previews, with no installer,
automatic upgrade or format-migration promise. macOS/ARM downloads are not built.

`lib/` contains the embedded library and optional loadable module; `include/`
contains the public C API header. The complete product uses the embedded library,
including SQLNEW compiler hooks. `BUILD.json`, `UPSTREAM.json` and `MANIFEST.json`
retain configuration, source provenance and hashes; release `SHA256SUMS` covers
the archive files. Use the README shipped inside a particular download.

### Build from source

Use Python 3.12+ and an installed C compiler. Windows discovers Visual Studio C++
Build Tools. Linux uses cc and make. Install `requirements-dev.txt` for the full
Python tests, vector APIs and benchmarks. Direct region/folding checks need only
the Python standard library and the built native engine.
The native column store has no Python, NumPy, server, or Docker runtime dependency.

The [quickstart SQL](examples/quickstart.sql) is a small, in-memory walkthrough.
After building, run it from the repository root:

```powershell
.\build\sqlnew\sqlnew.exe :memory: ".read sqlnew/examples/quickstart.sql"
```

On Linux, use `./build/sqlnew/sqlnew` in place of the Windows executable.

```console
python sqlnew/build.py
python -m pip install -r requirements-dev.txt
python -m unittest discover -s sqlnew -p 'test_*.py' -v
python sqlnew/exercise_columnar.py --database build/my-events.sqlnew
```

The 0.2.1 direct workflows and version comparison use:

```console
python sqlnew/exercise_dev021.py
python sqlnew/benchmark_build.py --fetch-upstream
python sqlnew/benchmark_dev021.py
```

Direct workflows save their report under `build/dev021/contracts.json`; use
`--output PATH` to retain an explicit report without changing historical evidence.

`SQLNEW_BUILD_DIR` selects an alternate output root for the builder, Python
driver, isolated Windows CPython bridge and direct workflows. Set it to the
same path in each process; the default is this checkout's `build/` directory.
On Linux, `sh sqlnew/check_linux.sh` builds and checks both optimized and scalar
configurations. Install the compiler, make and `requirements-dev.txt` first;
the helper does not install system packages.

The inherited source and its build tools live together under `engine/sqlite/`.
Generation uses that explicit source root; generated output stays in `build/`.

To prepare a download, first commit its source, build it, then run
`python sqlnew/package.py`. On Windows set `SQLNEW_STATIC_CRT=1` for the build;
this selects `/MT` and avoids a separate Visual C++ runtime installation.
`SQLNEW_DISABLE_SIMD=1` selects the scalar archive. Packages default to
`build/dist/` and verify the walkthrough from an isolated extraction. Use
`--allow-dirty` only for a local packaging rehearsal. CI attaches both
configurations as artifacts; a `v*` tag publishes an experimental prerelease
only after the product and core jobs pass.

### Document retrieval comparison

```console
python sqlnew/benchmark_build.py --fetch-upstream
python sqlnew/benchmark_documents.py --fetch-corpus
```

The [corpus manifest](corpora/public-documents.json) pins four complete public
books and three technical documents by SHA-256. Remote originals stay in ignored
`build/document-corpus/`. Downloads require the explicit flag, and changed
upstream files are rejected. Non-overlapping passages preserve every original
byte. All arms use a common contentless FTS5 candidate index and include its cost.
See [benchmark methodology](BENCHMARKS.md) for scope and current measurements.

For durable reproduction, download the versioned corpus ZIP identified by
[public-documents-archive.json](corpora/public-documents-archive.json), then run
`python sqlnew/corpus_archive.py --extract PATH/TO/public-documents-v1.zip`.
This restores all seven originals into the ignored cache without network access
or changes to engine source files. The benchmark verifies their pins again.
`--create PATH` on the same helper preserves already-prepared originals.
With the tagged preview sources, you can instead extract the corpus ZIP directly
into `build/document-corpus/`, then run `python sqlnew/benchmark_documents.py`.

The benchmark builds its fixtures from deterministic numbers and source text in
this repository. It compares current columns, folded columns, regions and the
revision-matched indexed SQLite library prepared by `benchmark_build.py`.
`--fetch-upstream` permits fetching the exact public baseline commit if a fresh
clone lacks that Git object; the native SQLNEW build itself makes no downloads.
Optionally pass `--previous-library PATH` to compare a retained older SQLNEW DLL
or shared library as a fifth arm. No private checkout or old binary is required.

Results default to `build/dev021/benchmark.json`; `--output PATH` saves an
explicit report. They include complete file sizes, ingestion/sealing costs,
exact SQL results and read/update latencies. The original
[pre-maintenance run](results/dev021-benchmark.json) remains historical evidence.
The [attempt log](../experiments/exact_fold/ATTEMPTS.txt) records implementation
choices, unsuccessful attempts and measured limitations.

## Shared regions and separate changes

```sql
SELECT sqlnew_version(),sqlnew_capabilities();
CREATE VIRTUAL TABLE readings USING sqlnew_region(64,64,64,INTEGER);
INSERT INTO readings(x,y,z,value) VALUES(3,4,5,100);
INSERT INTO readings(__sqlnew_command) VALUES('compact');
UPDATE readings SET value=101 WHERE x=3 AND y=4 AND z=5;
SELECT value FROM readings WHERE x=3 AND y=4 AND z=5;
SELECT x,y,value FROM readings WHERE z=5;
SELECT sqlnew_region_stats('readings');
```

Creation arguments are X/Y/Z dimensions and one nullable value type: INTEGER,
REAL, TEXT or BLOB. Coordinates are nonnegative integers inside that fixed extent.
Only occupied cells are stored; an occupied NULL differs from a missing cell.
The implicit rowid is `x + nx*(y + ny*z)`, starting at zero. Coordinates are unique.
Normal INSERT/UPDATE/DELETE, IGNORE/REPLACE, joins, aggregation, savepoints,
WAL reader snapshots, rename and attached files use the existing SQL machinery.

Each 8x8x8 region refers to an immutable encoded base. Numeric bases normalize
an exact 64-bit anchor and choose reversible axis differences, native folding
and byte compression. Identical complete encoded bases share one stored payload;
exact byte equality resolves hash collisions. Edits remain in a coordinate-local
change B-tree. Point reads seek one changed slot; ranges select intersecting
changed slots and apply them in a bounded native batch. A changed point can skip
base decoding. Each connected table retains at most one decoded base cache;
cursors copy the leaf they use, preserving independent reader state.

Text/blob values have separately compressed, content-shared payloads. Fetching
one large passage decodes that value, avoiding expansion of other cell bodies.
The native byte codec's approximately 64 MiB frame limit still applies per value.
This trades compression across neighboring values for selective retrieval.

`compact` seals current cells into replacement shared bases, removes their
changes, and collects unreferenced bases and value payloads in the same transaction.
Existing WAL snapshots retain their previous view. Initial ingestion is staged
as changes; include sealing when measuring setup and storage. There is no automatic
dense rewrite into a shared base. Restoring a base value removes its change entry.

The region store currently has no secondary value indexes or metadata aggregate
fast path. General SQL evaluates its aggregates and value predicates. The current
column store provides those optimizations. Region scans visit spatial block
metadata and emit cells; they do not promise equally fast traversal in every axis.
Dimensions are fixed; explicit historical generations and cross-file publication
are not part of this API. The `.srg`/`.sov` prototype packets are separate experiments
and are not the native database format.

## Exact numeric folding

```sql
SELECT sqlnew_columnar_folding(1);
-- Future eligible numeric column writes can use the SFL1 representation.
SELECT sqlnew_unfold(sqlnew_fold(?));
SELECT sqlnew_fold_merge(?,?);
```

Folding preserves opaque little-endian 64-bit words exactly. Adjacent streams
with compatible absolute origins merge canonically. Automatic column folding is
off by default; readers can decode folded data regardless of the current setting.
The selection keeps the older encoding when it would be smaller. Canonicality
concerns the encoded word stream, not SQL floating aggregate order or whole files.

The isolated Windows CPython bridge now checks the configured product version.
`SQLNEW_DOC2SQL_CHECKOUT` selects an explicit doc2sql checkout for
`run_doc2sql.py`; its default remains the paused isolated `build/doc2sql` checkout.
Batch/worker subprocess bootstrap is still outside that bridge's scope.

Outputs are `build/sqlnew/sqlnew.exe` and `sqlnewdb.dll` on Windows; Linux builds
`sqlnew` and `sqlnewdb.so`. Each exercise needs a fresh database filename.

## Ordinary SQL over compressed columns

```sql
CREATE VIRTUAL TABLE events USING sqlnew_columnar(
  seq INTEGER, ts INTEGER, sensor INTEGER, reading REAL, message TEXT
);

INSERT INTO events VALUES
  (1,1700000001000,7,12.5,'device ready'),
  (2,1700000002000,7,13.0,'device ready'),
  (3,1700000003000,8,NULL,'measurement unavailable');

SELECT count(*),sum(seq),min(ts),max(ts) FROM events;
SELECT seq,message FROM events
  WHERE ts BETWEEN 1700000002000 AND 1700000003000 ORDER BY seq;
UPDATE events SET reading=14.0 WHERE rowid=2;
DELETE FROM events WHERE rowid=3;
PRAGMA integrity_check;
```

The engine chooses encodings and maintains visibility, summaries, and range
indexes as rows change. Callers do not pack blobs or rebuild the store manually.
Tables support nullable INTEGER, REAL, TEXT, and BLOB columns, implicit signed
64-bit rowids, joins, grouping, sorting, transactions, and savepoints.

Integer columns use constant, affine-sequence, or range-based bit packing.
Text and blobs choose raw or dictionary representations. All-NULL columns
carry no bitmap or value payload. Encoded groups receive lossless compression
and checksums. Readers allocate and decode only requested columns.

Decoded dictionary values and pending-write snapshots share immutable text/blob
payloads. A connection tracks unique retained payload allocations across its
columnar tables, predicates, and aggregates. The default limit is 64 MiB;
`SELECT sqlnew_columnar_memory_limit()` reads it and
`SELECT sqlnew_columnar_memory_limit(134217728)` sets 128 MiB, returning the old
limit. Exceeding the limit returns `SQLITE_NOMEM`. Lowering it does not evict
live values. This budget covers payloads and their ownership headers; cell
arrays, temporary codec buffers, the pager cache, and the SQL sorter are separate.

Group metadata is stored separately from payloads. Numeric and binary-collation
range predicates can skip groups before their payloads are read. The SQL
compiler automatically uses summaries for whole-table COUNT, integer SUM, MIN,
and MAX where eligible. It preserves integer overflow detection using 128-bit
summary arithmetic. Eligible GROUP BY and filtered COUNT/SUM/AVG/MIN/MAX execute
in native column batches, with compensated floating summation. An 8 MiB group
state budget and a 16,384-group ceiling retain the general SQL sorter as fallback.
The sorter's own memory/disk behavior follows `temp_store`. Unsupported
expressions, collations, DISTINCT, HAVING, and window aggregates retain SQL paths.
UTF-16 databases also use the engine's encoding-aware path for text filtering,
grouping, extrema, and ordering. Canonical UTF-8 text keys and summaries are
not used to decide UTF-16 binary order; numeric fast paths remain available.

```sql
EXPLAIN QUERY PLAN SELECT count(*),sum(seq) FROM events;
-- COLUMNAR SUMMARY events
SELECT sqlnew_columnar_stats('events');
```

The counters expose groups read, columns decoded, payload bytes, rows emitted,
summary groups, columns rewritten, patches read/written, compactions, native
batch rows/fallbacks, and covering-index entries read/written.
The `connection_payload_bytes`, `connection_payload_peak`, and
`connection_payload_limit` fields describe shared payload memory for the whole
connection, even when requested through one table's statistics.

## Updates and optional covering indexes

Ordinary same-rowid UPDATE now stores patches for changed columns. It avoids
recompressing the other columns and removes patches when the original value is
restored. Reads, range pruning, summaries, indexes, rollback, and WAL snapshots
include these patches. Dense patches merge automatically at 64 changed slots
per column/group; full compaction is an explicit transactional command.

```sql
INSERT INTO events(__sqlnew_command) VALUES(
  '{"create_index":"time","keys":["ts"],"include":["reading"]}'
);
SELECT count(*),sum(reading) FROM events WHERE ts>=1700000002000;
INSERT INTO events(__sqlnew_command) VALUES('compact');
INSERT INTO events(__sqlnew_command) VALUES('{"drop_index":"time"}');
```

The planner can seek and project from the covering B-tree without decoding
column groups. Covered scalar aggregates run directly over that B-tree through
the SQL VM, avoiding per-row virtual-column callbacks. Composite keys accept descending fields, for example
`"keys":["sensor",{"column":"reading","desc":true}]`. Indexes consume file
space and add writes when their fields change, so they are optional. Native
grouping needs no persistent grouping index. Index removal empties and retires
the root; an identical future definition can reuse it.

Version 0.2 uses the `SQLNEW format 2` file signature and requires fresh files.
Old experimental files and upstream SQLite files are rejected by this build.

## Recorded million-row run (0.2)

[columnar-v2.json](results/columnar-v2.json) records deterministic events with varying
readings and unique messages on this machine:

| Measurement | Result |
|---|---:|
| SQL ingestion, 1,000,000 rows | 1.66 seconds |
| COUNT, SUM, MIN, MAX together | 2.13 ms median |
| 100-row time slice | 0.63 ms median |
| Rowid lookup with three columns | 0.20 ms median |
| Complete checkpointed database | 28,274,688 bytes |
| Logical typed values | 79,777,786 bytes |

The aggregate decoded no data columns. The time slice visited one of 977 groups,
decoding 88 bytes of compressed column payload plus reading metadata.
Logical bytes are not another database's size, and these timings are warm local
measurements. That exercise measured SQLNEW alone.

## Comparisons using public workloads

[Controlled benchmark results](BENCHMARKS.md) compare this column store against
pristine SQLite from the same source revision, with matching compiler and
durability settings, and against an indexed SQLite configuration.

The first comparison exposed expensive whole-group updates and row-at-a-time
grouping. The 0.2 comparison includes the frozen previous library, the new engine
with and without optional indexes, and both SQLite configurations. It records
all prior workloads plus indexed-column mutations and complete update/read/
compaction/checkpoint cycles. See [BENCHMARKS.md](BENCHMARKS.md) for measured
tradeoffs and [VALIDATION.md](VALIDATION.md) for the scope of testing.

## Current practical limits

Columnar tables use the explicit creation form above; ordinary CREATE TABLE
retains the row store. They currently accept 1..63 typed columns without inline
SQL constraints. Their rowid is unique; optional binary-collation covering
indexes use the command interface above. Unique/expression/partial secondary
indexes and declared foreign keys are not implemented for this storage method. Values must match
their declared type, with integer-to-real conversion allowed.

INSERT, DELETE, rowid changes, and dense updates can still rewrite groups. Wide column groups
remain subject to the current codec size limit. Broad overlapping value ranges
may prune nothing. The pager and journal remain SQLite-derived; this has not
received production-scale crash and concurrency validation.

These describe the current code, not restrictions on future implementation.
See [formats](FORMAT.md) and [validation](VALIDATION.md) for reproducible details.

Native byte compression, packed integer SQL functions, vector blocks, AVX2/scalar
distance kernels, and the transactional vector index remain available.
[index.py](index.py) contains that API; [GEOMETRY.md](GEOMETRY.md) states the
arithmetic assumptions behind its optional geometric pruning.
