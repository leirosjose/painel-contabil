"""Qualidade de dados na entrada: limpeza antes do raw.

Cada linha do arquivo de origem sai daqui por um de três caminhos:
aceita como veio, aceita depois de normalizada (formato brasileiro de data
ou valor, CNPJ com máscara, nome com espaços/caixa misturados) ou rejeitada
com o motivo. Nada é descartado em silêncio: a rejeição vai para
`ops.rejeitados` e os totais para `ops.qualidade_carga`.

A limpeza não muda regra de negócio: o que sai aceito tem exatamente o
contrato de `raw_schema.TIPOS`, e o pipeline segue igual a partir dali.
"""
import json
import re
from dataclasses import dataclass, field

import pandas as pd

from raw_schema import COLUNA_JANELA, DATA, INTEIRO, RAW_KEYS, TEXTO, TIPOS, VALOR

N_DATA_BR = "data em formato dd/mm/aaaa"
N_VALOR_BR = "valor com vírgula decimal"
N_CNPJ = "CNPJ com máscara ou espaços"
N_TEXTO = "nome com espaços ou caixa misturados"

M_OBRIGATORIA = "coluna obrigatória vazia"
M_DATA_INVALIDA = "data ilegível"
M_DATA_IMPLAUSIVEL = "data fora do intervalo plausível"
M_VALOR_INVALIDO = "valor ilegível"
M_VALOR_AMBIGUO = "valor ambíguo (ponto de milhar sem decimais?)"
M_INTEIRO_INVALIDO = "código ilegível"
M_EMPRESA = "empresa inexistente no cadastro"
M_PARCELA_ORFA = "parcela sem nota de serviço"
M_CONTA = "conta fora do plano de contas"

ANO_MIN, ANO_MAX = 1990, 2100
COLUNAS_NOME = frozenset({"nome_emp", "apel_emp", "nome_cta"})

_RE_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[ T]00:00:00)?")
_RE_BR = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
_RE_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_RE_MILHAR_SEM_DECIMAL = re.compile(r"-?\d{1,3}(?:\.\d{3})+")
_RE_NUM_BR = re.compile(r"-?(?:\d{1,3}(?:\.\d{3})+|\d+),\d+")
_RE_INT = re.compile(r"-?\d+(?:\.0+)?")


class _Rejeita(Exception):
    pass


def obrigatorias(tabela: str) -> list[str]:
    """Chave natural, empresa e a data da janela: sem elas a linha não tem lugar."""
    cols = [c for c in RAW_KEYS.get(tabela, []) if c in TIPOS[tabela]]
    for c in ("codi_emp", COLUNA_JANELA.get(tabela)):
        if c and c in TIPOS[tabela] and c not in cols:
            cols.append(c)
    return cols


@dataclass
class Resultado:
    tabela: str
    lidas: int
    aceitas: pd.DataFrame
    rejeitadas: pd.DataFrame
    normalizadas: dict[str, int] = field(default_factory=dict)
    duplicatas: int = 0
    completude: dict[str, float] = field(default_factory=dict)

    def rejeitadas_por_motivo(self) -> dict[str, int]:
        return self.rejeitadas["motivo"].value_counts().to_dict() if len(self.rejeitadas) else {}


def _vazio(v) -> bool:
    if isinstance(v, str):
        return not v.strip()
    return v is None or bool(pd.isna(v))


def _como_texto(v) -> str:
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _data(col, s, notas):
    s = s.strip()
    if m := _RE_ISO.fullmatch(s):
        ano, iso = int(m.group(1)), f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    elif m := _RE_BR.fullmatch(s):
        ano, iso = int(m.group(3)), f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        notas.append(N_DATA_BR)
    else:
        raise _Rejeita(f"{M_DATA_INVALIDA}: {col}")
    if not ANO_MIN <= ano <= ANO_MAX:
        raise _Rejeita(f"{M_DATA_IMPLAUSIVEL}: {col}")
    try:
        pd.Timestamp(iso)
    except ValueError:
        raise _Rejeita(f"{M_DATA_INVALIDA}: {col}") from None
    return iso


def _valor(col, s, notas):
    s = s.strip()
    if _RE_MILHAR_SEM_DECIMAL.fullmatch(s):
        raise _Rejeita(f"{M_VALOR_AMBIGUO}: {col}")
    if _RE_NUM.fullmatch(s):
        return s
    if _RE_NUM_BR.fullmatch(s):
        notas.append(N_VALOR_BR)
        return s.replace(".", "").replace(",", ".")
    raise _Rejeita(f"{M_VALOR_INVALIDO}: {col}")


def _inteiro(col, s, notas):
    s = s.strip()
    if _RE_INT.fullmatch(s):
        return s.split(".")[0]
    raise _Rejeita(f"{M_INTEIRO_INVALIDO}: {col}")


def _texto(col, s, notas):
    if col == "cgce_emp":
        digitos = re.sub(r"\D", "", s)
        if digitos != s:
            notas.append(N_CNPJ)
        return digitos
    if col in COLUNAS_NOME:
        limpo = " ".join(s.split()).upper()
        if limpo != s:
            notas.append(N_TEXTO)
        return limpo
    return s


_POR_TIPO = {DATA: _data, VALOR: _valor, INTEIRO: _inteiro, TEXTO: _texto}


def limpa(tabela: str, bruto: pd.DataFrame, empresas: set[int] | None = None,
          contas: set[tuple[int, int]] | None = None,
          notas_servico: set[tuple[int, int]] | None = None) -> Resultado:
    """Normaliza e valida as linhas de `bruto` (tudo como texto) contra o contrato.

    `empresas`, `contas` e `notas_servico` são os cadastros de referência para
    as checagens de consistência (None desliga a checagem).
    """
    tipos = {c: t for c, t in TIPOS[tabela].items() if c in bruto.columns}
    obrig = set(obrigatorias(tabela))
    aceitas, rejeitadas, notas_aceitas = [], [], {}
    for pos, linha in enumerate(bruto.to_dict("records")):
        notas: list[str] = []
        try:
            saida = {}
            for col, tipo in tipos.items():
                v = linha.get(col)
                if _vazio(v):
                    if col in obrig:
                        raise _Rejeita(f"{M_OBRIGATORIA}: {col}")
                    saida[col] = None
                    continue
                saida[col] = _POR_TIPO[tipo](col, _como_texto(v), notas)
            _checa_consistencia(tabela, saida, empresas, contas, notas_servico)
        except _Rejeita as r:
            rejeitadas.append({"linha": pos + 2, "motivo": str(r),
                               "conteudo": json.dumps({k: _como_texto(v) if not _vazio(v) else None
                                                       for k, v in linha.items()}, ensure_ascii=False)})
            continue
        aceitas.append(saida)
        for n in notas:
            notas_aceitas[n] = notas_aceitas.get(n, 0) + 1

    df = pd.DataFrame(aceitas, columns=list(tipos))
    antes = len(df)
    df = df.drop_duplicates().reset_index(drop=True)
    completude = {c: (float(df[c].notna().mean()) if len(df) else 0.0) for c in tipos}
    return Resultado(tabela=tabela, lidas=len(bruto), aceitas=df,
                     rejeitadas=pd.DataFrame(rejeitadas, columns=["linha", "motivo", "conteudo"]),
                     normalizadas=notas_aceitas, duplicatas=antes - len(df), completude=completude)


def _checa_consistencia(tabela, linha, empresas, contas, notas_servico):
    emp = int(linha["codi_emp"]) if linha.get("codi_emp") is not None else None
    if empresas is not None and tabela != "geempre" and emp is not None and emp not in empresas:
        raise _Rejeita(M_EMPRESA)
    if tabela == "ctlancto" and contas is not None:
        for col in ("cdeb_lan", "ccre_lan"):
            if linha.get(col) is not None and (emp, int(linha[col])) not in contas:
                raise _Rejeita(f"{M_CONTA}: {col}")
    if tabela == "efservicospar" and notas_servico is not None and linha.get("codi_ser") is not None:
        if (emp, int(linha["codi_ser"])) not in notas_servico:
            raise _Rejeita(M_PARCELA_ORFA)


DDL_QUALIDADE = """
CREATE TABLE IF NOT EXISTS ops.qualidade_carga (
    run_id                 BIGINT NOT NULL,
    tabela                 TEXT   NOT NULL,
    lidas                  INTEGER NOT NULL,
    aceitas                INTEGER NOT NULL,
    rejeitadas             INTEGER NOT NULL,
    duplicatas             INTEGER NOT NULL,
    normalizadas           JSONB  NOT NULL,
    rejeitadas_por_motivo  JSONB  NOT NULL,
    completude             JSONB  NOT NULL,
    gravado_em             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, tabela)
);
CREATE TABLE IF NOT EXISTS ops.rejeitados (
    run_id    BIGINT  NOT NULL,
    tabela    TEXT    NOT NULL,
    linha     INTEGER NOT NULL,
    motivo    TEXT    NOT NULL,
    conteudo  JSONB   NOT NULL,
    PRIMARY KEY (run_id, tabela, linha)
);
"""


def grava_relatorio(conn, run_id: int, relatorio: dict[str, Resultado]) -> None:
    """Grava o relatório de qualidade da carga e cada linha rejeitada no `ops`."""
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS ops")
        cur.execute(DDL_QUALIDADE)
        for tabela, r in sorted(relatorio.items()):
            cur.execute(
                "INSERT INTO ops.qualidade_carga (run_id, tabela, lidas, aceitas, rejeitadas, duplicatas, "
                "normalizadas, rejeitadas_por_motivo, completude) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (run_id, tabela, r.lidas, len(r.aceitas), len(r.rejeitadas), r.duplicatas,
                 json.dumps(r.normalizadas, ensure_ascii=False),
                 json.dumps(r.rejeitadas_por_motivo(), ensure_ascii=False),
                 json.dumps(r.completude)))
            for rej in r.rejeitadas.itertuples(index=False):
                cur.execute("INSERT INTO ops.rejeitados (run_id, tabela, linha, motivo, conteudo) "
                            "VALUES (%s,%s,%s,%s,%s)", (run_id, tabela, rej.linha, rej.motivo, rej.conteudo))
    conn.commit()
