"""Alteracoes retroativas -- prova REAL contra Postgres.

Banco scratch por teste (fixture banco_scratch): o painel_bi NUNCA e tocado.
"""
from datetime import datetime

import pandas as pd
import psycopg2
import pytest

import connections
import load as L
import main as M
import retroativas as R

pytestmark = pytest.mark.db


def _conn():
    return connections.connect_postgres()


def _rows(sql, params=None):
    c = _conn()
    try:
        with c.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        c.close()


def _exec(sql, params=None):
    c = _conn()
    try:
        with c.cursor() as cur:
            cur.execute(sql, params)
        c.commit()
    finally:
        c.close()


class TestDdlNaGuardaDeBoot:
    def test_ensure_schemas_cria_a_tabela_e_registra_a_versao_de_forma_idempotente(self, banco_scratch):
        L.ensure_schemas()
        L.ensure_schemas()                                   # idempotente
        assert _rows("SELECT to_regclass('ops.alteracoes_retroativas') IS NOT NULL")[0][0] is True
        assert _rows("SELECT count(*) FROM ops.schema_version WHERE versao = %s",
                     ("2026-09-24-alteracoes-retroativas",))[0][0] == 1
        cols = {r[0] for r in _rows("SELECT column_name FROM information_schema.columns "
                                    "WHERE table_schema='ops' AND table_name='alteracoes_retroativas'")}
        assert {"run_id", "detectado_em", "codi_emp", "competencia", "numero", "valor_antes", "valor_depois",
                "diferenca", "causa", "parte_dominio", "parte_carga_nova", "fontes", "janela_calculo",
                "git_commit", "git_commit_r0"} <= cols

    def test_check_de_numero_e_causa_e_unique_do_run(self, banco_scratch):
        L.ensure_schemas()
        base = {"run_id": 1, "codi_emp": 1, "competencia": "2025-01", "numero": "NFe", "valor_antes": None,
                "valor_depois": 10, "diferenca": 10, "causa": "carga nova", "parte_dominio": None,
                "parte_carga_nova": None, "fontes": "x", "janela_calculo": "2025-01..2025-12",
                "git_commit": None, "git_commit_r0": None}
        c = _conn()
        try:
            for ruim in ({"numero": "Lucro"}, {"causa": "sei la"}):
                with pytest.raises(psycopg2.errors.CheckViolation):
                    with c.cursor() as cur:
                        d = {**base, **ruim}
                        cur.execute("INSERT INTO ops.alteracoes_retroativas (run_id, codi_emp, competencia, "
                                    "numero, diferenca, causa, janela_calculo) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                                    (d["run_id"], d["codi_emp"], d["competencia"], d["numero"], d["diferenca"],
                                     d["causa"], d["janela_calculo"]))
                c.rollback()
        finally:
            c.close()

    def test_abre_run_ganha_a_coluna_git_commit_e_grava_o_commit(self, banco_scratch):
        _exec("CREATE SCHEMA IF NOT EXISTS ops")
        _exec("CREATE TABLE ops.controle_runs (run_id bigserial PRIMARY KEY, iniciado_em timestamptz, "
              "terminado_em timestamptz DEFAULT now(), empresa text, modo text, inicio text, fim text, "
              "fim_declarado text, watermark text, status text, linhas_raw bigint, linhas_mart bigint)")
        _exec("INSERT INTO ops.controle_runs (empresa, modo, status) VALUES ('all','completo','ok')")
        rep = {"args": {"empresa": "all", "inicio": "2025-01", "fim": "2025-12", "modo": "completo"}}
        rid = M._abre_run(rep, datetime(2026, 9, 24, 10, 0))
        M._abre_run(dict(rep), datetime(2026, 9, 24, 11, 0))                 # ALTER idempotente
        antigo, novo = _rows("SELECT git_commit FROM ops.controle_runs ORDER BY run_id LIMIT 2")
        assert antigo[0] is None                                             # run antigo segue NULL
        esperado = R.git_commit_curto(R.RAIZ)
        assert novo[0] == esperado and rep["git_commit"] == esperado
        assert _rows("SELECT git_commit FROM ops.controle_runs WHERE run_id = %s", (rid,))[0][0] == esperado


def _linha(**kw):
    base = {"run_id": 5, "codi_emp": 1, "competencia": "2025-03", "numero": "NFe", "valor_antes": None,
            "valor_depois": 100, "diferenca": 100, "causa": "carga nova", "parte_dominio": None,
            "parte_carga_nova": None, "fontes": "x", "janela_calculo": "2025-01..2025-12",
            "git_commit": "abc", "git_commit_r0": None}
    base.update(kw)
    return base


class TestAppendOnly:
    def test_mesma_chave_duas_vezes_nao_regrava_nem_duplica(self, banco_scratch):
        L.ensure_schemas()
        c = _conn()
        R.grava_alteracoes(c, [_linha()])
        R.grava_alteracoes(c, [_linha(valor_depois=999, diferenca=999, causa="inesperado")])
        c.close()
        rows = _rows("SELECT diferenca, causa FROM ops.alteracoes_retroativas")
        assert [(float(d), ca) for d, ca in rows] == [(100.0, "carga nova")]

    def test_mesmo_par_em_run_seguinte_vira_outra_linha(self, banco_scratch):
        L.ensure_schemas()
        c = _conn()
        R.grava_alteracoes(c, [_linha(run_id=5), _linha(run_id=6, valor_antes=100, valor_depois=150, diferenca=50)])
        c.close()
        assert _rows("SELECT count(*) FROM ops.alteracoes_retroativas")[0][0] == 2


def _prepara_cenario(commit_r0="aaaaaaa"):
    """1 empresa, mart de faturamento com valo_imobiliario TEXT (tipo legado),
    run 1 'ok' (R0) e run 2 'executando' (o atual)."""
    L.ensure_schemas()
    _exec("CREATE TABLE mart.faturamento_mensal (codi_emp int, competencia text, valo_saidas double precision, "
          "valo_servicos double precision, valo_imobiliario text)")
    _exec("INSERT INTO mart.faturamento_mensal VALUES (1,'2025-03',100.0,20.0,'0'), (1,'2025-04',50.0,0,'0')")
    _exec(M._DDL_CONTROLE_RUNS)
    _exec("ALTER TABLE ops.controle_runs ADD COLUMN IF NOT EXISTS git_commit text")
    _exec("INSERT INTO ops.controle_runs (run_id, iniciado_em, empresa, modo, inicio, fim, status, git_commit) "
          "VALUES (1, '2026-09-10 10:00', 'all', 'completo', '2025-01', '2025-12', 'ok', %s), "
          "(2, '2026-09-24 10:00', 'all', 'completo', '2025-01', '2025-12', 'executando', NULL)", (commit_r0,))
    return {"faturamento_mensal": pd.DataFrame({"codi_emp": [1], "competencia": ["2025-03"]})}


def _publica_efsaidas(run_id, gen, pedido_run=None, pedido_gen=None):
    if pedido_run is not None:
        _exec("INSERT INTO ops.raw_generation (generation_id, run_id, table_name, status) "
              "VALUES (%s,%s,'efsaidas','PUBLISHED')", (pedido_gen, pedido_run))
        _exec("INSERT INTO ops.raw_generation_scope (generation_id, table_name, scope_type, codi_emp, "
              "scope_start, scope_end, status) VALUES (%s,'efsaidas','pedido',NULL,202501,202512,'PUBLISHED')",
              (pedido_gen,))
    _exec("INSERT INTO ops.raw_generation (generation_id, run_id, table_name, status) "
          "VALUES (%s,%s,'efsaidas','PUBLISHED')", (gen, run_id))
    _exec("INSERT INTO ops.raw_generation_scope (generation_id, table_name, scope_type, codi_emp, scope_value, "
          "authoritative, row_count, prev_row_count, status) VALUES (%s,'efsaidas','fatia',1,202503,true,1,1,'PUBLISHED')",
          (gen,))


def _roda(marts, commit_atual, r0_teto=None):
    rep = {}
    est = R.captura_antes(marts, "2025-01", "2025-12", 2, datetime(2026, 9, 24, 10, 0), rep)
    _exec("UPDATE mart.faturamento_mensal SET valo_saidas = 250.0 WHERE competencia = '2025-03'")
    R.fecha_comparacao(est, "2025-01", "2025-12", 2, rep, commit_atual, r0_teto)
    return rep, est


class TestNumerosEOrquestracao:
    def test_le_numeros_coage_valo_imobiliario_TEXT_e_soma_o_total(self, banco_scratch):
        _prepara_cenario()
        c = _conn()
        n = R.le_numeros(c, [1], "2025-01", "2025-12", "2026-09")
        c.close()
        assert n[(1, "2025-03", "NFe")] == 100.0 and n[(1, "2025-03", "NFS-e")] == 20.0
        assert n[(1, "2025-03", "Outras receitas")] == 0.0 and n[(1, "2025-03", "Total")] == 120.0
        # consumer real sem dado de imposto/folha: Impostos NaN (fica de fora), Folha 0,0 (folha_pm sem NaN)
        assert (1, "2025-03", "Impostos") not in n and n[(1, "2025-03", "Folha")] == 0.0

    def test_mes_da_carga_e_meses_seguintes_nao_entram_nos_numeros(self, banco_scratch):
        _prepara_cenario()
        c = _conn()
        n = R.le_numeros(c, [1], "2025-01", "2025-12", "2025-04")
        c.close()
        assert (1, "2025-03", "NFe") in n and (1, "2025-04", "NFe") not in n

    def test_sem_publicacao_e_commit_diferente_grava_mudou_o_calculo(self, banco_scratch):
        marts = _prepara_cenario("aaaaaaa")
        rep, _ = _roda(marts, "bbbbbbb")
        assert rep["retroativas"] == {"total": 1, "dominio": 0, "calculo": 1, "inesperado": 0}   # sem o Total
        rows = _rows("SELECT numero, valor_antes, valor_depois, diferenca, causa, fontes, git_commit, "
                     "git_commit_r0, janela_calculo FROM ops.alteracoes_retroativas ORDER BY numero")
        assert [(r[0], float(r[1]), float(r[2]), float(r[3]), r[4]) for r in rows] == [
            ("NFe", 100.0, 250.0, 150.0, "mudou o calculo"), ("Total", 120.0, 270.0, 150.0, "mudou o calculo")]
        assert rows[0][6:] == ("bbbbbbb", "aaaaaaa", "2025-01..2025-12") and "aaaaaaa" in rows[0][5]

    def test_fatia_publicada_e_ja_pedida_antes_grava_mudou_no_dominio(self, banco_scratch):
        marts = _prepara_cenario("aaaaaaa")
        _publica_efsaidas(run_id=2, gen=21, pedido_run=1, pedido_gen=11)
        rep, _ = _roda(marts, "aaaaaaa")
        assert rep["retroativas"] == {"total": 1, "dominio": 1, "calculo": 0, "inesperado": 0}
        rows = _rows("SELECT numero, causa, fontes FROM ops.alteracoes_retroativas ORDER BY numero")
        assert [(r[0], r[1]) for r in rows] == [("NFe", "mudou no Dominio"), ("Total", "mudou no Dominio")]
        assert "efsaidas 1 202503 (run 2)" in rows[0][2]

    def test_fatia_nunca_pedida_antes_e_carga_nova(self, banco_scratch):
        marts = _prepara_cenario("aaaaaaa")
        _publica_efsaidas(run_id=2, gen=21)                       # nenhum pedido de run anterior
        rep, _ = _roda(marts, "aaaaaaa")
        assert _rows("SELECT DISTINCT causa FROM ops.alteracoes_retroativas") == [("carga nova",)]
        assert rep["retroativas"]["dominio"] == 0

    def test_mesmo_commit_e_nada_publicado_e_inesperado_com_alerta_no_resumo(self, banco_scratch):
        marts = _prepara_cenario("aaaaaaa")
        rep, _ = _roda(marts, "aaaaaaa")
        assert rep["retroativas"]["inesperado"] == 1
        assert "ALERTA" in M.format_run_report({"args": {}, **rep})

    def test_primeiro_run_de_banco_novo_nao_compara(self, banco_scratch):
        marts = _prepara_cenario()
        _exec("UPDATE ops.controle_runs SET status = 'erro' WHERE run_id = 1")
        rep, est = _roda(marts, "bbbbbbb")
        assert est is None and rep["retroativas_pulada"] == R.MSG_PULADA_SEM_RUN
        assert _rows("SELECT count(*) FROM ops.alteracoes_retroativas")[0][0] == 0

    def test_falha_no_calculo_vira_erro_no_report_e_nunca_levanta(self, banco_scratch, monkeypatch):
        marts = _prepara_cenario()
        rep = {}

        def boom(*a, **k):
            raise RuntimeError("explodiu OK mesmo")
        monkeypatch.setattr(R, "le_numeros", boom)
        est = R.captura_antes(marts, "2025-01", "2025-12", 2, datetime(2026, 9, 24), rep)
        assert est is None and "explodiu" in rep["retroativas_erro"]
        assert "OK " not in rep["retroativas_erro"]
        rep2 = {}
        R.fecha_comparacao({"antes": {}, "emps": [1], "mes": "2026-09"}, "2025-01", "2025-12", 2, rep2, "x")
        assert "explodiu" in rep2["retroativas_erro"] and "retroativas" not in rep2

    def test_falha_de_conexao_nunca_levanta(self, monkeypatch):
        monkeypatch.setattr(R, "_conecta", lambda: (_ for _ in ()).throw(RuntimeError("sem banco")))
        rep = {}
        assert R.captura_antes({}, "2025-01", "2025-12", 2, datetime(2026, 9, 24), rep) is None
        assert "sem banco" in rep["retroativas_erro"]
        R.fecha_comparacao({"antes": {}, "emps": [1], "mes": "2026-09"}, "2025-01", "2025-12", 2, rep, None)
        assert "sem banco" in rep["retroativas_erro"]


class TestAvisoRunAnteriorMortoComBanco:
    """Processo morto no meio do load perde o ANTES (estava em memoria). O run seguinte avisa."""

    def _cenario(self, status_anterior, publicou):
        marts = _prepara_cenario()
        _exec("UPDATE ops.controle_runs SET status = %s WHERE run_id = 2", (status_anterior,))
        _exec("INSERT INTO ops.controle_runs (run_id, iniciado_em, empresa, modo, inicio, fim, status) "
              "VALUES (3, '2026-09-24 12:00', 'all', 'completo', '2025-01', '2025-12', 'executando')")
        if publicou:
            _exec("INSERT INTO ops.raw_generation (generation_id, run_id, table_name, status) "
                  "VALUES (30, 2, 'efsaidas', 'PUBLISHED')")
        rep = {}
        R.captura_antes(marts, "2025-01", "2025-12", 3, datetime(2026, 9, 24, 12, 0), rep)
        return rep

    def test_run_anterior_executando_que_ja_publicou_avisa(self, banco_scratch):
        rep = self._cenario("executando", publicou=True)
        assert rep.get("retroativas_aviso") == R.aviso_run_morto(2, "executando")

    def test_run_anterior_com_erro_que_ja_publicou_avisa(self, banco_scratch):
        rep = self._cenario("erro", publicou=True)
        assert rep.get("retroativas_aviso") == R.aviso_run_morto(2, "erro")

    def test_run_anterior_morto_que_nao_publicou_nada_nao_avisa(self, banco_scratch):
        assert "retroativas_aviso" not in self._cenario("executando", publicou=False)

    def test_run_anterior_ok_ou_parcial_nao_avisa(self, banco_scratch):
        assert "retroativas_aviso" not in self._cenario("ok", publicou=True)

    def test_run_anterior_parcial_nao_avisa(self, banco_scratch):
        assert "retroativas_aviso" not in self._cenario("parcial", publicou=True)

    def test_aviso_sai_mesmo_quando_a_comparacao_e_pulada_por_falta_de_run_ok(self, banco_scratch):
        marts = _prepara_cenario()
        _exec("UPDATE ops.controle_runs SET status = 'executando' WHERE run_id IN (1, 2)")
        _exec("INSERT INTO ops.controle_runs (run_id, iniciado_em, empresa, modo, inicio, fim, status) "
              "VALUES (3, '2026-09-24 12:00', 'all', 'completo', '2025-01', '2025-12', 'executando')")
        _exec("INSERT INTO ops.raw_generation (generation_id, run_id, table_name, status) "
              "VALUES (30, 2, 'efsaidas', 'PUBLISHED')")
        rep = {}
        R.captura_antes(marts, "2025-01", "2025-12", 3, datetime(2026, 9, 24, 12, 0), rep)
        assert rep.get("retroativas_aviso") == R.aviso_run_morto(2, "executando")
        assert rep["retroativas_pulada"] == R.MSG_PULADA_SEM_RUN

    def test_so_o_run_IMEDIATAMENTE_anterior_conta_penultimo_morto_nao_avisa(self, banco_scratch):
        # run 2 morreu depois de publicar, mas o run 3 (imediatamente anterior) fechou 'ok':
        # o aviso daquele periodo ja passou -- so o run 3 conta.
        marts = _prepara_cenario()
        _exec("UPDATE ops.controle_runs SET status = 'executando' WHERE run_id = 2")
        _exec("INSERT INTO ops.controle_runs (run_id, iniciado_em, empresa, modo, inicio, fim, status) "
              "VALUES (3, '2026-09-24 12:00', 'all', 'completo', '2025-01', '2025-12', 'ok'), "
              "(4, '2026-09-24 13:00', 'all', 'completo', '2025-01', '2025-12', 'executando')")
        _exec("INSERT INTO ops.raw_generation (generation_id, run_id, table_name, status) "
              "VALUES (30, 2, 'efsaidas', 'PUBLISHED')")
        rep = {}
        R.captura_antes(marts, "2025-01", "2025-12", 4, datetime(2026, 9, 24, 13, 0), rep)
        assert "retroativas_aviso" not in rep
