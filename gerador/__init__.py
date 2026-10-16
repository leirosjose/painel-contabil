"""Gerador determinístico de dados sintéticos no formato das tabelas do Domínio.

`gera(seed)` devolve um DataFrame por tabela de origem, já no contrato de
`etl/raw_schema.py`, para cinco empresas fictícias (Simples, Presumido, Real
e uma filial) de dez/2023 a dez/2025. `suja` aplica, por cima disso, uma
sujeira determinística de taxa baixa para exercitar a limpeza na entrada.
"""
import sys
from pathlib import Path

from faker import Faker

_ETL = Path(__file__).resolve().parent.parent / "etl"
if str(_ETL) not in sys.path:
    sys.path.insert(0, str(_ETL))

from gerador.contabil import CONTA_BANCOS, gera_contabil  # noqa: E402
from gerador.empresas import EMPRESAS, MESES, cnpj_valido, gera_geempre  # noqa: E402
from gerador.fiscal import gera_fiscal  # noqa: E402
from gerador.folha import gera_folha  # noqa: E402

SEED = 20261019

__all__ = ["SEED", "EMPRESAS", "MESES", "CONTA_BANCOS", "cnpj_valido", "gera"]


def gera(seed: int = SEED) -> dict:
    """Todas as tabelas de origem (exceto `ctsaldo_inicial`, que a fonte deriva)."""
    fk = Faker("pt_BR")
    fk.seed_instance(seed)
    rnd = fk.random
    tabelas = {"geempre": gera_geempre()}
    fiscal = gera_fiscal(rnd)
    folha = gera_folha(rnd)
    tabelas.update(fiscal)
    tabelas.update(folha)
    tabelas.update(gera_contabil(rnd, fiscal, folha))
    return tabelas
