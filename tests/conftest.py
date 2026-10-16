import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
for p in (RAIZ, RAIZ / "etl"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
