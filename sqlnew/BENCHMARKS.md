# SQLNEW benchmarks

## Public documents and passage retrieval

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
