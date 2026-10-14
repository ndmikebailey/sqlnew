# Lattice warp experiment

Seed: 20260905. Each dataset is 32 x 32 x 32 uint8 cells.

| Dataset | Raw packet | Ordinary zlib | Best warp + zlib | Best A, B | Exact recovery |
|---|---:|---:|---:|---|---|
| Patterned | 32,779 B | 4,090 B | 350 B | 5, 3 | 32,768 cells |
| 10% noise | 32,779 B | 12,569 B | 8,023 B | 5, 3 | 32,768 cells |
| Random | 32,779 B | 32,795 B | 32,795 B | 0, 0 | 32,768 cells |

Every file size includes its header, dimensions, and any compressed shift map.
The generic Python decoder is shared, just as a zlib decoder is shared; its source size is not charged per file.

## What was actually tested

The synthetic patterned data contains curved, periodic bands by construction. A second dataset replaces about 10% of cells with random bytes. A third consists of independent pseudorandom bytes. The encoder receives only each completed array; it does not receive the generating profile or its bend amplitudes.

The search evaluates all 81 amplitude pairs from 0 through 8 and chooses the smallest complete packet. This is a finite search over a chosen warp family, not a globally optimal compressor. Results on the constructed example demonstrate this mechanism; they do not establish performance on arbitrary real data.

A warp shifts each vertical column by an integer number of cells. The stored array wraps at its top and bottom, making the operation a permutation. The picture is unwrapped so the deformation can be seen without a discontinuous seam. The shift map is stored explicitly, compressed, so the decoder needs neither the original data nor knowledge of how the map was generated.

All 81 candidate packets for every dataset were decoded and compared byte for byte. Unique cell IDs were separately warped and inverted for all maps; repeated values cannot hide swapped cells. Best, baseline, and raw packets were also written and independently decoded from disk.

Original grid adjacency (95,232 undirected edges) is recoverable from the restored cell indices and dimensions. It does not mean distances, angles, or geometric nearest neighbors remain unchanged. This is a kinematic deformation, not a mechanical bone or tension/compression simulation.

## Squeezing and precision

The squeeze control scales the displayed coordinates. The byte packets remain the same. A separate experiment rounds coordinates to a fixed grid spacing of 1/8 original cell width and counts distinct coordinate triples. Coincident triples are ambiguous if position alone must identify a cell; keeping separate cell IDs/values avoids that ambiguity but still requires storage. This measures coordinate collisions, not automatic corruption of the separately stored values.

Repeated-zlib lengths in results.json measure five additional compression passes. They are observations for this codec, not a general impossibility proof. Likewise, the precision experiment describes its fixed-grid model; it does not rule out other coordinate encodings.

A general lossless encoder must distinguish all possible inputs. There are 2^N strings of length N and only 2^N-1 strings of length less than N, even including the empty string. Therefore an injective encoder cannot shorten every N-bit input. This counting proof concerns arbitrary data; special growing families can have unbounded compression ratios. An experiment alone cannot establish or refute a universal compression theorem.

## Reproduce or extend

Run `python experiment.py --open` from the parent directory. Requires numpy and matplotlib. The standalone HTML needs no network. Change the seed with `--seed 123`, the cube size with `--size 24`, or add your own cubic uint8 NumPy array with `--input data.npy --size 32`. Array axes are z, y, x. Other types must be encoded deliberately; silently rounding them would change the experiment.

The `.lat` files can be recovered independently using:

```python
from pathlib import Path
import numpy as np
from experiment import decode
data = decode(Path("output/patterned.warped.lat").read_bytes())
np.save("recovered.npy", data)
```

Useful extensions include other reversible warp families, a held-out real dataset, other codecs, and explicitly different definitions of which relationships must survive.
