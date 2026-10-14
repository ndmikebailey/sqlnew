# Archived exact-fold prototypes

These standalone experiments led to the native `fold.c` and `region.c` modules.
The active build does not use these Python implementations or packet formats.

- `scout.py`, `native.py`, `column.py`: codec exploration and native comparisons.
- `regions.py`: translated spatial sharing and sparse/ragged traversal.
- `overlays.py`: immutable bases with separate changes and pinned snapshots.
- `retrieval.py`: selective changes and bounded patch application.
- `*-results.json` and `results.json`: original measurements, including failures.

See the [single journal](../../exact_fold/ATTEMPTS.txt) for assumptions and
limits. Older scripts may require the original Windows DLL snapshots under
`build/math-exploration`, NumPy, or the original doc2sql source fixture. They are
retained for investigation, not advertised as fresh-clone product checks.
Canonical native folding contracts now live in `sqlnew/fold_contracts.py` and
run through `sqlnew/exercise_dev021.py` without these dependencies.
