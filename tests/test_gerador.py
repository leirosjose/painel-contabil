"""Gerador sintético: determinístico, no contrato das raw e cobrindo as regras de negócio."""
import hashlib
import re

import pytest

import gerador
from mappings import (CFOPS_DEVOLUCAO_VENDA, CFOPS_ENTREGA_FUTURA_EMISSAO, CFOPS_ENTREGA_FUTURA_ENTREGA,
                      CFOPS_RECEITA_NFE, FAMILIAS_LINHA_FOLHA)
from raw_schema import TIPOS

CFOP_VENDA_A_ORDEM = 5923


@pytest.fixture(scope="module")
def t():
    return gerador.gera(seed=gerador.SEED)


def _hash(tabelas):
    h = hashlib.sha256()
    for nome in sorted(tabelas):
        h.update(nome.encode())
        h.update(tabelas[nome].to_csv(index=False).encode())
    return h.hexdigest()


def test_mesma_semente_gera_os_mesmos_dados(t):
    assert _hash(gerador.gera(seed=gerador.SEED)) == _hash(t)
    assert _hash(gerador.gera(seed=gerador.SEED + 1)) != _hash(t)


def test_toda_tabela_do_contrato_com_as_mesmas_colunas(t):
    esperadas = set(TIPOS) - {"ctsaldo_inicial"}
    assert set(t) == esperadas
    for nome in esperadas:
        assert list(t[nome].columns) == list(TIPOS[nome]), nome


def test_cfops_da_allow_list_mais_venda_a_ordem_fora_dela(t):
    cfops = set(t["efsaidas"]["codi_nat"])
    fora = cfops - set(CFOPS_RECEITA_NFE)
    assert CFOP_VENDA_A_ORDEM in fora
    assert fora <= {CFOP_VENDA_A_ORDEM} | set(CFOPS_ENTREGA_FUTURA_EMISSAO)
    assert set(CFOPS_ENTREGA_FUTURA_ENTREGA) & cfops
    assert set(t["efentradas"]["codi_nat"]) & set(CFOPS_DEVOLUCAO_VENDA)


def test_iss_proprio_e_retido_sao_disjuntos_e_somam_o_servico(t):
    ser, par, imp = t["efservicos"], t["efservicospar"], t["efsdoimp"]
    assert len(par) and len(imp[imp["codi_imp"] == 3])
    ser = ser.assign(mes=ser["dser_ser"].str[:7])
    retidas = ser.merge(par[["codi_emp", "codi_ser"]].drop_duplicates(), on=["codi_emp", "codi_ser"])
    guia = imp[imp["codi_imp"] == 3].assign(mes=imp["data_sim"].str[:7])
    for (emp, mes), g in ser.groupby(["codi_emp", "mes"]):
        base_guia = guia[(guia.codi_emp == emp) & (guia.mes == mes)]["bcal_sim"].sum()
        base_ret = retidas[(retidas.codi_emp == emp) & (retidas.mes == mes)]["vcon_ser"].sum()
        assert round(base_guia + base_ret, 2) == round(g["vcon_ser"].sum(), 2), (emp, mes)


def test_rubricas_cobrem_as_familias_da_folha_e_prov_desc_valido(t):
    usadas = set(t["foclassificacoes_eventos"]["descricao"])
    for familia in ("Folha", "Hora Extra", "Comissão", "Férias"):
        assert usadas & set(FAMILIAS_LINHA_FOLHA[familia]), familia
    assert set(t["fomovto"]["prov_desc"]) <= {"P", "D", "I", "ID"}


def test_rubricas_fecham_com_a_base_da_folha(t):
    mov, bas = t["fomovto"], t["fobases"]
    soma = (mov.assign(comp=mov["data"].str[:7])
            .pivot_table(index=["codi_emp", "i_empregados", "comp", "tipo_proces"], columns="prov_desc",
                         values="valor_cal", aggfunc="sum", fill_value=0).reset_index())
    bas = bas.assign(comp=bas["competencia"].str[:7])
    junto = soma.merge(bas, left_on=["codi_emp", "i_empregados", "comp", "tipo_proces"],
                       right_on=["codi_emp", "i_empregados", "comp", "tipo_process"], validate="1:1")
    assert len(junto) == len(bas)
    assert (junto["P"].round(2) == junto["proventos"].round(2)).all()
    assert (junto["D"].round(2) == junto["descontos"].round(2)).all()


def test_imobiliario_tem_as_tres_origens(t):
    assert set(t["efimob_pagamentos"]["origem_pagamento"]) == {"B", "L", "R"}


def test_matriz_e_filial_com_a_mesma_raiz_e_cnpjs_validos(t):
    emp = t["geempre"].set_index("codi_emp")["cgce_emp"]
    assert emp[103][:8] == emp[105][:8] and emp[103][8:12] == "0001" and emp[105][8:12] == "0002"
    for cnpj in emp:
        assert re.fullmatch(r"\d{14}", cnpj) and gerador.cnpj_valido(cnpj), cnpj


def test_lancamentos_so_em_contas_do_plano_e_imposto_todo_mes(t):
    contas = set(zip(t["ctcontas"].codi_emp, t["ctcontas"].codi_cta))
    lan = t["ctlancto"]
    assert set(zip(lan.codi_emp, lan.cdeb_lan)) <= contas
    assert set(zip(lan.codi_emp, lan.ccre_lan)) <= contas
    meses = {m for m in t["efsaidas"]["dsai_sai"].str[:7]}
    imp = t["efsdoimp"].assign(mes=t["efsdoimp"]["data_sim"].str[:7])
    for emp in t["geempre"]["codi_emp"]:
        assert meses <= set(imp[imp.codi_emp == emp]["mes"]), emp


def test_bancos_nunca_negativo_no_fim_do_mes(t):
    lan = t["ctlancto"].assign(mes=t["ctlancto"]["data_lan"].str[:7])
    bancos = gerador.CONTA_BANCOS
    for emp, g in lan.groupby("codi_emp"):
        mov = (g[g.cdeb_lan == bancos].groupby("mes")["vlor_lan"].sum()
               .sub(g[g.ccre_lan == bancos].groupby("mes")["vlor_lan"].sum(), fill_value=0))
        assert (mov.sort_index().cumsum() >= 0).all(), emp


def test_volume_pequeno_para_a_carga_rodar_rapido(t):
    assert sum(len(df) for df in t.values()) < 50_000


def _por_mes(g, debito=None, credito=None):
    sel = g[g.cdeb_lan == debito] if debito is not None else g[g.ccre_lan == credito]
    return sel.groupby("mes")["vlor_lan"].sum()


def test_custo_das_vendas_nunca_passa_das_compras_do_mes(t):
    lan = t["ctlancto"].assign(mes=t["ctlancto"]["data_lan"].str[:7])
    lan = lan[lan.data_lan > "2023-11-30"]
    estoque, cmv = 5, 22
    for emp, g in lan.groupby("codi_emp"):
        custo = _por_mes(g, debito=cmv)
        if custo.empty:
            continue
        compras = _por_mes(g, debito=estoque).reindex(custo.index, fill_value=0)
        assert (custo.round(2) <= compras.round(2)).all(), (emp, list(custo[custo > compras].index))


def test_estoque_nunca_negativo_no_fim_do_mes(t):
    lan = t["ctlancto"].assign(mes=t["ctlancto"]["data_lan"].str[:7])
    estoque = 5
    for emp, g in lan.groupby("codi_emp"):
        mov = _por_mes(g, debito=estoque).sub(_por_mes(g, credito=estoque), fill_value=0)
        assert (mov.sort_index().cumsum() >= 0).all(), emp


def test_simples_faturamento_da_entrega_futura_nao_baixa_estoque(t):
    """O 5922 só fatura; a mercadoria sai na entrega (5116). Custo sobre os dois contaria a saída duas vezes."""
    sai = t["efsaidas"].assign(mes=t["efsaidas"]["dsai_sai"].str[:7])
    fisica = sai[~sai.codi_nat.isin([CFOP_VENDA_A_ORDEM, *CFOPS_ENTREGA_FUTURA_EMISSAO])]
    base = fisica.groupby(["codi_emp", "mes"])["vcon_sai"].sum()
    lan = t["ctlancto"].assign(mes=t["ctlancto"]["data_lan"].str[:7])
    custo = lan[lan.cdeb_lan == 22].groupby(["codi_emp", "mes"])["vlor_lan"].sum()
    razao = (custo / base.reindex(custo.index)).dropna()
    assert razao.between(0.58 - 1e-4, 0.62 + 1e-4).all(), razao[~razao.between(0.58 - 1e-4, 0.62 + 1e-4)]


def test_receita_do_balancete_nao_conta_o_simples_faturamento_5922(t):
    """A receita reconhece a venda uma vez: na entrega (5116), nunca no simples faturamento (5922).

    Contar os dois punha a mesma venda duas vezes na receita da ALFA, e na GAMA
    matriz contava um 5922 que o faturamento do painel não conta.
    """
    def _por_mes(df, col_data, col_valor):
        return df.groupby([df["codi_emp"], df[col_data].str[:7].rename("mes")])[col_valor].sum()

    sai = t["efsaidas"]
    vendas = sai[~sai.codi_nat.isin([CFOP_VENDA_A_ORDEM, *CFOPS_ENTREGA_FUTURA_EMISSAO])]
    esperada = (_por_mes(vendas, "dsai_sai", "vcon_sai")
                .add(_por_mes(t["efservicos"], "dser_ser", "vcon_ser"), fill_value=0)
                .add(_por_mes(t["efimob_pagamentos"], "data_pagamento", "valor_pago"), fill_value=0))
    lan = t["ctlancto"]
    receita = _por_mes(lan[lan.ccre_lan.isin([16, 17])], "data_lan", "vlor_lan")
    dif = receita.sub(esperada.reindex(receita.index), fill_value=0).round(2)
    erradas = dif[dif.abs() > 0.01]
    resumo = {int(emp): (len(g), round(float(g.sum()), 2)) for emp, g in erradas.groupby(level=0)}
    assert erradas.empty, f"empresa: (meses errados, receita a mais) = {resumo}"
