> Archived experiment notes, superseded by subsequent implementation. These are not implementation requirements.

# Implementation status

## Present

- Current SQLite source imported with pinned Git and Fossil provenance.
- Native product build, SQLNEW file identity, CLI and in-process C library.
- Lossless bounded-window compression with raw fallback and checksum.
- Nullable int64 column blocks, range-based bit widths, SQL scan and predicates.
- Float32 and symmetric int8 vector-block aggregate formats.
- Runtime-dispatched scalar/AVX2 float64 distance kernels for both formats.
- Native heap-based top-k, signed-ID tie breaking, pre-selection ID filters.
- Offline centroid training and atomic block-index sealing.
- Immediate update/delete visibility through a transactional delta.
- Exact original-vector search with conservative cluster-ball pruning and an
  exhaustive fallback; see the arithmetic assumptions in [GEOMETRY.md](../../../../sqlnew/GEOMETRY.md).
- Compressed document storage with contentless FTS and hybrid ranking.
- Correctness tests for native formats, malformed inputs, scoring, filters,
  index rebuild, rollback, snapshot isolation, text maintenance, and reopen.

The storage implementation resides in `ext/sqlnew/`. Product build
configuration is in `sqlnew/config.h`. The Python index layer orchestrates native
SQL operations; it is not a server. Existing SQL syntax and C API names are useful
starting machinery, not compatibility requirements.

## Next cuts

1. Integrate typed blocks into SQL table storage and batch execution. Current
   integer blocks are explicit SQL values, not automatic table compression.
2. Stream sealing in C to cap memory, then add bounded incremental compaction.
3. Avoid repeated block validation through trusted, transactionally maintained
   metadata; retain strict validation at untrusted byte boundaries.
4. Make coarse routing filter-aware and expose bytes/candidates visited.
5. Extend integer codecs with measured choices for deltas, exceptions, and runs;
   no one encoding is best for every distribution.
6. Implement the new extent/manifest protocol under failure injection before
   replacing transactional storage.

These steps are not required to preserve migrations, SQLite file compatibility,
or an old product interface. There is no upstream performance scoreboard.

## Validation policy

Check behavior against the contract: bytes round-trip, corrupt frames fail,
mathematical neighbors rank correctly, mandatory filters hold, and transactional
updates are visible at the correct snapshot. Record this engine's resource use
where it informs a design decision. Never describe synthetic search recall as
real semantic relevance, quantized index size as total database savings, or a
new file signature as a complete pager redesign.

The upstream test tree remains available. Changes to `src/` require at least
the upstream `devtest` check under repository guidance; the present native
implementation and product configuration do not modify `src/`.
