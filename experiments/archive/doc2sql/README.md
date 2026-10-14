# Paused consumer integration

An earlier isolated doc2sql experiment explored captured evidence packets and
snapshot-consistent logic-context reads. It is separate from the native engine
and its default build, and is not required to reproduce SQLNEW benchmarks.

The consumer-specific patch is preserved locally at
`build/archive/private-consumers/doc2sql.patch`, outside the source distribution.
The current single-process Windows bridge remains in `sqlnew/python_runtime.py`
and `sqlnew/run_doc2sql.py`; `SQLNEW_DOC2SQL_CHECKOUT` selects a checkout explicitly.
The consumer repository was not modified by this preparation pass. Worker
subprocess bootstrap and automatic schema routing remain unimplemented.

Original experiment details and outcomes remain in the
[single journal](../../exact_fold/ATTEMPTS.txt). Local binary/data archives are
intentionally not prerequisites for the native build or current benchmark.
