# Experiments

The native implementation selected from these experiments is in `ext/sqlnew/`.
Current reproducible workflows are `sqlnew/exercise_dev021.py` and
`sqlnew/benchmark_dev021.py`; they do not import archived prototypes.

| Location | Purpose |
|---|---|
| [Attempt journal](exact_fold/ATTEMPTS.txt) | Single chronological record of hypotheses, failures, corrections and decisions |
| [Exact-fold archive](archive/exact_fold/) | Python codec/region/overlay/retrieval prototypes and original measurements |
| [Lattice-warp archive](archive/lattice_warp/README.md) | Earlier reversible-warp experiment and optional local viewer |
| [Early design archive](archive/early-sqlnew/) | Superseded architecture and validation documents |
| [Consumer experiment](archive/doc2sql/README.md) | Status of the optional paused doc2sql integration |
| [Native results](../sqlnew/results/README.md) | Recorded product validation and benchmark evidence |

Archives retain evidence rather than define requirements. Recorded paths and
hashes describe their original runs; path corrections for the archive do not
retroactively change those results. Generated corpora, binaries and databases
remain in ignored `build/` or an experiment's ignored output directory.
