"""Empresas fictícias, calendário e utilidades comuns do gerador."""
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Empresa:
    codi: int
    nome: str
    raiz_cnpj: str
    filial: str
    regime: str          # simples | presumido | real
    perfil: str          # comercio | servicos | industria | imobiliaria
    receita_base: float  # faturamento mensal médio
    empregados: int

    @property
    def cnpj(self) -> str:
        return completa_cnpj(self.raiz_cnpj + self.filial)


EMPRESAS = (
    Empresa(101, "ALFA FICTÍCIA COMÉRCIO DE ALIMENTOS LTDA", "89039653", "0001", "simples", "comercio", 180_000, 3),
    Empresa(102, "BETA FICTÍCIA SERVIÇOS TÉCNICOS LTDA", "76352993", "0001", "presumido", "servicos", 120_000, 4),
    Empresa(103, "GAMA FICTÍCIA INDÚSTRIA DE EMBALAGENS LTDA", "91634207", "0001", "real", "industria", 900_000, 6),
    Empresa(104, "DELTA FICTÍCIA EMPREENDIMENTOS IMOBILIÁRIOS LTDA", "98110021", "0001", "presumido", "imobiliaria",
            250_000, 2),
    Empresa(105, "GAMA FICTÍCIA INDÚSTRIA DE EMBALAGENS LTDA", "91634207", "0002", "real", "industria", 300_000, 3),
)

MESES = tuple(f"{a}-{m:02d}" for a in (2023, 2024, 2025) for m in range(1, 13)
              if "2023-12" <= f"{a}-{m:02d}" <= "2025-12")
DATA_ABERTURA = "2023-11-30"


def _dv(base: str) -> str:
    pesos = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    r = sum(int(d) * p for d, p in zip(base, pesos[-len(base):])) % 11
    return "0" if r < 2 else str(11 - r)


def completa_cnpj(doze: str) -> str:
    """12 primeiros dígitos -> CNPJ de 14 dígitos com os dois DVs."""
    treze = doze + _dv(doze)
    return treze + _dv(treze)


def cnpj_valido(cnpj: str) -> bool:
    return len(cnpj) == 14 and cnpj.isdigit() and completa_cnpj(cnpj[:12]) == cnpj


def fator(rnd, i_mes: int) -> float:
    """Sazonalidade leve + crescimento de ~1% ao mês + ruído."""
    sazonal = (1.0, 0.92, 0.97, 1.0, 1.03, 1.0, 0.98, 1.02, 1.04, 1.06, 1.08, 1.15)[i_mes % 12]
    return sazonal * (1 + 0.01 * i_mes) * rnd.uniform(0.94, 1.06)


def dia(rnd, mes: str) -> str:
    return f"{mes}-{rnd.randint(1, 28):02d}"


def r2(x: float) -> float:
    return round(float(x), 2)


def gera_geempre() -> pd.DataFrame:
    return pd.DataFrame([{"codi_emp": e.codi, "nome_emp": e.nome, "cgce_emp": e.cnpj, "stat_emp": "A"}
                         for e in EMPRESAS])
