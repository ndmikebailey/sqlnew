# Geometric pruning of original vectors

The exact-search path can avoid scoring clusters whose original vectors cannot
beat the current top-k threshold. This is implemented in `index.py`; it does
not certify recall for quantized or limited-probe approximate searches.

## Bound in real arithmetic

For a cluster center c, sealing stores a radius R that bounds the Euclidean
distance from c to every original vector x in that cluster. The triangle
inequality gives:

```
||q - x|| >= max(0, ||q - c|| - R) = L
```

Search visits clusters in increasing L. Once k eligible originals have been
scored, let T be the largest distance in that result set. A remaining cluster
can be skipped only when its lower bound is strictly greater than T. Equality
must be scanned because a smaller signed row ID can win a distance tie.
Original vectors, rather than reconstructed int8 codes, define both the radius
and the exact ranking.

## Floating-point enclosure

Supported inputs have 1..4,096 finite binary32 coordinates. Scoring subtracts,
squares, and accumulates in binary64 without fast-math. For these dimensions and
coordinate magnitudes, nonzero squared differences and sums stay within the
normal binary64 range. The implementation assumes ordinary IEEE-754 rounding
and standard square-root behavior; changing the process floating-point mode is
outside this contract.

For unit roundoff u, a chain of n rounded operations has the usual conservative
relative-error factor `gamma(n) = n*u / (1 - n*u)`. Nonnegative squared-distance
terms do not introduce cancellation in summation. Scalar and AVX2 paths have
different summation orders, so exact bit agreement is not assumed.

The implemented guard is `g = 64 * dimension * eps64`, with `eps64 = 2*u`.
This deliberately exceeds the rounding budget of subtraction, multiplication,
summation, square root, and the final bound operations at the supported sizes.
Sealing and search use these outward adjustments:

```
R = nextafter(sqrt(max_computed_squared_radius) * (1 + g), +infinity)
L = max(0, nextafter(sqrt(computed_center_distance) * (1 - g) - R, -infinity))
T = nextafter(sqrt(kth_computed_squared_distance) * (1 + g), +infinity)
```

Pruning uses only `L > T`. The extra slack also protects comparisons between
distances accumulated in different orders. This is an engineering enclosure
under the stated arithmetic assumptions, not a machine-checked proof covering
every compiler or floating-point environment. Tests compare this path with an
exhaustive scan over original vectors, including very small and large inputs,
near ties, filters, and edits. `search(exact=True, geometric=False)` provides the
exhaustive path when the pruning assumptions are unsuitable.

## Transactional validity

Centers, radii, membership, and code blocks are published by one sealing
transaction. Search reads one database snapshot. Inserts and edits enter a
transactional delta; the exact path scores their current originals before
using cluster bounds. Delta IDs are excluded from sealed memberships. Deletions
cannot enlarge a radius. Rebuilding replaces all membership and radii together.

The contract assumes tables are maintained through the index API and its
triggers. Manually altering radius or membership metadata can invalidate it.

## Limits that remain

Broad or overlapping clusters can yield zero lower bounds and no pruning.
High dimension can make this common. Search reports clusters visited and
pruned, and makes no sublinear worst-case promise. Sealing still materializes
the dataset and holds a writer transaction. Native streaming construction and
page-level bounds remain future work.
