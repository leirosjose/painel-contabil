"""Pagina "Alteracoes retroativas".

Duas familias:
  - source-level (sem banco, sem AppTest): o desenho da navegacao e a pagina
    read-only;
  - AppTest da PAGINA contra um banco SCRATCH (`painel_teste_<pid>`): o
    `painel_bi` nunca e aberto. Marker `db` so (nao `apptest`): nao precisa do
    dump, so de Postgres vivo.

O scratch cria so as colunas da tabela `ops.alteracoes_retroativas` que a
pagina le.
"""
import re
from pathlib import Path

import psycopg2
import pytest

ROOT = Path(__file__).resolve().parent.parent
PAGINA = "pages/alteracoes_retroativas.py"
APP_PY = ROOT / "app.py"


# --------------------------------------------------------------------------
# source-level
# --------------------------------------------------------------------------

def test_navegacao_automatica_desligada_no_config():
    cfg = (ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8")
    m = re.search(r"^\[client\](.*?)(?=^\[|\Z)", cfg, re.S | re.M)
    assert m, "secao [client] ausente"
    assert re.search(r"^showSidebarNavigation\s*=\s*false\s*$", m.group(1), re.M)


def test_app_tem_os_links_de_pagina_logo_abaixo_do_atualizar():
    src = APP_PY.read_text(encoding="utf-8")
    assert src.count("st.page_link(") == 2
    i_btn = src.index('etl_btn = st.button("🔄 Executar Atualização"')
    i_link = src.index(f'st.page_link("{PAGINA}"')
    i_qualidade = src.index('st.page_link("pages/qualidade_dados.py"')
    i_proximo = src.index("Seletor de MODELOS", i_btn)
    assert i_btn < i_link < i_qualidade < i_proximo


def test_rotulos_dos_links_de_navegacao():
    """O texto do link da sidebar e do link de volta.

    O AppTest RENDERIZA o st.page_link quando o entrypoint e o app.py; so a
    pagina aberta SOZINHA da KeyError `url_pathname`. Este teste trava o
    rotulo no FONTE, sem banco e sem AppTest.
    """
    app = APP_PY.read_text(encoding="utf-8")
    assert re.search(
        r'st\.page_link\("pages/alteracoes_retroativas\.py",\s*label="Alterações retroativas"\s*\)', app)
    pag = (ROOT / PAGINA).read_text(encoding="utf-8")
    assert re.search(r'st\.page_link\("app\.py",\s*label="Painel"', pag)


def test_pagina_e_read_only_e_tem_link_de_volta():
    src = (ROOT / PAGINA).read_text(encoding="utf-8")
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE)\b", src)
    assert 'st.page_link("app.py", label="Painel"' in src
    assert "Total" in src and "<> 'Total'" in src          # Total fora da pagina


# --------------------------------------------------------------------------
# AppTest da pagina (banco scratch)
# --------------------------------------------------------------------------

def _conn_scratch(nome):
    import connections
    c = psycopg2.connect(dbname=nome, host=connections.PG_HOST, port=connections.PG_PORT,
                         user=connections.PG_USER, password=connections.PG_PASS or None)
    c.autocommit = True
    return c


@pytest.fixture
def scratch(banco_scratch, monkeypatch):
    """Scratch + PG_DB no ambiente (a pagina, como o app.py, le os.getenv)."""
    monkeypatch.setenv("PG_DB", banco_scratch)
    import connections
    monkeypatch.setenv("PG_HOST", str(connections.PG_HOST))
    monkeypatch.setenv("PG_PORT", str(connections.PG_PORT))
    monkeypatch.setenv("PG_USER", str(connections.PG_USER))
    monkeypatch.setenv("PG_PASS", connections.PG_PASS or "")
    return banco_scratch


def _abre_pagina():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(APP_PY), default_timeout=60)
    at.switch_page(PAGINA)
    at.run()
    return at


def _cria_geempre(conn):
    with conn.cursor() as c:
        c.execute("CREATE SCHEMA raw")
        c.execute("CREATE TABLE raw.geempre (codi_emp int, nome_emp text)")
        c.execute("INSERT INTO raw.geempre VALUES (207, 'EMPRESA ALFA   ')")  # 234 ausente de proposito


def _cria_tabela(conn):
    with conn.cursor() as c:
        c.execute("CREATE SCHEMA IF NOT EXISTS ops")
        c.execute("""CREATE TABLE ops.alteracoes_retroativas (
            id bigserial PRIMARY KEY, run_id bigint NOT NULL,
            detectado_em timestamptz NOT NULL DEFAULT now(),
            codi_emp bigint NOT NULL, competencia text NOT NULL, numero text NOT NULL,
            valor_antes numeric(18,2), valor_depois numeric(18,2),
            diferenca numeric(18,2) NOT NULL, causa text NOT NULL)""")


@pytest.mark.db
def test_tabela_ausente_mostra_frase_e_nao_levanta(scratch):
    at = _abre_pagina()
    assert not at.exception
    assert [i.value for i in at.info] == ["Nenhuma alteração registrada ainda."]
    assert len(at.dataframe) == 0


@pytest.mark.db
def test_tabela_vazia_tambem_mostra_frase(scratch):
    c = _conn_scratch(scratch)
    _cria_geempre(c)
    _cria_tabela(c)
    at = _abre_pagina()
    assert not at.exception
    assert [i.value for i in at.info] == ["Nenhuma alteração registrada ainda."]


@pytest.mark.db
def test_com_linhas_filtra_causa_sem_total_e_ordena_detectado_desc_competencia_desc(scratch):
    c = _conn_scratch(scratch)
    _cria_geempre(c)
    _cria_tabela(c)
    # (run, detectado_em, emp, competencia, numero, antes, depois, causa)
    linhas = [
        (1, "2026-09-20 10:00:00-03", 207,  "2025-11", "NFe",      100.00, 150.50, "mudou no Dominio"),
        (1, "2026-09-20 10:00:00-03", 207,  "2025-12", "Impostos", 10.00,  25.00,  "mudou no Dominio"),
        (2, "2026-09-24 09:00:00-03", 207,  "2025-10", "NFe",      None,   10.00,  "mudou no Dominio"),
        (2, "2026-09-24 09:00:00-03", 234,  "2025-10", "Folha",    500.00, 400.00, "mudou no Dominio"),
        (2, "2026-09-24 09:00:00-03", 234,  "2025-03", "NFS-e",    1.00,   3.00,   "mudou no Dominio"),
        # fora da pagina: Total, outras causas
        (2, "2026-09-24 09:00:00-03", 234,  "2025-10", "Total",    1.00,   9.00,   "mudou no Dominio"),
        (2, "2026-09-24 09:00:00-03", 234,  "2025-10", "Total",    1.00,   9.00,   "mudou no Dominio + carga nova"),
        (2, "2026-09-24 09:00:00-03", 234,  "2025-09", "NFe",      0.00,   7.00,   "carga nova"),
        (2, "2026-09-24 09:00:00-03", 234,  "2025-08", "NFe",      1.00,   9.00,   "mudou o calculo"),
        (2, "2026-09-24 09:00:00-03", 234,  "2025-07", "NFe",      1.00,   9.00,   "inesperado"),
    ]
    with c.cursor() as cur:
        for run, det, emp, comp, num, a, d, causa in linhas:
            cur.execute(
                "INSERT INTO ops.alteracoes_retroativas (run_id, detectado_em, codi_emp, competencia,"
                " numero, valor_antes, valor_depois, diferenca, causa) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (run, det, emp, comp, num, a, d, (d or 0) - (a or 0), causa))
    at = _abre_pagina()
    assert not at.exception
    df = at.dataframe[0].value
    assert list(df.columns) == ["Atualização", "Empresa", "Número", "Mês/Ano", "Antes", "Depois"]
    esperado = [
        # mais recente DETECTADA primeiro; dentro dela, competencia DESC (empate: codi_emp)
        ("24/09/26", "EMPRESA ALFA (207)",         "NFe",                   "Out/25", "—",         "R$ 10,00"),
        ("24/09/26", "234",                        "Folha + Encargos (R$)", "Out/25", "R$ 500,00", "R$ 400,00"),
        ("24/09/26", "234",                        "NFS-e",                 "Mar/25", "R$ 1,00",   "R$ 3,00"),
        ("20/09/26", "EMPRESA ALFA (207)",         "Impostos (R$)",         "Dez/25", "R$ 10,00",  "R$ 25,00"),
        ("20/09/26", "EMPRESA ALFA (207)",         "NFe",                   "Nov/25", "R$ 100,00", "R$ 150,50"),
    ]
    assert [tuple(r) for r in df.itertuples(index=False)] == esperado
    assert [c_.value for c_ in at.caption][-1] == (
        "Valores calculados por ano-calendário; o painel com outra janela pode mostrar "
        "outro valor no mesmo mês.")
    assert len(at.info) == 0

