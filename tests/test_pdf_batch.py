# ---------------------------------------------------------------------------
# Robustez do lote PDF (chunk nao derruba vizinhos). Cobre o CORE testavel sem
# Playwright/Postgres via monkeypatch dos seams _render_chunk / _run_render /
# _build_html_for_empresa.
# ---------------------------------------------------------------------------
import io
import subprocess
import zipfile

import pandas as pd
import pytest

import pdf_generator as P


def _items(n):
    return [(f"<html><body>{i}</body></html>", f"label{i}.pdf") for i in range(n)]


# ── ler PDFs presentes em disco independente do returncode ──────────
class TestBatchReadByPresence:
    def test_falha_de_um_nao_descarta_os_corretos_do_chunk(self, monkeypatch):
        # rc!=0 + 4 de 5 PDFs no disco -> os 4 voltam com bytes, so o ausente
        # entra como erro com a mensagem real.
        def fake_render_chunk(manifest, timeout):
            status = {}
            for m in manifest:
                if m["index"] == 4:                       # o "culpado": nao escreve
                    status[m["pdf_path"]] = {"ok": False, "error": "render boom emp4"}
                else:
                    with open(m["pdf_path"], "wb") as f:
                        f.write(b"%PDF-1.4 fake")
                    status[m["pdf_path"]] = {"ok": True, "error": None}
            return status, "subprocess rc=1"
        monkeypatch.setattr(P, "_render_chunk", fake_render_chunk)
        res = P._html_to_pdf_batch(_items(5))
        ok = [(lbl, b) for lbl, b, err in res if b is not None]
        bad = [(lbl, err) for lbl, b, err in res if b is None]
        assert len(ok) == 4
        assert all(b.startswith(b"%PDF") for _, b in ok)
        assert len(bad) == 1 and bad[0][0] == "label4.pdf"
        assert "boom emp4" in bad[0][1]

    def test_falha_no_primeiro_de_cada_chunk_10_de_12_sobrevivem(self, monkeypatch):
        # Determinismo de chunk: falha no item 0 de cada chunk (global 0 e 10)
        # nao contamina os vizinhos -> 10 sobrevivem (prova escopo por-chunk).
        def fake_render_chunk(manifest, timeout):
            status = {}
            for m in manifest:
                if m["index"] == 0:
                    status[m["pdf_path"]] = {"ok": False, "error": "falha idx0"}
                else:
                    with open(m["pdf_path"], "wb") as f:
                        f.write(b"%PDF-1.4 fake")
                    status[m["pdf_path"]] = {"ok": True, "error": None}
            return status, None
        monkeypatch.setattr(P, "_render_chunk", fake_render_chunk)
        res = P._html_to_pdf_batch(_items(12))
        ok = [lbl for lbl, b, err in res if b is not None]
        bad = [lbl for lbl, b, err in res if b is None]
        assert len(ok) == 10
        assert sorted(bad) == ["label0.pdf", "label10.pdf"]

    def test_stub_truncado_com_status_falho_vira_erro(self, monkeypatch):
        # Arquivo presente porem status ok=False (crash no meio do page.pdf):
        # o status manda -> erro, nao empacota PDF truncado.
        def fake_render_chunk(manifest, timeout):
            status = {}
            for m in manifest:
                with open(m["pdf_path"], "wb") as f:
                    f.write(b"%PDF-trunc")               # stub nao-vazio no disco
                status[m["pdf_path"]] = {"ok": False, "error": "crash mid pdf"}
            return status, None
        monkeypatch.setattr(P, "_render_chunk", fake_render_chunk)
        res = P._html_to_pdf_batch(_items(1))
        assert res[0][1] is None and "crash mid pdf" in res[0][2]

    def test_timeout_sem_status_usa_proc_err_e_le_o_que_existe(self, monkeypatch):
        # Sem JSON de status (processo morto no timeout): le por presenca; os
        # ausentes herdam proc_err (prova fallback do contrato).
        def fake_render_chunk(manifest, timeout):
            for m in manifest:
                if m["index"] == 0:
                    with open(m["pdf_path"], "wb") as f:
                        f.write(b"%PDF-1.4 fake")
            return {}, "timeout apos 3s (lote seguiu)"
        monkeypatch.setattr(P, "_render_chunk", fake_render_chunk)
        res = P._html_to_pdf_batch(_items(2))
        assert res[0][1] is not None and res[0][1].startswith(b"%PDF")
        assert res[1][1] is None and "timeout" in res[1][2]


# ── TimeoutExpired capturado + process group reapado ────────────────
class TestRunRenderTimeoutReap:
    def test_timeout_dispara_reap_e_marca_kind_timeout(self, monkeypatch):
        # _run_render: no TimeoutExpired, chama _reap (mata o grupo) e devolve
        # kind=="timeout" SEM propagar (lote segue). Sem processo real.
        reaped = {"n": 0}

        class FakePopen:
            def __init__(self, *a, **k):
                self.pid = 4242
                self.returncode = -9
                self._calls = 0

            def communicate(self, input=None, timeout=None):
                self._calls += 1
                if self._calls == 1:
                    raise subprocess.TimeoutExpired(cmd="pdf_render.py", timeout=timeout)
                return (b"", b"")

        monkeypatch.setattr(subprocess, "Popen", FakePopen)
        monkeypatch.setattr(P, "_reap", lambda proc: reaped.__setitem__("n", reaped["n"] + 1))
        rc, out, err, kind = P._run_render(b"[]", timeout=3, cwd=".")
        assert kind == "timeout"
        assert reaped["n"] == 1


# ── conn.rollback por empresa evita cascata de transacao abortada ───
class TestRollbackPorEmpresa:
    def test_falha_de_build_faz_rollback_e_nao_derruba_o_resto(self, monkeypatch):
        unidades = [
            {"ids": [1], "nome": "EMP_A", "cnpj": "", "filename": "A.pdf"},
            {"ids": [2], "nome": "EMP_B", "cnpj": "", "filename": "B.pdf"},
        ]
        monkeypatch.setattr(P, "listar_unidades_lote", lambda *a, **k: unidades)

        def fake_build(ids, inicio, fim, conn, nome="", cnpj="", modelos=None):
            if ids == [1]:
                raise Exception("current transaction is aborted")
            return "<html>ok</html>"
        monkeypatch.setattr(P, "_build_html_for_empresa", fake_build)
        monkeypatch.setattr(P, "_html_to_pdf_batch",
                            lambda items: [(lbl, b"%PDF-1.4 fake", None) for _, lbl in items])

        class FakeConn:
            def __init__(self): self.rollbacks = 0
            def rollback(self): self.rollbacks += 1

        conn = FakeConn()
        zip_bytes, sucessos, erros = P.gerar_todos_pdfs("2025-01", "2025-01", conn)
        assert conn.rollbacks == 1                         # rollback na empresa que falhou
        assert sucessos == 1                               # EMP_B entrou
        assert len(erros) == 1 and "EMP_A" in erros[0]
        assert zip_bytes is not None


# ── PDF por seleção: lista explícita de unidades em gerar_todos_pdfs ──────────
class TestUnidadesExplicitas:
    def _patch_render(self, monkeypatch):
        monkeypatch.setattr(
            P, "_build_html_for_empresa",
            lambda ids, inicio, fim, conn, nome="", cnpj="", modelos=None: "<html>ok</html>",
        )
        monkeypatch.setattr(
            P, "_html_to_pdf_batch",
            lambda items: [(lbl, b"%PDF-1.4 fake", None) for _, lbl in items],
        )

    def test_lista_explicita_gera_so_as_passadas_sem_consultar_listar(self, monkeypatch):
        # Selecao vinda da UI: listar_unidades_lote NAO pode ser consultado
        # (a lista ja veio pronta) e o ZIP contem exatamente os selecionados.
        def boom(*a, **k):
            raise AssertionError("listar_unidades_lote nao deve ser chamado")
        monkeypatch.setattr(P, "listar_unidades_lote", boom)
        self._patch_render(monkeypatch)
        unidades = [
            {"ids": [7], "nome": "EMP_X", "cnpj": "", "filename": "X.pdf"},
            {"ids": [9], "nome": "EMP_Y", "cnpj": "", "filename": "Y.pdf"},
        ]
        zip_bytes, sucessos, erros = P.gerar_todos_pdfs(
            "2025-01", "2025-03", None, unidades=unidades)
        assert sucessos == 2 and erros == []
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            assert sorted(zf.namelist()) == ["X.pdf", "Y.pdf"]

    def test_lista_explicita_vazia_retorna_none(self, monkeypatch):
        self._patch_render(monkeypatch)
        zip_bytes, sucessos, erros = P.gerar_todos_pdfs(
            "2025-01", "2025-01", None, unidades=[])
        assert zip_bytes is None and sucessos == 0 and erros == []

    def test_falha_no_meio_da_selecao_nao_aborta(self, monkeypatch):
        # 1 unidade quebra no build -> as demais seguem e a
        # falha e reportada (mesmo contrato do lote TODOS).
        def fake_build(ids, inicio, fim, conn, nome="", cnpj="", modelos=None):
            if ids == [1]:
                raise Exception("sem dados no periodo")
            return "<html>ok</html>"
        monkeypatch.setattr(P, "_build_html_for_empresa", fake_build)
        monkeypatch.setattr(
            P, "_html_to_pdf_batch",
            lambda items: [(lbl, b"%PDF-1.4 fake", None) for _, lbl in items],
        )

        class FakeConn:
            def rollback(self):
                pass

        unidades = [
            {"ids": [1], "nome": "EMP_A", "cnpj": "", "filename": "A.pdf"},
            {"ids": [2], "nome": "EMP_B", "cnpj": "", "filename": "B.pdf"},
        ]
        zip_bytes, sucessos, erros = P.gerar_todos_pdfs(
            "2025-01", "2025-01", FakeConn(), unidades=unidades)
        assert sucessos == 1
        assert len(erros) == 1 and "EMP_A" in erros[0]
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            assert zf.namelist() == ["B.pdf"]


# ── PDF por seleção: campo raiz nas unidades (label "NOME — raiz") ────────────
class TestListarUnidadesRaiz:
    # Default `ativa` True aqui: estes
    # testes são sobre a RAIZ, e empresa ativa aparece independente de dado.
    _FLAGS = {"ativa": True,
              "tem_fat": True, "tem_imp": False, "tem_func": False,
              "tem_folha": False, "tem_bal": False}

    def _mock_df(self, monkeypatch, rows):
        df = pd.DataFrame([{**self._FLAGS, **r} for r in rows])
        monkeypatch.setattr(P.pd, "read_sql", lambda *a, **k: df)

    def test_singleton_raiz_8_digitos(self, monkeypatch):
        self._mock_df(monkeypatch, [
            {"codi_emp": 1, "nome_emp": "ALFA", "cgce_emp": "49275198000106"},
        ])
        us = P.listar_unidades_lote("2025-01", "2025-03", None, filtro="all")
        assert us[0]["raiz"] == "49275198"

    def test_cnpj_invalido_raiz_vazia(self, monkeypatch):
        # Sentinela interna _solo_<id> nao pode vazar pro label da UI.
        self._mock_df(monkeypatch, [
            {"codi_emp": 2, "nome_emp": "BETA", "cgce_emp": None},
        ])
        us = P.listar_unidades_lote("2025-01", "2025-03", None, filtro="all")
        assert us[0]["raiz"] == ""

    def test_grupo_consolidado_raiz_do_grupo(self, monkeypatch):
        self._mock_df(monkeypatch, [
            {"codi_emp": 3, "nome_emp": "GAMA", "cgce_emp": "82381183000106"},
            {"codi_emp": 4, "nome_emp": "GAMA F2", "cgce_emp": "82381183000289"},
        ])
        us = P.listar_unidades_lote("2025-01", "2025-03", None,
                                    filtro="all", consolidar=True)
        assert len(us) == 1
        assert us[0]["raiz"] == "82381183"


class TestListarUnidadesCarteiraInativa:
    """ESPELHO do dropdown do app: o lote usa o MESMO `carteira_visivel`.

    Sem isto o app mostraria a inativa-com-dado e o ZIP sairia sem o PDF dela.
    """
    _SEM_DADO = {"tem_fat": False, "tem_imp": False, "tem_func": False,
                 "tem_folha": False, "tem_bal": False}

    def _mock_df(self, monkeypatch, rows):
        df = pd.DataFrame([{**self._SEM_DADO, **r} for r in rows])
        monkeypatch.setattr(P.pd, "read_sql", lambda *a, **k: df)

    def test_inativa_com_dado_entra_no_lote(self, monkeypatch):
        self._mock_df(monkeypatch, [
            {"codi_emp": 213, "nome_emp": "EMPRESA ALFA", "ativa": False,
             "cgce_emp": "89039653000279", "tem_fat": True},
        ])
        us = P.listar_unidades_lote("2025-01", "2025-12", None, filtro="all")
        assert [u["ids"] for u in us] == [[213]]

    def test_a_marca_vai_pro_display_e_NAO_pro_filename(self, monkeypatch):
        # O contrato que protege o nome do arquivo (_safe_filename come os
        # colchetes e produziria "EMPRESA_ALFA_INATIVA.pdf").
        self._mock_df(monkeypatch, [
            {"codi_emp": 213, "nome_emp": "EMPRESA ALFA", "ativa": False,
             "cgce_emp": "89039653000279", "tem_fat": True},
        ])
        u = P.listar_unidades_lote("2025-01", "2025-12", None, filtro="all")[0]
        assert u["nome_display"] == "EMPRESA ALFA [INATIVA]"
        assert u["nome"] == "EMPRESA ALFA"
        assert u["filename"] == "EMPRESA_ALFA.pdf"
        assert "INATIVA" not in u["filename"]

    def test_inativa_sem_dado_na_janela_fica_de_fora(self, monkeypatch):
        self._mock_df(monkeypatch, [
            {"codi_emp": 235, "nome_emp": "EMPRESA BETA F2", "ativa": False,
             "cgce_emp": "76352993000203"},
        ])
        assert P.listar_unidades_lote("2025-01", "2025-12", None,
                                      filtro="all") == []

    def test_grupo_se_forma_com_a_filial_inativa_e_o_grupo_NAO_e_marcado(
            self, monkeypatch):
        # Sem a 213 a raiz cai pra 1 filial e o item de GRUPO
        # deixa de existir. O grupo em si nao e inativo — quem e sao as filiais.
        self._mock_df(monkeypatch, [
            {"codi_emp": 214, "nome_emp": "EMPRESA ALFA", "ativa": True,
             "cgce_emp": "89039653000198", "tem_fat": True},
            {"codi_emp": 213, "nome_emp": "EMPRESA ALFA", "ativa": False,
             "cgce_emp": "89039653000279", "tem_fat": True},
        ])
        us = P.listar_unidades_lote("2025-01", "2025-12", None,
                                    filtro="all", consolidar=True)
        assert len(us) == 1
        assert us[0]["ids"] == [213, 214]
        assert "INATIVA" not in us[0]["nome_display"]

    def test_ativa_nao_ganha_marca(self, monkeypatch):
        self._mock_df(monkeypatch, [
            {"codi_emp": 201, "nome_emp": "EMPRESA BETA", "ativa": True,
             "cgce_emp": "76352993000122", "tem_fat": True},
        ])
        u = P.listar_unidades_lote("2025-01", "2025-12", None, filtro="all")[0]
        assert u["nome_display"] == "EMPRESA BETA"


# ── kaleido: lote nao segue fora do server ────
# Em oneshot sao ~2,3 min por PDF (~15 h no lote) e em off o PDF sai sem
# grafico com rc=0. O lote para na fronteira de unidade e devolve o que saiu.
class TestLoteAbortaForaDoModoServer:
    @staticmethod
    def _unidades(n):
        return [{"ids": [i], "nome": f"EMP_{i}", "cnpj": "", "filename": f"{i}.pdf"}
                for i in range(1, n + 1)]

    def _arma(self, monkeypatch, modo_inicial="server", cai_em=None, cai_para=None):
        monkeypatch.setattr(P, "_kaleido_modo", modo_inicial, raising=False)
        monkeypatch.setattr(P, "_kaleido_motivo", "render travou (sem resposta em 30s)",
                            raising=False)
        monkeypatch.setattr(P, "_kaleido_resondar", lambda: None, raising=False)
        construidas = []

        def fake_build(ids, inicio, fim, conn, nome="", cnpj="", modelos=None):
            construidas.append(ids[0])
            if ids[0] == cai_em:
                P._kaleido_modo = cai_para
            return "<html>ok</html>"
        monkeypatch.setattr(P, "_build_html_for_empresa", fake_build)
        monkeypatch.setattr(P, "_html_to_pdf_batch",
                            lambda items: [(lbl, b"%PDF-1.4 fake", None) for _, lbl in items])
        return construidas

    def test_desce_pro_oneshot_no_meio_aborta_antes_da_proxima(self, monkeypatch):
        construidas = self._arma(monkeypatch, cai_em=2, cai_para="oneshot")
        with pytest.raises(P.LoteAbortadoKaleido) as exc:
            P.gerar_todos_pdfs("2025-01", "2025-01", None, unidades=self._unidades(3))
        e = exc.value
        assert construidas == [1, 2]            # a 3a nem comeca
        assert e.exit_code == 2
        assert e.modo == "oneshot"
        assert "oneshot" in str(e) and "render travou" in str(e)
        # a unidade que desceu pro oneshot entra: pixel identico ao server
        assert e.sucessos == 2 and e.pendentes == ["EMP_3"]
        with zipfile.ZipFile(io.BytesIO(e.zip_bytes)) as zf:
            assert sorted(zf.namelist()) == ["1.pdf", "2.pdf"]

    def test_unidade_que_caiu_em_off_no_meio_nao_entra(self, monkeypatch):
        self._arma(monkeypatch, cai_em=2, cai_para="off")
        with pytest.raises(P.LoteAbortadoKaleido) as exc:
            P.gerar_todos_pdfs("2025-01", "2025-01", None, unidades=self._unidades(3))
        e = exc.value
        assert e.modo == "off" and e.sucessos == 1
        assert e.pendentes == ["EMP_2", "EMP_3"]
        with zipfile.ZipFile(io.BytesIO(e.zip_bytes)) as zf:
            assert zf.namelist() == ["1.pdf"]

    def test_ja_degradado_no_inicio_aborta_sem_construir_nada(self, monkeypatch):
        construidas = self._arma(monkeypatch, modo_inicial="oneshot")
        with pytest.raises(P.LoteAbortadoKaleido) as exc:
            P.gerar_todos_pdfs("2025-01", "2025-01", None, unidades=self._unidades(2))
        e = exc.value
        assert construidas == []
        assert e.zip_bytes is None and e.sucessos == 0
        assert e.pendentes == ["EMP_1", "EMP_2"]

    def test_modo_server_segue_o_contrato_de_sempre(self, monkeypatch):
        self._arma(monkeypatch)
        zip_bytes, sucessos, erros = P.gerar_todos_pdfs(
            "2025-01", "2025-01", None, unidades=self._unidades(2))
        assert sucessos == 2 and erros == [] and zip_bytes

    def test_pdf_individual_nao_aborta_fora_do_server(self, monkeypatch):
        monkeypatch.setattr(P, "_kaleido_modo", "oneshot", raising=False)
        chamadas = []
        monkeypatch.setattr(P, "_kaleido_resondar", lambda: chamadas.append("resonda"),
                            raising=False)
        monkeypatch.setattr(P, "_build_html_for_empresa",
                            lambda *a, **k: "<html>ok</html>")
        monkeypatch.setattr(P, "_html_to_pdf", lambda html: b"%PDF-1.4 fake")
        assert P.gerar_pdf_empresa([1], "2025-01", "2025-01", None) == b"%PDF-1.4 fake"
        assert chamadas == ["resonda"]          # fronteira de PDF re-sonda
