# Inherited engine source

`sqlite/` contains the pinned SQLite source used by SQLNEW, including the small
SQLNEW SQL-compiler additions. The directory is kept internally intact so its
generators, build tools and development tests can run with an explicit source
root. SQLNEW modules are in [`../ext/sqlnew/`](../ext/sqlnew/).

Build the product with `python sqlnew/build.py` from the repository root. The
generic configure/make targets here build the inherited core, without SQLNEW
module registration. See the [product guide](../sqlnew/README.md),
[contributor guide](../CONTRIBUTING.md) and [source pin](../sqlnew/upstream.json).

The engine files were relocated without changing their contents. Historical
reports retain their original root-relative source paths and hashes.
