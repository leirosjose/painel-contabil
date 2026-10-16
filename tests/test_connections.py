# ---------------------------------------------------------------------------
# Testes de connect_postgres — PG_PASS ausente vs vazia explicita.
# Sem conexao real: psycopg2.connect e stubado no caso permitido.
# ---------------------------------------------------------------------------
import pytest

import connections


class TestPgPassFailFast:
    def test_pg_pass_ausente_dispara_runtime_error_claro(self, monkeypatch):
        # None = variavel NAO setada no .env -> fail-fast antes do connect
        monkeypatch.setattr(connections, "PG_PASS", None)
        with pytest.raises(RuntimeError, match="PG_PASS"):
            connections.connect_postgres()

    def test_pg_pass_vazia_explicita_permitida(self, monkeypatch):
        # "" = PG_PASS= declarada vazia no .env (trust auth local)
        chamado = {}

        class _FakeConn:
            pass

        def fake_connect(**kw):
            chamado.update(kw)
            return _FakeConn()

        monkeypatch.setattr(connections, "PG_PASS", "")
        monkeypatch.setattr(connections.psycopg2, "connect", fake_connect)
        conn = connections.connect_postgres()
        assert isinstance(conn, _FakeConn)
        assert chamado["password"] == ""
