"""Run the isolated local doc2sql checkout using the SQLNEW embedded engine."""
from pathlib import Path
import os
import runpy
import sys

from python_runtime import activate


def main():
    activate()
    checkout = Path(os.environ.get("SQLNEW_DOC2SQL_CHECKOUT",
                    str(Path(__file__).resolve().parents[1] / "build/doc2sql"))).resolve()
    if not (checkout / "src/doc2sql").is_dir():
        raise SystemExit("doc2sql checkout is missing at " + str(checkout))
    sys.path.insert(0, str(checkout / "src"))
    if len(sys.argv) > 1 and sys.argv[1] == "--pytest":
        import pytest
        raise SystemExit(pytest.main(sys.argv[2:]))
    if len(sys.argv) > 2 and sys.argv[1] == "--script":
        script = sys.argv[2]
        sys.argv = sys.argv[2:]
        runpy.run_path(script, run_name="__main__")
        return
    sys.argv[0] = "doc2sql-sqlnew"
    runpy.run_module("doc2sql", run_name="__main__")


if __name__ == "__main__":
    main()
