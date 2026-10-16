# Recorded evidence

Reports are measurements of specific source/library hashes and fixtures. They
are not continuously updated claims about every later build. Local paths identify
the original run; generated databases and binaries remain under ignored `build/`.

| Reports | Scope |
|---|---|
| [Common application tasks](dev021-common-tasks.json) | 100,000 events, 15 tasks, six configurations, 1,170 exact retained samples; released 3.54.0 and matched-core baselines, full storage/index/write costs |
| [Version read audit](dev021-version-reads.json) | Identical 100,000-row fixture on retained 0.1.0, saved 0.2.0 and current 0.2.1 columns; four tasks, exact answers and reopened inputs |
| [Join-order diagnosis](dev021-join-order.json) | Read-only follow-up on restored files, default and forced event-outer plans, exact answers, unchanged hashes and native work counters |
| [Documents against released SQLite](dev021-documents-sqlite-release.json) | Fresh default-passage comparison with official 3.54.0; common FTS5 discovery, exact reconstruction, edits, maintenance and reopen |
| [Published preview validation](dev021-preview-hosted.json) | Tagged Windows/Linux product/core checks, all four package identities and published download verification |
| [Documents: 1,024](dev021-documents-1024.json), [4,096](dev021-documents-4096.json), [16,384](dev021-documents-16384.json) | Nine exact real-text arms; common FTS5 discovery, points, whole documents, changes and compaction |
| [Document repeats](dev021-documents-repeat.json) | Both passes' raw timing samples; matched originals, native binaries and file sizes, including observed timing variation |
| [Layout and package validation](dev021-layout-validation.json) | Relocated inherited tree, fresh generation, core devtest, optimized/scalar Windows static-runtime packages |
| [Hosted CI](dev021-hosted-ci.json) | Initial public commit: Windows/Linux optimized and scalar product checks, Linux core devtest |
| [Maintenance validation](dev021-maintenance-validation.json) | Local maintenance intake, product checks and core devtest summary; its pending CI list predates the hosted run |
| [Maintenance benchmark](dev021-maintenance-benchmark.json) | Self-contained benchmark, including the optional previous-library arm |
| [Direct contracts](dev021-contracts.json) | Current direct region/folding workflows |
| [Pre-maintenance benchmark](dev021-benchmark.json) | Historical four-arm run, using saved 0.2 library and doc2sql source passages |
| [Density](dev021-density.json), [doc2sql](dev021-doc2sql.json) | Pre-maintenance density and single-process consumer checks |
| [Core review](core-review-validation.json) | September 6 UTF-16, index lifecycle and shared-memory review |
| [0.2 comparison](comparison-v2.json), [initial run](comparison-v2-initial.json), [columnar](columnar-v2.json), [columns](columns-v2.json), [vectors](vectors-v2.json), [validation](validation.json) | Earlier 0.2 development results |
| [0.1 comparison](comparison.json), [columnar](columnar.json), [columns](columns.json), [vectors](latest.json), [validation](validation-0.1.json) | Historical 0.1 results; `latest.json` is a historical vector run |
| [Update phases](update-phases.json) | Earlier update/commit timing diagnostic |

See [validation](../VALIDATION.md), [benchmark methodology](../BENCHMARKS.md) and
the [attempt journal](../../experiments/exact_fold/ATTEMPTS.txt) for interpretation.
New benchmark runs default to `build/dev021/benchmark.json`; recorded evidence is
updated only with an explicit `--output` path. Historical reports remain intact.
