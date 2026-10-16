"""Contrato das tabelas raw: colunas e tipos que a fonte entrega ao pipeline.

É o formato em que o driver JDBC do Domínio devolve cada coluna, e é o que
`load` usa para criar `raw_hist.<tabela>`. Qualquer fonte (banco do Domínio
ou arquivos) tem de entregar exatamente isto:

- ``data``: str ``'YYYY-MM-DD'`` (o driver devolve DATE como texto);
- ``valor``: float64;
- ``inteiro``: int64, ou float64 se a coluna tiver vazio no lote;
- ``texto``: str, com vazio como ``None``.
"""

DATA, VALOR, INTEIRO, TEXTO = "data", "valor", "inteiro", "texto"

TIPOS: dict[str, dict[str, str]] = {
    "geempre": {"codi_emp": INTEIRO, "nome_emp": TEXTO, "apel_emp": TEXTO, "cgce_emp": TEXTO,
                "stat_emp": TEXTO},
    "ctcontas": {"codi_emp": INTEIRO, "codi_cta": INTEIRO, "clas_cta": TEXTO, "nome_cta": TEXTO,
                 "grdre_cta": INTEIRO},
    "ctlancto": {"codi_emp": INTEIRO, "nume_lan": INTEIRO, "data_lan": DATA, "cdeb_lan": INTEIRO,
                 "ccre_lan": INTEIRO, "vlor_lan": VALOR, "orig_lan": INTEIRO},
    "ctsaldo_inicial": {"codi_emp": INTEIRO, "codi_cta": INTEIRO, "valo_deb": VALOR, "valo_cre": VALOR,
                        "data_corte": DATA},
    "efsaidas": {"codi_emp": INTEIRO, "codi_sai": INTEIRO, "dsai_sai": DATA, "vcon_sai": VALOR,
                 "codi_nat": INTEIRO, "codi_acu": INTEIRO},
    "efentradas": {"codi_emp": INTEIRO, "codi_ent": INTEIRO, "dent_ent": DATA, "vcon_ent": VALOR,
                   "codi_nat": INTEIRO},
    "efservicos": {"codi_emp": INTEIRO, "codi_ser": INTEIRO, "dser_ser": DATA, "vcon_ser": VALOR},
    "efservicospar": {"codi_emp": INTEIRO, "codi_ser": INTEIRO, "parc_serp": INTEIRO, "vissr_serp": VALOR},
    "efimob_pagamentos": {
        "codi_emp": INTEIRO, "i_contrato": INTEIRO, "i_parcela": INTEIRO, "i_pagamento": INTEIRO,
        "data_pagamento": DATA, "valor_pago": VALOR, "valor_correcao": VALOR, "valor_juros": VALOR,
        "valor_multa": VALOR, "valor_desconto": VALOR, "valor_liquido": VALOR,
        "base_calculo_pis": VALOR, "base_calculo_cofins": VALOR, "origem_pagamento": TEXTO,
        "valor_devolucao": VALOR, "cst_pis_cofins_receita_financeira_correcao": TEXTO,
        "base_calculo_pis_cofins_receita_financeira_correcao": VALOR,
        "base_calculo_pis_cofins_receita_financeira_juros_multa": VALOR,
    },
    "efsdoimp": {"codi_emp": INTEIRO, "codi_imp": INTEIRO, "data_sim": DATA, "pdic_sim": INTEIRO,
                 "srec_sim": VALOR, "bcal_sim": VALOR, "inc1_sim": VALOR},
    "foempregados": {"codi_emp": INTEIRO, "i_empregados": INTEIRO, "admissao": DATA},
    "fobases": {"codi_emp": INTEIRO, "i_empregados": INTEIRO, "competencia": DATA,
                "tipo_process": INTEIRO, "proventos": VALOR, "descontos": VALOR,
                "val_inss_empresa": VALOR, "val_acid_trabalho": VALOR, "val_inss_terceiro": VALOR,
                "valor_fgts": VALOR, "i_calculos": TEXTO},
    "foguiainss": {"codi_emp": INTEIRO, "i_guiainss": TEXTO, "competencia": DATA,
                   "tipo_process": INTEIRO, "empresa": VALOR, "deducoes_comp_sf": VALOR,
                   "deducoes_comp_sf_retroativo": VALOR},
    "fofgtsfilial": {"codi_emp": INTEIRO, "i_fgtsfilial": TEXTO, "competencia": DATA,
                     "tipo_process": INTEIRO, "total_fgts": VALOR},
    "foguiagrfc": {"i_calculos": TEXTO, "codi_emp": INTEIRO, "vencimento": DATA,
                   "saldo_fins_resc": VALOR, "multa_fgts": VALOR, "mes_ant_valor": VALOR,
                   "resc_valor": VALOR, "aviso_previo_valor": VALOR},
    "forescisoes": {"codi_emp": INTEIRO, "i_calculos": TEXTO, "i_empregados": INTEIRO,
                    "demissao": DATA, "tipo": INTEIRO},
    "fomovto": {"codi_emp": INTEIRO, "i_empregados": INTEIRO, "data": DATA, "tipo_proces": INTEIRO,
                "i_eventos": INTEIRO, "valor_inf": VALOR, "valor_cal": VALOR, "prov_desc": TEXTO,
                "i_calculos": TEXTO},
    "foeventos": {"codi_emp": INTEIRO, "i_eventos": INTEIRO, "nome": TEXTO, "prov_desc": TEXTO,
                  "classificacao": INTEIRO},
    "foeventosdef": {"i_eventos": INTEIRO, "nome": TEXTO, "classificacao": INTEIRO},
    "foclassificacoes_eventos": {"i_classificacao": INTEIRO, "descricao": TEXTO},
    "hrfaturamento": {"codi_emp": INTEIRO, "i_faturamento": INTEIRO, "data_faturamento": DATA},
}

# Chave natural de cada tabela no raw (upsert e validacao da geracao).
RAW_KEYS: dict[str, list[str]] = {
    "geempre":         ["codi_emp"],
    "ctcontas":        ["codi_emp", "codi_cta"],
    "ctlancto":        ["codi_emp", "nume_lan"],
    "efsaidas":        ["codi_emp", "codi_sai"],
    "efentradas":      ["codi_emp", "codi_ent"],
    "efservicos":      ["codi_emp", "codi_ser"],
    # PK declarada na origem (SYS.SYSIDX).
    "efservicospar":   ["codi_emp", "codi_ser", "parc_serp"],
    # Chave = PK da origem; pdic_sim (slot de apuracao) preserva as
    # guias-irmas do mesmo mes que o keep="last" do trio descartava.
    "efsdoimp":        ["codi_emp", "codi_imp", "data_sim", "pdic_sim"],
    "foempregados":    ["codi_emp", "i_empregados"],
    # Grao real do calculo: i_calculos preserva os recibos RPA irmaos do
    # mesmo mes que o keep="last" descartaria — dedup vira no-op e
    # extract == --skip-extract (mesmo caso do pdic_sim).
    "fobases":         ["codi_emp", "i_empregados", "competencia", "tipo_process",
                        "i_calculos"],
    "hrfaturamento":   ["codi_emp", "i_faturamento"],
    # `data_corte` NA CHAVE: o si e "soma de lancamentos < --inicio",
    # entao cada janela produz uma GERACAO propria. Sem a terceira coluna,
    # o extract de uma janela incremental (que e um SUPERSET dos pares do
    # corte canonico) sobrescreve o canonico IN PLACE e a carteira inteira
    # fica sem saldo de abertura. Com ela, as geracoes convivem e
    # `run_transform` filtra a do proprio run.
    # MIGRACAO: banco existente NAO se atualiza sozinho (_ensure_table so
    # faz CREATE TABLE IF NOT EXISTS).
    "ctsaldo_inicial": ["codi_emp", "codi_cta", "data_corte"],
    # Guias de folha (ids unicos verificados na origem)
    "foguiainss":      ["codi_emp", "i_guiainss"],
    "fofgtsfilial":    ["codi_emp", "i_fgtsfilial"],
    "foguiagrfc":      ["codi_emp", "i_calculos"],
    # Registro de rescisoes (id unico verificado na origem)
    "forescisoes":     ["codi_emp", "i_calculos"],
    # Folha por rubrica. A origem NAO declara indice unico na FOMOVTO —
    # i_calculos entra na chave, como em fobases.
    "fomovto":         ["codi_emp", "i_empregados", "data", "tipo_proces",
                        "i_eventos", "i_calculos"],
    "foeventos":       ["codi_emp", "i_eventos"],
    # dicionario PADRAO de rubricas, sem codi_emp. Cobre as
    # empresas que nunca materializaram a copia (FOEVENTOS).
    "foeventosdef":    ["i_eventos"],
    # dicionario GLOBAL de familias, sem codi_emp
    "foclassificacoes_eventos": ["i_classificacao"],
    # Recebimento de parcela de venda de imovel.
    # PK DECLARADA na origem (SYS.SYSINDEXES, "Primary Key") e confirmada pelo
    # DADO (ZERO duplicatas). Diferente de fobases/fomovto, que
    # nao declaram indice unico e precisaram de `i_calculos`, aqui NAO entra
    # coluna discriminadora — inventar uma dividiria o grao sem motivo.
    "efimob_pagamentos": ["codi_emp", "i_contrato", "i_parcela", "i_pagamento"],
}

# Coluna de data que define a janela de cada tabela (None = sem janela própria).
COLUNA_JANELA: dict[str, str | None] = {
    "geempre": None, "ctcontas": None, "ctlancto": "data_lan", "efsaidas": "dsai_sai",
    "efentradas": "dent_ent", "efservicos": "dser_ser", "efservicospar": None,
    "efimob_pagamentos": "data_pagamento", "efsdoimp": "data_sim", "foempregados": None,
    "fobases": "competencia", "foguiainss": "competencia", "fofgtsfilial": "competencia",
    "foguiagrfc": "vencimento", "forescisoes": "demissao", "fomovto": "data",
    "foeventos": None, "foeventosdef": None, "foclassificacoes_eventos": None,
    "hrfaturamento": "data_faturamento",
}

# Dicionários sem codi_emp: nunca levam filtro de empresa nem de janela.
GLOBAIS = frozenset({"foeventosdef", "foclassificacoes_eventos"})


def inicio_do_mes(periodo: str | None) -> str | None:
    """'YYYY-MM' -> 'YYYY-MM-01'; entrada que já traz dia passa como veio."""
    return f"{periodo}-01" if periodo and len(periodo) == 7 else periodo


def limites_do_mes(inicio: str | None, fim: str | None) -> tuple[str | None, str | None]:
    """Período 'YYYY-MM'..'YYYY-MM' -> intervalo meia-aberto [lower, upper) de datas.

    upper é o 1º dia do mês seguinte a `fim`, o que preserva linhas com dia
    diferente de 01 no último mês.
    """
    upper = None
    if fim is not None:
        y, m = int(fim[:4]), int(fim[5:7])
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        upper = f"{y:04d}-{m:02d}-01"
    return inicio_do_mes(inicio), upper
