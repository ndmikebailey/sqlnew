# SQLNEW 0.2.1 development formats

These formats are experimental. Recreate data when they change. There is no
migration support or obligation to open legacy SQLite databases.

## Database identity

The first 16 bytes are `SQLNEW format 2\0`, configured by `sqlnew/config.h`.
The present pager, B-tree layout, rollback journal, and WAL remain derived from
the imported SQLite tree. Changing the signature establishes product identity;
it does not by itself create a new storage algorithm. Writable column groups
and the native byte, integer, and vector encodings below implement the storage
changes.

CSC1, SNZ1, SNI1, and SNV1 frame fields use little-endian byte order.
Covering-index sort keys use big-endian order as described below. The underlying pager's
fields continue to use its own encoding. Do not infer frame byte order from
pager byte order.

Dev 0.2.1 retains the format-2 pager signature. `sqlnew_capabilities()` reports
available storage modules and codecs. Region tables require `sqlnew_region`;
an older engine without that module cannot query them. New folded column frames
require the SFL1 decoder. These capabilities, rather than the pager signature
alone, determine whether a reader can interpret every table. No migration of
older experimental prototype packets is provided.

## Native shared region tables

`sqlnew_region(nx,ny,nz,TYPE)` presents X/Y/Z and one nullable typed value.
Dimensions are positive, individually at most INT32_MAX, with a product fitting
signed int64. Rowid is `x + nx*(y + ny*z)`. Each coordinate belongs to an 8-cube:
slot is `(x%8) + 8*(y%8) + 64*(z%8)`; block is
`(x/8) + ceil(nx/8)*((y/8) + ceil(ny/8)*(z/8))`.
Unoccupied outer slots are absent; occupied NULL is separately represented.

For table `t`, shadow storage consists of:

| Object | Contents |
|---|---|
| `t_blocks` | Spatial block identity, immutable base ID, anchor, and block X/Y/Z |
| `t_bases` | Shared encoded base payloads and hash lookup metadata |
| `t_values` | Independently compressed TEXT/BLOB payloads and hash metadata |
| `t_patch` | Latest state and word/value ID per changed block/slot |
| `t_refs` | Base-to-TEXT/BLOB-value dependencies for collection |

Base/value IDs are monotone AUTOINCREMENT identities. Hash indexes narrow
candidate payloads; complete encoded-byte comparison establishes equality.
Hash lookup indexes use automatic `UNIQUE(hash,id)` constraints, avoiding
name conflicts when a region table is renamed and its original name is reused.
State 0 in patches is deletion, 1 is occupied NULL, 2 is a value. INTEGER/REAL
values use raw 64-bit patterns in a signed SQL integer limb; TEXT/BLOB values
use an immutable value ID. Original-state restoration removes the patch.

Each base is one SNZ1 checksummed frame around RSC1. RSC1 has a 16-byte header:
magic `RSC1` at 0, version 1 at 4, type (1 INTEGER, 2 REAL, 3 TEXT, 4 BLOB) at 5,
codec (0 raw words, 1 SFL1 words) at 6, axis-difference mask at 7, little-endian
slot count 512 at 8, and four zero bytes at 12. Two 64-byte bitmaps follow:
occupied slots, then occupied NULL slots. Bits run in increasing slot order.
The remaining payload is 512 little-endian words or a single SFL1 frame of
those words. Numeric non-NULL words are normalized modulo 2^64 by a common
anchor stored in the block directory. Empty and NULL words are zero before
the axis transform. Text/blob words are value IDs with anchor zero.

Mask bits 1/2/4 select X/Y/Z first differences with boundary seeds retained.
Inverse prefix sums restore the normalized words exactly. The writer evaluates
the eight numeric masks and raw/folded payloads through SNZ1, choosing the
smallest complete frame. Text/blob references use the byte-preserving mode.
Value bodies themselves are SNZ1 frames; no numeric transform touches their bytes.

All shadow writes participate in the ordinary pager transaction. Compaction
publishes replacement block/base references, clears patches and collects
unreferenced payloads together. Rollback restores the old state, and concurrent
WAL snapshots continue to see their own roots and values. Historical retention
beyond normal database snapshots has no separate persistent-generation API.

## SFL1 exact folding and CSC1 codec 2

SFL1 has a 32-byte header: magic at 0; tile log2 7 at 4; zero bytes 5..7;
unsigned word origin at 8; unsigned word count at 16; zero bytes 24..31.
Tiles end at absolute multiples of 128 words. Each tile has a four-byte
mode/width/zero/zero prefix. Modes are raw, minimum offsets, first differences,
second differences, and XOR differences. Nonraw modes retain their minimum
residual, necessary seeds and bit-packed offsets. Arithmetic is modulo 2^64;
difference residuals use reversible zigzag. Fixed mode order breaks size ties.
Unused packed bits are zero. Adjacent-origin merges refold only an unaligned
boundary tile; other encoded bytes are copied. Materialized merging still
copies the output and is not constant time.

CSC1 numeric codec 2 retains the original NULL map and stores non-NULL words
in slot order in an SFL1 payload at origin zero. Width is zero and header bytes
8..27 are zero. The usual outer SNZ1 supplies the checksum. The connection's
`sqlnew_columnar_folding(0|1)` controls future selection, not decoding. The
folding selector retains legacy encodings unless the candidate materially
improves the complete compressed column frame.

## Writable column tables

`sqlnew_columnar` stores each group of 1,024 consecutive rowid positions as
separate typed columns. Group ID is floor(rowid / 1024); a row's slot is the
low ten bits of its signed 64-bit rowid. Negative rowids use the same mapping.
Each group has a 128-byte live bitmap and a live-row count. Deleted positions
have no live bit and NULL values in every encoded column.

For a table named `t`, the module creates:

| Shadow object | Contents |
|---|---|
| `t_meta` | Next automatic rowid and exhausted-rowid flag |
| `t_blocks` | Group ID, live bitmap, live-row count |
| `t_columns` | Per-group, per-column extrema, non-NULL count, integer sum and prefix extrema |
| Automatic index on `t_columns` | Covering index over column, maximum, minimum, count, and group |
| `t_data` | Per-group, per-column compressed payload |
| `t_patch` | Sparse changed values and their original base values, keyed by column/group/slot |
| `t_indexes` | Covering-index definitions, identity, and active state |
| `t_index_N` | Optional covering B-tree with encoded keys, rowid, and typed projection values |

The range index is declared as a UNIQUE constraint; `(col,block)` already
identifies each entry. This lets the SQL core maintain its name on table
rename, without leaving an index that prevents reuse of the old table name.
Metadata and payload are separate B-trees. `t_columns` is WITHOUT ROWID;
`t_data` has an ordinary rowid B-tree and a unique `(block,col)` index, allowing
large payloads to use ordinary table overflow pages. SQL predicates consult
the range metadata before reading payloads. SQL residual predicates remain
in place to preserve comparison and affinity semantics.

Integer summaries store a signed 128-bit sum and the minimum and maximum
prefix sums in rowid order. Each number occupies two 64-bit SQL INTEGER limbs,
with the low limb interpreted as unsigned. Combining a group's prefix extrema
with the preceding total detects intermediate signed-64-bit SUM overflow,
including cancellation where the final mathematical sum would fit. COUNT,
eligible integer SUM, and binary-collation MIN/MAX can read these summaries
without decoding columns. The SQL compiler selects this path for eligible
whole-table aggregates. Patched groups recompute the requested summary from
that column; untouched groups retain the summary path. Eligible filtered and
grouped aggregates use the bounded native batch executor.

One mutable group is cached per table. Changing groups, synchronizing a
transaction, or entering a savepoint flushes it through ordinary shadow-table
writes. Flushing before the pager opens the savepoint preserves the state that
ROLLBACK TO must restore. Rollback discards cached changes and reloads rowid
state. A reader includes a snapshot of the connection's pending group. Shadow
writes, data, and summaries participate in the same pager transaction.

These shadow objects are internal storage. Direct edits can corrupt their
relationships. Module integrity checking decodes columns and checks live rows,
extrema, counts, integer sums/prefixes, and orphaned metadata or payloads.
Normal summary queries trust the persisted metadata, like index lookups do.

### Sparse writes and covering indexes

Same-rowid updates request only changed columns through `sqlite3_vtab_nochange`.
The patch B-tree stores the new value and its original base value. Restoring the
original removes the patch. Readers merge patches in their own pager snapshot;
numeric pruning includes every patched group so stale base extrema cannot omit
new matches. At 64 changed slots in one column/group, the writing transaction
merges that column. `INSERT INTO t(__sqlnew_command) VALUES('compact')` merges
all remaining patches. Compaction rewrites only affected column payloads and
summaries. INSERT, DELETE, and rowid changes retain the mutable-group path.

Optional covering indexes use WITHOUT ROWID B-trees in the same transaction and
file. NULL encodes as byte 0; non-NULL keys start with byte 1. Signed integers
flip the sign bit and encode big-endian. REAL keys use an order-preserving IEEE
sign transformation and canonical zero. TEXT keys append canonical UTF-8 bytes;
BLOB keys append their raw bytes. Both use binary comparison. UTF-16 database
text queries retain engine collation evaluation instead of using these keys
or UTF-8 extrema for pruning, native grouping, or consumed ordering. Key fields
may independently ascend or descend.
Stored typed values cover projections without decoding column groups. These
keys duplicate values deliberately; their space and write costs are measured.

Index creation validates name conflicts before any storage mutation. An ignored
duplicate creation leaves no backing table behind.

Index removal clears entries and marks the definition inactive. The empty root
is retained because nested DROP during a virtual-table write is locked by the
SQL core. A later identical definition reuses it; dropping the parent table
removes all roots. Statements prepared before removal retain their ordering
through a base scan and SQL sort. Index names are local to each column table.

The 0.2 file signature rejects 0.1 databases. Recreate experimental data; no
migration path is provided. These schema details are implementation state, not
constraints on later designs.

### CSC1 column frame

Each `t_data.data` value is an SNZ1 frame containing a CSC1 frame:

| Offset | Bytes | Meaning |
|---|---:|---|
| 0 | 4 | ASCII `CSC1` |
| 4 | 1 | Type: 1 INTEGER, 2 REAL, 3 TEXT, 4 BLOB |
| 5 | 1 | Codec: 0 default, 1 affine integer or dictionary bytes |
| 6 | 1 | Integer offset width or dictionary-code width |
| 7 | 1 | NULL mode: 0 none, 1 bitmap, 2 all NULL |
| 8 | 8 | Integer minimum or affine first value |
| 16 | 8 | Integer maximum or affine step |
| 24 | 4 | First non-NULL slot for affine encoding |
| 28 | 4 | Slot count, currently 1,024 |
| 32 | variable | Optional NULL bitmap and values |

NULL mode 1 adds a 128-byte bitmap, with a set bit marking a NULL position.
Mode 2 consists of the header alone. Encodings are chosen independently for
each column and group:

- **INTEGER, codec 0:** 1,024 fixed-width offsets from the minimum, packed
  least-significant bits first. The width is the bit length of unsigned
  `maximum - minimum`, including the full signed range. Constant values need
  zero offset bits. NULL offsets are zero.
- **INTEGER, codec 1:** a value at slot `i` is
  `first_value + step * (i - first_slot)`. There is no per-value payload.
  Encoding and decoding check exact arithmetic and signed range; intervening
  NULLs do not advance a separate ordinal.
- **REAL, codec 0:** finite IEEE-754 binary64 values, little-endian, for
  non-NULL slots only. Lossless byte compression follows this encoding.
- **TEXT/BLOB, codec 0:** non-NULL values in slot order, each with a four-byte
  unsigned byte length followed by its bytes. TEXT is UTF-8, including any
  embedded NUL bytes.
- **TEXT/BLOB, codec 1:** a four-byte dictionary-entry count; entries each
  contain a four-byte length and bytes; 1,024 fixed-width dictionary codes
  follow. The width is the bit length of `entry_count - 1`. The writer chooses
  this only when smaller than the raw representation.

The current unwrapped column frame limit is 64 MiB minus the outer header.
The outer SNZ1 frame supplies a checksum; CSC1 validates structure, lengths,
codes, types, and integer reconstruction. These checks detect accidental
corruption and malformed input, not authenticated changes to values.

## SNZ1 lossless byte frame

| Offset | Bytes | Meaning |
|---|---:|---|
| 0 | 4 | ASCII `SNZ1` |
| 4 | 4 | Decoded byte count, unsigned |
| 8 | 4 | Adler-style checksum: low half sum of bytes plus 1, high half cumulative sum; modulo 65,521 |
| 12 | 1 | Codec: 0 raw, 1 bounded-window matches |
| 13 | 3 | Reserved, must be zero |
| 16 | variable | Payload |

For codec 1, each token is either:

- High bit clear: low 7 bits plus 1 literal bytes follow (1..128).
- High bit set: low 7 bits plus 4 bytes are copied (4..131); a two-byte
  little-endian backward distance follows (1..65,535). Overlapping copies work
  by expanding bytes in order.

The encoder uses a fixed 65,536-slot hash match finder and greedy selection.
If coded output is no smaller, it stores raw payload. Consequently output is
at most input length plus the 16-byte header. Empty input is a valid raw frame.
NULL propagates through both SQL functions. Compression returns bytes; text
requires explicit conversion on decode.

Both functions enforce SQLite's connection length limit and a 64 MiB ceiling.
The encoder reserves header space within that ceiling. Decode rejects bad
magic, unknown codecs, nonzero reserved bytes, impossible lengths, truncated
tokens, zero/out-of-range distances, excess decoded bytes, and checksum errors.
The checksum detects accidental corruption; it is not authentication.

## SNI1 nullable integer block

| Offset | Bytes | Meaning |
|---|---:|---|
| 0 | 4 | ASCII `SNI1` |
| 4 | 4 | Row count, unsigned, nonzero |
| 8 | 8 | Minimum non-NULL value, signed int64 |
| 16 | 8 | Maximum non-NULL value, signed int64 |
| 24 | 4 | Bits per offset, unsigned, 0..64 |
| 28 | 4 | NULL count, unsigned |
| 32 | 4 | Adler-style checksum over the complete frame, excluding these four bytes |
| 36 | 4 | Reserved, must be zero |
| 40 | variable | Optional NULL bitmap followed by packed offsets |

If the NULL count is nonzero, the bitmap has `ceil(count/8)` bytes. Bit `i % 8`
of byte `i / 8` marks row i as NULL. Otherwise the bitmap is absent.
Offsets occupy exactly `ceil(count * width / 8)` bytes, least-significant bits
first. Each non-NULL offset is `value - minimum` evaluated as unsigned int64;
reconstruction uses unsigned addition and interprets the resulting bits as
signed int64. The width is the bit length of `maximum - minimum`. This includes
the full signed range without overflowing signed arithmetic. NULL offsets are
zero. Unused tail bits are zero on write and ignored on read.

Constant blocks have width zero. All-NULL blocks have minimum = maximum = zero.
The aggregate accepts only SQL INTEGER and NULL, preserving input order; use
aggregate `ORDER BY` for deterministic ordinals. Empty input returns NULL, and
scanning NULL returns no rows. `ordinal` starts at 1; it is not a source row ID.

The builder caps its entry buffer at 64 MiB (4,194,304 entries on the tested
targets). Frames also enforce a 64 MiB ceiling and the connection length limit.
The reader verifies the checksum, length, width, every offset, actual extrema,
and actual NULL count. A checksum is accidental-corruption detection, not
authentication. Whole-block validation precedes pruning, so reading an excluded
block is not constant-time. The scan checks interruption every 1,024 rows.

`sqlnew_int_scan` pushes usable integer equality and range bounds into its
iterator. SQLite rechecks the predicates to retain SQL affinity semantics for
other bound types. NULL comparison bounds select no rows. Ascending ordinal
order can be delivered directly. Blocks do not carry an auxiliary index.

## SNV1 vector block

| Offset | Bytes | Meaning |
|---|---:|---|
| 0 | 4 | ASCII `SNV1` |
| 4 | 4 | Dimension, unsigned, 1..4,096 |
| 8 | 4 | Vector count, unsigned |
| 12 | 4 | Mode: 0 float32, 1 int8 |
| 16 | variable | Fixed-stride records |

A mode 0 record is an 8-byte signed row ID followed by `dimension` IEEE-754
binary32 coordinates. NaN and infinity are rejected. A mode 1 record contains
the ID, one finite nonnegative binary32 scale, and signed byte codes -127..127.
The code -128 is invalid. Decode uses `coordinate = code * scale` in float64.
The encoder selects `scale = max(abs(coordinate))/127`, with an underflow guard
for subnormal inputs, rounds to nearest with half values away from zero, and
clamps to the code range. All-zero vectors use scale zero.

The aggregate rejects mixed dimensions/modes and noninteger IDs. An empty
aggregate returns NULL. Row IDs need not be sorted or unique in a raw block;
the persistent index enforces global row-ID uniqueness through its primary key.
A valid explicitly encoded zero-record block is empty. Malformed length, mode,
dimension, scale, and scanned coordinates are rejected. Coordinates of rows
excluded by the filter are not decoded.

There is no separate checksum inside SNV1. Persistent index blocks are wrapped
in SNZ1, which supplies a checksum. Hand-crafted bare blocks must be treated
as untrusted inputs, but changes to finite values can remain structurally valid.

## Search semantics

Distances are squared L2 with float64 accumulation. Scalar and SIMD summation
order can differ by floating-point rounding; bit-identical scores across CPUs
are not promised. Equal computed distances are ordered by signed row ID.
The top-k heap retains at most k records, then sorts that bounded set.
An index query merges per-block candidate lists, current delta candidates,
and optional original-vector reranking under one database snapshot.

The block scan is exhaustive over eligible vectors in the supplied block.
For int8 mode, that is exhaustive over reconstructed quantized values, not over
the original vectors. Coarse-cluster probing and bounded candidate reranking
add further approximation. The Python `exact=True` path bypasses these losses
and scores originals. It can skip clusters using the conservative bounds in
[GEOMETRY.md](GEOMETRY.md); `geometric=False` forces an exhaustive original scan.

Excluded and included ID blobs contain ascending unique signed int64 values.
Membership is checked before heap selection. Empty inclusion selects nothing.
Omitted inclusion selects all otherwise eligible rows. The scan checks database
interruption at least every 256 rows; it does not run SQL progress callbacks
inside each distance kernel.
