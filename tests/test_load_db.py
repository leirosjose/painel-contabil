# ---------------------------------------------------------------------------
# test_load_db.py -- prova REAL de upsert_mart/run_load_mart contra Postgres
# local (banco_scratch). tests/test_load.py mocka conn/execute_values e
# verifica a SQL gerada; aqui roda contra um banco de verdade, sempre por
# chamada direta de funcao Python (nunca CLI/`python etl/main.py`).
# ---------------------------------------------------------------------------
import pandas as pd
import pytest

import load as L
import main as M

pytestmark = pytest.mark.db


def _conn():
    import connections
    return connections.connect_postgres()


class TestUpsertMartAtualizaSemDeletar:
    """upsert_mart sem scope_cols: 2a chamada com uma chave repetida tem que
    ATUALIZAR o valor (ON CONFLICT DO UPDATE) sem apagar as chaves que nao
    vieram no 2o df."""

    def test_atualiza_a_chave_repetida_preserva_as_demais(self, banco_scratch):
        L.ensure_schemas()
        primeiro = pd.DataFrame({"codi_emp": [1, 2, 3], "nome": ["A", "B", "C"]})
        L.upsert_mart("empresas", primeiro, ["codi_emp"])
        n = L.upsert_mart("empresas", pd.DataFrame({"codi_emp": [2], "nome": ["B2"]}), ["codi_emp"])
        assert n == 1
        c = _conn()
        with c.cursor() as cur:
            cur.execute('SELECT codi_emp, nome FROM mart."empresas" ORDER BY codi_emp')
            rows = cur.fetchall()
        c.close()
        assert rows == [(1, "A"), (2, "B2"), (3, "C")]


class TestDeleteEscopadoPorParPresente:
    """upsert_mart COM scope_cols (via run_load_mart, MART_KEYS real de
    impostos_mensais): par (codi_emp, competencia) PRESENTE no 2o df apaga as
    linhas do par que nao vieram de novo -- o DELETE-escopado contra o
    banco de verdade."""

    def test_par_presente_apaga_a_linha_que_sumiu_do_df(self, banco_scratch):
        L.ensure_schemas()
        primeiro = {"impostos_mensais": pd.DataFrame({
            "codi_emp": [1, 1], "competencia": ["2026-01", "2026-01"],
            "codi_imp": [1, 2], "srec_sim": [10.0, 20.0],
        })}
        M.run_load_mart(primeiro)
        segundo = {"impostos_mensais": pd.DataFrame({
            "codi_emp": [1], "competencia": ["2026-01"],
            "codi_imp": [1], "srec_sim": [99.0],
        })}
        M.run_load_mart(segundo)
        c = _conn()
        with c.cursor() as cur:
            cur.execute('SELECT codi_imp, srec_sim FROM mart."impostos_mensais" '
                        'WHERE codi_emp = 1 AND competencia = %s ORDER BY codi_imp', ("2026-01",))
            rows = cur.fetchall()
        c.close()
        assert rows == [(1, 99.0)]   # codi_imp=2 nao veio no 2o df -> some (par PRESENTE)


class TestDfVazioNaoCriaTabela:
    """upsert_mart com DataFrame vazio nao pode criar a tabela no Postgres real
    -- df vazio tem toda coluna em `object`, e CREATE TABLE IF NOT EXISTS
    fossilizaria TEXT pra sempre."""

    def test_df_vazio_nao_cria_tabela(self, banco_scratch):
        L.ensure_schemas()
        vazio = pd.DataFrame(columns=["codi_emp", "competencia", "valor"])
        n = L.upsert_mart("load_qa_vazio", vazio, ["codi_emp", "competencia"])
        assert n == 0
        c = _conn()
        with c.cursor() as cur:
            cur.execute("SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema = 'mart' AND table_name = 'load_qa_vazio'")
            existe = cur.fetchone()
        c.close()
        assert existe is None


class TestCodiEmpBigintReal:
    """codi_emp tem que nascer BIGINT na tabela REAL mesmo vindo como texto da
    origem -- _TIPO_DECLARADO sobrepoe a inferencia por dtype (que daria TEXT
    pra uma coluna object)."""

    def test_codi_emp_nasce_bigint_mesmo_vindo_como_texto(self, banco_scratch):
        L.ensure_schemas()
        df = pd.DataFrame({"codi_emp": ["201", "256"], "nome": ["X", "Y"]})
        L.upsert_mart("load_qa_bigint", df, ["codi_emp"])
        c = _conn()
        with c.cursor() as cur:
            cur.execute("SELECT data_type FROM information_schema.columns "
                        "WHERE table_schema = 'mart' AND table_name = 'load_qa_bigint' "
                        "AND column_name = 'codi_emp'")
            tipo = cur.fetchone()[0]
        c.close()
        assert tipo == "bigint"
