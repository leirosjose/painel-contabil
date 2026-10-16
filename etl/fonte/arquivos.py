"""Fonte de arquivos: um CSV ou XLSX por tabela, no formato das tabelas do Domínio."""
from pathlib import Path

import pandas as pd

from raw_schema import (COLUNA_JANELA, DATA, GLOBAIS, INTEIRO, TEXTO, TIPOS, VALOR,
                            inicio_do_mes, limites_do_mes)


def _normaliza(df: pd.DataFrame, tabela: str) -> pd.DataFrame:
    tipos = TIPOS[tabela]
    faltando = [c for c in tipos if c not in df.columns]
    if faltando:
        raise ValueError(f"{tabela}: coluna(s) do contrato ausente(s): {faltando}")
    out = pd.DataFrame(index=df.index)
    for col, tipo in tipos.items():
        s = df[col]
        if tipo == DATA:
            out[col] = pd.to_datetime(s, errors="raise").dt.strftime("%Y-%m-%d")
            out[col] = out[col].astype(object).where(s.notna(), None)
        elif tipo == VALOR:
            out[col] = pd.to_numeric(s, errors="raise").astype("float64")
        elif tipo == INTEIRO:
            n = pd.to_numeric(s, errors="raise")
            out[col] = n.astype("int64") if n.notna().all() else n.astype("float64")
        elif tipo == TEXTO:
            out[col] = s.astype(object).where(s.notna(), None)
            out[col] = out[col].map(lambda v: v if v is None else str(v))
    return out.reset_index(drop=True)


class FonteArquivos:
    """Lê `<pasta>/<tabela>.csv` (ou `.xlsx`) e entrega o contrato de `raw_schema`."""

    def __init__(self, pasta: str | Path):
        self.pasta = Path(pasta)

    def _bruto(self, tabela: str) -> pd.DataFrame:
        csv, xlsx = self.pasta / f"{tabela}.csv", self.pasta / f"{tabela}.xlsx"
        if csv.exists():
            return pd.read_csv(csv, dtype=str, keep_default_na=False, na_values=[""])
        if xlsx.exists():
            return pd.read_excel(xlsx)
        raise FileNotFoundError(f"{tabela}: nem {csv.name} nem {xlsx.name} em {self.pasta}")

    def _tabela(self, tabela: str) -> pd.DataFrame:
        return _normaliza(self._bruto(tabela), tabela)

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
