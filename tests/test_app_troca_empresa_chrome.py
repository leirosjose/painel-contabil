# ---------------------------------------------------------------------------
# Troca de empresa num NAVEGADOR REAL (Playwright + Chromium, sem reload).
#
# O AppTest nao reproduz os defeitos que estes testes guardam: ele escreve o
# valor direto pelo id do widget, sem passar pelo frontend. No navegador o
# valor do selectbox vai e volta pelo ROTULO (Streamlit 1.57), entao rotulo
# repetido e identidade de widget instavel so aparecem aqui.
#
# Marker `chrome` (fora do CI, como os outros testes com Chrome) e `db`: o app
# sobe num subprocesso contra o `painel_bi` com carga (`banco_carregado` pula
# se nao houver carga). Headful por padrao; PW_HEADLESS=1 roda sem janela.
# ---------------------------------------------------------------------------
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from conftest import APP_PY, ROOT, _pg_conn_app

pytestmark = [pytest.mark.chrome, pytest.mark.db]

playwright_api = pytest.importorskip("playwright.sync_api")


def _porta_livre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def empresas(banco_carregado):
    """codi_emp -> (nome, cnpj) como o app mostra no titulo e na linha do CNPJ."""
    conn = _pg_conn_app()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT codi_emp, nome_emp, cgce_emp FROM raw.geempre ORDER BY codi_emp")
            return {int(c): (str(n), str(j)) for c, n, j in cur.fetchall()}
    finally:
        conn.close()


@pytest.fixture(scope="module")
def app_url(banco_carregado):
    porta = _porta_livre()
    kw = {"start_new_session": True} if os.name == "posix" else {}
    proc = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", str(APP_PY), "--server.port", str(porta),
         "--server.headless", "true", "--browser.gatherUsageStats", "false"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    url = f"http://127.0.0.1:{porta}"
    try:
        limite = time.monotonic() + 60
        while True:
            try:
                with urllib.request.urlopen(f"{url}/_stcore/health", timeout=2) as r:
                    if r.status == 200:
                        break
            except OSError:
                pass
            if time.monotonic() > limite or proc.poll() is not None:
                pytest.fail("streamlit nao subiu em 60s")
            time.sleep(0.5)
        yield url
    finally:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=20)


@pytest.fixture
def pagina(app_url):
    with playwright_api.sync_playwright() as pw:
        nav = pw.chromium.launch(headless=os.getenv("PW_HEADLESS") == "1")
        p = nav.new_page(viewport={"width": 1440, "height": 900}, accept_downloads=True)
        p.goto(app_url)
        p.wait_for_selector("h2", timeout=60_000)
        _espera(p)
        yield p
        nav.close()


def _espera(p, timeout=60_000):
    """Espera o rerun terminar: o widget de status some quando o script para."""
    p.wait_for_timeout(400)
    p.wait_for_selector('[data-testid="stStatusWidget"]', state="detached", timeout=timeout)
    p.wait_for_timeout(600)


def _tela(p):
    """(titulo, linha do CNPJ) do cabecalho do dashboard."""
    titulo = p.locator("h2").first.inner_text().strip()
    cnpj = p.locator("p", has_text="CNPJ:").first.inner_text().strip()
    return titulo, cnpj


def _escolhe(p, texto):
    p.locator(".st-key-emp_box [data-baseweb='select']").click()
    p.get_by_role("option").filter(has_text=texto).first.click()
    _espera(p)


def _consolidacao(p):
    p.get_by_text("Mostrar grupos consolidados").click()
    _espera(p)


def _busca(p, texto):
    campo = p.get_by_placeholder("Nome ou CNPJ")
    campo.fill(texto)
    campo.press("Enter")
    _espera(p)


def _de(p, rotulo):
    # A lista de meses e virtualizada (so as opcoes visiveis existem no DOM):
    # digita o rotulo pra filtrar antes de clicar.
    p.locator(".st-key-sel_de [data-baseweb='select']").click()
    p.keyboard.type(rotulo)
    p.get_by_role("option").filter(has_text=rotulo).first.click()
    _espera(p)


def _cnpj_pontuado(d):
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"


class _Alvos:
    """O que cada empresa mostra no cabecalho (titulo, CNPJ) e no dropdown."""

    def __init__(self, emp):
        self.emp = emp

    def avulsa(self, codi):
        nome, cnpj = self.emp[codi]
        return lambda t: nome in t[0] and "GRUPO" not in t[0] and t[1].startswith(f"CNPJ: {cnpj}")

    def grupo(self):
        return lambda t: "GRUPO CONSOLIDADO" in t[0]

    def opcao(self, codi):
        return self.emp[codi][0]


@pytest.fixture
def alvos(empresas):
    return _Alvos(empresas)


def test_consolidacao_desligada_escolher_a_matriz_abre_a_matriz(pagina, alvos):
    # Matriz e filial com o mesmo nome viram dois itens avulsos quando a
    # consolidacao desliga. Com rotulo identico, o Streamlit devolvia a ULTIMA
    # opcao com aquele texto: escolher a matriz abria a filial.
    p = pagina
    _consolidacao(p)
    p.locator(".st-key-emp_box [data-baseweb='select']").click()
    nome_gama = alvos.opcao(103)
    opcoes = [o.strip() for o in p.get_by_role("option").filter(has_text=nome_gama).all_inner_texts()]
    p.get_by_role("option").filter(has_text=nome_gama).first.click()  # a matriz vem primeiro
    _espera(p)
    assert alvos.avulsa(103)(_tela(p)), (_tela(p), opcoes)
    assert len(opcoes) == 2 and len(set(opcoes)) == 2, opcoes        # a pessoa distingue as duas
    _escolhe(p, alvos.opcao(101))
    p.locator(".st-key-emp_box [data-baseweb='select']").click()
    p.get_by_role("option").filter(has_text=nome_gama).nth(1).click()
    _espera(p)
    assert alvos.avulsa(105)(_tela(p)), _tela(p)
