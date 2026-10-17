import html
import json
import os
import re
import subprocess
import sys
import time
import warnings
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import psycopg2
import streamlit as st
from dotenv import load_dotenv

from etl.mappings import GRUPOS_BAL
from etl.queries import (
    SQL_COBERTURA_EMPRESAS,
    SQL_PASSIVO_IRPJ_CSLL,
    carteira_visivel,
    params_cobertura,
    params_passivo_ir,
)
from etl.utils import (
    bal_grupo_serie,
    classify_etl_proc,
    cnpj_filial,
    cnpj_raiz,
    consume_role,
    etl_progress_frac,
    fmt_brl,
    fmt_ind,
    fmt_int,
    fmt_pct,
    label_empresa,
    marca_inativa,
    only_digits,
    p_label,
    periods_in_range,
    rotulo_tipo_process,
    rotulos_unicos,
)
from etl.metrics import (
    ALERTA_PCT_IMPOSTOS,
    LINHA_FOLHA_ENCARGOS,
    LINHA_OUTRAS_RECEITAS,
    LINHA_PCT_FAT,
    LINHA_PCT_IMPOSTOS,
    LUC_SEM_REGISTROS,
    FRASE_IMP_SEM_INFO,
    FRASE_IMP_SO_RETENCAO,
    NOTA_ENCARGOS_GUIA,
    NOTA_LINHA_13O,
    SUBTITULO_DESTAQUES_FOLHA,
    TITULO_DESTAQUES_FOLHA,
    flags_inconsistencia,
    legenda_abatimentos,
    legenda_travessao,
    folha_destaques,
    folha_por_linha,
    impostos_sem_irpj_proprio,
    NOTA_SALDO_INVERTIDO,
    NOTA_BAL_SEM_DADO_PERIODO,
    NOTA_BAL_SEM_MOVIMENTO_PERIODO,
    CTX_BALANCETE_EM_REVISAO,
    LUC_JANELA_CURTA_MESES,
    LUC_JANELA_CURTA_FRAG,
    cards_saldo_invertido,
    kpis_agregados,
    kpis_yoy,
    last_valid,
    series_mensais,
    tabela_impostos,
)

# read_sql com conexao psycopg2 avisa que prefere SQLAlchemy; o aviso nao se aplica.
warnings.filterwarnings("ignore", message=".*SQLAlchemy.*")

load_dotenv()

# Celula AUSENTE nas tabelas que podem receber NaN (Impostos, Indicadores):
# sem isto o st.dataframe desenha "None" no navegador, ignorando o "—" dos
# formatadores. As demais tabelas nao recebem NaN.
PLACEHOLDER_AUSENTE = "—"

# ── Cores ─────────────────────────────────────────────────────────────────────

COR_BOA      = "#2ecc71"
COR_RUIM     = "#e74c3c"
COR_ATENCAO  = "#f39c12"
COR_NEUTRA   = "#4a9eff"
BG_DARK      = "#0e1117"
BG_CARD      = "#1e2329"

PALETA = [
    "#4a9eff", "#2ecc71", "#f39c12", "#9b59b6",
    "#1abc9c", "#e67e22", "#3498db", "#e91e63", "#00bcd4", "#e74c3c",
]

# ── Conexão ───────────────────────────────────────────────────────────────────

def get_conn():
    return psycopg2.connect(
        host=os.getenv("PG_HOST", "localhost"),
        port=int(os.getenv("PG_PORT", 5432)),
        dbname=os.getenv("PG_DB", "painel_bi"),
        user=os.getenv("PG_USER", "postgres"),
        password=os.getenv("PG_PASS", ""),
    )

@st.cache_data(ttl=300)
def query(sql, params=None):
    # Converte tuples aninhadas (ex: lista de codi_emp) para listas, que psycopg2
    # mapeia corretamente para ARRAY em "= ANY(%s)". A tuple externa fica hashável
    # para o cache do streamlit.
    if params is not None and isinstance(params, tuple):
        params = tuple(list(p) if isinstance(p, tuple) else p for p in params)
    with get_conn() as conn:
        return pd.read_sql(sql, conn, params=params)

# ── Pivot & agregação ────────────────────────────────────────────────────────

def tabela_pivot(df, index_col, value_col, periods, agg="sum"):
    labels = [p_label(p) for p in periods]
    if df.empty:
        return pd.DataFrame(columns=labels)
    tmp = df.copy()
    tmp[index_col] = tmp[index_col].astype(str)
    piv = tmp.pivot_table(index=index_col, columns="competencia",
                         values=value_col, aggfunc=agg, fill_value=0)
    piv = piv.reindex(columns=periods, fill_value=0)
    piv.columns = labels
    return piv

# ── Formatação ────────────────────────────────────────────────────────────────

def _cell_color(v, threshold, bad_if_above):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    bad = v > threshold if bad_if_above else v < threshold
    return f"color: {COR_RUIM}; font-weight: bold" if bad else f"color: {COR_BOA}"

def style_brl(df):
    return df.style.format(fmt_brl).set_properties(**{"text-align": "right"})

def style_com_pct_row(df, pct_row, threshold, bad_if_above=True):
    outras = [i for i in df.index if i != pct_row]
    def color_row(row):
        if row.name == pct_row:
            return [_cell_color(v, threshold, bad_if_above) for v in row]
        return [""] * len(row)
    s = df.style
    if outras:
        s = s.format(fmt_brl, subset=pd.IndexSlice[outras, :])
    if pct_row in df.index:
        s = s.format(fmt_pct, subset=pd.IndexSlice[[pct_row], :])
    return s.apply(color_row, axis=1).set_properties(**{"text-align": "right"})

# ── Gauge ─────────────────────────────────────────────────────────────────────

def make_gauge(title, value, ranges):
    # Ausente != zero: sem valor, sem ponteiro, sem numero e sem
    # faixa de cor -- o "—" declara a ausencia. Zero REAL segue como "0".
    ausente = value is None or pd.isna(value)
    steps, prev = [], 0
    for rmax, cor in ranges:
        steps.append({"range": [prev, rmax], "color": cor})
        prev = rmax
    gauge = {"axis": {"range": [0, ranges[-1][0]], "tickcolor": "#aaa"},
             "steps": [] if ausente else steps,
             "bar": {"color": "#fff", "thickness": 0.2},
             "bgcolor": BG_CARD, "bordercolor": BG_CARD}
    fig = go.Figure(go.Indicator(
        mode="gauge" if ausente else "gauge+number",
        value=None if ausente else round(float(value), 2),
        title={"text": title, "font": {"size": 13, "color": "#e0e0e0"}},
        gauge=gauge,
        number={"font": {"color": "#e0e0e0", "size": 28}},
    ))
    fig.update_layout(paper_bgcolor=BG_DARK, margin=dict(t=60, b=10, l=30, r=30), height=210)
    if ausente:
        fig.add_annotation(text="—", x=0.5, y=0.25, xref="paper", yref="paper",
                           showarrow=False, font={"size": 28, "color": "#e0e0e0"})
    return fig

# ── Plotly dark layout ────────────────────────────────────────────────────────

def dark(fig, height=340):
    fig.update_layout(
        paper_bgcolor=BG_DARK, plot_bgcolor=BG_CARD,
        font=dict(color="#e0e0e0"),
        legend=dict(bgcolor=BG_CARD, bordercolor="#444"),
        xaxis=dict(gridcolor="#2a2a2a", linecolor="#555", tickfont=dict(size=11)),
        yaxis=dict(gridcolor="#2a2a2a", linecolor="#555"),
        margin=dict(t=30, b=30, l=10, r=10),
        height=height, hovermode="x unified",
    )
    return fig

# ── Dashboard para uma empresa ───────────────────────────────────────────────

def render_dashboard(emp_ids, de_str, ate_str, empresa_nome, empresa_cnpj,
                     show_ui=True):
    if isinstance(emp_ids, int):
        emp_ids = (emp_ids,)
    else:
        emp_ids = tuple(int(e) for e in emp_ids)
    _args = (emp_ids, de_str, ate_str)
    _flt = "WHERE codi_emp = ANY(%s) AND competencia>=%s AND competencia<=%s"

    fat_df    = query(f"SELECT * FROM mart.faturamento_mensal           {_flt}", _args)
    imp_df    = query(f"SELECT * FROM mart.impostos_mensais             {_flt}", _args)
    impd_df   = query(f"SELECT * FROM mart.impostos_destacados_mensais  {_flt}", _args)
    folha_df  = query(f"SELECT * FROM mart.folha_mensal                 {_flt}", _args)
    func_df   = query(f"SELECT * FROM mart.funcionarios_mensal          {_flt}", _args)
    bal_df    = query(f"SELECT * FROM mart.balancete_mensal             {_flt}", _args)
    regime_df = query(f"SELECT * FROM mart.empresa_regime_periodo       {_flt}", _args)
    # Passivo de IRPJ/CSLL — fallback para empresa que APURA e PAGA mas nao
    # emite guia pelo modulo do Dominio. Query dedicada e leve, com a janela
    # ALARGADA em +3 meses: o `params_passivo_ir` cuida disso, e o
    # alargamento nao e detalhe (a quota de jan-mar liquida o T4 do ano
    # anterior; sem ele o dezembro da janela sai vazio, em silencio).
    bal_ir_df = query(SQL_PASSIVO_IRPJ_CSLL,
                      params_passivo_ir(emp_ids, de_str, ate_str))
    # Encargos fiel a guia; guard de 1o deploy (a tabela so existe apos a
    # 1a carga do ETL) via information_schema: nunca lanca.
    _enc_exists = not query(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='mart' AND table_name='encargos_mensais'").empty
    encargos_df = (query(f"SELECT * FROM mart.encargos_mensais           {_flt}", _args)
                   if _enc_exists else pd.DataFrame())
    # DRE / resultado contabil (lucratividade); mesmo guard de 1o deploy:
    # entre o pull e a 1a carga ETL o mart nao existe e o card degrada ao
    # rotulo de supressao (transitorio esperado).
    _dre_exists = not query(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='mart' AND table_name='dre_mensal'").empty
    dre_df = (query(f"SELECT * FROM mart.dre_mensal                     {_flt}", _args)
              if _dre_exists else pd.DataFrame())
    # Folha por RUBRICA — as 8 linhas do dash de folha. Mesmo
    # guard de 1o deploy: o mart so existe apos a 1a carga COM extract das
    # tabelas novas (fomovto/foeventos/foclassificacoes). Ausente = o bloco
    # degrada pro aviso de sem-dados, nunca lanca.
    _rub_exists = not query(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='mart' AND table_name='folha_rubricas_mensais'").empty
    rub_df = (query(f"SELECT * FROM mart.folha_rubricas_mensais         {_flt}", _args)
              if _rub_exists else pd.DataFrame())

    # Seed de carry-in — última linha pré-janela por conta de
    # balanço. bal_grupo_serie (ffill por união de índice) ancora nela o
    # saldo que vem de antes da janela; sem isso, conta sem movimento no
    # 1º mês da janela apareceria zerada. SÓ os call sites de série usam
    # o frame seedado — encargos, tabela amostra e guards seguem em bal_df.
    seed_df = query(
        "SELECT DISTINCT ON (codi_emp, codi_cta) * FROM mart.balancete_mensal "
        "WHERE codi_emp = ANY(%s) AND competencia < %s "
        "AND LEFT(clas_cta,1) IN ('1','2') "
        "ORDER BY codi_emp, codi_cta, competencia DESC",
        (emp_ids, de_str),
    )
    bal_serie_df = (pd.concat([seed_df, bal_df], ignore_index=True)
                    if not seed_df.empty else bal_df)

    periods  = periods_in_range(de_str, ate_str)
    col_lbls = [p_label(p) for p in periods]

    # KPIs unificados: metrics.py é a fonte canônica. As diferenças
    # legítimas por consumer (quebra NFe/NFS-e, deltas, gauges, devoluções,
    # alerts) seguem inline abaixo.
    S = series_mensais(fat_df, impd_df, folha_df, func_df, bal_serie_df, periods,
                       encargos_df=encargos_df, dre_df=dre_df, rub_df=rub_df,
                       imp_df=imp_df, bal_ir_df=bal_ir_df)
    K = kpis_agregados(S)

    # YoY: delta dos cards = "vs mesmo periodo do ano anterior"
    # (kpis_yoy). Janela prev = [-12m] SO dos 5 marts
    # leves (sem balancete/seed — nenhum card YoY os usa). Range > 12 meses
    # fica SEM YoY: a janela -12m sobreporia a atual (delta viraria ruido).
    yoy = None
    if len(periods) <= 12:
        de_prev = str(pd.Period(de_str, freq="M") - 12)
        ate_prev = str(pd.Period(ate_str, freq="M") - 12)
        _args_prev = (emp_ids, de_prev, ate_prev)
        fat_prev = query(f"SELECT * FROM mart.faturamento_mensal          {_flt}", _args_prev)
        impd_prev = query(f"SELECT * FROM mart.impostos_destacados_mensais {_flt}", _args_prev)
        folha_prev = query(f"SELECT * FROM mart.folha_mensal               {_flt}", _args_prev)
        enc_prev = (query(f"SELECT * FROM mart.encargos_mensais            {_flt}", _args_prev)
                    if _enc_exists else pd.DataFrame())
        dre_prev = (query(f"SELECT * FROM mart.dre_mensal                  {_flt}", _args_prev)
                    if _dre_exists else pd.DataFrame())
        # o YoY tem de ler a MESMA base do card, senao o delta compara bases
        # diferentes
        rub_prev = (query(f"SELECT * FROM mart.folha_rubricas_mensais     {_flt}", _args_prev)
                    if _rub_exists else pd.DataFrame())
        # o numerador do card e a GUIA — o YoY precisa da MESMA base, senao o
        # delta compara guia contra apurado.
        imp_prev = query(f"SELECT * FROM mart.impostos_mensais             {_flt}", _args_prev)
        # O fallback de IRPJ/CSLL tem de vir nos DOIS anos. Carregar so num
        # lado faz o delta comparar numerador COM fallback contra numerador
        # SEM — degrau artificial, nao variacao do negocio.
        bal_ir_prev = query(SQL_PASSIVO_IRPJ_CSLL,
                            params_passivo_ir(emp_ids, de_prev, ate_prev))
        S_prev = series_mensais(fat_prev, impd_prev, folha_prev, pd.DataFrame(),
                                pd.DataFrame(), periods_in_range(de_prev, ate_prev),
                                encargos_df=enc_prev, dre_df=dre_prev, rub_df=rub_prev,
                                imp_df=imp_prev, bal_ir_df=bal_ir_prev)
        yoy = kpis_yoy(S, S_prev)

    def _yoy_delta(card_key, unit, color):
        # apresentacao do delta/label do kpis_yoy pro st.metric: sinal na
        # frente (o Streamlit colore por ele); label vem VERBATIM do metrics
        # (fonte unica) com cor "off"; sem YoY (range >12m) = sem delta.
        if yoy is None:
            return None, "off"
        card = yoy[card_key]
        if card["label"]:
            return card["label"], "off"
        if card["delta"] is None:
            return None, "off"
        val = f"{card['delta']:+.1f}".replace(".", ",")
        # o padrao de relatorio financeiro BR e a frase por extenso, nao a
        # abreviacao "vs".
        return f"{val}{unit} em relação ao mesmo período/{yoy['ref']}", color

    if "tipo_sai" not in fat_df.columns:
        fat_df["tipo_sai"] = "Saídas"

    # NFe/NFCe (efsaidas) e NFS-e (efservicos) são disjuntos. Consumers que
    # querem "Faturamento Total" devem somar valo_saidas + valo_servicos.
    if "valo_servicos" not in fat_df.columns:
        fat_df["valo_servicos"] = 0.0
    fat_df["valo_servicos"] = fat_df["valo_servicos"].fillna(0)
    # 3a origem: parcelas de venda de imovel. MESMO guard de
    # coluna ausente do valo_servicos — banco entre o `git pull` e o 1o run.
    if "valo_imobiliario" not in fat_df.columns:
        fat_df["valo_imobiliario"] = 0.0
    fat_df["valo_imobiliario"] = fat_df["valo_imobiliario"].fillna(0)

    fat_por_mes = S["fat_pm"]
    fat_nfe_pm  = fat_df.groupby("competencia")["valo_saidas"].sum().reindex(periods, fill_value=0)
    fat_nfse_pm = fat_df.groupby("competencia")["valo_servicos"].sum().reindex(periods, fill_value=0)
    fat_imob_pm = fat_df.groupby("competencia")["valo_imobiliario"].sum().reindex(periods, fill_value=0)

    # ── RESUMO EXECUTIVO ──────────────────────────────────────────────────────

    fat_total = K["fat_total"]

    # Consumer lê mart.impostos_destacados_mensais.
    # SUM(num)/SUM(den) é média ponderada que sobrevive à consolidação por CNPJ raiz —
    # NÃO usar mean(pct_impostos) (quebra quando filiais zeram deduções: a média
    # simples dilui o % da matriz pelas filiais zeradas).
    # min_count=1 preserva NaN quando todos os meses são sem_deducao/sem_dado (fmt_pct → '—').
    # Agregação do KPI usa SUM/SUM ao agregar meses (média ponderada), não
    # clean_mean da série mensal. O denominador do % é o fat_pm EXIBIDO (não
    # base_receita — que pra R/P é receita contábil e faria o par R$/% do card
    # não fechar); invariante
    # pct_imp_med == imp_total/fat_total testado em TestCoerenciaCardImpostos.
    pct_imp_med = K["pct_imp_med"]

    if "valo_bruto" not in folha_df.columns:
        folha_df["valo_bruto"] = 0
    folha_pm = S["folha_pm"]
    # KPI agregado usa SUM/SUM (média ponderada), mesmo padrão de
    # pct_imp_med. clean_mean de pcts
    # mensais distorce quando numerador/denominador não são proporcionais.
    pct_folenc_med = K["pct_folenc_med"]
    enc_total = K["enc_total"]

    # Lucratividade CONTABIL: resultado liquido / receita contabil do
    # mart.dre_mensal (razao) — nao o proxy NF, que inverte o sinal em
    # meses de margem baixa. So a lucratividade e contabil; % Impostos/Folha/
    # Encargos seguem NF-based.
    pct_luc_pm = S["pct_luc_pm"]
    pct_luc_med = K["pct_luc_med"]
    # Supressao SO por dado — ausencia de movimento em DESPESA
    # (mart.dre_mensal.despesas, clas 4 excl 40106). Nao ha supressao por
    # REGIME: empresa com escrituracao de resultado mostra a lucratividade real; so-receita/sem-despesa cai no rotulo unificado. O
    # metrics ja NaN-a resultado/pct_luc quando o trigger dispara.
    luc_sem_registros = S["luc_sem_despesas"]

    # Limiar unico com a linha de % da tabela: o valor e MEDIDO e a
    # justificativa vive no `ALERTA_PCT_IMPOSTOS` do metrics. Com limiares
    # diferentes a mesma tela mostraria ⚠️ no card e verde na linha.
    imp_alert = not pd.isna(pct_imp_med) and pct_imp_med > ALERTA_PCT_IMPOSTOS
    fol_alert = not pd.isna(pct_folenc_med) and pct_folenc_med > 35
    luc_alert = not pd.isna(pct_luc_med) and pct_luc_med < 0

    if show_ui:
        st.markdown("### Resumo Executivo")
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            fat_d, fat_dc = _yoy_delta("fat", "%", "normal")
            st.metric("Faturamento Total", fmt_brl(fat_total),
                      delta=fat_d, delta_color=fat_dc)
        with c2:
            # Vocabulário único + frase-régua do IRPJ/CSLL; o detalhe técnico
            # da fonte por regime segue aqui no tooltip.
            # Display do R$ com o mesmo guard NaN->0,00 do PDF (nuance sem_deducao).
            # O numerador INCLUI o IRPJ/CSLL provisionado; a linha com o R$ da
            # provisão fica neste card (fonte única K["irpj_total"]).
            _imp_total_disp = 0.0 if pd.isna(K["imp_total"]) else K["imp_total"]
            # Numerador AUSENTE nao vira "R$ 0,00": sem informacao, a frase diz
            # isso POR EXTENSO. So vale pra `sem_dado`: em `sem_deducao` a
            # empresa escritura e o zero e afirmacao que o dado sustenta,
            # entao FICA. Mesma frase do PDF.
            # DUAS ausencias, duas frases — "so tem imposto de
            # terceiro" diz POR QUE nao ha numero e e mais informativa que a
            # generica. Nenhuma das duas vira R$ 0,00.
            if S.get("imp_so_retencao"):
                _imp_str = FRASE_IMP_SO_RETENCAO
            elif K.get("imp_sem_dado"):
                _imp_str = FRASE_IMP_SEM_INFO
            else:
                _imp_str = f"{fmt_brl(_imp_total_disp)} de impostos do período"
            # Denominador POSITIVO porem imaterial suprime a razao. O rotulo
            # diz o motivo — "—" mudo faria o leitor achar que falta dado,
            # quando o que falta e BASE.
            if K.get("fat_den_insuf"):
                _imp_help = ("faturamento insuficiente para o indicador no "
                             f"período — {fmt_brl(K['fat_total'])} não sustenta "
                             f"um percentual confiável. {_imp_str}.")
            elif K["imp_sem_fat"]:
                _imp_help = (f"{_imp_str}, mas sem faturamento NF pra "
                             "basear o % — por isso o card mostra \"—\". "
                             "No grupo consolidado por CNPJ o número aparece.")
            elif S.get("imp_so_retencao"):
                _imp_help = (
                    f"{_imp_str}. Todas as guias do período são de imposto de "
                    "TERCEIRO (ISS/INSS retido, CSRF) — a empresa é agente de "
                    "retenção, não contribuinte deles. O card mede o que ela "
                    "recolheu como imposto PRÓPRIO.")
            elif K.get("imp_sem_dado"):
                # Sem numerador, a explicacao de COMO o % e composto contradiz
                # a frase. Fica so o que o leitor precisa: nao ha informacao, e
                # onde procurar. O gatilho (`imp_sem_dado`) é do universo da
                # guia (etl/metrics.py — `imp_pm` vem de `tabela_impostos`, e
                # o discriminador é `imp_guia_declarada`, não "escriturado" de
                # balancete).
                _imp_help = (f"{_imp_str}. Não há guia de imposto recolhida "
                             "no período — a tabela da seção Impostos, que "
                             "soma o mesmo valor, também fica sem a linha "
                             "TOTAL nesses meses.")
            else:
                # A régua do IRPJ/CSLL SAI no Simples — lá o imposto sobre o lucro está
                # dentro do DAS e a empresa não apura os dois. O resto do
                # tooltip fica: a explicação de onde vem o número dela
                # está na frase única abaixo (guia, sem distinção de regime).
                _regua_ir = ("" if impostos_sem_irpj_proprio(
                                 regime_df, S["periods_efetivos"], K["irpj_total"])
                             else "O valor inclui IRPJ/CSLL sobre o lucro, "
                                  "no mês em que a provisão é lançada. ")
                # O numerador é o MESMO total que a tabela da seção Impostos
                # exibe (guia + ISS pela carga) — card e tabela são o mesmo
                # universo, nunca "outro".
                _imp_help = (f"{_imp_str} — o % divide esse valor pelo "
                             f"Faturamento Total ao lado. {_regua_ir}"
                             "É o que a empresa recolheu em guia no período "
                             "(DAS, DARF, ICMS e demais), fora as retenções "
                             "de terceiros. O ISS entra pela carga "
                             "tributária, porque costuma ser retido na "
                             "fonte pelo tomador. É a mesma soma da linha "
                             "TOTAL da tabela da seção Impostos, logo "
                             "abaixo.")
            if abs(K["irpj_total"]) >= 0.005:
                _imp_help += (f"  \nIRPJ/CSLL do período: "
                              f"{fmt_brl(K['irpj_total'])}")
            imp_d, imp_dc = _yoy_delta("imp", " p.p.", "inverse")
            # `$` PAREADO vira formula LaTeX: o `help=` e markdown igual ao
            # st.caption, e este tooltip tem DOIS "R$" (o valor + a linha do
            # IRPJ/CSLL) — sem o escape o
            # trecho entre eles renderiza em fonte de formula e os cifroes
            # somem. Escape no RENDER: o builder usa fmt_brl, compartilhado
            # com o PDF, que e HTML e nao tem o problema.
            st.metric("% Impostos" + (" ⚠️" if imp_alert else ""), fmt_pct(pct_imp_med),
                      delta=imp_d, delta_color=imp_dc,
                      help=_imp_help.replace("$", "\\$"))
        with c3:
            # UM custo: a base do card e a MESMA do CUSTO TOTAL da seção
            # Folha — folha de custo COM o 13º integral + encargos da
            # guia. Convencao: tp=92 (RPA) permanece no calculo, o
            # rotulo declara e nao filtra.
            _fol_help = (f"{fmt_brl(float(folha_pm.sum()))} de folha (inclui "
                         f"13º e autônomos/RPA) + "
                         + (f"{fmt_brl(enc_total)} de encargos (guias INSS/FGTS/GRFC)"
                            if not pd.isna(enc_total) else "encargos: — (sem guia no período)")
                         + ". Mesma base do CUSTO TOTAL da seção Folha.")
            if K.get("fat_den_insuf"):
                _fol_help = ("faturamento insuficiente para o indicador no "
                             f"período — {fmt_brl(K['fat_total'])} não sustenta "
                             f"um percentual confiável.  \n{_fol_help}")
            fol_d, fol_dc = _yoy_delta("folenc", " p.p.", "inverse")
            # mesmo par de cifroes do card ao lado: folha + encargos na MESMA
            # string (e um 3o "R$" no ramo de fat_den_insuf).
            st.metric("% Folha + Encargos" + (" ⚠️" if fol_alert else ""), fmt_pct(pct_folenc_med),
                      delta=fol_d, delta_color=fol_dc,
                      help=_fol_help.replace("$", "\\$"))
        with c4:
            luc_d, luc_dc = _yoy_delta("luc", " p.p.", "normal")
            # A linha do R$ do IRPJ/CSLL fica no card % Impostos (que cobra o
            # imposto) e a margem e o LAIR — o tooltip DECLARA a base quando
            # há número.
            _luc_help_parts = []
            if K.get("luc_den_insuf"):
                # Aqui o substantivo e RECEITA — a lucratividade divide
                # pela receita CONTABIL, nao pelo faturamento NF.
                _luc_help_parts.append(
                    "receita insuficiente para o indicador no período — a "
                    "receita contábil registrada não sustenta um percentual "
                    "confiável.")
            elif luc_sem_registros:
                _luc_help_parts.append(LUC_SEM_REGISTROS)
            elif not pd.isna(pct_luc_med):
                _luc_help_parts.append(
                    "Resultado antes do IRPJ/CSLL (o imposto entra no card "
                    "% Impostos), sobre a receita contábil do período.")
            st.metric("Lucratividade" + (" ⚠️" if luc_alert else ""), fmt_pct(pct_luc_med),
                      delta=luc_d, delta_color=luc_dc,
                      help="  \n".join(_luc_help_parts) or None)
        st.markdown("---")

    # ── FATURAMENTO ───────────────────────────────────────────────────────────

    if not fat_df.empty:
        # Stacked bar: NFe (efsaidas) + NFS-e (efservicos) + Outras receitas
        # (imobiliario -- ver comentario da tabela abaixo)
        tem_nfse = fat_nfse_pm.sum() > 0
        tem_imob = fat_imob_pm.sum() > 0.005
        fig_fat = go.Figure()
        fig_fat.add_trace(go.Bar(name="NFe (saídas)", x=col_lbls,
                                 y=fat_nfe_pm.values,
                                 marker_color=PALETA[0]))
        if tem_nfse:
            fig_fat.add_trace(go.Bar(name="NFS-e", x=col_lbls,
                                     y=fat_nfse_pm.values,
                                     marker_color=PALETA[1]))
        if tem_imob:
            fig_fat.add_trace(go.Bar(name=LINHA_OUTRAS_RECEITAS, x=col_lbls,
                                     y=fat_imob_pm.values,
                                     marker_color=PALETA[2]))
        fig_fat.update_layout(barmode="stack")
        dark(fig_fat)

        # Tabela pivot com 2 origens (NFe / NFS-e) — só inclui NFS-e se houver
        nfe_long  = fat_df[["competencia", "valo_saidas"]].rename(columns={"valo_saidas": "valor"}).copy()
        nfe_long["origem"] = "NFe (saídas)"
        if tem_nfse:
            nfse_long = fat_df[["competencia", "valo_servicos"]].rename(columns={"valo_servicos": "valor"}).copy()
            nfse_long["origem"] = "NFS-e"
            fat_long = pd.concat([nfe_long, nfse_long], ignore_index=True)
        else:
            fat_long = nfe_long
        # "Outras receitas": receita que NAO vem de nota fiscal.
        # SO aparece com valor na janela (regra da origem-sem-movimento, mesmo
        # limiar 0,005 do fmt_brl): empresa sem o componente ve a tabela
        # sem essa linha. A faixa entra no chart e ganha card de componente,
        # mesma condicao `tem_imob`, mesmo rotulo -- os 3 pontos nascem e somem
        # JUNTOS; senao o card Total contradiria os componentes visiveis.
        if tem_imob:
            imob_long = fat_df[["competencia", "valo_imobiliario"]].rename(
                columns={"valo_imobiliario": "valor"}).copy()
            imob_long["origem"] = LINHA_OUTRAS_RECEITAS
            fat_long = pd.concat([fat_long, imob_long], ignore_index=True)
        piv_fat = tabela_pivot(fat_long, "origem", "valor", periods)
        piv_fat.loc["TOTAL"] = piv_fat.sum()

        if show_ui:
            st.markdown("### Faturamento")

            # Quebra visual por origem (NFe / NFS-e / [Outras receitas] / Total)
            nfe_total  = float(fat_nfe_pm.sum())
            nfse_total = float(fat_nfse_pm.sum())
            imob_total = float(fat_imob_pm.sum())
            # O card "Total" e o faturamento REAL — o MESMO numero do hero
            # "Faturamento Total". As 3 origens somam pra ele por construcao.
            tot_total  = nfe_total + nfse_total + imob_total
            dev_total  = float(fat_df["valo_devolucoes"].fillna(0).sum()) if "valo_devolucoes" in fat_df.columns else 0.0
            # Componentes primeiro, soma por ultimo --
            # NFe / NFS-e / [Outras receitas] / Total.
            if tem_imob:
                cb1, cb2, cb3, cb4 = st.columns(4)
            else:
                cb1, cb2, cb3 = st.columns(3)
            with cb1:
                st.metric("NFe (saídas)", fmt_brl(nfe_total))
            with cb2:
                st.metric("NFS-e", fmt_brl(nfse_total))
            if tem_imob:
                with cb3:
                    st.metric(LINHA_OUTRAS_RECEITAS, fmt_brl(imob_total))
                with cb4:
                    st.metric("Total", fmt_brl(tot_total))
            else:
                with cb3:
                    st.metric("Total", fmt_brl(tot_total))

            # Métrica auxiliar (contador): devoluções de venda — JÁ abatidas do NFe.
            cd1, _, _ = st.columns(3)
            with cd1:
                st.metric(
                    "Devoluções de venda",
                    fmt_brl(dev_total),
                    help="Soma de notas de devolução de venda no período (CFOPs da SEFAZ; devolução de transferência não entra). Já abatidas do Faturamento (NFe e Total), no mês da entrada da nota, como no Relatório de Faturamento do Domínio; por isso o denominador de % Impostos / % Folha também é líquido.",
                )

            st.plotly_chart(fig_fat, width="stretch")
            st.dataframe(style_brl(piv_fat), width="stretch")
            st.markdown("---")
    elif show_ui:
        st.markdown("### Faturamento")
        st.warning("⚠️ Sem dados de faturamento para o período selecionado")
        st.markdown("---")

    # ── IMPOSTOS ──────────────────────────────────────────────────────────────

    # Tabela + series do bloco Impostos: TODA a decisao (imposto proprio,
    # de-dup do ISS pela carga, corte de tributo zerado, predicado
    # so_retencao) mora em etl.metrics.tabela_impostos — fonte unica com o
    # PDF de Detalhamento de Impostos. Aqui so a renderizacao.
    # `linha_extra` = as duas linhas do fallback contabil, VINDAS DA MESMA
    # serie que o `series_mensais` usou pro numerador do card. Nao ha segunda
    # derivacao, entao card e tabela sao o mesmo numero por construcao —
    # espelho literal de `pdf_generator._extra`.
    _I = tabela_impostos(imp_df, impd_df, periods, fat_por_mes,
                         linha_extra=S.get("irpj_contabil") or None)

    if _I["tem_dado"]:
        fig_imp = go.Figure()
        for i, nome in enumerate(_I["series_chart"]):
            fig_imp.add_trace(go.Bar(name=nome, x=col_lbls,
                                     y=_I["series_chart"][nome].values,
                                     marker_color=PALETA[i % len(PALETA)]))
        fig_imp.update_layout(barmode="group")
        dark(fig_imp)

        piv_imp = _I["tabela"].copy()
        piv_imp.columns = [p_label(c) for c in piv_imp.columns]

        if show_ui:
            st.markdown("### Impostos")
            st.plotly_chart(fig_imp, width="stretch")
            # Sem `column_config` de largura AQUI: sem width explicito o
            # Streamlit auto-dimensiona a coluna de indice ao conteudo, e o
            # rotulo mais longo (LINHA_PCT_IMPOSTOS) ja aparece INTEIRO. Forcar
            # "large" aqui alargaria a coluna e comeria UM MES de tela.
            # NAO confundir com a tabela de folha, que precisa do "large":
            # width explicito CAPA a largura. E outro caso, nao o mesmo.
            st.dataframe(style_com_pct_row(piv_imp, LINHA_PCT_IMPOSTOS,
                                           ALERTA_PCT_IMPOSTOS),
                         width="stretch", placeholder=PLACEHOLDER_AUSENTE)
            st.caption(
                ("ISS pela carga tributária (balancete, guia ou retido na "
                 "fonte); demais tributos pelas guias recolhidas — "
                 if _I["iss_fontes"] else
                 "impostos que a empresa recolheu no período "
                 "(DAS, DARF e demais guias) — ")
                + "retenções de terceiros e tributos de SCP ficam fora")
            st.markdown("---")
    elif show_ui:
        st.markdown("### Impostos")
        if _I["so_retencao"]:
            # a empresa TEM guias, mas todas de imposto de terceiro: dizer
            # isso e informacao; "sem dados" seria mentira.
            st.info("Sem impostos próprios no período (apenas retenções de "
                    "terceiros)")
        else:
            st.warning("⚠️ Sem dados de impostos para o período selecionado")
        st.markdown("---")

    # ── FUNCIONÁRIOS ──────────────────────────────────────────────────────────

    if not func_df.empty:
        # series do metrics (fonte unica com o pdf_generator)
        adm = S["adm_pm"]
        res = S["dem_pm"]
        atv = S["atv_pm"]
        # is_partial agregado por mês — qualquer filial parcial marca o mês.
        partial_pm = S["is_partial_pm"]
        col_lbls_func = [
            f"{lbl} ⚠️" if partial_pm.get(p, False) else lbl
            for lbl, p in zip(col_lbls, periods)
        ]

        fig_func = go.Figure()
        fig_func.add_trace(go.Bar(name="Entradas", x=col_lbls_func, y=adm.values,
                                  marker_color=COR_BOA, yaxis="y2", opacity=0.75))
        fig_func.add_trace(go.Bar(name="Saídas", x=col_lbls_func, y=res.values,
                                  marker_color=COR_RUIM, yaxis="y2", opacity=0.75))
        fig_func.add_trace(go.Scatter(name="Ativos (fim do mês)", x=col_lbls_func, y=atv.values,
                                      mode="lines+markers",
                                      line=dict(color=COR_NEUTRA, width=2),
                                      marker=dict(size=6)))
        fig_func.update_layout(
            barmode="group",
            yaxis=dict(title="Funcionários ativos"),
            yaxis2=dict(title="Movimentação", overlaying="y", side="right"),
        )
        dark(fig_func)

        tbl_f = pd.DataFrame({"Entradas": adm.values, "Saídas": res.values,
                               "Ativos (fim)": atv.values}, index=col_lbls_func).T

        if show_ui:
            st.markdown("### Funcionários")
            st.caption("Meses marcados com ⚠️ podem estar com dados em fechamento.")
            st.plotly_chart(fig_func, width="stretch")
            st.dataframe(tbl_f.style.format(fmt_int).set_properties(**{"text-align": "right"}),
                         width="stretch")
            st.markdown("---")
    elif show_ui:
        st.markdown("### Funcionários")
        st.warning("⚠️ Sem dados de funcionários para o período selecionado")
        st.markdown("---")

    # ── FOLHA E ENCARGOS ──────────────────────────────────────────────────────

    has_folha = not folha_df.empty

    # Tabela UNICA de custo de folha — UM custo, UM percentual.
    # A particao das linhas, o corte das opcionais sem movimento e o % moram no
    # metrics (fonte unica com o PDF); aqui so a renderizacao. Grao do mart e
    # por EMPRESA (o de pessoa fica no raw, LGPD).
    _R = folha_por_linha(rub_df, periods, encargos_df=encargos_df,
                         fat_pm=fat_por_mes)
    _rub_tem_dado = not _R["tabela"].empty and len(_R["tabela"].columns) > 0

    if _rub_tem_dado:
        # UMA barra por mes, com o total: a altura e o CUSTO TOTAL da coluna,
        # sem decomposicao em cores. Uma pilha das linhas da tabela,
        # inclusive a faixa "Outros", mostraria no visual um agrupamento que
        # a tabela nao tem, e o grafico desmentiria o numero na mesma tela.
        # SEM YoY: isso e do PDF. Aqui a janela vai a 48 meses e o leitor e
        # operador — o par de barras por mes viraria uma cerca.
        fig_folha = go.Figure(go.Bar(
            x=[p_label(c) for c in _R["tabela"].columns],
            y=[float(v) for v in _R["custo"]], marker_color=PALETA[0]))
        dark(fig_folha, height=300)
    elif has_folha:
        # Degradacao (empresa sem carga do mart de rubricas): o chart por
        # tipo_process fica como rede, senao a secao perde o unico visual.
        fig_folha = go.Figure()
        for i, tp in enumerate(folha_df["tipo_process"].unique()):
            vals = (folha_df[folha_df["tipo_process"] == tp]
                    .groupby("competencia")["valo_bruto"].sum()
                    .reindex(periods, fill_value=0))
            fig_folha.add_trace(go.Bar(name=rotulo_tipo_process(tp),
                                       x=col_lbls, y=vals.values,
                                       marker_color=PALETA[i % len(PALETA)]))
        fig_folha.update_layout(barmode="stack")
        dark(fig_folha, height=300)

    if show_ui and has_folha:
        st.markdown("### Folha")
        if _rub_tem_dado:
            _cols = list(_R["tabela"].columns)
            _lbls = [p_label(c) for c in _cols]

            def _tab(frame):
                # altura explicita: o default do Streamlit corta as ultimas
                # linhas, e as ultimas sao justamente as que importam.
                st.dataframe(
                    frame.style.set_properties(**{"text-align": "right"}),
                    width="stretch",
                    height=(len(frame) + 1) * 35 + 3,
                    # "large": rotulo cortado e informacao perdida — a mesma
                    # regra do PDF; alargar, nunca cortar.
                    column_config={
                        "_index": st.column_config.Column(width="large")})

            # EM CIMA, O TOTAL: quem le o relatorio quer o custo do mes e o
            # quanto ele pesa sobre o faturamento. As rubricas destacadas estao
            # logo abaixo, ja CONTIDAS neste numero.
            _tot = pd.DataFrame(index=[LINHA_FOLHA_ENCARGOS, LINHA_PCT_FAT],
                                columns=_lbls, dtype=object)
            _tot.loc[LINHA_FOLHA_ENCARGOS] = [fmt_brl(v) for v in _R["custo"]]
            _tot.loc[LINHA_PCT_FAT] = [fmt_pct(v) for v in _R["pct_fat"]]
            _tab(_tot)
            _leg = legenda_travessao(_R["pct_fat"])
            if _leg:
                st.caption(_leg)
            # O 13o fica DENTRO do total, sem linha propria — a nota explica
            # em que mes ele pousa.
            st.caption(f"13º Salário: {NOTA_LINHA_13O}.")
            _ant = float(_R["antecipacoes"].sum())
            if _ant > 0:
                # A 1ª parcela do 13º NAO entra nesta lista — ela conta no
                # mês em que é paga. A frase só vale pros três: o
                # adiantamento (evt 981), o recibo de férias (evt 937) e o RPA
                # são recuperados DENTRO do tp11 mesmo. A 1ª parcela não — o
                # espelho dela está no fechamento, não no mensal.
                st.caption(f"Adiantamento, recibo de férias e RPA "
                           f"({fmt_brl(_ant)} no período) já estão dentro da "
                           f"folha mensal — não entram como linha própria.")
            st.caption(NOTA_ENCARGOS_GUIA)

            # EMBAIXO, OS PRINCIPAIS CUSTOS — lista FIXA e CONTIDA no total
            # acima. Rubrica sem valor na janela nao aparece: o
            # corte mora no metrics, entao o PDF herda identico. O subtitulo e
            # a UNICA protecao contra somar o detalhe com o total (a contencao
            # nao e uma identidade, nao ha gate aritmetico possivel).
            _det = folha_destaques(_R["tabela"])
            if not _det.empty:
                st.markdown(f"**{TITULO_DESTAQUES_FOLHA}**")
                st.caption(SUBTITULO_DESTAQUES_FOLHA)
                _tab(pd.DataFrame(
                    {lbl: [fmt_brl(v) for v in _det[c]]
                     for c, lbl in zip(_det.columns, _lbls)},
                    index=_det.index))

            st.markdown(f"**{LINHA_FOLHA_ENCARGOS}**")
            st.plotly_chart(fig_folha, width="stretch")
            # Declara o que saiu do custo (afastamento do INSS, faltas,
            # salario familia compensado). Sem isso o numero cai e ninguem sabe
            # por que — e a ponte pra conciliar com o "Resumo da Folha".
            _leg_abat = legenda_abatimentos(_R.get("abatimentos"))
            if _leg_abat:
                # `$` PAREADO vira matematica LaTeX no markdown do Streamlit:
                # esta legenda lista tres naturezas com valor, entao sem escape
                # o render come os cifroes e joga trechos em fonte de formula.
                # As captions de um "R$" so nao sofrem (cifrao impar nao fecha
                # par). O escape mora AQUI, no render, e nao no helper — o PDF
                # consome o mesmo texto e e HTML.
                st.caption(_leg_abat.replace("$", "\\$"))
        else:
            st.info("⚠️ Sem folha por rubrica no período — rode o ETL para "
                    "carregar o detalhamento.")
            st.markdown("**Folha Bruta (por tipo de processo)**")
            st.plotly_chart(fig_folha, width="stretch")
        st.markdown("---")

    # ── BALANCETE ─────────────────────────────────────────────────────────────

    BAL_VISUAL = {
        "Caixa":              GRUPOS_BAL["Caixa"],
        "Bancos":             GRUPOS_BAL["Bancos"],
        "Aplicações":         GRUPOS_BAL["Aplicações"],
        "Ativo Circulante":   GRUPOS_BAL["Ativo Circulante"],
        "Estoque":            GRUPOS_BAL["Estoque"],
        "Passivo Circulante": GRUPOS_BAL["Passivo Circulante"],
        "Exigível LP":        GRUPOS_BAL["Exigível LP"],
        "Patrimônio Líquido": GRUPOS_BAL["Patrimônio Líquido"],
        "Receitas":           GRUPOS_BAL["Receitas"],
        # Paridade com a planilha de referência: rotulo e recorte — 401-405,
        # SEM IRPJ/CSLL (406). O 406 segue nos impostos e dentro da
        # lucratividade (base despesas_pm do metrics).
        "Despesas e Custos":  GRUPOS_BAL["Despesas e Custos"],
    }

    rows_bal = {}
    for label, prefixes in BAL_VISUAL.items():
        s = bal_grupo_serie(bal_serie_df, periods, prefixes)
        if abs(s).sum() > 0:
            rows_bal[label] = s.values

    # Sem NENHUMA linha de balanco na janela (o MESMO criterio
    # dos Indicadores, `bal_tem_dado_periodo`) o que a tabela/chart mostrariam
    # e saldo HERDADO do seed, reto em todos os meses. Ausencia se declara:
    # nem tabela, nem grafico, nem o aviso de "grupos padrao" (que e sobre
    # plano de contas, nao sobre ausencia). O mes e o do seed (ja filtrado por
    # ANY(emp_ids) e pre-janela); em grupo, o max entre as filiais.
    if not S.get("bal_tem_dado_periodo", True):
        if show_ui:
            st.markdown("### Balancete")
            _ult = (seed_df["competencia"].max() if not seed_df.empty else None)
            st.info("Sem balancete no período" +
                    (f" — último balancete: {p_label(_ult)}" if _ult else ""))
            st.markdown("---")
    elif rows_bal:
        piv_bal = pd.DataFrame(rows_bal, index=col_lbls).T

        fig_bal = go.Figure()
        for i, (grupo, vals) in enumerate(rows_bal.items()):
            fig_bal.add_trace(go.Scatter(name=grupo, x=col_lbls, y=vals,
                                         mode="lines+markers",
                                         line=dict(color=PALETA[i % len(PALETA)], width=2),
                                         marker=dict(size=5)))
        dark(fig_bal)

        if show_ui:
            st.markdown("### Balancete")
            st.dataframe(style_brl(piv_bal), width="stretch")
            st.plotly_chart(fig_bal, width="stretch")
            # Banco com saldo credor
            # (negativo) ganha anotacao em vez de renderizar cru.
            _bancos_neg = [lbl for lbl in ("Bancos", "Aplicações")
                           if lbl in rows_bal
                           and (pd.Series(rows_bal[lbl]).fillna(0) < 0).any()]
            if _bancos_neg:
                st.caption(f"⚠️ {' e '.join(_bancos_neg)} com saldo credor "
                           f"(negativo) no período")
            st.markdown("---")
    elif show_ui:
        st.markdown("### Balancete")
        st.warning("Nenhum dos grupos padrão encontrado no plano de contas.")
        if not bal_df.empty:
            amostra = bal_df.groupby("clas_cta")["valo_saldo"].sum().reset_index()
            amostra = amostra.sort_values("valo_saldo", ascending=False).head(25)
            st.dataframe(amostra, width="stretch")
        st.markdown("---")

    # ── INDICADORES ───────────────────────────────────────────────────────────

    ac  = S["ac"]
    est = S["est"]
    pc  = S["pc"]
    elp = S["elp"]
    pl  = S["pl"]
    at  = S["at"]

    liq_c = S["liq_c"]
    liq_s = S["liq_s"]
    end_p = S["end_p"]

    # Inconsistência canônica via metrics.py (guard isna).
    # Gauges e ranges de cor são UI do Streamlit (diferença legítima do app).
    liq_inc, endiv_inc = flags_inconsistencia(ac, pc, pl, at)
    _lc_ranges = [(3.0, COR_RUIM)] if liq_inc else [(1.0,COR_RUIM),(1.5,COR_ATENCAO),(3.0,COR_BOA)]
    _ls_ranges = [(2.0, COR_RUIM)] if liq_inc else [(0.5,COR_RUIM),(1.0,COR_ATENCAO),(2.0,COR_BOA)]
    gauge_lc = make_gauge("Liquidez Corrente",    last_valid(liq_c), _lc_ranges)
    gauge_ls = make_gauge("Liquidez Seca",        last_valid(liq_s), _ls_ranges)
    _ed_val = last_valid(end_p)
    _ed_negativo = bool(not pd.isna(_ed_val) and _ed_val < 0)
    _ed_ranges = ([(120, COR_RUIM)] if (_ed_negativo or endiv_inc)
                  else [(50,COR_BOA),(70,COR_ATENCAO),(120,COR_RUIM)])
    gauge_ed = make_gauge("Endividamento (%)",    _ed_val, _ed_ranges)

    # A cor sozinha (acima) nao declara o motivo — o selo "balancete em
    # revisão" (mesma fonte etl.metrics do PDF) declara. O numero cru
    # CONTINUA visivel: NAO mascara ac/pc/liq_c/etc — so acrescenta o selo.
    _revisao = [lbl for lbl, flag in (("Liquidez Corrente", liq_inc),
                                      ("Liquidez Seca", liq_inc),
                                      ("Endividamento (%)", endiv_inc or _ed_negativo))
               if flag]

    cores_luc = [COR_BOA if not pd.isna(v) and v >= 0 else COR_RUIM for v in pct_luc_pm.values]
    fig_luc = go.Figure(go.Bar(x=col_lbls, y=pct_luc_pm.values,
                               marker_color=cores_luc, name="Lucratividade %"))
    fig_luc.add_hline(y=0, line_dash="dot", line_color="#888")
    dark(fig_luc, height=260)
    fig_luc.update_layout(showlegend=False, yaxis_title="Lucratividade (%)")

    tbl_ind = pd.DataFrame({
        "Liquidez Corrente": liq_c.values,
        "Liquidez Seca":     liq_s.values,
        "Endividamento (%)": end_p.values,
        "Lucratividade (%)": pct_luc_pm.values,
    }, index=col_lbls).T

    if show_ui:
        st.markdown("### Indicadores Financeiros")
        g1, g2, g3 = st.columns(3)
        with g1:
            st.plotly_chart(gauge_lc, width="stretch")
        with g2:
            st.plotly_chart(gauge_ls, width="stretch")
        with g3:
            st.plotly_chart(gauge_ed, width="stretch")
        if _revisao:
            st.caption(f"⚠️ {' · '.join(_revisao)}: {CTX_BALANCETE_EM_REVISAO} "
                       "— a cor do medidor não é a única informação; o "
                       "número segue exibido cru (aqui e na tabela abaixo).")
        # Paridade com a planilha de referência: valores-insumo dos indicadores (espelho
        # dos cards do PDF; saldo vigente no fim do periodo via ffill) +
        # formulas por extenso como caption muted. Sem balancete -> "—".
        b1, b2, b3, b4, b5 = st.columns(5)
        _sem_bal = bal_df.empty
        def _bal_val(serie):
            return fmt_brl(float("nan") if _sem_bal else last_valid(serie))
        with b1:
            st.metric("Ativo Circulante", _bal_val(ac))
        with b2:
            st.metric("Ativo Total", _bal_val(at))
        with b3:
            st.metric("Estoque", _bal_val(est))
        with b4:
            st.metric("Exigível LP", _bal_val(elp))
        with b5:
            st.metric("Passivo Circulante", _bal_val(pc))
        # Card negativo sai CRU e ganha anotacao — mesmo mecanismo de
        # Bancos/Aplicacoes.
        # Esconder apagaria a evidencia (defeito de escrituracao na origem).
        _bal_inv = [] if _sem_bal else cards_saldo_invertido({
            "Ativo Circulante": ac, "Ativo Total": at, "Estoque": est,
            "Exigível LP": elp, "Passivo Circulante": pc})
        if _bal_inv:
            st.caption(f"⚠️ {' · '.join(_bal_inv)}: "
                       f"{NOTA_SALDO_INVERTIDO}")
        # Declara o mes do balancete, como o PDF (balanco_note): o valor e o
        # ffill do ultimo saldo conhecido, e sem o mes o leitor nao sabe de
        # quando ele e. col_lbls[-1] e o MESMO ponto que o last_valid dos 5
        # cards escolhe.
        # col_lbls[-1] SO e confiavel quando ha balancete
        # DENTRO da janela — bal_grupo_serie fecha com fillna(0.0), mas
        # `bal_tem_dado_periodo` (series_mensais) distingue "serie preenchida
        # por dado real" de "serie preenchida so pelo carry-in de fora da
        # janela". No 2o caso a caption declara ausencia em vez de
        # nomear um mes cujo saldo nao e desta janela — "zero afirma, ausencia
        # declara".
        if not S.get("bal_tem_dado_periodo", True):
            st.caption(NOTA_BAL_SEM_DADO_PERIODO)
        else:
            # HA dado real na janela, so nao no ULTIMO
            # mes (col_lbls[-1]) — o valor exibido segue o ffill de sempre
            # ("so exibicao": saldo patrimonial e cumulativo, apagar seria
            # errado), a caption e que ganha a declaracao pra nao insinuar
            # movimento onde houve carry-in.
            _sem_mov = (", " + NOTA_BAL_SEM_MOVIMENTO_PERIODO
                       if S.get("bal_ultimo_periodo_sem_movimento", False) else "")
            st.caption(f"Valores do balancete em {col_lbls[-1]}{_sem_mov} · "
                       "LC = Ativo Circulante ÷ Passivo Circulante · "
                       "LS = (Ativo Circulante − Estoque) ÷ Passivo Circulante · "
                       "Endividamento = (Passivo Circulante + Exigível LP) ÷ Ativo Total")
        if luc_sem_registros:
            st.info(LUC_SEM_REGISTROS)
        elif pct_luc_pm.notna().any():
            st.plotly_chart(fig_luc, width="stretch")
            if len(periods) <= LUC_JANELA_CURTA_MESES:
                # NAO clampar; ganha selo de janela curta — poucos meses concentram o resultado de um so mes
                # no chart, e o numero pode sair extremo. O chart segue
                # exibindo o valor CRU (nunca capado); a caption so declara o
                # contexto que explica a magnitude.
                st.caption(f"⚠️ {LUC_JANELA_CURTA_FRAG} — poucos meses podem "
                           "extremar a lucratividade (número exibido cru, "
                           "sem clamp).")
        else:
            # guard de cobertura de receita e demais sem-serie:
            # fallback GENERICO — nunca chart vazio mudo nem o rotulo de despesas.
            st.info("⚠️ Sem dados para calcular lucratividade")
        st.dataframe(tbl_ind.style.format(fmt_ind).set_properties(**{"text-align": "right"}),
                     width="stretch", placeholder=PLACEHOLDER_AUSENTE)
        if _revisao:
            # a tabela nao tem cor pro numero cru de linha inconsistente —
            # declaracao explicita aqui.
            st.caption(f"{', '.join(_revisao)}: {CTX_BALANCETE_EM_REVISAO} "
                       "— número acima é o dado bruto, não confiável.")

    return


# ══════════════════════════════════════════════════════════════════════════════
# INÍCIO DA APP
# ══════════════════════════════════════════════════════════════════════════════

st.set_page_config(page_title="Painel Contábil", layout="wide", page_icon="📊")

# Logo no topo da sidebar; precisa de alpha real e texto claro pro tema
# escuro. Guard de arquivo ausente: o logo e cosmetico e NUNCA pode derrubar
# o dashboard.
_LOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "assets", "logo.png")
if os.path.exists(_LOGO):
    st.logo(_LOGO, size="large")
    # UNICA regra de CSS do tema: st.logo trava a altura em 32px no
    # "large" — nesse tamanho o texto do logo fica ilegivel. Nao ha parametro
    # pra isso e o theme nao alcanca. O logo ocupa a largura util do topo da
    # sidebar: o wrapper do <img> dimensiona por CONTEUDO (width:100% no img
    # sozinho e circular), entao quem cresce e o wrapper (flex:1) — o header
    # e flex/space-between e o botao de colapsar continua ancorado a direita.
    # Altura AUTO preserva o aspecto do arquivo. Se um Streamlit futuro mudar
    # o markup, o sintoma e o logo voltar ao tamanho pequeno: cosmetico, nao
    # quebra nada.
    st.markdown(
        """
        <style>
        /* header tem height:60px fixo — o logo maior vazava por cima e o
           topo das letras saia cortado; altura auto deixa a faixa crescer */
        [data-testid="stSidebarHeader"] {
            height: auto;
            padding-top: 0.75rem;
            padding-bottom: 0.25rem;
        }
        [data-testid="stSidebarHeader"] > div:first-child {
            flex: 1 1 auto;
            min-width: 0;
        }
        [data-testid="stSidebarLogo"] {
            width: 100%;
            height: auto !important;
            max-height: none !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

# ── Slot global do subprocess de ETL ─────────────────────────────────────────

@st.cache_resource
def _etl_slot():
    """Slot ÚNICO server-wide do subprocess de ETL.

    Vive no processo do servidor (st.cache_resource): sobrevive a reruns,
    F5 e segunda aba — session_state não (morre com a sessão e abriria
    caminho pra um 2º pipeline). Mutado IN-PLACE: mutação de dict não passa
    pelo SafeSessionState, cujas operações são yield points que podem
    levantar RerunException ANTES de persistir (perderia linha já
    consumida da Queue). O Lock fecha a corrida de spawn entre sessões.
    """
    import threading as _th
    return {"lock": _th.Lock()}


_ETL_SLOT_KEYS = ("proc", "q", "t0", "n_ok", "lines", "last_output",
                  "cancelled", "timed_out", "cancel_req", "inicio", "fim",
                  "janela_ctx")


def _etl_cleanup():
    """Desarma o slot e volta a sessão pra idle.

    Mutações do slot (dict puro) não são interrompíveis; as 2 ops de
    session_state no fim são yield points — interrompido entre elas, o
    pior caso é stage="running" com slot vazio, que o caminho
    "absent"-sem-etl_spawn resolve sem respawn.
    """
    slot = _etl_slot()
    for k in _ETL_SLOT_KEYS:
        slot.pop(k, None)
    st.session_state.pop("etl_spawn", None)
    st.session_state["etl_stage"] = "idle"


# ── Sidebar ───────────────────────────────────────────────────────────────────

def _make_badges(cov):
    badges = ""
    for flag, icone in (("tem_fat", "📊"), ("tem_imp", "💰"), ("tem_func", "👥"),
                        ("tem_folha", "💵"), ("tem_bal", "📒")):
        if cov.get(flag):
            badges += icone
    return badges or "⚠️"


def _watermark_atual():
    """Última competência processada (ops.controle_runs) — só
    leitura PG, barata pro sidebar; None se a tabela ainda não existe
    (antes do 1º run). Definida ANTES do sidebar (caption usa na seleção)."""
    try:
        with get_conn() as c:
            with c.cursor() as cur:
                cur.execute("SELECT to_regclass('ops.controle_runs')")
                if cur.fetchone()[0] is None:
                    return None
                cur.execute(
                    "SELECT MAX(watermark) FROM ops.controle_runs WHERE "
                    "status = 'ok' AND empresa = 'all' AND modo = 'completo'"
                )
                row = cur.fetchone()
                return row[0] if row else None
    except psycopg2.Error:
        return None


with st.sidebar:
    st.markdown("---")

    # Flag de running calculada ANTES de qualquer widget (todos
    # recebem o disabled desta flag). Se existe pipeline vivo no slot
    # global mas a sessão está idle (F5/aba nova), re-attacha a exibição.
    if (st.session_state.get("etl_stage") != "running"
            and classify_etl_proc(_etl_slot().get("proc")) == "alive"):
        st.session_state["etl_stage"] = "running"
    _etl_running = st.session_state.get("etl_stage") == "running"

    # ── Período global: competências disponíveis em qualquer mart ────────────
    comp_all = query("""
        SELECT DISTINCT competencia FROM (
            SELECT competencia FROM mart.faturamento_mensal
            UNION SELECT competencia FROM mart.impostos_mensais
            UNION SELECT competencia FROM mart.folha_mensal
            UNION SELECT competencia FROM mart.funcionarios_mensal
            UNION SELECT competencia FROM mart.balancete_mensal
        ) t ORDER BY competencia
    """)

    if comp_all.empty:
        st.error("Nenhum dado encontrado nos marts.")
        st.stop()

    comps_list = sorted(comp_all["competencia"].tolist())
    comp_labels = [p_label(c) for c in comps_list]

    st.markdown("**Período**")
    st.caption(f"{len(comps_list)} meses disponíveis: {comp_labels[0]} a {comp_labels[-1]}")
    idx_de  = st.selectbox("De",  range(len(comps_list)), index=0,
                           format_func=lambda i: comp_labels[i], key="sel_de",
                           disabled=_etl_running)
    idx_ate = st.selectbox("Até", range(len(comps_list)), index=len(comps_list) - 1,
                           format_func=lambda i: comp_labels[i], key="sel_ate",
                           disabled=_etl_running)

    de_str  = comps_list[idx_de]
    ate_str = comps_list[idx_ate]

    if de_str > ate_str:
        st.error("'De' deve ser anterior ou igual a 'Até'.")
        st.stop()

    n_months_sel = len(periods_in_range(de_str, ate_str))
    pdf_disabled = n_months_sel > 12
    if pdf_disabled:
        st.warning(
            f"⚠️ PDF suporta no máximo 12 meses (selecionados: {n_months_sel}). "
            "Pra comparar anos diferentes, gere 2 PDFs separados."
        )

    st.markdown("---")
    consolidar = st.checkbox("🔗 Mostrar grupos consolidados", value=True,
                             help="Agrupa filiais com mesmo CNPJ raiz num único item",
                             disabled=_etl_running)

    # ── Empresas + cobertura por mart no período selecionado ─────────────────
    # `carteira_visivel` é o critério de quem aparece (etl/queries.py): ativa,
    # OU inativa que ainda tem dado nesta janela. O `pdf_generator` chama o
    # MESMO helper — a carteira do dropdown e a do lote de PDF são uma só.
    cov_df = carteira_visivel(
        query(SQL_COBERTURA_EMPRESAS, params_cobertura(de_str, ate_str))
    )

    if cov_df.empty:
        st.warning("Nenhuma empresa cadastrada.")
        st.stop()

    # Se NENHUMA empresa tem dado em qualquer mart no período, avisa antes do dropdown
    _flag_cols = ["tem_fat", "tem_imp", "tem_func", "tem_folha", "tem_bal"]
    if not cov_df[_flag_cols].any().any():
        st.warning("⚠️ Nenhuma empresa tem dados no período selecionado. "
                   "Ajuste o período ou rode 'Atualizar Dados' no sidebar.")

    cov_df["cnpj_raiz"] = cov_df["cgce_emp"].apply(cnpj_raiz)
    # Para CNPJs inválidos/NULL, atribuímos uma chave única por codi_emp para que o
    # groupby produza grupos de 1 elemento — assim caem no branch singleton.
    mask_invalid = cov_df["cnpj_raiz"].isna()
    cov_df.loc[mask_invalid, "cnpj_raiz"] = cov_df.loc[mask_invalid, "codi_emp"].apply(
        lambda e: f"_solo_{int(e)}"
    )

    # Construir lista de itens do dropdown:
    # cada item = (label, ids_tuple, nome_exibicao, cnpj_exibicao)
    itens = []  # (sort_key, ordem, label, ids, nome, cnpj)
    cov_by_id = {int(r.codi_emp): {
        "tem_fat": bool(r.tem_fat), "tem_imp": bool(r.tem_imp),
        "tem_func": bool(r.tem_func), "tem_folha": bool(r.tem_folha),
        "tem_bal": bool(r.tem_bal),
    } for r in cov_df.itertuples()}
    # Quem está na carteira só porque ainda tem dado na janela (`carteira_visivel`).
    # A ausência DECLARA: sem a marca, a empresa reaparece calada e o operador
    # não tem como saber que aquele número é de uma empresa já baixada.
    ativa_by_id = {int(r.codi_emp): bool(r.ativa) for r in cov_df.itertuples()}
    # Homonimas (tipico de matriz e filial) viram itens avulsos com o MESMO
    # nome quando a consolidacao desliga. O rotulo ganha o sufixo de filial
    # pra pessoa distinguir, e porque no navegador o rotulo e identidade (ver
    # `rotulos_unicos` abaixo).
    _homonimas = cov_df["nome_emp"].astype(str).str.lower().value_counts()

    for raiz, grupo in cov_df.groupby("cnpj_raiz"):
        grupo = grupo.sort_values("codi_emp").reset_index(drop=True)
        nome_grupo = str(grupo.iloc[0]["nome_emp"])
        sort_key = nome_grupo.lower()
        ids_grupo = tuple(int(x) for x in grupo["codi_emp"].tolist())

        if len(grupo) == 1 or not consolidar:
            for i, row in grupo.iterrows():
                eid = int(row["codi_emp"])
                cov = cov_by_id[eid]
                badges = _make_badges(cov)
                nome = str(row["nome_emp"])
                cnpj = str(row["cgce_emp"]) if pd.notna(row["cgce_emp"]) else ""
                sufixo = ""
                if _homonimas[nome.lower()] > 1:
                    fnum = cnpj_filial(cnpj)
                    sufixo = f" — Filial {fnum}" if fnum else f" — #{eid}"
                # A marca e o sufixo entram pelo ROTULO, nunca pelo `nome`
                # (item[4]): ele vira `empresa_nome` e daí o filename do PDF.
                label = label_empresa(f"{nome}{sufixo}{marca_inativa(ativa_by_id[eid])}",
                                      badges)
                itens.append((sort_key, i, label, (eid,), nome, cnpj))
        else:
            # GRUPO consolidado
            cov_grupo = {
                k: any(cov_by_id[int(r.codi_emp)][k] for r in grupo.itertuples())
                for k in ("tem_fat", "tem_imp", "tem_func", "tem_folha", "tem_bal")
            }
            badges = _make_badges(cov_grupo)
            label_grp = label_empresa(
                f"📁 {nome_grupo} — GRUPO ({len(grupo)} filiais)", badges)
            itens.append((sort_key, 0, label_grp, ids_grupo,
                          f"{nome_grupo} — GRUPO CONSOLIDADO",
                          f"CNPJ RAIZ: {raiz} ({len(grupo)} filiais)"))
            # Ordena filiais por fnum (sufixo de filial). CNPJ inválido
            # (fnum=None) cai no fim via sentinela "9999". Tiebreaker
            # secundário: codi_emp.
            grupo_ord = grupo.copy()
            grupo_ord["_fnum_sort"] = grupo_ord["cgce_emp"].apply(
                lambda c: cnpj_filial(c) or "9999"
            )
            grupo_ord = grupo_ord.sort_values(["_fnum_sort", "codi_emp"]).reset_index(drop=True)
            for i, row in grupo_ord.iterrows():
                eid = int(row["codi_emp"])
                cov = cov_by_id[eid]
                badges_f = _make_badges(cov)
                nome_filial = str(row["nome_emp"])
                cnpj = str(row["cgce_emp"]) if pd.notna(row["cgce_emp"]) else ""
                fnum = cnpj_filial(cnpj)
                if fnum is None:
                    rotulo = f"#{eid}"
                elif nome_filial.lower() == nome_grupo.lower():
                    rotulo = f"Filial {fnum}"
                else:
                    rotulo = f"Filial {fnum} — {nome_filial}"
                itens.append((sort_key, i + 1,
                              label_empresa(
                                  f"   └ {rotulo}{marca_inativa(ativa_by_id[eid])}",
                                  badges_f),
                              (eid,), nome_filial, cnpj))

    itens.sort(key=lambda x: (x[0], x[1]))
    # Rede de seguranca: nenhum rotulo pode se repetir (mesmo nome e mesma
    # filial em raizes diferentes, por exemplo). Ver `rotulos_unicos`.
    itens = [(*it[:2], rot, *it[3:])
             for it, rot in zip(itens, rotulos_unicos([(it[3], it[2]) for it in itens]))]

    # Sem nenhuma empresa cadastrada: erro fatal de configuração.
    if not itens:
        st.warning("Nenhuma empresa cadastrada.")
        st.stop()

    # ── Empresa: campo de busca SEPARADO + dropdown que so ESCOLHE ───────
    # A digitacao no dropdown e desligada por `filter_mode=None`, o caminho
    # suportado (nada de CSS que torne o <input> do BaseWeb inerte ao mouse:
    # seria divida acoplada a VERSAO). A busca nativa (modo "contains") nao
    # serve: clicar na caixa entra em modo de edicao do VALOR ATUAL, entao pra
    # procurar outra empresa o operador teria de APAGAR o rotulo inteiro antes
    # de digitar. Por isso o campo de busca e separado.
    #
    # A opcao e a TUPLA DE IDS, nunca o indice posicional: com
    # `options=range(len(labels))`, mudar N remontaria o widget em index=0 e
    # limpar a busca nao devolveria a empresa aberta. No navegador, porem, o
    # valor trafega pelo ROTULO, e por isso ele e unico (`rotulos_unicos`).
    _lbl_por_ids   = {it[3]: it[2] for it in itens}
    _dados_por_ids = {it[3]: (it[4], it[5]) for it in itens}
    # A marca vive num mapa PROPRIO, fora do `nome` de `_dados_por_ids`: aquele
    # vira `empresa_nome`, que alimenta o filename do PDF individual. Item de
    # GRUPO nunca e marcado — o grupo nao e inativo; quem e sao as filiais, e a
    # arvore ja marca cada uma.
    _marca_por_ids = {
        it[3]: marca_inativa(ativa_by_id[it[3][0]]) if len(it[3]) == 1 else ""
        for it in itens
    }

    # Re-ancora ANTES de criar o widget: valor persistido que sumiu da carteira
    # NAO pode cair no indice 0 — a empresa trocaria sozinha.
    _sel = st.session_state.get("emp_atual")
    if _sel is not None and _sel not in _lbl_por_ids:
        # Consolidacao DESLIGADA: o item de grupo (tupla de N ids) some e as
        # filiais viram itens avulsos. Cai na PRIMEIRA filial dele, que existe
        # nos dois modos. (O caminho inverso — ligar a consolidacao — nao
        # precisa de nada: o branch de grupo emite o item do grupo E cada
        # filial, entao a tupla singleton continua nas opcoes.)
        _alvo = (_sel[0],) if (_sel[0],) in _lbl_por_ids else None
        if _alvo is not None:
            _sel = _alvo
        else:
            # Sumiu por outro motivo (empresa fora do periodo): deixa o
            # Streamlit usar o default em vez de apontar pra uma opcao morta.
            _sel = None

    # ── Busca por nome/CNPJ, em Python ────────────────────────────────────
    # O predicado casa nome (case-insensitive) OU digitos do CNPJ, e e por ele
    # que a busca por CNPJ vive: ele normaliza os DOIS lados, entao o CNPJ com
    # ou sem pontuacao, inteiro ou so a raiz, acha a mesma empresa. Por isso
    # o rotulo NAO
    # carrega os digitos — quem consulta o CNPJ e o predicado, nao o texto.
    busca = st.text_input(
        "🔍 Buscar empresa",
        placeholder="Nome ou CNPJ",
        key="busca_emp",
        disabled=_etl_running,
    ).strip()

    if busca:
        busca_lower  = busca.lower()
        busca_digits = only_digits(busca)
        def _match_busca(item):
            # item = (sort_key, ordem, label, ids, nome, cnpj)
            if busca_lower in item[4].lower():
                return True
            cnpj_digits = only_digits(item[5])
            return bool(busca_digits) and busca_digits in cnpj_digits
        itens_visiveis = [it for it in itens if _match_busca(it)]
    else:
        itens_visiveis = itens

    # ── O que a busca faz com a SELECAO ──────────────────────────────────
    # Digitar uma busca que CASA ja e a intencao de trocar de empresa. Quatro
    # casos, e o unico perigoso e o terceiro:
    #
    #   1. casa, e a empresa aberta NAO esta entre os resultados
    #        -> AUTO-SELECIONA o primeiro. O painel troca sem clique.
    #   2. casa, e a aberta ESTA entre os resultados -> mantem a aberta.
    #        Quem ja esta olhando a empresa certa nao quer ser jogado pra outra
    #        so porque ela vem antes na ordem alfabetica.
    #   3. NAO casa nada -> avisa e mantem a aberta, caindo na carteira cheia.
    #        Cuidado: options vazias fazem o Streamlit
    #        DESCARTAR o valor e reabrir na primeira da carteira. Nunca pular.
    #   4. busca limpa -> mantem a selecao (a auto-selecionada conta como
    #        selecao: ela foi gravada em `emp_sel`, e nao e um estado a parte).
    #
    # Por causa da regra 1 nao ha reinsercao-no-fim: se a aberta nao casa o
    # filtro, a selecao ja pulou pro resultado, entao nao ha valor fora da
    # lista pro Streamlit descartar. No caso 3 o fallback e a carteira
    # INTEIRA, que ja a contem.
    if not itens_visiveis:
        st.warning("Nenhuma empresa encontrada com o filtro atual.")
        itens_visiveis = itens
    elif busca and _sel is not None and _sel not in {it[3] for it in itens_visiveis}:
        _sel = itens_visiveis[0][3]

    # ── Por que o estado NAO mora na chave do widget ─────────────────────
    # O estado do selectbox trafega do browser como INDICE, nao como valor, e o
    # Streamlit resolve esse indice contra a lista NOVA **antes** do script
    # rodar. Se a lista mudou de tamanho nesse meio tempo, `session_state[key]`
    # ja chega apontando pra outra empresa — nao ha o que "pinar" depois.
    # (Ex.: ligar/desligar a consolidacao muda o tamanho da lista e o indice
    # antigo cai noutro item.)
    #
    # Por isso o estado vive em `emp_atual`, chave NOSSA, e o widget recebe
    # `index=` explicito. Sem fallback silencioso pra indice 0 quando o
    # filtro exclui a selecao: `_sel` chega ao widget
    # SEMPRE deliberado — anterior, re-ancorado, ou auto-selecionado pela regra
    # 1 — e `index=` aponta pra ele. O valor de volta so difere se o operador
    # clicou, que e precisamente o que se quer persistir.
    _ids_vis = [it[3] for it in itens_visiveis]
    if _sel not in _ids_vis:
        # So chega aqui quem sumiu da carteira (troca de periodo), ja que os
        # ramos acima cobrem filtro e consolidacao.
        _sel = _ids_vis[0]

    # ── Cursor: seta, nao I-beam ──────────────────────────────────────────
    # O `filter_mode=None` deixa o <input> READONLY (`el.readOnly` e
    # True so neste selectbox), mas o browser nao sabe disso pelo cursor — ele
    # mostra I-beam em qualquer <input>, e com nome longo, que enche a caixa,
    # a coisa LE como campo de digitacao.
    #
    # E cursor-only, deliberadamente. Nada de `pointer-events: none`: o clique
    # precisa atravessar o input pra abrir o dropdown, e isso seria divida
    # acoplada ao markup do BaseWeb. Aqui nada de comportamento muda — so o
    # desenho do ponteiro. Quem bloqueia a digitacao e o `filter_mode`.
    #
    # O escopo e SO este selectbox, via a classe `st-key-` do container. Os
    # outros 4 do sidebar ("De", "Ate", "O que atualizar?", "Quais empresas
    # incluir?") ficam no `filter_mode` default e ACEITAM digitacao,
    # entao neles o I-beam esta CERTO e o caret tem de aparecer. Um seletor
    # generico de sidebar mentiria em 4 widgets pra consertar 1.
    st.markdown(
        """
        <style>
        .st-key-emp_box input {
            cursor: pointer !important;
            caret-color: transparent !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    emp_ids = st.container(key="emp_box").selectbox(
        "Empresa", _ids_vis,
        index=_ids_vis.index(_sel),
        format_func=lambda ids: _lbl_por_ids[ids],
        # O dropdown so ESCOLHE — quem busca e o campo acima. `None` desliga a
        # digitacao pelo caminho nativo (o `inputReadOnly` do widget). NAO
        # trocar por "contains": clicar passa a editar o valor atual e obriga
        # a apagar o rotulo antes de procurar.
        filter_mode=None,
        disabled=_etl_running,
    )
    st.session_state["emp_atual"] = emp_ids
    empresa_nome, empresa_cnpj = _dados_por_ids[emp_ids]
    empresa_marca = _marca_por_ids[emp_ids]

    st.caption("**Legenda:** 📊 Faturamento | 💰 Impostos | 👥 Funcionários | 💵 Folha | 📒 Balancete | ⚠️ Sem dados")

    st.markdown("---")
    st.markdown("**🔄 Atualizar Dados**")
    modo_etl = st.selectbox(
        "O que atualizar?",
        options=["incremental", "completo", "custom"],
        format_func=lambda x: {
            "incremental": "Só os períodos novos (rápido)",
            "completo":    "Desde 2020 (demorado)",
            "custom":      "Período customizado",
        }[x],
        index=0,
        disabled=_etl_running,
    )
    if modo_etl == "custom":
        custom_ini = st.text_input("Início (YYYY-MM)", value=date.today().strftime("%Y-01"),
                                   disabled=_etl_running)
        custom_fim = st.text_input("Fim (YYYY-MM)", value=date.today().strftime("%Y-%m"),
                                   disabled=_etl_running)
    else:
        custom_ini = custom_fim = None
    if modo_etl == "incremental":
        _wm = _watermark_atual()
        st.caption(f"processado até **{_wm}**" if _wm
                   else "sem watermark ainda — 1º run fará a janela completa")
    etl_btn = st.button("🔄 Executar Atualização",
                        disabled=_etl_running,
                        width="stretch")
    st.page_link("pages/alteracoes_retroativas.py", label="Alterações retroativas")

    st.markdown("---")
    # Seletor de MODELOS — um widget, valido pros 3 botoes de PDF (e a CLI tem
    # a flag equivalente). Nada por botao: a escolha desce como kwarg `modelos`
    # pelo funil unico do pdf_generator. As opcoes vem do REGISTRO (MODELOS),
    # entao um modelo novo aparece aqui sem tocar no app.
    from pdf_generator import MODELOS as _PDF_MODELOS
    _rotulo_modelo = {m["key"]: m["label"] for m in _PDF_MODELOS}
    modelos_sel = st.multiselect(
        "Modelos do PDF",
        options=[m["key"] for m in _PDF_MODELOS],
        default=[m["key"] for m in _PDF_MODELOS],
        format_func=lambda k: _rotulo_modelo[k],
        key="pdf_modelos",
        disabled=_etl_running,
        help="Cada modelo vira uma página do PDF, na ordem em que aparecem "
             "aqui. Vale para os três botões abaixo.",
    )
    # Selecao vazia geraria PDF sem pagina nenhuma: o pdf_generator levanta
    # ValueError, entao o botao para ANTES, com o motivo na tela.
    sem_modelo = not modelos_sel
    if sem_modelo:
        st.caption("⚠️ Selecione ao menos um modelo para gerar PDFs.")

    pdf_btn     = st.button("📄 Gerar PDF",             width="stretch",
                            disabled=pdf_disabled or _etl_running or sem_modelo)

    st.markdown("**Geração em lote**")
    filtro_lote = st.selectbox(
        "Quais empresas incluir?",
        options=["fat_or_bal", "all", "complete"],
        format_func=lambda x: {
            "all":        "Todas as ativas",
            "fat_or_bal": "Só com faturamento OU balancete (recomendado)",
            "complete":   "Só com 4+ marts completos",
        }[x],
        index=0,
        disabled=_etl_running,
    )
    pdf_all_btn = st.button("📦 Gerar TODOS os PDFs",   width="stretch",
                            disabled=pdf_disabled or _etl_running or sem_modelo)

# ══════════════════════════════════════════════════════════════════════════════
# PDF por seleção de empresas (expander no main area, acima do dashboard)
# Máquina de estados espelho do lote TODOS: idle → preview → running → done,
# chaves selpdf_* (prefixo próprio — "sel_de"/"sel_ate" são os selectboxes de
# período). Universo = listar_unidades_lote(filtro="all") — mesmas unidades,
# ids e filenames do lote TODOS; consolidação segue o toggle do sidebar.
# 1 selecionada = PDF direto (filename idêntico ao fluxo individual); 2+ = ZIP.
# ══════════════════════════════════════════════════════════════════════════════

# Segundos por PDF, ~2x de folga deliberada sobre o medido (maquina mais
# fraca, empresas maiores) — estimativa conservadora > otimista. So pra
# mensagem de estimativa do lote (TODOS e por seleção).
_SEC_POR_PDF = 10


@st.cache_data(ttl=300)
def _unidades_selecao(inicio, fim, consolidar):
    from pdf_generator import listar_unidades_lote
    with get_conn() as conn:
        return listar_unidades_lote(inicio, fim, conn, filtro="all",
                                    consolidar=consolidar)


_MD_ESPECIAIS = re.compile(r"([\\`*_{}\[\]<>#|~$])")


def _escapa_markdown(texto):
    """Neutraliza o markdown de texto que NAO e nosso (str(e) de terceiro).

    Mesma classe do `$` que vira LaTeX: as crases da mensagem do kaleido
    virariam trecho de codigo dentro do aviso.
    """
    return _MD_ESPECIAIS.sub(r"\\\1", texto)


def _mostra_aviso_kaleido(container, estado, lote=None):
    """Exibe o aviso de modo dos graficos a partir do ESTADO gravado na geracao.

    Texto em pdf_generator.aviso_kaleido (linguagem de quem usa); o motivo
    tecnico fica num expander FECHADO. O markdown e neutralizado no RENDER nos
    dois: o motivo vem de excecao de terceiro.
    """
    from pdf_generator import aviso_kaleido, detalhe_kaleido
    aviso = aviso_kaleido(estado, lote=lote)
    if not aviso:
        return
    nivel, texto = aviso
    getattr(container, nivel)(_escapa_markdown(texto))
    detalhe = detalhe_kaleido(estado)
    if detalhe:
        with container.expander("Detalhes técnicos", expanded=False):
            st.caption(_escapa_markdown(detalhe))


def _fp_desatualizado(session_state, fp_key, fp_atual):
    """Mesmo idioma do `_pdfi_fp`: um artefato (PDF/ZIP) só
    sobrevive ao rerun se o fingerprint gravado em `session_state[fp_key]`
    ainda descreve a tela atual (`fp_atual`) — empresas/seleção, período e
    modelos. Um mecanismo só, reusado pelos três consumidores (PDF
    individual, seleção, lote): inventar um fingerprint por consumidor é
    como nasce a próxima divergência. O chamador já garante que o artefato
    foi gerado antes de perguntar isso — aqui só se decide se ele ENVELHECEU.
    """
    return session_state.get(fp_key) != fp_atual


if not _etl_running:
    with st.expander("🎯 PDF por seleção de empresas"):
        try:
            unidades_sel = _unidades_selecao(de_str, ate_str, consolidar)
        except Exception as e:
            unidades_sel = []
            st.error(f"Erro ao listar empresas: {e}")

        if unidades_sel:
            por_ids = {tuple(u["ids"]): u for u in unidades_sel}

            # Seleção muda de universo (toggle consolidar, empresa nova pós-
            # ETL) → widget state guarda ÍNDICES nas options; lista diferente
            # remaparia a seleção silenciosamente. Fingerprint reseta.
            _fp = hash(tuple(por_ids.keys()))
            if st.session_state.get("selpdf_fp") != _fp:
                st.session_state["selpdf_fp"] = _fp
                st.session_state.pop("selpdf_ms", None)

            def _label_sel(ids_t):
                u = por_ids[ids_t]
                # `nome_display` carrega o " [INATIVA]" quando for o caso — o
                # seletor tem de dizer o mesmo que o dropdown do sidebar.
                nome = u.get("nome_display", u["nome"])
                lbl = f"{nome} — {u['raiz']}" if u["raiz"] else nome
                if u["n_marts"] == 0:
                    lbl += " ⚠️ sem dados"
                return lbl

            escolhidas = st.multiselect(
                "Empresas (digite pra buscar por nome ou CNPJ)",
                options=list(por_ids.keys()),
                format_func=_label_sel,
                key="selpdf_ms",
                placeholder="Escolha uma ou mais empresas...",
            )

            gerar_sel_btn = st.button(
                f"📄 Gerar PDFs das selecionadas ({len(escolhidas)})",
                disabled=pdf_disabled or not escolhidas or sem_modelo,
                key="selpdf_btn",
            )
            if pdf_disabled:
                st.caption("⚠️ PDF suporta no máximo 12 meses — ajuste o período.")

            if gerar_sel_btn:
                st.session_state["selpdf_stage"] = "preview"
                # Snapshot da seleção E da janela: mudanças de período/seleção
                # entre o preview e o Confirmar não alteram o lote confirmado.
                st.session_state["selpdf_unidades"] = [por_ids[t] for t in escolhidas]
                st.session_state["selpdf_janela"] = (de_str, ate_str)
                for k in ("selpdf_zip", "selpdf_pdf", "selpdf_sucessos", "selpdf_erros",
                          "selpdf_kaleido", "selpdf_abort"):
                    st.session_state.pop(k, None)

            if st.session_state.get("selpdf_stage") == "preview":
                uds = st.session_state.get("selpdf_unidades", [])
                n = len(uds)
                eta_min = max(1, (n * _SEC_POR_PDF) // 60)
                st.info(f"📦 {n} PDF(s) serão gerados — estimativa ~{eta_min} min "
                        "(pode variar conforme complexidade dos dados).")
                c1, c2 = st.columns(2)
                confirmar = c1.button("✅ Confirmar", width="stretch",
                                      key="selpdf_ok")
                cancelar = c2.button("❌ Cancelar", width="stretch",
                                     key="selpdf_no")
                if confirmar:
                    st.session_state["selpdf_stage"] = "running"
                    st.rerun()
                if cancelar:
                    st.session_state["selpdf_stage"] = "idle"
                    st.rerun()

            if st.session_state.get("selpdf_stage") == "running":
                uds = st.session_state.get("selpdf_unidades", [])
                de_j, ate_j = st.session_state.get("selpdf_janela", (de_str, ate_str))
                # Fingerprint do que está SENDO gerado (idioma do `_pdfi_fp`):
                # empresas da própria unidade (não a seleção AO VIVO, que pode
                # ter mudado durante o preview), janela snapshotada e modelos
                # ao vivo (é o que os dois `gerar_*` abaixo realmente recebem).
                _selpdf_out_fp = (tuple(tuple(u["ids"]) for u in uds),
                                  de_j, ate_j, tuple(modelos_sel))
                progress = None
                try:
                    from pdf_generator import (LoteAbortadoKaleido, gerar_pdf_empresa,
                                               gerar_todos_pdfs, kaleido_estado)
                    if len(uds) == 1:
                        u = uds[0]
                        with st.spinner(f"Gerando PDF: {u['nome']}..."):
                            with get_conn() as conn:
                                pdf_bytes = gerar_pdf_empresa(
                                    u["ids"], de_j, ate_j, conn,
                                    u.get("nome_display", u["nome"]), u["cnpj"],
                                    modelos=modelos_sel)
                        st.session_state["selpdf_pdf"] = pdf_bytes
                        st.session_state["selpdf_pdf_name"] = (
                            f"Dashboard_{u['nome']}_{de_j}_{ate_j}.pdf".replace(" ", "_"))
                        st.session_state["selpdf_out_fp"] = _selpdf_out_fp
                        st.session_state["selpdf_sucessos"] = 1
                        st.session_state["selpdf_erros"] = []
                        st.session_state["selpdf_kaleido"] = kaleido_estado()
                    else:
                        progress = st.progress(0, text="Iniciando...")

                        def _cb_sel(current, total, nome):
                            frac = (current / total) if total else 0
                            progress.progress(min(frac, 1.0),
                                              text=f"Gerando {current}/{total}: {nome[:40]}")

                        with get_conn() as conn:
                            zip_bytes, sucessos, erros = gerar_todos_pdfs(
                                de_j, ate_j, conn,
                                unidades=uds, progress_callback=_cb_sel,
                                modelos=modelos_sel)
                        progress.empty()
                        if zip_bytes is not None:
                            st.session_state["selpdf_zip"] = zip_bytes
                            st.session_state["selpdf_zip_name"] = (
                                f"Dashboards_sel_{de_j}_{ate_j}.zip")
                            st.session_state["selpdf_out_fp"] = _selpdf_out_fp
                        st.session_state["selpdf_sucessos"] = sucessos
                        st.session_state["selpdf_erros"] = erros
                    st.session_state["selpdf_stage"] = "done"
                    st.rerun()
                except ImportError as e:
                    # ANTES do LoteAbortadoKaleido: o nome vem do import deste
                    # try, e avaliar a clausula com o import falho daria NameError.
                    st.error(f"Dependência faltando: {e}")
                    st.session_state["selpdf_stage"] = "idle"
                except LoteAbortadoKaleido as e:
                    # Barra limpa ANTES de pintar: congelada no meio le como travamento.
                    if progress is not None:
                        progress.empty()
                    st.session_state["selpdf_zip"] = e.zip_bytes
                    st.session_state["selpdf_zip_name"] = f"Dashboards_sel_{de_j}_{ate_j}.zip"
                    st.session_state["selpdf_out_fp"] = _selpdf_out_fp
                    st.session_state["selpdf_sucessos"] = e.sucessos
                    st.session_state["selpdf_erros"] = e.erros
                    st.session_state["selpdf_abort"] = {
                        "estado": {"modo": e.modo, "motivo": e.motivo},
                        "lote": (e.sucessos, len(e.erros), len(e.pendentes)),
                        "pendentes": e.pendentes,
                    }
                    st.session_state["selpdf_stage"] = "done"
                    st.rerun()
                except Exception as e:
                    st.error(f"Erro ao gerar: {e}")
                    import traceback
                    st.code(traceback.format_exc())
                    st.session_state["selpdf_stage"] = "idle"

            if st.session_state.get("selpdf_stage") == "done":
                # Invalidação: o resultado só sobrevive se a
                # tela atual (empresas escolhidas, período, modelos) ainda é a
                # que o gerou — mesma regra do PDF individual (`_pdfi_fp`).
                _selpdf_out_fp_atual = (tuple(escolhidas), de_str, ate_str,
                                        tuple(modelos_sel))
                _tem_saida = (st.session_state.get("selpdf_pdf") is not None
                             or st.session_state.get("selpdf_zip") is not None)
                if _tem_saida and _fp_desatualizado(
                        st.session_state, "selpdf_out_fp", _selpdf_out_fp_atual):
                    for _k in ("selpdf_pdf", "selpdf_zip", "selpdf_out_fp"):
                        st.session_state.pop(_k, None)
                    st.session_state["selpdf_stage"] = "idle"
                else:
                    pdf_b = st.session_state.get("selpdf_pdf")
                    zip_b = st.session_state.get("selpdf_zip")
                    sucessos = st.session_state.get("selpdf_sucessos", 0)
                    erros = st.session_state.get("selpdf_erros", [])
                    abort = st.session_state.get("selpdf_abort")
                    if abort:
                        # A contagem (gerados / com erro / nao gerados) vem no aviso.
                        _mostra_aviso_kaleido(st, abort["estado"], abort["lote"])
                    if pdf_b:
                        st.download_button(
                            "⬇️ Baixar PDF", data=pdf_b,
                            file_name=st.session_state.get("selpdf_pdf_name", "dashboard.pdf"),
                            mime="application/pdf", key="selpdf_dl")
                        st.success("PDF gerado!")
                        _mostra_aviso_kaleido(st, st.session_state.get("selpdf_kaleido"))
                    elif zip_b:
                        st.download_button(
                            "⬇️ Baixar ZIP", data=zip_b,
                            file_name=st.session_state.get("selpdf_zip_name", "dashboards.zip"),
                            mime="application/zip", key="selpdf_dl")
                        if not abort:
                            st.success(f"✅ Gerados: {sucessos}  |  ⚠️ Falharam: {len(erros)}")
                    elif not abort:
                        st.warning("Nenhum PDF foi gerado.")
                    if abort and abort["pendentes"]:
                        with st.expander(f"Não gerados ({len(abort['pendentes'])})", expanded=False):
                            for nome in abort["pendentes"]:
                                st.write(f"• {nome}")
                    if erros:
                        st.markdown(f"**Falhas ({len(erros)}):**")
                        for e in erros[:50]:
                            st.write(f"• {e}")
                        if len(erros) > 50:
                            st.write(f"... e mais {len(erros) - 50}")
        else:
            st.info("Nenhuma empresa cadastrada.")

# ── Cabeçalho + Dashboard ─────────────────────────────────────────────────────
# Sempre há uma empresa selecionada — quem cobre a carteira vazia é o
# `st.stop()` do "Nenhuma empresa cadastrada", lá em cima.

if _etl_running:
    # Marts em pleno upsert — não renderizar números
    # meio-atualizados; a tela do run assume.
    st.info("⏳ Atualização de dados em andamento — o dashboard volta quando concluir.")
else:
    de_lbl, ate_lbl = p_label(de_str), p_label(ate_str)
    st.markdown(
        f"<h2 style='margin-bottom:2px'>{html.escape(empresa_nome)}"
        f"{html.escape(empresa_marca)}</h2>"
        f"<p style='color:#888;margin-top:0'>CNPJ: {html.escape(empresa_cnpj)}&nbsp;&nbsp;|&nbsp;&nbsp;"
        f"{de_lbl} a {ate_lbl}</p>",
        unsafe_allow_html=True,
    )
    st.markdown("---")
    render_dashboard(
        emp_ids, de_str, ate_str, empresa_nome, empresa_cnpj, show_ui=True,
    )

# ══════════════════════════════════════════════════════════════════════════════
# Atualizar Dados — chama etl/main.py via subprocess e mostra progresso ao vivo
# ══════════════════════════════════════════════════════════════════════════════

def _etl_periods(modo, custom_ini, custom_fim, today=None):
    """Calcula (inicio, fim) YYYY-MM para completo/custom. O modo
    incremental NÃO passa aqui: a janela dele é decidida por
    `_probe_janela` (watermark × fim declarado da origem), nunca por
    adivinhação de MAX em mart — adiantamento (férias, rescisão futura,
    recorrência) contamina qualquer MAX de competência."""
    today = today or date.today().strftime("%Y-%m")
    if modo == "completo":
        return ("2020-01", today)
    return (custom_ini or today, custom_fim or today)


def _probe_janela():
    """Decisão da janela incremental via subprocess `main.py --probe-janela`
    (a leitura da origem roda fora do processo do Streamlit).
    Retorna o dict do probe ou None em falha."""
    etl_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "etl")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        out = subprocess.run(
            [sys.executable, "main.py", "--probe-janela"],
            capture_output=True, text=True, cwd=etl_dir, env=env, timeout=120,
        )
        if out.returncode != 0:
            return None
        return json.loads(out.stdout.strip().splitlines()[-1])
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return None


_OK_LINE_RE = re.compile(r"OK ")
_PIPELINE_OK_TOTAL = 38  # 11 extract (10 tabelas + ctsaldo_inicial) + 11 raw load + 8 transform + 8 mart load

_YYYY_MM_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

if etl_btn and not _etl_running:
    if modo_etl == "custom":
        if not _YYYY_MM_RE.match(custom_ini or ""):
            st.sidebar.error(f"Início inválido: '{custom_ini}'. Use YYYY-MM (ex: 2024-01).")
            st.stop()
        if not _YYYY_MM_RE.match(custom_fim or ""):
            st.sidebar.error(f"Fim inválido: '{custom_fim}'. Use YYYY-MM (ex: 2024-12).")
            st.stop()
        if custom_ini > custom_fim:
            st.sidebar.error("Início deve ser <= Fim.")
            st.stop()
    if modo_etl == "incremental":
        # A decisão de janela é explícita e visível — watermark
        # (processado até) × fim declarado da origem, via probe. Origem sem
        # períodos novos = aviso âmbar e NADA roda (nunca verde silencioso).
        with st.sidebar:
            with st.spinner("Consultando origem (janela incremental)..."):
                probe = _probe_janela()
        if probe is None:
            st.sidebar.error("Falha ao consultar a origem pra decidir a "
                             "janela — origem no ar? Use 'Período "
                             "customizado' se precisar rodar assim mesmo.")
            st.stop()
        if probe["acao"] == "noop":
            st.sidebar.warning(probe["mensagem"])
            st.stop()
        inicio, fim = probe["inicio"], probe["fim"]
        st.sidebar.info(
            f"processado até **{probe['watermark'] or '— (1º run)'}** · "
            f"origem tem até **{probe['fim_declarado']}** · "
            f"janela **{inicio}..{fim}**"
        )
        # Quando a origem tem dado PARCIAL além do declarado
        # (folha/serviços andam à frente da escrituração fiscal), o painel
        # do run explica por que a janela para no declarado.
        if probe.get("alem_declarado"):
            d = probe.get("alem_declarado_data") or ""
            data_fmt = f"{d[8:10]}/{d[5:7]}/{d[0:4]}" if len(d) == 10 else d
            st.session_state["etl_janela_ctx"] = (
                f"neste banco os dados de {p_label(probe['alem_declarado'])} "
                f"ainda estão parciais na origem (escrituração fiscal "
                f"importada até {p_label(probe['fim_declarado'])}) — "
                f"por isso rodando até {p_label(probe['fim_declarado'])}. "
                f"Se quiser incluir os dados que já temos até {data_fmt}, "
                f"rode com 'Período customizado' "
                f"({probe['fim_declarado']}..{probe['alem_declarado']})."
            )
        else:
            st.session_state["etl_janela_ctx"] = None
    else:
        inicio, fim = _etl_periods(modo_etl, custom_ini, custom_fim)
        st.session_state["etl_janela_ctx"] = None
    # Limpa cache ANTES do subprocess para que queries durante a execução não
    # retornem dados stale do mart pré-atualização.
    st.cache_data.clear()
    # Descarta restos de run anterior (proc "finished" velho no slot
    # mascararia o spawn) e autoriza UM único Popen via flag one-shot.
    slot = _etl_slot()
    for k in _ETL_SLOT_KEYS:
        slot.pop(k, None)
    st.session_state["etl_spawn"] = True
    st.session_state["etl_stage"] = "running"
    st.session_state["etl_inicio"] = inicio
    st.session_state["etl_fim"] = fim
    st.session_state["etl_modo"] = modo_etl
    st.session_state["etl_cancel"] = False
    st.rerun()

_IDLE_TIMEOUT_SEC = 30 * 60   # mata pipeline se ficar 30min sem produzir output

def _reader_thread(stream, q):
    """Lê linhas do subprocess em thread daemon — não bloqueia o Streamlit."""
    try:
        for line in iter(stream.readline, ""):
            q.put(line)
    finally:
        q.put(None)  # sentinela de EOF


if st.session_state.get("etl_stage") == "running":
    slot = _etl_slot()
    st.warning(
        "⚠️ Atualização em andamento — pode acompanhar por aqui.  \n"
        "Para abortar, use o botão ❌ Cancelar abaixo."
    )
    st.markdown(
        f"**Período:** `{slot.get('inicio', st.session_state.get('etl_inicio', '?'))}` → "
        f"`{slot.get('fim', st.session_state.get('etl_fim', '?'))}`"
    )
    # fallback de session_state (padrão da linha Período acima): o painel
    # renderiza ANTES do spawn popular o slot — sem o fallback o caption só
    # aparece após o primeiro rerun de polling (atraso de segundos), e a
    # mensagem que explica a decisão deve estar visível desde o primeiro
    # render. O slot segue como fonte pro re-attach cross-tab.
    _jctx = slot.get("janela_ctx") or st.session_state.get("etl_janela_ctx")
    if _jctx:
        st.caption(f"ℹ️ {_jctx}")
    cancel_btn = st.button("❌ Cancelar atualização", key="etl_cancel_btn")
    if cancel_btn:
        st.session_state["etl_cancel"] = True
        slot["cancel_req"] = True   # cross-tab — o cancel chega no owner pelo slot
    progress = st.progress(0, text="Iniciando pipeline...")
    log_box = st.empty()

    import queue as _qmod
    import threading as _th

    estado = classify_etl_proc(slot.get("proc"))

    if estado == "finished":
        # Run de script anterior morreu (rerun) DEPOIS do pipeline acabar:
        # reporta o desfecho registrado no slot e NÃO respawna.
        if slot.get("lines"):
            log_box.code("\n".join(slot["lines"]), language=None)
        rc = slot["proc"].returncode
        if slot.get("cancelled") or st.session_state.get("etl_cancel"):
            st.error("❌ Atualização cancelada pelo usuário.")
        elif slot.get("timed_out"):
            st.error(f"❌ Pipeline travou (>{_IDLE_TIMEOUT_SEC//60} min sem output). Processo encerrado.")
        elif rc == 0:
            st.cache_data.clear()
            st.success("✅ Atualização concluída. Os novos períodos já estão disponíveis no seletor.")
        else:
            st.error(f"❌ Pipeline falhou (exit code {rc}). Veja log acima.")
        _etl_cleanup()
        st.stop()

    if estado == "absent":
        # Spawn só com autorização one-shot do handler — cleanup interrompido
        # no meio NUNCA pode virar respawn fantasma.
        if not st.session_state.pop("etl_spawn", False):
            st.session_state["etl_stage"] = "idle"
            st.rerun()
        etl_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "etl")
        saldo_mode = "always" if st.session_state.get("etl_modo") == "completo" else "auto"
        cmd = [sys.executable, "main.py",
               "--empresa", "all",
               "--inicio", st.session_state["etl_inicio"],
               "--fim", st.session_state["etl_fim"],
               "--saldo-inicial", saldo_mode]
        # Forçar UTF-8 no subprocess (Windows default cp1252 corrompe acentos)
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        with slot["lock"]:
            if classify_etl_proc(slot.get("proc")) == "alive":
                st.rerun()  # outra sessão spawnou no meio — vira re-attach
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, cwd=etl_dir,
                encoding="utf-8", errors="replace",
                env=env,
            )
            q = _qmod.Queue()
            _th.Thread(target=_reader_thread, args=(proc.stdout, q), daemon=True).start()
            now = time.time()
            slot.update({
                "proc": proc, "q": q, "t0": now, "n_ok": 0, "lines": [],
                "last_output": now, "cancelled": False, "timed_out": False,
                "cancel_req": False,   # reset na largada (mata cancel residual de corrida anterior)
                "inicio": st.session_state["etl_inicio"],
                "fim": st.session_state["etl_fim"],
                "janela_ctx": st.session_state.get("etl_janela_ctx"),
            })
    else:
        # alive → RE-ATTACH: sem novo Popen e sem nova thread leitora (a
        # daemon antiga segue drenando o stdout na mesma Queue). Semeia o
        # display do estado persistido pra UI não regredir a "Iniciando...".
        if slot["lines"]:
            log_box.code("\n".join(slot["lines"]), language=None)
        progress.progress(
            min(slot["n_ok"] / _PIPELINE_OK_TOTAL, 0.95),
            text=f"Retomando acompanhamento...  ({slot['n_ok']}/{_PIPELINE_OK_TOTAL})",
        )

    # Re-attach NÃO consome. Quem segura o slot["lock"] (try-lock
    # NÃO-bloqueante) é o consumidor ÚNICO ("owner"); os demais ("spectator")
    # só exibem o snapshot e dão poll, sem tocar a Queue. Garante 1 consumidor.
    if consume_role(slot) == "spectator":
        if slot.get("lines"):
            log_box.code("\n".join(slot["lines"]), language=None)
        progress.progress(
            etl_progress_frac(slot.get("n_ok", 0), _PIPELINE_OK_TOTAL),
            text=f"Acompanhando...  ({slot.get('n_ok', 0)}/{_PIPELINE_OK_TOTAL})",
        )
        time.sleep(1.0)
        st.rerun()

    # owner: consumidor único — segura o lock até o fim (release no finally)
    proc = slot["proc"]
    q = slot["q"]
    status = "Processando..."
    try:
        while True:
            if st.session_state.get("etl_cancel") or slot.get("cancel_req"):
                slot["cancelled"] = True
                proc.terminate()
                break
            try:
                line = q.get(timeout=1.0)
            except _qmod.Empty:
                if proc.poll() is not None:
                    break  # EOF já consumido por um run anterior que morreu
                if time.time() - slot["last_output"] > _IDLE_TIMEOUT_SEC:
                    slot["timed_out"] = True
                    proc.terminate()
                    break
                continue
            if line is None:
                break  # EOF
            # Persistir no slot ANTES de qualquer st.* (yield point): linha
            # consumida da Queue nunca se perde num rerun.
            slot["last_output"] = time.time()
            line = line.rstrip()
            if not line:
                continue
            slot["lines"].append(line)
            if len(slot["lines"]) > 20:
                slot["lines"][:] = slot["lines"][-20:]
            up = line.upper()
            if "EXTRACT " in up or "READ RAW" in up:
                status = "Lendo a origem..."
            elif "LOAD RAW" in up:
                status = "Carregando RAW..."
            elif "TRANSFORM" in up:
                status = "Gerando marts..."
            elif "LOAD MART" in up:
                status = "Carregando MART..."
            if _OK_LINE_RE.search(line):
                slot["n_ok"] += 1
            frac = min(slot["n_ok"] / _PIPELINE_OK_TOTAL, 0.95)
            progress.progress(frac, text=f"{status}  ({slot['n_ok']}/{_PIPELINE_OK_TOTAL})")
            log_box.code("\n".join(slot["lines"]), language=None)

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            # terminate não bastou (ex.: flush de transação) — escalar. Sem
            # isso o proc VIVO escaparia pro except e viraria órfão.
            proc.kill()
            proc.wait()
        elapsed = (time.time() - slot["t0"]) / 60
        if slot.get("cancelled"):
            st.error(f"❌ Atualização cancelada pelo usuário após {elapsed:.1f} min.")
        elif slot.get("timed_out"):
            st.error(f"❌ Pipeline travou (>{_IDLE_TIMEOUT_SEC//60} min sem output). Processo encerrado.")
        elif proc.returncode == 0:
            progress.progress(1.0, text=f"✅ Concluído em {elapsed:.1f} min")
            st.success(f"✅ Atualização concluída em {elapsed:.1f} minutos. "
                       "Os novos períodos já estão disponíveis no seletor.")
            st.cache_data.clear()
            _etl_cleanup()
            time.sleep(1.5)
            st.rerun()
        else:
            st.error(f"❌ Pipeline falhou (exit code {proc.returncode}). Veja log acima.")
        _etl_cleanup()
    except Exception as e:
        # StopException/RerunException NÃO são capturadas aqui (herdam de
        # BaseException) — propagam por fora, o slot fica intacto e o próximo
        # rerun re-attacha. NÃO trocar por except BaseException nem por
        # finally com terminate (cancelaria o pipeline em qualquer interação).
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        st.error(f"❌ Erro ao executar pipeline: {e}")
        _etl_cleanup()
    finally:
        # Solta o gate de consumo em QUALQUER saída — inclusive
        # Rerun/StopException (BaseException) que propagam por aqui. _etl_cleanup
        # não remove "lock" (fora de _ETL_SLOT_KEYS), então o release é válido.
        # Takeover: o próximo espectador re-adquire e continua consumindo.
        slot["lock"].release()

    # Não renderiza o resto do dashboard durante a atualização
    st.stop()


# ══════════════════════════════════════════════════════════════════════════════
# PDF — empresa selecionada
# ══════════════════════════════════════════════════════════════════════════════

# O PDF gerado VIVE em session_state (chaves `pdfi_*`, prefixo proprio como
# `selpdf_*` e `lote_*`): um `st.download_button` DENTRO do `if pdf_btn:`
# existiria so no rerun do clique, e qualquer outra interacao sumiria com o
# PDF ja gerado, obrigando a gerar de novo.
#
# NAO se copia a maquina de estados deles: ela existe pelo passo de preview
# ("N PDFs, ~Y min, Confirmar/Cancelar"), que num PDF so nao faz sentido. Copia-se
# a metade que se aplica — bytes persistidos + fingerprint de invalidacao, o
# idioma do `selpdf_fp`.
#
# REGRA DA INVALIDACAO: o link existe SE E SOMENTE SE descreve a tela atual.
# Uma regra, nao uma lista de casos — ela decide sozinha qualquer eixo que
# apareca depois. Por isso o fingerprint tem os QUATRO insumos do PDF: trocar de
# empresa, de periodo ou de modelos dessincroniza o link da tela do mesmo jeito,
# e um PDF de 2024 aberto com a tela em 2025 e a mesma armadilha que um de outra
# empresa.
_pdfi_fp = (tuple(emp_ids), de_str, ate_str, tuple(modelos_sel))

if pdf_btn:
    with st.spinner("Gerando PDF..."):
        try:
            from pdf_generator import gerar_pdf_empresa, kaleido_estado
            with get_conn() as conn:
                # Cabecalho do PDF leva a marca (espelho do header da tela);
                # o filename abaixo segue no `empresa_nome` LIMPO.
                pdf_bytes = gerar_pdf_empresa(list(emp_ids), de_str, ate_str, conn,
                                              f"{empresa_nome}{empresa_marca}",
                                              empresa_cnpj,
                                              modelos=modelos_sel)
            st.session_state["pdfi_bytes"] = pdf_bytes
            st.session_state["pdfi_name"] = (
                f"Dashboard_{empresa_nome}_{de_str}_{ate_str}.pdf".replace(" ", "_"))
            st.session_state["pdfi_fp"] = _pdfi_fp
            st.session_state["pdfi_kaleido"] = kaleido_estado()
        except ImportError as e:
            st.sidebar.error(f"Dependência faltando: {e}\n\nRode: pip install pdfkit jinja2 kaleido")
        except Exception as e:
            st.sidebar.error(f"Erro ao gerar PDF: {e}")
            import traceback
            st.sidebar.code(traceback.format_exc())

# Fora do `if`: e o que faz o link sobreviver ao rerun. E a comparacao de
# fingerprint e o que o faz sumir quando deixa de descrever a tela.
if st.session_state.get("pdfi_bytes") is not None:
    if _fp_desatualizado(st.session_state, "pdfi_fp", _pdfi_fp):
        for _k in ("pdfi_bytes", "pdfi_name", "pdfi_fp", "pdfi_kaleido"):
            st.session_state.pop(_k, None)
    else:
        st.sidebar.download_button(
            "⬇️ Baixar PDF", data=st.session_state["pdfi_bytes"],
            file_name=st.session_state.get("pdfi_name", "dashboard.pdf"),
            mime="application/pdf", width="stretch", key="pdfi_dl")
        st.sidebar.success("PDF gerado!")
        _mostra_aviso_kaleido(st.sidebar, st.session_state.get("pdfi_kaleido"))

# ══════════════════════════════════════════════════════════════════════════════
# PDF — TODAS as empresas (ZIP)
# Fluxo em duas etapas:
#   1. Click "Gerar TODOS"  -> mostra preview (X PDFs, ~Y min) + Confirmar/Cancelar
#   2. Click "Confirmar"    -> roda geração e expõe download do ZIP
# Estado entre reruns persiste em st.session_state.
# ══════════════════════════════════════════════════════════════════════════════

# _SEC_POR_PDF (estimativa do preview) definido na seção "PDF por seleção",
# compartilhado pelos dois fluxos de lote.

if pdf_all_btn:
    st.session_state["lote_stage"] = "preview"
    st.session_state.pop("lote_zip", None)
    st.session_state.pop("lote_abort", None)

if st.session_state.get("lote_stage") == "preview":
    try:
        from pdf_generator import listar_unidades_lote
        with get_conn() as conn:
            unidades = listar_unidades_lote(de_str, ate_str, conn,
                                            filtro=filtro_lote, consolidar=consolidar)
        n = len(unidades)
        eta_min = max(1, (n * _SEC_POR_PDF) // 60)
        if n == 0:
            st.sidebar.warning("Nenhuma empresa atende ao filtro selecionado.")
            st.session_state["lote_stage"] = "idle"
        else:
            st.sidebar.info(f"📦 {n} PDFs serão gerados — estimativa ~{eta_min} min "
                            "(pode variar conforme complexidade dos dados).")
            c1, c2 = st.sidebar.columns(2)
            with c1:
                confirmar = st.button("✅ Confirmar", width="stretch", key="lote_ok")
            with c2:
                cancelar = st.button("❌ Cancelar", width="stretch", key="lote_no")
            if confirmar:
                st.session_state["lote_stage"] = "running"
                st.session_state["lote_filtro"] = filtro_lote
                st.session_state["lote_consolidar"] = consolidar
                st.rerun()
            if cancelar:
                st.session_state["lote_stage"] = "idle"
                st.rerun()
    except Exception as e:
        st.sidebar.error(f"Erro: {e}")
        st.session_state["lote_stage"] = "idle"

if st.session_state.get("lote_stage") == "running":
    progress = None
    try:
        from pdf_generator import LoteAbortadoKaleido, gerar_todos_pdfs
        progress = st.sidebar.progress(0, text="Iniciando...")
        status   = st.sidebar.empty()
        _lote_filtro_usado = st.session_state.get("lote_filtro", filtro_lote)
        _lote_consolidar_usado = st.session_state.get("lote_consolidar", consolidar)
        # Fingerprint do que está SENDO gerado (idioma do `_pdfi_fp`/`_selpdf_out_fp`).
        _lote_out_fp = (de_str, ate_str, _lote_filtro_usado,
                        _lote_consolidar_usado, tuple(modelos_sel))

        def _cb(current, total, nome):
            frac = (current / total) if total else 0
            progress.progress(min(frac, 1.0),
                              text=f"Gerando {current}/{total}: {nome[:40]}")

        with get_conn() as conn:
            zip_bytes, sucessos, erros = gerar_todos_pdfs(
                de_str, ate_str, conn,
                filtro=_lote_filtro_usado,
                consolidar=_lote_consolidar_usado,
                progress_callback=_cb,
                modelos=modelos_sel,
            )
        progress.empty()
        if zip_bytes is None:
            st.sidebar.warning("Nenhum PDF foi gerado.")
        else:
            st.session_state["lote_zip"] = zip_bytes
            st.session_state["lote_zip_name"] = f"Dashboards_{de_str}_{ate_str}.zip"
            st.session_state["lote_out_fp"] = _lote_out_fp
            st.session_state["lote_sucessos"] = sucessos
            st.session_state["lote_erros"] = erros
        st.session_state["lote_stage"] = "done"
        st.rerun()
    except ImportError as e:
        st.sidebar.error(f"Dependência faltando: {e}")
        st.session_state["lote_stage"] = "idle"
    except LoteAbortadoKaleido as e:
        # Barra limpa ANTES de pintar: congelada no meio le como travamento.
        if progress is not None:
            progress.empty()
        st.session_state["lote_zip"] = e.zip_bytes
        st.session_state["lote_zip_name"] = f"Dashboards_{de_str}_{ate_str}.zip"
        st.session_state["lote_out_fp"] = _lote_out_fp
        st.session_state["lote_sucessos"] = e.sucessos
        st.session_state["lote_erros"] = e.erros
        st.session_state["lote_abort"] = {
            "estado": {"modo": e.modo, "motivo": e.motivo},
            "lote": (e.sucessos, len(e.erros), len(e.pendentes)),
            "pendentes": e.pendentes,
        }
        st.session_state["lote_stage"] = "done"
        st.rerun()
    except Exception as e:
        st.sidebar.error(f"Erro: {e}")
        import traceback
        st.sidebar.code(traceback.format_exc())
        st.session_state["lote_stage"] = "idle"

if st.session_state.get("lote_stage") == "done":
    # Invalidação: o ZIP só sobrevive se a tela atual
    # (período, filtro, consolidação, modelos) ainda é a que o gerou — mesma
    # regra do PDF individual (`_pdfi_fp`).
    _lote_out_fp_atual = (de_str, ate_str, filtro_lote, consolidar, tuple(modelos_sel))
    if (st.session_state.get("lote_zip") is not None
            and _fp_desatualizado(st.session_state, "lote_out_fp", _lote_out_fp_atual)):
        for _k in ("lote_zip", "lote_zip_name", "lote_out_fp"):
            st.session_state.pop(_k, None)
        st.session_state["lote_stage"] = "idle"
    else:
        sucessos = st.session_state.get("lote_sucessos", 0)
        erros = st.session_state.get("lote_erros", [])
        zip_bytes = st.session_state.get("lote_zip")
        abort = st.session_state.get("lote_abort")
        if abort:
            # A contagem (gerados / com erro / nao gerados) vem no aviso.
            _mostra_aviso_kaleido(st.sidebar, abort["estado"], abort["lote"])
        if zip_bytes:
            st.sidebar.download_button(
                "⬇️ Baixar ZIP", data=zip_bytes,
                file_name=st.session_state.get("lote_zip_name", "dashboards.zip"),
                mime="application/zip", width="stretch",
            )
        if not abort:
            st.sidebar.success(f"✅ Gerados: {sucessos}  |  ⚠️ Falharam: {len(erros)}")
        elif abort["pendentes"]:
            with st.sidebar.expander(f"Não gerados ({len(abort['pendentes'])})", expanded=False):
                for nome in abort["pendentes"]:
                    st.write(f"• {nome}")
        if erros:
            with st.sidebar.expander(f"Falhas ({len(erros)})"):
                for e in erros[:50]:
                    st.write(f"• {e}")
                if len(erros) > 50:
                    st.write(f"... e mais {len(erros)-50}")
