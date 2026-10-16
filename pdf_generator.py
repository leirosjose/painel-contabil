import asyncio
import base64
import contextlib
import html
import io
import os
import sys
import threading
import time
import warnings
import zipfile
from datetime import datetime
from pathlib import Path

import kaleido
import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
from jinja2 import Environment, FileSystemLoader

from etl.mappings import GRUPOS_BAL, NOMES_MES
from etl.queries import SQL_PASSIVO_IRPJ_CSLL, params_passivo_ir
from etl.queries import SQL_COBERTURA_EMPRESAS, carteira_visivel, params_cobertura
from etl.utils import (
    bal_grupo_serie as _bal_grupo_serie,
    cnpj_raiz as _normalize_raiz,
    marca_inativa,
    fmt_brl as _fmt_brl,
    fmt_brl_curto,
    fmt_ind as _fmt_ind,
    fmt_pct as _fmt_pct,
    p_label as _p_label,
    periods_in_range as _periods_in_range,
    safe_div as _safe_div,
)
from etl.metrics import (
    FRASE_IMP_SEM_INFO as _FRASE_IMP_SEM_INFO,
    FRASE_IMP_SO_RETENCAO as _FRASE_IMP_SO_RETENCAO,
    LINHA_ENCARGOS,
    LINHA_FOLHA_ENCARGOS,
    LINHA_PCT_FAT,
    TITULO_DESTAQUES_FOLHA,
    LINHA_PCT_IMPOSTOS,
    LINHA_TOTAL_IMPOSTOS,
    TITULO_IMPOSTOS_GUIAS,
    LUC_SEM_REGISTROS,
    NOTA_SALDO_INVERTIDO,
    NOTA_BAL_SEM_DADO_PERIODO,
    NOTA_BAL_SEM_MOVIMENTO_PERIODO,
    CTX_BALANCETE_EM_REVISAO as _CTX_BALANCETE_EM_REVISAO,
    LUC_JANELA_CURTA_MESES as _LUC_JANELA_CURTA_MESES,
    LUC_JANELA_CURTA_FRAG,
    cards_saldo_invertido,
    folha_destaques as _folha_destaques,
    folha_por_linha as _folha_por_linha,
    legenda_travessao as _legenda_travessao,
    maiores_linhas as _maiores_linhas,
    irpj_contabil_pm as _irpj_contabil_pm,
    tabela_impostos as _tabela_impostos,
    flags_inconsistencia as _flags_inconsistencia,
    custo_folha_pm,
    kpis_agregados as _kpis_agregados,
    kpis_yoy as _kpis_yoy,
    last_valid as _last_valid,
    series_mensais as _series_mensais,
)

# read_sql com conexao psycopg2 avisa que prefere SQLAlchemy; o aviso nao se aplica.
warnings.filterwarnings("ignore", message=".*SQLAlchemy.*")

# Cores das métricas — espelham --cor-* em templates/base.html.
# Plotly não lê CSS variables; constantes mantêm os hex em UM lugar só
# em Python e são referenciadas em todos os _make_* / _make_sparkline.
COR_FAT           = "#185FA5"
# Ano ANTERIOR no chart YoY de Faturamento. Fica
# aqui e nao na PALETA_SERIES porque nao e "mais uma serie": e o par fixo do
# COR_FAT, com semantica de ano-anterior, do mesmo jeito que COR_ADM/COR_DEM
# sao um par fixo de entrada/saida.
COR_FAT_ANTERIOR  = "#C0392B"
COR_IMP           = "#993C1D"
COR_FOLENC        = "#854F0B"
COR_FUNC          = "#0F6E56"

# Cores universais de movimentação (entrada/saída). NÃO são da paleta
# das métricas — semantica fixa em qualquer chart de adm/rescisão.
COR_ADM = "#2ecc71"   # admissões — verde universal
COR_DEM = "#e74c3c"   # rescisões — vermelho universal

# Paleta de charts MULTI-SERIE (folha por rubrica, tributos por mes). Tom
# fechado pra impressao P&B ainda separar as faixas; a ordem segue a da
# tabela, entao serie e linha se correspondem pela POSICAO.
# 11 cores DISTINTAS porque ciclar e defeito de leitura: com 4 cores e 6
# tributos, COFINS e ISS sairiam da mesma cor no mesmo grafico — duas series
# indistinguiveis na legenda.
PALETA_SERIES = [
    "#185FA5", "#993C1D", "#854F0B", "#0F6E56", "#5B4B8A", "#A5185F",
    "#2E7D9A", "#7A6A1B", "#4A4A4A", "#1D6B99", "#8A3B3B",
]

TEMPLATES_DIR = Path(__file__).parent / "templates"

# ── Registro de modelos de PDF ───────────────────────────────────────────────
# FONTE UNICA de "quais paginas existem". Todo ponto de geracao (botao
# individual, selecao por multiselect, lote, CLI) passa o MESMO kwarg
# `modelos` pelo funil unico
# (gerar_pdf_empresa / gerar_todos_pdfs -> _build_html_for_empresa ->
# _render_html) e nenhum deles carrega logica propria.
#
# A ordem da tupla E a ordem das paginas no PDF. `key` e o identificador
# estavel (vai pro session_state do Streamlit e pra CLI); `label` e o texto
# que o usuario le no seletor.
MODELOS: tuple[dict[str, str], ...] = (
    {"key": "fiscal",   "label": "Fiscal",                   "template": "fiscal.html"},
    {"key": "contabil", "label": "Contabil",                 "template": "contabil.html"},
    {"key": "impostos", "label": "Detalhamento de Impostos",  "template": "impostos.html"},
    {"key": "folha",    "label": "Detalhamento de Folha",     "template": "folha.html"},
)

# Default = o registro INTEIRO. Quando o seletor nao existe (CLI sem flag) ou
# nao foi ligado, todo ponto de geracao produz todos os modelos — o seletor
# so ESTREITA, nunca e pre-requisito pra gerar.
MODELOS_KEYS: tuple[str, ...] = tuple(m["key"] for m in MODELOS)


def _resolve_modelos(modelos=None):
    """Chaves pedidas -> lista de templates, na ordem do REGISTRO.

    `None` (default) devolve o registro inteiro. A ordem de `modelos` e
    ignorada de proposito: a sequencia das paginas e um contrato do produto,
    nao do call site — dois botoes nunca podem produzir a mesma selecao em
    ordens diferentes.

    Chave desconhecida ou selecao VAZIA levantam ValueError nomeando as
    chaves validas. Degradar em silencio aqui produziria um PDF sem paginas
    (ou sem a pagina pedida) que so seria descoberto na frente do cliente.
    """
    if modelos is None:
        escolhidas = set(MODELOS_KEYS)
    else:
        escolhidas = {str(k) for k in modelos}
        desconhecidas = sorted(escolhidas - set(MODELOS_KEYS))
        if desconhecidas:
            raise ValueError(
                f"Modelo(s) de PDF desconhecido(s): {', '.join(desconhecidas)}. "
                f"Validos: {', '.join(MODELOS_KEYS)}."
            )
        if not escolhidas:
            raise ValueError(
                "Nenhum modelo de PDF selecionado. "
                f"Validos: {', '.join(MODELOS_KEYS)}."
            )
    return [m["template"] for m in MODELOS if m["key"] in escolhidas]


# Janela ate este tamanho renderiza a faixa de Faturamento do fiscal em MEIA
# largura, centrada, em vez de full-width: com 1-3 meses o bloco cheio vira
# oceano vazio em volta de poucas barras. Complementar ao _bargap_faixa — aquele mantem a
# BARRA em proporcao, este encolhe o BLOCO. O limiar vive aqui porque o
# template e o render precisam concordar: se divergirem, o PNG letterboxa.
FAIXA_CURTA_MAX_MESES = 3

# Tamanhos de render dos charts das paginas de DETALHAMENTO. Uma constante por
# FORMA DE SLOT (nao por pagina): mexer no layout de uma pagina nao pode mudar
# a outra em silencio. Meio = metade da largura util (~141mm); cheio = ~285mm.
_CHART_DETALHE_MEIO = {"width": 500, "height": 270}
_CHART_DETALHE_CHEIO = {"width": 1000, "height": 270}
# Terceira FORMA: chart cheio e SOZINHO numa row que cresce — o do
# Detalhamento de Impostos. A row leva toda a sobra vertical da pagina, entao
# o slot e bem mais alto que o do `_CHEIO` (que divide a altura com a legenda
# e a tabela da pagina de folha).
# O aspecto do slot (caixa do `.block-chart`) varia com o TAMANHO DA TABELA
# abaixo — de ~2,3 com 1 tributo a ~2,8 com 6. Escolhido LIGEIRAMENTE ACIMA
# do maior: com o aspecto do PNG >= o do slot, o
# `object-fit: contain` limita pela LARGURA e a sobra vira folga em cima e
# embaixo. Abaixo do maior, a sobra viraria margem LATERAL — chart mais
# estreito que o bloco, flutuando, que le como desalinhamento.
_CHART_DETALHE_FAIXA = {"width": 1000, "height": 345}
# QUARTA FORMA: faixa cheia numa pagina com DUAS tabelas abaixo do chart — o
# Detalhamento de Folha, com o detalhe num bloco proprio embaixo do total.
# Duas tabelas comem mais altura que uma, e o slot fica mais BAIXO (aspecto
# mais alto) que o da pagina de impostos: ~2,9 a ~3,1 na caixa do
# `.block-chart`, contra ~2,4 a ~2,8 do slot de impostos. Nao
# da pra reusar o `_FAIXA` (2,90): abaixo do aspecto do slot, o
# `object-fit: contain` limita pela ALTURA e a sobra vira margem LATERAL, com
# o chart flutuando. Escolhido LIGEIRAMENTE acima do maior medido, que e a
# mesma regra do `_FAIXA`.
_CHART_DETALHE_FAIXA_BAIXA = {"width": 1000, "height": 320}
# QUINTA FORMA: o slot de MEIA LARGURA do fiscal — o chart de Folha, que
# divide a row com a tabela de Movimentacao de Pessoal. O default do
# `_fig_to_b64` (500x250) nao cabe 24 barras ROTULADAS (12 meses x 2 anos).
# Quando o rotulo nao cabe, o Plotly ENCOLHE o texto sozinho, e ele le como
# "fonte ilegivel" e nao como "rotulo cortado".
#
# A caixa do `.block-chart` mede 533 x 276 = **1,93**, estavel — a row e
# fixa, o slot nao respira com o conteudo como os dos detalhamentos. A largura
# logica vai a 1000, a MESMA do slot cheio, porque o que resolve a compressao
# do texto e espaco LOGICO por barra, nao resolucao de saida; a altura sai do
# aspecto medido (1000 / 1,93 = 518).
#
# Nome por FORMA, nunca por pagina.
_CHART_MEIA_LARGURA = {"width": 1000, "height": 518}

# Rotulo unico de card inconsistente (centralizado: o wording muda so aqui)
CTX_INCONSISTENTE = "inconsistente"

_jinja_env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=False)


# ── Helpers ──────────────────────────────────────────────────────────────────

_RANKING_DESTAQUE = ("maior item do período", "2º maior item", "3º maior item")


def _card_destaque(rotulo, total, posicao):
    """Card de destaque das faixas dos detalhamentos: rotulo + total + ranking.

    Paleta NEUTRA de proposito: as cores dos cards do hero sao semanticas (fat,
    imp, folenc, func) e pintar um tributo de "azul do faturamento" seria
    ruido. Sem sparkline — a serie de cada item esta no chart logo abaixo.

    O contexto DECLARA que o numero e um item do ranking (nomear o que o
    numero E). Sem ele, na pagina de folha o card da linha "Folha" ficaria ao
    lado do card de % que cita o custo total de folha — a mesma palavra com
    dois sentidos, lado a lado. Um e a LINHA da tabela, o outro e o custo TOTAL.
    """
    return {"key": "destaque", "label": rotulo, "value": _fmt_brl(total),
            "spark": "",
            "context": _RANKING_DESTAQUE[posicao] if posicao < len(_RANKING_DESTAQUE)
                       else f"{posicao + 1}º maior item"}


def _yoy_frag(card, ref, unit):
    """Fragmento YoY do kpi-context ('' quando o proprio atual nao tem KPI).

    `card` = dict de kpis_yoy (delta XOR label). Delta vira
    "em relação ao mesmo período/{ref}: {seta}{valor}{unit}"; label sai
    verbatim ("sem dados de {ref}"). Sem "vs": abreviação de memorando não
    é vocabulário de relatório financeiro — o padrão do setor (ex.: releases
    de resultado de banco) é a frase por extenso.
    """
    if card["label"]:
        return card["label"]
    if card["delta"] is None:
        return ""
    seta = "↑" if card["delta"] >= 0 else "↓"
    val = f"{abs(card['delta']):.1f}".replace(".", ",")
    # &nbsp; entre "em" e "relação": sem ele a quebra do kpi-context caía
    # exatamente aí no card mais carregado (% Impostos), deixando o "em"
    # pendurado no fim da linha. Entidade funciona porque o env do Jinja
    # tem autoescape=False por design (ver _render_html).
    return f"em&nbsp;relação ao mesmo período/{ref}: {seta}{val}{unit}"


# ── Chart helpers ────────────────────────────────────────────────────────────

_CHART_BG = "rgba(0,0,0,0)"
_CHART_PLOT_BG = "#f8f8f8"


_kaleido_server_lock = threading.Lock()
_kaleido_server_ok = False

# Layouts que `playwright install` cria dentro de chromium-<rev>/.
_PLAYWRIGHT_CHROME_GLOBS = (
    "chrome-win*/chrome.exe",
    "chrome-linux*/chrome",
    "chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/"
    "Google Chrome for Testing",
)


def _revisao_pinada_chromium():
    """Le do `browsers.json` do proprio pacote instalado a revisao do
    chromium que o `playwright==1.59.0` pina. Sem isto o
    resolver nao tem como distinguir uma revisao antiga/nova deixada no
    disco por outro `playwright install` da que o requirements realmente
    contratou -- devolve None se o arquivo nao existir ou vier no formato
    inesperado, e o chamador cai no fallback de sempre (maior revisao).
    """
    import json

    import playwright

    try:
        caminho = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
        dados = json.loads(caminho.read_text())
    except (OSError, ValueError):
        return None
    for navegador in dados.get("browsers", []):
        if navegador.get("name") == "chromium":
            sufixo = navegador.get("revision", "")
            return int(sufixo) if str(sufixo).isdigit() else None
    return None


def _resolver_chrome():
    """Devolve o chrome PINADO que o kaleido deve usar, ou None.

    Sem isto o choreographer resolve sozinho e pode cair no ULTIMO degrau
    da cascata dele: os `typical_paths` hardcoded, ou seja o browser do
    SISTEMA, sob auto-update do Google -- uma atualizacao pode quebrar esse
    binario e travar o PDF com BrowserFailedError. O kaleido NAO le
    PLAYWRIGHT_BROWSERS_PATH.

    A cascata aqui e explicita:
      1) CHROME_PATH -- escape hatch manual, verbatim;
      2) o chromium do playwright (PLAYWRIGHT_BROWSERS_PATH), que ja esta
         instalado, ja e Chrome for Testing e ja vem pinado pelo
         `playwright==1.59.0` do requirements -- um browser so pros DOIS
         motores de PDF;
      3) None -- devolve o comportamento padrao (find_browser do
         choreographer), pra ambiente sem a variavel seguir funcionando.

    Glob de filesystem de proposito, NUNCA
    `sync_playwright().chromium.executable_path`: aquele exige subir o
    driver node, e isto roda DENTRO do Streamlit -- Sync API do playwright
    em thread com loop asyncio e modo de falha conhecido.
    """
    manual = os.environ.get("CHROME_PATH", "").strip()
    if manual:
        return manual

    raiz = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if not raiz:
        return None
    base = Path(raiz)
    if not base.is_dir():
        return None

    def _rev(pasta):
        sufixo = pasta.name.rpartition("-")[2]
        return int(sufixo) if sufixo.isdigit() else -1

    pinada = _revisao_pinada_chromium()

    def _prioridade(pasta):
        # A revisao PINADA vence sempre que presente; entre as
        # demais, a MAIOR primeiro -- o install antigo fica ao lado do novo.
        rev = _rev(pasta)
        eh_pinada = 0 if pinada is not None and rev == pinada else 1
        return (eh_pinada, -rev)

    # `chromium-*` NAO casa com `chromium_headless_shell-*` (underscore), e
    # isso e proposital: o headless shell nao serve pro choreographer.
    for pasta in sorted(
        (d for d in base.glob("chromium-*") if d.is_dir()),
        key=_prioridade,
    ):
        for padrao in _PLAYWRIGHT_CHROME_GLOBS:
            for exe in sorted(pasta.glob(padrao)):
                if exe.is_file():
                    return str(exe)
    return None


# ── Blindagem do kaleido ─────────────────────────────────────────────────────
# Tres defeitos do server persistente:
#   (1) a lib faz put -> join -> get sem correlacionar task e resultado
#      (kaleido/_sync_server.py:120-122): duas sessoes renderizando ao mesmo
#      tempo TROCAM o PNG entre si. Cura: lock em volta do
#      render. Custo de vazao zero -- o server ja processa uma imagem por vez.
#   (2) start_sync_server volta antes de o Chrome subir; se ele nao sobe, o
#      join da lib espera pra sempre. Cura: smoke render com watchdog antes de
#      declarar o server pronto.
#   (3) depois que o Chrome morre, o 1o render levanta e o 2o TRAVA (a espera
#      pela aba fica fora do timeout da lib, kaleido.py:307 x :358); o
#      stop_sync_server tambem trava. Cura: escada server -> oneshot -> "" com
#      watchdog do CHAMADOR em toda chamada.
# O oneshot usa kaleido.calc_fig direto (API publica) e NAO pio.to_image: este
# roteia pro singleton enquanto ele existe (kaleido/__init__.py:167) e cairia
# no mesmo hang.
#
# Timeouts calibrados por dado de campo. Recalibrar com a linha
# "[kaleido] frio:" que o start loga a cada boot do servico.
# N_START: ~5x o maior Chrome frio observado (~6s); abaixo dos 90s da lib.
KALEIDO_N_START_S = 30.0
# M_RENDER: render quente medido ~0,05s => 30s e margem > 100x.
KALEIDO_M_RENDER_S = 30.0
# N_ONESHOT: Chrome frio por imagem (~10s), com margem.
KALEIDO_N_ONESHOT_S = 45.0
KALEIDO_T_STOP_S = 5.0      # stop_sync_server: 0,001-0,2s medido; acima disso travou
KALEIDO_COOLDOWN_S = 600.0  # re-sonda um degrau acima, so na fronteira de PDF

# Ordem de aquisicao FIXA: _kaleido_render_lock -> _kaleido_server_lock.
# O render lock NAO e reentrante de proposito: smoke e render usam as
# primitivas `_render_server` / `_render_oneshot`, nunca `_fig_to_b64`.
_kaleido_render_lock = threading.Lock()
_kaleido_modo = "server"            # "server" | "oneshot" | "off"
_kaleido_motivo = ""
_kaleido_desde = None
_kaleido_ultima_tentativa = 0.0     # monotonic da ultima transicao
_kaleido_ultimo_restart = None      # monotonic do ultimo restart do singleton
_kaleido_server_ja_subiu = False    # a instancia ATUAL passou no smoke?
_kaleido_singleton_preso = False    # um stop nao voltou: server perdido no processo


class _KaleidoTimeout(Exception):
    """A chamada ao kaleido nao voltou dentro do watchdog."""


class LoteAbortadoKaleido(RuntimeError):
    """O lote parou porque os graficos sairam do modo server.

    Nao ha CLI de lote: quem chama e o app. `exit_code` = 2 e o codigo que um
    chamador de linha de comando deve devolver. Carrega o ZIP do que JA saiu,
    pra nada gerado corretamente ser descartado.
    """

    exit_code = 2

    def __init__(self, modo, motivo, zip_bytes, sucessos, erros, pendentes):
        self.modo = modo
        self.motivo = motivo
        self.zip_bytes = zip_bytes
        self.sucessos = sucessos
        self.erros = erros
        self.pendentes = pendentes
        super().__init__(
            f"Lote abortado: os graficos sairam do modo server "
            f"(modo={modo}: {motivo}). {sucessos} PDF(s) gerado(s) antes da "
            f"queda; {len(pendentes)} unidade(s) nao geradas. Reinicie o "
            f"servico e gere o lote de novo.")


def kaleido_estado():
    """Estado da escada de render dos graficos: {"modo", "motivo", "desde"}.

    Leitura sem lock de proposito: quem pergunta e a tela, e o lock pode estar
    seguro por um render de ate KALEIDO_N_START_S.
    """
    return {"modo": _kaleido_modo, "motivo": _kaleido_motivo,
            "desde": _kaleido_desde}


_MOTIVO_MAX = 120


def _motivo_curto(motivo):
    """1a frase do motivo, no maximo _MOTIVO_MAX caracteres, parenteses fechados.

    O motivo carrega str(e) de TERCEIRO: a mensagem inteira do kaleido pode
    vir duas vezes (render + smoke) e esticar a sidebar. O motivo
    completo segue em kaleido_estado() e no stderr -- so a TELA encurta.
    """
    m = " ".join(str(motivo).split())
    corte = m.find(". ")
    fim = min(corte + 1 if corte != -1 else len(m), _MOTIVO_MAX)
    if fim >= len(m):
        return m
    curto = m[:fim].rstrip() + "…"
    return curto + ")" * max(0, curto.count("(") - curto.count(")"))


def aviso_kaleido(estado, lote=None):
    """Aviso de TELA sobre o modo dos graficos: (nivel, texto) ou None.

    Quem le e CONTADOR, so com navegador. Estrutura fixa: o que houve -> o PDF
    serve? -> o que fazer. Sem termo tecnico e sem pedir reinicio de servico
    (quem le nao tem a maquina): o motivo tecnico vai pra `detalhe_kaleido`,
    e a instrucao de reinicio vive no gen_pdf.py e no stderr.
    `nivel` e o nome do metodo do Streamlit: oneshot sai correto e lento
    ("warning"); off sai sem grafico ("error"). `lote` = (gerados, com_erro,
    nao_gerados) quando o lote abortou; as pendentes sao contagem PROPRIA,
    nunca erro.
    """
    if not estado or estado.get("modo") == "server":
        return None
    lento = estado.get("modo") == "oneshot"
    if lote is not None:
        gerados, com_erro, nao_gerados = lote
        contagem = (f"PDFs: {gerados} {'gerado' if gerados == 1 else 'gerados'}, "
                    f"{com_erro} com erro, {nao_gerados} "
                    f"{'não gerado' if nao_gerados == 1 else 'não gerados'}.")
        if lento:
            return ("warning",
                    f"O lote foi interrompido porque os gráficos passaram para o "
                    f"modo lento. {contagem} Os que saíram no ZIP estão corretos e "
                    f"podem ser usados normalmente. Avise o responsável pelo "
                    f"sistema antes de gerar os que faltaram.")
        saiu = (" Os que saíram no ZIP estão corretos e podem ser usados."
                if gerados else "")
        return ("error",
                f"O lote foi interrompido: não foi possível gerar os gráficos. "
                f"{contagem}{saiu} Avise o responsável pelo sistema antes de "
                f"gerar o lote de novo.")
    if lento:
        return ("warning",
                "Os gráficos estão sendo gerados no modo lento — cada PDF demora "
                "alguns minutos. Os PDFs saem corretos e podem ser usados "
                "normalmente. Avise o responsável pelo sistema quando puder.")
    return ("error",
            "Não foi possível gerar os gráficos: este PDF saiu sem eles. Não use "
            "este arquivo com o cliente. Avise o responsável pelo sistema.")


def detalhe_kaleido(estado):
    """Motivo TECNICO curto, pro expander fechado "Detalhes técnicos" da tela.

    O motivo inteiro segue em kaleido_estado() e no stderr; a tela so mostra a
    1a frase (`_motivo_curto`), e so pra quem abrir o expander.
    """
    if not estado or estado.get("modo") == "server":
        return None
    return _motivo_curto(estado.get("motivo") or "sem detalhe")


def _com_watchdog(fn, limite_s):
    """Roda fn numa thread daemon e espera ate limite_s.

    Estourou -> _KaleidoTimeout. A thread fica presa DENTRO da lib, sem
    segurar lock nosso; o chamador segue e libera o que tiver.
    """
    res = {}

    def alvo():
        try:
            res["v"] = fn()
        except BaseException as e:  # noqa: BLE001 -- repassada ao chamador
            res["e"] = e

    t = threading.Thread(target=alvo, daemon=True, name="kaleido-watchdog")
    t.start()
    t.join(limite_s)
    if t.is_alive():
        raise _KaleidoTimeout(f"sem resposta em {limite_s:g}s")
    if "e" in res:
        raise res["e"]
    return res["v"]


def _render_server(fig, width, height):
    return pio.to_image(fig, format="png", width=width, height=height,
                        scale=2)


def _render_oneshot(fig, width, height):
    """Um Chrome por imagem, fora do singleton. Mesmo binario do server."""
    exe = _resolver_chrome()
    kopts = {"path": exe} if exe else {}
    return asyncio.run(kaleido.calc_fig(
        fig.to_dict(),
        opts=dict(format="png", width=width, height=height, scale=2),
        topojson=None, kopts=kopts))


def _kaleido_smoke(render, limite_s):
    """Figura minima ponta a ponta: Chrome + aba + plotly.js + PNG de volta."""
    png = _com_watchdog(lambda: render(go.Figure(go.Bar(y=[1])), 64, 64),
                        limite_s)
    if not png:
        raise RuntimeError("smoke devolveu PNG vazio")


def _kaleido_transicao(modo, motivo):
    global _kaleido_modo, _kaleido_motivo, _kaleido_desde
    global _kaleido_ultima_tentativa
    anterior = _kaleido_modo
    _kaleido_modo = modo
    _kaleido_motivo = motivo
    _kaleido_desde = datetime.now().isoformat(timespec="seconds")
    _kaleido_ultima_tentativa = time.monotonic()
    print(f"[kaleido] {anterior} -> {modo}: {motivo}", file=sys.stderr)


def _ensure_kaleido_server():
    """Liga o server persistente do kaleido — 1 Chrome por processo, lazy.

    Sem ele, cada pio.to_image faz oneshot: Chrome novo POR IMAGEM, que
    domina o tempo de geracao do PDF.
    O server so vale como pronto DEPOIS do smoke: o start da lib volta
    antes de o Chrome subir. Smoke reprovado levanta, e quem decide o degrau
    e `_kaleido_render`. Parar o server so acontece no descarte, sob o render
    lock -- nunca no meio do render de outra aba.
    """
    global _kaleido_server_ok, _kaleido_server_ja_subiu
    if _kaleido_server_ok:
        return
    with _kaleido_server_lock:
        if not _kaleido_server_ok:
            exe = _resolver_chrome()
            t_frio = time.monotonic()
            if exe:
                kaleido.start_sync_server(path=exe, silence_warnings=True)
            else:
                kaleido.start_sync_server(silence_warnings=True)
            # Uma linha, sempre: sem ela a escolha do binario fica
            # INVISIVEL, e um auto-update aparece como "o PDF travou" em vez
            # de "trocaram o meu browser".
            print(f"[kaleido] chrome: {exe or 'auto (choreographer)'}",
                  file=sys.stderr)
            _kaleido_server_ja_subiu = False
            _kaleido_smoke(_render_server, KALEIDO_N_START_S)
            # Regua de producao das constantes KALEIDO_*_S: start + smoke = Chrome frio.
            print(f"[kaleido] frio: {time.monotonic() - t_frio:.2f}s", file=sys.stderr)
            _kaleido_server_ja_subiu = True
            _kaleido_server_ok = True


def _descartar_server(motivo):
    """Tira o server de uso e escolhe o proximo degrau. Chamar sob o render lock.

    Restart so quando a instancia JA tinha passado no smoke (morreu no meio) e
    o stop voltou; uma vez por cooldown. Chrome que nunca subiu nao ganha
    segunda tentativa de 45s -- vai direto pro oneshot.
    """
    global _kaleido_server_ok, _kaleido_singleton_preso, _kaleido_ultimo_restart
    _kaleido_server_ok = False
    try:
        _com_watchdog(lambda: kaleido.stop_sync_server(silence_warnings=True),
                      KALEIDO_T_STOP_S)
        parou = True
    except Exception:  # noqa: BLE001 -- timeout OU erro: estado incerto
        parou = False
        _kaleido_singleton_preso = True
    agora = time.monotonic()
    pode_reiniciar = (
        parou and _kaleido_server_ja_subiu
        and (_kaleido_ultimo_restart is None
             or agora - _kaleido_ultimo_restart >= KALEIDO_COOLDOWN_S))
    if pode_reiniciar:
        _kaleido_ultimo_restart = agora
        _kaleido_transicao("server", f"{motivo}; reiniciando o server")
    else:
        extra = "" if parou else "; server preso ate reiniciar o processo"
        _kaleido_transicao("oneshot", f"{motivo}{extra}")


def _kaleido_render(fig, width, height):
    """PNG pelo degrau atual, descendo a escada quando ele falha.

    Chamar sob `_kaleido_render_lock`. Devolve bytes, None (modo off) ou
    levanta o erro da FIGURA (o smoke passou: o Chrome esta bom). A figura que
    derrubou um degrau e tentada de novo no degrau de baixo.
    """
    if _kaleido_modo == "server":
        try:
            _ensure_kaleido_server()
        except Exception as e:  # noqa: BLE001
            _descartar_server(f"Chrome nao subiu ({e})")
            return _kaleido_render(fig, width, height)
        try:
            return _com_watchdog(lambda: _render_server(fig, width, height),
                                 KALEIDO_M_RENDER_S)
        except _KaleidoTimeout as e:
            # Sem segunda chance: a task abandonada ficou na fila da lib e o
            # proximo get receberia o resultado dela (PNG trocado de novo).
            _descartar_server(f"render travou ({e})")
            return _kaleido_render(fig, width, height)
        except Exception as e:  # noqa: BLE001
            try:
                _kaleido_smoke(_render_server, KALEIDO_M_RENDER_S)
            except Exception as e2:  # noqa: BLE001
                _descartar_server(f"render falhou ({e}) e o smoke tambem ({e2})")
                return _kaleido_render(fig, width, height)
            raise
    if _kaleido_modo == "oneshot":
        try:
            return _com_watchdog(lambda: _render_oneshot(fig, width, height),
                                 KALEIDO_N_ONESHOT_S)
        except _KaleidoTimeout as e:
            _kaleido_transicao("off", f"oneshot travou ({e})")
            return None
        except Exception as e:  # noqa: BLE001
            try:
                _kaleido_smoke(_render_oneshot, KALEIDO_N_ONESHOT_S)
            except Exception as e2:  # noqa: BLE001
                _kaleido_transicao(
                    "off", f"oneshot falhou ({e}) e o smoke tambem ({e2})")
                return None
            raise
    return None


def _kaleido_resondar():
    """Fronteira de PDF: degradado ha mais de KALEIDO_COOLDOWN_S, tenta subir UM degrau.

    off -> oneshot (smoke); oneshot -> server so se o singleton nao ficou
    preso. Nunca no meio de um PDF.
    """
    with _kaleido_render_lock:
        if _kaleido_modo == "server":
            return
        if time.monotonic() - _kaleido_ultima_tentativa < KALEIDO_COOLDOWN_S:
            return
        if _kaleido_modo == "oneshot":
            if _kaleido_singleton_preso:
                return
            _kaleido_transicao("server", "re-sonda apos cooldown")
            try:
                _ensure_kaleido_server()
            except Exception as e:  # noqa: BLE001
                _descartar_server(f"re-sonda falhou ({e})")
            return
        try:
            _kaleido_smoke(_render_oneshot, KALEIDO_N_ONESHOT_S)
        except Exception as e:  # noqa: BLE001
            _kaleido_transicao("off", f"re-sonda falhou ({e})")
            return
        _kaleido_transicao("oneshot", "re-sonda apos cooldown")


class _ContadorCharts:
    """Conta quantas vezes `_fig_to_b64` foi CHAMADO (= quantos charts a
    pagina decidiu que tinha dado pra desenhar, ja que so ha chamada quando
    existe uma `fig` valida — o `if X.notna().any() else ""` de cada chart
    decide isso ANTES de chamar) e quantas vieram vazios. "Esperado" nunca e
    numero cravado: e o proprio total de chamadas desta pagina."""

    def __init__(self):
        self.tentativas = 0
        self.falhas = 0

    def registra(self, ok):
        self.tentativas += 1
        if not ok:
            self.falhas += 1


# Thread-local, NAO global simples: duas sessoes (Streamlit + subprocess de
# lote, ou duas abas) podem chamar `_fig_to_b64` ao mesmo tempo — e' o MESMO
# fato que motiva o `_kaleido_render_lock` — e um global compartilhado
# deixaria uma sessao contar os charts da OUTRA. `threading.local()` da uma
# copia de `atual` por thread, sem mudar a assinatura de `_build_html_for_empresa`
# nem dos call sites em `gerar_pdf_empresa`/`gerar_todos_pdfs`.
_contador_charts_tls = threading.local()


@contextlib.contextmanager
def _contando_charts():
    """Ativa o `_ContadorCharts` do build de UMA pagina NESTA THREAD (nao
    aninha dentro da mesma thread: builds de pagina nao chamam build de
    pagina)."""
    anterior = getattr(_contador_charts_tls, "atual", None)
    contador = _ContadorCharts()
    _contador_charts_tls.atual = contador
    try:
        yield contador
    finally:
        _contador_charts_tls.atual = anterior


def _fig_to_b64(fig, width=500, height=250):
    resultado = ""
    try:
        with _kaleido_render_lock:
            png = _kaleido_render(fig, width, height)
        if png:
            resultado = base64.b64encode(png).decode()
    except Exception as e:
        title = ""
        try:
            title = (fig.layout.title.text or "").strip() if fig.layout.title else ""
        except Exception:
            pass
        print(f"[chart fail] {title or 'untitled'}: {e}", file=sys.stderr)
    contador = getattr(_contador_charts_tls, "atual", None)
    if contador is not None:
        contador.registra(bool(resultado))
    return resultado


def _chart_layout(fig, tick_angle=0, n_labels=12):
    fig.update_layout(
        margin=dict(l=60, r=10, t=8, b=30),
        paper_bgcolor=_CHART_BG,
        plot_bgcolor=_CHART_PLOT_BG,
        font=dict(size=8, color="#333"),
        xaxis=dict(
            tickfont=dict(size=6 if n_labels <= 12 else 5),
            tickangle=tick_angle,
            gridcolor="#eee",
        ),
        yaxis=dict(
            tickfont=dict(size=7),
            gridcolor="#e0e0e0",
            tickformat=",.0f",
        ),
        bargap=0.15,
        showlegend=False,
    )
    return fig


# Tamanho do rotulo em cima da barra. CONSTANTE, nao rampa por densidade: o
# PDF rejeita janela acima de 12 meses no proprio ponto de entrada
# (gerar_pdf_empresa, ValueError mais abaixo neste arquivo), entao o caso mais
# denso e o chart YoY de Faturamento com 24 barras (12 meses x 2 anos), que
# cabe com este tamanho sem colisao de rotulo. O pior caso e a empresa cujas 24
# barras caem TODAS na casa dos milhares (rotulos "NNN mil" lado a lado), e ele
# passa sem sobreposicao. Empresa com valores na casa dos milhoes nao e o caso
# denso: "6,2M" e mais CURTO que "988 mil".
# 8 e o mesmo tamanho da fonte base do chart (_chart_layout).
_FONTE_ROTULO_BARRA = 8


def _make_bar(labels, values, color, tick_angle=0, rotulos=False):
    """Barra mensal de UMA serie, sem legenda. `rotulos=True` escreve o valor
    abreviado em cima de cada barra.

    O `rotulos` nao tem call site hoje: os charts rotulados usam o
    `_make_bar_yoy`, que rotula sempre. O parametro FICA — e opcao de
    builder, nao codigo morto de feature — mas e opt-in: ligar pra todo mundo
    encheria o chart que ja tem tabela de valores logo abaixo.

    O Streamlit NAO recebe rotulo: la a janela vai a 48
    meses e o problema seria outro.
    """
    txt = [fmt_brl_curto(v) for v in values] if rotulos else None
    fig = go.Figure(go.Bar(x=labels, y=values, marker_color=color,
                           text=txt, textposition="outside",
                           textfont=dict(size=_FONTE_ROTULO_BARRA),
                           cliponaxis=False))
    _chart_layout(fig, tick_angle, len(labels))
    if rotulos:
        # O rotulo da barra mais alta encosta no topo do plot e o Kaleido
        # CORTA sem avisar (mesma classe do overflow:hidden da .page). 12% de
        # folga resolve sem mexer no layout dos outros charts.
        _vals = [float(v) for v in values if v is not None and v == v]
        if _vals and max(_vals) > 0:
            fig.update_yaxes(range=[0, max(_vals) * 1.12])
    # Janela curta nao vira barra-paredao. Vale pra TODO chart de barra
    # simples, nao so pro full-width: o defeito e da JANELA, nao do slot — no
    # fiscal de 1 mes o Faturamento corrigido ficaria ao lado de uma Folha
    # solida, dois tratamentos na mesma pagina. N>=12 devolve exatamente o
    # bargap historico, entao a janela cheia fica byte-identica.
    fig.update_layout(bargap=_bargap_faixa(len(labels)))
    return fig


def _make_bar_yoy(labels, vals_atual, vals_anterior, nome_atual, nome_anterior,
                  tick_angle=0, *, tem_atual=True, tem_anterior=True):
    """Faturamento do fiscal em YoY — barras AGRUPADAS por mes.

    Ano anterior em vermelho, ano da janela em azul, legenda com os dois nomes
    em cima e o valor abreviado sobre CADA barra.

    **NAO e uma variante do `_make_bar`, e isso e o ponto.** Se o YoY morasse
    dentro do builder compartilhado (ligado por sniff de `rotulos=True`), outro
    chart que pedisse rotulo em cima da barra (ex.: Detalhamento de Folha)
    viraria comparacao contra a serie de FATURAMENTO do ano anterior — numero
    errado e MUDO. Cada chart YoY entra por CALL SITE PROPRIO, e o
    discriminador estrutural vale tambem pra tentacao inversa: o `_make_bar` ainda serve chart SEM serie do ano anterior
    carregada (Despesas e Custos le o balancete, que a janela -12m nao carrega
    de proposito), e absorver o YoY o faria comparar contra vazio, calado.

    `tem_atual` / `tem_anterior` decidem quais series ENTRAM. Ano ausente nao
    vira barra de altura zero nem chip na legenda — ele simplesmente nao
    existe no chart, entao empresa sem historico anterior le como o chart de
    sempre. Devolve None quando nenhum dos dois entra, que e o UNICO caso em
    que a pagina exibe "Sem dados de faturamento".
    """
    series = []
    if tem_anterior:
        series.append((nome_anterior, vals_anterior, COR_FAT_ANTERIOR))
    if tem_atual:
        series.append((nome_atual, vals_atual, COR_FAT))
    if not series:
        return None

    fig = go.Figure()
    for nome, valores, cor in series:
        fig.add_trace(go.Bar(
            name=nome, x=labels, y=valores, marker_color=cor,
            text=[fmt_brl_curto(v) for v in valores], textposition="outside",
            textfont=dict(size=_FONTE_ROTULO_BARRA), cliponaxis=False))

    _chart_layout(fig, tick_angle, len(labels))
    # Legenda DEPOIS do _chart_layout — ele seta showlegend=False por default e
    # a ordem invertida ENGOLE a legenda (mesma armadilha do _make_grouped_bar).
    # t=20 abre espaco pra legenda horizontal.
    fig.update_layout(
        barmode="group",
        showlegend=True,
        legend=dict(font=dict(size=7), orientation="h",
                    y=1.12, x=0.5, xanchor="center"),
        margin=dict(t=20),
    )
    # `_bargap_faixa` fixa a largura da BARRA, e o slot do mes se divide entre
    # as series PRESENTES: o argumento e n_series * n_labels. Com uma serie so
    # isso devolve exatamente o bargap do chart antigo — sem isso a empresa sem
    # ano anterior ganharia barra com o dobro da largura e um vao fantasma no
    # lugar da vermelha que nao veio.
    fig.update_layout(bargap=_bargap_faixa(len(series) * len(labels)))
    # Mesma folga de 12% do `_make_bar` com rotulo: o Kaleido corta o rotulo da
    # barra mais alta sem avisar (mesma classe do overflow:hidden da .page).
    _vals = [float(v) for _n, _serie, _c in series for v in _serie
             if v is not None and v == v]
    if _vals and max(_vals) > 0:
        fig.update_yaxes(range=[0, max(_vals) * 1.12])
    return fig


def _make_grouped_bar(labels, traces, tick_angle=0):
    fig = go.Figure()
    for name, vals, color in traces:
        fig.add_trace(go.Bar(name=name, x=labels, y=vals, marker_color=color))
    _chart_layout(fig, tick_angle, len(labels))
    # Legenda DEPOIS do _chart_layout — ele seta showlegend=False por default
    # e a ordem invertida ENGOLE a legenda deste builder.
    # t=20 abre espaço pra legenda horizontal acima do plot.
    fig.update_layout(
        barmode="group",
        showlegend=True,
        legend=dict(font=dict(size=7), orientation="h",
                    y=1.12, x=0.5, xanchor="center"),
        margin=dict(t=20),
    )
    return fig


# Largura de barra que 12 meses produzem com o bargap historico (0.15): a
# barra ocupa 0,85 do slot e o slot e 1/12 da faixa. E o alvo que a janela
# curta persegue — assim N=12 cai EXATAMENTE no comportamento de antes.
_BARGAP_HISTORICO = 0.15
_LARGURA_ALVO = (1.0 - _BARGAP_HISTORICO) / 12


def _bargap_faixa(n_labels, alvo=_LARGURA_ALVO, teto=1.0 - _BARGAP_HISTORICO):
    """bargap que mantem a BARRA com largura constante numa faixa full-width.

    Num chart categorico a barra ocupa `1 - bargap` do slot da categoria, e o
    slot e `1/N` da largura do plot. Com N=12 o default (0.15) rende barras de
    ~7% da faixa; com N=1 a MESMA regra renderia uma barra-paredao de 85% da
    largura — uma serie temporal de 1 ponto virava um bloco solido.

    Aqui a conta e invertida: fixa-se a fracao ALVO da largura do plot que a
    barra deve ocupar (~7,08%, exatamente o que 12 meses produzem com o
    bargap historico) e deriva-se o bargap. O teto faz N>=12 cair EXATAMENTE
    em 0,15, entao o PDF de janela cheia nao muda em um pixel.
    """
    n = max(int(n_labels), 1)
    return 1.0 - min(teto, alvo * n)


def _make_line(labels, values, color):
    fig = go.Figure(go.Scatter(
        x=labels, y=values, mode="lines+markers",
        line=dict(color=color, width=2), marker=dict(size=4),
    ))
    return _chart_layout(fig, 0, len(labels))


def _make_luc_bar(labels, values):
    # Cap de escala: mês extremo (ex.: −330%) esmagaria as demais barras —
    # exibição clipada em [-100, +100] e o valor
    # REAL vira rótulo na barra clipada. Meses de receita mínima continuam
    # exibidos, nunca ocultados; NaN preservado (mês sem dado segue ausente).
    clipped, anotados = [], []
    for lbl, v in zip(labels, values):
        if pd.isna(v) or -100.0 <= v <= 100.0:
            clipped.append(v)
        else:
            cap = 100.0 if v > 0 else -100.0
            clipped.append(cap)
            anotados.append((lbl, cap, v))
    colors = [COR_ADM if (not pd.isna(v) and v >= 0) else COR_DEM for v in values]
    fig = go.Figure(go.Bar(x=labels, y=clipped, marker_color=colors))
    fig.add_hline(y=0, line_dash="dot", line_color="#999", line_width=1)
    _chart_layout(fig, 0, len(labels))
    fig.update_layout(yaxis_title=dict(text="%", font=dict(size=7)))
    if anotados:
        # range fixo só quando há clip — garante espaço pro rótulo fora da
        # ponta da barra; charts normais mantêm autorange.
        fig.update_yaxes(range=[-122, 122])
        for lbl, cap, v in anotados:
            fig.add_annotation(
                x=lbl, y=cap, text=f"{v:.0f}%", showarrow=False,
                yanchor="bottom" if cap > 0 else "top",
                font=dict(size=6, color="#333"))
    return fig


def _make_sparkline(values, color):
    """Mini-gráfico de linha SEM eixos, SEM legenda, SEM grid — só linha.

    Usado nos KPI cards do hero. Render alvo: 240×56 px (display 120×28 @2x).
    Margem mínima pra preencher quase todo o espaço sem cortar a linha.
    """
    fig = go.Figure(go.Scatter(
        x=list(range(len(values))), y=values,
        mode="lines", line=dict(color=color, width=1.5),
    ))
    fig.update_layout(
        margin=dict(l=2, r=2, t=2, b=2),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        showlegend=False,
    )
    return fig


# ── Period helpers ───────────────────────────────────────────────────────────

def _make_labels(pp):
    return [_p_label(p) for p in pp]


def _make_labels_mes(pp):
    """Rotulo do eixo X do chart YoY de Faturamento: so o nome do mes ("Jan").

    O ano vive na LEGENDA. Com duas barras por mes,
    "Jan/25" embaixo do PAR mentiria sobre a barra vermelha, que e de Jan/24 —
    o tick fica centrado no slot da categoria, nao em cada barra.

    Seguro como CATEGORIA do plotly porque o PDF cobre no maximo 12 meses
    (guard em `gerar_pdf_empresa`): 12 meses consecutivos nunca repetem nome de
    mes. Dois rotulos iguais COLAPSARIAM duas categorias numa so — por isso o
    guard dos 12 meses e pre-requisito desta funcao, nao coincidencia.

    Janela que CRUZA o ano-calendario e a excecao: o 1o mes de cada ano leva o
    ano junto ("Out ... Jan/26"), senao nada na faixa diria que Jan e de 2026 —
    e ali a legenda tambem nao pode dizer, porque nomeia intervalos.
    """
    cross = len({p[:4] for p in pp}) > 1
    vistos, out = set(), []
    for p in pp:
        ano, mes = p.split("-")
        rotulo = NOMES_MES[int(mes) - 1]
        if cross and ano not in vistos:
            rotulo = f"{rotulo}/{ano[2:]}"
        vistos.add(ano)
        out.append(rotulo)
    return out


def _rotulos_anos(pp):
    """(nome da serie da JANELA, nome da serie do ANO ANTERIOR) na legenda.

    Janela dentro de UM ano-calendario rende o rotulo limpo:
    "2025" / "2024". Janela que CRUZA o ano nao tem "o ano da
    janela" — ali a legenda nomeia os INTERVALOS, porque chamar de "2025" uma
    serie que vai de outubro a marco do ano seguinte seria rotulo falso.
    """
    anos = {p[:4] for p in pp}
    if len(anos) == 1:
        ano = int(next(iter(anos)))
        return str(ano), str(ano - 1)

    def _faixa(desloc):
        de = str(pd.Period(pp[0], freq="M") - desloc)
        ate = str(pd.Period(pp[-1], freq="M") - desloc)
        return f"{_p_label(de)} a {_p_label(ate)}"

    return _faixa(0), _faixa(12)


def _serie_anterior_alinhada(serie_prev, pp):
    """Serie do ano anterior na ORDEM dos meses exibidos (cada `pp` menos 12m).

    Alinhamento por PERIODO, nunca por posicao: `pp` e a pagina e `serie_prev`
    vive no indice da janela -12m inteira.

    **Mes AUSENTE do indice vira 0,0; valor NULO no indice CONTINUA nulo.** A
    distincao nao e detalhe: o `fat_pm` preenche 0.0 e nunca traz nulo, mas o
    `imp_pm` (2o consumidor) e NULL-aware — mes sem imposto
    escriturado vem NaN de proposito, e o `_series_to_vals` do ano CORRENTE ja
    preserva esse NaN (`reindex(fill_value=0)` so preenche chave faltante).
    Fazer diferente aqui poria uma barra de R$ 0,00 no ano anterior onde o ano
    corrente nao poe nenhuma — afirmando "nao houve imposto" onde o que ha e
    ausencia de dado.

    O `to_numeric` e o que impede o crash: a serie pode carregar `None` (nao
    NaN) e o `float()` cru estouraria TypeError.
    """
    chaves = [str(pd.Period(p, freq="M") - 12) for p in pp]
    s = pd.to_numeric(pd.Series(serie_prev, dtype="object"), errors="coerce")
    return s.reindex(chaves, fill_value=0.0).astype(float).values.tolist()


def _series_to_vals(series, pp):
    return series.reindex(pp, fill_value=0).values.tolist()


# ── Data loading ─────────────────────────────────────────────────────────────

def _load_data(conn, emp_ids, de_str, ate_str):
    def q(sql, params):
        return pd.read_sql(sql, conn, params=params)

    if isinstance(emp_ids, int):
        emp_ids = [emp_ids]
    else:
        emp_ids = [int(e) for e in emp_ids]
    args = (emp_ids, de_str, ate_str)
    flt = "WHERE codi_emp = ANY(%s) AND competencia>=%s AND competencia<=%s"

    fat_df = q(f"SELECT * FROM mart.faturamento_mensal {flt}", args)
    impd_df = q(f"SELECT * FROM mart.impostos_destacados_mensais {flt}", args)
    folha_df = q(f"SELECT * FROM mart.folha_mensal {flt}", args)
    func_df = q(f"SELECT * FROM mart.funcionarios_mensal {flt}", args)
    bal_df = q(f"SELECT * FROM mart.balancete_mensal {flt}", args)
    # Passivo de IRPJ/CSLL na janela ALARGADA em +3 meses: a quota de jan-mar
    # liquida o T4 do ano anterior, entao sem os 3 meses seguintes o dezembro
    # da janela sairia vazio. Query DEDICADA e leve (filtro por clas_cta), nao
    # o balancete inteiro alargado.
    bal_ir_df = q(SQL_PASSIVO_IRPJ_CSLL,
                  params_passivo_ir(emp_ids, de_str, ate_str))
    regime_df = q(f"SELECT * FROM mart.empresa_regime_periodo {flt}", args)
    # Universo GUIA por codi_imp — insumo do modelo de Detalhamento de
    # Impostos. Sem guard de information_schema: o mart e base, sempre
    # presente (mesma classe do faturamento).
    imp_df = q(f"SELECT * FROM mart.impostos_mensais {flt}", args)

    # Encargos fiel a guia. Guard de 1o deploy: a tabela so existe
    # apos a 1a carga — checa via information_schema (nunca lanca,
    # nao envenena a conexao) e degrada pra DataFrame vazio ("encargos: —").
    _enc_exists = not q(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='mart' AND table_name='encargos_mensais'", None).empty
    encargos_df = (q(f"SELECT * FROM mart.encargos_mensais {flt}", args)
                   if _enc_exists else pd.DataFrame())

    # DRE / resultado contabil (lucratividade); mesmo guard de 1o deploy:
    # antes da 1a carga o mart nao existe e o card degrada ao rotulo de
    # supressao (transitorio esperado).
    _dre_exists = not q(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='mart' AND table_name='dre_mensal'", None).empty
    dre_df = (q(f"SELECT * FROM mart.dre_mensal {flt}", args)
              if _dre_exists else pd.DataFrame())

    # Folha por RUBRICA — insumo do modelo de Detalhamento de
    # Folha. MESMO guard de 1o deploy dos dois acima (e do app.py): o mart
    # so existe apos a carga; ausente, o modelo degrada
    # pro aviso em vez de estourar.
    _rub_exists = not q(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='mart' AND table_name='folha_rubricas_mensais'", None).empty
    rub_df = (q(f"SELECT * FROM mart.folha_rubricas_mensais {flt}", args)
              if _rub_exists else pd.DataFrame())

    # Seed de carry-in — última linha pré-janela por conta de
    # balanço, ancorando o ffill de bal_grupo_serie no saldo que vem de
    # antes da janela. SÓ as séries usam o frame seedado (bal_serie_df);
    # encargos, sparklines e gates seguem em bal_df.
    seed_df = q(
        "SELECT DISTINCT ON (codi_emp, codi_cta) * FROM mart.balancete_mensal "
        "WHERE codi_emp = ANY(%s) AND competencia < %s "
        "AND LEFT(clas_cta,1) IN ('1','2') "
        "ORDER BY codi_emp, codi_cta, competencia DESC",
        (emp_ids, de_str),
    )
    bal_serie_df = (pd.concat([seed_df, bal_df], ignore_index=True)
                    if not seed_df.empty else bal_df)

    return (fat_df, impd_df, folha_df, func_df, bal_df, bal_serie_df, bal_ir_df,
            regime_df, encargos_df, dre_df, imp_df, rub_df)


def _load_spark_data(conn, emp_ids, de_str, ate_str,
                     fat_df, impd_df, folha_df, func_df, bal_df,
                     encargos_df=None, dre_df=None, rub_df=None, imp_df=None):
    """Retorna dfs cobrindo a janela usada pelas sparklines do hero.

    Otimização (early-return): se o range pedido [de_str, ate_str] já
    cobre >= 6 meses, os dfs principais já têm tudo o necessário —
    reusa sem nova query. Caso contrário, puxa do mart os meses extras
    a partir de (de_str − 5 meses) e retorna fresco.

    Sparklines podem ter MENOS de 6 pontos quando o mart não tem
    histórico anterior suficiente (empresa nova) — degrada graciosamente.

    Retorna dict com chaves: fat, imp, folha, func, bal, encargos, dre.
    (`dre`: a provisão de IRPJ/CSLL entra no spark do % Impostos.)
    """
    if encargos_df is None:
        encargos_df = pd.DataFrame()
    if dre_df is None:
        dre_df = pd.DataFrame()
    if rub_df is None:
        rub_df = pd.DataFrame()
    n_periods = len(_periods_in_range(de_str, ate_str))
    if n_periods >= 6:
        return {"fat": fat_df, "imp": impd_df, "folha": folha_df,
                "func": func_df, "bal": bal_df, "encargos": encargos_df,
                "dre": dre_df, "rub": rub_df,
                "imp_guia": imp_df if imp_df is not None else pd.DataFrame()}

    spark_inicio = str(pd.Period(de_str, freq="M") - 5)
    if isinstance(emp_ids, int):
        emp_ids = [emp_ids]
    else:
        emp_ids = [int(e) for e in emp_ids]
    args = (emp_ids, spark_inicio, ate_str)
    flt = "WHERE codi_emp = ANY(%s) AND competencia>=%s AND competencia<=%s"
    # Mesmo guard de tabela-ausente do _load_data (1o deploy).
    _enc_exists = not pd.read_sql(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='mart' AND table_name='encargos_mensais'", conn).empty
    _dre_exists = not pd.read_sql(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='mart' AND table_name='dre_mensal'", conn).empty
    _rub_exists = not pd.read_sql(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='mart' AND table_name='folha_rubricas_mensais'", conn).empty
    return {
        "fat":   pd.read_sql(f"SELECT * FROM mart.faturamento_mensal {flt}", conn, params=args),
        "imp":   pd.read_sql(f"SELECT * FROM mart.impostos_destacados_mensais {flt}", conn, params=args),
        # A sparkline do % Impostos segue o card, que le a GUIA —
        # janela ESTENDIDA igual as outras, senao a linha conta outra base
        # que o numero em cima dela.
        "imp_guia": pd.read_sql(f"SELECT * FROM mart.impostos_mensais {flt}", conn, params=args),
        "folha": pd.read_sql(f"SELECT * FROM mart.folha_mensal {flt}", conn, params=args),
        "func":  pd.read_sql(f"SELECT * FROM mart.funcionarios_mensal {flt}", conn, params=args),
        "bal":   pd.read_sql(f"SELECT * FROM mart.balancete_mensal {flt}", conn, params=args),
        "encargos": (pd.read_sql(f"SELECT * FROM mart.encargos_mensais {flt}", conn, params=args)
                     if _enc_exists else pd.DataFrame()),
        "dre": (pd.read_sql(f"SELECT * FROM mart.dre_mensal {flt}", conn, params=args)
                if _dre_exists else pd.DataFrame()),
        # janela ESTENDIDA (de_str - 5m): o rub tem de cobrir os mesmos meses,
        # senao a sparkline de folha conta outra base que o numero em cima dela
        "rub": (pd.read_sql(f"SELECT * FROM mart.folha_rubricas_mensais {flt}",
                            conn, params=args) if _rub_exists else pd.DataFrame()),
        # O fallback contabil de IRPJ/CSLL tambem
        # aqui, senao a sparkline conta outra base que o numero em cima dela —
        # o mesmo defeito que a janela estendida do `imp_guia` veio evitar.
        "bal_ir": pd.read_sql(SQL_PASSIVO_IRPJ_CSLL, conn,
                              params=params_passivo_ir(emp_ids, de_str, ate_str)),
    }


def _load_data_yoy(conn, emp_ids, de_prev, ate_prev):
    """Janela do YoY ([inicio-12m, fim-12m]) — SO os marts dos cards.

    Espelho leve do _load_data: faturamento, destacados, folha, encargos e
    dre (~1 linha/emp/mes cada). SEM balancete/seed/funcionarios/regime —
    nenhum card YoY os consome (o balancete e o mart pesado). Mesmos guards
    de 1o deploy (tabela ausente -> DataFrame vazio).
    """
    def q(sql, params):
        return pd.read_sql(sql, conn, params=params)

    if isinstance(emp_ids, int):
        emp_ids = [emp_ids]
    else:
        emp_ids = [int(e) for e in emp_ids]
    args = (emp_ids, de_prev, ate_prev)
    flt = "WHERE codi_emp = ANY(%s) AND competencia>=%s AND competencia<=%s"

    _enc_exists = not q(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='mart' AND table_name='encargos_mensais'", None).empty
    _dre_exists = not q(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='mart' AND table_name='dre_mensal'", None).empty
    _rub_exists = not q(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='mart' AND table_name='folha_rubricas_mensais'", None).empty
    return {
        "fat":   q(f"SELECT * FROM mart.faturamento_mensal {flt}", args),
        "impd":  q(f"SELECT * FROM mart.impostos_destacados_mensais {flt}", args),
        # O numerador do card e a GUIA, entao o YoY precisa da MESMA
        # base — sem isso o delta compararia guia contra apurado e viraria
        # ruido disfarcado de variacao do negocio.
        "imp":   q(f"SELECT * FROM mart.impostos_mensais {flt}", args),
        "folha": q(f"SELECT * FROM mart.folha_mensal {flt}", args),
        "encargos": (q(f"SELECT * FROM mart.encargos_mensais {flt}", args)
                     if _enc_exists else pd.DataFrame()),
        "dre":   (q(f"SELECT * FROM mart.dre_mensal {flt}", args)
                  if _dre_exists else pd.DataFrame()),
        # o YoY le a MESMA base do card: sem isso o delta compara bases
        # diferentes e vira ruido
        "rub":   (q(f"SELECT * FROM mart.folha_rubricas_mensais {flt}", args)
                  if _rub_exists else pd.DataFrame()),
        # O fallback contabil de IRPJ/CSLL tem de vir nos DOIS anos: senao,
        # nas empresas que o ativam, o delta compara numerador COM fallback
        # contra numerador SEM — degrau artificial, nao variacao do negocio.
        "bal_ir": q(SQL_PASSIVO_IRPJ_CSLL,
                    params_passivo_ir(emp_ids, de_prev, ate_prev)),
    }


# ── Build context for templates ──────────────────────────────────────────────

def _build_context(fat_df, impd_df, folha_df, func_df, bal_df, spark_dfs,
                   all_periods, pp, empresa_nome, cnpj,
                   de_str, ate_str, *, bal_serie_df, regime_df=None,
                   encargos_df=None, dre_df=None, yoy_dfs=None, imp_df=None,
                   rub_df=None, bal_ir_df=None):
    labels = _make_labels(pp)
    tick_a = 45 if len(labels) > 12 else 0
    n_months = len(all_periods)

    def vals(s):
        return _series_to_vals(s, pp)

    # ── Spark window ────────────────────────────────────────────────────
    # Janela usada pelas sparklines do hero. Quando n_months >= 6 a janela
    # coincide com [de_str, ate_str]; caso contrário spark_inicio = de_str
    # − 5 meses (puxado por _load_spark_data via _load_spark_data).
    spark_inicio = (str(pd.Period(de_str, freq="M") - 5)
                    if n_months < 6 else de_str)
    spark_periods = _periods_in_range(spark_inicio, ate_str)

    # KPIs unificados: metrics.py canonico. ent_pm, bancos_pm, sparklines,
    # _delta_vs_first, luc_ctx e a montagem dos cards seguem inline (legitimos).
    S = _series_mensais(fat_df, impd_df, folha_df, func_df, bal_serie_df, all_periods,
                        encargos_df=encargos_df, dre_df=dre_df, rub_df=rub_df,
                        imp_df=imp_df, bal_ir_df=bal_ir_df)
    K = _kpis_agregados(S)

    # YoY: S da janela -12m com os MESMOS marts/semantica;
    # yoy_dfs=None (call site sem a janela) degrada pra prev vazio -> label
    # "sem dados de {ano}" VISIVEL (nunca supressao muda).
    de_prev = str(pd.Period(de_str, freq="M") - 12)
    ate_prev = str(pd.Period(ate_str, freq="M") - 12)
    periods_prev = _periods_in_range(de_prev, ate_prev)
    _e = pd.DataFrame()
    yd = yoy_dfs or {}
    S_prev = _series_mensais(yd.get("fat", _e), yd.get("impd", _e),
                             yd.get("folha", _e), _e, _e, periods_prev,
                             encargos_df=yd.get("encargos", _e),
                             dre_df=yd.get("dre", _e), rub_df=yd.get("rub", _e),
                             imp_df=yd.get("imp", _e),
                             bal_ir_df=yd.get("bal_ir"))
    yoy = _kpis_yoy(S, S_prev)

    # Series — Faturamento Total = NFe (valo_saidas) + NFS-e (valo_servicos).
    # Tabelas disjuntas; somar pra refletir bruto real. Consumers do PDF
    # mantêm visualização única (sem quebra NFe/NFS-e — decisão de UX).
    fat_pm = S["fat_pm"]
    ent_pm = (fat_df.groupby("competencia")["valo_entradas"].sum().reindex(all_periods, fill_value=0)
              if "valo_entradas" in fat_df.columns
              else pd.Series(0.0, index=all_periods))
    # Consumer lê mart.impostos_destacados_mensais.
    # SUM(num)/SUM(den) é média ponderada que sobrevive à consolidação por CNPJ raiz —
    # NÃO usar mean(pct_impostos) (quebra quando filiais zeram deduções).
    # min_count=1 preserva NaN quando todos os meses são sem_deducao/sem_dado (fmt_pct → '—').
    imp_pm = S["imp_pm"]
    folha_pm = S["folha_pm"]

    # Movimentacao de pessoal: series do metrics (fonte unica, compartilhada
    # com o app).
    adm_pm = S["adm_pm"]
    dem_pm = S["dem_pm"]
    atv_pm = S["atv_pm"]
    ini_pm = S["ini_pm"]
    partial_pm = S["is_partial_pm"]

    # Balancete series (frame seedado — carry-in pré-janela)
    ac = S["ac"]
    est = S["est"]
    pc = S["pc"]
    elp = S["elp"]
    pl = S["pl"]
    at_total = S["at"]
    # Card soma c/c (1010102) + aplicacoes financeiras (1010103),
    # subgrupo irmao (= E40 da planilha de referência). Caixa (1010101) fica fora.
    bancos_pm = _bal_grupo_serie(bal_serie_df, all_periods, GRUPOS_BAL["Bancos e Aplicações"])
    # Paridade com a planilha de referência: Caixa com chart proprio (separado
    # de Bancos, como na planilha) e "Despesas e Custos" = 401-405 (SEM
    # IRPJ/CSLL do 406; o 406 segue na lucratividade via despesas_pm, espelho
    # do GASTOS E TRIBUTOS).
    caixa_pm = S["caixa_pm"]
    desp_custos_pm = S["desp_custos_pm"]

    # Indicators
    liq_c = S["liq_c"]
    liq_s = S["liq_s"]
    end_p = S["end_p"]
    pct_luc = S["pct_luc_pm"]

    # Agregação do KPI usa SUM/SUM ao agregar meses (média ponderada por
    # receita), não clean_mean da série mensal. Coerente com comentário
    # inline acima sobre consolidação CNPJ raiz.
    pct_imp_med = K["pct_imp_med"]
    # KPIs médios agregam via SUM/SUM (média ponderada), mesmo padrão
    # acima. clean_mean das pcts mensais distorce quando num/den não são
    # proporcionais.
    # O card "% Folha + Encargos" usa encargo MENSAL fiel a GUIA
    # (mart.encargos_mensais), NUNCA o estoque 2xx do balancete.
    pct_folenc_med = K["pct_folenc_med"]
    pct_luc_med = K["pct_luc_med"]
    # Lucratividade CONTABIL (resultado liquido / receita contabil do
    # mart.dre_mensal). Supressao SO por dado (sem movimento de DESPESA na
    # DRE), nunca por REGIME — o metrics ja NaN-a resultado/pct_luc quando o
    # trigger dispara.
    lc_val = _last_valid(liq_c)
    ls_val = _last_valid(liq_s)
    ed_val = _last_valid(end_p)

    # ── Spark series ────────────────────────────────────────────────────
    # Cada série spark é reindexada em spark_periods pra evitar gaps.
    # Quando spark_dfs == dfs principais (early-return), spark_periods ==
    # all_periods e os valores são idênticos aos pm.
    sf_fat = spark_dfs["fat"].copy() if not spark_dfs["fat"].empty else pd.DataFrame()
    if not sf_fat.empty:
        # 3 origens (saidas, servicos, imobiliario). Guard por coluna,
        # nao if/else por forma: o sparkline tem que dar o MESMO numero que o
        # `_fat_pm` do metrics, e as duas colunas opcionais degradam igual.
        for _c in ("valo_servicos", "valo_imobiliario"):
            sf_fat[_c] = (sf_fat[_c].fillna(0) if _c in sf_fat.columns else 0.0)
        sf_fat["valo_total"] = (sf_fat["valo_saidas"] + sf_fat["valo_servicos"]
                                + sf_fat["valo_imobiliario"])
        fat_spark_pm = sf_fat.groupby("competencia")["valo_total"].sum().reindex(spark_periods, fill_value=0)
    else:
        fat_spark_pm = pd.Series(0.0, index=spark_periods)

    # Sparkline do % Impostos = MESMO universo do card (guia + ISS
    # carga), pelo MESMO helper. NaN preservado pra "—".
    # O fallback contabil passa pelo MESMO portao do card (`irpj_contabil_pm`,
    # fonte unica no metrics) — reimplementar o "guia vence o periodo inteiro"
    # aqui seria uma segunda derivacao do criterio.
    _extra_spark = _irpj_contabil_pm(spark_dfs.get("imp_guia", pd.DataFrame()),
                                     spark_dfs.get("bal_ir"), spark_periods)
    _tab_spark = _tabela_impostos(spark_dfs.get("imp_guia", pd.DataFrame()),
                                  spark_dfs["imp"], spark_periods, None,
                                  linha_extra=_extra_spark or None)
    imp_spark_pm = (_tab_spark["tabela"].loc[LINHA_TOTAL_IMPOSTOS].astype("float64")
                    if _tab_spark["tem_dado"]
                    else pd.Series(float("nan"), index=spark_periods))
    # UM custo: spark de folha e INLINE (fora do metrics) — aplica o
    # mesmo folha_custo do canonico (COM o 13o) pra nao driftar do hero/card.
    folha_spark_pm = custo_folha_pm(spark_dfs["folha"], spark_dfs.get("rub"),
                                    spark_periods, spark_dfs.get("encargos"))
    # Spark de encargos — mesma soma NULL-aware do metrics (componente
    # NULL nao soma; mes sem guia -> NaN -> entra como folha-only no ratio).
    sf_enc = spark_dfs.get("encargos")
    if sf_enc is None or sf_enc.empty:
        enc_spark_pm = pd.Series(float("nan"), index=spark_periods)
    else:
        _enc_comp = (sf_enc.groupby("competencia")[["inss_empresa", "fgts", "fgts_resc"]]
                     .sum(min_count=1))
        enc_spark_pm = _enc_comp.sum(axis=1, min_count=1).reindex(spark_periods)
    func_spark_pm = (spark_dfs["func"].groupby("competencia")["qtd_final"].sum().reindex(spark_periods, fill_value=0)
                     if not spark_dfs["func"].empty else pd.Series(0.0, index=spark_periods))

    # Coerência: spark do % Impostos divide pelo fat (mesma base do
    # hero/card — o par R$/% fecha na calculadora do leitor).
    pct_imp_spark = _safe_div(imp_spark_pm, fat_spark_pm) * 100
    pct_folenc_spark = _safe_div(folha_spark_pm + enc_spark_pm.fillna(0), fat_spark_pm) * 100

    # Indicadores contábeis (sparkline) — usam o mesmo bal_df do range pedido
    # (não há janela "estendida" pra balancete; sparkline degrada se range curto).
    liq_c_spark = liq_c
    liq_s_spark = liq_s
    end_p_spark = end_p
    pct_luc_spark = pct_luc

    # ── Charts as base64 (string vazia indica seção sem dados) ──
    # Aspecto dos charts da pagina CONTABIL: com 3 fileiras de chart
    # + fileira de cards do balanco, o slot ficou ~3,7:1 (half) / ~7,4:1
    # (full) — PNG 2:1 letterboxa (object-fit: contain). Meia-largura rende
    # 500x135; largura-cheia 1000x135 (mesma fonte aparente nos dois).
    _HALF = {"width": 500, "height": 135}
    _FULL = {"width": 1000, "height": 135}
    _fig_fat_gastos = _make_grouped_bar(
        labels,
        [("Faturamento", vals(fat_pm), COR_FAT),
         ("Gastos", vals(ent_pm), COR_IMP)],
        tick_a) if not fat_df.empty else None
    # Legenda + definicao da serie "Gastos" como nota do chart.
    # Composicao REAL: ent_pm = SUM(valo_entradas) do
    # mart.faturamento_mensal (efentradas) — devolucoes de venda sao SUBSET
    # de valo_entradas (nunca subtrair de novo). O Faturamento e liquido da
    # devolucao, que segue dentro de Gastos no mesmo chart — a nota declara
    # os dois lados.
    fat_gastos_note = ("Gastos = total das notas de entrada do período "
                       "(compras; inclui devoluções de venda, que também "
                       "são descontadas do Faturamento)")
    chart_fat_gastos_flat = (_fig_to_b64(_fig_fat_gastos, **_HALF)
                             if _fig_fat_gastos else "")
    # Faturamento SOZINHO (layout do fiscal): FAIXA horizontal de largura
    # cheia no topo — serie temporal le melhor deitada. O slot e ~285mm x
    # ~78mm (aspecto ~3,7), entao 1000x270; um PNG 2:1 letterboxaria com o
    # object-fit: contain do .block-chart.
    # A largura de barra pra janela curta vem do proprio _make_bar. O TAMANHO
    # do render acompanha o slot: full-width (~3,7:1) na janela normal, meia
    # largura (~1,85:1) na curta — o mesmo limiar do template, senao o
    # object-fit: contain letterboxa.
    _FAT_CHART = ({"width": 500, "height": 270} if n_months <= FAIXA_CURTA_MAX_MESES
                  else {"width": 1000, "height": 270})
    # YoY intercalado: a serie do ano anterior e a MESMA que alimenta o delta dos
    # cards (S_prev, montada de `_load_data_yoy` la em cima) — uma fonte so pro
    # mesmo numero, senao card e chart divergem com o mesmo nome na tela.
    # Call site sem `yoy_dfs` (defaults / testes) da prev VAZIO e o chart
    # degrada pra serie unica.
    _fat_prev_df = yd.get("fat", _e)
    _nome_atual, _nome_anterior = _rotulos_anos(pp)
    _fig_fat = _make_bar_yoy(
        _make_labels_mes(pp), vals(fat_pm),
        _serie_anterior_alinhada(S_prev["fat_pm"], pp),
        _nome_atual, _nome_anterior, tick_a,
        tem_atual=not fat_df.empty, tem_anterior=not _fat_prev_df.empty)
    # GUARD DOS DOIS ANOS: a pagina so declara "sem dados de faturamento"
    # quando NENHUM dos dois anos tem dado — mes sem faturamento na janela
    # nao pode apagar tambem a barra do ano anterior.
    chart_fat = _fig_to_b64(_fig_fat, **_FAT_CHART) if _fig_fat else ""

    # YoY intercalado ("ano anterior em vez de duplicar"). MESMA mecanica do chart de
    # Faturamento do fiscal, MESMO builder — e a serie do ano anterior e a
    # MESMA que alimenta o delta YoY do card de % Impostos (`S_prev`, montado
    # de `_load_data_yoy` la em cima). Uma fonte so pro mesmo numero: card e
    # chart nao podem divergir com o mesmo nome na tela.
    #
    # Nao ha chart vizinho de "Tributos por mes": no Simples ele seria o
    # MESMO numero deste aqui. O detalhamento por tributo fica inteiro na
    # TABELA da secao, que e quem o contador le.
    #
    # O `tem_anterior` olha a SERIE, nao o df: `imp_pm` soma destacados +
    # provisao de IRPJ/CSLL, e ha empresa com provisao e ZERO destacado —
    # testar so `impd` esconderia a barra vermelha dela.
    #
    # SEM variavel intermediaria de slot, e de proposito: uma constante de
    # chart nomeada pela PAGINA acopla as paginas (mexer numa muda a outra
    # em silencio) — vide `test_charts_dos_detalhamentos_usam_constante_de_SLOT`,
    # que faz grep no fonte. Aqui so aparecem as constantes de FORMA DE SLOT,
    # inline.
    _imp_prev = _serie_anterior_alinhada(S_prev["imp_pm"], pp)
    _fig_imp = _make_bar_yoy(
        _make_labels_mes(pp), vals(imp_pm), _imp_prev,
        _nome_atual, _nome_anterior, tick_a,
        # O guard olha a SERIE, nao o `impd_df`. Com o numerador na
        # guia, empresa que TEM destacado mas so tem guia de imposto de
        # TERCEIRO passaria no `not impd_df.empty` e desenharia um plot
        # VAZIO, com eixo e legenda e nenhuma barra — pior que nao desenhar.
        tem_atual=bool(imp_pm.notna().any()),
        tem_anterior=bool(S_prev["imp_pm"].notna().any()))
    chart_impostos = (_fig_to_b64(_fig_imp,
                                  **(_CHART_DETALHE_MEIO
                                     if n_months <= FAIXA_CURTA_MAX_MESES
                                     else _CHART_DETALHE_FAIXA))
                      if _fig_imp else "")
    # Gate na presenca da DRE (pct_luc vem do mart.dre_mensal, nao mais
    # do balancete/faturamento) — pct_luc.notna().any() ja encoda "tem DRE".
    chart_lucratividade = _fig_to_b64(_make_luc_bar(labels, vals(pct_luc)), **_FULL) if pct_luc.notna().any() else ""
    # Espelho do app: sem balancete NA janela (mesmo criterio dos
    # Indicadores, `bal_tem_dado_periodo`) Bancos e Caixa seriam so o saldo
    # HERDADO do seed, reto em todos os meses -- ausencia se declara, nao se
    # desenha. Mes = ultimo balancete pre-janela (seed; em grupo, o max entre
    # filiais). "" = caminho normal (o template cai na mensagem de sempre).
    bal_ausente_msg = ""
    if not S.get("bal_tem_dado_periodo", True):
        _pre = (bal_serie_df[bal_serie_df["competencia"] < de_str]
                if "competencia" in bal_serie_df.columns else bal_serie_df.iloc[0:0])
        bal_ausente_msg = "Sem balancete no período" + (
            f" — último balancete: {_p_label(_pre['competencia'].max())}"
            if not _pre.empty else "")
    chart_bancos = (_fig_to_b64(_make_line(labels, vals(bancos_pm), COR_FAT), **_HALF)
                    if bancos_pm.abs().sum() > 0 and not bal_ausente_msg else "")
    # Saldo negativo em Bancos/Aplicacoes e
    # contabilmente atipico (saldo credor) — anota no chart em vez de exibir
    # cru sem contexto. Nota vazia = macro nao renderiza nada (regressao-zero
    # pra quem tem banco positivo).
    bancos_note = ("saldo credor (negativo) no período"
                   if (bancos_pm.fillna(0) < 0).any() else "")
    # Blocos de paridade — mesma regra de supressao do chart de
    # bancos: serie all-zero (sem conta no balancete) -> empty_msg do macro.
    chart_caixa = (_fig_to_b64(_make_line(labels, vals(caixa_pm), COR_FAT), **_HALF)
                   if caixa_pm.abs().sum() > 0 and not bal_ausente_msg else "")
    chart_despesas = (_fig_to_b64(_make_bar(labels, vals(desp_custos_pm), COR_IMP, tick_a), **_HALF)
                      if desp_custos_pm.abs().sum() > 0 else "")

    # ── Chart de folha (sem encargos) ──
    # YoY: MESMA mecanica dos charts de Faturamento e de
    # Impostos, MESMO builder. A serie do ano anterior e `S_prev["folha_pm"]`,
    # a MESMA que alimenta o delta YoY do card "% Folha + Encargos" — uma
    # fonte so pro mesmo numero.
    #
    # A serie do ano CORRENTE e `folha_pm`: o chart compara a folha de CUSTO
    # (sem encargos).
    #
    # `tem_anterior` olha as DUAS fontes porque `custo_folha_pm` le o mart de
    # rubricas quando ele existe e degrada pro `folha_mensal` quando nao: a
    # empresa pode ter historico anterior em so uma das duas.
    _folha_prev_vazio = (yd.get("folha", _e).empty and yd.get("rub", _e).empty)
    _fig_folha = _make_bar_yoy(
        _make_labels_mes(pp), vals(folha_pm),
        _serie_anterior_alinhada(S_prev["folha_pm"], pp),
        _nome_atual, _nome_anterior, tick_a,
        tem_atual=not folha_df.empty, tem_anterior=not _folha_prev_vazio)
    # GUARD DOS DOIS ANOS: a pagina so declara "sem dados de folha" quando
    # NENHUM dos dois anos tem dado — mes vazio na janela nao pode apagar a
    # barra do ano anterior.
    chart_folha = (_fig_to_b64(_fig_folha, **_CHART_MEIA_LARGURA)
                   if _fig_folha else "")

    # ── Sparklines do hero ──
    def _spark_b64(values, color):
        # Sparkline omitida quando não há ao menos 2 pontos não-zero
        # (linha plana em zero é informação ruim).
        non_zero = sum(1 for v in values if v and not pd.isna(v) and v != 0)
        if non_zero < 2:
            return ""
        return _fig_to_b64(_make_sparkline(values, color), width=240, height=56)

    chart_fat_spark        = _spark_b64(fat_spark_pm.values.tolist(),       COR_FAT)
    chart_pct_imp_spark    = _spark_b64(pct_imp_spark.values.tolist(),      COR_IMP)
    chart_pct_folenc_spark = _spark_b64(pct_folenc_spark.values.tolist(),   COR_FOLENC)
    chart_func_spark       = _spark_b64(func_spark_pm.values.tolist(),      COR_FUNC)
    chart_liq_c_spark      = _spark_b64(liq_c_spark.values.tolist(),        COR_FAT)
    chart_liq_s_spark      = _spark_b64(liq_s_spark.values.tolist(),        COR_FAT)
    chart_end_p_spark      = _spark_b64(end_p_spark.values.tolist(),        COR_IMP)
    chart_pct_luc_spark    = _spark_b64(pct_luc_spark.values.tolist(),      COR_FUNC)

    # ── Funcionários table rows ──
    func_rows = []
    if n_months <= 12:
        adm_v = vals(adm_pm)
        dem_v = vals(dem_pm)
        atv_v = vals(atv_pm)
        ini_v = vals(ini_pm)
        partial_v = [bool(partial_pm.get(p, False)) for p in pp]
        for i, lb in enumerate(labels):
            func_rows.append({
                "mes": lb,
                "inicial": int(ini_v[i]),
                "adm": int(adm_v[i]),
                "dem": int(dem_v[i]),
                "final": int(atv_v[i]),
                "is_partial": partial_v[i],
            })
    else:
        p_adm = adm_pm.reindex(pp, fill_value=0)
        p_dem = dem_pm.reindex(pp, fill_value=0)
        p_atv = atv_pm.reindex(pp, fill_value=0)
        p_ini = ini_pm.reindex(pp, fill_value=0)
        p_partial = partial_pm.reindex(pp, fill_value=False)
        first_i = int(p_ini.iloc[0]) if not p_ini.empty else 0
        last_a = int(p_atv.iloc[-1]) if not p_atv.empty else 0
        func_rows.append({
            "mes": f"{_p_label(pp[0])}-{_p_label(pp[-1])}",
            "inicial": first_i,
            "adm": int(p_adm.sum()),
            "dem": int(p_dem.sum()),
            "final": last_a,
            "is_partial": bool(p_partial.any()),
        })

    periodo_lbl = f"{_p_label(de_str)} a {_p_label(ate_str)}"
    gerado_em = datetime.now().strftime("%d/%m/%Y %H:%M")

    # ── Hero KPI cards ──────────────────────────────────────────────────
    # Cada card vira dict com key (CSS modifier), label, value, spark, context.
    # Janela EFETIVA: total e media vem do metrics, que ja aparou a
    # cauda de meses nao fechados — a media divide pelos meses COM dado.
    fat_total = K["fat_total"]
    n_efetivo = len(S["periods_efetivos"])
    # Range de 1 mês tambem compara YoY (com o mesmo mês do ano anterior) —
    # o "1 mês isolado" só quando nem YoY ha.
    # SUB-LINHA DE CARD E SO O FRAGMENTO YoY. Card e leitura RAPIDA; o
    # detalhe vive na TABELA da propria pagina, e o leitor do PDF e o
    # CLIENTE. No app o leitor e operador e o tooltip tem espaco pra frase
    # longa.
    #
    # Sobrevive UMA classe de excecao, e ela e nomeada: a frase que explica
    # POR QUE o valor e "—". Sem ela o travessao fica mudo e le como falta de
    # dado, que e o oposto do que houve.
    #
    # A montagem e lista + join, e nao concatenacao incremental: assim
    # parte ausente nunca produz separador orfao.
    fat_frag = _yoy_frag(yoy["fat"], yoy["ref"], "%")
    # Sem "média mensal" (repete uma conta que o leitor faz do proprio
    # valor). Fica o "sem dados fechados no período", que e da classe
    # preservada — ele explica o "—" do VALOR.
    fat_partes = ["sem dados fechados no período"] if n_efetivo == 0 else []
    if fat_frag:
        fat_partes.append(fat_frag)
    fat_ctx = " · ".join(fat_partes)

    # Rótulo ÚNICO nos 3 regimes (o detalhe técnico da fonte vive no tooltip
    # do app). O par R$/% divide um no outro: denominador do % = o
    # Faturamento exibido no card vizinho (K["imp_total"]/K["pct_imp_med"] do
    # metrics, invariante testado em TestCoerenciaCardImpostos).
    # O numerador INCLUI o IRPJ/CSLL provisionado — por isso o rótulo não diz
    # "sobre vendas".
    # Numerador AUSENTE não vira "R$ 0,00": "se não tem informação aparece
    # sem informação", por EXTENSO. Só vale pra `sem_dado`: em `sem_deducao`
    # a empresa escritura e o zero é afirmação que o dado sustenta, então
    # FICA. O invariante do par R$/% segue usando o K cru (NaN -> % "—").
    # O detalhe em R$ (impostos do período, IRPJ/CSLL) fica na tabela do
    # Detalhamento de Impostos e no TOOLTIP do app; o criterio que o decide
    # (`impostos_sem_irpj_proprio`) segue no metrics.
    #
    # Ficam as tres frases de ausencia, que SOMAM entre si.
    imp_partes = []
    if S.get("imp_so_retencao"):
        # Duas ausencias DIFERENTES, duas frases: esta diz POR QUE
        # nao ha numero, e e mais informativa que a generica.
        imp_partes.append(_FRASE_IMP_SO_RETENCAO)
    elif K.get("imp_sem_dado"):
        imp_partes.append(_FRASE_IMP_SEM_INFO)
    if K.get("fat_den_insuf"):
        # Denominador POSITIVO porém imaterial. "—" mudo
        # faria o leitor achar que falta dado, quando o que falta é BASE.
        imp_partes.append("faturamento insuficiente para o indicador no período")
    elif K["imp_sem_fat"]:
        # imposto > 0 sem NENHUM fat NF (contabilidade na matriz): "—" nunca
        # mudo — label curto explica (consolidado por CNPJ mostra o número).
        imp_partes.append("sem faturamento NF no período pra basear o %")
    imp_frag = _yoy_frag(yoy["imp"], yoy["ref"], " p.p.")
    if imp_frag:
        imp_partes.append(imp_frag)
    imp_ctx = " · ".join(imp_partes)

    # Sem decomposicao "R$ X de folha + R$ Y de encargos" no card: a tabela
    # do Detalhamento de Folha tem a linha de Encargos separada e o CUSTO
    # TOTAL — e o app mostra as duas no tooltip.
    fol_partes = []
    if K.get("fat_den_insuf"):
        fol_partes.append("faturamento insuficiente para o indicador no período")
    fol_frag = _yoy_frag(yoy["folenc"], yoy["ref"], " p.p.")
    if fol_frag:
        fol_partes.append(fol_frag)
    fol_ctx = " · ".join(fol_partes)

    # Estoque pontual do ultimo mes FECHADO (K do metrics): ler o ultimo do
    # RANGE faria mes em fechamento/sem carga virar demissao em massa.
    func_last = K["func_final"]
    func_first = K["func_inicial"]
    if not func_df.empty and func_last is not None:
        ganho = func_last - func_first
        if n_efetivo >= 2 and ganho != 0:
            sinal = "+" if ganho > 0 else ""
            func_ctx = f"início: {func_first} · {sinal}{ganho} no período"
        elif n_efetivo >= 2:
            func_ctx = f"estável em {func_last}"
        else:
            func_ctx = "1 mês isolado"
        func_value = str(func_last)
    else:
        func_value = "—"
        func_ctx = "sem folha no período"

    # Rotulo do corte: quando a cauda foi aparada, o leitor precisa
    # saber que o numero NAO e do range inteiro. Texto vem do metrics
    # (verbatim, igual ao label do YoY) e so existe quando houve corte.
    # Fica SO no card de Funcionarios: nos outros tres ele competiria com o
    # fragmento YoY pelo mesmo espaco; aqui ele e a leitura (o headcount e
    # ESTOQUE PONTUAL, entao "de quando e este numero" muda o sentido dele).
    if S["label_corte"]:
        func_ctx = f"{func_ctx} · {S['label_corte']}"

    # Os 4 cards sao construidos aqui e DISTRIBUIDOS pelas paginas abaixo. Cada
    # dict e criado UMA vez: a pagina que exibe o card exibe o MESMO objeto,
    # entao nao ha como dois lugares mostrarem numeros diferentes.
    card_fat = {"key": "fat", "label": "Faturamento",
                "value": _fmt_brl(fat_total), "spark": chart_fat_spark,
                "context": fat_ctx}
    card_imp = {"key": "imp", "label": "% Impostos",
                "value": _fmt_pct(pct_imp_med), "spark": chart_pct_imp_spark,
                "context": imp_ctx}
    # Encargo fiel a GUIA
    card_folenc = {"key": "folenc", "label": "% Folha + Encargos",
                   "value": _fmt_pct(pct_folenc_med),
                   "spark": chart_pct_folenc_spark, "context": fol_ctx}
    card_func = {"key": "func", "label": "Funcionários",
                 "value": func_value, "spark": chart_func_spark,
                 "context": func_ctx}

    # A faixa do fiscal leva os QUATRO cards. O card de impostos aparece TAMBEM no modelo de Detalhamento —
    # e o MESMO dict, criado uma vez so, entao as duas paginas nunca podem
    # mostrar numeros diferentes.
    hero_fiscal = [card_fat, card_imp, card_folenc, card_func]

    # ── Modelo: Detalhamento de Impostos ─────────────────────────────────
    # Universo CARGA, montado pelo MESMO helper que o bloco Impostos do
    # Streamlit (etl.metrics.tabela_impostos). Reimplementar aqui criaria
    # dois universos com o mesmo nome.
    # A linha do fallback de IRPJ/CSLL vem da MESMA serie que
    # o `series_mensais` usou pro numerador — nao ha segunda derivacao, entao
    # card e tabela continuam sendo o mesmo numero por construcao.
    _extra = S.get("irpj_contabil") or None
    _I = _tabela_impostos(imp_df if imp_df is not None else pd.DataFrame(),
                          impd_df, pp, fat_pm, linha_extra=_extra)
    # Sem chart "Tributos por mes" no PDF: ele repetiria em cores a
    # decomposicao que a tabela logo abaixo da em numeros, e no Simples seria
    # o MESMO numero do chart ao lado. O universo GUIA E a tabela desta
    # secao. `_I["series_chart"]` segue vivo no helper porque o Streamlit
    # consome (la a pilha por tributo fica, o leitor e operador e a janela vai
    # a 48 meses).
    #
    # `imp_empty_msg` e o vazio da TABELA. Sem isso o `so_retencao` — "a
    # empresa TEM guias, mas todas de imposto de TERCEIRO" — nao teria
    # superficie nenhuma e a pagina leria "sem dados", que e mentira.
    imp_empty_msg = (_FRASE_IMP_SO_RETENCAO if _I["so_retencao"]
                     else "⚠️ Sem dados de impostos para o período selecionado")
    # Faixa do detalhamento: o MESMO card da faixa do fiscal (nao e
    # copia nem recalculo — e o mesmo objeto) + os 3 MAIORES tributos do
    # periodo. Os destaques leem a PROPRIA tabela exibida abaixo, entao card e
    # linha sao o mesmo numero por construcao. Universo = o da TABELA (carga).
    # ── A tabela e o card medem a GUIA RECOLHIDA ────────────────────────
    # "O numero e o que pagou". O universo e o do
    # `tabela_impostos` (guia propria + ISS pela carga), e o card, o chart e
    # esta tabela leem TODOS o mesmo TOTAL — a pagina segue 100% um universo
    # so. O ISS entra pela carga porque, quando ele e RETIDO NA FONTE, nunca
    # vira guia da empresa: o imposto E dela, so muda quem recolhe.
    imp_tem_dado = _I["tem_dado"]
    _T = _I["tabela"]
    imp_titulo = TITULO_IMPOSTOS_GUIAS
    imp_col_labels = labels
    imp_rows = []
    _linha_pct = LINHA_PCT_IMPOSTOS
    for linha in _T.index:
        vals = _T.loc[linha]
        # A linha de % NUNCA e formatada como R$: o styler do app faz o mesmo
        # casamento por NOME. Constante compartilhada, nao literal duplicado.
        e_pct = (linha == _linha_pct)
        imp_rows.append({
            "label": linha,
            "destaque": linha in (LINHA_TOTAL_IMPOSTOS, _linha_pct),
            "values": [(_fmt_pct(v) if e_pct else _fmt_brl(v)) for v in vals],
        })

    # Os destaques leem `_T` — a MESMA tabela exibida abaixo —, nao o `_I`.
    # E a regra do `maiores_linhas` ("card e linha sao o mesmo numero por
    # construcao"): ler o `_I` poria o ICMS da guia na faixa ao lado do ICMS
    # apurado na tabela — dois numeros com o mesmo nome na mesma pagina.
    # No Simples `_T` E o `_I`, entao a faixa dele nao muda.
    hero_impostos = [card_imp] + [
        _card_destaque(nome, total, i)
        for i, (nome, total) in enumerate(_maiores_linhas(
            _T, 3, excluir=(LINHA_TOTAL_IMPOSTOS, _linha_pct)))]

    # ── Modelo: Detalhamento de Folha ────────────────────────────────────
    # Contrato = a TELA. Mesmo helper (etl.metrics.folha_por_linha), mesma
    # tabela unica de custo, mesmo chart empilhado. UM custo, UM percentual:
    # nao ha ponte nem segundo total. As linhas opcionais ja vem cortadas de
    # dentro do helper — o render nao decide nada.
    _F = _folha_por_linha(rub_df if rub_df is not None else pd.DataFrame(),
                          pp, encargos_df=encargos_df, fat_pm=fat_pm)
    _f_tab = _F["tabela"]
    folha_tem_rubrica = not _f_tab.empty and len(_f_tab.columns) > 0
    folha_col_labels = [_p_label(c) for c in _f_tab.columns]
    # HIERARQUIA: EM CIMA O TOTAL, EMBAIXO OS PRINCIPAIS.
    #
    # O topo e so o total (uma linha de dinheiro, uma de %), pro leitor nao
    # ter que somar a coluna de cabeca, e a decomposicao vira uma lista curta
    # e FIXA logo abaixo.
    #
    # O % pode passar de 100% e isso e VERDADE — nunca capar nem esconder.
    folha_rows = []
    if folha_tem_rubrica:
        folha_rows = [
            {"label": LINHA_FOLHA_ENCARGOS, "destaque": True,
             "values": [_fmt_brl(v) for v in _F["custo"]]},
            {"label": LINHA_PCT_FAT, "destaque": True,
             "values": [_fmt_pct(v) for v in _F["pct_fat"]]},
        ]

    # A regiao de baixo e CONTIDA no total, nao aditiva: estas rubricas ja
    # estao dentro da linha "Folha + Encargos" acima. Nao existe gate de
    # "soma dos destaques == total" — quem protege o leitor de contar 2x e o
    # subtitulo, que declara a contencao ao lado do numero.
    #
    # A selecao mora no metrics (fonte unica com o app), e le o MESMO
    # DataFrame que a tabela de cima resume. Rubrica sem valor na janela nao
    # vira linha; sem NENHUMA delas o bloco inteiro nao renderiza, porque
    # titulo de contencao sem linha e ruido.
    #
    # TODA linha e fmt_brl, Hora Extra inclusive: a tabela e de CUSTO e a
    # coluna mostra dinheiro. A serie qtd_he continua no helper.
    _f_det = _folha_destaques(_f_tab) if folha_tem_rubrica else pd.DataFrame()
    folha_destaque_rows = [
        {"label": linha, "destaque": False,
         "values": [_fmt_brl(v) for v in _f_det.loc[linha]]}
        for linha in _f_det.index]
    # O chart e UMA barra por mes com o total, rotulada, e nao uma pilha: a
    # pilha repetiria, em cores, a decomposicao que a tabela logo abaixo ja da
    # em numeros — e a tabela e quem o contador le. O que o leitor precisa VER
    # no grafico e a evolucao do total. O rotulo em cima da barra vem do
    # proprio `_make_bar_yoy`, que rotula sempre.
    # YoY, MESMO builder dos outros tres. A barra azul E a
    # linha "Folha + Encargos" da tabela logo abaixo — o argumento e o proprio
    # `_F["custo"]`, nao uma segunda derivacao; recalcular por outro caminho
    # poria dois numeros com o mesmo nome na pagina.
    #
    # A serie do ano anterior e `folha_pm + enc_pm` da janela -12m. A soma
    # reproduz o CUSTO TOTAL **por construcao**, e nao por coincidencia: os
    # dois lados passam por `rubricas_de_custo`/`custo_folha_pm` e por
    # `encargos_por_mes`, os mesmos helpers que montam a tabela. O `fillna(0)`
    # e o idioma do card (mes sem guia entra como folha-only).
    #
    # O eixo sao os meses COM DADO (`_f_tab.columns`), nao a janela: rotulos,
    # alinhamento do ano anterior e nomes das series saem TODOS dai. Por isso
    # os nomes podem diferir dos da pagina fiscal quando os meses com folha sao
    # um subconjunto que nao cruza o ano — cada chart nomeia o proprio eixo, e
    # nomear "2025" uma serie que nao e de 2025 seria rotulo falso.
    _f_cols = list(_f_tab.columns)
    _custo_vals = [float(v) for v in _F["custo"]] if folha_tem_rubrica else []
    _FOLHA_CHART = (_CHART_DETALHE_MEIO
                    if len(folha_col_labels) <= FAIXA_CURTA_MAX_MESES
                    else _CHART_DETALHE_FAIXA_BAIXA)
    _f_nome_atual, _f_nome_anterior = (_rotulos_anos(_f_cols) if _f_cols
                                       else (_nome_atual, _nome_anterior))
    _fig_folha_det = _make_bar_yoy(
        _make_labels_mes(_f_cols), _custo_vals,
        _serie_anterior_alinhada(
            S_prev["folha_pm"] + S_prev["enc_pm"].fillna(0), _f_cols),
        _f_nome_atual, _f_nome_anterior,
        45 if len(folha_col_labels) > 12 else 0,
        tem_atual=folha_tem_rubrica, tem_anterior=not _folha_prev_vazio)
    chart_folha_detalhe = (_fig_to_b64(_fig_folha_det, **_FOLHA_CHART)
                           if _fig_folha_det else "")
    # Sem legenda do "Outros": a tabela nao tem agrupamento "Outros". O helper
    # `etl.metrics.legenda_outros` segue vivo e com teste — so nao tem
    # consumer aqui, igual ao `legenda_abatimentos` logo abaixo.
    # A legenda de ABATIMENTOS ("Fora do custo no periodo") NAO vai pro PDF:
    # este PDF vai pro CLIENTE, e a lista de exclusoes e ruido pra quem so
    # quer saber quanto custou. Ela vive no APP, que e onde a CONCILIACAO
    # acontece — o helper `etl.metrics.legenda_abatimentos` segue vivo e com
    # teste, so nao e renderizado aqui.
    # Texto NEUTRO, sem imperativo de operador: este PDF vai pro CLIENTE, e
    # "rode o ETL" e instrucao pra quem opera o sistema. O app mantem a versao
    # acionavel — la o leitor E o operador.
    folha_empty_msg = "Sem folha por rubrica no período."
    # FAIXA: CUSTO TOTAL -> % SOBRE FATURAMENTO -> ENCARGOS -> FUNCIONARIOS.
    # Sem ranking das 3 maiores linhas: ele repetiria a tabela e obrigaria o
    # card a se explicar ("2o maior item") pra nao ser confundido com o total.
    #
    # Os DOIS cards do meio-fim sao os MESMOS DICTS do hero fiscal, criados
    # uma vez so — as duas paginas nunca podem mostrar numeros diferentes, e a
    # identidade de objeto e travada por teste. Isso da de graca o piso de
    # materialidade do `%` ("—" quando o faturamento nao sustenta) e a
    # definicao de Funcionarios que o dash usa: `func_final`, o estoque
    # PONTUAL do ultimo mes fechado (nao media).
    #
    # Os DOIS novos saem do MESMO `_F` que a tabela renderiza, entao
    # "card == soma da linha" vale por construcao, nao por coincidencia.
    # Sem rubrica na janela eles NAO entram: card de R$ 0,00 afirmaria que a
    # folha custou zero, quando o que houve foi ausencia de dado.
    hero_folha = [card_folenc, card_func]
    if folha_tem_rubrica:
        _custo_total = float(pd.Series(_F["custo"]).fillna(0).sum())
        _enc_total = float(pd.Series(_F["encargos"]).fillna(0).sum())
        hero_folha = [
            # sub-linha vazia: "no período · N meses" repetiria o
            # cabecalho da pagina, que ja traz o intervalo.
            {"key": "custo_total", "label": LINHA_FOLHA_ENCARGOS,
             "value": _fmt_brl(_custo_total), "spark": "", "context": ""},
            card_folenc,
            # idem: "INSS patronal e FGTS recolhidos" repetiria o proprio
            # rotulo do card, que ja declara a fonte ("(INSS + FGTS, guia)").
            {"key": "encargos", "label": LINHA_ENCARGOS, "spark": "",
             "value": _fmt_brl(_enc_total), "context": ""},
            card_func,
        ]

    def _ctx_band(val, thresholds_good, label_good="saudável", label_warn="atenção", label_bad="crítico"):
        if val is None or pd.isna(val):
            return "—"
        if val < 0:
            # So liq/end usam este helper -- CTX_BALANCETE_EM_REVISAO, nao
            # CTX_INCONSISTENTE
            # (que segue vivo so pro caso semantico diferente da Lucratividade).
            return _CTX_BALANCETE_EM_REVISAO
        good_th, warn_th = thresholds_good
        if val >= good_th:
            return label_good
        if val >= warn_th:
            return label_warn
        return label_bad

    def _ctx_band_inv(val, thresholds_bad, label_good="saudável", label_warn="atenção", label_bad="alto"):
        # Inverso: valor MENOR é melhor (endividamento). Negativo = balancete
        # em revisão.
        if val is None or pd.isna(val):
            return "—"
        if val < 0:
            return _CTX_BALANCETE_EM_REVISAO
        good_th, warn_th = thresholds_bad
        if val <= good_th:
            return label_good
        if val <= warn_th:
            return label_warn
        return label_bad

    luc_last = float(pct_luc.dropna().iloc[-1]) if len(pct_luc.dropna()) > 0 else None
    fat_pm_last = _last_valid(fat_pm)
    # Supressão por causa-despesas (Simples SEMPRE + qualquer
    # regime com zero despesas na janela) carrega o rótulo unificado no card.
    luc_sem_registros = S["luc_sem_despesas"]
    if K.get("luc_den_insuf"):
        # Aqui o substantivo é RECEITA — a lucratividade divide pela
        # receita CONTÁBIL, não pelo faturamento NF.
        luc_ctx = "receita insuficiente para o indicador no período"
    elif luc_last is None:
        luc_ctx = LUC_SEM_REGISTROS if luc_sem_registros else "—"
    elif fat_pm_last <= 0:
        luc_ctx = CTX_INCONSISTENTE
    else:
        # Vírgula decimal (pt-BR), como todo % do PDF.
        luc_ctx = "último mês: " + f"{luc_last:+.1f}".replace(".", ",") + "%"
        # YoY so nos branches numericos — card suprimido nao ganha ruido.
        luc_frag = _yoy_frag(yoy["luc"], yoy["ref"], " p.p.")
        if luc_frag:
            luc_ctx += f" · {luc_frag}"
        # Janela curta pode extremar a lucratividade
        # (poucos meses concentram o LAIR de um so). NAO clampa o valor
        # (pct_luc_med acima segue cru) -- so contextualiza.
        if n_months <= _LUC_JANELA_CURTA_MESES:
            luc_ctx += f" · {LUC_JANELA_CURTA_FRAG}"

    # Inconsistência do balanço: PC<=0 ou AC<0 invalidam LC/LS.
    # Detecção via componentes — não dá pra inferir do indicador (negativo /
    # negativo = positivo, mascara o problema).
    liq_inconsistente, endivid_inconsistente = _flags_inconsistencia(ac, pc, pl, at_total)
    lc_ctx = _CTX_BALANCETE_EM_REVISAO if liq_inconsistente else _ctx_band(lc_val, (1.5, 1.0))
    ls_ctx = _CTX_BALANCETE_EM_REVISAO if liq_inconsistente else _ctx_band(ls_val, (1.0, 0.5))

    # Endividamento: PL<=0 (se extraído) ou AT<=0 invalidam o indicador
    # (subset PC+ELP sobre AT pode ficar matematicamente positivo mesmo
    # com balanço quebrado). Detecção via componentes, mesmo
    # padrão da liquidez acima. PL ausente do plano (Simples Nacional sem 204)
    # NÃO conta como insolvência — distinguido via pl.fillna(0) != 0.
    end_ctx = _CTX_BALANCETE_EM_REVISAO if endivid_inconsistente else _ctx_band_inv(ed_val, (50, 70))

    # Indicadores contábeis usam paleta NEUTRA (cinza) — domínio separado
    # do fiscal; cores fat/imp/folenc/func ficam reservadas pra
    # Faturamento/Impostos/Folha/Funcionários no hero fiscal.
    hero_contabil = [
        {"key": "neutro", "label": "Liquidez Corrente",
         "value": _fmt_ind(float("nan") if liq_inconsistente else lc_val), "spark": chart_liq_c_spark,
         "context": lc_ctx},
        {"key": "neutro", "label": "Liquidez Seca",
         "value": _fmt_ind(float("nan") if liq_inconsistente else ls_val), "spark": chart_liq_s_spark,
         "context": ls_ctx},
        {"key": "neutro", "label": "Endividamento Total",
         "value": _fmt_pct(float("nan") if endivid_inconsistente else ed_val), "spark": chart_end_p_spark,
         "context": end_ctx},
        {"key": "neutro", "label": "Lucratividade Média",
         "value": _fmt_pct(pct_luc_med), "spark": chart_pct_luc_spark,
         "context": luc_ctx},
    ]

    # Bloco de valores do balanco (paridade com a planilha de referência): os
    # INSUMOS dos indicadores, saldo vigente no fim do periodo (ffill do
    # bal_grupo_serie). Sem balancete -> "—" (nunca "R$ 0,00" fabricado).
    # Formulas por extenso em nota muted, como a planilha de referência exibe
    # ao lado do bloco BALANCETE.
    def _bal_val(serie):
        return _fmt_brl(_last_valid(serie) if not bal_df.empty else float("nan"))

    # Card de balanco NEGATIVO sai CRU e leva a nota no proprio context —
    # nunca abs(), nunca escondido. Fonte unica com o app:
    # cards_saldo_invertido do metrics.
    _bal_inv = set(cards_saldo_invertido({
        "Ativo Circulante": ac, "Ativo Total": at_total, "Estoque": est,
        "Exigível LP": elp, "Passivo Circulante": pc}))

    def _bal_ctx(label):
        return NOTA_SALDO_INVERTIDO if label in _bal_inv else ""

    balanco_cards = [
        {"key": "neutro", "label": "Ativo Circulante",   "value": _bal_val(ac),       "context": _bal_ctx("Ativo Circulante")},
        {"key": "neutro", "label": "Ativo Total",        "value": _bal_val(at_total), "context": _bal_ctx("Ativo Total")},
        {"key": "neutro", "label": "Estoque",            "value": _bal_val(est),      "context": _bal_ctx("Estoque")},
        {"key": "neutro", "label": "Exigível LP",        "value": _bal_val(elp),      "context": _bal_ctx("Exigível LP")},
        {"key": "neutro", "label": "Passivo Circulante", "value": _bal_val(pc),       "context": _bal_ctx("Passivo Circulante")},
    ]
    # Mesma decisao do app — `bal_tem_dado_periodo`
    # (series_mensais) distingue "serie preenchida por dado REAL dentro da
    # janela" de "serie preenchida so pelo carry-in de fora dela".
    # No 2o caso a nota declara ausencia em vez de nomear um mes emprestado.
    if not S.get("bal_tem_dado_periodo", True):
        balanco_note = NOTA_BAL_SEM_DADO_PERIODO
    else:
        # Mesma decisao do app — dado real na janela, so
        # nao no ULTIMO mes (labels[-1]). "So exibicao": os valores seguem o
        # ffill de sempre, so a nota ganha a declaracao.
        _sem_mov = (", " + NOTA_BAL_SEM_MOVIMENTO_PERIODO
                   if S.get("bal_ultimo_periodo_sem_movimento", False) else "")
        balanco_note = (f"Valores do balancete em {labels[-1]}{_sem_mov} · "
                        # "/" no lugar de "÷": a extração de texto de PDF
                        # corrompe o "÷" em "+" ("LC = AC + PC");
                        # "/" sobrevive a qualquer extração e lê igual.
                        "LC = Ativo Circulante / Passivo Circulante · "
                        "LS = (Ativo Circulante − Estoque) / Passivo Circulante · "
                        "Endividamento = (Passivo Circulante + Exigível LP) / Ativo Total")

    # O cnpj de GRUPO consolidado já chega como "CNPJ raiz X (N filiais)" —
    # um prefixo fixo "CNPJ:" no header duplicaria ("CNPJ: CNPJ raiz...").
    # O label completo nasce aqui; o template exibe.
    _cnpj_esc = html.escape(cnpj)
    cnpj_label = (f"CNPJ raiz: {_cnpj_esc[len('CNPJ raiz '):]}"
                  if _cnpj_esc.startswith("CNPJ raiz ")
                  else f"CNPJ: {_cnpj_esc}")

    ctx = {
        # Unico ponto onde dado externo (nome/CNPJ da origem) entra no
        # HTML — autoescape e False por design (entidades em args de macro).
        "empresa_nome": html.escape(empresa_nome),
        "cnpj_label": cnpj_label,
        "periodo": periodo_lbl,
        "gerado_em": gerado_em,
        "n_months": n_months,
        # Faixa do Faturamento encolhe e centraliza em janela curta.
        "fat_faixa_curta": n_months <= FAIXA_CURTA_MAX_MESES,
        # Impostos e chart UNICO: sozinho na row ele ocupa
        # a largura cheia, entao herda o mesmo tratamento de janela curta do
        # Faturamento — senao 1-3 meses viram barra-paredao num slot de 1000px.
        # O limiar olha a JANELA (n_months), como o fiscal: a serie do chart
        # existe pros dois anos e nao encolhe por falta de dado de um mes.
        "imp_faixa_curta": n_months <= FAIXA_CURTA_MAX_MESES,
        # o limiar da folha olha as colunas COM DADO (`folha_col_labels`), nao
        # a janela pedida: empresa com 2 meses de folha numa janela de 6 tem o
        # mesmo problema de barra-paredao que o fiscal de 2 meses.
        "folha_faixa_curta": (folha_tem_rubrica
                              and len(folha_col_labels) <= FAIXA_CURTA_MAX_MESES),
        # A causa comunicada é o DADO.
        # Rótulo unificado quando a supressão vem de despesas (Simples sempre +
        # zero-despesas em qualquer regime); o legado fica só pro caso raro em
        # que a causa NÃO é despesa (ex.: sem faturamento com despesas presentes).
        "luc_empty_msg": (LUC_SEM_REGISTROS if luc_sem_registros
                          else "⚠️ Sem dados para calcular lucratividade"),
        "charts": {
            "fat": chart_fat,
            "fat_gastos_flat": chart_fat_gastos_flat,
            "impostos": chart_impostos,
            "lucratividade": chart_lucratividade,
            "bancos": chart_bancos,
            "folha": chart_folha,
            "caixa": chart_caixa,
            "despesas": chart_despesas,
            "folha_detalhe": chart_folha_detalhe,
        },
        "bancos_note": bancos_note,
        "bal_ausente_msg": bal_ausente_msg,
        "fat_gastos_note": fat_gastos_note,
        "balanco_cards": balanco_cards,
        "balanco_note": balanco_note,
        # Hero
        "hero_fiscal": hero_fiscal,
        "hero_contabil": hero_contabil,
        # Modelo Detalhamento de Impostos
        "hero_impostos": hero_impostos,
        "imp_rows": imp_rows,
        "imp_col_labels": imp_col_labels,
        "imp_tem_dado": imp_tem_dado,
        # Rodape do PDF: sem notas explicativas.
        # A protecao contra ler um numero pelo universo errado mora nos
        # ROTULOS — "(carga destacada + IRPJ/CSLL)", "(guias recolhidas)",
        # "(tributos exibidos)" —, que ficam sempre visiveis ao lado do
        # numero; o rodape so competiria com eles. Fica UMA nota: a
        # legenda do "—", porque simbolo sem explicacao numa pagina de
        # CLIENTE e pior que nota. No APP as demais notas continuam, que la
        # o leitor opera.
        "imp_titulo": imp_titulo,
        "imp_note": (_legenda_travessao(_T.loc[_linha_pct])
                     if imp_tem_dado else ""),
        # Modelo Detalhamento de Folha
        "hero_folha": hero_folha,
        "folha_rows": folha_rows,
        "folha_col_labels": folha_col_labels,
        "folha_tem_rubrica": folha_tem_rubrica,
        # Idem folha: so a legenda do travessao (ver comentario acima).
        "folha_notas": (_legenda_travessao(_F["pct_fat"])
                        if folha_tem_rubrica else ""),
        # UM NOME SO pro mesmo universo: o card do hero, o titulo do chart,
        # o titulo do bloco e a linha da tabela saem TODOS desta constante —
        # nunca tres jeitos de nomear o mesmo numero na mesma pagina.
        "folha_titulo_total": LINHA_FOLHA_ENCARGOS,
        "folha_destaque_rows": folha_destaque_rows,
        "folha_tem_destaque": bool(folha_destaque_rows),
        "folha_destaque_titulo": TITULO_DESTAQUES_FOLHA,
        "folha_empty_msg": folha_empty_msg,
        "imp_empty_msg": imp_empty_msg,
        # Tabela mensal de funcionários (consumida em fiscal.html quando
        # n_months > 1; mini-cards quando n_months == 1).
        "func_rows": func_rows,
        # A legenda do marcador (*) só renderiza se
        # ALGUM mês do range é parcial — nota órfã confundia.
        "func_tem_parcial": bool(partial_pm.fillna(False).any()),
    }
    return ctx


# ── HTML rendering ───────────────────────────────────────────────────────────

def _render_html(ctx, modelos=None):
    """Concatena as paginas dos `modelos` pedidos num HTML unico.

    Os templates recebem o ctx INTEIRO (**ctx): cada pagina le so as chaves
    que usa, e por isso um modelo novo nao obriga a fatiar o contexto.

    O <head> sai do base.html UMA vez e vale pro documento todo — e por isso
    que todo template de modelo TEM que estender base.html: CSS proprio de um
    modelo nao teria onde morar.
    """
    base_tpl = _jinja_env.get_template("base.html")
    base_src = base_tpl.render()
    head_end = base_src.find("</head>")
    head_section = base_src[:head_end + len("</head>")]
    head_content = head_section.split("<head>")[1].split("</head>")[0]

    bodies = []
    for template in _resolve_modelos(modelos):
        pagina = _jinja_env.get_template(template).render(**ctx)
        corpo = pagina[pagina.find("<div class=\"page\""):]
        bodies.append(corpo[:corpo.find("</body>")])
    corpo_doc = "\n".join(bodies)

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>{head_content}</head>
<body>
{corpo_doc}
</body>
</html>"""


# ── PDF conversion ───────────────────────────────────────────────────────────

def _html_to_pdf(html_str):
    import subprocess
    import sys
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        pdf_path = f.name

    try:
        proc = subprocess.run(
            [sys.executable, "pdf_render.py", pdf_path],
            input=html_str.encode("utf-8"),
            capture_output=True,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"PDF render failed: {proc.stderr.decode()}")
        with open(pdf_path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(pdf_path):
            os.unlink(pdf_path)


_BATCH_CHUNK = 10


def _reap(proc):
    """Mata o process group inteiro do render (Chromium e netos), portavel.

    subprocess mata so o filho imediato (o python do pdf_render); o Chromium e
    neto e ficaria orfao no timeout. POSIX: killpg(SIGKILL) no grupo (Popen com
    start_new_session). Windows: taskkill /F /T (arvore). Stdlib-only, sem psutil.
    """
    import signal
    import subprocess
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_render(payload, timeout, cwd):
    """Roda pdf_render.py num process group proprio, com timeout.

    Retorna (returncode, stdout_bytes, stderr_bytes, kind), kind in
    {"ok", "timeout"}. No TimeoutExpired REAPA o grupo (Chromium nao fica orfao)
    e NAO propaga — o lote segue. Popen+communicate (nao subprocess.run) pra
    poder matar o grupo: start_new_session no POSIX / CREATE_NEW_PROCESS_GROUP
    no Windows.
    """
    import subprocess
    import sys

    kwargs = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [sys.executable, "pdf_render.py"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=cwd, **kwargs,
    )
    try:
        out, err = proc.communicate(input=payload, timeout=timeout)
        return proc.returncode, out, err, "ok"
    except subprocess.TimeoutExpired:
        _reap(proc)
        try:
            out, err = proc.communicate()       # drena pipes apos o kill
        except Exception:
            out, err = b"", b""
        return proc.returncode, out, err, "timeout"


def _render_chunk(manifest, timeout):
    """Renderiza um chunk via pdf_render.py e devolve (status_by_path, proc_err).

    status_by_path: pdf_path -> {"ok", "error"} parseado do JSON que o
    pdf_render emite por item (best-effort: stdout pode vir sujo de log do
    Chromium). proc_err: None no caminho feliz, senao a causa (timeout ou rc!=0)
    usada como mensagem dos itens sem PDF/sem status. A LEITURA dos PDFs NAO
    acontece aqui — fica no chamador, por presenca de arquivo.
    """
    import json

    payload = json.dumps([
        {"html_path": m["html_path"], "pdf_path": m["pdf_path"], "index": m["index"]}
        for m in manifest
    ]).encode("utf-8")
    rc, out, err, kind = _run_render(
        payload, timeout, os.path.dirname(os.path.abspath(__file__)))
    status_by_path = {}
    try:
        for rec in json.loads(out.decode("utf-8")):
            status_by_path[rec["pdf_path"]] = rec
    except Exception:
        pass
    if kind == "timeout":
        proc_err = f"timeout apos {timeout}s (lote seguiu)"
    elif rc != 0:
        proc_err = f"render rc={rc}: {err.decode('utf-8', 'replace')[:200]}"
    else:
        proc_err = None
    return status_by_path, proc_err


def _html_to_pdf_batch(items):
    """items: list of (html_str, label). Returns list of (label, pdf_bytes | None, error | None).

    Robustez de lote: a falha de UM item nao derruba os vizinhos. _render_chunk
    isola cada render (pdf_render.py) e o coletor le os PDFs por PRESENCA em
    disco, independente do returncode — os corretos do chunk SEMPRE entram. Um
    item so vira falha se nao gerou arquivo (ou status ok=False = stub truncado).
    O rmtree do scratch fica no finally, APOS a leitura em todos os casos.
    """
    import shutil
    import tempfile

    all_results = []

    for chunk_start in range(0, len(items), _BATCH_CHUNK):
        chunk = items[chunk_start:chunk_start + _BATCH_CHUNK]
        tmp_dir = tempfile.mkdtemp(prefix="painel_pdf_")
        manifest = []

        for i, (html_str, label) in enumerate(chunk):
            html_path = os.path.join(tmp_dir, f"{i}.html")
            pdf_path = os.path.join(tmp_dir, f"{i}.pdf")
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html_str)
            manifest.append({"html_path": html_path, "pdf_path": pdf_path,
                             "label": label, "index": i})

        try:
            status_by_path, proc_err = _render_chunk(manifest, timeout=len(chunk) * 60)
            for m in manifest:
                rec = status_by_path.get(m["pdf_path"])
                failed_by_status = rec is not None and rec.get("ok") is False
                if (not failed_by_status and os.path.exists(m["pdf_path"])
                        and os.path.getsize(m["pdf_path"]) > 0):
                    with open(m["pdf_path"], "rb") as f:
                        all_results.append((m["label"], f.read(), None))
                else:
                    err = (rec.get("error") if rec else None) or proc_err or "PDF nao gerado"
                    all_results.append((m["label"], None, err))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    return all_results


# ── Public API ───────────────────────────────────────────────────────────────

def _build_html_for_empresa(codi_emps, inicio, fim, conn, empresa_nome="", cnpj="",
                            modelos=None):
    (fat_df, impd_df, folha_df, func_df, bal_df, bal_serie_df, bal_ir_df,
     regime_df, encargos_df, dre_df, imp_df, rub_df) = _load_data(conn, codi_emps, inicio, fim)
    spark_dfs = _load_spark_data(
        conn, codi_emps, inicio, fim,
        fat_df, impd_df, folha_df, func_df, bal_df,
        encargos_df=encargos_df, dre_df=dre_df, rub_df=rub_df, imp_df=imp_df,
    )
    # YoY: janela -12m dos marts leves — o FIO REAL que o default None do
    # kwarg nao cobre.
    yoy_dfs = _load_data_yoy(conn, codi_emps,
                             str(pd.Period(inicio, freq="M") - 12),
                             str(pd.Period(fim, freq="M") - 12))
    all_periods = _periods_in_range(inicio, fim)
    page_groups = [all_periods]

    all_html_pages = []
    for pp in page_groups:
        ctx = _build_context(fat_df, impd_df, folha_df, func_df, bal_df, spark_dfs,
                             all_periods, pp,
                             empresa_nome, cnpj, inicio, fim,
                             bal_serie_df=bal_serie_df, bal_ir_df=bal_ir_df,
                             regime_df=regime_df,
                             encargos_df=encargos_df, dre_df=dre_df,
                             yoy_dfs=yoy_dfs, imp_df=imp_df, rub_df=rub_df)
        all_html_pages.append(ctx)

    if len(all_html_pages) == 1:
        return _render_html(all_html_pages[0], modelos=modelos)

    parts = []
    for ctx in all_html_pages:
        html_part = _render_html(ctx, modelos=modelos)
        body_start = html_part.find("<body>") + len("<body>")
        body_end = html_part.find("</body>")
        parts.append(html_part[body_start:body_end])

    first_html = _render_html(all_html_pages[0], modelos=modelos)
    head_content = first_html.split("<head>")[1].split("</head>")[0]

    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>{head_content}</head>
<body>
{"".join(parts)}
</body>
</html>"""


def gerar_pdf_empresa(codi_emps, inicio, fim, conn, empresa_nome="", cnpj="",
                      modelos=None):
    n_months = len(_periods_in_range(inicio, fim))
    if n_months > 12:
        raise ValueError(
            f"PDF suporta no máximo 12 meses (selecionados: {n_months}, "
            f"{_p_label(inicio)} a {_p_label(fim)}). Pra comparar anos "
            f"diferentes, gere 2 PDFs separados (ex: jan-dez/25 + jan-dez/26)."
        )
    # PDF individual NAO aborta fora do modo server: segue
    # lento, e quem chama le kaleido_estado() pra avisar.
    _kaleido_resondar()
    with _contando_charts() as contador:
        html = _build_html_for_empresa(codi_emps, inicio, fim, conn, empresa_nome, cnpj,
                                       modelos=modelos)
    # Modo "off" e degradacao JA DECIDIDA (comentario
    # acima) — o PDF sai sem grafico mas NAO aborta, e quem avisa e o
    # `kaleido_estado()` que o chamador ja le. O buraco fechado aqui e
    # OUTRO: um chart falhar sozinho com o modo ainda "server"/"oneshot"
    # (a origem so faz `raise` de volta pro `_fig_to_b64` quando o smoke
    # confirma que a infra esta saudavel — ver `_kaleido_render`) e sair
    # com rc=0 como se nada tivesse faltado.
    if contador.falhas and _kaleido_modo != "off":
        raise RuntimeError(
            f"{contador.falhas} de {contador.tentativas} gráfico(s) "
            "falharam ao renderizar (kaleido reportou saudável) — PDF "
            "contraditório, não gerado")
    return _html_to_pdf(html)


def _safe_filename(nome: str) -> str:
    """Normaliza nome de empresa para uso seguro como filename.

    Pipeline: NFKD para decompor acentos, drop não-ASCII, drop punctuation, espaços→_,
    bloqueia '..' (path traversal), trunca a 80 chars.
    """
    import re
    import unicodedata
    s = unicodedata.normalize("NFKD", str(nome or "")).encode("ascii", "ignore").decode()
    s = re.sub(r"[^\w\s-]", "", s).strip()
    s = re.sub(r"\s+", "_", s)
    s = s.replace("..", "_")
    return s[:80] if s else "empresa"


def listar_unidades_lote(inicio, fim, conn, filtro="fat_or_bal", consolidar=True):
    """Retorna a lista de UNIDADES de geração de PDF (cada unidade vira 1 PDF).

    Filtros:
        "all"        - todas as ativas
        "fat_or_bal" - só com faturamento OU balancete no período
        "complete"   - só com 4+ marts dentre {fat,imp,func,folha,bal}

    Consolidação: quando True, filiais com mesmo CNPJ raiz (LEFT(cgce_emp,8)) viram
    uma unidade única (1 PDF por grupo). Singletons sempre são 1 PDF cada.

    Retorna lista de dicts: {ids, nome, nome_display, cnpj, raiz, filename,
    n_marts}. `raiz` = 8 primeiros dígitos do CNPJ (vazio quando
    inválido/ausente) — usado no label do seletor de PDFs ("NOME — raiz").

    `nome` é o nome LIMPO (filename e ordenação); `nome_display` é o que vai
    pro cabeçalho do PDF e pode carregar `" [INATIVA]"`. São campos separados
    de propósito — vide `etl.utils.marca_inativa`.
    """
    # MESMO critério do dropdown do app (etl/queries.py): a carteira do lote e
    # a da tela são uma só. Sem isso o app mostraria a inativa-com-dado e o ZIP
    # sairia sem o PDF dela.
    df = carteira_visivel(
        pd.read_sql(SQL_COBERTURA_EMPRESAS, conn, params=params_cobertura(inicio, fim))
    )
    if df.empty:
        return []

    # CNPJ raiz: inválidos/curtos viram singletons "_solo_<id>" para não colapsar
    # empresas distintas num grupo "nan".
    df["cnpj_raiz"] = df["cgce_emp"].apply(_normalize_raiz)
    mask_invalid = df["cnpj_raiz"].isna()
    df.loc[mask_invalid, "cnpj_raiz"] = df.loc[mask_invalid, "codi_emp"].apply(
        lambda e: f"_solo_{int(e)}"
    )
    flag_cols = ["tem_fat", "tem_imp", "tem_func", "tem_folha", "tem_bal"]
    df["n_marts"] = df[flag_cols].sum(axis=1)

    unidades = []
    seen_files = set()

    def _add(ids, nome, cnpj, n_marts, raiz, marca=""):
        base = _safe_filename(nome)
        fname = f"{base}.pdf"
        # evitar colisões de nome
        i = 2
        while fname in seen_files:
            fname = f"{base}_{i}.pdf"
            i += 1
        seen_files.add(fname)
        unidades.append({
            "ids": [int(x) for x in ids],
            # `nome` LIMPO: é dele que sai o filename (acima) e a ordenação.
            "nome": nome,
            "nome_display": f"{nome}{marca}",
            "cnpj": cnpj,
            # sentinela _solo_<id> (CNPJ inválido) não vaza pro label
            "raiz": "" if str(raiz).startswith("_solo_") else str(raiz),
            "filename": fname,
            "n_marts": int(n_marts),
        })

    for raiz, grupo in df.groupby("cnpj_raiz"):
        grupo = grupo.sort_values("codi_emp").reset_index(drop=True)
        if len(grupo) > 1 and consolidar:
            # Cobertura combinada do grupo
            cov = {c: bool(grupo[c].any()) for c in flag_cols}
            n_marts = sum(cov.values())
            if not _passa_filtro(cov, n_marts, filtro):
                continue
            nome_grupo = str(grupo.iloc[0]["nome_emp"])
            _add(
                ids=grupo["codi_emp"].tolist(),
                nome=f"{nome_grupo}_GRUPO",
                cnpj=f"CNPJ raiz {raiz} ({len(grupo)} filiais)",
                n_marts=n_marts,
                raiz=raiz,
            )
        else:
            for _, row in grupo.iterrows():
                cov = {c: bool(row[c]) for c in flag_cols}
                if not _passa_filtro(cov, int(row["n_marts"]), filtro):
                    continue
                _add(
                    ids=[int(row["codi_emp"])],
                    nome=str(row["nome_emp"]),
                    cnpj=str(row["cgce_emp"]) if pd.notna(row["cgce_emp"]) else "",
                    n_marts=int(row["n_marts"]),
                    raiz=row["cnpj_raiz"],
                    # Só a unidade INDIVIDUAL é marcada. A unidade de GRUPO
                    # consolida filiais e não é ela mesma inativa.
                    marca=marca_inativa(bool(row["ativa"])),
                )

    unidades.sort(key=lambda u: u["nome"].lower())
    return unidades


def _passa_filtro(cov, n_marts, filtro):
    if filtro == "all":
        return True
    if filtro == "fat_or_bal":
        return cov["tem_fat"] or cov["tem_bal"]
    if filtro == "complete":
        return n_marts >= 4
    return True


def gerar_todos_pdfs(
    inicio, fim, conn,
    filtro="fat_or_bal",
    consolidar=True,
    progress_callback=None,
    unidades=None,
    modelos=None,
):
    """Gera 1 PDF por unidade (empresa individual ou grupo consolidado).

    `unidades=None` computa a lista via listar_unidades_lote (lote TODOS);
    uma lista explícita (PDF por seleção) é usada como veio — `filtro` e
    `consolidar` são ignorados nesse caso.

    `modelos` (chaves de MODELOS) vale pro lote INTEIRO — a seleção é do run,
    não da empresa. None = registro completo, igual aos demais pontos de
    geração.

    Retorna (zip_bytes, sucessos, erros). Erros não interrompem o batch — empresas
    que falharam aparecem na lista `erros` como "<nome>: <mensagem>".

    EXCECAO: se os graficos sairem do modo server, o lote
    para na fronteira de unidade e levanta `LoteAbortadoKaleido` com o ZIP do
    que ja saiu. Em oneshot cada PDF leva minutos (horas no lote inteiro) e em
    off o PDF sai sem grafico com rc=0 -- lote quebrado, nao lote lento. A
    unidade que DESCEU pro oneshot no meio entra (o oneshot tem o mesmo pixel
    do server); a que caiu em off no meio, nao.
    """
    if unidades is None:
        unidades = listar_unidades_lote(inicio, fim, conn, filtro=filtro, consolidar=consolidar)
    if not unidades:
        return None, 0, []

    total = len(unidades)
    items = []
    erros = []
    abortado = None   # (modo, motivo, nomes pendentes)

    _kaleido_resondar()
    for idx, u in enumerate(unidades):
        if _kaleido_modo != "server":
            abortado = (_kaleido_modo, _kaleido_motivo,
                        [x["nome"] for x in unidades[idx:]])
            break
        if progress_callback:
            progress_callback(idx, total, u["nome"])
        try:
            # `nome_display` (com a marca) vai pro cabeçalho; `filename`, já
            # calculado do nome LIMPO em `_add`, é quem nomeia o arquivo.
            with _contando_charts() as contador:
                html = _build_html_for_empresa(u["ids"], inicio, fim, conn,
                                               u.get("nome_display", u["nome"]), u["cnpj"],
                                               modelos=modelos)
            if _kaleido_modo == "off":
                abortado = (_kaleido_modo, _kaleido_motivo,
                            [x["nome"] for x in unidades[idx:]])
                break
            # O modo "off" acima e degradacao JA DECIDIDA
            # (aborta o lote, nao conta). O buraco fechado aqui e OUTRO: um
            # chart falhar sozinho com o modo ainda "server"/"oneshot" (a
            # origem so faz `raise` de volta pro `_fig_to_b64` quando o
            # smoke confirma que a infra esta saudavel) — a unidade nao
            # pode contar em "Gerados" com um grafico faltando e rc=0.
            if contador.falhas:
                raise RuntimeError(
                    f"{contador.falhas} de {contador.tentativas} gráfico(s) "
                    "falharam ao renderizar (kaleido reportou saudável) — "
                    "PDF contraditório, não gerado")
            items.append((html, u["filename"]))
        except Exception as exc:
            # rollback por empresa: um erro de query psycopg2 deixa a transacao
            # compartilhada "aborted" e cascatearia "current transaction is
            # aborted" em TODAS as empresas seguintes. rollback corta a cascata;
            # em conn limpa / erro nao-DB e no-op inofensivo (guardado).
            try:
                conn.rollback()
            except Exception:
                pass
            erros.append(f"{u['nome']}: {exc}")

    if progress_callback:
        progress_callback(total, total, "renderizando PDFs...")

    if not items:
        if abortado:
            raise LoteAbortadoKaleido(*abortado[:2], None, 0, erros, abortado[2])
        return None, 0, erros

    results = _html_to_pdf_batch(items)

    buf = io.BytesIO()
    sucessos = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for label, pdf_bytes, err in results:
            if err:
                erros.append(f"{label}: {err}")
            elif pdf_bytes:
                zf.writestr(label, pdf_bytes)
                sucessos += 1

    if abortado:
        raise LoteAbortadoKaleido(abortado[0], abortado[1],
                                  buf.getvalue() if sucessos else None,
                                  sucessos, erros, abortado[2])
    return buf.getvalue(), sucessos, erros
