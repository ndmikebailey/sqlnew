#!/bin/sh
set -eu
# Disposable Debian build environment. No database server is installed or run.
apt-get update -qq
apt-get install -y -qq --no-install-recommends build-essential python3 python3-numpy ca-certificates tcl-dev
python3 sqlnew/build.py
python3 -m unittest discover -s sqlnew -p 'test_*.py' -v
export SQLNEW_CHECK_TIMEOUT=300
python3 sqlnew/check_native.py
export SQLNEW_DISABLE_SIMD=1
python3 sqlnew/build.py
python3 -m unittest discover -s sqlnew -p 'test_*.py' -v
"$SQLNEW_BUILD_DIR/sqlnew/sqlnew" :memory: 'SELECT sqlnew_version(),sqlnew_backend();'
