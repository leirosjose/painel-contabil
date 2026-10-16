"""Sujeira determinística de taxa baixa sobre os dados limpos.

Dois tipos de sujeira, com efeito diferente na carga:
- normalizável: muda só o FORMATO de um valor verdadeiro (data dd/mm/aaaa,
  valor "1.234,56", CNPJ com máscara, nome com espaços/caixa trocados);
- rejeitável: linhas EXTRAS (ano absurdo, coluna obrigatória vazia, empresa
  fora do cadastro, parcela sem nota, conta fora do plano) e cópias exatas.
Depois da limpeza, o que sobra é exatamente o dado limpo. O manifesto diz
quanto de cada tipo foi aplicado, por tabela.
"""
import random

import pandas as pd

# tabela -> {tipo: quantidade}
PLANO_SUJEIRA = {
    "efsaidas": {"data_br": 5, "valor_br": 5, "duplicada": 3, "ano_absurdo": 2, "obrigatoria_vazia": 2,
                 "empresa_inexistente": 2},
    "efentradas": {"valor_br": 3, "ano_absurdo": 1},
    "ctlancto": {"data_br": 5, "duplicada": 2, "conta_fora_do_plano": 2},
    "efservicospar": {"parcela_orfa": 2},
    "geempre": {"cnpj_mascara": 2, "texto_sujo": 1},
    "ctcontas": {"texto_sujo": 3},
}
COLUNA = {
    ("efsaidas", "data_br"): "dsai_sai", ("ctlancto", "data_br"): "data_lan",
    ("efsaidas", "valor_br"): "vcon_sai", ("efentradas", "valor_br"): "vcon_ent",
    ("efsaidas", "ano_absurdo"): "dsai_sai", ("efentradas", "ano_absurdo"): "dent_ent",
    ("efsaidas", "obrigatoria_vazia"): "dsai_sai",
    ("geempre", "cnpj_mascara"): "cgce_emp", ("geempre", "texto_sujo"): "nome_emp",
    ("ctcontas", "texto_sujo"): "nome_cta",
}
CHAVE_NOVA = {"efsaidas": "codi_sai", "efentradas": "codi_ent", "ctlancto": "nume_lan",
              "efservicospar": "codi_ser"}
NORMALIZAVEIS = ("data_br", "valor_br", "cnpj_mascara", "texto_sujo")
EMPRESA_INEXISTENTE = 999
CONTA_INEXISTENTE = 999


def _data_br(v):
    return f"{v[8:10]}/{v[5:7]}/{v[:4]}"


def _valor_br(v):
    return f"{float(v):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _cnpj_mascara(v):
    return f" {v[:2]}.{v[2:5]}.{v[5:8]}/{v[8:12]}-{v[12:]} "


def _texto_sujo(v):
    return "  " + "  ".join(w.capitalize() for w in v.split()) + " "


_FORMATA = {"data_br": _data_br, "valor_br": _valor_br, "cnpj_mascara": _cnpj_mascara,
            "texto_sujo": _texto_sujo}


def suja(tabelas: dict, seed: int) -> tuple[dict, dict]:
    rnd = random.Random(seed)
    saida = dict(tabelas)
    manifesto = {}
    for tabela, plano in PLANO_SUJEIRA.items():
        df = tabelas[tabela].astype(object).reset_index(drop=True)
        livres = list(df.index)
        rnd.shuffle(livres)
        extras = []
        origem_chave = tabelas["efservicos"] if tabela == "efservicospar" else df
        proximo = (int(pd.to_numeric(origem_chave[CHAVE_NOVA[tabela]]).max()) + 1000
                   if tabela in CHAVE_NOVA else 0)
        for tipo, qtd in plano.items():
            for _ in range(qtd):
                if tipo in NORMALIZAVEIS:
                    i, col = livres.pop(), COLUNA[(tabela, tipo)]
                    df.at[i, col] = _FORMATA[tipo](df.at[i, col])
                    continue
                if tipo == "duplicada":
                    extras.append(df.loc[livres.pop()].copy())
                    continue
                limpo = tabelas[tabela].astype(object)
                linha = limpo.loc[rnd.choice(limpo.index)].copy()
                linha[CHAVE_NOVA[tabela]] = proximo
                proximo += 1
                if tipo == "ano_absurdo":
                    col = COLUNA[(tabela, tipo)]
                    linha[col] = ("2204" if tabela == "efsaidas" else "1899") + str(linha[col])[4:]
                elif tipo == "obrigatoria_vazia":
                    linha[COLUNA[(tabela, tipo)]] = None
                elif tipo == "empresa_inexistente":
                    linha["codi_emp"] = EMPRESA_INEXISTENTE
                elif tipo == "conta_fora_do_plano":
                    linha["cdeb_lan"] = CONTA_INEXISTENTE
                extras.append(linha)
        saida[tabela] = pd.concat([df, pd.DataFrame(extras)], ignore_index=True) if extras else df
        manifesto[tabela] = dict(plano)
    return saida, manifesto
