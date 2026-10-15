SQLNEW 0.2.1 development preview: exact folding, compressed columns, sparse shared
regions, separate edits and selective passage retrieval in an embedded SQL engine.

Download the archive for Windows x64 or Linux x64, extract it, and follow its
`README.txt`. The optimized build selects AVX2 at runtime when available; the
scalar build disables SIMD. Windows includes the C runtime. Linux builds use
the hosted Ubuntu 24.04 glibc baseline. macOS and ARM binaries are not included.

Each archive contains the CLI, embedded core library, optional loadable module,
public C header, SQL walkthrough, source provenance, build configuration and
file hashes. `SHA256SUMS` covers the downloadable archives. Packaging runs the
SQL walkthrough from an isolated extraction; hosted builds also run the product
checks in each configuration and inherited-core devtest before publication.

This is an experimental preview, not a production release. SQLNEW format 2
requires fresh databases; SQLite files and earlier experimental formats are
not interchangeable, and future migration compatibility is not promised.

The inherited engine is pinned to an unreleased SQLite branch-3.54 maintenance
snapshot, identified in `UPSTREAM.json`. SQLNEW is an independent project.
See the repository's build guide, validation records and measured tradeoffs.
