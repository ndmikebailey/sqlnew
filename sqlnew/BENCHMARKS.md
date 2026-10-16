# SQLNEW benchmarks

## Common application tasks against released SQLite

The October 15 comparison uses **100,000 commerce-style events, 1,000 customers,
15 tasks and six configurations**. Every answer, mutation, full restoration and
reopened file passed exact checks: **90 combinations / 1,170 retained samples**.
The fixture uses integer cents, nullable quantities, timestamps, status and unique
short memos. It is synthetic, without duplicate body padding. The large integer
SUM exceeds 32 bits. An independent Python oracle checks typed answers.

The primary baseline is unmodified, released
[SQLite 3.54.0](https://sqlite.org/releaselog/3_54_0.html), built from the
[official amalgamation](https://sqlite.org/2026/sqlite-amalgamation-3540000.zip).
The [release pin](sqlite-release.json) records archive/source hashes and runtime
source ID. All libraries use the same MSVC `/O2 /MD` compiler settings and common
features. These are source builds, not claims about vendor binary defaults.
An additional pristine **unreleased 3.54.1** baseline matches SQLNEW's inherited
revision, separating version differences from fork changes.

The indexed row and column configurations have the same two five-key covering
indexes: `(customer,created,amount,qty,status)` and
`(created,customer,amount,qty,status)`. Unindexed arms keep only row identity.
The following are median **milliseconds**; row arms and the last column include
both indexes. Points return full records; the range and recent-record requests
use covered projections.

**SQLNEW ordinary tables** means the current SQLNEW library with ordinary
`CREATE TABLE` storage. It is not a previous version or an experiment. Compressed
columns use `CREATE VIRTUAL TABLE ... USING sqlnew_columnar(...)` in that same
library. Region storage remains available but is a different table type; this
common-task matrix does not measure it. Current table creation explicitly
selects storage. SQLNEW does not automatically move a column table's individual
record lookups to the region representation.

| Task | Released SQLite (ms) | SQLNEW ordinary tables (ms) | Compressed columns (ms) | Compressed columns + indexes (ms) |
|---|---:|---:|---:|---:|
| 100 point requests | 2.263 | 2.098 | 32.018 | 32.436 |
| 100-row time range | 1.066 | 1.054 | 1.391 | 1.165 |
| Latest 20 records for one customer | 0.252 | 0.243 | 10.588 | 0.329 |
| Whole-table COUNT/SUM/MIN/MAX | 7.860 | 7.788 | 0.422 | 0.416 |
| Summary including nullable COUNT | 8.867 | 9.034 | 0.540 | 0.498 |
| GROUP BY customer | 14.789 | 14.663 | 16.809 | 16.892 |
| Join customer segments and group | 38.335 | 37.855 | 4,563.656 | 86.385 |
| Top 20 amounts, descending | 6.557 | 6.591 | 6.672 | 19.199 |
| Insert 1,000 records + commit | 18.406 | 17.535 | 7.681 | 26.387 |
| Update 100 memos + commit | 3.150 | 2.703 | 28.486 | 28.252 |
| Update 100 indexed amounts + commit | 5.610 | 4.894 | 16.623 | 31.958 |
| Delete 100 records + commit | 5.195 | 4.705 | 69.863 | 73.299 |
| Replace 100 records + commit | 6.521 | 5.999 | 65.994 | 68.642 |
| 20 individual insert/commit pairs | 12.971 | 10.420 | 17.714 | 21.676 |
| Update 100 records and roll back | 0.716 | 0.701 | 27.095 | 26.997 |

Complete checkpointed database sizes include customers, all keys and metadata.
KiB means 1,024 bytes. Initial files have been vacuumed equally; post-task files
retain allocation growth after restoring data, native compaction and checkpoint.
Setup costs are single observations in milliseconds, not sampled medians.

| Configuration | Initial KiB | Post-task KiB | Ingest + commit ms | Index + commit ms | ANALYZE ms |
|---|---:|---:|---:|---:|---:|
| Released SQLite, indexed | 11,040 | 11,452 | 55.798 | 125.948 | 19.776 |
| Released SQLite, unindexed | 5,824 | 5,884 | 66.071 | 0.001 | 0.895 |
| Matched 3.54.1, indexed | 11,040 | 11,452 | 56.183 | 106.048 | 19.893 |
| SQLNEW rows, indexed | 11,040 | 11,452 | 57.762 | 103.827 | 19.749 |
| SQLNEW columns | 1,700 | 1,720 | 83.670 | 0.001 | 0.763 |
| SQLNEW columns, indexed | 16,168 | 17,744 | 90.919 | 403.796 | 37.200 |

Ordinary SQLNEW rows track SQLite closely in this matrix. Native summaries use
maintained metadata and are about **19x faster** here. Unindexed columns use
about **3.43x less storage than unindexed SQLite**; adding these two broad native
indexes makes the file **46.4% larger than indexed SQLite**. Indexed columns
improve customer/range retrieval, while full-record points and small mutations
remain slower. Even a covered index can hurt another task: the top-20 sort is
slower in the indexed column configuration. There is no combined winner score.
The raw report also retains all unindexed SQLite and matched-version timings.

The default unindexed column join exposes a major plan problem: customers are
the outer loop, repeatedly scanning the column table. Its original 4.56-second
score remains in this matrix. A separate join-order diagnosis below checks the
effect of scanning events once; it does not substitute a hinted result here.

Each arm has its own persistent process, with only one timed at a time. Arm
order rotates each iteration: two warmups and 13 retained samples. Prepared
reads include binding and Python/ctypes result copying. Writes include BEGIN and
FULL commit; the rollback task ends with ROLLBACK. All files use 4 KiB pages,
WAL FULL, 64 MiB connection caches, mmap off, memory temporary data and one SQL
worker. Fixture preparation and validation are outside timing; bulk ingestion,
indexes and ANALYZE are separate. The fixture is supplied by a common TEMP table
before native INSERT SELECT, so this is not a CSV/parser benchmark.

Every write sample has separately timed undo and compaction/checkpoint. For
example, indexed-column insert-1,000 takes 26.387 ms for the application change,
38.743 ms for later compaction/checkpoint, and 93.703 ms for the whole benchmark
cycle including undo. Those medians need not add together. A benchmark cycle
includes fixture restoration and is not a normal application transaction.

This is one warm Windows 11 run on an Intel Core i7-10750H with Python 3.14.4;
it does not measure cold-cache, concurrent writers or crash recovery. Download
packages use `/MT`; this timing library uses `/MD`. No native engine or format
changed for this measurement. Timed source hashes are preserved in commit
`038d8d0e5` and the [raw report](results/dev021-common-tasks.json), alongside every
sample, p95, query plan, setting, input hash and library/build identity.

Reproduce from the repository root, sequentially after other builds/checks end:

```console
python sqlnew/build.py
python sqlnew/benchmark_build.py --fetch-upstream
python sqlnew/benchmark_release.py --fetch-release
python sqlnew/benchmark_common.py --rows 100000 --repetitions 13
```

The first baseline builder retrieves the exact inherited revision; the second
checks and compiles the official released archive. Both downloads are explicit.
The [common-task runner](benchmark_common.py) defaults to ignored
`build/common-tasks/benchmark.json`; `--output PATH` retains another run.

### Version check on identical data

To check whether older improvements had been discarded, the retained **0.1.0-dev**
and **0.2.0-dev** libraries were compared directly with **0.2.1-dev**. All use
unindexed compressed columns, the same 100,000 commerce records, six fields,
query text, parameters, driver and settings. Three isolated persistent processes
rotate their sequential timing order, with two warmups and 13 retained samples.
Every answer, complete input hash, integrity check and reopened input passed.
Median elapsed time for the entire named task:

| Task | SQLNEW 0.1.0 (ms) | Saved SQLNEW 0.2.0 (ms) | Current SQLNEW 0.2.1 (ms) |
|---|---:|---:|---:|
| 100 full-record lookups | 33.465 | 32.855 | 32.311 |
| 100-row time range | 1.105 | 1.128 | 1.119 |
| Whole-table summary | 0.290 | 0.366 | 0.367 |
| Group by customer | 49.846 | 16.225 | 16.010 |

This full-record lookup cost was already present in 0.1. All three versions
decode the same 600 column groups and count 1,407,705 compressed input bytes per
100 requests. The group size remains 1,024 positions. Retrieving one record
can expand the requested columns for neighboring records, even though only
one record is returned. Those are native decoder-input counters, not disk I/O.
Grouping now uses the native batch path; summaries still avoid column decoding,
but the newer version is about 27% slower than 0.1 in this paired run. Retaining
the feature does not mean all of its read costs were retained unchanged.

The old [million-row event exercise](results/columnar.json) reported **0.170 ms
for one lookup of three fields**. The new common task returns **100 complete
six-field records** at different IDs; comparing 0.170 directly with 32.311 would
mix operations and projections. Separately, the historical matched-workload
[0.1-to-0.2 comparison](results/comparison-v2.json) did record real regressions:
taxi's 100 three-field points went from 8.386 to 11.562 ms, while grouping fell
from 1,495.449 to 174.221 ms and 100 updates from 186.299 to 14.492 ms. Those
earlier gains and losses remain valid for their fixtures and recorded builds.

Current registration still includes columns, summary/batch execution, optional
covering indexes, exact folding, regions, and vector/text helpers. The integer
codec source hash is unchanged from 0.1. Storage selection is still explicit;
retaining these paths does not combine every path's strengths automatically.
The two fixed base planner estimates are also present in the retained 0.1
source. The join experiment identifies a current bad plan, but without an
identical old join run it does not establish which release introduced it.

[Raw version audit](results/dev021-version-reads.json), [runner](benchmark_versions.py).
The 0.1 library hash matches the frozen previous arm in the historical comparison;
the saved 0.2 library is the pre-0.2.1 snapshot, including intervening fixes,
not necessarily the exact early 0.2 binary in every historical report. Their
inherited cores differ; the comparison measures version bundles, not a single
code change. The native libraries were not rebuilt or modified for this audit.

```console
python sqlnew/benchmark_versions.py --v01-library PATH --v02-library PATH
```

This optional audit requires retained old libraries; they are not shipped with
the public source. Exact library hashes are in the report. Fresh databases are
created under ignored `build/version-reads/`; no old database is migrated.

### Join-order diagnosis

A separate read-only follow-up uses the same libraries and fully restored
post-task databases. Each query has two warmups and five retained samples.
`CROSS JOIN` forces events to be the outer loop, following SQLite's
[documented join-order control](https://sqlite.org/optoverview.html#manual_control_of_query_plans_using_cross_join).
The five exact segment answers are unchanged, and full input and database-file
hashes match before/after. Median milliseconds:

| Configuration | Default JOIN | Events outer |
|---|---:|---:|
| Released SQLite, indexed | 44.368 | 61.479 |
| SQLNEW columns, unindexed | 3,657.044 | 56.763 |
| SQLNEW columns, indexed | 77.106 | 54.848 |

For unindexed columns, the default plan visits 98,000 native blocks per call;
events-outer visits 98. Filtered-out rows fall from 99,900,000 to zero. Cached
compressed decoder inputs fall from about 332 MB to 0.332 MB per call. These
counters exclude physical I/O, customer-table reads and temporary grouping.
The follow-up's **64x** improvement strongly identifies join planning as an
actionable source of wasted work. It does not establish that one forced order
is best generally: SQLite's indexed plan is faster with its default order.
Default timings also varied between the matrix and follow-up; compare the two
orders within this follow-up. No planner fix or engine change was made.

[Raw diagnostic](results/dev021-join-order.json), including both plans, samples
and counters; [runner](benchmark_join_order.py). After running the common matrix:

```console
python sqlnew/benchmark_join_order.py
```

If the matrix used `--output PATH`, pass that same path with `--common-report`.
The diagnostic deliberately requires its retained databases and matching library
hashes, rather than quietly creating a different fixture.

## Public documents and passage retrieval

### Comparison against released SQLite 3.54.0

The same seven originals were freshly compared with the official released
baseline above, using **763 non-overlapping passages of at most 4,096 characters**
and the common FTS5 candidate index. All three arms passed exact reconstruction,
search answers, updates, restoration, integrity and reopen checks. Median
milliseconds use two warmups and 13 retained samples:

| Measurement | Released SQLite | SQLNEW regions | SQLNEW columns |
|---|---:|---:|---:|
| Complete initial database, KiB | 4,864 | 4,500 | 3,428 |
| 64 point requests, ms | 0.997 | 3.281 | 176.490 |
| Seven keyword context requests, ms | 0.875 | 2.796 | 160.848 |
| First complete document, ms | 1.189 | 2.682 | 2.654 |
| 64 edits + FTS synchronization + commit, ms | 19.991 | 24.544 | 178.158 |
| Compaction / FTS optimization / checkpoint, ms | 37.682 | 39.421 | 78.343 |
| Restored file after edits/maintenance, KiB | 6,488 | 6,128 | 5,036 |

The edit and maintenance rows are single observations, not sampled medians.
Initial sizes include all metadata and FTS indexes. Later files retain free-page
and allocation growth; restoring original values does not implicitly VACUUM.
Region bases remain unchanged under edits. Regions use **7.5% less initial
space** than this indexed SQLite database, while SQLite remains faster for
selective retrieval. Columns save more space but amplify body decoding for
isolated passages. This is lexical candidate discovery plus exact body recovery,
not a relevance evaluation.

[Raw released-baseline report](results/dev021-documents-sqlite-release.json)
retains corpus identities, full samples, counters, setup and maintenance costs.
To reproduce after preparing SQLNEW, the released baseline and the corpus:

```powershell
python sqlnew/benchmark_documents.py --sqlite-library build/comparison/sqlite-release/sqlite-release.dll
```

Use `.so` on Linux or `.dylib` on macOS. The default remains the matched baseline,
so historical comparisons keep their meaning. The evidence below is preserved
from that earlier matched-core comparison, not silently replaced with this run.

### Passage-size comparison against the matched core

The [document runner](benchmark_documents.py) compares indexed pristine SQLite,
SQLNEW regions and SQLNEW columns over **seven complete originals / 2,863,312
UTF-8 bytes**: four public books and three technical documents. The
[manifest](corpora/public-documents.json) pins their source identities. Passages
do not overlap or repeat padding; joining them reconstructs each original hash.
This is a bounded English prose/technical collection, not an enterprise corpus.

All three arms include the same contentless FTS5 candidate index and a document
catalog. SQLite also has a `(doc,ordinal)` index. The keyword workflow finds
candidate rowids, then fetches their bodies through the chosen storage method;
callers do not provide coordinates. Seven fixed terms return up to 16 passages
each, ordered by rowid. This measures lexical retrieval and exact body recovery,
not semantic relevance or ranked-search quality. Pristine FTS5 supplies the
candidate oracle; bodies independently match the original text.

Complete initial database sizes, **KiB**, and median milliseconds for **64 point
requests** in the final pass:

| Maximum passage characters | Passages | Columns KiB | Regions KiB | SQLite KiB | Columns ms | Regions ms | SQLite ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| 1,024 | 3,486 | 3,468 | 4,632 | 4,796 | 138.427 | 2.640 | 0.703 |
| 4,096 | 763 | 3,428 | 4,500 | 4,864 | 171.627 | 3.193 | 1.013 |
| 16,384 | 181 | 3,588 | 3,564 | 4,252 | 183.032 | 7.379 | 6.628 |

For the default 4,096-character passages, median milliseconds:

| Workload | Columns | Regions | Indexed SQLite |
|---|---:|---:|---:|
| Seven keyword context requests, 85 returned passages | 173.416 | 2.771 | 0.884 |
| Complete first document, 607,606 bytes | 2.965 | 3.004 | 1.237 |

With default passages, regions use about 31% more space than columns and take
about 54x less time for the 64 point requests. Their native compressed-payload
counter records **237,065 bytes per batch**, versus **36,545,207 bytes** for
columns. Those are decoder-input counts including cached reads, excluding FTS
and physical I/O. They explain the work avoided; they are not disk-bandwidth
measurements. Connection memory gauges are recorded separately.

At 16,384 characters the complete region and column files differ by only
**24 KiB**, while regions take about 25x less time for points in this run.
Larger passages change row counts, FTS granularity and returned bytes: the
64-point batches return about 998 KB there, versus 243 KB at the default size.
Compare storage methods within each row; passage sizes are different retrieval
units. Indexed SQLite has the fastest points in all three final comparisons.

Edits synchronize the common FTS index in the same FULL-synchronous transaction.
At the default size, 64 updates and commit took **197.40 / 23.33 / 19.73 ms** for
columns / regions / SQLite. Native compaction where applicable, FTS optimization
and a truncating checkpoint took **75.88 / 36.02 / 41.29 ms**. These are single
setup/maintenance observations, not sampled medians. Reports retain ingestion,
sealing, indexing and larger files after edits/compaction; no implicit VACUUM
shrinks those costs away. Region base payloads stayed unchanged under edits.
Full restoration, original hashes, integrity and reopen checks passed in all
nine final arms.

Two passes used the same native binaries and original corpus hashes. Timings
varied: SQLite's 16,384-character point median ranged from **2.988 to 6.628 ms**,
and regions from **7.379 to 7.525 ms**. Complete file sizes and answers matched.
The [repeat record](results/dev021-documents-repeat.json) retains both sets of
raw timing samples. These are warm, single-host Windows observations, with
prepared calls, result copying, two warmups and 13 retained samples; p95 is the
lower empirical quantile. Compilation and core checks ended before timing.
All files use 4 KiB pages, WAL FULL, a 64 MiB cache and no mmap.
These timings use the retained MSVC `/O2 /MD` core. Windows download builds
use `/MT`; their package validation is separate from this timing run.

Raw final reports: [1,024](results/dev021-documents-1024.json),
[4,096](results/dev021-documents-4096.json),
[16,384](results/dev021-documents-16384.json). This preserves the measured
compression/retrieval blend; it does not establish a universal storage winner.

To reproduce, build SQLNEW and the pinned baseline, then either fetch the
originals or restore the [versioned corpus archive](corpora/public-documents-archive.json):

```console
python sqlnew/benchmark_build.py --fetch-upstream
python sqlnew/corpus_archive.py --extract PATH/TO/public-documents-v1.zip
python sqlnew/benchmark_documents.py
python sqlnew/benchmark_documents.py --passage-chars 1024
python sqlnew/benchmark_documents.py --passage-chars 16384
```

`--fetch-corpus` downloads originals instead; every pin must still match.
The corpus archive preserves their original bytes and notices if remote editions
change. Outputs default to ignored `build/documents/benchmark.json`; explicit
`--output` paths retain distinct runs. Corpus preparation is outside timing.

## Current 0.2.1 comparison

Run `python sqlnew/benchmark_build.py --fetch-upstream` after building SQLNEW,
then `python sqlnew/benchmark_dev021.py`. The default comparison uses current
columns, folded columns, regions and pinned indexed SQLite. Numeric fixtures
are deterministic; text passages are overlapping windows from this repository's
native sources. No private checkout or saved older DLL is required. Optional
`--previous-library PATH` adds an earlier columnar build as a fifth arm.

Reports default to ignored `build/dev021/benchmark.json`; use `--output PATH`
to retain a specific run. The text fixture manifest, complete database sizes,
setup costs, result hashes, per-arm source IDs and library hashes accompany the
timings. Run without other benchmark/test jobs competing for the host.

Each file uses 4 KiB pages, WAL FULL, a 64 MiB cache and no mmap. Fixture loading
is excluded; table ingestion, region sealing and row indexes are reported
separately. Fixture pages are reclaimed before file sizing. Warm prepared calls
include result copying: two warmups and 13 samples, with a lower empirical p95.
These are single-host sequential measurements, not cold-cache or concurrent
throughput claims. The translated motif is intentionally favorable; overlapping
source passages are not a representative document collection.

### October 14 maintenance candidate

[dev021-maintenance-benchmark.json](results/dev021-maintenance-benchmark.json)
records **20 exact, file-backed runs**: four fixtures and five arms, including
the saved immediately preceding SQLNEW column engine. Tests and builds completed
before timing. These are complete checkpointed file sizes in bytes:

| Fixture | Columns | Folded columns | Regions | Indexed SQLite |
|---|---:|---:|---:|---:|
| Translated motif | 2,457,600 | 2,457,600 | 53,248 | 13,570,048 |
| Quadratic clock | 1,712,128 | 274,432 | 237,568 | 13,053,952 |
| Independent bits | 2,457,600 | 2,457,600 | 2,420,736 | 13,570,048 |
| Source passages | 1,638,400 | 1,638,400 | 9,695,232 | 17,907,712 |

Median milliseconds for 64 prepared point requests, including result copying:

| Fixture | Previous columns | Current columns | Regions | Indexed SQLite |
|---|---:|---:|---:|---:|
| Translated motif | 4.692 | 4.406 | 1.772 | 0.670 |
| Quadratic clock | 3.437 | 3.206 | 2.364 | 0.554 |
| Independent bits | 4.693 | 4.368 | 2.685 | 0.551 |
| Source passages | 669.413 | 672.099 | 4.155 | 1.388 |

Current columns have the same measured file sizes as the pre-integration column
engine on every fixture. A single sequential run does not establish a general
maintenance speedup. Indexed rows remain fastest for these warm point requests.
Regions retain favorable repeated-shape sharing; separately stored text bodies
trade compression across passages for selective reads. Numeric regions cost
about 0.8 seconds to ingest and seal versus about 0.09 seconds for columns.

[dev021-benchmark.json](results/dev021-benchmark.json) retains the earlier run
against the pre-maintenance engine and a saved 0.2 library. It used doc2sql source
passages, so its text sizes and timings are not interchangeable with the current
default source fixture. Earlier 0.2 workload results below also describe their
recorded source revisions, not the current maintenance candidate.

## Historical 0.2: measured gains and costs

Sparse transactional updates, native grouping, exact filter pushdown, and
optional covering indexes now work together in the embedded file. The complete
comparison measures their write, read, compaction, loading, and storage costs.
SQLNEW is still not a general performance replacement for indexed SQLite.

All **525 timed samples passed** in the final run, with full typed input
hashes, independent query answers, exact update restoration, integrity checks,
and reopen checks for all ten databases. Raw samples, interquartile ranges,
query plans, work counters, settings, and source/library hashes are in
[comparison-v2.json](results/comparison-v2.json).

## Public workloads and method

The same pinned workloads from the original comparison are retained:

- [H2O.ai queries](https://github.com/h2oai/db-benchmark/blob/c7421051af7530951d16b1505158371aebc0d2c1/duckdb/groupby-duckdb.R)
  and [generator](https://github.com/h2oai/db-benchmark/blob/c7421051af7530951d16b1505158371aebc0d2c1/_data/groupby-datagen.R):
  1,000,000 unsorted rows, nine columns, K=100, PCG64 seed 108. This reproduces
  distributions rather than R's exact rows. Queries 1-5, 7, 8, and 10 are included;
  Q6 median/stddev and Q9 correlation remain explicitly unsupported in every arm.
- [dsq script](https://github.com/multiprocessio/dsq/blob/c3ae0bafb0c3283e3c98cb250ada5a19e79ad58e/scripts/benchmark.sh)
  and [taxi fixture](https://github.com/multiprocessio/dsq/blob/c3ae0bafb0c3283e3c98cb250ada5a19e79ad58e/testdata/taxi.csv.7z):
  all 2,171,187 April 2021 rows and 18 columns. Arrow supplies typed interpretation;
  timestamps become Unix seconds, NULL and empty text remain distinct, and signed
  zero is canonicalized. Grouping, time range, summary, point, and mutation cases
  use the same data and SQL as the previous comparison.

These are local workload adaptations, not official published-suite scores. No
other database implementation was studied. SQLite is the pristine revision
imported by the fork, built with the same MSVC /O2 /MD settings and common
features. The frozen 0.1 DLL is the actual pre-change build.

This run used Windows 11 on an Intel Core i7-10750H. Each of five configurations
runs in an isolated persistent worker. Timings are sequential, with one checked
warm-up and **5 balanced repetitions** per case. Every arm uses 4 KiB
pages, WAL, FULL synchronization, 64 MiB connection cache, mmap disabled,
memory-backed temporary results, and one SQL worker. Queries time preparation
and CREATE TEMP TABLE AS SELECT. Oracle/hash checks and fixture restoration
are outside the timer. Integers, types, keys, NULLs, and cardinalities are exact;
floating aggregates use relative tolerance 1e-10 and absolute tolerance 1e-7.

SQLite indexed retains the original five H2O indexes and three taxi indexes.
SQLNEW 0.2 indexed adds one index: (id6 ASC, v3 DESC) for H2O, or timestamp with
included total_amount for taxi. Native grouping needs no persistent grouping
index. Thus these are disclosed workload configurations with different index
costs, not equal-storage configurations. Exact definitions are in the JSON.

## File size and setup

MB is 1,000,000 bytes. Sizes include all indexes, metadata, and free pages after
a truncating WAL checkpoint. Setup timings are single observations, not sampled
throughput estimates. Ready time includes ingestion/commit, index creation,
ANALYZE, and checkpoint; preprocessing is excluded.

| Dataset / engine | File MB | Ingest s | Index s | Ready s |
| --- | ---: | ---: | ---: | ---: |
| groupby / SQLite | 52.09 | 1.41 | 0.00 | 1.61 |
| groupby / SQLite indexed | 174.63 | 1.26 | 6.22 | 8.47 |
| groupby / SQLNEW 0.1 | 24.83 | 2.28 | 0.00 | 2.34 |
| groupby / SQLNEW 0.2 | 24.84 | 2.14 | 0.00 | 2.21 |
| groupby / SQLNEW 0.2 indexed | 69.40 | 2.07 | 2.30 | 4.66 |
| taxi / SQLite | 197.71 | 6.31 | 0.00 | 6.99 |
| taxi / SQLite indexed | 335.45 | 6.53 | 7.69 | 15.52 |
| taxi / SQLNEW 0.1 | 74.14 | 7.96 | 0.00 | 8.24 |
| taxi / SQLNEW 0.2 | 74.15 | 7.63 | 0.00 | 7.95 |
| taxi / SQLNEW 0.2 indexed | 151.78 | 7.89 | 3.19 | 11.80 |

## Query and update medians

All values below are milliseconds. Point and update cases contain 100 operations.
Random updates change v1 or fare_amount; indexed updates change v3 (an index key)
or total_amount (an included value), and both include one FULL-synchronous commit.

### H2O, 1,000,000 rows

| Workload | SQLite | SQLite indexed | SQLNEW 0.1 | SQLNEW 0.2 | SQLNEW 0.2 indexed |
| --- | ---: | ---: | ---: | ---: | ---: |
| h2o_q1 | 770.60 | 70.78 | 882.36 | 153.64 | 155.82 |
| h2o_q2 | 1,192.11 | 94.17 | 1,344.46 | 321.55 | 344.83 |
| h2o_q3 | 913.88 | 95.65 | 1,094.77 | 317.25 | 290.96 |
| h2o_q4 | 833.53 | 112.18 | 868.82 | 124.17 | 128.95 |
| h2o_q5 | 892.82 | 109.80 | 895.31 | 188.03 | 171.05 |
| h2o_q7 | 895.82 | 97.43 | 1,073.60 | 272.54 | 252.94 |
| h2o_q8 | 1,363.38 | 468.35 | 1,445.73 | 1,379.59 | 680.64 |
| h2o_q10 | 2,128.37 | 2,173.01 | 2,623.07 | 2,860.26 | 2,612.49 |
| summary | 114.89 | 90.90 | 3.046 | 3.542 | 3.643 |
| update_read_compact_100 | 938.76 | 136.94 | 1,098.25 | 189.19 | 189.26 |
| indexed_updates_100 | 2.220 | 10.36 | 145.17 | 14.92 | 25.30 |
| point_reads_100 | 1.551 | 1.478 | 17.84 | 20.90 | 20.20 |
| random_updates_100 | 2.552 | 13.21 | 145.29 | 12.93 | 12.43 |

### Taxi, 2,171,187 rows

| Workload | SQLite | SQLite indexed | SQLNEW 0.1 | SQLNEW 0.2 | SQLNEW 0.2 indexed |
| --- | ---: | ---: | ---: | ---: | ---: |
| dsq_taxi_groupby | 1,518.75 | 145.67 | 1,495.45 | 174.22 | 170.81 |
| summary | 328.43 | 337.17 | 5.445 | 6.438 | 6.018 |
| time_range | 217.84 | 0.750 | 5.638 | 5.616 | 1.284 |
| payment_groupby | 1,644.44 | 216.64 | 1,509.41 | 224.73 | 235.97 |
| update_read_compact_100 | 1,666.99 | 234.27 | 1,764.68 | 262.44 | 262.71 |
| indexed_updates_100 | 3.804 | 10.20 | 167.69 | 16.61 | 29.35 |
| point_reads_100 | 1.212 | 1.195 | 8.386 | 11.56 | 11.36 |
| random_updates_100 | 2.388 | 7.293 | 186.30 | 14.49 | 15.34 |

## Complete maintenance cycle

This separately times 100 updates and commit, a grouping query that reads the
changed column, merging every outstanding patch and committing, and a
truncating WAL checkpoint. The changed answer is independently validated before
and after merging. SQLite and 0.1 have no deferred patch merge. Update-statement preparation,
checks, and exact fixture restoration are excluded. Component medians need not
sum to the median total. This exposes deferred work rather than treating a cheap
patch write as the entire mutation cost.

| Dataset / engine | Update + commit | Affected read | Merge + commit | Checkpoint | Total |
| --- | ---: | ---: | ---: | ---: | ---: |
| groupby / SQLite | 3.796 | 914.88 | 0.000 | 9.309 | 938.76 |
| groupby / SQLite indexed | 15.72 | 78.45 | 0.000 | 37.06 | 136.94 |
| groupby / SQLNEW 0.1 | 157.91 | 919.49 | 0.000 | 16.97 | 1,098.25 |
| groupby / SQLNEW 0.2 | 13.35 | 152.31 | 13.94 | 9.681 | 189.19 |
| groupby / SQLNEW 0.2 indexed | 14.65 | 148.52 | 13.52 | 9.107 | 189.26 |
| taxi / SQLite | 2.974 | 1,645.64 | 0.000 | 9.332 | 1,666.99 |
| taxi / SQLite indexed | 6.810 | 208.50 | 0.000 | 17.16 | 234.27 |
| taxi / SQLNEW 0.1 | 163.12 | 1,537.47 | 0.000 | 29.44 | 1,764.68 |
| taxi / SQLNEW 0.2 | 15.74 | 224.28 | 17.52 | 7.524 | 262.44 |
| taxi / SQLNEW 0.2 indexed | 15.72 | 219.54 | 19.79 | 7.351 | 262.71 |

## Balance and remaining gaps

- Taxi grouping moved from 1,495.45 to
  174.22 ms without a persistent grouping index;
  indexed SQLite took 145.67 ms.
- The time range moved from 5.638 to
  5.616 ms by default, or 1.284 ms
  with the covering index. Indexed SQLite took 0.750 ms.
  The index visits exactly 11,953 matching entries and decodes no column payload.
  Covered scalar aggregates execute directly in the B-tree SQL VM.
- The original taxi update workload moved from 186.30
  to 14.49 ms. These sparse writes rewrite no base
  column payload; compaction costs are explicitly included in the cycle above.
  Indexed-field updates still cost 29.35 ms
  versus SQLite indexed's 10.20 ms.
- Patch-aware reads have a cost. Taxi point reads moved from
  8.386 to 11.56 ms
  per 100 lookups. Taxi summaries moved from 5.445
  to 6.438 ms. Both effects remain visible in the report.
- H2O Q10 exceeds the bounded group state and uses the general sorter:
  2,623.07 ms previously versus 2,860.26 ms now.
  The native hash state is capped at 8 MiB and 16,384 groups; the fallback sorter's
  memory/disk policy follows temp_store. This is not a bound on total query memory.
- Optional indexes add 77.62 MB
  for taxi and 44.56 MB
  for H2O. They remain optional because their narrower range/window benefits
  carry real storage, build, and mutation costs. No overall winner score is used.

The [initial 0.2 run](results/comparison-v2-initial.json) validated 1,050 timed
samples but exposed a default range regression. Its counters showed decoding
aggregate payloads in groups with zero exact matches. The final implementation
skips those payloads, touches only used columns in the batch loop, and sends
covered scalar aggregates directly to the index. The full matrix was rerun
afterward. [The original 0.1 comparison](results/comparison.json) and
[update-phase diagnosis](results/update-phases.json) remain historical evidence.

## Other established metrics and limits

The million-row event, packed-integer, and vector exercises were also rerun:
[columnar-v2.json](results/columnar-v2.json),
[columns-v2.json](results/columns-v2.json), and
[vectors-v2.json](results/vectors-v2.json). These are engine-only exercises;
historical timings are not interleaved comparisons. Correctness, storage,
reopen/integrity, and synthetic vector recall remain checked. The vector and
integer algorithms were not changed by this pass.

Separate earlier and current exercise observations (not interleaved timing comparisons):

| Exercise metric | Earlier 0.1 | Current 0.2 |
| --- | ---: | ---: |
| Event ingestion, 1M rows, seconds | 1.528 | 1.656 |
| Event summary, ms | 1.703 | 2.131 |
| Event 100-row slice, ms | 0.499 | 0.632 |
| Event point read, ms | 0.170 | 0.198 |
| Complete event file, bytes | 28,262,400 | 28,274,688 |
| Packed-integer file, bytes | 1,880,064 | 1,880,064 |
| Vector file, bytes | 10,412,032 | 10,412,032 |
| Vector search p50, ms | 9.127 | 9.702 |
| Synthetic recall@10, mean | 1.000 | 1.000 |

The added empty shadow structures cost 12,288 bytes per column table at the
tested 4 KiB page size. The event
exercise shows higher read latency too. The unchanged vector and integer
algorithms retain their exact storage sizes; separate latency observations do
not isolate a code effect from host variability. All five integer distributions
retain their frame sizes and pass their mathematical scan checks.

All results are warm, single-connection measurements from one Windows machine.
They do not establish cold-cache, concurrent-writer, cross-platform, or
production crash performance. See [VALIDATION.md](VALIDATION.md) for the 45-test
AVX2/scalar runs, allocation-failure checks, and 880,666-case core development
matrix, including its instrumentation limits.

## Reproduce

```console
python sqlnew/build.py
python sqlnew/benchmark_build.py
python sqlnew/benchmark_data.py
python sqlnew/benchmark_v2.py --repetitions 5
```

The comparison additionally needs the frozen 0.1 library retained in this
workspace at build/comparison/before-v2/build/sqlnew/sqlnewdb.dll. Its hash is
recorded with every previous-engine configuration. Downloads use pinned public
files; all engines share the same native CSV loader and independent typed oracle.
No Docker or database server was used for this comparison. The runner requires
a repetition count divisible by five and creates fresh files for every run.
