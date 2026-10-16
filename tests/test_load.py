# ---------------------------------------------------------------------------
# Testes de caracterização de etl/load.py.
# _to_py é o funil único DataFrame -> psycopg2 (pd.NA estourava o adapt).
# ---------------------------------------------------------------------------
import datetime
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

import load
from load import _to_py


class TestToPy:
    # -- ausências: tudo vira None --
    def test_pd_na_vira_none(self):
        assert _to_py(pd.NA) is None

    def test_nat_vira_none(self):
        # Antes escapava cru (branch NaT morto: isoformat capturava antes)
        # e o psycopg2 adaptava como datetime -> string 'NaT' no Postgres.
        assert _to_py(pd.NaT) is None

    def test_none_continua_none(self):
        assert _to_py(None) is None

    def test_nan_vira_none(self):
        assert _to_py(float("nan")) is None

    # -- caracterização do caminho feliz (congela comportamento atual) --
    def test_numpy_scalar_vira_nativo(self):
        out = _to_py(np.int64(7))
        assert out == 7 and type(out) is int

    def test_date_passa_direto(self):
        d = datetime.date(2026, 6, 11)
        assert _to_py(d) is d

    def test_timestamp_passa_direto(self):
        # datetime64 via itertuples chega como pd.Timestamp — input de data REAL
        # do raw load. Deve passar CRU (psycopg2 adapta), igual ao codigo velho
        # (branch isoformat -> return v). `is` discrimina passthrough de conversao.
        ts = pd.Timestamp("2026-06-11 10:30:00")
        assert _to_py(ts) is ts

    def test_string_passa_direto(self):
        assert _to_py("abc") == "abc"


class TestUpsertScopeDelete:
    # _upsert com scope_cols emite DELETE-por-pares ANTES do INSERT, na mesma
    # transacao (mock do execute_values; conn MagicMock; sem Postgres real).

    @staticmethod
    def _capture(monkeypatch):
        calls = []
        monkeypatch.setattr(
            load, "execute_values",
            lambda cur, sql, args, *a, **k: calls.append((sql, list(args))),
        )
        return calls

    def test_scope_emite_delete_antes_do_insert(self, monkeypatch):
        calls = self._capture(monkeypatch)
        df = pd.DataFrame({
            "codi_emp": [2, 2, 3],
            "competencia": ["2025-05", "2025-05", "2025-06"],
            "clas_cta": ["x", "y", "z"],
            "valo_saldo": [1.0, 2.0, 3.0],
        })
        load._upsert(MagicMock(), "mart", "balancete_mensal", df,
                     ["codi_emp", "competencia", "clas_cta"],
                     scope_cols=["codi_emp", "competencia"])
        assert len(calls) == 2                                   # DELETE, depois INSERT
        assert "DELETE FROM" in calls[0][0] and "IN (VALUES" in calls[0][0]
        assert calls[0][1] == [(2, "2025-05"), (3, "2025-06")]   # pares distintos do df
        assert all(type(p[0]) is int for p in calls[0][1])       # native int, nao numpy.int64
        assert "INSERT INTO" in calls[1][0]

    def test_sem_scope_nao_emite_delete(self, monkeypatch):      # guard path empresas/raw
        calls = self._capture(monkeypatch)
        df = pd.DataFrame({"codi_emp": [1], "nome": ["x"]})
        load._upsert(MagicMock(), "mart", "empresas", df, ["codi_emp"])
        assert len(calls) == 1 and "INSERT INTO" in calls[0][0]  # so INSERT, zero DELETE

    def test_dedup_balancete_grao_novo_e_noop(self, monkeypatch):
        # Com a chave real do mart (MART_KEYS), contas distintas
        # co-classificadas na mesma clas_cta NAO colapsam no dedup — as
        # duas linhas chegam ao INSERT.
        from main import MART_KEYS
        calls = self._capture(monkeypatch)
        df = pd.DataFrame({
            "codi_emp": [1, 1],
            "competencia": ["2025-01", "2025-01"],
            "clas_cta": ["401020129", "401020129"],
            "codi_cta": [275, 813],
            "nome_cta": ["Rescisao", "Servicos PJ"],
            "valo_saldo": [300.0, 200.0],
        })
        load._upsert(MagicMock(), "mart", "balancete_mensal", df,
                     MART_KEYS["balancete_mensal"],
                     scope_cols=["codi_emp", "competencia"])
        assert len(calls) == 2
        assert len(calls[1][1]) == 2   # INSERT leva as DUAS linhas


class TestEnsureTableTipos:
    # O CREATE TABLE do raw nasce do dtype do DataFrame (load.py::_pg_type, que
    # tem TEXT como default de dtype fora do mapa) e `CREATE TABLE IF NOT
    # EXISTS` congela esse tipo pra sempre — nao ha um unico ALTER TYPE no
    # codebase. Sintetico igual ao TestUpsertScopeDelete: MagicMock de conexao,
    # zero Postgres.

    @staticmethod
    def _capture(monkeypatch, colunas_existentes=None):
        """Devolve a lista de SQL que upsert_raw passa a cur.execute.

        colunas_existentes alimenta o fetchall do _add_missing_columns — a
        lista cheia silencia o ALTER; uma lista parcial exercita o 2o call
        site do _pg_type.
        """
        sqls: list[str] = []
        cur = MagicMock()
        cur.execute.side_effect = lambda sql, *a, **k: sqls.append(sql)
        cur.fetchall.return_value = [(c,) for c in (colunas_existentes or [])]
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        monkeypatch.setattr(load, "connect_postgres", lambda: conn)
        monkeypatch.setattr(load, "execute_values", lambda *a, **k: None)
        return sqls

    def test_df_vazio_nao_cria_tabela(self, monkeypatch):
        # Extract que volta vazio NAO pode criar a tabela: df vazio tem todas
        # as colunas em object, entao o CREATE sairia com TUDO TEXT e o
        # IF NOT EXISTS congelaria isso.
        sqls = self._capture(monkeypatch)
        vazio = pd.DataFrame(columns=["codi_emp", "nume_lan", "vlor_lan"])
        n = load.upsert_raw("ctlancto", vazio, ["codi_emp", "nume_lan"])
        criados = [s for s in sqls if "CREATE TABLE" in s]
        assert n == 0
        assert not criados, f"df vazio criou tabela: {criados}"

    def test_codi_emp_declarado_bigint_mesmo_vindo_str(self, monkeypatch):
        # codi_emp e o EIXO de todo join do projeto: o tipo dele nao pode
        # depender do lote que criou a tabela. Vindo como str da origem, a
        # coluna ainda tem que nascer BIGINT (o psycopg2 manda str como
        # literal unknown, que o Postgres coage: '201' = 201::bigint -> t).
        # As demais colunas sao REGRESSAO-ZERO: seguem inferidas do dtype.
        cols = ["codi_emp", "i_guiainss", "competencia", "tipo_process", "empresa"]
        sqls = self._capture(monkeypatch, colunas_existentes=cols)
        df = pd.DataFrame({
            "codi_emp":     ["201", "201"],
            "i_guiainss":   ["a1b2", "c3d4"],
            "competencia":  ["2025-06", "2025-07"],
            "tipo_process": [11, 11],
            "empresa":      [1000.0, 2000.0],
        })
        load.upsert_raw("foguiainss", df, ["codi_emp", "i_guiainss"])
        create = next(s for s in sqls if "CREATE TABLE" in s)
        assert '"codi_emp" BIGINT' in create, create
        assert '"i_guiainss" TEXT' in create, create              # GUID: TEXT certo
        assert '"competencia" TEXT' in create, create             # data: fora de escopo
        assert '"tipo_process" BIGINT' in create, create          # int64 inferido
        assert '"empresa" DOUBLE PRECISION' in create, create     # float64 inferido

    def test_codi_emp_declarado_no_add_column(self, monkeypatch):
        # 2o call site do _pg_type (_add_missing_columns): coluna adicionada
        # DEPOIS, num schema que evoluiu, tem que nascer BIGINT tambem --
        # senao o fix cobre so metade da superficie.
        sqls = self._capture(monkeypatch, colunas_existentes=["codi_sai"])
        df = pd.DataFrame({"codi_emp": ["201"], "codi_sai": [1]})
        load.upsert_raw("efsaidas", df, ["codi_emp", "codi_sai"])
        add = [s for s in sqls if "ADD COLUMN" in s and "codi_emp" in s]
        assert add and "BIGINT" in add[0], add


# ── (d) data_corte na chave do saldo inicial ────────────────────────────────

class TestChaveDoSaldoInicial:
    """Sem `data_corte` na chave, o ON CONFLICT sobrescreve a geracao
    canonica IN PLACE.

    O extract de uma janela incremental (corte = watermark) produz um
    SUPERSET dos pares (codi_emp, codi_cta) do corte canonico, entao o
    upsert varre a tabela inteira. Com `data_corte` na
    chave as geracoes CONVIVEM e o filtro que ja existe no `run_transform`
    escolhe a certa por run.
    """

    def test_chave_do_si_inclui_data_corte(self):
        from main import RAW_KEYS
        assert RAW_KEYS["ctsaldo_inicial"] == [
            "codi_emp", "codi_cta", "data_corte"]

    def test_upsert_emite_on_conflict_das_3_colunas(self):
        df = pd.DataFrame({"codi_emp": [1], "codi_cta": [10],
                           "valo_deb": [1.0], "valo_cre": [0.0],
                           "data_corte": ["2023-01-01"]})
        conn = MagicMock()
        with patch("load.execute_values") as ev:
            load._upsert(conn, "raw", "ctsaldo_inicial", df,
                         ["codi_emp", "codi_cta", "data_corte"])
        sql = ev.call_args_list[-1][0][1]
        assert 'ON CONFLICT ("codi_emp", "codi_cta", "data_corte")' in sql

    def test_geracoes_de_cortes_diferentes_nao_colapsam_no_dedup(self):
        # O _upsert faz drop_duplicates(subset=key_cols): com a chave de 2
        # colunas as duas geracoes do MESMO par virariam uma so, ANTES
        # mesmo do banco.
        df = pd.DataFrame({"codi_emp": [1, 1], "codi_cta": [10, 10],
                           "valo_deb": [1.0, 2.0], "valo_cre": [0.0, 0.0],
                           "data_corte": ["2023-01-01", "2026-01-01"]})
        conn = MagicMock()
        with patch("load.execute_values") as ev:
            load._upsert(conn, "raw", "ctsaldo_inicial", df,
                         ["codi_emp", "codi_cta", "data_corte"])
        rows = ev.call_args_list[-1][0][2]
        assert len(rows) == 2


# ── expurgo das geracoes nao-canonicas do saldo inicial ─────────────────────

class TestExpurgaGeracoesSi:
    """O canonico NUNCA pode entrar no escopo do DELETE.

    O expurgo existe porque, com `data_corte` na chave, as geracoes param
    de se sobrescrever e o raw cresce ~15 MB por janela distinta. Ele e o
    unico DELETE em `raw` do pipeline, e o modo de falha e apagar
    exatamente o que o fix veio preservar.
    """

    @staticmethod
    def _conn(apagados=(), nulos=0):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = [(c,) for c in apagados]
        cur.fetchone.return_value = (nulos,)
        return conn, cur

    def test_expurgo_tambem_limpa_o_mapa_da_fatia(self):
        conn, cur = self._conn(apagados=["2024-01-01"])
        with patch("load.connect_postgres", return_value=conn):
            load.expurga_geracoes_si(["2023-01-01", "2026-01-01"])
        sql, params = cur.execute.call_args_list[-1][0]
        assert "ops.raw_slice_vigente" in sql and params[0] == [20230101, 20260101]

    def test_delete_exclui_todos_os_cortes_mantidos(self):
        conn, cur = self._conn(apagados=["2024-01-01"])
        with patch("load.connect_postgres", return_value=conn):
            out = load.expurga_geracoes_si(["2023-01-01", "2026-01-01"])
        sql, params = cur.execute.call_args_list[0][0]
        assert "DELETE FROM" in sql and "ctsaldo_inicial" in sql
        assert "data_corte <> ALL(%s)" in sql
        assert params[0] == ["2023-01-01", "2026-01-01"]
        assert out["linhas"] == 1 and out["cortes"] == ["2024-01-01"]

    def test_nao_toca_linha_com_corte_NULL(self):
        # `NOT IN`/`<> ALL` nunca casa NULL, e o guard explicito documenta:
        # o que fazer com o lixo legado e decisao da migracao.
        conn, cur = self._conn(nulos=7)
        with patch("load.connect_postgres", return_value=conn):
            out = load.expurga_geracoes_si(["2023-01-01"])
        sql = cur.execute.call_args_list[0][0][0]
        assert "data_corte IS NOT NULL" in sql
        assert out["nulos"] == 7

    def test_lista_vazia_e_erro_nao_delete_geral(self):
        conn, _ = self._conn()
        with patch("load.connect_postgres", return_value=conn):
            with pytest.raises(AssertionError):
                load.expurga_geracoes_si([])

    def test_run_canonico_mantem_um_corte_so_e_ele_e_o_canonico(self):
        # Quando o run E o canonico, `manter` colapsa em 1 item — e esse
        # item nao pode ser outro senao o canonico.
        from config import CORTE_CANONICO
        from raw_schema import inicio_do_mes
        manter = sorted({CORTE_CANONICO, inicio_do_mes("2023-01")})
        assert manter == [CORTE_CANONICO]
