"""Folha mínima: um empregado por família de rubrica, sem rescisão nem 13º."""
import pandas as pd

from gerador.empresas import EMPRESAS, MESES, r2
from raw_schema import TIPOS

TP_MENSAL = 11
# (i_eventos, nome, prov_desc, i_classificacao, descrição da classificação)
EVENTOS = (
    (1, "SALARIO", "P", 1, "Salários"),
    (2, "HORAS EXTRAS 50%", "P", 2, "Horas Extras"),
    (3, "COMISSOES", "P", 3, "Comissão"),
    (4, "INSALUBRIDADE", "P", 4, "Insalubridade"),
    (5, "FERIAS", "P", 5, "Férias Gozadas"),
    (6, "INSS", "D", 6, "INSS"),
    (7, "IRRF", "D", 7, "IRRF"),
    (8, "FGTS", "I", 8, "FGTS"),
)
SALARIO_MINIMO = 1_412.00
ADMISSAO_NOVA = {(103, 6): "2024-07-01"}


def _df(linhas, tabela):
    return pd.DataFrame(linhas, columns=list(TIPOS[tabela]))


def gera_folha(rnd) -> dict:
    empregados, bases, movto, guias_inss, guias_fgts, eventos_emp = [], [], [], [], [], []
    for e in EMPRESAS:
        simples = e.regime == "simples"
        quadro = []
        for n in range(1, e.empregados + 1):
            admissao = ADMISSAO_NOVA.get((e.codi, n),
                                         f"{rnd.randint(2016, 2023)}-{rnd.randint(1, 10):02d}-01")
            salario = r2(rnd.uniform(2_200, 7_500))
            quadro.append((n, admissao, salario))
            empregados.append({"codi_emp": e.codi, "i_empregados": n, "admissao": admissao})
        usados = set()
        for i, mes in enumerate(MESES):
            inss_emp = fgts_tot = 0.0
            for n, admissao, salario in quadro:
                if admissao[:7] > mes:
                    continue
                prov = {1: salario}
                if n == 2:
                    prov[2] = r2(salario * rnd.uniform(0.05, 0.15))
                if n == 3 and e.perfil in ("comercio", "industria"):
                    prov[3] = r2(rnd.uniform(600, 1_800))
                if n == 1 and e.perfil == "industria":
                    prov[4] = r2(SALARIO_MINIMO * 0.20)
                if int(admissao[5:7]) == int(mes[5:]) and admissao[:7] < mes:
                    prov[5] = r2(salario / 3)
                total_p = r2(sum(prov.values()))
                desc = {6: r2(total_p * 0.09)}
                if total_p > 4_000:
                    desc[7] = r2((total_p - 4_000) * 0.15)
                fgts = r2(total_p * 0.08)
                calc = f"C{e.codi}-{mes}-{n}"
                for evt, valor in [*prov.items(), *desc.items(), (8, fgts)]:
                    nome, pd_ = EVENTOS[evt - 1][1], EVENTOS[evt - 1][2]
                    usados.add(evt)
                    movto.append({"codi_emp": e.codi, "i_empregados": n, "data": f"{mes}-01",
                                  "tipo_proces": TP_MENSAL, "i_eventos": evt, "valor_inf": 0.0,
                                  "valor_cal": valor, "prov_desc": pd_, "i_calculos": calc})
                inss = 0.0 if simples else r2(total_p * 0.20)
                bases.append({"codi_emp": e.codi, "i_empregados": n, "competencia": f"{mes}-01",
                              "tipo_process": TP_MENSAL, "proventos": total_p,
                              "descontos": r2(sum(desc.values())), "val_inss_empresa": inss,
                              "val_acid_trabalho": 0.0 if simples else r2(total_p * 0.02),
                              "val_inss_terceiro": 0.0 if simples else r2(total_p * 0.058),
                              "valor_fgts": fgts, "i_calculos": calc})
                inss_emp += 0.0 if simples else r2(total_p * (0.20 + 0.02 + 0.058))
                fgts_tot += fgts
            guias_inss.append({"codi_emp": e.codi, "i_guiainss": f"G{e.codi}-{mes}", "competencia": f"{mes}-01",
                               "tipo_process": TP_MENSAL, "empresa": r2(inss_emp), "deducoes_comp_sf": 0.0,
                               "deducoes_comp_sf_retroativo": 0.0})
            guias_fgts.append({"codi_emp": e.codi, "i_fgtsfilial": f"F{e.codi}-{mes}", "competencia": f"{mes}-01",
                               "tipo_process": TP_MENSAL, "total_fgts": r2(fgts_tot)})
        for evt in sorted(usados):
            _, nome, pd_, cls, _ = EVENTOS[evt - 1]
            eventos_emp.append({"codi_emp": e.codi, "i_eventos": evt, "nome": nome, "prov_desc": pd_,
                                "classificacao": cls})
    return {
        "foempregados": _df(empregados, "foempregados"),
        "fobases": _df(bases, "fobases"),
        "fomovto": _df(movto, "fomovto"),
        "foeventos": _df(eventos_emp, "foeventos"),
        "foeventosdef": _df([{"i_eventos": ev, "nome": nome, "classificacao": cls}
                             for ev, nome, _, cls, _ in EVENTOS], "foeventosdef"),
        "foclassificacoes_eventos": _df([{"i_classificacao": cls, "descricao": desc}
                                         for _, _, _, cls, desc in EVENTOS], "foclassificacoes_eventos"),
        "foguiainss": _df(guias_inss, "foguiainss"),
        "fofgtsfilial": _df(guias_fgts, "fofgtsfilial"),
        "foguiagrfc": _df([], "foguiagrfc"),
        "forescisoes": _df([], "forescisoes"),
    }
