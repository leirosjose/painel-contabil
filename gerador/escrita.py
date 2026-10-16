"""Escrita das tabelas geradas: um CSV (ou XLSX) por tabela."""
import json
from pathlib import Path


def escreve(tabelas: dict, pasta, formato: str = "csv", manifesto: dict | None = None) -> None:
    pasta = Path(pasta)
    pasta.mkdir(parents=True, exist_ok=True)
    for nome, df in sorted(tabelas.items()):
        if formato == "xlsx":
            df.to_excel(pasta / f"{nome}.xlsx", index=False)
        else:
            df.to_csv(pasta / f"{nome}.csv", index=False)
    if manifesto is not None:
        (pasta / "_sujeira.json").write_text(json.dumps(manifesto, indent=2, sort_keys=True) + "\n")
