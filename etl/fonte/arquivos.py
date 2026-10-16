"""Fonte de arquivos: um CSV ou XLSX por tabela, no formato das tabelas do Domínio."""
from pathlib import Path

import pandas as pd

import qualidade as Q
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
    """Lê `<pasta>/<tabela>.csv` (ou `.xlsx`), limpa (`qualidade.limpa`) e
    entrega o contrato de `raw_schema`. O resultado da limpeza de cada tabela
    fica em `relatorio[tabela]`."""

    def __init__(self, pasta: str | Path):
        self.pasta = Path(pasta)
        self.relatorio: dict[str, Q.Resultado] = {}
        self._cache: dict[str, pd.DataFrame] = {}

    def _bruto(self, tabela: str) -> pd.DataFrame:
        csv, xlsx = self.pasta / f"{tabela}.csv", self.pasta / f"{tabela}.xlsx"
        if csv.exists():
            return pd.read_csv(csv, dtype=str, keep_default_na=False, na_values=[""])
        if xlsx.exists():
            return pd.read_excel(xlsx)
        raise FileNotFoundError(f"{tabela}: nem {csv.name} nem {xlsx.name} em {self.pasta}")

    def _existe(self, tabela: str) -> bool:
        return any((self.pasta / f"{tabela}.{ext}").exists() for ext in ("csv", "xlsx"))

    def _referencia(self, tabela: str, colunas: list[str]) -> set | None:
        if not self._existe(tabela):
            return None
        df = self._tabela(tabela)
        return {tuple(int(v) for v in r) if len(r) > 1 else int(r[0])
                for r in df[colunas].itertuples(index=False)}

    def _tabela(self, tabela: str) -> pd.DataFrame:
        if tabela not in self._cache:
            ref = {}
            if tabela != "geempre" and "codi_emp" in TIPOS[tabela]:
                ref["empresas"] = self._referencia("geempre", ["codi_emp"])
            if tabela == "ctlancto":
                ref["contas"] = self._referencia("ctcontas", ["codi_emp", "codi_cta"])
            if tabela == "efservicospar":
                ref["notas_servico"] = self._referencia("efservicos", ["codi_emp", "codi_ser"])
            r = Q.limpa(tabela, self._bruto(tabela), **ref)
            self.relatorio[tabela] = r
            self._cache[tabela] = _normaliza(r.aceitas, tabela)
        return self._cache[tabela].copy()

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
