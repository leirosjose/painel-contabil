# ---------------------------------------------------------------------------
# metrics.py — orquestração PURA dos KPIs financeiros, compartilhada entre
# app.py (Streamlit) e pdf_generator.py (PDF). Centraliza o cálculo que seria
# duplicado nos dois consumers.
#
#   - last_valid -> NaN se série vazia/all-NaN. fmt_* já mapeiam NaN/inf -> "—"
#     (no-data honesto, não "0,00" fabricado).
#   - flags_inconsistencia MANTÉM o guard pd.isna — inerte enquanto
#     bal_grupo_serie preenche ac/pc/pl/at com 0.0, load-bearing se a fonte
#     passar a emitir NaN p/ conta ausente.
#
# Módulo PURO: importa só pandas + etl.utils + etl.mappings. SEM DB, SEM
# psycopg2/jaydebeapi. Os consumers montam os DataFrames (raw/mart) e o
# bal_serie_df já-seedado (carry-in) e passam pra cá.
#
# As diferenças LEGÍTIMAS ficam FORA daqui (decisão de produto, por consumer):
# quebra NFe/NFS-e, semântica de delta, ent_pm "Compras", sparklines −5m,
# gauges/ranges, devoluções.
# ---------------------------------------------------------------------------
import pandas as pd

from etl.mappings import (
    CODI_IMP_RETENCAO_TERCEIROS,
    FAMILIAS_FALTA,
    RE_AFASTAMENTO_ESPELHO,
    RE_ESTORNO_13O,
    RE_FALTA_ESPELHO,
    CLAS_PASSIVO_IRPJ_CSLL,
    CODI_IMP_IRPJ_CSLL,
    CODI_IMP_ISS,
    CODI_IMP_SCP,
    FAMILIAS_LINHA_FOLHA,
    GRUPOS_BAL,
    IMP_NOMES,
    RE_PASSIVO_CSLL,
    RE_PASSIVO_FORA,
    RE_PASSIVO_IRPJ,
    RE_REFLEXO_DSR,
    RE_RESCISAO,
    TIPOS_PROCESS_ANTECIPACAO,
)
from etl.utils import (
    bal_grupo_serie, fmt_brl, normaliza_rubrica, p_label, safe_div,
    weighted_pct)

# Rótulo ÚNICO da lucratividade suprimida, em app E pdf — a causa comunicada é o
# DADO (sem despesas escrituradas o cálculo é impossível), nunca o regime.
# A frase é deliberadamente o gancho do contador pra cobrar os documentos
# de despesa do cliente.
LUC_SEM_REGISTROS = "Sem registros de despesas no período"

# Guard de cobertura de receita: receita contabil abaixo desta fracao do
# faturamento NF do periodo = a escrituracao de receita e INCOMPLETA (ex.:
# honorarios lancados num modulo fora do razao) e a margem sairia absurda.
# Nesse caso pct_luc vira NaN e os consumers caem no fallback GENERICO
# ("Sem dados para calcular lucratividade"), NUNCA no rotulo de despesas.
# Heuristica REVISAVEL: empresas com escrituracao completa ficam em ~90%+.
LUC_COBERTURA_RECEITA_MIN = 0.10


def folha_custo(folha_df):
    """Folha de CUSTO = tudo MENOS as antecipacoes (tp 41/51/92).

    **INCLUI o 13o integral (tp52)**: nao existem dois custos totais. O mes
    que pagou 13o sobe; o que nao pagou, nao. Base UNICA do card "% Folha +
    Encargos" do hero E do CUSTO TOTAL da tabela de folha por rubrica (fonte
    unica: sem isso a tela mostraria dois numeros com o mesmo nome).

    O que sai e SO duplicata do mesmo dinheiro (TIPOS_PROCESS_ANTECIPACAO):
    tp41 adiantamento (recuperado 1:1 por desconto dentro do tp11), tp51 1a
    parcela do 13o (estornada dentro do tp52) e tp92 RPA (detalhe do recibo,
    consolidado no tp11). Somar qualquer um deles conta 2x.

    Puro, nao muta o input. DataFrame vazio ou SEM a coluna tipo_process
    (consumers/testes que passam folha ja agregada) retorna INALTERADO —
    guard de compatibilidade, nao filtro silencioso.
    """
    if folha_df.empty or "tipo_process" not in folha_df.columns:
        return folha_df
    return folha_df[~folha_df["tipo_process"].isin(TIPOS_PROCESS_ANTECIPACAO)]


def rubricas_de_custo(rub_df):
    """As rubricas que compoem o CUSTO de folha — CRITERIO UNICO.

    Fonte unica do card do hero E da tabela de folha por rubrica: os dois leem
    o grao rubrica, entao nao ha dois universos com o mesmo nome na mesma
    tela. Ler o card de `mart.folha_mensal` (grao tipo_process) divergiria na
    familia "Simples domestico", encargo de empregador domestico que a FOMOVTO
    lanca como provento e a FOBASES nao poe em `proventos`. Assim a igualdade
    e ESTRUTURAL.

    Tira o que outro processamento ja contem (`TIPOS_PROCESS_ANTECIPACAO`). Nao muta
    o input; frame vazio ou sem `tipo_process` volta inalterado.
    """
    if rub_df is None or rub_df.empty or "tipo_process" not in rub_df.columns:
        return rub_df
    return rub_df[~rub_df["tipo_process"].isin(TIPOS_PROCESS_ANTECIPACAO)]


# Cada abatimento pousa na linha do PROVENTO QUE ELE ESPELHA. Nao e escolha
# estetica: afastamento e falta espelham verba da familia "Salarios", que e a
# linha "Folha"; o salario familia espelha o proprio provento de salario
# familia, que a origem classifica em "Outros Proventos" e cai em "Outros".
# Assim nenhuma linha fica negativa e o invariante "soma da coluna == CUSTO
# TOTAL" sobrevive por construcao, em vez de virar excecao declarada.


def abatimentos_de_custo(rub_df, periods):
    """Descontos-ESPELHO por competencia: o que o provento diz que foi pago e
    a empresa nao desembolsou. Devolve dict natureza -> Series (+ "total").

    Fonte unica do card e da tabela — as duas subtraem a MESMA serie, entao o
    `% Folha + Encargos` e o `% sobre Faturamento` continuam sendo o mesmo
    numero depois do ajuste, que e o contrato do bloco.

    Casa sobre o nome NORMALIZADO (acento nao pode decidir se uma rubrica
    entra) e so sobre `valor_d`.
    """
    idx = list(periods)
    zero = pd.Series(0.0, index=idx)
    out = {n: zero.copy() for n in NATUREZAS_ABATIMENTO}
    if rub_df is None or rub_df.empty or "valor_d" not in rub_df.columns:
        out["total"] = zero.copy()
        return out

    d = rub_df.copy()
    d["valor_d"] = pd.to_numeric(d["valor_d"], errors="coerce").fillna(0.0)
    nome = normaliza_rubrica(d["nome"].fillna(""))
    fam = d["desc_classificacao"] if "desc_classificacao" in d.columns else pd.Series("", index=d.index)

    tem_d = d["valor_d"] > 0
    # O estorno do 13o vem PRIMEIRO na cadeia: a rubrica "ADIANTAMENTO 13
    # SALARIO RESCISAO" tambem casaria a regra de rescisao se ela viesse antes,
    # e ele e estorno, nao verba de acerto.
    m_est = tem_d & nome.str.contains(RE_ESTORNO_13O, na=False)
    # e o destino se parte pelo PROCESSO em que o estorno caiu
    tp = (pd.to_numeric(d["tipo_process"], errors="coerce")
          if "tipo_process" in d.columns else pd.Series(0, index=d.index))
    m_est_fech = m_est & tp.eq(52)
    m_est_resc = m_est & ~tp.eq(52)
    m_afast = tem_d & ~m_est & nome.str.contains(RE_AFASTAMENTO_ESPELHO, na=False)
    # familia UNIAO nome: a familia sozinha pega so metade (o "FALTAS DSR"
    # mora na familia generica "Descontos")
    m_falta = tem_d & ~m_est & ~m_afast & (fam.isin(FAMILIAS_FALTA)
                                           | nome.str.contains(RE_FALTA_ESPELHO, na=False))

    for natureza, mask in (("afastamento", m_afast), ("falta", m_falta),
                           ("estorno_13o", m_est_fech),
                           ("estorno_13o_resc", m_est_resc)):
        if mask.any():
            out[natureza] = (d.loc[mask].groupby("competencia")["valor_d"].sum()
                             .reindex(idx).fillna(0.0))
    out["total"] = sum(out[n] for n in NATUREZAS_ABATIMENTO)
    return out


def sf_compensado_por_mes(encargos_df, periods):
    """Salario familia COMPENSADO na guia, por competencia.

    A guia vem BRUTA — o `inss_empresa` e o patronal ANTES da compensacao
    (Lei 8.213 art.68). O provento de salario familia FICA no custo, porque a
    empresa desembolsou mesmo; o que abate e o que ela RECEBEU de volta na
    guia.

    O abatimento pousa no CUSTO TOTAL, nao dentro do `inss_empresa`: na
    maioria das empresas com salario familia o patronal e 0,00 (Simples) e a
    compensacao sai dos segurados, entao o patronal menos o SF ficaria
    NEGATIVO. Assim a linha de Encargos continua fiel a guia e o total
    continua sendo desembolso.

    Mart sem a coluna (pre-carga do contrato novo) devolve zero — aqui zero e
    honesto: sem o dado, nao ha abatimento a fazer, e o numero exibido volta
    a ser o de antes em vez de sumir.
    """
    idx = list(periods)
    if (encargos_df is None or encargos_df.empty
            or "sf_compensado" not in encargos_df.columns):
        return pd.Series(0.0, index=idx)
    return (encargos_df.groupby("competencia")["sf_compensado"].sum()
            .reindex(idx).fillna(0.0))


def custo_folha_pm(folha_df, rub_df, periods, encargos_df=None):
    """A serie UNICA de custo de folha por competencia (o `folha_pm` do card).

    Le o mart de RUBRICAS quando ele existe — a MESMA base da tabela, entao
    `folha_pm == custo_rubricas` passa a valer por construcao, e nao mais por
    coincidencia. Sem rubricas degrada pro `mart.folha_mensal` de antes
    (retrocompat + empresa/janela sem carga do mart novo).
    """
    r = rubricas_de_custo(rub_df)
    if r is not None and not r.empty and "valor_p" in r.columns:
        v = pd.to_numeric(r["valor_p"], errors="coerce").fillna(0.0)
        bruto = v.groupby(r["competencia"]).sum().reindex(periods, fill_value=0.0)
        # os TRES abatimentos, aqui e na tabela — se um deles ficar so de um
        # lado, o card e o "% sobre Faturamento" voltam a ser dois numeros
        return (bruto - abatimentos_de_custo(r, periods)["total"]
                - sf_compensado_por_mes(encargos_df, periods))
    f = folha_custo(folha_df)
    if f is not None and not f.empty and "valo_bruto" in f.columns:
        return f.groupby("competencia")["valo_bruto"].sum().reindex(periods, fill_value=0)
    return pd.Series(0.0, index=periods)


# ── Dash de folha por RUBRICA ──────────────────────────────────────────────

# Ordem FIXA decidida pelos stakeholders — nao e alfabetica nem por valor. O
# 13o fecha as linhas de rubrica (e o item que so aparece em nov/dez) e os
# encargos vem depois dele, ja fora da folha propriamente dita.
# Rotulo QUALIFICADO da linha do 13o. Ele responde no proprio
# nome a pergunta "cade a 1a parcela?": o que a linha mostra e o 13o INTEGRAL,
# lancado no mes do FECHAMENTO (tp52), nao uma das duas parcelas. A 1a parcela
# (tp51) esta DENTRO desse valor — o tp52 traz o integral e estorna o
# adiantamento por dentro, entao somar as duas contaria a 1a duas vezes.
# CUIDADO: e diferente da FAMILIA "13º Salário" que vem do dicionario da
# origem (desc_classificacao) — a familia aparece em 4 tipos de processo e so
# o tp52 traz o integral.
LINHA_13O = "13º Salário"

LINHAS_FOLHA_RUBRICA: tuple[str, ...] = (
    "Folha", "Férias", "Rescisão", "Hora Extra",
    "Comissão", "DSR", "Outros", LINHA_13O)

# Linhas ESTRUTURAIS: aparecem sempre, mesmo zeradas — sao o esqueleto da
# leitura ("quanto custou a folha" tem que ter resposta em todo mes). As
# demais sao OPCIONAIS e so entram se houver movimento na janela: empresa que nunca pagou comissao nao carrega uma linha de zeros.
LINHAS_FOLHA_SEMPRE: frozenset[str] = frozenset({"Folha"})

# O destino do estorno do 13o depende do PROCESSO em que ele cai, e nao so da
# natureza: no fechamento (tp52) ele espelha a linha do 13o; no acerto
# (tp11/tp42) ele espelha o 13o proporcional, que a precedencia de nome manda
# pra "Rescisão". Apontar tudo pro 13o estouraria a linha nos meses em que ela
# e 0,00; com o destino amarrado a origem, nenhuma das duas fica negativa.
LINHA_ABATIMENTO = {"afastamento": "Folha", "falta": "Folha",
                    "salario_familia": "Outros",
                    "estorno_13o": LINHA_13O, "estorno_13o_resc": "Rescisão"}

NATUREZAS_ABATIMENTO = ("afastamento", "falta", "estorno_13o", "estorno_13o_resc")

# Rotulo dos encargos DECLARA a fonte: vem da GUIA recolhida
# (mart.encargos_mensais), nao das rubricas da folha. Mesma regra dos
# "rotulos que NOMEIAM o universo" — generico ao lado de numero de outra
# natureza e defeito, nao economia de texto.
LINHA_ENCARGOS = "Encargos (INSS + FGTS, guia)"

# UM NOME SO PRO MESMO UNIVERSO: o card, o titulo do chart, o titulo do bloco
# e a linha da tabela dizem TODOS "Folha + Encargos", que e o que o numero e:
# a folha de custo somada aos encargos da guia.
LINHA_FOLHA_ENCARGOS = "Folha + Encargos"
LINHA_PCT_FAT = "% sobre Faturamento"
# Linha da tabela de faturamento do app para receita que NAO vem de nota fiscal.
# E BALDE, nao sinonimo de uma fonte: hoje so tem as parcelas de venda de
# imovel; se amanha entrar outra, a tabela nao muda -- muda a soma do
# componente. O rastro por fonte fica na COLUNA do mart (`valo_imobiliario`),
# que nomeia a origem; este e rotulo de EXIBICAO.
# NAO confundir com a conta contabil "Outras Receitas".
LINHA_OUTRAS_RECEITAS = "Outras receitas"

# As rubricas que o detalhamento de folha DESTACA. Ordem FIXA — nao e
# alfabetica nem por valor.
#
# O total fica no topo (a linha `LINHA_FOLHA_ENCARGOS` + o percentual) e
# embaixo fica esta lista curta, que e **CONTIDA** no total, nunca aditiva.
# Nao existe — e nao pode existir — checagem de "soma dos destaques ==
# total": quem protege o leitor de somar duas vezes e o
# `SUBTITULO_DESTAQUES_FOLHA`, que declara a contencao ao lado do numero.
#
# CADA NOME AQUI E UMA LINHA QUE O PIPELINE PRODUZ — `_linha_da_rubrica`, que
# decide pela CLASSIFICACAO do dicionario (`desc_classificacao` via
# FAMILIAS_LINHA_FOLHA) e, so onde a origem erra a familia, pelos regexes de
# NOME ja normalizados (RE_RESCISAO / RE_REFLEXO_DSR). Nunca por `i_eventos`
# cru: o codigo de rubrica e POR EMPRESA (o mesmo codigo pode ser hora extra
# numa empresa e outra verba em outra). E nunca por um criterio proprio
# daqui: um segundo classificador seria uma TAXONOMIA PARALELA — quando os
# dois discordassem, cada tela mostraria um. Este modulo so SELECIONA o que o
# pipeline ja decidiu, e o teste trava
# RUBRICAS_DESTAQUE_FOLHA <= LINHAS_FOLHA_RUBRICA.
RUBRICAS_DESTAQUE_FOLHA: tuple[str, ...] = (
    "Férias", "Rescisão", "Hora Extra", "Comissão", "DSR")

TITULO_DESTAQUES_FOLHA = "Principais custos da folha"
SUBTITULO_DESTAQUES_FOLHA = "valores já incluídos em Folha + Encargos"

# Limiar de movimento de uma linha opcional: o MESMO do fmt_brl.
# O que arredonda pra R$ 0,00 na exibicao nao e movimento — criar linha por
# causa de R$ 0,004 mostraria "R$ 0,00" em todos os meses.
_EPS_MOVIMENTO = 0.005

# Sem codigo de sistema no texto exibido: "tp52" e jargao interno e o leitor
# do relatorio nao liga pro codigo — mesma regua do rotulo do tipo de processo
# (rotulo_tipo_process). O QUE a linha e continua dito, em portugues.
NOTA_LINHA_13O = ("cada parcela no mês do pagamento; reflexos de 13º pagos "
                  "dentro do mensal constam em Outros")

# Quantas rubricas o detalhe de "Outros" nomeia antes de agregar o resto em
# "Demais rubricas". Constante porque os DOIS consumers exibem a mesma lista —
# corte diferente de cada lado seria dois "Outros" com o mesmo nome. No PDF o
# bloco vem DEPOIS da tabela cheia e mais linhas nao cabem na altura restante.
OUTROS_TOP_N = 6

# Legenda do travessao na linha de percentual. So aparece quando ha "—" na
# linha (nota orfa confunde — mesma licao do marcador (*) de is_partial).
# Cobre as DUAS causas sem mentir: denominador ZERO e denominador positivo
# porem IMATERIAL (abaixo do piso) produzem o mesmo travessao.
NOTA_TRAVESSAO_PCT = ('"—" = mês sem faturamento que sirva de base para o '
                      "percentual.")


def legenda_travessao(valores):
    """A legenda do "—" quando ela e necessaria, "" quando nao e.

    Recebe os valores CRUS da linha de percentual (nao os formatados): e o
    NaN que vira "—" no fmt_pct. Fonte unica dos dois consumers.
    """
    if valores is None:
        return ""
    serie = pd.Series(list(valores), dtype="float64")
    if serie.empty:
        return ""
    tem_travessao = bool(serie.replace([float("inf"), float("-inf")],
                                       float("nan")).isna().any())
    return NOTA_TRAVESSAO_PCT if tem_travessao else ""

# Nota padrao da linha de encargos: DECLARA a fonte (guia recolhida), porque
# e natureza diferente das rubricas. Constante pro app e o PDF de
# Detalhamento de Folha usarem a MESMA frase.
NOTA_ENCARGOS_GUIA = "Encargos vêm das guias de INSS e FGTS recolhidas."

# Complemento quando a empresa nao recolhe INSS patronal em guia: a linha de
# encargos continua UMA so (o CUSTO TOTAL nao muda) e a ausencia vira NOTA —
# assim ninguem soma errado e o leitor entende por
# que a linha so traz o FGTS.
NOTA_ENCARGOS_SIMPLES = ("No Simples o INSS patronal entra no DAS — a linha de "
                         "encargos traz apenas o FGTS.")

# O card de % Impostos nao AFIRMA "R$ 0,00 de impostos" quando o numerador
# simplesmente NAO EXISTE: sem informacao, aparece "sem informacao" — POR
# EXTENSO. O travessao seco fica so no slot do PERCENTUAL, que e numerico;
# no contexto o card nao tem legenda que explique simbolo (as tabelas tem a
# NOTA_TRAVESSAO_PCT, o card nao). Fonte unica pros 2 consumers.
FRASE_IMP_SEM_INFO = "sem informação de impostos no período"
# A OUTRA ausencia. Com o numerador na guia, "nao ha dado" e "so tem imposto
# de TERCEIRO" sao casos distintos, e a segunda diz POR QUE nao ha numero. O
# texto e o mesmo do vazio da tabela do PDF (`imp_empty_msg`), compartilhado
# com o CARD. Nenhum dos dois exibe R$ 0,00, que seria FALSO.
FRASE_IMP_SO_RETENCAO = (
    "sem impostos próprios no período (apenas retenções de terceiros)")
FONTE_SEM_DADO = "sem_dado"


def encargos_sem_inss_patronal(regime_df, encargos_df, periods):
    """A nota do Simples se aplica? Regime `simples` E INSS patronal ZERO.

    **O `AND` e load-bearing — regime SOZINHO MENTIRIA.** Ha empresas do
    Simples com INSS patronal > 0: construcao civil e sociedades de advogados
    sao o perfil canonico do **Anexo IV**, que recolhe INSS patronal FORA do
    DAS. Afirmar "esta no DAS" pra elas seria falso.
    O segundo termo olha o DADO: a nota afirma exatamente o que foi
    verificado, entao nao ha como mentir mesmo se o regime estiver errado.

    Regime costuma ser estavel na janela, mas o criterio nao exige
    unanimidade: basta HAVER mes de Simples e NENHUM INSS
    patronal. Sem `regime_df` (mart ausente) devolve False — sem saber o
    regime, a nota nao pode ser afirmada.
    """
    if regime_df is None or regime_df.empty or "regime" not in regime_df.columns:
        return False
    na_janela = regime_df[regime_df["competencia"].isin(list(periods))]
    if not (na_janela["regime"] == "simples").any():
        return False
    if encargos_df is None or encargos_df.empty:
        return True
    enc = encargos_df[encargos_df["competencia"].isin(list(periods))]
    if enc.empty or "inss_empresa" not in enc.columns:
        return True
    return float(enc["inss_empresa"].fillna(0).abs().sum()) < 0.005


def impostos_sem_irpj_proprio(regime_df, periods, irpj_total):
    """A regua "inclui IRPJ/CSLL sobre o lucro" MENTE nesta empresa?

    **Empresa do Simples nao tem IRPJ/CSLL proprio** — ele esta dentro do
    DAS. A frase declara o universo do numerador (que soma a provisao da
    `dre_mensal`) e e verdadeira em Real/Presumido; no Simples ela afirma um
    tributo que a empresa nao apura. Fonte unica do criterio pros 2 consumers
    — o TEXTO fica em cada um (o tooltip do app tem espaco pra frase longa, o
    contexto do card do PDF nao), a DECISAO e uma so.

    Vale so pra este trecho: a frase "Fonte por regime: ... Simples = DAS
    registrado" do tooltip continua, e e ela que explica de onde vem o numero.

    A regua e do UNIVERSO, nao do VALOR: Real/Presumido sem provisao na janela
    (prejuizo fiscal) MANTEM a frase, porque o regime apura IRPJ/CSLL e o numerador o inclui por
    definicao. Quem perde a frase e quem nao tem o tributo, nao quem teve zero
    dele.

    O segundo termo (`irpj_total`) e COERENCIA DO CARD, nao gate de regime: o
    card exibe "IRPJ/CSLL do periodo: R$ X" sempre que |irpj_total| >= 0,005, e
    esconder a regua enquanto mostra o valor deixaria um numero orfao na mesma
    tela. Empresa com mes de Simples normalmente nao tem provisao de
    IRPJ/CSLL, entao o AND quase nunca muda numero; ele existe pro dia em que
    mudar. (Contraste deliberado com o irmao `encargos_sem_inss_patronal`,
    onde o AND MORDE: empresas do Anexo IV recolhem INSS patronal.)

    `any` de Simples na janela basta — regime costuma ser estavel e a transicao mid-year fica coberta pelo AND: se a
    perna Real/Presumido gerou provisao, a frase volta. Sem `regime_df` (mart
    ausente, 1o deploy) devolve False: sem saber o regime a frase nao pode ser
    RETIRADA, e o default preserva o comportamento legado.
    """
    if regime_df is None or getattr(regime_df, "empty", True):
        return False
    if "regime" not in regime_df.columns:
        return False
    na_janela = regime_df[regime_df["competencia"].isin(list(periods))]
    if not (na_janela["regime"] == "simples").any():
        return False
    # NaN nao e provisao: mes sem DRE nao pode segurar a frase.
    if irpj_total is not None and abs(float(irpj_total)) >= 0.005:
        return False
    return True


def _linha_da_rubrica(tipo_process, familia, nome):
    """A qual das 8 linhas esta rubrica pertence. PRECEDENCIA importa.

    Ordem: **tp52 -> nome-RESCISAO -> nome-DSR -> familias -> Outros**.
    Primeira regra que casa vence — e por isso cada rubrica cai em EXATAMENTE
    um balde e a soma das linhas fecha por construcao.

    **tp52 primeiro** e blindagem da ponte: o 13o integral e o degrau que
    reconcilia com a tela, entao nada pode escapar dele. Rubricas de RESCISAO
    e de DSR normalmente aparecem so em tp11 e tp42, logo a regra raramente
    muda numero — existe pro dia em que mudar.

    **RESCISAO antes das familias** porque a origem erra a familia nessas
    verbas: "13 SALARIO INTEGRAL RESCISAO", "VANTAGENS 13o RESCISAO" e
    "MEDIA HORAS 13o RESCISAO" estao todas em "13º Salário", e o "ESTOURO
    RESCISAO" em "Fechamento". Sem o criterio de nome, essas verbas ficariam
    fora da linha Rescisao. Limite declarado do criterio em RE_RESCISAO
    (mappings).

    **DSR antes das familias** porque o `REFLEXO COMISSOES DSR` vem
    classificado como "Comissão" pela origem — sem a precedencia o mesmo
    dinheiro contaria na linha Comissao E na DSR.

    **13o por tipo_process e nao por familia:** a familia "13º Salário"
    aparece em varios tps (tp11, tp42, tp51, tp52) e so o tp52 traz o
    INTEGRAL. O corte por familia duplicaria.
    """
    txt = str(nome or "")
    if int(tipo_process) in (51, 52):
        return LINHA_13O
    if RE_RESCISAO.search(txt):
        return "Rescisão"
    if RE_REFLEXO_DSR.search(txt):
        return "DSR"
    for linha in ("Rescisão", "Hora Extra", "Comissão", "Férias", "Folha"):
        if familia in FAMILIAS_LINHA_FOLHA[linha]:
            return linha
    return "Outros"


def encargos_por_mes(encargos_df, periods, na=float("nan")):
    """INSS patronal + FGTS (normal e rescisorio) da GUIA, por competencia.

    Fonte unica do encargo mensal: a serie do card do hero
    (`series_mensais`) e a linha de Encargos da tabela de folha saem daqui.
    Soma NULL-aware por componente (componente NULL nao soma); mes sem
    NENHUMA guia vira `na` — os dois consumers escolhem o que fazer com ele:
    a razao do card usa 0 (folha-only) e a tabela exibe R$ 0,00.
    """
    if encargos_df is None or encargos_df.empty:
        s = pd.Series(float("nan"), index=periods)
    else:
        comp = (encargos_df.groupby("competencia")[["inss_empresa", "fgts", "fgts_resc"]]
                .sum(min_count=1))
        s = comp.sum(axis=1, min_count=1).reindex(periods)
    return s if pd.isna(na) else s.fillna(na)


def folha_por_linha(rub_df, periods, encargos_df=None, fat_pm=None):
    """A tabela UNICA de custo de folha. FONTE UNICA app x PDF.

    **Um custo, um percentual**: a tabela tem UM fundo — `CUSTO TOTAL` — e um
    so percentual sobre ele, sem dois totais de nomes parecidos que obriguem
    o leitor a escolher um.

    Recebe `mart.folha_rubricas_mensais` (+ `mart.encargos_mensais` e a serie
    de faturamento do consumer) e devolve:
        tabela      DataFrame (linhas VISIVEIS x meses COM dado), em R$:
                    as de rubrica na ordem de LINHAS_FOLHA_RUBRICA + a de
                    encargos (LINHA_ENCARGOS) no fim
        custo       Series: CUSTO TOTAL = soma de TODAS as linhas da tabela
                    (as de rubrica, o 13o e os encargos da guia)
        pct_fat     Series: CUSTO TOTAL / faturamento do mes, em %
        custo_rubricas  Series: so as linhas de rubrica (ancora com
                    `mart.folha_mensal` sem tp 41/51/92)
        encargos    Series: a linha de encargos, isolada
        decimo_terceiro  Series: o tp52 (inspecao; ja e linha da tabela)
        qtd_he      Series: horas de hora extra por mes
        antecipacoes Series: adiantamento + 13o 1a parcela + RPA — NOTA, nunca
                    linha somavel (e o mesmo dinheiro do mensal)
        outros_top  Series (nome da rubrica -> R$ na janela, top
                    OUTROS_TOP_N) e
        outros_resto float: o que abre a caixa-preta da linha "Outros"

    **LINHAS OPCIONAIS SO APARECEM COM MOVIMENTO**: Ferias,
    Rescisao, Hora Extra, Comissao, DSR, Outros e 13o entram na tabela apenas
    se houver valor != 0 em ALGUM mes da janela EXIBIDA. Dentro da janela, mes
    zerado de uma linha presente mostra R$ 0,00 normalmente — ali o zero e
    informacao ("nao pagou comissao em marco"), enquanto a linha inteira
    zerada e so ruido. O criterio mora AQUI, nao no render, pra o PDF herdar
    identico. Estruturais (LINHAS_FOLHA_SEMPRE + encargos) nunca somem.

    **So `valor_p`**: a visao e de CUSTO BRUTO. Desconto (D) e informativo (I,
    onde mora o FGTS por rubrica) nao somam — o FGTS entra UMA vez, pela guia.

    **Mes sem dado vira coluna AUSENTE, nunca zero** — zero afirmaria "custou
    R$ 0,00", ausencia declara que nao ha folha.

    Sem `rub_df` a tabela sai VAZIA mesmo que haja encargos: uma tabela so de
    encargos nao e a visao de folha, e o consumer degrada pro aviso.
    """
    vazio = pd.Series(dtype=float)
    if rub_df is None or rub_df.empty:
        return {"tabela": pd.DataFrame(), "qtd_he": vazio, "custo": vazio,
                "pct_fat": vazio, "custo_rubricas": vazio, "encargos": vazio,
                "decimo_terceiro": vazio, "antecipacoes": vazio,
                "abatimentos": abatimentos_de_custo(None, []),
                "outros_top": vazio, "outros_resto": 0.0}

    df = rub_df.copy()
    df["valor_p"] = pd.to_numeric(df["valor_p"], errors="coerce").fillna(0.0)

    antec = df[df["tipo_process"].isin(TIPOS_PROCESS_ANTECIPACAO)]
    linhas = rubricas_de_custo(df).copy()
    # Meses COM dado DE CUSTO, na ordem de `periods` (nao a do frame). O corte
    # e sobre `linhas`, nao sobre `df`: mes que so tem ANTECIPACAO nao tem
    # custo nenhum a exibir, e deixar a coluna sobreviver zerada afirmaria
    # "a folha custou R$ 0,00" — quando o que houve foi folha ainda nao
    # processada (ex.: mes so com recibo de ferias, tp60). Mes sem dado =
    # coluna AUSENTE, sempre.
    presentes = [p for p in periods if p in set(linhas["competencia"])]
    linhas["_linha"] = [
        _linha_da_rubrica(tp, fam, nome)
        for tp, fam, nome in zip(linhas["tipo_process"],
                                 linhas["desc_classificacao"], linhas["nome"])]

    def _por_mes(frame, col="valor_p"):
        if frame.empty:
            return pd.Series(0.0, index=presentes)
        s = frame.groupby("competencia")[col].sum()
        return s.reindex(presentes).fillna(0.0)

    rub = (linhas.pivot_table(index="_linha", columns="competencia",
                              values="valor_p", aggfunc="sum", fill_value=0.0)
           .reindex(index=list(LINHAS_FOLHA_RUBRICA), columns=presentes,
                    fill_value=0.0)
           .fillna(0.0))
    rub.index.name = None
    rub.columns.name = None

    # Linha opcional sem movimento na janela EXIBIDA nao entra. `custo` nao
    # muda com o corte (o que sai e zero em todos os meses) — a soma segue
    # fechando por construcao.
    visiveis = [ln for ln in LINHAS_FOLHA_RUBRICA
                if ln in LINHAS_FOLHA_SEMPRE
                or bool((rub.loc[ln].abs() >= _EPS_MOVIMENTO).any())]
    rub = rub.reindex(index=visiveis)

    # Os espelhos abatem a linha do provento que espelham — "Folha", onde a
    # familia "Salarios" vive. Aplicado DEPOIS do corte de linhas opcionais
    # porque "Folha" e estrutural (LINHAS_FOLHA_SEMPRE) e esta sempre no
    # indice; e ANTES da soma, pra `custo_rubricas` ja sair liquido e o card
    # (que subtrai a mesma serie em `custo_folha_pm`) bater por construcao.
    abat = abatimentos_de_custo(linhas, presentes)
    abat["salario_familia"] = sf_compensado_por_mes(encargos_df, presentes)
    abat["total"] = abat["total"] + abat["salario_familia"]
    for natureza, linha in LINHA_ABATIMENTO.items():
        v = abat.get(natureza)
        if v is None or float(v.abs().sum()) <= 0 or linha not in rub.index:
            continue
        rub.loc[linha] = rub.loc[linha] - v

    enc = encargos_por_mes(encargos_df, presentes, na=0.0)
    tabela = pd.concat([rub, enc.to_frame(LINHA_ENCARGOS).T])

    custo_rubricas = rub.sum()
    custo = custo_rubricas + enc
    if fat_pm is None:
        pct_fat = pd.Series(float("nan"), index=presentes)
    else:
        fat = pd.to_numeric(pd.Series(fat_pm), errors="coerce").reindex(presentes)
        # Mesmo piso de materialidade dos cards, ponto a ponto: faturamento POSITIVO porem
        # imaterial nao sustenta percentual — e a linha ficaria gritando
        # 40.000% exatamente onde o card do hero exibe "—".
        pct_fat = (safe_div(custo, fat.where(fat >= piso_denominador(1))) * 100)

    he = linhas[linhas["_linha"] == "Hora Extra"]
    qtd_he = (_por_mes(he, "qtd_informada") if "qtd_informada" in linhas.columns
              else pd.Series(0.0, index=presentes))

    # Caixa-preta da linha "Outros": sem isso o leitor ve um numero grande sem
    # nome (pro-labore, auxilio transporte, multa do Art. 479, reflexos de 13o
    # pagos no mensal...). Agregado da JANELA, ordenado por valor.
    _o = linhas[linhas["_linha"] == "Outros"]
    if _o.empty:
        outros_top, outros_resto = vazio, 0.0
    else:
        _s = (_o.groupby(_o["nome"].astype(str))["valor_p"].sum()
              .sort_values(ascending=False))
        _s = _s[_s.abs() >= _EPS_MOVIMENTO]
        outros_top = _s.head(OUTROS_TOP_N)
        outros_resto = float(_s.iloc[OUTROS_TOP_N:].sum())

    d13 = (rub.loc[LINHA_13O] if LINHA_13O in rub.index
           else pd.Series(0.0, index=presentes))
    return {"tabela": tabela, "qtd_he": qtd_he, "custo": custo,
            "pct_fat": pct_fat, "custo_rubricas": custo_rubricas,
            "encargos": enc, "decimo_terceiro": d13,
            # indexada nos meses COM antecipacao, nao em `presentes`: o mes
            # que so tem recibo sai da TABELA, mas o dinheiro dele nao pode
            # sumir da nota — seria esconder caixa que a empresa desembolsou.
            "antecipacoes": (antec.groupby("competencia")["valor_p"].sum()
                             .reindex([p for p in periods
                                       if p in set(antec["competencia"])],
                                      fill_value=0.0)
                             if not antec.empty else pd.Series(dtype=float)),
            "abatimentos": abat,
            "outros_top": outros_top, "outros_resto": outros_resto}


def folha_destaques(tabela):
    """As linhas de destaque da tabela de folha, na ordem FIXA.

    Projecao de EXIBICAO sobre a tabela que `folha_por_linha` ja montou — le o
    **MESMO DataFrame que a tela exibe**, nunca uma segunda derivacao. E a
    mesma regra do `maiores_linhas`: card e linha tem que ser o mesmo numero, e
    a unica forma de garantir isso e nao recalcular.

    O resultado e **CONTIDO** no CUSTO TOTAL, nao aditivo: estas rubricas ja
    estao dentro da linha "Folha + Encargos" que fica acima. Somar as duas
    regioes conta o mesmo dinheiro duas vezes — por isso o consumer e obrigado
    a exibir o `SUBTITULO_DESTAQUES_FOLHA` junto.

    **Linha sem movimento na janela nao entra** (some a linha, nao vira linha
    de zeros). O `folha_por_linha` ja corta as opcionais, mas o filtro se
    repete aqui pra a funcao ser correta sobre QUALQUER tabela — inclusive a
    de um consumer que monte a sua. Dentro da janela, mes zerado de uma linha
    PRESENTE segue exibindo R$ 0,00: ali o zero e informacao ("nao pagou
    comissao em julho"), enquanto a linha inteira zerada e so ruido.

    Fonte unica app x PDF: os dois renderizam exatamente esta selecao.
    """
    if tabela is None or getattr(tabela, "empty", True) or not len(tabela.columns):
        return pd.DataFrame()
    presentes = [ln for ln in RUBRICAS_DESTAQUE_FOLHA
                 if ln in tabela.index
                 and bool((tabela.loc[ln].abs() >= _EPS_MOVIMENTO).any())]
    return tabela.reindex(index=presentes)


# O ESTORNO DO 13o NAO ENTRA AQUI, e isso e decisao — nao esquecimento.
# A legenda abre com "Fora do custo no periodo:", e as tres naturezas abaixo sao
# dinheiro que a empresa NAO desembolsou. O estorno da 1a parcela e o oposto:
# dinheiro que FOI desembolsado, so em outro mes. Lista-lo ali afirmaria que o
# 13o saiu do custo, o que e falso. Quem explica o
# estorno ao leitor e a NOTA da linha do 13o. Como `legenda_abatimentos` itera
# este dict, nao registrar ja exclui.
ROTULO_ABATIMENTO = {
    "afastamento": "afastamento pago pelo INSS",
    "falta": "faltas",
    "salario_familia": "salário família compensado na guia",
}


def legenda_abatimentos(abatimentos):
    """A linha que DECLARA o que foi abatido do custo — fonte unica app x PDF.

    O abatimento pousa dentro da linha (a tabela mantem a forma e a soma
    segue fechando) MAIS uma legenda que nomeia cada ajuste com valor. Sem
    ela o numero cai e ninguem sabe por que — e o dash nao concilia com o
    "Resumo da Folha" do Dominio.

    Mesma forma do `legenda_outros`: linha corrida, separador " · ", so o que
    tem valor. Devolve "" quando nao houve abatimento, pro chamador nao
    renderizar nada.
    """
    if not abatimentos:
        return ""
    itens = []
    for chave, rotulo in ROTULO_ABATIMENTO.items():
        serie = abatimentos.get(chave)
        if serie is None:
            continue
        v = float(pd.Series(serie).fillna(0).sum())
        if v >= _EPS_MOVIMENTO:
            itens.append(f"{rotulo} {fmt_brl(v)}")
    return "Fora do custo no período: " + " · ".join(itens) if itens else ""


def legenda_outros(outros_top, outros_resto):
    """Linha de legenda que abre o "Outros" -- fonte unica do app e do PDF.

    O conteudo e o top OUTROS_TOP_N + "Demais", ja cortado por
    `folha_por_linha`, na FORMA de uma linha corrida embaixo do grafico de
    composicao, em fonte de rodape -- nao uma secao propria.

    Nomes das rubricas saem COMO ESTAO na origem (nao traduzir): "MULTA ART.
    479/CLT" e o que o contador procura no Dominio. Separador " · " porque a
    linha e corrida; se estourar a largura, QUEBRA em duas linhas -- nunca
    truncar (o rotulo cortado perde informacao).

    Devolve "" quando nao ha rubrica em Outros, pro chamador simplesmente nao
    renderizar nada.
    """
    if outros_top is None or not len(outros_top):
        return ""
    itens = [f"{nome} {fmt_brl(float(v))}" for nome, v in outros_top.items()]
    if float(outros_resto or 0.0) > 0:
        itens.append(f"Demais {fmt_brl(float(outros_resto))}")
    return "Outros: " + " · ".join(itens)


def impostos_exibidos(imp_df):
    """Tabela de impostos EXIBIDA = so imposto PROPRIO da empresa.

    Saem dois universos, por motivos juridicos DIFERENTES (constantes
    separadas em mappings, mesmo destino):
      - CODI_IMP_RETENCAO_TERCEIROS: guia e da empresa, imposto e de
        terceiro (ISS/INSS Ret, IRRF aluguel PF, CSRF...) — mesma regua
        que tira esses codigos do numerador do card.
      - CODI_IMP_SCP: tributo da Sociedade em Conta de Participacao; o
        DARF em nome do ostensivo e veiculo de recolhimento.
    O 83 "RET" (Regime Especial de Tributacao, patrimonio de afetacao)
    PERMANECE: o nome abreviado engana, mas e regime PROPRIO.

    Puro, nao muta o input. DataFrame vazio ou SEM a coluna codi_imp
    retorna INALTERADO (guard de compatibilidade, nao filtro silencioso).
    Resultado VAZIO com input nao-vazio e informacao: a empresa so recolheu
    imposto de terceiro no periodo — o consumer deve dizer isso, nunca
    "sem dados" (agentes de retencao puros).
    """
    if imp_df.empty or "codi_imp" not in imp_df.columns:
        return imp_df
    fora = CODI_IMP_RETENCAO_TERCEIROS | CODI_IMP_SCP
    return imp_df[~imp_df["codi_imp"].isin(fora)]


# PISO DE MATERIALIDADE do denominador.
#
# Razao CERTA sobre denominador desprezivel quebra a tela (ex.: YoY de
# milhoes de % sobre uma base de poucos reais). Os guards de denominador == 0
# (razao vira NaN e o card mostra "—") nao barram denominador POSITIVO porem
# imaterial. O `fat_prev <= 0` do YoY e guard de SINAL, nao de grandeza.
#
# O piso e POR MES e escala com a janela, pra 1 mes e 12 meses exigirem a
# mesma materialidade media — sem isso um range longo passaria com um unico
# mes de movimento. Denominador abaixo do piso -> NaN + flag de motivo; os
# consumers ja renderizam "—" pra NaN (mesmo caminho do sem-registros).
PISO_DENOMINADOR_MENSAL: float = 1000.0


def piso_denominador(n_meses: int) -> float:
    """Piso do denominador para uma janela de `n_meses`.

    max(n, 1) porque janela vazia nao pode zerar o piso — zero deixaria
    passar exatamente o caso que o piso existe pra barrar.
    """
    return PISO_DENOMINADOR_MENSAL * max(int(n_meses), 1)


# Cards de balanco com saldo NEGATIVO.
# O valor exibido NAO muda: nunca abs(), nunca esconder. A causa provavel e
# defeito de ORIGEM (sinal invertido na escrituracao), e esconder apagaria a
# evidencia que o contador leva pro cliente. A tela ANOTA.
NOTA_SALDO_INVERTIDO = "saldo invertido na escrituração"

# Empresa com balancete SO fora da janela: o consumer
# troca a caption "Valores do balancete em {mes}" (que nomearia um mes cujo
# saldo veio 100% do carry-in) por esta, declarando a ausencia em vez de
# afirmar um numero emprestado de fora do periodo. Fonte unica (app + PDF).
NOTA_BAL_SEM_DADO_PERIODO = "Sem balancete no período selecionado."

# Empresa com balancete real na janela, mas SEM
# movimento no ULTIMO mes dela (o saldo exibido e ffill de um mes anterior,
# ainda assim DENTRO da janela). O numero fica (saldo patrimonial e
# cumulativo — apagar seria errado); a caption declara que o ultimo mes nao
# teve movimento, pra nao insinuar dado fresco onde ha carry-in.
NOTA_BAL_SEM_MOVIMENTO_PERIODO = "sem movimento no período — saldo carregado do último balancete"

# Rotulo do badge de Liquidez/Endividamento com balanco QUEBRADO
# (flags_inconsistencia: PC<=0/AC<0 ou PL<=0/AT<=0). "Inconsistente" soaria a
# defeito do SISTEMA; a causa provavel e defeito de ESCRITURACAO na origem, e
# o rotulo aponta pra la. Fonte unica app+PDF; NAO cobre o uso de
# CTX_INCONSISTENTE em Lucratividade (fat_pm_last<=0), que e um caso
# semantico DIFERENTE (falta de faturamento NF, nao balanco quebrado).
CTX_BALANCETE_EM_REVISAO = "balancete em revisão"

# Lucratividade extrema em janela curta: NAO clampar; ganha selo de janela
# curta (numero feio e honesto > numero bonito). Poucos meses concentram o LAIR de um so mes no card — o numero
# pode sair extremo, e a resposta e CONTEXTUALIZAR, nao capar. Limiar
# proprio (mesmo valor de `pdf_generator.FAIXA_CURTA_MAX_MESES`, mas
# constante SEPARADA de proposito: aquela e concern de LAYOUT de chart, esta
# e concern de badge de DADO — mesmo numero, motivos diferentes, nomes
# separados, mesmo padrao das constantes _CHART_* do pdf_generator).
LUC_JANELA_CURTA_MESES = 3
LUC_JANELA_CURTA_FRAG = "janela curta"


def cards_saldo_invertido(cards):
    """Labels dos cards de balanco cujo saldo FINAL e negativo.

    `cards` = dict {label: serie mensal}, na ordem em que a tela exibe — a
    nota lista na mesma ordem. Olha o MESMO ponto que o card (last_valid).
    Limiar -0,005 casa com o fmt_brl: o que arredonda pra "R$ 0,00" nao e
    negativo. Fonte unica de app e PDF.
    """
    fora = []
    for label, serie in cards.items():
        v = last_valid(serie)
        if pd.notna(v) and v < -0.005:
            fora.append(label)
    return fora


def iss_carga_mensal(impd_df, periods):
    """Serie mensal do componente ISS pro bloco Impostos — fonte unica app x pdf.

    O bloco de Impostos exibe o universo GUIA (efsdoimp). Empresa com ISS
    100% retido na fonte pelo tomador nunca tem guia de ISS, entao o bloco
    mostraria ZERO e o "% sobre Faturamento" sairia subestimado. O valor
    certo por mes e o `valo_iss` do mart.impostos_destacados_mensais
    (balancete quando ha conta de (-)ISS no mes, senao guia cod 3 + retido
    sofrido); aqui ele so e preparado pro pivot.

    Devolve None quando NAO ha componente ISS em mes nenhum (empresa sem ISS,
    Simples, ou mart antigo sem a coluna) — o consumer nao desenha a linha.
    Caso contrario, Series indexada por `periods`, com 0.0 nos meses sem
    valor, pra alinhar com as linhas de guia (que ja usam fill_value=0).

    PRESENCA e decidida por notna(), NUNCA por `> 0`: conta escriturada com
    ZERO significa "nao houve ISS no mes", e isso e informacao — a linha
    aparece e renderiza R$ 0,00.
    """
    if impd_df is None or impd_df.empty or "valo_iss" not in impd_df.columns:
        return None
    s = impd_df.groupby("competencia")["valo_iss"].sum(min_count=1)
    if int(s.notna().sum()) == 0:
        return None
    return s.reindex(periods).fillna(0.0)


# Rotulo da linha de % do bloco Impostos. NOMEIA O UNIVERSO: o % e a soma DO
# QUE ESTA NA TABELA, nao o do card do hero — os dois divergem por universo.
# CONSTANTE porque o styler do app casa a linha por NOME: trocar o texto em
# um lugar so faria a linha perder cor e formatacao de percentual, em
# silencio.
# Rotulo da tabela de GUIAS. O qualificador "(tributos exibidos)" FICA: essa
# tabela so aparece no Simples, e la ela nomeia um universo diferente do
# apurado. Ver TITULO_IMPOSTOS_GUIAS logo abaixo.
LINHA_PCT_IMPOSTOS = "% sobre Faturamento (tributos exibidos)"
TITULO_IMPOSTOS_GUIAS = "Guias recolhidas por tributo"
LINHA_TOTAL_IMPOSTOS = "TOTAL"

# Limiar de ALERTA do % Impostos — UM valor, dois consumidores no app (o ⚠️ do
# card e a cor da linha na tabela).
#
# Calibrado no universo GUIA, cujo % tem cauda mais curta que o do apurado; o
# valor e MEDIDO na distribuicao da carteira, nao arbitrado (mesmo metodo do
# PISO_DENOMINADOR). Nao mexer sem refazer a medicao.
#
# Numero medido nao pode viver hardcoded em dois lugares — o card e a linha da
# tabela divergiriam na MESMA tela — e por isso ele mora aqui.
ALERTA_PCT_IMPOSTOS = 20.0


def tabela_impostos(imp_df, impd_df, periods, fat_pm, linha_extra=None):
    """O bloco Impostos (tabela + series do chart). FONTE UNICA app x PDF.

    Universo **CARGA**: guia recolhida por `codi_imp` (`mart.impostos_mensais`)
    para todos os tributos, MENOS o ISS, que vem pela carga tributaria
    (`valo_iss` do `mart.impostos_destacados_mensais`, via `iss_carga_mensal`).

    O app e o PDF de Detalhamento de Impostos consomem o MESMO codigo.
    Reimplementar de um lado criaria dois universos com o mesmo nome na mesma
    tela — a mesma razao de existir de `folha_por_linha` e `folha_custo`.

    Devolve dict:
        tabela       DataFrame linhas x meses. Linhas = tributos com valor
                     (+ ISS quando ha) em ordem alfabetica, depois TOTAL e
                     LINHA_PCT_IMPOSTOS. **Colunas = `periods` CRUS**
                     ("YYYY-MM") — rotular e do consumer, igual a
                     `folha_por_linha`.
        series_chart dict {nome -> Series} SO dos tributos com movimento: a
                     barra de altura zero so ocuparia a legenda. Da TABELA
                     nunca sai — conta de (-)ISS escriturada com zero e
                     informacao ("nao houve ISS no mes"), nao ausencia.
        iss_fontes   tupla ordenada dos valores distintos de `fonte` na
                     janela COM componente de ISS. Declara a PROVENIENCIA
                     ('balancete' / 'guia' / 'retido' e combinacoes). Nunca
                     ratear entre os componentes: dentro de 'guia+retido' o
                     mart guarda so o `valo_iss` combinado, sem o split em R$.
        so_retencao  a empresa TEM guias, mas todas de imposto de TERCEIRO.
                     Dizer isso e informacao; "sem dados" seria mentira.
        tem_dado     ha o que exibir (tributo proprio com valor OU ISS).

    O de-dup do ISS e load-bearing: quando `iss_carga_mensal` existe, o
    `codi_imp=3` SAI das linhas de guia — senao o mesmo ISS (guia +
    balancete) seria somado duas vezes.
    """
    imp = imp_df.copy() if imp_df is not None else pd.DataFrame()
    if not imp.empty:
        imp["nome_imp"] = imp["codi_imp"].map(IMP_NOMES).fillna(
            imp["codi_imp"].astype(str))

    # So imposto PROPRIO. Retencao de terceiros e SCP saem — o 83 "RET" FICA,
    # e regime proprio apesar do nome.
    imp_proprios = impostos_exibidos(imp)
    iss_serie = iss_carga_mensal(impd_df, periods)
    if iss_serie is not None and not imp_proprios.empty:
        imp_proprios = imp_proprios[
            imp_proprios["codi_imp"] != CODI_IMP_ISS]

    # O valor exibido e o DEVIDO da guia: principal + adicional de IRPJ.
    # GUARD DE 1o DEPLOY: entre a atualizacao do codigo e a carga o mart
    # ainda nao tem a coluna — cai pro principal em vez de estourar KeyError
    # na tela. Mesmo padrao do card de encargos.
    col_val = "valo_devido" if "valo_devido" in imp.columns else "srec_sim"

    if not imp_proprios.empty:
        totais = imp_proprios.groupby("nome_imp")[col_val].sum()
        ativos = totais[totais.abs() > 0].index.tolist()
        imp_vis = imp_proprios[imp_proprios["nome_imp"].isin(ativos)]
    else:
        imp_vis = imp_proprios

    # "tinha valor e sobrou nada" != "nunca teve dado" — empresas que so sao
    # agentes de retencao merecem a frase honesta, nao o generico. O
    # predicado olha o que a tabela REALMENTE exibiria (proprio E com valor):
    # com tributos proprios ZERADOS junto das retencoes com valor, checar so
    # "sobrou linha propria" deixaria a tabela vazia.
    so_retencao = (not imp.empty
                   and float(imp[col_val].abs().sum()) > 0
                   and imp_vis.empty)

    if imp_vis.empty and iss_serie is None and not (linha_extra or {}):
        return {"tabela": pd.DataFrame(), "series_chart": {},
                "iss_fontes": (), "so_retencao": so_retencao, "tem_dado": False}

    # `linha_extra` = {rotulo: Serie} do fallback de IRPJ/CSLL pela
    # contabilidade. Entra AQUI, e nao no consumer,
    # pra o TOTAL e o "% sobre Faturamento" saírem recalculados por dentro —
    # card == TOTAL da tabela continua valendo POR CONSTRUCAO.
    extra = dict(linha_extra or {})

    linhas = {}
    if not imp_vis.empty:
        for nome in imp_vis["nome_imp"].unique():
            linhas[str(nome)] = (imp_vis[imp_vis["nome_imp"] == nome]
                                 .groupby("competencia")[col_val].sum()
                                 .reindex(periods, fill_value=0))
    if iss_serie is not None:
        linhas["ISS"] = iss_serie
    for _rot, _serie in extra.items():
        linhas[_rot] = pd.Series(_serie).reindex(periods).astype("float64")
    if extra:
        # a empresa PAGA imposto proprio pela contabilidade: nao e "so retencao"
        so_retencao = False

    # Ordem alfabetica UNICA pro chart e a tabela: ordem de APARICAO faria os
    # dois divergirem.
    tabela = pd.DataFrame({n: linhas[n] for n in sorted(linhas)}).T
    tabela.index.name = None
    tabela.columns.name = None
    series_chart = {n: linhas[n] for n in sorted(linhas)
                    if float(linhas[n].abs().sum()) != 0}

    total = tabela.sum()
    fat = pd.Series(fat_pm) if fat_pm is not None else pd.Series(dtype=float)
    tabela.loc[LINHA_TOTAL_IMPOSTOS] = total
    # fat <= 0 -> "—": o NFe liquido de devolucao (sem piso) pode fechar o
    # mes NEGATIVO, e razao sobre denominador nao positivo nao existe (mesma
    # regua do % Folha e dos cards).
    tabela.loc[LINHA_PCT_IMPOSTOS] = [
        (total[p] / fat_v * 100) if (fat_v := fat.get(p, 0)) > 0 else float("nan")
        for p in periods]

    return {"tabela": tabela, "series_chart": series_chart,
            "iss_fontes": _iss_fontes(impd_df) if iss_serie is not None else (),
            "so_retencao": so_retencao, "tem_dado": True}


def maiores_linhas(tabela, n=3, excluir=()):
    """As `n` maiores linhas de uma tabela ja montada, por soma no periodo.

    Alimenta os cards de destaque das faixas dos dois detalhamentos. Le o **MESMO DataFrame que a tabela exibe** — nunca uma
    segunda derivacao: o card e a linha tem que ser o mesmo numero, e a unica
    forma de garantir isso e nao recalcular.

    `excluir` tira linhas de TOTAL/percentual, que nao sao itens e venceriam
    qualquer ranking. Linha sem valor (soma zero, NaN) fica FORA: com menos de
    `n` itens com movimento saem menos cards — nunca card de enchimento.

    Devolve lista de (rotulo, total) em ordem decrescente.
    """
    if tabela is None or tabela.empty or not len(tabela.columns):
        return []
    fora = set(excluir)
    somas = [(str(i), float(tabela.loc[i].sum()))
             for i in tabela.index if str(i) not in fora]
    somas = [(nome, v) for nome, v in somas
             if pd.notna(v) and abs(v) >= _EPS_MOVIMENTO]
    somas.sort(key=lambda t: t[1], reverse=True)
    return somas[:max(int(n), 0)]


def _iss_fontes(impd_df):
    """Valores distintos de `fonte` nos meses COM componente de ISS."""
    if impd_df is None or impd_df.empty or "fonte" not in impd_df.columns:
        return ()
    com_iss = impd_df[impd_df["valo_iss"].notna()]
    return tuple(sorted({str(f) for f in com_iss["fonte"].dropna().unique()}))


def last_valid(s):
    """Último valor REAL da série; NaN se vazia/all-NaN. Remove ±inf antes.

    NaN, não 0.0: 0.0 fabricaria um zero que (a) exibe "0,00" no card e (b)
    tropeçaria no guard `<= 0`. fmt_brl/pct/ind mapeiam
    NaN/inf -> "—".
    """
    v = s.replace([float("inf"), float("-inf")], float("nan")).dropna()
    return float(v.iloc[-1]) if not v.empty else float("nan")


# A linha do fallback NAO leva qualificador: ela sai identica em FORMA as
# linhas de guia — "IRPJ" e "CSLL". O contador le um tributo, nao a
# proveniencia dele.
def irpj_guia_por_mes(imp_df, periods):
    """IRPJ + CSLL RECOLHIDOS via guia (`codi_imp` 7 e 6), por mes."""
    g = imp_df if imp_df is not None else pd.DataFrame()
    if g.empty or "codi_imp" not in g.columns:
        return pd.Series(float("nan"), index=periods, dtype="float64")
    col = "valo_devido" if "valo_devido" in g.columns else "srec_sim"
    return (g[g["codi_imp"].isin(CODI_IMP_IRPJ_CSLL)]
            .groupby("competencia")[col].sum(min_count=1)
            .reindex(periods))


def irpj_contabil_pm(imp_df, bal_ir_df, periods):
    """O fallback contabil de IRPJ/CSLL **com o portao aplicado** — `{}` quando
    a guia manda.

    PORTAO ANTI DUPLA CONTAGEM (mesmo precedente do
    `GUIA_IMPORTAVEL_DESTACADOS`): o fallback entra SO onde a fonte
    primaria e vazia no PERIODO INTEIRO — nunca soma por cima, e nunca costura
    mes a mes entre as duas fontes.

    "Guia vence o periodo inteiro" NAO e caso de borda: e comum a guia ter
    valor em so 3-4 dos 12 meses, porque o Presumido apura por TRIMESTRE.
    Costurar por mes poria contabilidade nos meses sem guia dessas empresas e
    inflaria o numerador delas — o oposto do que o fallback quer.

    Funcao propria porque tem DOIS consumidores: `series_mensais` e a
    sparkline do PDF, que monta o numerador chamando `tabela_impostos`
    direto. Reimplementar o portao la seria uma segunda derivacao do mesmo
    criterio. Quem precisar do numerador com fallback chama daqui.
    """
    if float(irpj_guia_por_mes(imp_df, periods).fillna(0).abs().sum()) >= _EPS_MOVIMENTO:
        return {}
    return irpj_csll_pago_pm(bal_ir_df, periods) if bal_ir_df is not None else {}


LINHAS_IRPJ_CSLL = ("IRPJ", "CSLL")


def _competencia_do_trimestre(comp):
    """Mes do DEBITO -> mes-fim do trimestre que ele liquida.

    O IRPJ/CSLL do Presumido/Real e apurado por TRIMESTRE e pago em ate tres
    quotas no trimestre SEGUINTE. Exibir no mes do desembolso espalharia o
    imposto por meses que nao o geraram; a guia (`efsdoimp`) ja aparece na
    competencia, e a linha do fallback tem de ler igual — senao a mesma tabela
    teria dois eixos de tempo.

      abr-jun -> mar | jul-set -> jun | out-dez -> set | jan-mar -> dez ANTERIOR

    Valor = PAGO; eixo = competencia.
    """
    try:
        ano, mes = int(str(comp)[:4]), int(str(comp)[5:7])
    except (ValueError, IndexError):
        return None
    if mes in (4, 5, 6):
        return f"{ano}-03"
    if mes in (7, 8, 9):
        return f"{ano}-06"
    if mes in (10, 11, 12):
        return f"{ano}-09"
    return f"{ano - 1}-12"


def irpj_csll_pago_pm(bal_df, periods):
    """IRPJ/CSLL LIQUIDADO no passivo, por mês — o fallback de IRPJ/CSLL
    pela contabilidade.

    Empresa que apura e PAGA IRPJ/CSLL mas nao emite a guia pelo modulo do
    Dominio ficava invisivel no card. Aqui se le o DEBITO nas contas de
    passivo (`CLAS_PASSIVO_IRPJ_CSLL`) — dinheiro que SAIU —, coerente com o
    criterio "o numero e o que pagou". **Nao e a provisao** (`clas 40106`),
    que e competencia.

    O criterio e `clas` INTERSECAO `nome`, nunca um dos dois sozinho: a mesma
    `clas` e "CSLL" na maioria das empresas mas e "COFINS Nao Cumulativa" ou
    "IRRF S/ ALUGUEIS" em outras, e entraria errado.
    `RE_PASSIVO_FORA` tira parcelamento e retencao de terceiro, que casam por
    nome sem ser o imposto corrente.

    ⚠️ **O numero e CAIXA e descasa da competencia** — e o preco de medir
    desembolso. Uma empresa pode pagar menos que a provisao do ano (o resto
    sai no ano seguinte) ou muito mais, liquidando saldo de exercicios
    anteriores. Declarado, nao
    corrigido: "o IRPJ pago no periodo" nao e "o IRPJ do periodo".

    Devolve {"IRPJ": Serie, "CSLL": Serie} — SO as chaves com movimento. Duas
    linhas separadas, como a tabela de guias ja faz quando ha guia dos dois;
    juntar aqui criaria assimetria de forma na mesma tabela.
    Series NULL-aware (NaN onde nao ha pagamento).
    """
    vazio = {}
    need = {"clas_cta", "nome_cta", "valo_deb", "competencia"}
    if bal_df is None or bal_df.empty or not need.issubset(bal_df.columns):
        return vazio
    d = bal_df.copy()
    d = d[d["clas_cta"].astype(str).isin(CLAS_PASSIVO_IRPJ_CSLL)]
    if d.empty:
        return vazio
    d = d.assign(_trim=d["competencia"].map(_competencia_do_trimestre))
    d = d[d["_trim"].isin(list(periods))]
    if d.empty:
        return vazio
    nome = d["nome_cta"].fillna("").astype(str)
    fora = nome.str.contains(RE_PASSIVO_FORA, case=False, regex=True, na=False)
    ir = nome.str.contains(RE_PASSIVO_IRPJ, case=False, regex=True, na=False)
    cs = nome.str.contains(RE_PASSIVO_CSLL, case=False, regex=True, na=False)
    out = {}
    # Nome que cita os DOIS fica fora: atribuir por chute poria o tributo
    # errado numa linha que o contador confere.
    for rotulo, mask in (("IRPJ", ~fora & ir & ~cs), ("CSLL", ~fora & cs & ~ir)):
        sel = d.loc[mask]
        if sel.empty:
            continue
        serie = (sel.groupby("_trim")["valo_deb"].sum(min_count=1)
                 .reindex(periods).astype("float64"))
        if float(serie.fillna(0).abs().sum()) >= _EPS_MOVIMENTO:
            out[rotulo] = serie
    return out


def series_mensais(fat_df, impd_df, folha_df, func_df, bal_serie_df, periods,
                   encargos_df=None, dre_df=None, rub_df=None, imp_df=None,
                   bal_ir_df=None):
    """Todas as séries mensais unificadas (dict), reindexadas em `periods`.

    `bal_serie_df` JÁ vem seedado (carry-in pré-janela) pelo consumer — o seed
    é DB-bound e fica no call site (app.py / pdf_generator.py). Não muta os
    DataFrames do caller (faturamento opera sobre cópia).

    `encargos_df` (opcional): mart.encargos_mensais (inss_empresa /
    fgts / fgts_resc, NULL = sem guia). None/vazio -> enc_pm all-NaN e o
    pct_folenc degrada pra folha-only (retrocompat + "encargos: —").
    """
    def _fat_pm(fd):
        if fd.empty:
            return pd.Series(0.0, index=periods)
        d = fd.copy()
        # Assimetria intencional: valo_saidas entra CRUA (coluna obrigatória do
        # mart — fillna(0) garantido em build_faturamento_mensal/transform.py;
        # faltar = df malformado -> KeyError alto, não under-count calado). valo_servicos é OPCIONAL (NFS-e pode não existir) -> guard de
        # coluna ausente + fillna(0).
        if "valo_servicos" not in d.columns:
            d["valo_servicos"] = 0.0
        d["valo_servicos"] = d["valo_servicos"].fillna(0)
        # 3a origem: parcelas de venda de imovel. MESMO guard do valo_servicos:
        # entre a atualizacao do codigo e a 1a carga o mart ainda NAO tem a
        # coluna, e um KeyError aqui derrubaria o dashboard inteiro em vez de
        # degradar pro numero sem ela.
        if "valo_imobiliario" not in d.columns:
            d["valo_imobiliario"] = 0.0
        d["valo_imobiliario"] = d["valo_imobiliario"].fillna(0)
        d["valo_total"] = (d["valo_saidas"] + d["valo_servicos"]
                           + d["valo_imobiliario"])
        return d.groupby("competencia")["valo_total"].sum().reindex(periods, fill_value=0)

    fat_pm = _fat_pm(fat_df)
    # min_count=1 preserva NaN quando todos os meses são sem_deducao/sem_dado.
    # Impostos DESTACADOS (mart.impostos_destacados_mensais). O numerador do
    # card soma o IRPJ/CSLL provisionado por cima (imp_pm, mais abaixo — o
    # dre_pm só existe depois); esta série fica exposta pra inspeção.
    imp_destacados_pm = (impd_df.groupby("competencia")["valo_impostos"].sum(min_count=1).reindex(periods)
                         if not impd_df.empty else pd.Series(float("nan"), index=periods))
    rec_imp_pm = (impd_df.groupby("competencia")["base_receita"].sum(min_count=1).reindex(periods)
                  if not impd_df.empty else pd.Series(float("nan"), index=periods))
    # (onde ha escrituracao, o zero e afirmacao que o dado sustenta, e FICA).
    # E SERIE, nao booleano de janela cheia, porque `kpis_agregados` apara pela
    # janela EFETIVA: com a cauda aberta escriturada e a janela efetiva sem
    # dado, um booleano global manteria o "R$ 0,00" errado.
    # UM custo: folha_pm = folha de CUSTO, COM o 13o integral — a MESMA base
    # do CUSTO TOTAL da tabela de folha por rubrica. Sai so antecipacao
    # (tp 41/51/92).
    # `rub_df` (mart.folha_rubricas_mensais) entra pelo mesmo idioma DB-bound
    # de `encargos_df`/`dre_df` e e a base do card — com ele,
    # `folha_pm == custo_rubricas` vale por CONSTRUCAO. Sem ele, degrada pro
    # mart.folha_mensal.
    folha_pm = custo_folha_pm(folha_df, rub_df, periods, encargos_df)

    # Encargos fiel a GUIA (mart.encargos_mensais). Soma NULL-aware:
    # componente NULL nao soma; mes sem NENHUMA guia -> NaN. Nota de
    # honestidade metrica: em periodo de cobertura MISTA, o mes sem guia
    # entra no % Folha+Encargos como folha-only (fillna(0) no numerador) —
    # comportamento correto e documentado, nao under-count calado.
    enc_pm = encargos_por_mes(encargos_df, periods)
    # exposta pra inspecao; o abatimento ja aconteceu dentro do folha_pm
    sf_pm = sf_compensado_por_mes(encargos_df, periods)

    # despesas_pm (balancete 401-406) NAO alimenta a lucratividade;
    # segue exposta pra retrocompat/inspecao. NAO confundir com desp_custos_pm
    # (401-405, chart "Despesas e Custos").
    despesas_pm = bal_grupo_serie(bal_serie_df, periods, GRUPOS_BAL["Despesas"])

    # Lucratividade CONTABIL: resultado / receita contabil, do mart.dre_mensal
    # (razao), e nao um proxy NF-based, que inverte o sinal em meses de margem
    # baixa (a base fat_NF fica acima da receita contabil). `dre_df`
    # (empresa x competencia) vem do consumer (DB-bound, como encargos_df/
    # bal_serie_df). None/vazio -> series all-NaN -> card degrada a "—" (guard de
    # 1o deploy no consumer). Denominador contabil so aqui: % Impostos/% Folha/
    # % Encargos seguem NF-based.
    def _dre_pm(col):
        if dre_df is None or dre_df.empty or col not in dre_df.columns:
            return pd.Series(float("nan"), index=periods)
        return dre_df.groupby("competencia")[col].sum(min_count=1).reindex(periods)

    # O numerador da lucratividade e o LAIR, ANTES do IR: o IRPJ/CSLL e
    # tratado como imposto (bloco % Impostos), nao como redutor da margem.
    # Simples fica INTOCADO por construcao (IR ja esta no DAS -> provisao 0 ->
    # lair == liquido, nada muda).
    lair_pm = _dre_pm("lair")                        # numerador (antes do IR)
    receita_contabil_pm = _dre_pm("receita_bruta")   # denominador (grdre 1 / clas 30101)
    despesas_dre_pm = _dre_pm("despesas")            # so pro trigger de supressao
    provisao_ir_pm = _dre_pm("provisao_irpj_csll")   # o IR que migra pro % Impostos

    # NUMERADOR DO % IMPOSTOS = GUIA RECOLHIDA ("o numero e o que pagou").
    #
    # Ele e o TOTAL do `tabela_impostos` — o MESMO objeto que a tabela do
    # Detalhamento exibe —, entao card, chart e tabela sao o mesmo numero POR
    # CONSTRUCAO. Nao ha segundo somador de guia aqui de proposito.
    #
    # O ISS entra pela CARGA, dentro do helper: quando ele e 100% RETIDO NA
    # FONTE pelo tomador nunca vira guia da empresa — mas o imposto E dela, so
    # muda quem recolhe (identidade `balancete = guia + retido`). Ler guia
    # PURA subestimaria o numerador.
    #
    # `imp_df` e None em dicts minimos de teste e consumers antigos: sem ele o
    # numerador fica todo NaN (card "—"), nunca zero fabricado.
    _tab_imp = tabela_impostos(
        imp_df if imp_df is not None else pd.DataFrame(),
        impd_df, periods, None)
    if _tab_imp["tem_dado"]:
        imp_pm = _tab_imp["tabela"].loc[LINHA_TOTAL_IMPOSTOS].astype("float64")
    else:
        imp_pm = pd.Series(float("nan"), index=periods, dtype="float64")
    # A empresa TEM guias, mas todas de imposto de TERCEIRO. Dizer isso e
    # informacao; "sem informacao" seria menos, e "R$ 0,00" seria FALSO.
    imp_so_retencao = bool(_tab_imp["so_retencao"])
    # Discriminador do "R$ 0,00 x sem informacao" no universo da guia: onde a
    # empresa DECLARA — tem linha de guia propria no periodo, ainda que
    # zerada —, o zero e afirmacao que o dado sustenta e FICA. Empresa que
    # declarou e nao deveu nada afirma R$ 0,00; empresa sem linha nenhuma nao
    # tem o que afirmar.
    _decl = imp_df if imp_df is not None else pd.DataFrame()
    imp_guia_declarada = bool(
        not _decl.empty and "codi_imp" in _decl.columns
        and not impostos_exibidos(
            _decl[_decl["competencia"].isin(periods)]).empty)
    # IRPJ + CSLL RECOLHIDOS (codi_imp 7 e 6), pra linha "IRPJ/CSLL do periodo".
    irpj_guia_pm = irpj_guia_por_mes(imp_df, periods)

    # Fallback contabil, com o PORTAO ja aplicado pelo helper
    # (`irpj_contabil_pm` e fonte unica dele; a sparkline do PDF e o segundo
    # consumidor). `bal_ir_df` cobre a janela ALARGADA em +3 meses; sem ele
    # degrada pro `bal_serie_df` (janela crua) — o numero fica MENOR, nunca
    # errado de outra forma.
    irpj_contabil = irpj_contabil_pm(
        imp_df, bal_ir_df if bal_ir_df is not None else bal_serie_df, periods)
    if irpj_contabil:
        # REMONTA a tabela com a linha extra em vez de somar por fora: o
        # numerador do card TEM de ser o TOTAL da tabela exibida, senao os
        # dois voltam a divergir com o mesmo nome.
        _tab_imp = tabela_impostos(
            imp_df if imp_df is not None else pd.DataFrame(),
            impd_df, periods, None,
            linha_extra=irpj_contabil)
        imp_pm = _tab_imp["tabela"].loc[LINHA_TOTAL_IMPOSTOS].astype("float64")
        imp_so_retencao = bool(_tab_imp["so_retencao"])

    # Supressao data-driven: SEM movimento em contas de DESPESA (clas 4
    # excl provisao 40106) -> lucratividade nao calculavel (empresa so-receita
    # daria ~100% falso). Olha a DESPESA da DRE. NaN (sem dre_df / 1o deploy) -> sum 0 -> flag True ("—").
    luc_sem_despesas = float(despesas_dre_pm.abs().sum()) == 0
    if luc_sem_despesas:
        lair_pm = pd.Series(float("nan"), index=periods)
    # Guard de cobertura de receita: ver LUC_COBERTURA_RECEITA_MIN.
    # Ortogonal ao trigger de despesas (que tem precedencia e o rotulo proprio);
    # fat_pm == 0 nao dispara (sem NF nao ha base de comparacao).
    fat_total = float(fat_pm.sum())
    rec_total = float(receita_contabil_pm.sum())     # skipna: all-NaN -> 0.0
    luc_receita_incompleta = (
        not luc_sem_despesas
        and fat_total > 0
        and rec_total < LUC_COBERTURA_RECEITA_MIN * fat_total
    )
    if luc_receita_incompleta:
        lair_pm = pd.Series(float("nan"), index=periods)

    # O MESMO piso de materialidade, agora PONTO A PONTO: sem ele o card
    # agregado se calaria mas o chart mensal da MESMA pagina seguiria
    # desenhando barras absurdas.
    # Ponto com receita contabil do MES abaixo do piso vira NaN — NUNCA zero:
    # barra zero AFIRMARIA resultado nulo, que e outra mentira. Perda REAL
    # sobre receita material FICA (e informacao, nao absurdo) — e esse
    # contraste que define o criterio.
    # Fonte unica: os consumers (charts do app e do PDF, tabela) so leem a
    # serie ja mascarada.
    _pct_luc_pm = safe_div(lair_pm, receita_contabil_pm) * 100
    _rec_imaterial = receita_contabil_pm.fillna(0.0) < piso_denominador(1)
    _pct_luc_pm = _pct_luc_pm.mask(_rec_imaterial)

    ac  = bal_grupo_serie(bal_serie_df, periods, GRUPOS_BAL["Ativo Circulante"])
    est = bal_grupo_serie(bal_serie_df, periods, GRUPOS_BAL["Estoque"])
    pc  = bal_grupo_serie(bal_serie_df, periods, GRUPOS_BAL["Passivo Circulante"])
    elp = bal_grupo_serie(bal_serie_df, periods, GRUPOS_BAL["Exigível LP"])
    pl  = bal_grupo_serie(bal_serie_df, periods, GRUPOS_BAL["Patrimônio Líquido"])
    at  = bal_grupo_serie(bal_serie_df, periods, GRUPOS_BAL["Ativo Total"])

    # "Balancete SO FORA da janela": bal_serie_df JA vem seedado (carry-in
    # pre-janela), entao "ter linha" no bal nao prova nada — o ffill
    # do bal_grupo_serie carrega o saldo do seed pra DENTRO de `periods` sem
    # que nenhuma conta tenha se movido na janela. Se NENHUMA linha de conta
    # de BALANCO (clas 1/2) cai dentro de `periods`, o numero que
    # ac/est/pc/elp/pl/at exibiriam seria 100% herdado de fora do periodo.
    # Regra: "zero afirma, ausencia declara" -> vira NaN, nunca o saldo
    # emprestado. liq_c/liq_s/end_p (abaixo) herdam o NaN via safe_div.
    _tem_cols_bal = ({"clas_cta", "competencia"} <= set(bal_serie_df.columns)
                     if not bal_serie_df.empty else False)
    if _tem_cols_bal:
        _clas_bal = bal_serie_df["clas_cta"].astype(str)
        _mask_bal = _clas_bal.str.startswith("1") | _clas_bal.str.startswith("2")
        _comp_bal = bal_serie_df.loc[_mask_bal, "competencia"]
        bal_tem_dado_periodo = bool(_comp_bal.isin(periods).any())
    else:
        _comp_bal = pd.Series(dtype=object)
        bal_tem_dado_periodo = False
    if not bal_tem_dado_periodo:
        _bal_ausente = pd.Series(float("nan"), index=periods)
        ac, est, pc, elp, pl, at = (_bal_ausente, _bal_ausente.copy(),
                                    _bal_ausente.copy(), _bal_ausente.copy(),
                                    _bal_ausente.copy(), _bal_ausente.copy())

    # "Empresa sem movimento no periodo": DIFERENTE do caso acima. Aqui HA
    # linha real dentro de `periods` (bal_tem_dado_periodo True),
    # mas nao no ULTIMO mes da janela — os cards leem via last_valid/ffill, e
    # a caption "Valores do balancete em {ultimo mes}" nomearia um mes que
    # nao teve movimento algum. "So exibicao": ac/est/pc/elp/pl/at NAO mudam
    # (o saldo patrimonial e cumulativo; apagar ou mascarar seria errado) —
    # so a caption ganha a declaracao de que o ultimo mes ficou parado.
    bal_ultimo_periodo_sem_movimento = bool(
        bal_tem_dado_periodo and periods
        and not (_comp_bal == periods[-1]).any())

    # Paridade com a planilha de referencia: caixa separado de bancos (chart proprio) e
    # "Despesas e Custos" = 401-405 (SEM IRPJ/CSLL do 406 — que fica na
    # lucratividade via despesas_pm, espelho do GASTOS E TRIBUTOS).
    caixa_pm = bal_grupo_serie(bal_serie_df, periods, GRUPOS_BAL["Caixa"])
    desp_custos_pm = bal_grupo_serie(bal_serie_df, periods,
                                     GRUPOS_BAL["Despesas e Custos"])

    is_partial_pm = (func_df.groupby("competencia")["is_partial"].any().reindex(periods, fill_value=False)
                     if not func_df.empty and "is_partial" in func_df.columns
                     else pd.Series(False, index=periods))

    # Movimentacao de pessoal: fonte unica pros dois consumers (app.py e
    # pdf_generator.py), sem risco de drift. As series saem CHEIAS no
    # range pedido; quem consome CARD e quem decide a janela efetiva.
    def _func_pm(col):
        if func_df.empty or col not in func_df.columns:
            return pd.Series(0, index=periods)
        return func_df.groupby("competencia")[col].sum().reindex(periods, fill_value=0)

    adm_pm = _func_pm("qtd_admissoes")
    dem_pm = _func_pm("qtd_rescisoes")
    atv_pm = _func_pm("qtd_final")
    ini_pm = _func_pm("qtd_inicial")

    # ── Cauda aberta: mes do fim do range que ainda nao FECHOU. O PDF costuma
    # ser gerado no inicio do mes, com o range terminando em mes corrente/em
    # fechamento — sem o corte, os cards leriam ausencia de dado como queda.
    # Mes ABERTO = is_partial (folha em fechamento) OU sem linha em NENHUM
    # mart-ANCORA do mes. Sem flag nova: usa o que ja existe.
    # Apara-se so o SUFIXO — buraco no meio do range NAO reancora o hero.
    #
    # ANCORAS = funcionarios / faturamento / destacados. Ficam FORA:
    #   - balancete: e ffillado/seedado, "ter linha" nao prova nada;
    #   - folha/encargos/dre: a folha pode ter tp AVULSO num mes que nao
    #     fechou (ex.: SO ferias adiantadas, tp=60, sem folha mensal,
    #     faturamento nem linha de funcionarios). Ancorar em "qualquer folha"
    #     daria o mes como fechado.
    com_dado = set()
    for _df in (func_df, fat_df, impd_df):
        if _df is not None and not _df.empty and "competencia" in _df.columns:
            com_dado |= set(_df["competencia"].astype(str))

    def _mes_fechado(pp):
        if bool(is_partial_pm.get(pp, False)):
            return False
        return pp in com_dado

    periods_efetivos = list(periods)
    while periods_efetivos and not _mes_fechado(periods_efetivos[-1]):
        periods_efetivos.pop()
    cauda_aberta = list(periods[len(periods_efetivos):])
    # Rotulo SO quando houve corte — range fechado nao ganha ruido. Gerado
    # aqui (verbatim pros 2 consumers, mesmo padrao do label do YoY).
    label_corte = (f"dados até {p_label(periods_efetivos[-1])}"
                   if cauda_aberta and periods_efetivos else None)

    return {
        "periods_efetivos": periods_efetivos, "cauda_aberta": cauda_aberta,
        "label_corte": label_corte,
        "fat_pm": fat_pm, "imp_pm": imp_pm, "rec_imp_pm": rec_imp_pm, "folha_pm": folha_pm,
        "sf_pm": sf_pm,
        "enc_pm": enc_pm,
        "imp_destacados_pm": imp_destacados_pm, "provisao_ir_pm": provisao_ir_pm,
        "imp_so_retencao": imp_so_retencao,
        "irpj_contabil": irpj_contabil,
        "imp_guia_declarada": imp_guia_declarada,
        "irpj_guia_pm": irpj_guia_pm,
        "despesas_pm": despesas_pm, "lair_pm": lair_pm,
        "receita_contabil_pm": receita_contabil_pm,
        "caixa_pm": caixa_pm, "desp_custos_pm": desp_custos_pm,
        "ac": ac, "est": est, "pc": pc, "elp": elp, "pl": pl, "at": at,
        "bal_tem_dado_periodo": bal_tem_dado_periodo,
        "bal_ultimo_periodo_sem_movimento": bal_ultimo_periodo_sem_movimento,
        "liq_c": safe_div(ac, pc), "liq_s": safe_div(ac - est, pc),
        "end_p": safe_div(pc + elp, at) * 100,
        # Coerência do card: % Impostos divide pelo fat_pm — o faturamento
        # QUE O CARD EXIBE (NF), mesma base do % Folha/% Folha+Enc.
        # base_receita (rec_imp_pm) pra R/P é receita CONTÁBIL e faria o par
        # R$/% do card não fechar na calculadora do leitor. rec_imp_pm segue
        # exposta pra inspeção; o mart segue gravando base_receita (fonte
        # 'sem_deducao' etc.).
        "pct_imp_pm": safe_div(imp_pm, fat_pm) * 100,
        "pct_fol_pm": safe_div(folha_pm, fat_pm) * 100,
        "pct_folenc_pm": safe_div(folha_pm + enc_pm.fillna(0), fat_pm) * 100,
        "pct_luc_pm": _pct_luc_pm,
        "adm_pm": adm_pm, "dem_pm": dem_pm, "atv_pm": atv_pm, "ini_pm": ini_pm,
        "is_partial_pm": is_partial_pm,
        "luc_sem_despesas": luc_sem_despesas,
        "luc_receita_incompleta": luc_receita_incompleta,
    }


def kpis_agregados(s):
    """KPIs agregados via SUM/SUM (média ponderada).

    NUNCA clean_mean da série de pct (distorce quando num/den não são
    proporcionais — ex: matriz 21% + filiais 0%). `s` é o dict de series_mensais.

    Agrega sobre a JANELA EFETIVA (`periods_efetivos`): a cauda de meses
    ainda não fechados fica fora dos NÚMEROS de card — as séries seguem
    cheias pros charts. Dict sem a chave (fixtures mínimas/consumers antigos)
    agrega o range inteiro.
    """
    per = s.get("periods_efetivos")
    if per is not None and len(per) != len(s["fat_pm"]):
        s = {k: (v.reindex(per) if isinstance(v, pd.Series) else v)
             for k, v in s.items()}
    # imp_pm JÁ inclui o IRPJ/CSLL — o R$ do subtítulo e o % dividem o MESMO
    # numerador, então o par segue fechando.
    total_imp = s["imp_pm"].sum(min_count=1)
    # Coerência do card: denominador = fat_pm EXIBIDO (ver
    # comentário em series_mensais). Invariante consumido pelos 2 consumers:
    # pct_imp_med == imp_total / fat_total — o par R$/% fecha na calculadora.
    # Janela EFETIVA vazia (todo o range em aberto) != faturamento zero: o
    # card tem que dizer "—", nunca "R$ 0,00" — zero afirmaria que a empresa
    # nao vendeu. Mes FECHADO sem venda continua zero REAL.
    fat_total = float(s["fat_pm"].sum()) if len(s["fat_pm"]) else float("nan")
    # Piso de materialidade: denominador POSITIVO porem imaterial suprime a razao.
    # A flag distingue "imaterial" de "zero/ausente" — o rotulo do consumer
    # muda, senao mentiria ("sem faturamento" numa empresa que faturou pouco).
    _piso = piso_denominador(len(s["fat_pm"]))
    fat_den_insuf = bool(pd.notna(fat_total) and 0 < fat_total < _piso)
    fat_ok = bool(pd.notna(fat_total) and fat_total >= _piso)
    pct_imp_med = (total_imp / fat_total * 100) if (pd.notna(total_imp) and fat_ok) else float("nan")
    # imposto > 0 sem NENHUM fat NF (contabilidade na matriz): % incalculável
    # ("—") — flag pro label curto explicando, nunca "—" mudo.
    imp_sem_fat = bool(pd.notna(total_imp) and total_imp > 0 and fat_total <= 0)
    # Numerador AUSENTE (nenhum mes da janela efetiva com dado) != numerador
    # ZERO. So no primeiro o card troca o "R$ 0,00" pela FRASE_IMP_SEM_INFO.
    # Fallback CONSERVADOR: dict minimo sem a serie (teste/consumer antigo)
    # mantem o comportamento padrao, porque chave ausente nunca pode inverter
    # o que a tela diz.
    # Com o numerador na GUIA, o discriminador vem do proprio
    # `tabela_impostos`. Duas ausencias DIFERENTES, duas frases:
    #   so_retencao -> "tem guias, mas todas de imposto de TERCEIRO"
    #   sem dado    -> FRASE_IMP_SEM_INFO
    # Em nenhum dos dois o card exibe R$ 0,00, que ali seria FALSO.
    imp_so_retencao = bool(s.get("imp_so_retencao", False))
    # Fallback CONSERVADOR (chave ausente = declarada): dict minimo de teste ou
    # consumer antigo mantem o comportamento historico, porque chave que falta
    # nunca pode INVERTER o que a tela diz.
    imp_declarada = bool(s.get("imp_guia_declarada", True))
    imp_sem_dado = bool(pd.isna(total_imp) and not imp_so_retencao
                        and not imp_declarada)
    # enc_pm pode faltar em dicts minimos de teste/consumers antigos —
    # fallback all-NaN (equivale a "sem guia nenhuma").
    enc_pm = s.get("enc_pm")
    if enc_pm is None:
        enc_pm = pd.Series(float("nan"), index=s["folha_pm"].index)
    # Denominador da lucratividade = receita CONTABIL (dre_mensal), NAO
    # fat_NF. Dicts minimos (testes/consumers antigos) sem a chave -> NaN (card "—").
    rec_cont = s.get("receita_contabil_pm")
    if rec_cont is None:
        rec_cont = pd.Series(float("nan"), index=s["fat_pm"].index)
    # O R$ do IRPJ/CSLL do periodo e LINHA do card % Impostos — calculado
    # AQUI, fonte unica pros 2 consumers. Sem dado -> 0.0 e os consumers
    # OMITEM a linha.
    # A linha "IRPJ/CSLL do periodo" soma a GUIA (codi_imp 6 e 7), nao a
    # provisao — coerente com o numerador. Ela reflete a fonte ATIVA: guia
    # quando ha, contabilidade quando o fallback entrou.
    prov_ir = s.get("irpj_guia_pm")
    if prov_ir is None or float(pd.Series(prov_ir).fillna(0).abs().sum()) < 0.005:
        _fb = s.get("irpj_contabil") or {}
        if _fb:
            prov_ir = pd.concat([pd.Series(v) for v in _fb.values()],
                                axis=1).sum(axis=1, min_count=1)
    if prov_ir is None:
        prov_ir = pd.Series(float("nan"), index=s["fat_pm"].index)
    # Card de Funcionários: estoque PONTUAL do último mês fechado (e o
    # inicial do primeiro) — None quando não sobrou mês, pro consumer cair
    # no label de no-data em vez de exibir 0.
    # `func_inicial` e o ATIVOS DO PRIMEIRO MES (mesma medida do final), NAO
    # o qtd_inicial: o contexto compara fim-de-mes com fim-de-mes, e trocar a
    # medida mudaria o numero em range FECHADO. O corte mexe em QUAIS meses entram, nunca na medida.
    atv = s.get("atv_pm")
    func_final = (int(atv.iloc[-1]) if atv is not None and len(atv) else None)
    func_inicial = (int(atv.iloc[0]) if atv is not None and len(atv) else None)
    fat_media = float(s["fat_pm"].mean()) if len(s["fat_pm"]) else float("nan")
    # A Lucratividade divide por RECEITA CONTABIL, entao tem
    # piso e rotulo proprios ("receita", nao "faturamento").
    _rec_total = rec_cont.sum(min_count=1)
    luc_den_insuf = bool(pd.notna(_rec_total) and 0 < float(_rec_total) < _piso)
    _folenc = weighted_pct(s["folha_pm"] + enc_pm.fillna(0), s["fat_pm"])
    _luc = weighted_pct(s["lair_pm"], rec_cont)
    return {
        "fat_total": fat_total,
        "fat_media": fat_media,
        "func_final": func_final,
        "func_inicial": func_inicial,
        "pct_imp_med": pct_imp_med,
        "imp_total": float(total_imp),
        "imp_sem_fat": imp_sem_fat,
        "imp_sem_dado": imp_sem_dado,
        "irpj_total": float(prov_ir.sum()),
        "pct_fol_med": weighted_pct(s["folha_pm"], s["fat_pm"]),
        "enc_total": float(enc_pm.sum(min_count=1)),
        "pct_folenc_med": float("nan") if fat_den_insuf else _folenc,
        "pct_luc_med": float("nan") if luc_den_insuf else _luc,
        # Motivo da supressao, pro consumer escolher o substantivo certo
        # ("faturamento" x "receita") em vez de um "—" mudo.
        "fat_den_insuf": fat_den_insuf,
        "luc_den_insuf": luc_den_insuf,
        "piso_denominador": _piso,
    }


def kpis_yoy(s_cur, s_prev):
    """Comparativo YoY dos KPIs de card — mesma janela deslocada -12m.

    O delta dos cards e "vs mesmo periodo do ano anterior", nunca "vs inicio"
    (ultimo vs primeiro mes — confuso e inflado).

    `s_cur`/`s_prev` = dicts de series_mensais das duas janelas ([inicio, fim]
    e [inicio-12m, fim-12m] — pareamento POSICIONAL por construcao; consumers
    carregam a 2a janela dos MESMOS marts). Agregacao IDENTICA a dos cards:
    kpis_agregados nos dois lados (SUM/SUM ponderado) — zero calculo
    novo, zero mascara por valor (a ponderacao mes-com-dado ja vive no
    NaN/min_count das series; pro fat, zero e dado real — atual zerado =
    queda -100%, nunca aparado).

    Invariante por card: delta XOR label ("sem dados de {ref}"), ou ambos
    None quando o PROPRIO atual nao tem o KPI (card "—" nao ganha ruido).
    fat = delta RELATIVO em % (prev <= 0 -> label, nunca inf); imp/folenc/
    luc = delta ABSOLUTO em p.p. `prev` = agregado do ano anterior
    (auditoria; insumo de um sufixo "(N meses)"). Consumers formatam a
    APRESENTACAO (setas, cores, frase "vs mesmo periodo/{ref}"); o numero e
    a decisao delta-vs-label saem DAQUI (zero calculo duplicado).
    """
    # A janela prev acompanha o corte da atual: se a atual foi
    # aparada pra 3 meses, compara com os 3 PRIMEIROS meses do ano anterior —
    # senao o delta mentiria (3 meses de dado contra 5). O pareamento ja e
    # posicional por construcao, entao aparar o prefixo basta.
    n_efetivo = len(s_cur.get("periods_efetivos", s_cur["fat_pm"].index))
    if 0 < n_efetivo < len(s_prev["fat_pm"]):
        prev_per = list(s_prev["fat_pm"].index[:n_efetivo])
        s_prev = {k: (v.reindex(prev_per) if isinstance(v, pd.Series) else v)
                  for k, v in s_prev.items()}
        s_prev["periods_efetivos"] = prev_per

    k_cur, k_prev = kpis_agregados(s_cur), kpis_agregados(s_prev)
    idx = s_prev["fat_pm"].index
    y1, y2 = str(idx[0])[:4], str(idx[-1])[:4]
    ref = y1 if y1 == y2 else f"{y1}-{y2[2:]}"
    sem_dados = f"sem dados de {ref}"

    def _pp(key):
        cur, prev = k_cur[key], k_prev[key]
        if pd.isna(cur):
            return {"delta": None, "prev": None, "label": None}
        if pd.isna(prev):
            return {"delta": None, "prev": None, "label": sem_dados}
        return {"delta": float(cur - prev), "prev": float(prev), "label": None}

    fat_cur, fat_prev = k_cur["fat_total"], k_prev["fat_total"]
    if pd.isna(fat_cur):
        # atual sem KPI: nem delta nem label (mesma regra dos outros cards —
        # card "—" nao ganha ruido; sem isso a janela toda aberta virava
        # "-100%" contra o ano passado).
        fat = {"delta": None, "prev": None, "label": None}
    elif pd.isna(fat_prev) or fat_prev <= 0:
        fat = {"delta": None, "prev": None, "label": sem_dados}
    elif fat_prev < piso_denominador(len(s_prev["fat_pm"])):
        # Base POSITIVA porem imaterial: o guard `<= 0` acima e de sinal, nao
        # de grandeza. Rotulo PROPRIO: "sem dados" mentiria — a base existe,
        # so nao sustenta comparacao.
        fat = {"delta": None, "prev": float(fat_prev),
               "label": f"sem base comparável em {ref}"}
    else:
        fat = {"delta": (fat_cur - fat_prev) / fat_prev * 100,
               "prev": float(fat_prev), "label": None}

    return {"ref": ref, "fat": fat, "imp": _pp("pct_imp_med"),
            "folenc": _pp("pct_folenc_med"), "luc": _pp("pct_luc_med")}


def flags_inconsistencia(ac, pc, pl, at):
    """(liq_inconsistente, endiv_inconsistente) com guard pd.isna.

    Liquidez: PC<=0 ou AC<0 invalidam LC/LS (negativo/negativo = positivo
    mascara o problema). Endividamento: PL<=0 (se EXTRAÍDO) ou AT<=0.
    PL ausente (Simples Nacional sem conta 204) é distinguido de PL
    negativo real via (pl.fillna(0) != 0).any() — ausente NÃO é insolvência.
    """
    pc_last, ac_last = last_valid(pc), last_valid(ac)
    liq = (not pd.isna(pc_last) and pc_last <= 0) or (not pd.isna(ac_last) and ac_last < 0)
    pl_last, at_last = last_valid(pl), last_valid(at)
    pl_extraido = bool((pl.fillna(0) != 0).any())
    endiv = ((pl_extraido and not pd.isna(pl_last) and pl_last <= 0)
             or (not pd.isna(at_last) and at_last <= 0))
    return liq, endiv
