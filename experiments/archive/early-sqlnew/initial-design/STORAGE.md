> Archived experiment notes, superseded by subsequent implementation. These are not implementation requirements.

# Storage and transaction contract

## Implemented now

SQLNEW is an embedded file database. The CLI and Python layer call the C engine
in the same process. A future server is optional packaging around that engine.
The application needs no network service. All durable document data and indexes
belong to one database, with normal transactional journal/WAL sidecars while open.

The database supports a SQLNEW signature, nullable integer column blocks,
compressed bodies and attachments, compressed
vector-block frames, text postings, centroid metadata, and a versioned index
generation. These currently use the imported pager and SQL transaction machinery.
There is no format migration layer.

For each document index, the authoritative row table contains an integer identity,
the original embedding, a compressed body, and an optional compressed attachment.
Its insert/update/delete triggers update a delta-ID table and contentless FTS
postings in the same transaction. Updating a row ID marks both old and new IDs.

A sealed index generation contains centroids, conservative radii, membership,
and bounded vector blocks. Rebuild
takes the writer before reading its training data, creates the replacement inside
one transaction, and clears the delta only after replacement blocks exist.
Rollback restores the previous blocks, centroids, membership, delta, and
generation together.

Search opens a read snapshot spanning metadata, centroid selection, block scans,
delta scans, candidate reranking, and document visibility. A sealed candidate
whose ID is in the delta is excluded before block top-k. Live rows in the delta
are searched from their current original vectors; deleted rows have no live
counterpart. This prevents stale vectors and duplicate old/new versions.

Mandatory ID filters are applied inside each native block scan before selection.
Hybrid text filtering precedes lexical LIMIT as well. Python API nesting uses
savepoints, so a caller can combine multiple operations and roll everything back.
The wrapper has no direct external file, shell, or network action in its SQL
functions. Native allocations go through SQLite's allocator.

Ordinary SQLite tools cannot open SQLNEW files. The SQLite-derived pager is still
responsible for durability, and connection pragmas still matter. Tests use WAL
with its default durability policy. The implementation has not been through the
full upstream release suite or comprehensive power-loss fault injection.

## Proposed next storage engine — not implemented

The architecture proposes typed immutable blocks, a small mutable row delta,
and a manifest describing a complete table/index snapshot. Compression blocks,
directory nodes, execution batches, and physical transfer units are independent.

Before replacing the pager, implement and validate these invariants:

1. A published generation references only complete, validated extents.
2. One manifest publishes row versions, uniqueness structures, text postings,
   and vector indexes together, or explicitly routes uncovered rows through a
   complete delta.
3. A read transaction pins one generation; no referenced extent is reused while
   a live reader can access it.
4. A commit is acknowledged only after the required VFS durability boundary
   succeeds. Failure cannot be turned into success by caching the new root.
5. Recovery distinguishes an incomplete unacknowledged tail from corruption of
   data previously acknowledged durable. It must not silently drop a committed
   transaction by falling back to an older valid-looking root.
6. Compaction writes new extents before publication, and reclamation follows
   durable publication and reader release. Compaction is itself transactional.
7. Bounds and checksums are validated before lengths control allocation or I/O.
8. Torn roots, stale roots, short writes, out-of-space, and interrupted sync are
   explicit states. A 4 KiB sector is not assumed to be an atomic transaction.

An initial design can use an append-only transaction stream plus checksummed
checkpoint anchors. Reusing extents needs a durable free-space map and an
explicit crash-safe anchor protocol. Multiple writable processes need a shared
snapshot registry or a correct file-lock alternative. A process-local epoch
counter alone cannot protect readers in another process.

Start with one publishing writer and concurrent readers. Parallel encoding and
search can proceed before introducing multiwriter conflict detection. Long-lived
readers and background consolidation consume disk and memory budgets; the engine
must report backpressure or resource exhaustion instead of violating visibility.
