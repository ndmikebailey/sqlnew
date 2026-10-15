#!/bin/sh
set -eu
# Prerequisites: Python 3.12+, C compiler, make and requirements-dev.txt.
# Run from any directory; no system packages are installed by this helper.
CDPATH= cd "$(dirname "$0")/.."
PYTHON=${PYTHON:-python3}
export SQLNEW_BUILD_DIR="${SQLNEW_BUILD_DIR:-$PWD/build}"
export SQLNEW_DISABLE_SIMD=0
"$PYTHON" sqlnew/build.py
"$PYTHON" -m unittest discover -s sqlnew -p 'test_*.py' -v
"$PYTHON" sqlnew/exercise_dev021.py
export SQLNEW_DISABLE_SIMD=1
"$PYTHON" sqlnew/build.py
"$PYTHON" -m unittest discover -s sqlnew -p 'test_*.py' -v
"$PYTHON" sqlnew/exercise_dev021.py
"$SQLNEW_BUILD_DIR/sqlnew/sqlnew" :memory: 'SELECT sqlnew_version(),sqlnew_backend();'
