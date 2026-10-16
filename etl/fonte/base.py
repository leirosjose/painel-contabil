"""Interface da fonte de dados do extract."""
from typing import Protocol

import pandas as pd


class Fonte(Protocol):
    """De onde o pipeline lê as tabelas de origem.

    Toda implementação entrega o contrato de `raw_schema.TIPOS` e aplica a
    mesma janela que o banco do Domínio aplicaria. A escolha da janela de
    cada tabela (ex.: um mês a mais para trás) é do `main.run_extract`.
    """

    def ler(self, tabela: str, empresa: int | None = None,
            inicio: str | None = None, fim: str | None = None) -> pd.DataFrame: ...

    def saldo_inicial(self, empresa: int | None, inicio: str) -> pd.DataFrame: ...

    def max_datas(self) -> dict[str, str | None]: ...
