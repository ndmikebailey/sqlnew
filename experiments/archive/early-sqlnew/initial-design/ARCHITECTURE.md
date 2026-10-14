> Archived experiment notes, superseded by subsequent implementation. These are not implementation requirements.

# SQLNEW: spend fewer bytes, then fewer instructions

Status: proposed architecture, September 5, 2026. All size, latency, and
throughput examples are calculations or hypotheses. None are benchmark results.

## 1. The central decision

Build a new storage engine beneath SQLite's SQL front end. Its unit of durable
publication is an immutable transaction manifest. Its unit of storage is a
typed, independently decodable block. Its unit of bulk execution is a batch of
values and a selection mask. Its unit of search is a set of candidate row IDs
with an explicit exactness contract.

Keep recently changed rows in a small row-oriented delta. Seal colder rows into
column groups and compact search structures. A query combines both under one
snapshot. This is an explicit choice to pay for background consolidation and
version tracking in exchange for cheaper bulk reads and denser storage.

Retain SQLite's parser, SQL expression semantics, connection API where useful,
and VFS boundary. Introduce a new file signature. Do not pretend a new format
can be understood by ordinary SQLite. Migration support is not a requirement.

The constraints are physical:

- A byte not fetched saves bandwidth and often avoids decoding work.
- A dependent memory access cannot always be hidden by adding arithmetic units.
- Compression consumes compute and can increase random-read latency.
- Indexes save reads by spending storage, write work, and maintenance work.
- Approximate retrieval saves work by permitting missed neighbors.
- Durable commits must eventually wait for the storage durability boundary.

There is no universal maximum across these objectives. SQLNEW should expose
the tradeoffs and select representations by workload, while keeping SQL
correctness and transaction durability out of that tradeoff.

## 2. What the imported source actually tells us

The current tree's [sqliteLimit.h](../../../../src/sqliteLimit.h) defines a default page
size of 4,096 bytes and a maximum of 65,536 bytes. Its comments explain that
16-bit offsets within a page impose the upper limit. These are existing format
and implementation choices, not evidence that one size is optimal here.

[wal.c](../../../../src/wal.c) describes full revised pages appended as WAL frames and
subsequently transferred to the database by checkpointing. Its
`sqlite3WalBeginWriteTransaction()` is the write-transaction entry point.
[btree.c](../../../../src/btree.c) contains cell construction, page balancing, and row
insertion. [vdbe.c](../../../../src/vdbe.c) has the instruction execution loop and the
`OP_Column`, `OP_Next`, `OP_MakeRecord`, and `OP_IdxInsert` paths.

These identify places where representation and execution can change. They do
not establish that those paths are bottlenecks for an unspecified workload.

## 3. Storage: separate mutation, decoding, and transfer units

A single page size should not have to serve every purpose.

| Concern | Proposed starting point | Reason and cost |
|---|---|---|
| Metadata and point access | Small directory nodes, initially 4 KiB | Bounded reads; more nodes than a large-page directory |
| Compression | Independently decodable blocks, initially 16–64 KiB decoded | Local redundancy without decoding a whole extent |
| Sequential allocation | Variable-length extents, coalesced into larger writes | Amortizes I/O submission without padding every small block |
| Bulk execution | Adaptive batches, initially 256–2,048 rows | Amortizes dispatch; wide projections need smaller batches |
| Mutation | Transaction records containing changed fields | Avoids recompressing a large cold block for one update |
| Visibility | Manifest generation plus stable row identity | One snapshot across row, text, and vector structures |

The numbers are initial design parameters. They are not tuned to this machine.
A 64 KiB decode block can contain many rows; a point lookup must reach a restart
group within it or choose a different representation for a hot column group.

Each table has stable 64-bit row identities, a versioned row directory, an
active delta, sealed segments, and tombstone/version overlays. Segment-local
row ordinals are compact addresses, not globally stable identities. Compaction
can change ordinals, so every index generation must carry the corresponding
mapping.

Column groups are preferable to blindly separating every column. Frequently
co-read small fields can share a group. Large payloads and vectors live apart
from frequently filtered metadata. A point-read-heavy table may remain mostly
row oriented. A scan-heavy table may seal quickly into narrow groups.

Do not store two complete copies of every table by default. Retain a primary
representation and selectively materialized projections. Otherwise a storage
optimization becomes an unexplained duplication policy.

### Compression should remove structure first

Choose a codec per block, accounting for headers and restart tables:

| Data pattern | Candidate representation |
|---|---|
| Constant or all-null values | One value or null count |
| Small integer range | Base value plus fixed-width packed offsets |
| Sorted IDs or timestamps | Deltas, optionally second-order deltas |
| Repeated strings | Block dictionary plus packed IDs |
| Long shared string prefixes | Prefix coding with bounded restart groups |
| Dense booleans, nulls, visibility | Bitmaps |
| Mostly repetitive arbitrary bytes | Bounded-window byte compression |
| High-entropy or very small payload | Raw bytes |
| Embeddings | Explicitly lossy numeric encoding; originals optional |

Selection uses total stored bytes and the anticipated access path. Try a small
bounded set of candidates at sealing time. Store the codec and version in the
block header. Do not put codec selection in the read hot path.

Compression must preserve SQL values, integer boundaries, text bytes, and null
distinctions. Lossy vector encodings require their own type and policy; they
must never be silently applied to ordinary numeric columns.

Avoid decompressing merely to answer a predicate. Packed integers can be
compared in batches; dictionary equality can use codes when the dictionary's
collation contract supports it; min/max can skip an entire group. Locale-aware
ordering cannot be replaced by byte ordering. A dictionary ID is not inherently
an order-preserving sort key.

### The compression threshold

Let `U` be decoded bytes, `r = U / stored_bytes`, `B` storage bandwidth, and `D`
decoder throughput measured in decoded bytes per second. Ignoring fixed costs,
a serial read-and-decode path costs:

```
raw:         U / B
compressed:  U / (r B) + U / D
```

Compression reduces this cost only if `D > B r / (r - 1)`, for `r > 1`.
If transfer and decoding overlap perfectly, the cost instead approaches the
maximum of the two stages. Neither model includes cache interference, output
materialization, contention, or the codec's fixed setup cost.

This explains why a codec useful for storage reads may lose on already resident
data. Retaining decoded hot blocks and compressed cold blocks is a policy
decision, not an automatic win. Both caches need a shared memory budget.

For random point reads, include `I/O latency + fetched bytes/B + decoded bytes/D`.
A 16-byte answer from a 64 KiB block may be a poor deal. Restart groups, grouped
columns, and hot-row storage address this; increasing page size alone does not.

## 4. Execution: make one decision for many values

Introduce batch operators for scans, typed comparisons, boolean masks,
projection, aggregation, and top-k. Keep the scalar VDBE for control flow,
small requests, and expressions that cannot use a batch kernel. Crossing the
boundary should happen per batch, with a defined value layout, null bitmap,
ownership rule, and cancellation point.

The first fused path should be:

```
read block metadata
  -> reject impossible blocks
  -> decode required filter columns
  -> apply predicates into a selection mask
  -> score or aggregate selected values
  -> materialize final output columns
```

This delays string creation, row assembly, and large-payload reads. It also
reduces the number of interpreter decisions per surviving result. Joins can
later use batched key probes or partitioned hashing, but they are not a reason
to delay the simpler scan path.

Do not vectorize observable behavior away. SQL null logic, overflow rules,
collation, short-circuit requirements where applicable, errors, and functions
with side effects need explicit handling. Floating-point reductions may change
when reassociated; exact SQL execution cannot silently enable fast-math.

Prepare specialized kernels once when types and codecs are known. Dispatch
using runtime CPU and OS capability checks, with a portable scalar fallback.
For x86, AVX instructions require OS register-state support as well as CPU
feature bits. Never compile the entire binary for the development machine's
widest instruction set. ARM kernels can implement the same internal contract.

Batch size follows the working set: `rows × live bytes per row`, plus masks and
kernel state. Increasing lanes or threads beyond memory bandwidth can simply
move the bottleneck. Parallelize independent block ranges, produce local
aggregates or top-k lists, and merge bounded results. Put a common resource
budget around query workers and compaction workers.

## 5. Indexing: buy only the selectivity you use

Use one internal candidate-set interface across index kinds. It yields row
identities or segment-local ordinals, a snapshot, ordering metadata, and an
exactness designation. Represent the candidate set as a sorted list when
sparse and a bitmap when dense. Choose by the actual encoded cost.

For a universe of `N` rows, a flat bitmap costs `N/8` bytes. A plain list of
32-bit local ordinals costs `4k` bytes; ignoring headers, it is smaller when
`k < N/32`. Delta coding shifts this crossover. A compact representation can
be slower to intersect, so bytes alone are insufficient for the final choice.

Use a ladder of increasingly expensive structures:

1. Segment min/max, null counts, row counts, and encoding statistics.
2. Optional membership filters for equality-heavy negative probes. Include
   inserts in the delta path; deleting bits unsafely can create false negatives.
3. Compressed sorted key-to-row lists for ordered access and range scans.
4. Bitmaps/postings for repeated values and text terms.
5. Compact in-memory directories for active keys and segment routing.

The immutable sorted index is the first general durable index. Prefix-code
keys with restart points and encode row IDs in blocks. Search the restart
directory, then decode one small group. Keep top directory levels resident
when useful. Use relative offsets within an extent to reduce pointer width;
persist explicit byte order rather than native pointers.

Statistics must describe correlations and physical layout, not just counts.
A filter selecting 1% of rows scattered throughout every payload block can
still force almost every payload block to be read. The planner should estimate
blocks touched, decoding work, candidate materialization, and random-probe cost.

Maintain one real uniqueness mechanism independent of any approximate index.
Unique checks include committed sealed data and the transaction delta. Index
creation must either publish a complete snapshot-consistent generation or stay
invisible until it catches up. Every committed row must be represented by a
published index or a query-visible delta.

## 6. Semantic search: retrieval is a storage problem too

Accept externally produced embeddings. This engine does not need an embedding
API or model runtime to implement semantic retrieval. Store dimension, metric,
normalization policy, embedding-model identity, and model revision as index
metadata. Mixing models or dimensions is an error. Changing a model creates a
new index generation.

### Establish the exact path first

Store vectors in contiguous blocks and maintain per-block row identity maps.
Read the query once, scan vectors in batches, and keep a bounded top-k heap or
equivalent selection buffer. Do not construct a SQL value and invoke a scalar
function once for every coordinate or every candidate if a bulk operator can
do that work.

Float32 originals define one exactness contract. Exactness here means exhaustive
search over the stored values using the documented scoring and tie rule; it
does not imply real-number arithmetic. Squared L2 avoids square roots for
ranking. For unit vectors, maximizing dot product and minimizing squared L2
are equivalent by `||q-x||² = 2 - 2(q·x)`. Validate normalization and define
zero-vector, NaN, infinity, overflow, and signed-zero behavior.

At `N = 1,000,000` and `d = 768`, raw float32 coordinates occupy 3.072 GB in
decimal units. A hypothetical 30 GB/s scan has a 102.4 ms coordinate-transfer
floor. That is not a latency prediction: it excludes IDs, scoring, top-k,
cache effects, and contention. More arithmetic throughput cannot remove this
particular full-scan byte requirement.

### Compress candidate scoring

Symmetric int8 quantization can store one scale and `d` signed codes per vector.
With 8 bytes of identity and 4 bytes of scale, 768-dimensional records take
780 bytes versus 3,080 bytes for float32 plus identity: about 3.95 times smaller.
That saving applies to the search representation. If originals are retained,
the total vector footprint grows by approximately the added quantized index.

For nearest rounding with scale `s` and no clipping, each coordinate error is
at most `s/2`. Thus `||x-x_hat||₂ <= sqrt(d) s/2`. For a unit query `q`, the
dot-product error is at most that norm by Cauchy–Schwarz. This bound can be
loose; it does not establish recall. Quantizing the query introduces additional
error terms and needs a separate bound.

If an approximate dot score is `a` and its certified error is `e`, the true
score lies in `[a-e, a+e]`. A candidate can be safely rejected only if its upper
bound is below a valid lower bound on the current kth-best score, with careful
tie handling and conservative floating-point rounding. When bounds overlap,
read original values. This gives a possible exact compressed screening path,
although loose bounds may eliminate very little work.

Binary signatures and shorter codes can be an additional coarse screen. They
are not semantic truth, and Hamming order is not generally the same as original
embedding distance order.

### Add locality before adding a pointer graph

Partition vectors into coarse clusters and store each cluster as contiguous
code blocks. A query chooses clusters, scores their codes, and optionally
reranks candidates using originals. Train and publish the centroids and codes
together; record the training seed and sample definition. New rows enter a
searchable delta immediately. Retraining publishes a complete new generation.

With balanced clusters, scanning `p` of `C` clusters visits approximately
`N p/C` vectors. The full model also includes centroid scoring, skew, filter
selectivity, code decoding, and original-vector reads. This fraction is a work
estimate, never a recall guarantee. Hierarchical centroid routing may become
necessary when scoring all centroids is itself expensive.

Store a maximum residual radius per cluster if exact geometric pruning is
desired. By the triangle inequality, `max(0, ||q-c|| - radius)` lower-bounds L2
distance to vectors covered by that radius. A certified bound must include
quantization and rounding error and all visible inserts. High-dimensional
clusters may have such large radii that the bound is ineffective.

### Filtering and freshness are part of search correctness

Apply mandatory filters to the candidate universe before final top-k. Choosing
the global top-k and then discarding unauthorized or nonmatching rows can
return fewer than k and miss the best permitted results.

Use exact scans for small filtered sets. Use eligible-row masks inside larger
cluster scans. If an approximate search returns too few candidates, widen it
or return an explicit incompleteness indication; a fixed oversampling multiplier
cannot guarantee correctness for arbitrary selective filters.

Queries must merge the sealed index and visible delta, remove deleted or stale
versions, and deduplicate stable row IDs. Approximate mode can miss neighbors;
it must still honor transaction visibility and filters.

Expose three distinct modes: exhaustive exact, certified bound-based exact,
and budgeted approximate. Budgeted mode specifies candidate/probe/latency limits
and reports work performed. A latency cap can prevent returning k results;
the API must say so. Exact reranking of an approximate candidate set does not
make the whole search exact.

### Hybrid text and vectors

Use text postings and vector candidates under the same row identity and
snapshot. FTS5's tokenizer and existing text behavior provide useful starting
points in this source tree, but a shared compressed block format needs a new
storage integration.

For an initial hybrid query, combine separately ranked eligible candidates with
an explicit rank-fusion function, then fetch payloads. This avoids assuming
text and vector scores share a numerical scale. Rank fusion is a ranking policy
requiring relevance evaluation, not a derived optimum. Missing candidates in
either stream cannot be recovered by fusion alone.

## 7. Durability and space have to be designed together

Publish immutable data and index generations through the same commit manifest.
Readers pin a generation. A writer appends new data, records the manifest, makes
the transaction durable through the VFS, then publishes it to new readers.
Detailed invariants are in [STORAGE.md](STORAGE.md).

Start with one committing writer and concurrent readers. Parallelize encoding,
index preparation, and independent read work. Multiple concurrent committers
need conflict detection, unique-key arbitration, ordering, and recovery rules;
they are a separate design, not a switch to remove a mutex.

Small updates should append small logical changes. Sealing and compaction
eventually rewrite them. If each logical byte is first written once and then
rewritten `m` times at similar size, data write amplification is roughly `1+m`
before metadata, indexes, replication, padding, and retained versions. Removing
a page WAL does not make this cost disappear.

Long-lived readers pin old extents. Charge pinned bytes to a visible budget.
When storage is exhausted, delay new work or return a clear error; never recycle
an extent still needed by a live snapshot. Compaction needs temporary free
space and must have a bounded, resumable unit of work.

## 8. Ideas to defer or reject at the design stage

- **Compress every page transparently:** leaves row execution unchanged and
  can amplify point reads. Keep this only as a bounded alternate experiment.
- **Make every page huge:** couples point access to scan and compression units.
- **A universal graph index:** dependent pointer walks, link storage, and update
  maintenance deserve justification before displacing contiguous candidates.
- **GPU execution by default:** include transfer, queueing, residency, and
  end-to-end result costs. It is a future backend for sufficiently large or
  already resident workloads, not the baseline contract.
- **Lossy storage of ordinary SQL values:** reject. Loss is an explicit semantic
  vector option, not a database-wide compression trick.
- **Unlimited background optimization:** competes with foreground work and can
  consume all spare disk space. Every task receives a budget.
- **Multiwriter publication in the first format:** defer until visibility,
  reclamation, and crash recovery have executable correctness coverage.
- **A claim of maximum speed:** meaningless without workload, resources, and
  an answer-quality requirement. Design around those quantities instead.

The strongest initial bet is the combination: typed blocks reduce bytes,
candidate indexes avoid blocks, batches reduce work on the remaining values,
and late materialization avoids constructing rows that will be discarded.
