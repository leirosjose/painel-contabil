"""Fonte sintética: as tabelas do gerador determinístico, em memória, no layout do Domínio."""
import pandas as pd

from gerador import SEED, gera
from raw_schema import COLUNA_JANELA, GLOBAIS, TIPOS, aplica_contrato, inicio_do_mes, limites_do_mes


class FonteSintetica:
    """Entrega cada tabela no contrato de `raw_schema`, com a mesma janela que o
    banco do Domínio aplicaria (o extract real filtra no SQL; aqui, em pandas)."""

    def __init__(self, seed: int = SEED):
        self._tabelas = {t: aplica_contrato(df, t) for t, df in gera(seed).items()}

    def _tabela(self, tabela: str) -> pd.DataFrame:
        return self._tabelas[tabela].copy()

    def ler(self, tabela, empresa=None, inicio=None, fim=None):
        df = self._tabela(tabela)
        if tabela in GLOBAIS:
            return df
        if empresa is not None:
            df = df[df["codi_emp"] == int(empresa)]
        lower, upper = limites_do_mes(inicio, fim)
        col = COLUNA_JANELA[tabela]
        if col is not None:
            df = df[_na_janela(df[col], lower, upper)]
        elif tabela == "efservicospar":
            notas = self._tabela("efservicos")
            notas = notas[_na_janela(notas["dser_ser"], lower, upper)]
            df = df.merge(notas[["codi_emp", "codi_ser"]].drop_duplicates(), on=["codi_emp", "codi_ser"])
        elif tabela == "foempregados" and upper is not None:
            df = df[df["admissao"] < upper]
        elif tabela == "foeventos" and lower is not None and upper is not None:
            mov = self._tabela("fomovto")
            ativas = mov.loc[_na_janela(mov["data"], lower, upper), "codi_emp"].unique()
            df = df[df["codi_emp"].isin(ativas)]
        return df.reset_index(drop=True)

    def saldo_inicial(self, empresa, inicio):
        corte = inicio_do_mes(inicio)
        lan = self._tabela("ctlancto")
        lan = lan[lan["data_lan"] < corte]
        if empresa is not None:
            lan = lan[lan["codi_emp"] == int(empresa)]
        deb = (lan.groupby(["codi_emp", "cdeb_lan"], as_index=False)["vlor_lan"].sum()
               .rename(columns={"cdeb_lan": "codi_cta", "vlor_lan": "valo_deb"}))
        cre = (lan.groupby(["codi_emp", "ccre_lan"], as_index=False)["vlor_lan"].sum()
               .rename(columns={"ccre_lan": "codi_cta", "vlor_lan": "valo_cre"}))
        df = deb.merge(cre, on=["codi_emp", "codi_cta"], how="outer")
        df[["valo_deb", "valo_cre"]] = df[["valo_deb", "valo_cre"]].fillna(0.0)
        df["data_corte"] = corte
        return df[list(TIPOS["ctsaldo_inicial"])].reset_index(drop=True)

    def max_datas(self):
        out = {}
        for tabela, col in (("efsaidas", "dsai_sai"), ("efservicos", "dser_ser"), ("efentradas", "dent_ent")):
            datas = self._tabela(tabela)[col].dropna()
            out[tabela] = datas.max() if len(datas) else None
        return out


def _na_janela(datas: pd.Series, lower: str | None, upper: str | None) -> pd.Series:
    ok = datas.notna()
    if lower is not None:
        ok &= datas >= lower
    if upper is not None:
        ok &= datas < upper
    return ok
