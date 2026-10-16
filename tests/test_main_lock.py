# ---------------------------------------------------------------------------
# Um segundo `run` do ETL enquanto outro esta vivo tem de ser RECUSADO por
# advisory lock em `ops`, nao seguir e arriscar dois DELETE-escopado
# concorrentes no mesmo mart.
# ---------------------------------------------------------------------------
import pytest


class TestAdquireLockRun:
    @pytest.mark.db
    def test_segundo_run_e_recusado_enquanto_o_primeiro_detem_o_lock(self, banco_scratch):
        """RED por VALOR: adquire o lock uma vez (simulando o 1o run vivo) e
        prova que uma SEGUNDA tentativa, em conexao INDEPENDENTE (o mesmo
        que um segundo processo do ETL faria), e recusada -- nao silenciosa,
        levanta RunConcorrenteError. Sem isso dois `main()` concorrentes
        seguiriam ambos pro DELETE-escopado no mesmo mart.
        """
        from main import RunConcorrenteError, _adquire_lock_run, _libera_lock_run

        conn1 = _adquire_lock_run()
        try:
            with pytest.raises(RunConcorrenteError):
                _adquire_lock_run()
        finally:
            _libera_lock_run(conn1)

    @pytest.mark.db
    def test_lock_liberado_permite_novo_run(self, banco_scratch):
        """Depois que o run dono libera o lock (fim normal do run), um novo
        run consegue adquiri-lo -- a trava nao fica presa pra sempre."""
        from main import _adquire_lock_run, _libera_lock_run

        conn1 = _adquire_lock_run()
        _libera_lock_run(conn1)

        conn2 = _adquire_lock_run()
        _libera_lock_run(conn2)

    @pytest.mark.db
    def test_processo_morto_libera_o_lock_ao_fechar_a_conexao(self, banco_scratch):
        """Processo morre SEM chamar
        _libera_lock_run. A trava e de SESSAO (pg_advisory_lock, nao
        pg_advisory_xact_lock) -- fechar a conexao (o que o SO faz sozinho
        quando mata o processo) ja libera, sem precisar do release
        explicito. Simula o crash fechando a conexao na mao, sem unlock."""
        from main import _adquire_lock_run

        conn1 = _adquire_lock_run()
        conn1.close()  # "crash": ninguem chamou pg_advisory_unlock

        conn2 = _adquire_lock_run()
        conn2.close()


class TestMainRecusaRunConcorrente:
    @pytest.mark.db
    def test_main_sai_1_quando_ja_ha_run_vivo(self, banco_scratch, monkeypatch):
        """Ponta a ponta: um SEGUNDO `main()` (nao so o helper isolado) com
        outro run detendo o lock sai com codigo 1, ANTES de tocar em extract
        ou raw -- o criterio em cima do fluxo real, nao so da funcao
        interna."""
        from main import _adquire_lock_run, _libera_lock_run, main

        lock_conn = _adquire_lock_run()
        try:
            monkeypatch.setattr(
                "sys.argv",
                ["main.py", "--skip-extract", "--empresa", "all",
                 "--inicio", "2023-01", "--fim", "2023-01"],
            )
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1
        finally:
            _libera_lock_run(lock_conn)
