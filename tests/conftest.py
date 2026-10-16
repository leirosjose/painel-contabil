# ---------------------------------------------------------------------------
# conftest.py — sys.path pros dois estilos de import do projeto:
#   - etl/transform.py usa imports FLAT (`from mappings import ...`)
#     -> precisa de etl/ no path
#   - etl/utils.py e pdf_generator.py usam package (`from etl.mappings ...`)
#     -> precisa da RAIZ no path
# ---------------------------------------------------------------------------
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "etl"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


@pytest.fixture
def banco_scratch(monkeypatch):
    """Banco Postgres NOVO por teste -- so pra testes @pytest.mark.db.

    Nunca o painel_bi: o nome carrega o pid e o assert trava colisao. O
    connect_postgres le connections.PG_DB na hora da chamada, entao o
    monkeypatch redireciona TODO o pipeline (load, geracao, main).
    """
    import psycopg2
    import connections
    nome = f"painel_teste_{os.getpid()}"
    assert nome != "painel_bi"
    adm = psycopg2.connect(dbname="postgres", host=connections.PG_HOST, port=connections.PG_PORT,
                           user=connections.PG_USER, password=connections.PG_PASS or None)
    adm.autocommit = True
    with adm.cursor() as c:
        c.execute(f'DROP DATABASE IF EXISTS "{nome}"')
        c.execute(f'CREATE DATABASE "{nome}"')
    monkeypatch.setattr(connections, "PG_DB", nome)
    try:
        yield nome
    finally:
        with adm.cursor() as c:
            c.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                      "WHERE datname = %s AND pid <> pg_backend_pid()", (nome,))
            c.execute(f'DROP DATABASE IF EXISTS "{nome}"')
        adm.close()


# ---------------------------------------------------------------------------
# AppTest -- suites que EXECUTAM o app.py (st.testing.v1).
# ---------------------------------------------------------------------------

APP_PY = ROOT / "app.py"


def _pg_conn_app():
    """Conecta pelo MESMO caminho que o app.py usa (os.getenv + defaults).

    Nao passa por `etl.connections`: o `app.get_conn` le `os.getenv` direto,
    e a guarda tem de decidir pelo que o APP vai encontrar, nao pelo que o
    ETL encontraria.
    """
    import psycopg2
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    return psycopg2.connect(
        host=os.getenv("PG_HOST", "localhost"),
        port=int(os.getenv("PG_PORT", 5432)),
        dbname=os.getenv("PG_DB", "painel_bi"),
        user=os.getenv("PG_USER", "postgres"),
        password=os.getenv("PG_PASS", ""),
    )


@pytest.fixture(scope="session")
def banco_carregado():
    """Guarda dos testes @pytest.mark.apptest: painel_bi COM carga.

    A assimetria e DELIBERADA:
      - banco inalcancavel, ou `mart.faturamento_mensal` ausente/vazia -> SKIP
        (ambiente sem carga, ex.: Postgres de servico virgem no CI).
      - banco presente e a linha-gabarito faltando -> o TESTE falha, nunca
        skipa. Ali e regressao de DADO; skipar seria esconder o achado.
    """
    try:
        conn = _pg_conn_app()
    except Exception as e:                                   # noqa: BLE001
        pytest.skip(f"painel_bi indisponivel ({type(e).__name__}): "
                    "AppTest exige o banco carregado")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('mart.faturamento_mensal')")
            if cur.fetchone()[0] is None:
                pytest.skip("mart.faturamento_mensal ausente: "
                            "AppTest exige o banco carregado")
            cur.execute("SELECT count(*) FROM mart.faturamento_mensal")
            if cur.fetchone()[0] == 0:
                pytest.skip("mart.faturamento_mensal vazia: "
                            "AppTest exige o banco carregado")
        yield
    finally:
        conn.close()


@pytest.fixture
def abre_app(banco_carregado):
    """Abre o app.py no AppTest e pousa numa (empresa, janela) determinada.

    TRES FASES, e a ordem nao e estilo:
      1) `run()` descobre os ROTULOS de periodo -- o `.options` do selectbox
         devolve "Jan/26", nunca o indice cru que e a opcao de verdade;
      2) sel_de/sel_ate + `run()` poem a JANELA alvo;
      3) so entao `session_state["emp_atual"]` + `run()` pina a empresa.

    Pinar ANTES da janela funciona pra empresa avulsa e FALHA EM SILENCIO pra
    GRUPO: no 1o run a janela e a default, a composicao do grupo e outra, a
    tupla nao esta em `_lbl_por_ids` e o re-ancora cai em `(_sel[0],)` -- o
    card exibiria a matriz em vez do grupo.

    O estado da empresa mora em `emp_atual`, chave NOSSA, porque o selectbox
    de Empresa nao tem `key=` de proposito. NAO dar `key=` a ele pra
    "facilitar o teste".

    REGRA DO MODULO: nenhum teste de AppTest clica "Executar Atualizacao" nem
    os botoes de PDF. O slot do ETL e `@st.cache_resource`, process-global, e
    vazaria o subprocess pros testes seguintes.
    """
    def _abre(emp_ids=None, de=None, ate=None, busca=None, timeout=60):
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_file(str(APP_PY), default_timeout=timeout)
        at.run()
        if de is not None or ate is not None or busca is not None:
            if de is not None:
                sb = at.sidebar.selectbox(key="sel_de")
                sb.set_value(sb.options.index(de))
            if ate is not None:
                sb = at.sidebar.selectbox(key="sel_ate")
                sb.set_value(sb.options.index(ate))
            if busca is not None:
                at.sidebar.text_input("busca_emp").set_value(busca)
            at.run()
        if emp_ids is not None:
            at.session_state["emp_atual"] = tuple(emp_ids)
            at.run()
        return at

    return _abre
