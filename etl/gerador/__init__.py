"""Gerador determinístico de dados sintéticos no formato das tabelas do Domínio.

`gera(seed)` devolve um DataFrame por tabela de origem, com as colunas de
`raw_schema.TIPOS`, para cinco empresas fictícias (Simples, Presumido, Real
e uma filial) de dez/2023 a dez/2025.
"""
from faker import Faker

from gerador.contabil import CONTA_BANCOS, gera_contabil
from gerador.empresas import EMPRESAS, MESES, cnpj_valido, gera_geempre
from gerador.fiscal import gera_fiscal
from gerador.folha import gera_folha

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
