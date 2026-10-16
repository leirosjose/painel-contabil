# ---------------------------------------------------------------------------
# transform.py — Transformações e geração dos marts analíticos
# ---------------------------------------------------------------------------
# Marts produzidos:
#   mart.empresas
#   mart.faturamento_mensal
#   mart.impostos_mensais
#   mart.folha_mensal
#   mart.funcionarios_mensal
#   mart.balancete_mensal
#   mart.empresa_regime_periodo
#   mart.impostos_destacados_mensais
# ---------------------------------------------------------------------------

import numpy as np
import pandas as pd

from mappings import (
    CFOPS_DEVOLUCAO_VENDA,
    CFOPS_RECEITA_NFE,
    CFOPS_RECEITA_NFE_EMISSAO,
    CODI_IMP_RETENCAO_TERCEIROS,
    DEDUCAO_NOME_POR_IMPOSTO,
    ENTREGA_FUTURA_VIGENCIA,
    ORIGENS_IMOBILIARIO_CONHECIDAS,
    ORIGENS_RECEBIMENTO_IMOBILIARIO,
    CLAS_DEDUCAO_EXATA,
    CODI_IMP_ISS,
    GUIA_IMPORTAVEL_DESTACADOS,
    PREFIXOS_DEDUCAO,
    PREFIXOS_RECEITA,
    RE_IMPOSTOS_DESTACADOS,
)


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

# Sinal do saldo por natureza da conta (1o digito de clas_cta):
#   1=Ativo, 4=Despesa      -> natural devedor (deb - cre) -> +1
#   2=Passivo/PL, 3=Receita -> natural credor (cre - deb)  -> -1
SINAL_NATUREZA = {"1": 1, "2": -1, "3": -1, "4": 1}

# FOBASES.tipo_process
TIPO_PROCESS_FOLHA_MENSAL = 11   # folha mensal (headcount e fluxo)
# O fluxo se ancora na PRESENCA da folha tp=11; forescisoes entra como
# refinamento: demitido tipo=1 sai dos ATIVOS no mes do EVENTO.
TIPO_FORESCISAO_COMUM = 1        # forescisoes.tipo (1=comum, 2=complementar)

# Tipos de processamento que compoem o ENCARGO da GUIA.
# Assimetria PROPOSITAL entre os dois, ditada pelo fato gerador:
#   INSS  o patronal do 13o e devido UMA vez, no fechamento, sobre o total —
#         a origem nao tem tp51 de INSS (so 11/42/52).
#   FGTS  e devido a CADA pagamento, entao as duas parcelas do 13o tem guia
#         propria. Somar 51+52 NAO duplica: a soma da 8,00% do 13o bruto.
# NAO confundir com a FOLHA, onde o tp52 traz o integral e estorna a 1a parcela
# por dentro (la a linha do 13o usa tp52 SOZINHO).
TIPOS_PROCESS_GUIA_INSS = frozenset({11, 52})
TIPOS_PROCESS_GUIA_FGTS = frozenset({11, 51, 52})

# ctlancto.orig_lan
ORIG_LAN_ENCERRAMENTO = 2        # lancamento de encerramento de exercicio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_competencia(series: pd.Series) -> pd.Series:
    """Converte coluna de data para string YYYY-MM."""
    return pd.to_datetime(series, errors="coerce").dt.to_period("M").astype(str)


def _require_cols(df: pd.DataFrame, cols: list[str], context: str) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"[{context}] Colunas ausentes no DataFrame: {missing}")


def _mask_prefixo(series: pd.Series, prefixos) -> pd.Series:
    """Mascara booleana: True onde `series` (string) comeca por QUALQUER prefixo
    em `prefixos` (OR logico). Mesmo index de `series`; tudo False quando
    `prefixos` e vazio."""
    mask = pd.Series(False, index=series.index)
    for p in prefixos:
        mask = mask | series.str.startswith(p)
    return mask


def _mask_deducao(clas: pd.Series) -> pd.Series:
    """Mascara das contas de DEDUCAO tributaria no balancete.

    Uniao de duas regras: o prefixo classico (`PREFIXOS_DEDUCAO` = '3010201')
    e as contas EXATAS da arvore alternativa (`CLAS_DEDUCAO_EXATA`), onde
    alguns planos de contas penduram a deducao de ISS. Conta exata e nao
    prefixo — o motivo esta no comentario de `CLAS_DEDUCAO_EXATA` em mappings.

    Superset estrito do idioma antigo: mesmo index, e tudo que
    `_mask_prefixo(clas, PREFIXOS_DEDUCAO)` marcava continua marcado.

    NAO e usado por `build_empresa_regime_periodo`: aquele call site nao tem
    filtro de nome e as contas exatas sao usadas pra RECEITA em outras
    empresas da carteira.
    """
    return _mask_prefixo(clas, PREFIXOS_DEDUCAO) | clas.isin(CLAS_DEDUCAO_EXATA)


def _norm_chaves_bal(df: pd.DataFrame) -> pd.DataFrame:
    """Coage as chaves de join do balancete IN-PLACE (quando presentes):
    codi_emp/codi_cta->int64, clas_cta->str. Devolve o mesmo df (mutado)."""
    df["codi_emp"] = df["codi_emp"].astype("int64")
    if "codi_cta" in df.columns:
        df["codi_cta"] = pd.to_numeric(df["codi_cta"]).astype("int64")
    if "clas_cta" in df.columns:
        df["clas_cta"] = df["clas_cta"].astype(str)
    return df


# ---------------------------------------------------------------------------
# mart.faturamento_mensal
# ---------------------------------------------------------------------------

def build_faturamento_mensal(
    raw_saidas: pd.DataFrame,
    raw_entradas: pd.DataFrame,
    raw_servicos: pd.DataFrame,
    raw_imob: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Agrega NFs de saída, entrada e serviço em faturamento_mensal por empresa/mês.

    Colunas produzidas:
        codi_emp, competencia,
        valo_saidas, qtd_nfs_saida,
        valo_entradas, qtd_nfs_entrada,
        valo_devolucoes, qtd_nfs_devolucao,
        valo_servicos, qtd_nfs_servicos,
        valo_imobiliario,
        valo_liquido (= NFe BRUTO - valo_entradas; ver SEMÂNTICA abaixo)

    SEMÂNTICA — leitura obrigatória antes de consumir este mart:

      • valo_saidas é o faturamento de NFe/NFCe LÍQUIDO de devolução de
        venda: bruto de receita MENOS `valo_devolucoes`, com a devolução
        abatida no MÊS DA ENTRADA da nota de devolução (`dent_ent`) — não no
        mês da venda — e SEM PISO: mês com mais devolução que venda fica
        NEGATIVO (é o que o Relatório de Faturamento do Domínio mostra). O
        par (empresa, mês da entrada) com devolução e sem saída ganha linha
        (o merge é outer).
        O bruto é só das notas cujo `codi_nat` (CFOP) consta em
        `mappings.CFOPS_RECEITA_NFE`. Transferência entre filiais, remessa
        (comodato, industrialização, bonificação), devolução de compra,
        venda de ativo imobilizado e baixa de estoque NÃO entram — são
        saídas fiscais que não são receita. Sem a coluna no raw, o build
        degrada pra soma de todas as notas (comportamento pré-filtro).
        NÃO inclui NFS-e — pra empresas prestadoras de serviço, somar com
        valo_servicos pra ter o faturamento total.
        Stateless por mês (célula = função só do mês, sem pareamento entre
        meses): o run janelado e o --skip-extract dão o mesmo número.

        ENTREGA FUTURA: pras empresas de
        `mappings.ENTREGA_FUTURA_VIGENCIA`, a partir da competência de
        vigência DAQUELA empresa, a receita é reconhecida no SIMPLES
        FATURAMENTO (5922/6922) e a entrega (5116/5117/6116/6117) deixa de
        somar — lista `CFOPS_RECEITA_NFE_EMISSAO`. Antes da vigência, e pra
        empresa fora do dict, vale a regra antiga (conta a entrega). É
        decisão de reconhecimento, não de cálculo: o mesmo dinheiro muda de
        mês. Rationale no bloco ENTREGA FUTURA de mappings.py.

      • valo_servicos é o BRUTO faturado de NFS-e (efservicos). Tabela
        separada porque NFS-e não vai pra efsaidas no Domínio. Disjunto
        de valo_saidas (NFS-e e NFe são documentos diferentes).
        Faturamento total = valo_saidas (líquido) + valo_servicos +
        valo_imobiliario.

      • valo_entradas é o TOTAL de NFs em efentradas — mistura compras de
        mercadoria, devoluções de venda, aquisição de serviço, compra de
        imobilizado, transferências entre filiais, etc. Não use para
        análise de "compras" sem antes desagregar por codi_nat (CFOP).

      • valo_devolucoes é um SUBSET de valo_entradas — NÃO é disjunto.
        Soma das entradas cujo `codi_nat` (CFOP) consta em
        `mappings.CFOPS_DEVOLUCAO_VENDA` — a MESMA lista que abate o
        valo_saidas (1201-1204, 1410-1411, 1660-1662 e pares interestaduais
        22xx/24xx/266x). Devolução de TRANSFERÊNCIA (1208/1209/2208/2209),
        de ativo (2553) e anulação de transporte (1206/2206) ficam FORA:
        não abatem e não somam aqui. Devoluções de NFS-e ficam em efservicos
        com CFOPs próprios (9xxx) — não cobertas hoje.

      • valo_liquido (legacy) = BRUTO de NFe - valo_entradas, isto é,
        (valo_saidas + valo_devolucoes) - valo_entradas. Derivado do BRUTO
        de propósito: a devolução já está dentro de `valo_entradas`, então
        derivar do valo_saidas líquido a subtrairia duas vezes. Mantido por
        compatibilidade; como `valo_entradas` mistura compras com
        devoluções, é proxy operacional sem semântica clara, e nenhum
        consumer ativo (app.py, pdf_generator.py) o utiliza.

      • REGRA CRÍTICA: NUNCA fazer
            valo_saidas - valo_devolucoes
        nem `valo_saidas - valo_entradas - valo_devolucoes`: o valo_saidas
        JÁ é líquido da devolução (subtrair de novo dupla-conta), e a
        devolução já está dentro de `valo_entradas`. Faturamento NFe líquido
        = `valo_saidas`; faturamento total = `valo_saidas + valo_servicos +
        valo_imobiliario`.
    """
    _require_cols(raw_saidas,   ["codi_emp", "dsai_sai", "vcon_sai"], "faturamento_mensal/saidas")
    _require_cols(raw_entradas, ["codi_emp", "dent_ent", "vcon_ent"], "faturamento_mensal/entradas")

    s = raw_saidas.copy()
    s["competencia"] = _to_competencia(s["dsai_sai"])
    # Filtro de natureza: só CFOP de receita compõe faturamento. Sem a
    # coluna (raw anterior ao contrato novo) soma tudo — degradar pro
    # comportamento antigo é melhor que zerar faturamento.
    # CFOP nulo não é classificável como receita e fica de fora.
    #
    # A nota não-receita é ZERADA, não descartada: se ela sumisse e a empresa
    # não tivesse entrada nem serviço no mês, o par (empresa, competencia)
    # desapareceria do build — e o DELETE-escopado só alcança pares
    # PRESENTES no DataFrame novo, deixando a linha velha (pré-filtro) no
    # mart pra sempre. Zerando, o par sobrevive, o upsert sobrescreve o
    # valor antigo e o mart cura sozinho na primeira carga.
    if "codi_nat" in s.columns:
        cfop = pd.to_numeric(s["codi_nat"], errors="coerce")
        e_receita = cfop.isin(CFOPS_RECEITA_NFE)
        # Entrega futura reconhecida na EMISSÃO: a partir da vigência DA
        # EMPRESA, o 5922/6922 é a receita e o 5117/6117 sai.
        # Stateless de propósito — pareamento entre meses divergiria entre run
        # janelado e --skip-extract; ver o bloco ENTREGA
        # FUTURA em mappings.py. Empresa fora do dict fica na regra antiga
        # (o sentinela "9999-99" nunca é alcançado por uma competência real).
        vig = s["codi_emp"].map(ENTREGA_FUTURA_VIGENCIA).fillna("9999-99")
        regra_nova = s["competencia"] >= vig
        e_receita = e_receita.where(~regra_nova, cfop.isin(CFOPS_RECEITA_NFE_EMISSAO))
        s["vcon_sai"] = s["vcon_sai"].where(e_receita, 0.0)
    else:
        e_receita = pd.Series(True, index=s.index)
    s["_e_receita"] = e_receita.astype(int)
    saidas = (
        s.groupby(["codi_emp", "competencia"], as_index=False)
        .agg(valo_saidas=("vcon_sai", "sum"), qtd_nfs_saida=("_e_receita", "sum"))
    )

    e = raw_entradas.copy()
    e["competencia"] = _to_competencia(e["dent_ent"])
    entradas = (
        e.groupby(["codi_emp", "competencia"], as_index=False)
        .agg(valo_entradas=("vcon_ent", "sum"), qtd_nfs_entrada=("vcon_ent", "count"))
    )

    # Devoluções de venda — subset de entradas com CFOP de devolução.
    # Subset, NÃO disjunto: já contam em valo_entradas (ver docstring).
    if not e.empty and "codi_nat" in e.columns:
        cfop_int = pd.to_numeric(e["codi_nat"], errors="coerce")
        devol = e[cfop_int.isin(CFOPS_DEVOLUCAO_VENDA)]
        devolucoes = (
            devol.groupby(["codi_emp", "competencia"], as_index=False)
            .agg(valo_devolucoes=("vcon_ent", "sum"),
                 qtd_nfs_devolucao=("vcon_ent", "count"))
        )
    else:
        devolucoes = pd.DataFrame(columns=["codi_emp", "competencia",
                                           "valo_devolucoes", "qtd_nfs_devolucao"])

    # NFS-e (serviços) — disjunto de valo_saidas (tabela separada no Domínio).
    if raw_servicos is not None and not raw_servicos.empty:
        _require_cols(raw_servicos, ["codi_emp", "dser_ser", "vcon_ser"],
                      "faturamento_mensal/servicos")
        sv = raw_servicos.copy()
        sv["competencia"] = _to_competencia(sv["dser_ser"])
        servicos = (
            sv.groupby(["codi_emp", "competencia"], as_index=False)
            .agg(valo_servicos=("vcon_ser", "sum"),
                 qtd_nfs_servicos=("vcon_ser", "count"))
        )
    else:
        servicos = pd.DataFrame(columns=["codi_emp", "competencia",
                                          "valo_servicos", "qtd_nfs_servicos"])

    # 3a ORIGEM — parcelas de venda de IMOVEL.
    # NAO e documento fiscal: nao ha CFOP, nao ha nota, e o eixo do tempo e a
    # DATA DO PAGAMENTO (regime de CAIXA), nao a emissao. Vem do modulo
    # imobiliario do Dominio.
    # Na TELA vira a linha "Outras receitas" (LINHA_OUTRAS_RECEITAS), balde de
    # receita fora de NF — o rotulo e de EXIBICAO e a coluna e que nomeia a
    # fonte, entao uma 2a fonte no balde muda a soma e nao a tabela.
    if raw_imob is not None and not raw_imob.empty:
        _require_cols(raw_imob, ["codi_emp", "data_pagamento", "valor_pago",
                                 "origem_pagamento"], "faturamento_mensal/imobiliario")
        im = raw_imob.copy()
        # `.str.strip()` antes de comparar: dependendo do caminho do driver o
        # char(1) chega PADDED.
        origem = im["origem_pagamento"].astype(str).str.strip()
        desconhecidas = sorted(set(origem.unique()) - set(ORIGENS_IMOBILIARIO_CONHECIDAS))
        if desconhecidas:
            raise ValueError(
                f"[faturamento_mensal] origem_pagamento fora do contrato: "
                f"{desconhecidas}; conhecidos = "
                f"{sorted(ORIGENS_IMOBILIARIO_CONHECIDAS)}. Valor novo na origem "
                f"NAO entra calado no faturamento — investigar antes de somar.")
        im = im[origem.isin(ORIGENS_RECEBIMENTO_IMOBILIARIO)]
        im["competencia"] = _to_competencia(im["data_pagamento"])
        imobiliario = (
            im.groupby(["codi_emp", "competencia"], as_index=False)
            .agg(valo_imobiliario=("valor_pago", "sum"))
        )
    else:
        imobiliario = pd.DataFrame(columns=["codi_emp", "competencia",
                                            "valo_imobiliario"])

    result = saidas.merge(entradas, on=["codi_emp", "competencia"], how="outer")
    result = result.merge(devolucoes, on=["codi_emp", "competencia"], how="outer")
    result = result.merge(servicos, on=["codi_emp", "competencia"], how="outer")
    # outer TAMBEM aqui: empresa com faturamento de NF ZERO so existe no mart
    # por esta perna.
    result = result.merge(imobiliario, on=["codi_emp", "competencia"], how="outer")
    result["valo_saidas"]       = result["valo_saidas"].fillna(0)
    result["valo_entradas"]     = result["valo_entradas"].fillna(0)
    result["valo_devolucoes"]   = result["valo_devolucoes"].fillna(0)
    result["valo_servicos"]     = result["valo_servicos"].fillna(0)
    result["valo_imobiliario"]  = result["valo_imobiliario"].fillna(0)
    result["qtd_nfs_saida"]     = result["qtd_nfs_saida"].fillna(0).astype(int)
    result["qtd_nfs_entrada"]   = result["qtd_nfs_entrada"].fillna(0).astype(int)
    result["qtd_nfs_devolucao"] = result["qtd_nfs_devolucao"].fillna(0).astype(int)
    result["qtd_nfs_servicos"]  = result["qtd_nfs_servicos"].fillna(0).astype(int)
    # valo_liquido (legacy) sai do BRUTO, ANTES do abatimento abaixo: a
    # devolucao ja esta dentro de valo_entradas.
    result["valo_liquido"] = result["valo_saidas"] - result["valo_entradas"]
    # NFe LIQUIDO de devolucao de venda, no mes da ENTRADA (dent_ent), sem
    # piso. UM ponto so: todo consumer de
    # faturamento le esta coluna do mart e herda (chart, card, tabela, Total,
    # hero, PDF, den_fat). Stateless por mes -> run janelado == --skip-extract.
    result["valo_saidas"] = result["valo_saidas"] - result["valo_devolucoes"]

    return result.sort_values(["codi_emp", "competencia"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# mart.impostos_mensais
# ---------------------------------------------------------------------------

def build_impostos_mensais(raw_sdoimp: pd.DataFrame) -> pd.DataFrame:
    """Agrega impostos apurados por empresa/mês/tipo de imposto.

    Colunas produzidas:
        codi_emp, competencia, codi_imp,
        srec_sim, srec_adicional, valo_devido, bcal_sim, qtd_docs

    O grão da origem é a PK (codi_emp, codi_imp, data_sim,
    pdic_sim) — pdic_sim é SLOT de sub-período de apuração (1,2,3 = decêndios
    IRRF; 4,5 = quinzenas CSRF; 6 = mensal). O SUM abaixo soma os slots do
    mês: slots zerados somam R$ 0 e duas guias reais de mesmo valor
    (quinzenas idênticas, Lei 10.833 arts. 30/31) somam como a lei manda.
    qtd_docs = nº de slots do trio (sem consumidor em app/pdf hoje).

    ADICIONAL DE IRPJ — as TRÊS colunas de valor, e por quê:

      • `srec_sim` é o PRINCIPAL, fiel ao raw. Não somamos o adicional aqui
        de propósito: o nome vem da origem e significa "saldo a recolher";
        embutir o adicional faria `mart.srec_sim != raw.srec_sim` sem nada
        declarar.
      • `srec_adicional` é o `inc1_sim` agregado: o adicional de 10% sobre o
        excedente de 60 mil/trimestre, JÁ CALCULADO pela origem (memorial em
        VDI90-93 da efsdoimp).
      • `valo_devido` = principal + adicional. **É o que os consumers leem.**
        Existe pra que ninguém some por conta própria — dois consumers
        somando é como nascem dois universos com o mesmo nome.

    Só os codi_imp 7 (IRPJ) e 111 (SCP-IRPJ) têm adicional; o 111
    já sai da tabela exibida por `CODI_IMP_SCP`, levando o adicional junto.
    Raw anterior ao contrato novo não tem `inc1_sim` -> degrada pra zero e
    `valo_devido` vira o principal (o comportamento de antes), nunca KeyError.
    """
    _require_cols(raw_sdoimp, ["codi_emp", "data_sim", "codi_imp", "srec_sim", "bcal_sim"], "impostos_mensais")

    df = raw_sdoimp.copy()
    df["competencia"] = _to_competencia(df["data_sim"])
    if "inc1_sim" not in df.columns:
        df["inc1_sim"] = 0.0
    df["inc1_sim"] = pd.to_numeric(df["inc1_sim"], errors="coerce").fillna(0.0)

    result = (
        df.groupby(["codi_emp", "competencia", "codi_imp"], as_index=False)
        .agg(
            srec_sim=("srec_sim", "sum"),
            srec_adicional=("inc1_sim", "sum"),
            bcal_sim=("bcal_sim", "sum"),
            qtd_docs=("srec_sim", "count"),
        )
    )
    result["valo_devido"] = result["srec_sim"] + result["srec_adicional"]

    if "desc_imp" in df.columns:
        desc = df[["codi_imp", "desc_imp"]].drop_duplicates("codi_imp")
        result = result.merge(desc, on="codi_imp", how="left")

    return result.sort_values(["codi_emp", "competencia", "codi_imp"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# mart.folha_mensal
# ---------------------------------------------------------------------------

def build_folha_mensal(raw_fobases: pd.DataFrame) -> pd.DataFrame:
    """Agrega FOBASES por empresa/tipo_process/mês.

    Colunas produzidas:
        codi_emp, tipo_process, competencia,
        valo_bruto (sum proventos), valo_descontos (sum descontos),
        valo_liquido, qtd_funcionarios
    """
    _require_cols(raw_fobases, ["codi_emp", "competencia", "tipo_process", "proventos", "descontos"], "folha_mensal")

    df = raw_fobases.copy()
    df["competencia"] = _to_competencia(df["competencia"])

    result = df.groupby(["codi_emp", "tipo_process", "competencia"], as_index=False).agg(
        valo_bruto=("proventos", "sum"),
        valo_descontos=("descontos", "sum"),
        qtd_funcionarios=("i_empregados", "nunique"),
    )
    result["valo_liquido"] = result["valo_bruto"] - result["valo_descontos"]

    return result.sort_values(["codi_emp", "tipo_process", "competencia"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# mart.folha_rubricas_mensais (fundação do dash de Detalhamento)
# ---------------------------------------------------------------------------

_PROV_DESC_CONHECIDOS = ("P", "D", "I", "ID")


def build_folha_rubricas_mensais(raw_fomovto: pd.DataFrame,
                                 raw_foeventos: pd.DataFrame,
                                 raw_foclassificacoes: pd.DataFrame,
                                 raw_foeventosdef: pd.DataFrame | None = None) -> pd.DataFrame:
    """Folha aberta por RUBRICA — grão (codi_emp, competencia, tipo_process, i_eventos).

    Fundação do dash de Detalhamento de Folha. Ancoragem: `SUM(valor_p)`
    deste mart == `folha_mensal.valo_bruto` por (mês × tipo_process) — o
    nível rubrica reconstrói exatamente o nível de processo que a tela já
    exibe.

    **O grão de EMPREGADO não sobe** (LGPD): o dash é por
    empresa, e subir pessoa física pro mart amplia superfície sem ganho de
    tela. Quem precisar do grão fino lê `raw.fomovto`.

    `prov_desc` tem QUATRO valores, não dois — P (provento), D (desconto),
    I (informativo: **o FGTS mora aqui**) e ID (dedução informativa). Cada um vira coluna própria: somar
    sem separar infla o total. Valores desconhecidos LEVANTAM em vez de sumir
    — a projeção final descartaria a coluna nova em silêncio.

    `nome` e `classificacao` vêm desnormalizados do dicionário POR EMPRESA
    (foeventos, PK codi_emp+i_eventos) e `desc_classificacao` do dicionário
    GLOBAL (foclassificacoes_eventos, 64 famílias). **O consumer agrupa por
    `classificacao`, NUNCA por `i_eventos` cru:** o mesmo código de evento
    tem nomes distintos em empresas diferentes.
    Rubrica sem dicionário mantém o dinheiro e perde só o rótulo.

    `qtd_informada` (horas/dias) usa `min_count=1`: tudo-NaN vira NaN, nunca
    0.0 — "0 horas" afirma um fato, ausência apenas declara que não se sabe.
    """
    cols_saida = ["codi_emp", "competencia", "tipo_process", "i_eventos",
                  "nome", "classificacao", "desc_classificacao",
                  "fonte_dicionario",
                  "valor_p", "valor_d", "valor_i", "valor_id", "qtd_informada"]
    if raw_fomovto.empty:
        return pd.DataFrame(columns=cols_saida)
    _require_cols(raw_fomovto,
                  ["codi_emp", "data", "tipo_proces", "i_eventos",
                   "valor_cal", "prov_desc"], "folha_rubricas_mensais")

    df = raw_fomovto.copy()
    df["competencia"] = _to_competencia(df["data"])
    df["tipo_process"] = df["tipo_proces"]
    df["_pd"] = df["prov_desc"].astype(str).str.strip().str.upper()
    df["valor_cal"] = pd.to_numeric(df["valor_cal"], errors="coerce").fillna(0.0)

    # Guard: prov_desc fora do contrato viraria uma coluna que a projecao
    # final descarta — dinheiro sumindo calado. Levanta nomeando o valor e
    # quantas linhas ele tem, pra o diagnostico nao exigir nova query.
    desconhecidos = df.loc[~df["_pd"].isin(_PROV_DESC_CONHECIDOS), "_pd"]
    if not desconhecidos.empty:
        detalhe = ", ".join(f"{v!r} ({n} linhas)"
                            for v, n in desconhecidos.value_counts().items())
        raise ValueError(
            f"[folha_rubricas_mensais] prov_desc fora do contrato "
            f"{_PROV_DESC_CONHECIDOS}: {detalhe}")

    chave = ["codi_emp", "competencia", "tipo_process", "i_eventos"]
    piv = (df.pivot_table(index=chave, columns="_pd", values="valor_cal",
                          aggfunc="sum", fill_value=0.0)
             .reset_index())
    piv.columns.name = None
    for sigla in _PROV_DESC_CONHECIDOS:
        col = f"valor_{sigla.lower()}"
        if sigla in piv.columns:
            piv = piv.rename(columns={sigla: col})
        else:
            piv[col] = 0.0

    qtd_col = (pd.to_numeric(df["valor_inf"], errors="coerce")
               if "valor_inf" in df.columns else pd.Series(float("nan"), index=df.index))
    qtd = (df.assign(_qtd=qtd_col)
             .groupby(chave, as_index=False)["_qtd"]
             .sum(min_count=1)
             .rename(columns={"_qtd": "qtd_informada"}))
    out = piv.merge(qtd, on=chave, how="left")

    if not raw_foeventos.empty:
        eve = raw_foeventos[["codi_emp", "i_eventos", "nome", "classificacao"]]
        out = out.merge(eve, on=["codi_emp", "i_eventos"], how="left")
    else:
        out["nome"] = pd.NA
        out["classificacao"] = pd.NA

    # PRECEDENCIA: a copia da empresa VENCE; o padrao so preenche onde ela nao
    # respondeu. Nunca sobrescreve — o nome diverge por renomeacao cosmetica
    # (uma empresa chama de "SALARIO BASE" o que o padrao chama de "HORAS
    # NORMAIS") e deixar o padrao vencer trocaria rubricas de lado nas regras
    # de custo. O merge e por `i_eventos`
    # SOZINHO porque o dicionario padrao e global (nao tem codi_emp).
    out["fonte_dicionario"] = np.where(out["nome"].notna(), "empresa", "ausente")
    if raw_foeventosdef is not None and not raw_foeventosdef.empty:
        pad = (raw_foeventosdef[["i_eventos", "nome", "classificacao"]]
               .drop_duplicates(subset="i_eventos")
               .rename(columns={"nome": "_nome_pad",
                                "classificacao": "_cls_pad"}))
        out = out.merge(pad, on="i_eventos", how="left")
        cobre = out["nome"].isna() & out["_nome_pad"].notna()
        out.loc[cobre, "fonte_dicionario"] = "padrao"
        out["nome"] = out["nome"].fillna(out["_nome_pad"])
        out["classificacao"] = out["classificacao"].fillna(out["_cls_pad"])
        out = out.drop(columns=["_nome_pad", "_cls_pad"])

    if not raw_foclassificacoes.empty:
        cls = raw_foclassificacoes.rename(
            columns={"i_classificacao": "classificacao",
                     "descricao": "desc_classificacao"})
        out = out.merge(cls, on="classificacao", how="left")
    else:
        out["desc_classificacao"] = pd.NA

    return out[cols_saida].sort_values(chave).reset_index(drop=True)


# ---------------------------------------------------------------------------
# mart.encargos_mensais
# ---------------------------------------------------------------------------

def build_encargos_mensais(raw_foguiainss: pd.DataFrame,
                           raw_fofgtsfilial: pd.DataFrame,
                           raw_foguiagrfc: pd.DataFrame,
                           raw_forescisoes: pd.DataFrame | None = None) -> pd.DataFrame:
    """Encargo MENSAL fiel a GUIA — grao (codi_emp, competencia).

    Fontes FIXAS:
      inss_empresa = SUM(foguiainss.empresa) em TIPOS_PROCESS_GUIA_INSS
                     (mensal + 13o) — INSS patronal 20%; RAT (acid_trab),
                     terceiros e segurados ficam FORA (colunas separadas na
                     guia — e o que resolve a conflacao que refutou o
                     balancete como fonte).
      fgts         = SUM(fofgtsfilial.total_fgts) em TIPOS_PROCESS_GUIA_FGTS
                     (mensal + as DUAS parcelas do 13o) — FGTS depositado
                     (GRF/FGTS Digital).
      fgts_resc    = SUM(foguiagrfc.multa_fgts + mes_ant_valor + resc_valor
                     + aviso_previo_valor) com competencia = YYYY-MM(mes da
                     DEMISSAO): a GRFC nao tem coluna competencia na
                     origem, mas `i_calculos` e chave também em
                     `forescisoes` (RAW_KEYS), então `demissao` dá o mês do
                     fato gerador por JOIN. Guia sem rescisão casada cai no
                     fallback = YYYY-MM(vencimento). `raw_forescisoes`
                     omitido/vazio = 100% fallback.

    `saldo_fins_resc` NAO entra na soma. Ele e a BASE da multa de 40% — o
    saldo ja depositado na conta vinculada do FGTS ao longo dos anos —, nao
    desembolso do mes; a origem separa base de valor em TRES pares (mes_ant,
    resc, aviso_previo) e a base da multa e essa. Somar `multa + saldo`
    contaria o FGTS historico inteiro do empregado como custo do mes da
    rescisao.

    Coluna de valor AUSENTE (raw pre-contrato) -> fgts_resc NULL no mes, nunca
    a formula antiga nem zero: coluna que nao existe e ausencia de dado, e o
    1o run com extract cura. NULL por LINHA conta como zero — a origem manda
    0.00, nao NULL.

    O 13o entra no encargo: o tp52 integra o CUSTO TOTAL ("nao existem dois
    custos totais"), e sem o encargo dele a coluna exibiria 13o bruto e
    desencargado.

    tp=42 (rescisao complementar) EXCLUIDO DELIBERADAMENTE de INSS/FGTS: a
    guia que a planilha de referência copia e a mensal tp=11, e a
    materialidade do 42 e irrelevante. Assimetria CONSCIENTE com a folha
    exibida, que INCLUI o tp=42.

    NULL = sem guia do componente, NUNCA 0 silencioso.
    Simples vem com empresa=0,00 REAL da propria guia — sem gate de regime
    (Anexo IV vem com INSS naturalmente). `situacao` NAO e filtrada
    (materialidade irrelevante).
    """
    cols_out = ["codi_emp", "competencia", "inss_empresa", "fgts", "fgts_resc",
                "sf_compensado"]

    def _agg_tipos(df, val_col, out_col, tipos):
        need = {"codi_emp", "competencia", "tipo_process", val_col}
        if df.empty or not need.issubset(df.columns):
            return pd.DataFrame(columns=["codi_emp", "competencia", out_col])
        d = df[df["tipo_process"].isin(tipos)].copy()
        if d.empty:
            return pd.DataFrame(columns=["codi_emp", "competencia", out_col])
        d["competencia"] = _to_competencia(d["competencia"])
        g = d.groupby(["codi_emp", "competencia"], as_index=False)[val_col].sum()
        return g.rename(columns={val_col: out_col})

    inss = _agg_tipos(raw_foguiainss, "empresa", "inss_empresa",
                      TIPOS_PROCESS_GUIA_INSS)
    fgts = _agg_tipos(raw_fofgtsfilial, "total_fgts", "fgts",
                      TIPOS_PROCESS_GUIA_FGTS)
    # A guia vem BRUTA: `empresa` e o patronal ANTES da compensacao do
    # salario familia (Lei 8.213 art.68), que a origem guarda em coluna
    # SEPARADA: a identidade
    # `valor_inss = empresa + segurados + acid_trab - deducoes` fecha.
    #
    # A compensacao vira COLUNA PROPRIA e NAO abate o `inss_empresa`: o
    # `inss_empresa - SF` ficaria NEGATIVO com frequencia, porque na maioria
    # das empresas com salario familia o patronal e 0,00 (Simples) e a
    # compensacao sai dos SEGURADOS. Quem abate e o consumer, no CUSTO TOTAL —
    # e assim a linha de Encargos continua fiel a guia.
    if not raw_foguiainss.empty and "deducoes_comp_sf" in raw_foguiainss.columns:
        g = raw_foguiainss.copy()
        retro = (g["deducoes_comp_sf_retroativo"].fillna(0)
                 if "deducoes_comp_sf_retroativo" in g.columns else 0.0)
        g["_sf"] = g["deducoes_comp_sf"].fillna(0) + retro
        sf = _agg_tipos(g, "_sf", "sf_compensado", TIPOS_PROCESS_GUIA_INSS)
    else:
        # raw pre-contrato: NULL, nunca 0 — zero afirmaria "nao houve
        # compensacao", que e afirmacao que o dado nao sustenta.
        sf = pd.DataFrame(columns=["codi_emp", "competencia", "sf_compensado"])

    # `saldo_fins_resc` fica FORA do need: ela nao entra na soma, e exigi-la
    # so acoplaria o guard a uma coluna que o desembolso nao usa.
    COLS_VALOR_GRFC = ("multa_fgts", "mes_ant_valor", "resc_valor",
                       "aviso_previo_valor")
    need_grfc = {"codi_emp", "vencimento", *COLS_VALOR_GRFC}
    if raw_foguiagrfc.empty or not need_grfc.issubset(raw_foguiagrfc.columns):
        resc = pd.DataFrame(columns=["codi_emp", "competencia", "fgts_resc"])
    else:
        d = raw_foguiagrfc.copy()
        d["fgts_resc"] = sum(d[c].fillna(0) for c in COLS_VALOR_GRFC)
        # A competencia vira o mes da DEMISSAO, nao do vencimento -- o
        # desembolso pertence ao FATO GERADOR (a rescisao), e a guia so tem
        # prazo de pagamento separado. Join por [codi_emp, i_calculos] (chave
        # natural das duas, RAW_KEYS). Guia sem rescisao casada cai no
        # fallback pro vencimento. Nao filtra forescisoes.tipo: nao ha
        # duplicatas na chave sem filtro.
        tem_forescisoes = (
            raw_forescisoes is not None and not raw_forescisoes.empty
            and {"codi_emp", "i_calculos", "demissao"}.issubset(raw_forescisoes.columns)
            and "i_calculos" in d.columns
        )
        if tem_forescisoes:
            fr = (raw_forescisoes[["codi_emp", "i_calculos", "demissao"]]
                  .drop_duplicates(subset=["codi_emp", "i_calculos"]))
            d = d.merge(fr, on=["codi_emp", "i_calculos"], how="left")
            # _to_competencia termina em .astype(str): data invalida vira a
            # STRING "NaT", nao alcancavel por fallback. Usa pd.to_datetime cru
            # (NaN real) pra decidir o fallback ANTES de formatar.
            dt_demissao = pd.to_datetime(d["demissao"], errors="coerce")
            dt_vencimento = pd.to_datetime(d["vencimento"], errors="coerce")
            dt_final = dt_demissao.where(dt_demissao.notna(), dt_vencimento)
            d["competencia"] = dt_final.dt.to_period("M").astype(str)
        else:
            d["competencia"] = _to_competencia(d["vencimento"])
        resc = d.groupby(["codi_emp", "competencia"], as_index=False)["fgts_resc"].sum()

    out = (inss.merge(fgts, on=["codi_emp", "competencia"], how="outer")
               .merge(resc, on=["codi_emp", "competencia"], how="outer")
               .merge(sf, on=["codi_emp", "competencia"], how="outer"))
    if out.empty:
        return pd.DataFrame(columns=cols_out)
    for col in ("inss_empresa", "fgts", "fgts_resc", "sf_compensado"):
        out[col] = out[col].astype("float64") if col in out.columns else float("nan")
    out["codi_emp"] = out["codi_emp"].astype("int64")
    return out[cols_out].sort_values(["codi_emp", "competencia"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# mart.funcionarios_mensal
# ---------------------------------------------------------------------------

def _flag_partial(qf: pd.Series) -> pd.Series:
    """Flagga meses com folha potencialmente incompleta.

    Critério: qtd_final < 50% da mediana dos 3 meses anteriores E queda
    absoluta > 5 pessoas. O threshold absoluto evita falsos positivos
    em empresas pequenas (ex: 3 → 1 não é flagrado; 172 → 8 é). Requer
    ao menos 2 meses anteriores no histórico — antes disso, False.

    Aplicada sobre final_hist (histórico completo de FOBASES tipo_process=11),
    não sobre o grid filtrado em [inicio, fim], para que ranges curtos
    não percam contexto histórico.
    """
    qf = qf.astype(float)
    median_prev3 = qf.shift(1).rolling(3, min_periods=2).median()
    queda_abs = median_prev3 - qf
    flag = (median_prev3 > 0) & (qf < 0.5 * median_prev3) & (queda_abs > 5)
    return flag.fillna(False)


def build_funcionarios_mensal(
    raw_fobases: pd.DataFrame,
    raw_forescisoes: pd.DataFrame,
    inicio: str,
    fim: str,
) -> pd.DataFrame:
    """Estoque e fluxo mensal de funcionários — convenção HÍBRIDA V1
    (refinamento da convenção B: presença na folha).

    Conjunto-base por (codi_emp, competência):
        pres = i_empregados presentes na folha (FOBASES tipo_process=11)
        atv  = pres − demitidos com rescisão tipo=1 registrada NO PRÓPRIO
               mês (forescisoes; "ativos no fim do mês")
    e os QUATRO números derivam de um ÚNICO pipeline sobre `atv`:
        cur  = atv do mês; prev = atv do mês anterior NO HISTÓRICO (shift
               por empresa — a mesma âncora sempre; nunca calendário cru)
        qtd_final     = len(cur)          ATIVOS no fim do mês
        qtd_inicial   = len(prev)
        qtd_admissoes = len(cur − prev)   ENTRADAS
        qtd_rescisoes = len(prev − cur)   SAÍDAS
    Identidade |cur| = |prev| + |cur−prev| − |prev−cur| ⇒ Inicial +
    Entradas − Saídas == Final FECHA POR CONSTRUÇÃO em toda linha emitida.

    Híbrida V1 (o refinamento sobre B):
    - Demissão tipo=1 registrada conta no mês do EVENTO: o demitido de
      dia 20 sai do Final daquele mês, mesmo recebendo a folha do mês.
    - SEM registro degrada pra convenção B: sai quando some da
      folha. forescisoes vazio ⇒ híbrida ≡ B (os testes de B valem).
    - tipo=2 (complementar) NUNCA exclui (re-processamento).
    - Demitido sem presença na folha do mês: set-minus é no-op.
    - Edge V1: demitido em M ainda presente na folha de M+1 reaparece como
      ENTRADA — aceito e pinado em teste.
    - is_partial mede a FOLHA CRUA (len(pres), não ativos): rotação alta
      não flagra; queda real de folha flagra como antes.
    - Drift limitado por construção: re-âncora mensal na folha; o Final
      difere da presença só pelos demitidos do PRÓPRIO mês (nunca acumula).

    Herdado da convenção B: entradas/saídas incluem afastamentos e
    retornos; nascimento de série/janela = entradas cheias; encerramento
    terminal invisível; continuidade vertical em buracos não garantida.
    Overlap (vale também pro forescisoes): mês pré-`inicio`
    presente nos DOIS frames ancora o shift/diff e nunca vaza pro output.
    """
    cols = [
        "codi_emp", "competencia",
        "qtd_inicial", "qtd_admissoes", "qtd_rescisoes", "qtd_final",
        "is_partial",
    ]
    if raw_fobases.empty or "tipo_process" not in raw_fobases.columns:
        return pd.DataFrame(columns=cols)
    _require_cols(raw_fobases, ["codi_emp", "competencia", "i_empregados"],
                  "funcionarios_mensal")

    fb11 = raw_fobases[raw_fobases["tipo_process"] == TIPO_PROCESS_FOLHA_MENSAL][
        ["codi_emp", "competencia", "i_empregados"]
    ].copy()
    # nunique() dropa NaN; um set manteria `nan` e a
    # identidade quebraria por 1 — dropa explicitamente.
    fb11 = fb11.dropna(subset=["i_empregados"])
    if fb11.empty:
        return pd.DataFrame(columns=cols)
    fb11["competencia"] = _to_competencia(fb11["competencia"])

    # Demitidos tipo=1 por (emp, mes do EVENTO). Guard de colunas/vazio
    # -> dict vazio = degrada pra convencao B.
    dem_mes: dict[tuple, set] = {}
    need_fr = {"codi_emp", "i_empregados", "demissao", "tipo"}
    if (raw_forescisoes is not None and not raw_forescisoes.empty
            and need_fr.issubset(raw_forescisoes.columns)):
        fr = raw_forescisoes[raw_forescisoes["tipo"] == TIPO_FORESCISAO_COMUM][
            ["codi_emp", "i_empregados", "demissao"]
        ].dropna(subset=["i_empregados", "demissao"]).copy()
        fr["competencia"] = _to_competencia(fr["demissao"])
        fr = fr.drop_duplicates(["codi_emp", "i_empregados", "competencia"])
        for r in fr.itertuples():
            dem_mes.setdefault((r.codi_emp, r.competencia), set()).add(r.i_empregados)

    # Pipeline ÚNICO — final/inicial/entradas/saídas saem da
    # MESMA estrutura de histórico (mesma normalização, mesmo sort, mesma
    # âncora de shift). Sem pipelines paralelos = sem âncoras divergentes.
    hist = (
        fb11.groupby(["codi_emp", "competencia"])["i_empregados"]
        .agg(frozenset)
        .rename("pres")
        .reset_index()
        .sort_values(["codi_emp", "competencia"])
        .reset_index(drop=True)
    )
    # folha CRUA alimenta is_partial (mede fechamento da folha, nao rotacao)
    hist["folha_n"] = hist["pres"].map(len).astype(int)
    # ativos no fim do mes (hibrida V1)
    hist["cur"] = [
        p - frozenset(dem_mes.get((e, m), ()))
        for e, m, p in zip(hist["codi_emp"], hist["competencia"], hist["pres"])
    ]
    prev = hist.groupby("codi_emp")["cur"].shift(1)
    hist["prev"] = prev.apply(lambda s: s if isinstance(s, frozenset) else frozenset())
    hist["qtd_final"] = hist["cur"].map(len).astype(int)
    hist["qtd_inicial"] = hist["prev"].map(len).astype(int)
    hist["qtd_admissoes"] = [len(c - p) for c, p in zip(hist["cur"], hist["prev"])]
    hist["qtd_rescisoes"] = [len(p - c) for c, p in zip(hist["cur"], hist["prev"])]
    hist["is_partial"] = (
        hist.groupby("codi_emp")["folha_n"]
        .transform(_flag_partial)
        .astype(bool)
    )

    # Emissão = meses COM folha dentro de [inicio, fim] (linha sem folha não
    # existe).
    meses = {str(p) for p in pd.period_range(start=inicio, end=fim, freq="M")}
    result = hist[hist["competencia"].isin(meses)][cols].copy()
    return result.sort_values(["codi_emp", "competencia"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# mart.balancete_mensal
# ---------------------------------------------------------------------------

def build_balancete_mensal(
    raw_ctlancto: pd.DataFrame,
    raw_ctcontas: pd.DataFrame,
    raw_saldo_inicial: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Consolida lançamentos contábeis em balancete mensal por empresa/mês/CONTA.

    ctlancto não tem codi_cta diretamente — cada lançamento tem cdeb_lan (conta
    devedora) e ccre_lan (conta credora), que SÃO codi_cta. Fazemos JOIN com
    ctcontas para obter clas_cta e nome_cta.

    Grão: a identidade da conta é codi_cta — clas_cta é atributo de
    CLASSIFICAÇÃO (contas distintas colidem na mesma clas_cta, ex.:
    "Rescisão" e "Serviços PJ" co-classificadas) e nome_cta é RÓTULO cru do
    plano de contas (sem desambiguação — nada exibe nome hoje; display futuro
    sufixa NA exibição). Reclassificação na origem vira UPDATE in-place da
    linha (chave do mart não inclui clas/nome). cumsum, saldo inicial e
    phantom são POR CONTA — o si de uma conta nunca vaza pra irmã
    co-classificada, e cada conta carrega a própria série cumulativa.

    Colunas produzidas:
        codi_emp, competencia, codi_cta, clas_cta, nome_cta (se disponível),
        valo_deb, valo_cre, valo_saldo (deb - cre)
    """
    _require_cols(raw_ctlancto, ["codi_emp", "data_lan", "cdeb_lan", "ccre_lan"], "balancete_mensal/lanctos")
    _require_cols(raw_ctcontas, ["codi_emp", "codi_cta", "clas_cta"], "balancete_mensal/contas")

    lan = raw_ctlancto.copy()
    lan["competencia"] = _to_competencia(lan["data_lan"])

    # Detectar coluna de valor: pode vir separada (valo_deb/valo_cre) ou única (valo_lan)
    has_separate = "valo_deb" in lan.columns and "valo_cre" in lan.columns
    if not has_separate:
        val_col = next((c for c in ["valo_lan", "vlor_lan", "valo"] if c in lan.columns), None)
        if val_col is None:
            raise ValueError("[balancete_mensal] ctlancto precisa ter valo_deb/valo_cre ou valo_lan")
        lan["valo_deb"] = lan[val_col]
        lan["valo_cre"] = lan[val_col]

    # Robustez a dado esparso: coage o valor a numérico (float). Sem isso, uma
    # coluna object dtype (Decimal vindo do driver, ou fatia 100%-NULL via
    # read_sql) propaga até o cumsum das contas de balanço lá embaixo, que
    # estoura com "cumsum is not supported for object dtype". Complementa o
    # guard de janela-vazia: aquele cobre `result` vazio; este cobre `result`
    # NÃO-vazio com valor object. No-op no caminho float normal.
    lan["valo_deb"] = pd.to_numeric(lan["valo_deb"], errors="coerce")
    lan["valo_cre"] = pd.to_numeric(lan["valo_cre"], errors="coerce")

    # Lançamentos de encerramento de exercício (orig_lan == 2,
    # "apuração de resultado") transferem o saldo das contas de resultado pra
    # conta de Apuração (ARE), zerando qualquer agregação que cruze o
    # fechamento. Removemos a perna desses lançamentos que toca conta de
    # RESULTADO (clas_cta prefixo 3/4); a perna ARE↔PL (classes 9/2) é
    # preservada, então a transferência do lucro pro Patrimônio Líquido
    # continua intacta (Endividamento/Liquidez não regridem).
    lan_deb = lan
    lan_cre = lan
    if "orig_lan" in lan.columns:
        _res_ctas = set(
            raw_ctcontas.loc[
                raw_ctcontas["clas_cta"].astype(str).str[:1].isin(["3", "4"]),
                ["codi_emp", "codi_cta"],
            ].drop_duplicates().itertuples(index=False, name=None)
        )
        _enc = lan["orig_lan"].eq(ORIG_LAN_ENCERRAMENTO)
        _deb_res = pd.Series(
            list(zip(lan["codi_emp"], lan["cdeb_lan"])), index=lan.index
        ).isin(_res_ctas)
        _cre_res = pd.Series(
            list(zip(lan["codi_emp"], lan["ccre_lan"])), index=lan.index
        ).isin(_res_ctas)
        lan_deb = lan[~(_enc & _deb_res)]
        lan_cre = lan[~(_enc & _cre_res)]

    # Lado débito: cdeb_lan → codi_cta, somar valo_deb
    deb = (
        lan_deb[["codi_emp", "competencia", "cdeb_lan", "valo_deb"]]
        .rename(columns={"cdeb_lan": "codi_cta"})
        .groupby(["codi_emp", "competencia", "codi_cta"], as_index=False)
        .agg(valo_deb=("valo_deb", "sum"))
    )

    # Lado crédito: ccre_lan → codi_cta, somar valo_cre
    cre = (
        lan_cre[["codi_emp", "competencia", "ccre_lan", "valo_cre"]]
        .rename(columns={"ccre_lan": "codi_cta"})
        .groupby(["codi_emp", "competencia", "codi_cta"], as_index=False)
        .agg(valo_cre=("valo_cre", "sum"))
    )

    balancete = deb.merge(cre, on=["codi_emp", "competencia", "codi_cta"], how="outer")
    balancete["valo_deb"] = balancete["valo_deb"].fillna(0.0)
    balancete["valo_cre"] = balancete["valo_cre"].fillna(0.0)
    balancete["valo_saldo"] = balancete["valo_deb"] - balancete["valo_cre"]

    # JOIN com ctcontas para obter clas_cta e nome_cta (opcional)
    contas_cols = ["codi_emp", "codi_cta", "clas_cta"]
    if "nome_cta" in raw_ctcontas.columns:
        contas_cols.append("nome_cta")
    contas = raw_ctcontas[contas_cols].drop_duplicates(["codi_emp", "codi_cta"])
    result = balancete.merge(contas, on=["codi_emp", "codi_cta"], how="left")

    # codi_cta é a identidade; clas_cta/nome_cta pegam carona (dependência
    # funcional codi_cta -> (clas, nome) via `contas` deduplicado acima —
    # adicionar codi_cta só DIVIDE grupos, nunca funde). dropna default:
    # lançamento em conta órfã (clas NaN no merge left) segue fora, como antes.
    group_cols = ["codi_emp", "competencia", "codi_cta", "clas_cta"]
    if "nome_cta" in result.columns:
        group_cols.append("nome_cta")
    result = (
        result.groupby(group_cols, as_index=False)
        .agg(valo_deb=("valo_deb", "sum"), valo_cre=("valo_cre", "sum"))
    )
    result = result.sort_values(["codi_emp", "codi_cta", "competencia"]).reset_index(drop=True)

    # Janela sem lançamentos: `result` nasce vazio. Sem nenhuma linha real, as
    # phantom rows de saldo inicial não têm como ancorar (dependem de emp_first,
    # a 1ª competência COM movimento) — a empresa fica fora do mart por design
    # (limitação documentada abaixo). Guarda contra os crashes a jusante num
    # frame vazio: cumsum em dtype object (extract devolve 0 linhas object) e a
    # comparação str vs NaN na lógica de phantom (frame tipado vazio). Devolve o
    # frame vazio com o schema completo (inclui valo_saldo).
    if result.empty:
        result["valo_saldo"] = pd.Series(dtype="float64")
        return result

    # Sinal segundo natureza da conta (1º dígito de clas_cta):
    #   1=Ativo, 4=Despesa  -> natural devedor (deb - cre)
    #   2=Passivo/PL, 3=Receita -> natural credor (cre - deb)
    prefix = result["clas_cta"].astype(str).str[:1]
    is_balance = prefix.isin(["1", "2"])
    sinal = prefix.map(SINAL_NATUREZA).fillna(1)
    mov_mes = (result["valo_deb"] - result["valo_cre"]) * sinal

    # Saldo inicial por (emp, codi_cta) somado a partir de raw_saldo_inicial
    # (lançamentos anteriores ao período; o raw já é chaveado por conta). Sem
    # isso, o cumsum partiria de zero e subestimaria o saldo absoluto das
    # contas de balanço (ativo/passivo/PL). O si é DA CONTA, nunca o total da
    # clas.
    if raw_saldo_inicial is not None and not raw_saldo_inicial.empty:
        si = raw_saldo_inicial[["codi_emp", "codi_cta", "valo_deb", "valo_cre"]].copy()
        si = si.merge(contas, on=["codi_emp", "codi_cta"], how="left")
        si = si[si["clas_cta"].notna()]
        si_prefix = si["clas_cta"].astype(str).str[:1]
        si_sinal = si_prefix.map(SINAL_NATUREZA).fillna(1)
        si["mov_inicial"] = (si["valo_deb"] - si["valo_cre"]) * si_sinal
        # Somente contas de balanço (1, 2); o grão já é (emp, codi_cta) — o
        # groupby é robustez contra duplicata acidental no raw
        si = si[si_prefix.isin(["1", "2"])]
        saldo_inicial = si.groupby(["codi_emp", "codi_cta"], as_index=False)["mov_inicial"].sum()
        saldo_inicial = saldo_inicial.set_index(["codi_emp", "codi_cta"])["mov_inicial"]
    else:
        saldo_inicial = pd.Series(dtype=float)

    # Cumulativo só para contas de balanço; contas de resultado mantêm movimento mensal
    grupo_key = result["codi_emp"].astype(str) + "|" + result["codi_cta"].astype(str)
    cumsum = mov_mes.groupby(grupo_key).cumsum()
    # Adiciona o saldo inicial DA PRÓPRIA conta ao cumulativo
    si_lookup = result.set_index(["codi_emp", "codi_cta"]).index.map(saldo_inicial).to_series().fillna(0).reset_index(drop=True)
    cumsum_com_inicial = cumsum.values + si_lookup.values
    result["valo_saldo"] = mov_mes.where(~is_balance, pd.Series(cumsum_com_inicial, index=result.index))

    # ── Phantom rows de saldo inicial ─────────────────────────────────────────
    # Toda CONTA (codi_cta) com |si| >= 0.01 cuja primeira linha em `result` é
    # POSTERIOR à primeira competência da empresa ganha uma linha sintética na
    # emp_first com valo_saldo = si e movimento zero. Cobre conta DORMENTE
    # (nenhuma linha na janela — sem isso o saldo some do balancete;
    # inclusive a irmã dormente de uma clas_cta cuja outra conta está ativa)
    # e conta com primeiro movimento tardio (sem a phantom não há ponto real
    # pra ancorar o ffill do consumer). Nunca colide com o grain: acct_first > emp_first garante
    # ausência de linha real na emp_first. Empresa sem NENHUM lançamento na
    # janela segue fora do mart (limitação conhecida).
    if not saldo_inicial.empty:
        si_df = saldo_inicial.reset_index().rename(columns={"mov_inicial": "saldo"})
        _norm_chaves_bal(si_df)

        acct_first = (
            result.groupby(["codi_emp", "codi_cta"], as_index=False)["competencia"].min()
            .rename(columns={"competencia": "acct_first"})
        )
        _norm_chaves_bal(acct_first)
        si_df = si_df.merge(acct_first, on=["codi_emp", "codi_cta"], how="left")

        emp_first = result.groupby("codi_emp")["competencia"].min()
        si_df["competencia"] = si_df["codi_emp"].map(emp_first)

        si_df["acct_first"] = si_df["acct_first"].fillna("")
        precisa = (
            si_df["competencia"].notna()
            & (si_df["saldo"].abs() >= 0.01)
            & ((si_df["acct_first"] == "") | (si_df["acct_first"] > si_df["competencia"]))
        )
        phantom = si_df.loc[precisa].drop(columns=["acct_first"])

        if not phantom.empty:
            # clas_cta e nome_cta da PRÓPRIA conta, direto do plano — o nome
            # da phantom é exato
            lk = _norm_chaves_bal(contas.copy())
            phantom = phantom.merge(lk, on=["codi_emp", "codi_cta"], how="left")
            phantom = phantom[phantom["clas_cta"].notna()]
            if "nome_cta" in phantom.columns:
                phantom["nome_cta"] = phantom["nome_cta"].fillna("")
            phantom["valo_deb"] = 0.0
            phantom["valo_cre"] = 0.0
            phantom = phantom.rename(columns={"saldo": "valo_saldo"})
            for c in result.columns:
                if c not in phantom.columns:
                    phantom[c] = None
            result = pd.concat([result, phantom[result.columns]], ignore_index=True)

    return result.sort_values(["codi_emp", "competencia", "clas_cta", "codi_cta"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# mart.dre_mensal (resultado contabil do razao)
# ---------------------------------------------------------------------------

# grupos DRE da origem (ctcontas.grdre_cta -> CtGruposDre.codigo). So os que o
# build usa hoje; o resto e classificador de linha (display futuro).
GRDRE_RECEITA_OPERACIONAL = 1
GRDRE_DEDUCOES = 2
# fallback de clas_cta quando a empresa NAO configura grdre.
CLAS_RECEITA_BRUTA = "30101"
CLAS_DEDUCOES = "30102"
CLAS_PROVISAO_IRPJ_CSLL = "40106"   # provisao de IRPJ/CSLL (abaixo do LAIR)

DRE_COLS = [
    "codi_emp", "competencia", "receita_bruta", "deducoes", "receita_liquida",
    "despesas", "resultado_liquido", "lair", "provisao_irpj_csll",
]


def build_dre_mensal(
    raw_ctlancto: pd.DataFrame,
    raw_ctcontas: pd.DataFrame,
) -> pd.DataFrame:
    """DRE / resultado contabil mensal por empresa.

    Substitui o proxy de lucratividade (fat_NF - despesas - impostos) pelo
    resultado LIQUIDO derivado do razao — a DRE que o Dominio calcula. Grao
    (codi_emp, competencia): 1 linha/emp/mes (colunas, nao linhas). Contas de resultado sao FLUXO do periodo: SEM
    saldo_inicial/cumsum/phantom (diferente do balancete).

    Universo = TODAS as contas analiticas clas 3/4, excluindo a perna de
    encerramento (orig_lan=2) que toca conta de resultado (mesmo filtro do
    balancete). grdre_cta e CLASSIFICADOR de linha (NUNCA filtro de inclusao):
    receita_bruta = contas grdre_cta=1 quando a empresa configura grdre; senao
    prefixo clas 30101 (fallback POR EMPRESA).

    net_debit por conta = valo_deb - valo_cre. Receita (credor) tem net_debit
    negativo -> receita/resultado positivos via negacao:
      receita_bruta      = -sum(net_debit em contas de receita)   [+]
      deducoes           =  sum(net_debit em contas de deducao)   [+]
      despesas           =  sum(net_debit em clas 4 excl 40106)   [trigger de supressao]
      provisao_irpj_csll =  sum(net_debit em clas 40106)          [+]
      resultado_liquido  = -sum(net_debit em TODAS clas 3/4)  = lair - provisao
      lair               = -sum(net_debit em 3/4 excl 40106)  = resultado + provisao
      receita_liquida    = receita_bruta - deducoes

    Provisao aparece no mes lancado -> resultado_liquido difere do lair nos
    meses com provisao.
    """
    _require_cols(raw_ctlancto, ["codi_emp", "data_lan", "cdeb_lan", "ccre_lan"], "dre_mensal/lanctos")
    _require_cols(raw_ctcontas, ["codi_emp", "codi_cta", "clas_cta"], "dre_mensal/contas")

    lan = raw_ctlancto.copy()
    lan["competencia"] = _to_competencia(lan["data_lan"])
    val_col = next((c for c in ["valo_lan", "vlor_lan", "valo"] if c in lan.columns), None)
    if val_col is None:
        raise ValueError("[dre_mensal] ctlancto precisa ter valo_lan/vlor_lan")
    lan["_val"] = pd.to_numeric(lan[val_col], errors="coerce")

    # Excluir a perna de encerramento (orig_lan=2) que toca conta de
    # RESULTADO — mesmo filtro do build_balancete_mensal.
    lan_deb = lan
    lan_cre = lan
    if "orig_lan" in lan.columns:
        _res_ctas = set(
            raw_ctcontas.loc[
                raw_ctcontas["clas_cta"].astype(str).str[:1].isin(["3", "4"]),
                ["codi_emp", "codi_cta"],
            ].drop_duplicates().itertuples(index=False, name=None)
        )
        _enc = lan["orig_lan"].eq(ORIG_LAN_ENCERRAMENTO)
        _deb_res = pd.Series(list(zip(lan["codi_emp"], lan["cdeb_lan"])), index=lan.index).isin(_res_ctas)
        _cre_res = pd.Series(list(zip(lan["codi_emp"], lan["ccre_lan"])), index=lan.index).isin(_res_ctas)
        lan_deb = lan[~(_enc & _deb_res)]
        lan_cre = lan[~(_enc & _cre_res)]

    deb = (
        lan_deb[["codi_emp", "competencia", "cdeb_lan", "_val"]]
        .rename(columns={"cdeb_lan": "codi_cta"})
        .groupby(["codi_emp", "competencia", "codi_cta"], as_index=False)
        .agg(valo_deb=("_val", "sum"))
    )
    cre = (
        lan_cre[["codi_emp", "competencia", "ccre_lan", "_val"]]
        .rename(columns={"ccre_lan": "codi_cta"})
        .groupby(["codi_emp", "competencia", "codi_cta"], as_index=False)
        .agg(valo_cre=("_val", "sum"))
    )
    acc = deb.merge(cre, on=["codi_emp", "competencia", "codi_cta"], how="outer")
    acc["valo_deb"] = acc["valo_deb"].fillna(0.0)
    acc["valo_cre"] = acc["valo_cre"].fillna(0.0)
    acc["net_debit"] = acc["valo_deb"] - acc["valo_cre"]

    # JOIN clas_cta (+ grdre_cta se extraido; pode faltar em frame sintetico).
    contas_cols = ["codi_emp", "codi_cta", "clas_cta"]
    if "grdre_cta" in raw_ctcontas.columns:
        contas_cols.append("grdre_cta")
    contas = raw_ctcontas[contas_cols].drop_duplicates(["codi_emp", "codi_cta"])
    acc = acc.merge(contas, on=["codi_emp", "codi_cta"], how="left")
    if "grdre_cta" not in acc.columns:
        acc["grdre_cta"] = pd.NA

    # So contas de RESULTADO (clas 3/4). Sem elas, nenhuma linha DRE.
    clas = acc["clas_cta"].astype(str)
    acc = acc[clas.str.startswith("3") | clas.str.startswith("4")].copy()
    if acc.empty:
        return pd.DataFrame(columns=DRE_COLS)

    clas = acc["clas_cta"].astype(str)
    grd = pd.to_numeric(acc["grdre_cta"], errors="coerce")

    # Fallback POR EMPRESA e JANELA-INDEPENDENTE: "a empresa configura grdre" e
    # propriedade do PLANO (raw_ctcontas), NAO dos movimentos da janela. Derivar
    # do acc faria a classificacao (grdre x prefixo) oscilar conforme os meses
    # extraidos -> o mesmo mes gravaria receita diferente entre cargas
    # (divergencia silenciosa sob o UPSERT).
    if "grdre_cta" in raw_ctcontas.columns:
        _grd_plano = pd.to_numeric(raw_ctcontas["grdre_cta"], errors="coerce")
        emp_grdre1 = set(raw_ctcontas.loc[_grd_plano.eq(GRDRE_RECEITA_OPERACIONAL), "codi_emp"])
        emp_grdre2 = set(raw_ctcontas.loc[_grd_plano.eq(GRDRE_DEDUCOES), "codi_emp"])
    else:
        emp_grdre1, emp_grdre2 = set(), set()
    emp_in1 = acc["codi_emp"].isin(emp_grdre1)
    emp_in2 = acc["codi_emp"].isin(emp_grdre2)

    is_receita = (emp_in1 & grd.eq(GRDRE_RECEITA_OPERACIONAL)) | (~emp_in1 & clas.str.startswith(CLAS_RECEITA_BRUTA))
    is_deducao = (emp_in2 & grd.eq(GRDRE_DEDUCOES)) | (~emp_in2 & clas.str.startswith(CLAS_DEDUCOES))
    is_provisao = clas.str.startswith(CLAS_PROVISAO_IRPJ_CSLL)
    is_despesa = clas.str.startswith("4") & ~is_provisao

    nd = acc["net_debit"]
    acc["_receita"] = (-nd).where(is_receita, 0.0)
    acc["_deducoes"] = nd.where(is_deducao, 0.0)
    acc["_despesas"] = nd.where(is_despesa, 0.0)
    acc["_provisao"] = nd.where(is_provisao, 0.0)
    acc["_resultado"] = -nd                          # todas as 3/4
    acc["_lair"] = (-nd).where(~is_provisao, 0.0)    # 3/4 excl provisao 40106

    g = acc.groupby(["codi_emp", "competencia"], as_index=False).agg(
        receita_bruta=("_receita", "sum"),
        deducoes=("_deducoes", "sum"),
        despesas=("_despesas", "sum"),
        provisao_irpj_csll=("_provisao", "sum"),
        resultado_liquido=("_resultado", "sum"),
        lair=("_lair", "sum"),
    )
    g["receita_liquida"] = g["receita_bruta"] - g["deducoes"]
    return g[DRE_COLS].sort_values(["codi_emp", "competencia"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# mart.empresas
# ---------------------------------------------------------------------------

def build_empresas(raw_geempre: pd.DataFrame) -> pd.DataFrame:
    """Gera mart.empresas a partir de geempre (dimensão, sem agregação).

    Retorna todas as colunas disponíveis, desduplicadas por codi_emp.
    """
    _require_cols(raw_geempre, ["codi_emp"], "empresas")
    return raw_geempre.drop_duplicates("codi_emp").reset_index(drop=True)


# ---------------------------------------------------------------------------
# mart.empresa_regime_periodo
# ---------------------------------------------------------------------------

def build_empresa_regime_periodo(
    raw_efsdoimp: pd.DataFrame,
    mart_balancete: pd.DataFrame,
) -> pd.DataFrame:
    """Granularidade mensal — regime tributário por (codi_emp, competencia).

    Cascata de detecção (Simples + retenções é perfil POSITIVO de
    Simples, não transição):

    1. EXISTS efsdoimp WHERE codi_imp=44 no mês -> 'simples'
       (fonte_deteccao='efsdoimp_44'). Inclui Simples + retenções
       simultâneas — predicado positivo, não exclusivo.
    2. EXISTS efsdoimp WHERE codi_imp != 44 no mês (sem das) ->
       'real_presumido' (fonte_deteccao='efsdoimp_outros')
    3. EXISTS balancete WHERE clas_cta LIKE '3010201%' no mês ->
       'real_presumido' (fonte_deteccao='balancete_inferido')
    4. Senão -> regime=NULL (fonte_deteccao='sem_dado')

    Transição de regime mid-year fica detectada IMPLICITAMENTE: meses pré-transição com
    tem_das=True viram 'simples', pós-transição viram 'real_presumido'.
    Granularidade mensal cobre sem flag adicional.

    Grid restringido a empresas × meses com qualquer sinal em efsdoimp
    OU balancete; meses sem dado nenhum não entram no mart.

    Colunas produzidas:
        codi_emp, competencia, regime, fonte_deteccao
    """
    # 1. Sinal por (codi_emp, competencia) do efsdoimp
    if not raw_efsdoimp.empty:
        _require_cols(raw_efsdoimp, ["codi_emp", "codi_imp", "data_sim"],
                      "empresa_regime_periodo/efsdoimp")
        ef = raw_efsdoimp[["codi_emp", "codi_imp", "data_sim"]].copy()
        ef["competencia"] = _to_competencia(ef["data_sim"])
        ef = ef[ef["competencia"].notna()]
        ef_g = ef.groupby(["codi_emp", "competencia"], as_index=False).agg(
            tem_das=("codi_imp", lambda s: bool((s == 44).any())),
            tem_outros=("codi_imp", lambda s: bool((s != 44).any())),
        )
    else:
        ef_g = pd.DataFrame(columns=["codi_emp", "competencia", "tem_das", "tem_outros"])

    # 2. Sinal por (codi_emp, competencia) do balancete (clas_cta '3010201%')
    if not mart_balancete.empty and "clas_cta" in mart_balancete.columns:
        clas = mart_balancete["clas_cta"].astype(str)
        # NAO usa `_mask_deducao` de proposito: este sinal nao filtra por
        # nome ("tem conta de deducao, logo e R/P") e as contas de
        # `CLAS_DEDUCAO_EXATA` sao usadas pra RECEITA em outras empresas.
        mask = _mask_prefixo(clas, PREFIXOS_DEDUCAO)
        bal = mart_balancete.loc[mask, ["codi_emp", "competencia"]].drop_duplicates()
        bal["tem_3010201"] = True
    else:
        bal = pd.DataFrame(columns=["codi_emp", "competencia", "tem_3010201"])

    # 3. Combinar — outer join: empresa × competencia com qualquer sinal
    df = ef_g.merge(bal, on=["codi_emp", "competencia"], how="outer")
    if df.empty:
        return pd.DataFrame(columns=["codi_emp", "competencia", "regime", "fonte_deteccao"])
    df["tem_das"]      = df["tem_das"].fillna(False).astype(bool)
    df["tem_outros"]   = df["tem_outros"].fillna(False).astype(bool)
    df["tem_3010201"]  = df["tem_3010201"].fillna(False).astype(bool)

    # 4. Cascata vetorizada — prioridade descendente (ifs mutuamente exclusivos)
    regime = np.full(len(df), None, dtype=object)
    fonte  = np.full(len(df), "sem_dado", dtype=object)

    mask_das       = df["tem_das"].values
    mask_outros    = df["tem_outros"].values & ~mask_das
    mask_inferido  = df["tem_3010201"].values & ~mask_das & ~mask_outros

    regime[mask_das]      = "simples"
    fonte[mask_das]       = "efsdoimp_44"
    regime[mask_outros]   = "real_presumido"
    fonte[mask_outros]    = "efsdoimp_outros"
    regime[mask_inferido] = "real_presumido"
    fonte[mask_inferido]  = "balancete_inferido"

    df["regime"]         = regime
    df["fonte_deteccao"] = fonte

    return (
        df[["codi_emp", "competencia", "regime", "fonte_deteccao"]]
        .sort_values(["codi_emp", "competencia"])
        .reset_index(drop=True)
    )


# ---------------------------------------------------------------------------
# ISS retido SOFRIDO — intermediário do numerador
# ---------------------------------------------------------------------------

def _linhas_deducao(mart_balancete: pd.DataFrame, padrao: str) -> pd.DataFrame:
    """Linhas do balancete que escrituram a dedução de um imposto.

    Filtro = `clas_cta` em PREFIXOS_DEDUCAO E `nome_cta` casando `padrao`
    (os regex de DEDUCAO_NOME_POR_IMPOSTO). Devolve o frame FILTRADO — quem
    chama decide se quer os pares (portão do complemento) ou a soma de
    `valo_deb` (componente exibido). DataFrame vazio quando o balancete não
    tem as colunas, pra o chamador não precisar repetir o guard.
    """
    if (not padrao or mart_balancete.empty
            or "clas_cta" not in mart_balancete.columns
            or "nome_cta" not in mart_balancete.columns):
        return mart_balancete.iloc[0:0]
    clas = mart_balancete["clas_cta"].astype(str)
    nome = mart_balancete["nome_cta"].fillna("").astype(str)
    return mart_balancete.loc[
        _mask_deducao(clas)
        & nome.str.contains(padrao, case=False, regex=True, na=False)
    ]


def _fora_dos_meses_com_deducao(comp: pd.DataFrame,
                                deducao: pd.DataFrame) -> pd.DataFrame:
    """Apara do complemento (`comp`) os pares que JÁ têm dedução escriturada.

    É o portão anti dupla contagem, compartilhado pela guia e pelo
    ISS retido sofrido: onde o balancete escritura o imposto,
    ele MANDA e o complemento não entra.
    """
    if comp.empty or deducao.empty:
        return comp
    pares = (deducao[["codi_emp", "competencia"]]
             .drop_duplicates().assign(_tem_ded=True))
    j = comp.merge(pares, on=["codi_emp", "competencia"], how="left")
    return j.loc[j["_tem_ded"].isna(), comp.columns]


def build_iss_retido_sofrido(
    raw_efservicos: pd.DataFrame,
    raw_efservicospar: pd.DataFrame,
    report: dict | None = None,
) -> pd.DataFrame:
    """ISS retido na fonte pelo TOMADOR, sobre serviço que a empresa PRESTOU.

    É imposto DELA — só muda quem recolhe. NÃO confundir com o ISS retido DE
    TERCEIROS (ela toma o serviço e retém do prestador), que vive em
    `efentradaspar` / `efsdoimp` codi_imp=18 / `FOGUIA_ISS` e nunca chega
    aqui: a fonte é a parcela da nota EMITIDA.

    Competência = mês da NOTA (`dser_ser`), não o vencimento da parcela. O
    fato gerador é a prestação, e é assim que a escrituração lança:
    `balancete - guia(cod 3)` bate com esta agregação; agregando por
    vencimento, a guia implícita pode sair NEGATIVA, o que é impossível.

    Intermediário NÃO persistido (não é mart): consumido só por
    `build_impostos_destacados_mensais`. Colunas: codi_emp, competencia,
    valo_retido — só linhas com valor > 0.

    `report` (opcional): nota com `dser_ser` imparseável não tem
    competência derivável e é descartada ANTES do groupby — o NÚMERO não
    muda (o `groupby(dropna=True)` já produziria este mesmo resultado), mas
    com `report` passado o descarte fica CONTADO em vez de silencioso.
    """
    out_cols = ["codi_emp", "competencia", "valo_retido"]
    if raw_efservicos is None or raw_efservicospar is None:
        return pd.DataFrame(columns=out_cols)
    if raw_efservicos.empty or raw_efservicospar.empty:
        return pd.DataFrame(columns=out_cols)
    _require_cols(raw_efservicos, ["codi_emp", "codi_ser", "dser_ser"],
                  "iss_retido_sofrido/efservicos")
    _require_cols(raw_efservicospar, ["codi_emp", "codi_ser", "vissr_serp"],
                  "iss_retido_sofrido/efservicospar")

    notas = raw_efservicos[["codi_emp", "codi_ser", "dser_ser"]].copy()
    notas["competencia"] = _to_competencia(notas["dser_ser"])

    par = raw_efservicospar[["codi_emp", "codi_ser", "vissr_serp"]].copy()
    par["vissr_serp"] = pd.to_numeric(par["vissr_serp"],
                                      errors="coerce").fillna(0.0)

    # Descarte EXPLICITO de competência inválida (data imparseável em
    # dser_ser): `_to_competencia` devolve NaN FLOAT nesse caso, e o
    # `groupby(dropna=True)` mais abaixo comeria a linha em SILÊNCIO. O
    # número não muda — só a perda deixa de ser muda.
    sem_competencia = notas["competencia"].isna()
    if sem_competencia.any():
        notas_ruim = notas.loc[sem_competencia]
        perdido = notas_ruim.merge(
            par[["codi_emp", "codi_ser", "vissr_serp"]],
            on=["codi_emp", "codi_ser"], how="inner")
        if report is not None:
            report["iss_retido_competencia_invalida"] = {
                "linhas": int(sem_competencia.sum()),
                "soma": float(perdido["vissr_serp"].sum()),
                "por_empresa": {
                    int(k): float(v) for k, v in
                    perdido.groupby("codi_emp")["vissr_serp"].sum().items()
                },
            }
        notas = notas.loc[~sem_competencia]

    # INNER: parcela sem nota na janela não tem competência derivável.
    j = par.merge(notas[["codi_emp", "codi_ser", "competencia"]],
                  on=["codi_emp", "codi_ser"], how="inner")
    if j.empty:
        return pd.DataFrame(columns=out_cols)

    out = (
        j.groupby(["codi_emp", "competencia"], as_index=False)["vissr_serp"]
        .sum()
        .rename(columns={"vissr_serp": "valo_retido"})
    )
    out = out.loc[out["valo_retido"] > 0]
    return (
        out[out_cols]
        .sort_values(["codi_emp", "competencia"])
        .reset_index(drop=True)
    )


# ---------------------------------------------------------------------------
# mart.impostos_destacados_mensais
# ---------------------------------------------------------------------------

def build_impostos_destacados_mensais(
    mart_balancete: pd.DataFrame,
    mart_impostos: pd.DataFrame,
    mart_regime: pd.DataFrame,
    mart_faturamento: pd.DataFrame,
    retido_sofrido: pd.DataFrame | None = None,
    report: dict | None = None,
) -> pd.DataFrame:
    """Granularidade mensal — % impostos por regime tributário.

    Real/Presumido + numerador > 0 -> fonte='balancete':
        valo_impostos = SUM(valo_deb - valo_cre) em balancete onde
                        clas_cta começa com PREFIXOS_DEDUCAO ('3010201')
                        E nome_cta matching RE_IMPOSTOS_DESTACADOS
                        (\\b(?:ICMS|PIS|COFINS|IPI|ISS|RET)\\b).
                        LÍQUIDO de estorno:
                        estorno lançado a crédito na própria conta de
                        dedução anula o lançamento. `report`, quando
                        passado, ganha a chave `estorno_deducao_delta`
                        (total + por empresa) nos meses com estorno.
        base_receita  = SUM(valo_cre) em balancete onde clas_cta começa
                        com PREFIXOS_RECEITA ('3010101', '3010102',
                        '3010103' — Receita Bruta DRE clássica).
                        Fallback: valo_saidas+valo_servicos do mart_faturamento
                        quando balancete não tem receita escriturada
                        (caso PMCMV / imobiliária).

    Real/Presumido + ISS fora do balancete -> fonte COMPOSICIONAL:
        valo_impostos = num_balancete + num_guia + num_retido, onde os DOIS
                        últimos são o COMPLEMENTO e só entram no mês em que a
                        empresa NÃO escritura a conta de dedução de ISS
                        (DEDUCAO_NOME_POR_IMPOSTO). O balancete MANDA quando
                        existe — o complemento só preenche lacuna, nunca soma
                        por cima (anti dupla contagem).
          • num_guia   = SUM(srec_sim) dos impostos em
                         GUIA_IMPORTAVEL_DESTACADOS. Lista branca estreita
                         por duas armadilhas (IRPJ/CSLL colidiria com a
                         provisão da dre_mensal; ICMS da guia é "a recolher",
                         base diferente do destacado) — ver mappings.
          • num_retido = ISS retido SOFRIDO (build_iss_retido_sofrido): o
                         tomador reteve o ISS de serviço que a empresa
                         PRESTOU. Imposto DELA.
        Os dois são DISJUNTOS: a identidade `balancete = guia + retido` fecha
        MENSALMENTE. Por isso somam entre si quando o balancete não cobre.
        `fonte` junta com '+' os componentes presentes: 'balancete', 'guia',
        'retido', 'balancete+guia', 'balancete+retido', 'guia+retido',
        'balancete+guia+retido'.

    Real/Presumido + numerador == 0 + receita > 0 -> fonte='sem_deducao':
        valo_impostos = NaN  (UX deve mostrar '—')
        base_receita  = den_balancete OU den_fat (mesma cascata acima)
        (Quem PAGA imposto em guia sem escriturar a dedução sobe pra
        'guia'/'balancete+guia', não fica aqui.)

    Simples Nacional -> fonte='efsdoimp':
        valo_impostos = SUM(srec_sim) em mart_impostos, EXCLUINDO retenção
                        de terceiros (CODI_IMP_RETENCAO_TERCEIROS —
                        ISS/INSS Ret, IRRF aluguéis PF, CSRF etc.; imposto
                        próprio como DAS e ICMS antecipação permanece)
        base_receita  = valo_saidas+valo_servicos do mart_faturamento.

    Regime IS NULL ou sem dado -> fonte='sem_dado':
        valo_impostos = NaN, base_receita = NaN, pct_impostos = NaN.

    Colunas produzidas:
        codi_emp, competencia, valo_impostos, base_receita, fonte, pct_impostos
    """
    out_cols = [
        "codi_emp", "competencia",
        "valo_impostos", "base_receita", "fonte", "pct_impostos", "valo_iss",
    ]
    if mart_regime.empty:
        return pd.DataFrame(columns=out_cols)
    _require_cols(mart_regime, ["codi_emp", "competencia", "regime"],
                  "impostos_destacados/regime")

    # --- Numerador R/P (balancete: deduções tributárias 3010201xxx) ---
    # LÍQUIDO de estorno. Estorno lançado a CRÉDITO na própria conta de
    # dedução anula o próprio lançamento — somar só valo_deb (BRUTO) afirma
    # uma dedução que não existiu.
    if (not mart_balancete.empty
            and "clas_cta" in mart_balancete.columns
            and "nome_cta" in mart_balancete.columns):
        clas = mart_balancete["clas_cta"].astype(str)
        nome = mart_balancete["nome_cta"].fillna("").astype(str)
        mask_clas = _mask_deducao(clas)
        mask_nome = nome.str.contains(
            RE_IMPOSTOS_DESTACADOS, case=False, regex=True, na=False
        )
        ded_rp = mart_balancete.loc[mask_clas & mask_nome].copy()
        ded_rp["_liquido"] = ded_rp["valo_deb"] - ded_rp["valo_cre"]
        num_rp = (
            ded_rp
            .groupby(["codi_emp", "competencia"], as_index=False)["_liquido"]
            .sum()
            .rename(columns={"_liquido": "num_balancete"})
        )
        if report is not None:
            estornado = ded_rp.loc[ded_rp["valo_cre"] > 0]
            if not estornado.empty:
                report["estorno_deducao_delta"] = {
                    "total": float(estornado["valo_cre"].sum()),
                    "por_empresa": {
                        int(k): float(v) for k, v in
                        estornado.groupby("codi_emp")["valo_cre"].sum().items()
                    },
                }
    else:
        num_rp = pd.DataFrame(columns=["codi_emp", "competencia", "num_balancete"])

    # --- Numerador COMPLEMENTAR vindo da GUIA (lista branca) ---
    # Só entra o imposto da GUIA_IMPORTAVEL_DESTACADOS e SÓ no mês em que a
    # empresa NÃO escritura a conta de dedução daquele imposto — o balancete
    # MANDA quando existe (anti dupla contagem). Ver as duas
    # armadilhas no comentário de GUIA_IMPORTAVEL_DESTACADOS (mappings).
    num_guia = pd.DataFrame(columns=["codi_emp", "competencia", "num_guia"])
    # Fatia do ISS, SEPARADA: o `valo_iss` exibido e o
    # componente ISS, nao a lista branca inteira — ver CODI_IMP_ISS em
    # mappings. Sai daqui, e nao de um segundo filtro la embaixo, porque o
    # portao `_fora_dos_meses_com_deducao` e POR IMPOSTO e ja foi aplicado
    # neste ponto do loop.
    num_guia_iss = pd.DataFrame(
        columns=["codi_emp", "competencia", "num_guia_iss"])
    if (not mart_impostos.empty and "srec_sim" in mart_impostos.columns
            and "codi_imp" in mart_impostos.columns):
        for imp_cod in sorted(GUIA_IMPORTAVEL_DESTACADOS):
            g = (
                mart_impostos.loc[mart_impostos["codi_imp"] == imp_cod]
                .groupby(["codi_emp", "competencia"], as_index=False)["srec_sim"]
                .sum()
                .rename(columns={"srec_sim": "num_guia"})
            )
            if g.empty:
                continue
            # mês que JÁ tem a dedução escriturada: guia fora
            g = _fora_dos_meses_com_deducao(
                g, _linhas_deducao(mart_balancete,
                                   DEDUCAO_NOME_POR_IMPOSTO.get(imp_cod)))
            if imp_cod == CODI_IMP_ISS:
                num_guia_iss = g.rename(
                    columns={"num_guia": "num_guia_iss"}).copy()
            num_guia = (
                g if num_guia.empty
                else pd.concat([num_guia, g], ignore_index=True)
                     .groupby(["codi_emp", "competencia"], as_index=False)["num_guia"].sum()
            )

    # --- 2º componente do complemento: ISS retido SOFRIDO ---
    # MESMO portão da guia: só entra no mês em que a empresa NÃO escritura
    # a conta de dedução de ISS. guia e retido são DISJUNTOS (a identidade
    # `balancete = guia + retido` fecha mensalmente), então
    # quando o balancete não cobre, os dois entram JUNTOS; quando cobre,
    # nenhum entra — o balancete já contém os dois.
    # Retido de TERCEIROS não passa por aqui em hipótese alguma: a fonte é a
    # parcela da nota EMITIDA (efservicospar).
    ded_iss = _linhas_deducao(mart_balancete, DEDUCAO_NOME_POR_IMPOSTO.get(3))
    num_ret = pd.DataFrame(columns=["codi_emp", "competencia", "num_retido"])
    if retido_sofrido is not None and not retido_sofrido.empty:
        num_ret = _fora_dos_meses_com_deducao(
            retido_sofrido[["codi_emp", "competencia", "valo_retido"]]
            .rename(columns={"valo_retido": "num_retido"}),
            ded_iss)

    # --- Componente ISS do numerador, pro bloco Impostos -------------------
    # Ler SÓ guias (efsdoimp) exibiria ZERO pra empresa com ISS 100% retido
    # na fonte (nunca vira guia). O valor certo vive DENTRO do numerador.
    # Composição = o MESMO portão: balancete quando há conta de (-)ISS no
    # mês, senão guia(cod 3) + retido. Fica NaN fora do Real/Presumido (no
    # Simples o ISS está dentro do DAS: linha própria duplicaria) e quando
    # não há ISS em fonte nenhuma (consumer testa notna e não exibe linha).
    if not ded_iss.empty:
        iss_bal = (ded_iss.groupby(["codi_emp", "competencia"], as_index=False)
                   ["valo_deb"].sum().rename(columns={"valo_deb": "num_iss_bal"}))
    else:
        iss_bal = pd.DataFrame(columns=["codi_emp", "competencia", "num_iss_bal"])

    # --- Denominador via balancete (Receita Bruta DRE clássica) ---
    if not mart_balancete.empty and "clas_cta" in mart_balancete.columns:
        clas = mart_balancete["clas_cta"].astype(str)
        mask_rec = _mask_prefixo(clas, PREFIXOS_RECEITA)
        den_bal = (
            mart_balancete.loc[mask_rec]
            .groupby(["codi_emp", "competencia"], as_index=False)["valo_cre"]
            .sum()
            .rename(columns={"valo_cre": "den_balancete"})
        )
    else:
        den_bal = pd.DataFrame(columns=["codi_emp", "competencia", "den_balancete"])

    # --- Numerador Simples (mart.impostos_mensais re-agregado) ---
    if not mart_impostos.empty and "srec_sim" in mart_impostos.columns:
        # Retenção de terceiros (ISS/INSS Ret, IRRF aluguéis, CSRF)
        # não é carga tributária da empresa — fora do numerador.
        imp_proprio = mart_impostos.loc[
            ~mart_impostos["codi_imp"].isin(CODI_IMP_RETENCAO_TERCEIROS)
        ]
        # `valo_devido` (principal + adicional de IRPJ) pela mesma razão do
        # bloco exibido: um universo só. Aqui é NO-OP — empresa do
        # Simples não tem codi_imp=7 (o IR dela está no DAS, codi_imp=44),
        # então valo_devido == srec_sim. Guard mantido pro mart antigo.
        col_val = ("valo_devido" if "valo_devido" in imp_proprio.columns
                   else "srec_sim")
        num_sn = (
            imp_proprio.groupby(["codi_emp", "competencia"], as_index=False)[col_val]
            .sum()
            .rename(columns={col_val: "num_efsdoimp"})
        )
    else:
        num_sn = pd.DataFrame(columns=["codi_emp", "competencia", "num_efsdoimp"])

    # --- Denominador via faturamento mart (Simples + fallback R/P) ---
    if not mart_faturamento.empty:
        fat = mart_faturamento.copy()
        if "valo_saidas" not in fat.columns:
            fat["valo_saidas"] = 0.0
        if "valo_servicos" not in fat.columns:
            fat["valo_servicos"] = 0.0
        # 3a origem: o denominador do % Impostos TEM que seguir o card. O
        # invariante `pct_imp_med == imp_total/fat_total` esta congelado em
        # TestCoerenciaCardImpostos — se o card soma a receita imobiliaria e
        # este denominador nao, o par R$/% para de fechar na calculadora.
        if "valo_imobiliario" not in fat.columns:
            fat["valo_imobiliario"] = 0.0
        fat["den_fat"] = (fat["valo_saidas"].fillna(0)
                          + fat["valo_servicos"].fillna(0)
                          + fat["valo_imobiliario"].fillna(0))
        # mart.faturamento_mensal pode ter tipo_sai na chave — agrega tudo.
        den_fat = (
            fat.groupby(["codi_emp", "competencia"], as_index=False)["den_fat"]
            .sum()
        )
    else:
        den_fat = pd.DataFrame(columns=["codi_emp", "competencia", "den_fat"])

    # --- Combinar tudo no grid do regime ---
    df = mart_regime[["codi_emp", "competencia", "regime"]].copy()
    df = df.merge(num_rp,  on=["codi_emp", "competencia"], how="left")
    df = df.merge(den_bal, on=["codi_emp", "competencia"], how="left")
    df = df.merge(num_sn,  on=["codi_emp", "competencia"], how="left")
    df = df.merge(den_fat, on=["codi_emp", "competencia"], how="left")
    df = df.merge(num_guia, on=["codi_emp", "competencia"], how="left")
    df = df.merge(num_guia_iss, on=["codi_emp", "competencia"], how="left")
    df = df.merge(num_ret, on=["codi_emp", "competencia"], how="left")
    df = df.merge(iss_bal, on=["codi_emp", "competencia"], how="left")
    for c in ("num_balancete", "den_balancete", "num_efsdoimp", "den_fat",
              "num_guia", "num_guia_iss", "num_retido"):
        df[c] = df[c].fillna(0.0)

    n = len(df)
    valo_impostos = np.full(n, np.nan)
    base_receita  = np.full(n, np.nan)
    fonte         = np.full(n, "sem_dado", dtype=object)

    is_rp = (df["regime"] == "real_presumido").values
    is_sn = (df["regime"] == "simples").values

    num_bal = df["num_balancete"].values
    den_bal_v = df["den_balancete"].values
    den_fat_v = df["den_fat"].values
    num_sn_v  = df["num_efsdoimp"].values
    # Complemento da guia (só imposto da lista branca, só no mês sem
    # dedução escriturada dele).
    num_gui = df["num_guia"].values
    # 2º componente do complemento, disjunto da guia.
    num_ret_v = df["num_retido"].values
    num_rp_total = num_bal + num_gui + num_ret_v

    # Denominador R/P: balancete preferido, fallback faturamento se vazio.
    den_rp_pref = np.where(den_bal_v > 0, den_bal_v, den_fat_v)

    # R/P + numerador > 0 -> fonte declara a COMPOSIÇÃO: o híbrido
    # fica VISÍVEL no mart em vez de se esconder dentro de 'balancete'.
    mask_rp_bal = is_rp & (num_rp_total > 0)
    valo_impostos[mask_rp_bal] = num_rp_total[mask_rp_bal]
    base_receita[mask_rp_bal]  = den_rp_pref[mask_rp_bal]
    # fonte COMPOSICIONAL: declara quais componentes formaram o numerador.
    # Sem retido: 'balancete', 'balancete+guia', 'guia'; com retido surgem
    # 'retido', 'guia+retido', 'balancete+retido' e 'balancete+guia+retido'.
    for _i in np.flatnonzero(mask_rp_bal):
        _partes = []
        if num_bal[_i] > 0:
            _partes.append("balancete")
        if num_gui[_i] > 0:
            _partes.append("guia")
        if num_ret_v[_i] > 0:
            _partes.append("retido")
        fonte[_i] = "+".join(_partes) if _partes else "balancete"

    # R/P + numerador == 0 + denom > 0 -> 'sem_deducao' (valo NULL).
    # Empresa que paga em guia sem escriturar a dedução sobe pra
    # 'guia'/'balancete+guia'; sobra aqui quem realmente não tem numerador
    # em fonte nenhuma.
    mask_rp_sem_ded = is_rp & ~(num_rp_total > 0) & (den_rp_pref > 0)
    base_receita[mask_rp_sem_ded] = den_rp_pref[mask_rp_sem_ded]
    fonte[mask_rp_sem_ded] = "sem_deducao"

    # Simples -> 'efsdoimp'
    valo_impostos[is_sn] = num_sn_v[is_sn]
    base_receita[is_sn]  = den_fat_v[is_sn]
    fonte[is_sn] = "efsdoimp"

    # Componente ISS: balancete MANDA (inclusive zerado — conta escriturada
    # com 0 significa "não houve ISS no mês", não "olhe a guia"); sem conta,
    # é o complemento. NaN fora do R/P e quando não há ISS em fonte nenhuma.
    tem_conta_iss = df["num_iss_bal"].notna().values
    # SO o cod 3 do lado da guia (+ retido): `num_gui` carrega a lista
    # branca inteira, e PIS/COFINS tem linha PROPRIA na
    # tabela — somar aqui os contaria duas vezes no TOTAL.
    complemento_iss = df["num_guia_iss"].values + num_ret_v
    valo_iss = np.where(
        tem_conta_iss,
        df["num_iss_bal"].values,
        np.where(complemento_iss > 0, complemento_iss, np.nan),
    )
    # Numérico explícito: `num_iss_bal` vem do merge como object, e uma coluna
    # object vira TEXT quando o mart é criado num banco novo.
    df["valo_iss"] = pd.to_numeric(pd.Series(np.where(is_rp, valo_iss, np.nan), index=df.index))

    df["valo_impostos"] = valo_impostos
    df["base_receita"]  = base_receita
    df["fonte"]         = fonte

    with np.errstate(divide="ignore", invalid="ignore"):
        pct = np.where(
            (base_receita > 0) & ~np.isnan(valo_impostos),
            valo_impostos / base_receita,
            np.nan,
        )
    df["pct_impostos"] = pct

    return (
        df[out_cols]
        .sort_values(["codi_emp", "competencia"])
        .reset_index(drop=True)
    )
