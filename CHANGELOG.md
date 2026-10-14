# Changelog

## 0.2.1-dev — development candidate

- Add optional `sqlnew_region` tables with fixed three-dimensional extents,
  shared immutable bases, separate edits, selective large-value retrieval and
  transactional compaction.
- Add exact numeric folding and canonical adjacent merges. Columnar folding
  remains opt-in, with existing encodings retained when preferable.
- Preserve compressed columns, summary/batch aggregates, covering indexes and
  the vector/text Python APIs.
- Correct UTF-16 column comparisons and index lifecycle behavior; share retained
  dictionary payloads under a configurable connection memory budget.
- Update the inherited SQLite core to the pinned October 14 `branch-3.54`
  maintenance snapshot. This includes statement rollback, B-tree deletion,
  Windows WAL recovery and query-result fixes. Its upstream version is an
  unreleased 3.54.1 snapshot, not a released SQLite version.
- Organize historical experiments, add contributor guidance and Windows/Linux
  CI, and make the current benchmark independent of private consumer checkouts
  and saved old binaries.

This remains experimental. Files use `SQLNEW format 2`; compatibility with
SQLite files, old experimental files and future SQLNEW versions is not promised.
See [validation evidence](sqlnew/VALIDATION.md) for the scope of completed checks.
