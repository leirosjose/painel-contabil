"""python -m gerador --saida dados [--seed N] [--formato csv|xlsx] [--sem-sujeira]"""
import argparse
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
for p in (RAIZ, RAIZ / "etl"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from gerador import SEED, gera  # noqa: E402
from gerador.escrita import escreve  # noqa: E402
from gerador.sujeira import suja  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Gera os CSV/XLSX sintéticos de origem")
    ap.add_argument("--saida", default="dados", help="pasta de saída (default: ./dados)")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--formato", choices=["csv", "xlsx"], default="csv")
    ap.add_argument("--sem-sujeira", action="store_true", help="não injeta sujeira de qualidade")
    args = ap.parse_args()
    tabelas = gera(args.seed)
    manifesto = None
    if not args.sem_sujeira:
        tabelas, manifesto = suja(tabelas, args.seed)
    escreve(tabelas, args.saida, args.formato, manifesto)
    total = sum(len(df) for df in tabelas.values())
    print(f"{len(tabelas)} tabelas, {total} linhas -> {args.saida}/ (seed {args.seed})")


if __name__ == "__main__":
    main()
