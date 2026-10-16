"""Página "Qualidade dos dados".

Mostra o relatório da última carga gravado pelo ETL em `ops.qualidade_carga`
e `ops.rejeitados`: o que foi lido, aceito, normalizado e rejeitado (com o
motivo) por tabela, e as colunas que chegaram incompletas. Somente leitura.
"""
import os
import sys

import pandas as pd
import psycopg2
import streamlit as st
from dotenv import load_dotenv

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _RAIZ not in sys.path:
    sys.path.insert(0, _RAIZ)
load_dotenv(os.path.join(_RAIZ, ".env"))

FRASE_SEM_CARGA = "Nenhuma carga com relatório de qualidade ainda."

_SQL_EXISTE = "SELECT to_regclass('ops.qualidade_carga') IS NOT NULL"

_SQL_ULTIMA = """
    SELECT q.run_id, max(r.iniciado_em) AS iniciado_em, max(r.status) AS status
      FROM ops.qualidade_carga q
      LEFT JOIN ops.controle_runs r ON r.run_id = q.run_id
     WHERE q.run_id = (SELECT max(run_id) FROM ops.qualidade_carga)
     GROUP BY q.run_id
"""

_SQL_TABELAS = """
    SELECT q.tabela, q.lidas, q.aceitas, q.rejeitadas, q.duplicatas,
           COALESCE((SELECT sum(v::int) FROM jsonb_each_text(q.normalizadas) AS n(k, v)), 0)
               AS normalizadas,
           round(100.0 * q.aceitas / NULLIF(q.lidas, 0), 1) AS pct_aceitas
      FROM ops.qualidade_carga q
     WHERE q.run_id = %s
     ORDER BY q.rejeitadas DESC, q.tabela
"""

_SQL_MOTIVOS = """
    SELECT r.tabela, r.motivo, count(*) AS linhas, min(r.linha) AS primeira_linha
      FROM ops.rejeitados r
     WHERE r.run_id = %s
     GROUP BY r.tabela, r.motivo
     ORDER BY linhas DESC, r.tabela, r.motivo
"""

_SQL_NORMALIZACOES = """
    SELECT q.tabela, n.tipo, n.qtd::int AS celulas
      FROM ops.qualidade_carga q
     CROSS JOIN LATERAL jsonb_each_text(q.normalizadas) AS n(tipo, qtd)
     WHERE q.run_id = %s
     ORDER BY celulas DESC, q.tabela
"""

_SQL_INCOMPLETAS = """
    SELECT q.tabela, c.coluna, round(c.pct::numeric, 4)::float AS completude
      FROM ops.qualidade_carga q
     CROSS JOIN LATERAL jsonb_each_text(q.completude) AS c(coluna, pct)
     WHERE q.run_id = %s AND q.aceitas > 0 AND c.pct::numeric < 1
     ORDER BY completude, q.tabela, c.coluna
"""

_SQL_AMOSTRA = """
    SELECT tabela, linha, motivo, conteudo::text AS conteudo
      FROM ops.rejeitados
     WHERE run_id = %s
     ORDER BY tabela, linha
     LIMIT 200
"""


def _get_conn():
    return psycopg2.connect(
        host=os.getenv("PG_HOST", "localhost"),
        port=int(os.getenv("PG_PORT", 5432)),
        dbname=os.getenv("PG_DB", "painel_bi"),
        user=os.getenv("PG_USER", "postgres"),
        password=os.getenv("PG_PASS", ""),
    )


def _df(cur, sql, params):
    cur.execute(sql, params)
    return pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description])


def carrega_relatorio():
    """Relatório da última carga, ou None se nenhuma carga gravou relatório ainda."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SQL_EXISTE)
            if not cur.fetchone()[0]:
                return None
            cur.execute("SELECT to_regclass('ops.controle_runs') IS NOT NULL")
            sql_ultima = _SQL_ULTIMA if cur.fetchone()[0] else _SQL_ULTIMA.replace(
                "LEFT JOIN ops.controle_runs r ON r.run_id = q.run_id",
                "LEFT JOIN (SELECT NULL::bigint AS run_id, NULL::timestamp AS iniciado_em, "
                "NULL::text AS status) r ON false")
            cur.execute(sql_ultima)
            ultima = cur.fetchone()
            if ultima is None:
                return None
            run_id = ultima[0]
            return {
                "run_id": run_id, "iniciado_em": ultima[1], "status": ultima[2],
                "tabelas": _df(cur, _SQL_TABELAS, (run_id,)),
                "motivos": _df(cur, _SQL_MOTIVOS, (run_id,)),
                "normalizacoes": _df(cur, _SQL_NORMALIZACOES, (run_id,)),
                "incompletas": _df(cur, _SQL_INCOMPLETAS, (run_id,)),
                "amostra": _df(cur, _SQL_AMOSTRA, (run_id,)),
            }


st.set_page_config(page_title="Qualidade dos dados", layout="wide", page_icon="📊")
st.page_link("app.py", label="Painel", icon="⬅️")
st.markdown("### Qualidade dos dados")

_rel = carrega_relatorio()
if _rel is None:
    st.info(FRASE_SEM_CARGA)
else:
    _t = _rel["tabelas"]
    st.caption(f"Carga #{_rel['run_id']}"
               + (f" · {_rel['iniciado_em']:%d/%m/%Y %H:%M}" if _rel["iniciado_em"] else ""))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Linhas lidas", f"{int(_t['lidas'].sum()):,}".replace(",", "."))
    c2.metric("Normalizadas", f"{int(_t['normalizadas'].sum()):,}".replace(",", "."))
    c3.metric("Duplicatas removidas", f"{int(_t['duplicatas'].sum()):,}".replace(",", "."))
    c4.metric("Rejeitadas", f"{int(_t['rejeitadas'].sum()):,}".replace(",", "."))
    st.markdown("#### Por tabela")
    st.dataframe(_t, hide_index=True, width="stretch")
    st.markdown("#### Rejeitadas por motivo")
    st.dataframe(_rel["motivos"], hide_index=True, width="stretch")
    st.markdown("#### Normalizações aplicadas")
    st.dataframe(_rel["normalizacoes"], hide_index=True, width="stretch")
    st.markdown("#### Colunas incompletas (entre as linhas aceitas)")
    st.dataframe(_rel["incompletas"], hide_index=True, width="stretch")
    with st.expander("Linhas rejeitadas (até 200)"):
        st.dataframe(_rel["amostra"], hide_index=True, width="stretch")
