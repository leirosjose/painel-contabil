"""Plano de contas e lançamentos contábeis coerentes com as notas e a folha."""
from collections import defaultdict

import pandas as pd

from gerador.empresas import DATA_ABERTURA, EMPRESAS, MESES, r2
from gerador.fiscal import CFOP_VENDA_A_ORDEM, CFOPS_SEM_SAIDA_FISICA, MARGEM_COMPRA
from raw_schema import TIPOS

# (codi_cta, clas_cta, nome_cta, grdre_cta)
PLANO = (
    (1, "101010101", "CAIXA", None),
    (2, "101010201", "BANCOS CONTA MOVIMENTO", None),
    (3, "101010301", "APLICAÇÕES FINANCEIRAS", None),
    (4, "101020101", "CLIENTES", None),
    (5, "101030101", "ESTOQUE DE MERCADORIAS", None),
    (6, "102010101", "IMOBILIZADO", None),
    (7, "201010101", "FORNECEDORES", None),
    (8, "201010201", "IMPOSTOS A RECOLHER", None),
    (9, "201010206", "IRPJ A RECOLHER", None),
    (10, "201010207", "CSLL A RECOLHER", None),
    (11, "201010301", "SALÁRIOS A PAGAR", None),
    (12, "201010401", "INSS E FGTS A RECOLHER", None),
    (13, "202010101", "EMPRÉSTIMOS LONGO PRAZO", None),
    (14, "204010101", "CAPITAL SOCIAL", None),
    (15, "204030101", "LUCROS ACUMULADOS", None),
    (16, "301010101", "RECEITA DE VENDAS", 1),
    (17, "301010201", "RECEITA DE SERVIÇOS", 1),
    (18, "301020101", "(-) ICMS SOBRE VENDAS", 2),
    (19, "301020102", "(-) PIS SOBRE FATURAMENTO", 2),
    (20, "301020103", "(-) COFINS SOBRE FATURAMENTO", 2),
    (21, "301020104", "(-) ISS SOBRE SERVIÇOS", 2),
    (22, "401010101", "CUSTO DAS MERCADORIAS VENDIDAS", 3),
    (23, "402010101", "SALÁRIOS E ORDENADOS", 4),
    (24, "402010102", "ENCARGOS INSS E FGTS", 4),
    (25, "403010101", "ALUGUÉIS E DESPESAS ADMINISTRATIVAS", 4),
    (26, "404010101", "DESPESAS FINANCEIRAS", 4),
    (27, "401060101", "PROVISÃO PARA IRPJ E CSLL", 7),
)
CONTA_BANCOS = 2
CAIXA = 1
(BANCOS, CLIENTES, ESTOQUE, IMOBILIZADO, FORNECEDORES, IMPOSTOS, IRPJ, CSLL, SALARIOS_PAGAR, ENCARGOS_REC,
 EMPRESTIMOS, CAPITAL, RECEITA_VENDAS, RECEITA_SERVICOS, DED_ICMS, DED_PIS, DED_COFINS, CMV, SALARIOS,
 ENCARGOS, ALUGUEL, FINANCEIRAS, PROVISAO_IR) = (2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 18, 19, 20,
                                                 22, 23, 24, 25, 26, 27)

# Contas que cada perfil não usa ficam fora do plano da empresa.
FORA_DO_PLANO = {"simples": {18, 19, 20, 21, 27}, "presumido": {18, 21}, "real": {21}}
# Guia (codi_imp) que vira dedução no balancete, por regime.
DEDUCAO_POR_GUIA = {"real": {1: DED_ICMS, 17: DED_PIS, 19: DED_COFINS},
                    "presumido": {4: DED_PIS, 5: DED_COFINS}}


def _df(linhas, tabela):
    return pd.DataFrame(linhas, columns=list(TIPOS[tabela]))


def _soma_mes(df, col_emp, col_data, col_valor, filtro=None):
    if df.empty:
        return defaultdict(float)
    d = df if filtro is None else df[filtro(df)]
    s = d.groupby([d[col_emp], d[col_data].str[:7]])[col_valor].sum()
    return defaultdict(float, {k: float(v) for k, v in s.items()})


def gera_contabil(rnd, fiscal: dict, folha: dict) -> dict:
    contas, lanc = [], []
    seq = defaultdict(int)

    def lanca(emp, data, deb, cre, valor):
        if round(valor, 2) <= 0:
            return
        seq[emp] += 1
        lanc.append({"codi_emp": emp, "nume_lan": seq[emp], "data_lan": data, "cdeb_lan": deb,
                     "ccre_lan": cre, "vlor_lan": r2(valor), "orig_lan": 1})

    saidas = _soma_mes(fiscal["efsaidas"], "codi_emp", "dsai_sai", "vcon_sai",
                       lambda d: d["codi_nat"] != CFOP_VENDA_A_ORDEM)
    mercadoria = _soma_mes(fiscal["efsaidas"], "codi_emp", "dsai_sai", "vcon_sai",
                           lambda d: ~d["codi_nat"].isin(CFOPS_SEM_SAIDA_FISICA))
    servicos = _soma_mes(fiscal["efservicos"], "codi_emp", "dser_ser", "vcon_ser")
    imob = _soma_mes(fiscal["efimob_pagamentos"], "codi_emp", "data_pagamento", "valor_pago")
    compras = _soma_mes(fiscal["efentradas"], "codi_emp", "dent_ent", "vcon_ent",
                        lambda d: d["codi_nat"].isin([1101, 1102]))
    proventos = _soma_mes(folha["fobases"], "codi_emp", "competencia", "proventos")
    encargos = defaultdict(float)
    for nome, col in (("foguiainss", "empresa"), ("fofgtsfilial", "total_fgts")):
        for k, v in _soma_mes(folha[nome], "codi_emp", "competencia", col).items():
            encargos[k] += v
    guias = fiscal["efsdoimp"].assign(mes=fiscal["efsdoimp"]["data_sim"].str[:7])

    for e in EMPRESAS:
        fora = FORA_DO_PLANO[e.regime]
        contas += [{"codi_emp": e.codi, "codi_cta": c, "clas_cta": clas, "nome_cta": nome, "grdre_cta": g}
                   for c, clas, nome, g in PLANO if c not in fora]
        comercial = e.perfil in ("comercio", "industria")
        lanca(e.codi, DATA_ABERTURA, BANCOS, CAPITAL, 500_000)
        lanca(e.codi, DATA_ABERTURA, IMOBILIZADO, EMPRESTIMOS, 200_000)
        if comercial:
            lanca(e.codi, DATA_ABERTURA, ESTOQUE, FORNECEDORES, 150_000)
        anterior = {"receita": 0.0, "compras": 0.0, "impostos": 0.0, "encargos": 0.0, "ir": 0.0, "csll": 0.0}
        for mes in MESES:
            fim = f"{mes}-28"
            k = (e.codi, mes)
            receita = saidas[k] + servicos[k] + imob[k]
            conta_receita = RECEITA_SERVICOS if e.perfil == "servicos" else RECEITA_VENDAS
            lanca(e.codi, f"{mes}-05", BANCOS, CLIENTES, anterior["receita"])
            lanca(e.codi, fim, CLIENTES, conta_receita, receita)
            g = guias[(guias.codi_emp == e.codi) & (guias.mes == mes)].set_index("codi_imp")["srec_sim"]
            lanca(e.codi, f"{mes}-20", IMPOSTOS, BANCOS, anterior["impostos"])
            impostos = 0.0
            for codi_imp, conta in DEDUCAO_POR_GUIA.get(e.regime, {}).items():
                if codi_imp in g.index:
                    lanca(e.codi, fim, conta, IMPOSTOS, float(g[codi_imp]))
                    impostos += float(g[codi_imp])
            if e.regime == "simples" and 44 in g.index:
                lanca(e.codi, f"{mes}-20", ALUGUEL, BANCOS, float(g[44]))
            if comercial:
                lanca(e.codi, f"{mes}-10", FORNECEDORES, BANCOS, anterior["compras"])
                lanca(e.codi, fim, ESTOQUE, FORNECEDORES, compras[k])
                lanca(e.codi, fim, CMV, ESTOQUE, mercadoria[k] * rnd.uniform(0.58, MARGEM_COMPRA))
            lanca(e.codi, fim, SALARIOS, SALARIOS_PAGAR, proventos[k])
            lanca(e.codi, fim, SALARIOS_PAGAR, BANCOS, proventos[k])
            lanca(e.codi, f"{mes}-20", ENCARGOS_REC, BANCOS, anterior["encargos"])
            lanca(e.codi, fim, ENCARGOS, ENCARGOS_REC, encargos[k])
            lanca(e.codi, f"{mes}-02", CAIXA, BANCOS, 2_000.0)
            lanca(e.codi, f"{mes}-26", ALUGUEL, CAIXA, rnd.uniform(1_200.0, 1_900.0))
            lanca(e.codi, f"{mes}-15", ALUGUEL, BANCOS, max(3_000.0, receita * 0.03))
            lanca(e.codi, f"{mes}-25", FINANCEIRAS, BANCOS, receita * 0.003)
            lanca(e.codi, f"{mes}-20", IRPJ, BANCOS, anterior["ir"])
            lanca(e.codi, f"{mes}-20", CSLL, BANCOS, anterior["csll"])
            ir = csll = 0.0
            if e.regime != "simples":
                if 7 in g.index:
                    ir, csll = float(g[7]) + float(guias[(guias.codi_emp == e.codi) & (guias.mes == mes)
                                                         & (guias.codi_imp == 7)]["inc1_sim"].sum()), \
                        float(g.get(6, 0.0))
                elif e.perfil == "servicos" and mes[5:] in ("03", "06", "09", "12"):
                    trimestre = sum(servicos[(e.codi, m)] for m in MESES if m[:4] == mes[:4]
                                    and int(m[5:]) in range(int(mes[5:]) - 2, int(mes[5:]) + 1))
                    ir, csll = trimestre * 0.32 * 0.15, trimestre * 0.32 * 0.09
                lanca(e.codi, fim, PROVISAO_IR, IRPJ, ir)
                lanca(e.codi, fim, PROVISAO_IR, CSLL, csll)
            anterior = {"receita": receita, "compras": compras[k] if comercial else 0.0, "impostos": impostos,
                        "encargos": encargos[k], "ir": ir, "csll": csll}
    return {"ctcontas": _df(contas, "ctcontas"), "ctlancto": _df(lanc, "ctlancto")}
