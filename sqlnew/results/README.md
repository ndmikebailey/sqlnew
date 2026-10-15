# Recorded evidence

Reports are measurements of specific source/library hashes and fixtures. They
are not continuously updated claims about every later build. Local paths identify
the original run; generated databases and binaries remain under ignored `build/`.

| Reports | Scope |
|---|---|
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
