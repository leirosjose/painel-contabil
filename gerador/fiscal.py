"""Notas fiscais, recebimentos imobiliários e guias de impostos."""
import pandas as pd

from gerador.empresas import EMPRESAS, MESES, dia, fator, r2
from raw_schema import TIPOS

CFOP_VENDA_A_ORDEM = 5923
CFOP_ENTREGA_FUTURA_EMISSAO = 5922
CFOP_ENTREGA_FUTURA_ENTREGA = 5116

# Alíquotas fictícias, só para os números terem ordem de grandeza plausível.
ALIQ = {"das": 0.06, "icms": 0.12, "ipi": 0.05, "pis_nc": 0.0165, "cofins_nc": 0.076,
        "pis": 0.0065, "cofins": 0.03, "iss": 0.05}
PRESUNCAO_IRPJ = {"servicos": 0.32, "imobiliaria": 0.08, "industria": 0.08}


def _df(linhas, tabela):
    return pd.DataFrame(linhas, columns=list(TIPOS[tabela]))


def _notas(rnd, total, n):
    pesos = [rnd.uniform(0.5, 1.5) for _ in range(n)]
    s = sum(pesos)
    return [r2(total * p / s) for p in pesos]


def gera_fiscal(rnd) -> dict:
    saidas, entradas, servicos, parcelas, imob, guias = [], [], [], [], [], []
    seq = {}

    def prox(tab, emp):
        seq[(tab, emp)] = seq.get((tab, emp), 0) + 1
        return seq[(tab, emp)]

    def guia(emp, mes, codi_imp, base, aliq, adicional=0.0):
        guias.append({"codi_emp": emp, "codi_imp": codi_imp, "data_sim": f"{mes}-01", "pdic_sim": 6,
                      "srec_sim": r2(base * aliq), "bcal_sim": r2(base), "inc1_sim": r2(adicional)})

    for e in EMPRESAS:
        trimestre = 0.0
        for i, mes in enumerate(MESES):
            receita = e.receita_base * fator(rnd, i)
            if e.perfil in ("comercio", "industria"):
                n = 12 if e.perfil == "comercio" else (24 if e.codi == 103 else 10)
                cfops = ([5102] * 10 + [6102] * 2) if e.perfil == "comercio" else \
                    ([5101] * (n - 6) + [6101] * 4 + [5405] * 2)
                vendas = _notas(rnd, receita, len(cfops))
                for cfop, v in zip(cfops, vendas):
                    saidas.append({"codi_emp": e.codi, "codi_sai": prox("sai", e.codi), "dsai_sai": dia(rnd, mes),
                                   "vcon_sai": v, "codi_nat": cfop, "codi_acu": 1})
                extras = [(CFOP_VENDA_A_ORDEM, receita * 0.03)]
                if e.codi == 101:
                    extras += [(CFOP_ENTREGA_FUTURA_EMISSAO, receita * 0.05),
                               (CFOP_ENTREGA_FUTURA_ENTREGA, receita * 0.05)]
                if e.codi == 103 and i % 3 == 2:
                    extras += [(5501, receita * 0.08)]
                if e.codi == 103 and i % 6 == 0:
                    extras += [(CFOP_ENTREGA_FUTURA_EMISSAO, receita * 0.02)]
                for cfop, v in extras:
                    saidas.append({"codi_emp": e.codi, "codi_sai": prox("sai", e.codi), "dsai_sai": dia(rnd, mes),
                                   "vcon_sai": r2(v), "codi_nat": cfop, "codi_acu": 1})
                compra = 1102 if e.perfil == "comercio" else 1101
                for v in _notas(rnd, receita * 0.62, 4):
                    entradas.append({"codi_emp": e.codi, "codi_ent": prox("ent", e.codi), "dent_ent": dia(rnd, mes),
                                     "vcon_ent": v, "codi_nat": compra})
                devolucao = 1202 if e.perfil == "comercio" else 1201
                entradas.append({"codi_emp": e.codi, "codi_ent": prox("ent", e.codi), "dent_ent": dia(rnd, mes),
                                 "vcon_ent": r2(receita * rnd.uniform(0.005, 0.02)), "codi_nat": devolucao})
                if e.regime == "simples":
                    guia(e.codi, mes, 44, receita, ALIQ["das"])
                else:
                    guia(e.codi, mes, 1, receita, ALIQ["icms"])
                    guia(e.codi, mes, 2, receita, ALIQ["ipi"])
                    guia(e.codi, mes, 17, receita, ALIQ["pis_nc"])
                    guia(e.codi, mes, 19, receita, ALIQ["cofins_nc"])
                    if e.codi == 103:
                        base_ir = receita * PRESUNCAO_IRPJ["industria"]
                        guia(e.codi, mes, 7, base_ir, 0.15, adicional=0.10 * max(0.0, base_ir - 20_000))
                        guia(e.codi, mes, 6, receita * 0.12, 0.09)
            elif e.perfil == "servicos":
                base_guia = 0.0
                for j, v in enumerate(_notas(rnd, receita, 10)):
                    codi_ser = prox("ser", e.codi)
                    servicos.append({"codi_emp": e.codi, "codi_ser": codi_ser, "dser_ser": dia(rnd, mes),
                                     "vcon_ser": v})
                    if j == 0 or rnd.random() < 0.3:
                        parcelas.append({"codi_emp": e.codi, "codi_ser": codi_ser, "parc_serp": 1,
                                         "vissr_serp": r2(v * ALIQ["iss"])})
                    else:
                        base_guia += v
                guia(e.codi, mes, 3, base_guia, ALIQ["iss"])
                guia(e.codi, mes, 4, receita, ALIQ["pis"])
                guia(e.codi, mes, 5, receita, ALIQ["cofins"])
            else:
                total = 0.0
                for origem, qtd in (("B", 6), ("L", 3)):
                    for v in _notas(rnd, receita * (0.7 if origem == "B" else 0.3), qtd):
                        imob.append(_pagamento(rnd, e.codi, prox("imob", e.codi), mes, v, origem))
                        total += v
                if i % 4 == 3:
                    imob.append(_pagamento(rnd, e.codi, prox("imob", e.codi), mes,
                                           r2(receita * 0.05), "R"))
                guia(e.codi, mes, 4, total, ALIQ["pis"])
                guia(e.codi, mes, 5, total, ALIQ["cofins"])
                trimestre += total
                if mes[5:] in ("03", "06", "09", "12"):
                    base_ir = trimestre * PRESUNCAO_IRPJ["imobiliaria"]
                    guia(e.codi, mes, 7, base_ir, 0.15)
                    guia(e.codi, mes, 6, trimestre * 0.12, 0.09)
                    trimestre = 0.0
    return {"efsaidas": _df(saidas, "efsaidas"), "efentradas": _df(entradas, "efentradas"),
            "efservicos": _df(servicos, "efservicos"), "efservicospar": _df(parcelas, "efservicospar"),
            "efimob_pagamentos": _df(imob, "efimob_pagamentos"), "efsdoimp": _df(guias, "efsdoimp"),
            "hrfaturamento": _df([], "hrfaturamento")}


def _pagamento(rnd, emp, n, mes, valor, origem):
    devolucao = origem == "R"
    pago = 0.0 if devolucao else valor
    correcao = r2(pago * 0.02)
    return {"codi_emp": emp, "i_contrato": 1 + n % 12, "i_parcela": n, "i_pagamento": n,
            "data_pagamento": dia(rnd, mes), "valor_pago": pago, "valor_correcao": correcao,
            "valor_juros": 0.0, "valor_multa": 0.0, "valor_desconto": 0.0,
            "valor_liquido": r2(pago + correcao), "base_calculo_pis": r2(pago + correcao),
            "base_calculo_cofins": r2(pago + correcao), "origem_pagamento": origem,
            "valor_devolucao": valor if devolucao else 0.0,
            "cst_pis_cofins_receita_financeira_correcao": "01",
            "base_calculo_pis_cofins_receita_financeira_correcao": correcao,
            "base_calculo_pis_cofins_receita_financeira_juros_multa": 0.0}
