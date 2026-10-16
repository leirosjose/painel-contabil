# ---------------------------------------------------------------------------
# connections.py — Funções de conexão com SQL Anywhere e PostgreSQL
# ---------------------------------------------------------------------------

import jaydebeapi
import psycopg2

from config import (
    SA_JAR, SA_LIB, SA_DRIVER, SA_URL,
    PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASS,
)


def connect_sqlanywhere() -> jaydebeapi.Connection:
    """Abre conexão com o SAP SQL Anywhere 17 via JDBC/jaydebeapi.

    Returns:
        jaydebeapi.Connection pronta para uso.

    Raises:
        RuntimeError: se o servidor não estiver acessível ou o driver falhar.
    """
    try:
        conn = jaydebeapi.connect(
            SA_DRIVER,
            SA_URL,
            [],          # credenciais já embutidas na URL
            [SA_JAR],
            libs=[SA_LIB],
        )
        return conn
    except Exception as exc:
        msg = str(exc)
        if "connect" in msg.lower() or "refused" in msg.lower() or "timeout" in msg.lower():
            raise RuntimeError(
                "SQL Anywhere não está rodando em localhost:2638 — "
                "verifique se o serviço está iniciado."
            ) from exc
        if "classnotfound" in msg.lower() or "jar" in msg.lower():
            raise RuntimeError(
                f"Driver JDBC não encontrado. Confirme que o arquivo existe:\n  {SA_JAR}"
            ) from exc
        raise RuntimeError(f"Falha ao conectar no SQL Anywhere: {exc}") from exc


def connect_postgres() -> "psycopg2.connection":
    """Abre conexão com o PostgreSQL local (banco painel_bi).

    Returns:
        psycopg2.connection pronta para uso.

    Raises:
        RuntimeError: se PG_PASS estiver ausente no .env (fail-fast)
            ou se o servidor não estiver acessível.
    """
    if PG_PASS is None:
        raise RuntimeError(
            "PG_PASS ausente no .env — defina a senha do PostgreSQL "
            "(PG_PASS=...). Se o servidor local usa trust auth (dev), "
            "declare PG_PASS= vazio explicitamente."
        )
    try:
        conn = psycopg2.connect(
            host=PG_HOST,
            port=PG_PORT,
            dbname=PG_DB,
            user=PG_USER,
            password=PG_PASS,
        )
        return conn
    except psycopg2.OperationalError as exc:
        msg = str(exc).strip()
        raise RuntimeError(
            f"Falha ao conectar no PostgreSQL ({PG_USER}@{PG_HOST}:{PG_PORT}/{PG_DB}): {msg}"
        ) from exc


# ---------------------------------------------------------------------------
# Teste rápido de conexão — execute diretamente: python connections.py
# ---------------------------------------------------------------------------
def _test(label: str, fn):
    try:
        conn = fn()
        conn.close()
        print(f"[OK]   {label}")
    except RuntimeError as exc:
        print(f"[ERRO] {label}\n       {exc}")


if __name__ == "__main__":
    print("Testando conexões...\n")
    _test("SQL Anywhere (localhost:2638 / Contabil)", connect_sqlanywhere)
    _test("PostgreSQL   (localhost:5432 / painel_bi)", connect_postgres)
