"""Pagina "Alteracoes retroativas".

Lista os numeros de meses FECHADOS que mudaram no Dominio depois de ja terem
sido exibidos. Le SO `ops.alteracoes_retroativas` (escrita pelo ETL) e
`raw.geempre` pro nome. Read-only, sem filtro e sem widget alem do link de volta.

So entra a causa 'mudou no Dominio'; a linha 'Total' fica gravada na tabela
mas FORA da pagina (e derivada, repete NFe/NFS-e/Outras receitas).
Ordem: detectado_em DESC, competencia DESC.
"""
import os
import sys

import pandas as pd
import psycopg2
import streamlit as st
from dotenv import load_dotenv

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _RAIZ not in sys.path:                     # `etl` importavel de qualquer ponto de entrada
    sys.path.insert(0, _RAIZ)
load_dotenv(os.path.join(_RAIZ, ".env"))

from etl.metrics import LINHA_FOLHA_ENCARGOS          # noqa: E402
from etl.utils import fmt_brl, p_label                # noqa: E402

CAUSA_PAGINA = "mudou no Dominio"
FRASE_SEM_ALTERACAO = "Nenhuma alteração registrada ainda."
RODAPE_ANO_CALENDARIO = ("Valores calculados por ano-calendário; o painel com outra "
                         "janela pode mostrar outro valor no mesmo mês.")
# Rotulo de tela por `numero` gravado. Impostos e Folha sao R$ (numerador), nao o
# % do card; o nome da folha vem da constante do metrics (um nome so).
ROTULO_NUMERO = {
    "Impostos": "Impostos (R$)",
    "Folha": f"{LINHA_FOLHA_ENCARGOS} (R$)",
}
COLUNAS = ["Atualização", "Empresa", "Número", "Mês/Ano", "Antes", "Depois"]

_SQL_EXISTE = "SELECT to_regclass('ops.alteracoes_retroativas') IS NOT NULL"
_SQL_LISTA = """
    SELECT to_char(a.detectado_em AT TIME ZONE 'America/Sao_Paulo', 'DD/MM/YY') AS atualizacao,
           a.codi_emp, g.nome_emp, a.numero, a.competencia,
           a.valor_antes, a.valor_depois
      FROM ops.alteracoes_retroativas a
      LEFT JOIN raw.geempre g ON g.codi_emp = a.codi_emp
     WHERE a.causa = %s AND a.numero <> 'Total'
     ORDER BY a.detectado_em DESC, a.competencia DESC, a.codi_emp, a.numero
"""


def _get_conn():
    return psycopg2.connect(
        host=os.getenv("PG_HOST", "localhost"),
        port=int(os.getenv("PG_PORT", 5432)),
        dbname=os.getenv("PG_DB", "painel_bi"),
        user=os.getenv("PG_USER", "postgres"),
        password=os.getenv("PG_PASS", ""),
    )


def _num(v):
    """Decimal/None -> float/NaN; `fmt_brl` devolve "—" pro NaN (par ausente no mart)."""
    return float("nan") if v is None else float(v)


def carrega_alteracoes():
    """DataFrame formatado pra tela, ou None se a tabela ainda nao existe
    (entre o pull e a 1a carga do ETL -- transitorio esperado, nunca excecao)."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SQL_EXISTE)
            if not cur.fetchone()[0]:
                return None
            cur.execute(_SQL_LISTA, (CAUSA_PAGINA,))
            linhas = cur.fetchall()
    out = []
    for atualizacao, codi, nome, numero, comp, antes, depois in linhas:
        emp = f"{nome.rstrip()} ({codi})" if nome else str(codi)
        out.append({
            "Atualização": atualizacao,
            "Empresa": emp,
            "Número": ROTULO_NUMERO.get(numero, numero),
            "Mês/Ano": p_label(comp),
            "Antes": fmt_brl(_num(antes)),
            "Depois": fmt_brl(_num(depois)),
        })
    return pd.DataFrame(out, columns=COLUNAS)


st.set_page_config(page_title="Alterações retroativas", layout="wide", page_icon="📊")
st.page_link("app.py", label="Painel", icon="⬅️")
st.markdown("### Alterações retroativas")

_df = carrega_alteracoes()
if _df is None or _df.empty:
    st.info(FRASE_SEM_ALTERACAO)
else:
    st.dataframe(_df, hide_index=True, width="stretch")
st.caption(RODAPE_ANO_CALENDARIO)
