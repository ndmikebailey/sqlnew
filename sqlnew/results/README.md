# Recorded evidence

Reports are measurements of specific source/library hashes and fixtures. They
are not continuously updated claims about every later build. Local paths identify
the original run; generated databases and binaries remain under ignored `build/`.

| Reports | Scope |
|---|---|
| `dev021-maintenance-validation.json` | Current maintenance intake, product checks and core devtest summary |
| `dev021-maintenance-benchmark.json` | Current self-contained benchmark, including the optional previous-library arm |
| `dev021-contracts.json` | Current direct region/folding workflows |
| `dev021-benchmark.json` | Historical pre-maintenance four-arm run, using saved 0.2 library and doc2sql source passages |
| `dev021-density.json`, `dev021-doc2sql.json` | Pre-maintenance density and single-process consumer checks |
| `core-review-validation.json` | September 6 UTF-16, index lifecycle and shared-memory review |
| `comparison-v2*.json`, `columnar-v2.json`, `columns-v2.json`, `vectors-v2.json`, `validation.json` | Earlier 0.2 development results |
| `comparison.json`, `columnar.json`, `columns.json`, `latest.json`, `validation-0.1.json` | Historical 0.1 results; `latest.json` is a historical vector run |
| `update-phases.json` | Earlier update/commit timing diagnostic |

See [validation](../VALIDATION.md), [benchmark methodology](../BENCHMARKS.md) and
the [attempt journal](../../experiments/exact_fold/ATTEMPTS.txt) for interpretation.
New benchmark runs default to `build/dev021/benchmark.json`; recorded evidence is
updated only with an explicit `--output` path. Historical reports remain intact.
