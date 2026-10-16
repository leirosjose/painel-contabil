# ---------------------------------------------------------------------------
# connections.py — Conexão com o PostgreSQL
# ---------------------------------------------------------------------------

import psycopg2

from config import PG_DB, PG_HOST, PG_PASS, PG_PORT, PG_USER


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
    _test("PostgreSQL   (localhost:5432 / painel_bi)", connect_postgres)
