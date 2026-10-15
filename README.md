# SQLNEW

[![Build and validate](https://github.com/ndmikebailey/sqlnew/actions/workflows/build.yml/badge.svg?branch=main)](https://github.com/ndmikebailey/sqlnew/actions/workflows/build.yml)

**An experimental SQL engine for exact compression, shared structure, and selective retrieval.**

SQLNEW explores how a database can recognize repeated structure, store it
compactly, and retrieve useful pieces without repeatedly expanding the data
around them. It brings ordinary rows, compressed columns and shared spatial
regions into one embedded SQL engine built on SQLite.

The goal is practical: less data handling before a person or agent gets the
records, passages or context they need.

[Get started](#try-it) | [Recorded benchmarks](#benchmarks-recorded-so-far) |
[Explore the code](#explore-the-project) | [Contribute](CONTRIBUTING.md)

## The idea

Repeated structure can become a shared foundation. Changes can live beside
that foundation, preserving the original compressed representation. Retrieval
can follow a coordinate, row or index to the requested values and apply their
current changes as they are read.

This began as an exploration of sparse lattices, reversible folding and merging
data reproducibly. The useful parts now have native implementations. The
experiments, unsuccessful attempts and decisions remain available alongside
the code so the direction can be questioned and improved.

For document and agent workflows, the longer-term aim is to make collecting
useful context require less loading, decoding and repeated data movement.
Today's work focuses on the storage and retrieval primitives that support that
idea, with ordinary SQL as the interface.

## What works today

**Dev 0.2.1** combines several storage approaches, each useful for different work:

| Approach | What it provides | Implementation |
|---|---|---|
| Compressed columns | Typed encodings, summary aggregates, native grouped queries, sparse updates and optional covering indexes | [columnar.c](ext/sqlnew/columnar.c) |
| Shared regions | Sparse XYZ coordinates, immutable shared bases, separate changes and individual text/blob bodies | [region.c](ext/sqlnew/region.c) |
| Exact folding | Lossless 64-bit word encodings and canonical merges of adjacent streams; optional in columns | [fold.c](ext/sqlnew/fold.c) |
| Vector and text helpers | Transactional indexing and retrieval through the Python API | [index.py](sqlnew/index.py) |

Ordinary indexed rows remain available. These paths use the inherited SQL and
transactional pager. A region cell can hold a large passage or binary payload;
reading its body does not require expanding the bodies of neighboring cells.
Edits stay separate until explicit region compaction.

## Benchmarks recorded so far

### Current 0.2.1 comparison

Four fixtures were compared across current storage approaches and a pinned,
indexed SQLite baseline. Each numeric fixture contains **262,144 values**;
the text fixture contains **2,048 constructed source passages** of approximately
8 KiB each. All **20 runs**, including an additional previous-version column
arm, passed their input, query-answer, update/restoration and reopen checks.

**Complete database size, KiB (1 KiB = 1,024 bytes):**

| Fixture | Columns | Folded columns | Regions | Indexed SQLite |
|---|---:|---:|---:|---:|
| Repeated shape with different offsets | 2,400 | 2,400 | **52** | 13,252 |
| Quadratic clock sequence | 1,672 | 268 | **232** | 12,748 |
| Independent random bits | 2,400 | 2,400 | 2,364 | 13,252 |
| Overlapping source passages | **1,600** | **1,600** | 9,468 | 17,488 |

**Median time for 64 point requests, milliseconds:**

| Fixture | Columns | Regions | Indexed SQLite |
|---|---:|---:|---:|
| Repeated shape with different offsets | 4.406 | 1.772 | **0.670** |
| Quadratic clock sequence | 3.206 | 2.364 | **0.554** |
| Independent random bits | 4.368 | 2.685 | **0.551** |
| Overlapping source passages | 672.099 | 4.155 | **1.388** |

The repeated-shape fixture uses about **46x less storage** for regions than
columns. The passage fixture completes its 64 point requests about **162x faster**
with regions than columns, with about **5.9x more storage**. Indexed
SQLite leads these warm point lookups. These results support keeping several
storage paths available.

These are warm, single-host Windows measurements with prepared calls, result
copying, two warmups and 13 retained samples. SQLite includes three coordinate
indexes. Sizes include metadata and indexes; ingestion and region sealing costs
are recorded separately. The repeated-shape fixture is intentionally favorable,
random bits provide a control, and source windows are not a document corpus.

**Project files:** [raw measurements](sqlnew/results/dev021-maintenance-benchmark.json),
[benchmark runner](sqlnew/benchmark_dev021.py),
[baseline builder](sqlnew/benchmark_build.py), and
[full methodology and costs](sqlnew/BENCHMARKS.md).

### Public documents and keyword retrieval

A newer comparison uses **seven complete public books/technical documents**
with non-overlapping passages. Every storage method includes the same FTS5
candidate index, so keyword requests can find passages before fetching bodies.
All nine final arms across three passage sizes passed exact restoration,
update, compaction and reopen checks.

At the default 4,096-character passage size:

| Measurement | Columns | Regions | Indexed SQLite |
|---|---:|---:|---:|
| Complete database, KiB | 3,428 | 4,500 | 4,864 |
| 64 point requests, median ms | 171.627 | 3.193 | 1.013 |
| Seven keyword requests, median ms | 173.416 | 2.771 | 0.884 |

At 16,384 characters, region and column files are nearly equal in size and
regions take about 25x less time for point reads in this run. Passage size
changes the amount of returned context. Indexed SQLite retains the fastest
points. See [methods, setup costs and repeat variation](sqlnew/BENCHMARKS.md#public-documents-and-passage-retrieval),
[raw default results](sqlnew/results/dev021-documents-4096.json) and the
[corpus archive identity](sqlnew/corpora/public-documents-archive.json).

### Earlier development comparisons

Earlier 0.1-to-0.2 measurements also captured progress and costs on larger
workloads. All values below are median **milliseconds** and describe those
recorded versions, rather than a rerun of today's engine.

| Historical workload | SQLNEW 0.1 | SQLNEW 0.2 | Indexed SQLite |
|---|---:|---:|---:|
| H2O whole-table summary, 1 million rows | 3.046 | 3.542 | 90.90 |
| Taxi grouping, 2.17 million rows | 1,495.45 | 174.22 | 145.67 |
| Taxi sparse updates, 100 operations and commit | 186.30 | 14.49 | 7.293 |
| Taxi point reads, 100 operations | 8.386 | 11.56 | 1.195 |

The summary path reads maintained metadata. The grouping and update results
show the effect of native batches and separate patches; the point results
also preserve a regression between SQLNEW versions. Index layouts and complete
maintenance costs are disclosed in the
[comparison notes](sqlnew/BENCHMARKS.md#historical-02-measured-gains-and-costs).
The [original report](sqlnew/results/comparison-v2.json) retains samples, plans,
settings and source hashes.

## Try it

Get a [development preview download](https://github.com/ndmikebailey/sqlnew/releases)
for Windows x64 or Linux x64, extract it, and follow the included `README.txt`.
The CLI runs without Python or a compiler. Each preview includes optimized and
scalar archives, source/build identities, file hashes and the SQL walkthrough.

To build from source:

Use Python **3.12+** with Visual Studio C++ Build Tools on Windows, or a C
compiler and make on Linux:

```console
python sqlnew/build.py
python sqlnew/exercise_dev021.py
```

Run the [small SQL walkthrough](sqlnew/examples/quickstart.sql) with the built
CLI, from the repository root:

```powershell
# Windows PowerShell
.\build\sqlnew\sqlnew.exe :memory: ".read sqlnew/examples/quickstart.sql"
```

```sh
# Linux
./build/sqlnew/sqlnew :memory: ".read sqlnew/examples/quickstart.sql"
```

It demonstrates compressed columns, passage retrieval by coordinate, and
separate edits with rollback. Everything runs in memory.

The native engine requires no Python runtime or database server once built.
For SQL examples, optional Python dependencies and benchmark reproduction,
see the [build and usage guide](sqlnew/README.md).

## Explore the project

- [Native SQLNEW modules](ext/sqlnew/) and [product tools](sqlnew/) contain the current implementation.
- [Inherited engine](engine/README.md) keeps the SQL compiler, pager and its build/test tree together under `engine/sqlite/`.
- [Formats and storage behavior](sqlnew/FORMAT.md) describes how the native representations work.
- [Validation](sqlnew/VALIDATION.md) links the completed checks and their scope.
- [Experiment archive](experiments/README.md) preserves the prototypes and research trail.
- [Attempt journal](experiments/exact_fold/ATTEMPTS.txt) records findings, failures and course corrections in one place.
- [Changelog](CHANGELOG.md) and [contributor guide](CONTRIBUTING.md) describe current changes and how to take part.

SQLNEW is an independent experimental SQLite fork at **0.2.1-dev**. Local
validation includes 56 product tests in optimized and scalar builds, 360
canonical folding cases per configuration, and 881,414 core checks after source relocation.
See the [validation record](sqlnew/results/dev021-layout-validation.json)
for exact sources, configurations and remaining coverage.

The [first hosted run](https://github.com/ndmikebailey/sqlnew/actions/runs/38656971916)
also passed Windows and Linux optimized/scalar product checks, plus **979,196
Linux core checks with zero errors**. Its commit and per-build results are in
the [hosted CI record](sqlnew/results/dev021-hosted-ci.json).

Files use **SQLNEW format 2** and require fresh databases; SQLite files and
earlier experimental formats are not interchangeable. This is a development
project with no production or future format-compatibility commitment yet.
[Upstream provenance](sqlnew/upstream.json) and
[license information](LICENSE.md) are retained with the project.
