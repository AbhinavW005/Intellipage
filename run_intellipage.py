import multiprocessing
import sys
from pathlib import Path


# Make the source launcher self-contained. This keeps local runs consistent
# with the packaged executable and avoids requiring users to set PYTHONPATH.
_project_root = Path(__file__).resolve().parent
for _import_root in (_project_root / "src", _project_root / "work" / "pydeps"):
    if _import_root.exists() and str(_import_root) not in sys.path:
        sys.path.insert(0, str(_import_root))

from intellipage.cli import main


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
