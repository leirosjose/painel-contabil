"""Relatório de qualidade gravado no ops e lido de volta por SQL."""
import pandas as pd
import pytest

import qualidade as Q
from connections import connect_postgres

pytestmark = pytest.mark.db


def test_relatorio_e_rejeitados_vao_para_o_ops(banco_scratch):
    bruto = pd.DataFrame([
        {"codi_emp": "101", "codi_sai": "1", "dsai_sai": "31/01/2024", "vcon_sai": "10",
         "codi_nat": "5102", "codi_acu": "1"},
        {"codi_emp": "999", "codi_sai": "2", "dsai_sai": "2024-01-31", "vcon_sai": "10",
         "codi_nat": "5102", "codi_acu": "1"},
    ], dtype=object)
    r = Q.limpa("efsaidas", bruto, empresas={101})
    conn = connect_postgres()
    Q.grava_relatorio(conn, 7, {"efsaidas": r})
    with conn.cursor() as cur:
        cur.execute("SELECT lidas, aceitas, rejeitadas, normalizadas->>%s, "
                    "rejeitadas_por_motivo->>%s FROM ops.qualidade_carga WHERE run_id = 7",
                    (Q.N_DATA_BR, Q.M_EMPRESA))
        assert cur.fetchone() == (2, 1, 1, "1", "1")
        cur.execute("SELECT linha, motivo, conteudo->>'codi_emp' FROM ops.rejeitados WHERE run_id = 7")
        assert cur.fetchall() == [(3, Q.M_EMPRESA, "999")]
    conn.close()
