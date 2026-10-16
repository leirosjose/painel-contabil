# ---------------------------------------------------------------------------
# raw_escopo.py — FONTE UNICA do escopo de cada tabela do raw
# ---------------------------------------------------------------------------
"""Qual FATIA uma linha do raw ocupa e qual FATIA um run PEDIU a origem.

Um run COM extract e autoritativo so pro que PEDIU: a janela de datas de cada
tabela, a empresa (ou todas), a geracao do saldo inicial. A geracao nova de uma
tabela substitui a anterior DENTRO desse pedido, fatia a fatia -- nunca por
MAX(run_id). Espelha os filtros de extract.py.

Fatia e INTEIRO (4 bytes em ~10M linhas):
  AAAAMM    tabela com janela de datas (mes da coluna de data da linha)
  AAAAMMDD  saldo inicial (a data_corte)
  0         tabela ou empresa INTEIRA (sem data propria)
Esta e a fonte unica da regra -- nao escreve-la de novo em lugar nenhum.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

FATIA_TODA = 0
FATIA_SEM = -1          # linha sem fatia derivavel: so existe em geracao REJEITADA
EMPRESA_GLOBAL = 0      # dicionario global nao tem codi_emp: ocupa a empresa 0
IMPRESSAO_VAZIA = "0:0000000000000000:0000000000000000"


@dataclass(frozen=True)
class Escopo:
    tipo: str               # janela | ate_fim | via_nota | via_fomovto | inteira | global | corte
    coluna_data: str | None = None


ESCOPOS: dict[str, Escopo] = {
    "ctlancto": Escopo("janela", "data_lan"),
    "efsaidas": Escopo("janela", "dsai_sai"),
    "efentradas": Escopo("janela", "dent_ent"),
    "efservicos": Escopo("janela", "dser_ser"),
    "efservicospar": Escopo("via_nota"),
    "efsdoimp": Escopo("janela", "data_sim"),
    # recebimento de parcela de venda de imovel: mesma regra das
    # ef* com data propria — janela pelo MES de data_pagamento.
    "efimob_pagamentos": Escopo("janela", "data_pagamento"),
    "fobases": Escopo("janela", "competencia"),
    "fomovto": Escopo("janela", "data"),
    "foguiainss": Escopo("janela", "competencia"),
    "fofgtsfilial": Escopo("janela", "competencia"),
    "foguiagrfc": Escopo("janela", "vencimento"),
    "forescisoes": Escopo("janela", "demissao"),
    "hrfaturamento": Escopo("janela", "data_faturamento"),
    "foempregados": Escopo("ate_fim", "admissao"),
    "ctsaldo_inicial": Escopo("corte", "data_corte"),
    "geempre": Escopo("inteira"),
    "ctcontas": Escopo("inteira"),
    "foeventos": Escopo("via_fomovto"),
    "foeventosdef": Escopo("global"),
    "foclassificacoes_eventos": Escopo("global"),
}
GLOBAIS = frozenset(t for t, e in ESCOPOS.items() if e.tipo == "global")


def _na(index) -> pd.Series:
    return pd.Series(pd.NA, index=index, dtype="Int64")


def _mes(serie: pd.Series) -> pd.Series:
    s = serie.astype("string").str.slice(0, 7)
    ok = s.str.fullmatch(r"\d{4}-\d{2}").fillna(False).astype(bool)
    out = _na(serie.index)
    if ok.any():
        out.loc[ok] = (s[ok].str.slice(0, 4) + s[ok].str.slice(5, 7)).astype("int64").to_numpy()
    return out


def _dia(serie: pd.Series) -> pd.Series:
    s = serie.astype("string").str.slice(0, 10)
    ok = s.str.fullmatch(r"\d{4}-\d{2}-\d{2}").fillna(False).astype(bool)
    out = _na(serie.index)
    if ok.any():
        out.loc[ok] = s[ok].str.replace("-", "", regex=False).astype("int64").to_numpy()
    return out


def fatias(tabela: str, df: pd.DataFrame, notas: pd.DataFrame | None = None) -> pd.Series:
    """Fatia de cada linha (Int64; NA = sem fatia derivavel)."""
    esc = ESCOPOS[tabela]
    if df.empty:
        return _na(df.index)
    if esc.tipo in ("janela", "ate_fim"):
        return _mes(df[esc.coluna_data])
    if esc.tipo == "corte":
        return _dia(df[esc.coluna_data])
    if esc.tipo == "via_nota":
        if notas is None or notas.empty:
            return _na(df.index)
        mapa = notas[["codi_emp", "codi_ser", "dser_ser"]].drop_duplicates(["codi_emp", "codi_ser"])
        m = df[["codi_emp", "codi_ser"]].merge(mapa, on=["codi_emp", "codi_ser"], how="left")
        out = _mes(m["dser_ser"])
        out.index = df.index
        return out
    return pd.Series(FATIA_TODA, index=df.index, dtype="Int64")


def empresas(tabela: str, df: pd.DataFrame) -> pd.Series:
    """codi_emp de cada linha (int64); o dicionario global ocupa a empresa 0."""
    if tabela in GLOBAIS or "codi_emp" not in df.columns:
        return pd.Series(EMPRESA_GLOBAL, index=df.index, dtype="int64")
    return pd.to_numeric(df["codi_emp"]).astype("int64")


@dataclass(frozen=True)
class Pedido:
    """O que um run pediu a origem pra UMA tabela."""
    tabela: str
    codi_emp: int | None                 # None = todas as empresas
    fatia_min: int | None                # inclusivo; None = sem limite
    fatia_max: int | None                # inclusivo; None = sem limite
    empresas: frozenset | None = None    # via_fomovto: so estas empresas

    def cobre(self, codi_emp: int, fatia: int) -> bool:
        if self.codi_emp is not None and codi_emp != self.codi_emp:
            return False
        if self.empresas is not None and codi_emp not in self.empresas:
            return False
        if self.fatia_min is not None and fatia < self.fatia_min:
            return False
        if self.fatia_max is not None and fatia > self.fatia_max:
            return False
        return True


def _ym(periodo: str) -> int:
    return int(periodo[:4]) * 100 + int(periodo[5:7])


def pedido(tabela: str, empresa, inicio: str, fim: str, empresas_fomovto=None) -> Pedido:
    esc = ESCOPOS[tabela]
    if esc.tipo == "global":
        return Pedido(tabela, None, FATIA_TODA, FATIA_TODA)
    emp = None if empresa is None else int(empresa)
    if esc.tipo in ("janela", "via_nota"):
        return Pedido(tabela, emp, _ym(inicio), _ym(fim))
    if esc.tipo == "ate_fim":
        return Pedido(tabela, emp, None, _ym(fim))
    if esc.tipo == "corte":
        d = _ym(inicio) * 100 + 1
        return Pedido(tabela, emp, d, d)
    if esc.tipo == "via_fomovto":
        return Pedido(tabela, emp, FATIA_TODA, FATIA_TODA,
                      frozenset(int(e) for e in (empresas_fomovto or ())))
    return Pedido(tabela, emp, FATIA_TODA, FATIA_TODA)


def impressoes(tabela: str, df: pd.DataFrame, fat: pd.Series) -> pd.DataFrame:
    """Uma linha por fatia: codi_emp, fatia, linhas, impressao.

    Impressao = contagem + soma e XOR (modulo 2^64) dos hashes das linhas:
    independe da ORDEM e muda com qualquer valor, tipo ou coluna. So serve pra
    reconhecer fatia IDENTICA a vigente e nao regrava-la. `fat` nao
    pode ter NA -- o chamador separa as linhas sem fatia antes.
    """
    saida = ["codi_emp", "fatia", "linhas", "impressao"]
    if df.empty:
        return pd.DataFrame(columns=saida)
    cols = sorted(c for c in df.columns if not str(c).startswith("_"))
    h = pd.util.hash_pandas_object(df[cols], index=False).to_numpy(dtype=np.uint64)
    emp = empresas(tabela, df).to_numpy(dtype=np.int64)
    fa = fat.astype("int64").to_numpy()
    ordem = np.lexsort((fa, emp))
    emp, fa, h = emp[ordem], fa[ordem], h[ordem]
    corte = np.r_[True, (emp[1:] != emp[:-1]) | (fa[1:] != fa[:-1])]
    ini = np.flatnonzero(corte)
    soma = np.add.reduceat(h, ini)
    xor = np.bitwise_xor.reduceat(h, ini)
    linhas = np.diff(np.r_[ini, len(h)])
    return pd.DataFrame({
        "codi_emp": emp[ini], "fatia": fa[ini], "linhas": linhas,
        "impressao": [f"{int(n)}:{int(s):016x}:{int(x):016x}" for n, s, x in zip(linhas, soma, xor)],
    })
