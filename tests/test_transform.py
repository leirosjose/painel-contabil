# ---------------------------------------------------------------------------
# Testes de caracterização de etl/transform.py. Valores esperados derivados
# RODANDO os builders com DataFrames sintéticos (não por suposição).
# transform.py usa imports flat -> conftest põe etl/ no sys.path.
# ---------------------------------------------------------------------------
import math

import pandas as pd
import pytest

import mappings
import transform
from transform import (
    DRE_COLS,
    _flag_partial,
    _mask_prefixo,
    _norm_chaves_bal,
    build_balancete_mensal,
    build_dre_mensal,
    build_empresa_regime_periodo,
    build_encargos_mensais,
    build_faturamento_mensal,
    build_funcionarios_mensal,
    build_folha_rubricas_mensais,
    build_impostos_destacados_mensais,
    build_impostos_mensais,
    build_iss_retido_sofrido,
)
from mappings import (
    CFOPS_DEVOLUCAO_VENDA,
    CFOPS_ENTREGA_FUTURA_EMISSAO,
    CFOPS_ENTREGA_FUTURA_ENTREGA,
    CFOPS_RECEITA_AMBIGUOS,
    CFOPS_RECEITA_NFE,
    CFOPS_RECEITA_NFE_EMISSAO,
    CODI_IMP_RETENCAO_TERCEIROS,
    EMPRESAS_ENTREGA_FUTURA_FORA,
    ENTREGA_FUTURA_VIGENCIA,
    PREFIXOS_DEDUCAO,
    PREFIXOS_RECEITA,
)


# Vigência de entrega futura fixada para os testes (empresas sintéticas).
_VIGENCIA_TESTE = {201: "2024-11", 205: "2023-01", 206: "2023-01", 209: "2023-03"}


@pytest.fixture
def vigencia_teste(monkeypatch):
    monkeypatch.setattr(mappings, "ENTREGA_FUTURA_VIGENCIA", _VIGENCIA_TESTE)
    monkeypatch.setattr(transform, "ENTREGA_FUTURA_VIGENCIA", _VIGENCIA_TESTE)
    return _VIGENCIA_TESTE


# ── _flag_partial (âncora: regressão pandas 3.0) ────────────────────────────

class TestFlagPartial:
    def test_queda_brusca_e_grande_flagra(self):
        # 100->8: < 50% da mediana dos 3 anteriores E queda absoluta > 5.
        out = _flag_partial(pd.Series([100, 100, 100, 100, 8]))
        assert out.tolist() == [False, False, False, False, True]

    def test_empresa_pequena_nao_flagra(self):
        # 3->1: queda absoluta 2 <= 5 — threshold evita falso positivo.
        out = _flag_partial(pd.Series([3, 3, 3, 1]))
        assert out.tolist() == [False, False, False, False]

    def test_historico_curto_nao_flagra(self):
        assert _flag_partial(pd.Series([100])).tolist() == [False]


# ── build_faturamento_mensal (âncora: devoluções SUBSET de entradas) ────────

class TestCfopsReceitaNfe:
    """Lista de CFOP que compõe faturamento."""

    def test_familias_de_receita_estao_dentro(self):
        # vendas (51xx/61xx/71xx), ST (54xx/64xx), transporte/CT-e (535x/635x,
        # 5932/6932, 7358) e remessa com fim específico de exportação (5501/6501).
        for cfop in (5101, 5102, 6101, 6108, 5117, 5115,
                     5401, 5405, 6401,
                     5353, 5932, 6352, 6932, 7358,
                     5501, 6501, 7101):
            assert cfop in CFOPS_RECEITA_NFE, cfop

    def test_venda_a_ordem_e_zfm_de_mercadoria_estao_dentro(self):
        # REGRESSÃO: 5110/6110 (venda ZFM de mercadoria de terceiros) e
        # 5120/6120 (venda à ordem entregue pelo vendedor remetente) são
        # OPERAÇÕES na versao_nat=2 da bethadba.efnatureza. Só na versao 1
        # (legada) esses códigos carregam nome de cabeçalho em CAPS — ler a
        # tabela com MAX(nome_nat) mistura as versões e os disfarça de grupo.
        for cfop in (5110, 6110, 5120, 6120):
            assert cfop in CFOPS_RECEITA_NFE, cfop

    def test_vendas_de_energia_combustivel_e_comunicacao_estao_dentro(self):
        # Sem elas o filtro zeraria posto de combustível e prestadora de
        # comunicação.
        for cfop in (5251, 5258, 6251, 7251,          # energia elétrica
                     5651, 5655, 5656, 5667, 6656, 7654,  # combustível
                     5303, 5307, 6303, 6307):         # comunicação
            assert cfop in CFOPS_RECEITA_NFE, cfop

    def test_nao_receita_fica_fora(self):
        for cfop in (5151, 5152, 6151, 6152, 5408, 6408,   # transferência
                     5908, 6908, 5910, 6915, 5901,          # remessa/comodato
                     5201, 5202, 6201, 5556, 6556,          # devolução de compra
                     5551, 6551,                            # venda de ativo imob.
                     5927):                                 # baixa de estoque
            assert cfop not in CFOPS_RECEITA_NFE, cfop

    def test_familias_vetadas_ficam_fora(self):
        # 5949/6949 (outra saída), 5908/6908 (comodato), 5929/6929
        # (espelho de cupom) e 5922/6922 (faturamento simbólico) NÃO entram.
        for cfop in (5949, 6949, 5908, 6908, 5929, 6929, 5922, 6922):
            assert cfop not in CFOPS_RECEITA_NFE, cfop

    def test_entrega_futura_conta_a_venda_nao_o_faturamento_simbolico(self):
        # Par que duplicaria a mesma venda: 5922 é o simples faturamento e
        # 5116/5117 é a venda. Só a venda entra.
        assert 5116 in CFOPS_RECEITA_NFE and 5117 in CFOPS_RECEITA_NFE
        assert 6116 in CFOPS_RECEITA_NFE and 6117 in CFOPS_RECEITA_NFE
        assert 5922 not in CFOPS_RECEITA_NFE and 6922 not in CFOPS_RECEITA_NFE

    def test_cabecalhos_de_grupo_nao_sao_operacoes(self):
        # Títulos de grupo na versao_nat=2 da efnatureza (nome em CAPS, sem
        # operação por trás): x.100 "VENDAS DE PRODUÇÃO PRÓPRIA OU DE
        # TERCEIROS", x.150 "TRANSFERÊNCIAS...", x.200 "DEVOLUÇÕES DE
        # COMPRAS...", x.250 "VENDAS DE ENERGIA ELÉTRICA", x.350
        # "PRESTAÇÕES DE SERVIÇOS DE TRANSPORTE", x.400 "SAÍDAS ... ST",
        # x.500 "REMESSAS PARA FORMAÇÃO DE LOTE...".
        # 7110/7120 existem SÓ na versao 1 (legada) — nunca foram operação.
        for cfop in (5100, 6100, 7100, 7110, 7120,
                     5150, 6150, 5200, 6200, 5250, 6250, 7250,
                     5350, 6350, 7350, 5400, 6400, 5500, 6500):
            assert cfop not in CFOPS_RECEITA_NFE, cfop

    def test_disjunto_de_devolucao_de_venda(self):
        assert not (CFOPS_RECEITA_NFE & CFOPS_DEVOLUCAO_VENDA)

    def test_todo_codigo_e_cfop_de_saida(self):
        for cfop in CFOPS_RECEITA_NFE:
            assert isinstance(cfop, int), cfop
            assert 5000 <= cfop <= 7999, cfop

    def test_ambiguos_sao_documentacao_e_nao_vazam_pra_receita(self):
        # CFOPS_RECEITA_AMBIGUOS é documentação (verificação por empresa),
        # nunca numerador do faturamento.
        assert CFOPS_RECEITA_AMBIGUOS
        assert not (CFOPS_RECEITA_AMBIGUOS & CFOPS_RECEITA_NFE)


class TestBuildFaturamentoMensal:
    def test_devolucoes_subset_e_servicos_disjuntos(self):
        saidas = pd.DataFrame({
            "codi_emp": [1, 1],
            "dsai_sai": ["2025-01-10", "2025-01-20"],
            "vcon_sai": [100.0, 200.0],
        })
        # codi_nat 1202 = devolução de venda (CFOPS_DEVOLUCAO_VENDA);
        # 1102 = compra comum. Devolução conta em AMBOS (subset, não disjunto).
        entradas = pd.DataFrame({
            "codi_emp": [1, 1],
            "dent_ent": ["2025-01-05", "2025-01-06"],
            "vcon_ent": [50.0, 30.0],
            "codi_nat": [1102, 1202],
        })
        servicos = pd.DataFrame({
            "codi_emp": [1], "dser_ser": ["2025-01-15"], "vcon_ser": [80.0],
        })
        out = build_faturamento_mensal(saidas, entradas, servicos)
        assert len(out) == 1
        row = out.iloc[0]
        # valo_saidas e LIQUIDO da devolucao de venda do mes da entrada
        # (300 - 30 do 1202).
        assert row["valo_saidas"] == pytest.approx(270.0)
        assert row["valo_entradas"] == pytest.approx(80.0)
        assert row["valo_devolucoes"] == pytest.approx(30.0)  # só o 1202
        assert row["valo_servicos"] == pytest.approx(80.0)    # NFS-e disjunto
        # valo_liquido (legacy) segue derivado do BRUTO (300 - 80): a
        # devolucao ja esta dentro de valo_entradas, derivar do liquido
        # subtrairia 2x.
        assert row["valo_liquido"] == pytest.approx(220.0)
        assert row["qtd_nfs_devolucao"] == 1
        # a 3a origem existe SEMPRE como coluna, mesmo sem raw
        # imobiliario — e o que permite os consumers somarem sem guard de
        # presenca (o guard existe, mas pra banco pre-carga, nao pro build).
        assert row["valo_imobiliario"] == pytest.approx(0.0)


class TestFaturamentoLiquidoDeDevolucao:
    """valo_saidas = NFe de receita MENOS devolucao de venda, abatida no MES DA
    ENTRADA (dent_ent), sem piso -- como o Relatorio de Faturamento do
    Dominio. Substitui a regra "BRUTO, nunca descontar devolucoes".
    """

    @staticmethod
    def _saida(emp, comp_dia, valor, cfop=5102):
        return {"codi_emp": emp, "dsai_sai": comp_dia, "vcon_sai": valor, "codi_nat": cfop}

    @staticmethod
    def _entrada(emp, dia, valor, cfop):
        return {"codi_emp": emp, "dent_ent": dia, "vcon_ent": valor, "codi_nat": cfop}

    def _build(self, saidas, entradas):
        return build_faturamento_mensal(
            pd.DataFrame(saidas), pd.DataFrame(entradas),
            pd.DataFrame(columns=["codi_emp", "dser_ser", "vcon_ser"]))

    def test_lista_de_abatimento_exclui_transferencia_e_ambiguos(self):
        # 2553 e 1206/2206 NAO abatem; transferencia (1208/1209/2208/2209)
        # nao e receita e nao abate.
        assert CFOPS_DEVOLUCAO_VENDA.isdisjoint({1208, 1209, 2208, 2209})
        assert CFOPS_DEVOLUCAO_VENDA.isdisjoint({1553, 2553, 1206, 2206})
        assert {1201, 1202, 1204, 1410, 1411, 2201, 2202, 2411} <= CFOPS_DEVOLUCAO_VENDA

    def test_abate_no_mes_da_entrada_nao_no_da_venda(self):
        # venda em jan, devolucao em fev: jan fica bruto, fev fica negativo.
        out = self._build([self._saida(1, "2026-01-10", 100.0)],
                          [self._entrada(1, "2026-02-05", 30.0, 1202)])
        por_mes = out.set_index("competencia")["valo_saidas"]
        assert por_mes["2026-01"] == pytest.approx(100.0)
        assert por_mes["2026-02"] == pytest.approx(-30.0)

    def test_nfe_liquida_negativa_aparece_negativa_sem_piso(self):
        out = self._build([self._saida(208, "2026-01-10", 49.0)],
                          [self._entrada(208, "2026-01-20", 83.0, 1202)])
        assert out.iloc[0]["valo_saidas"] == pytest.approx(-34.0)

    def test_transferencia_nao_abate_e_nao_entra_em_valo_devolucoes(self):
        out = self._build([self._saida(1, "2026-01-10", 100.0)],
                          [self._entrada(1, "2026-01-11", 40.0, 1209),
                           self._entrada(1, "2026-01-12", 5.0, 1208),
                           self._entrada(1, "2026-01-13", 6.0, 2208)])
        row = out.iloc[0]
        assert row["valo_saidas"] == pytest.approx(100.0)
        assert row["valo_devolucoes"] == pytest.approx(0.0)
        assert row["valo_entradas"] == pytest.approx(51.0)

    def test_ambiguos_2553_1206_nao_abatem(self):
        out = self._build([self._saida(1, "2023-02-10", 100.0)],
                          [self._entrada(1, "2023-02-15", 180.0, 2553),
                           self._entrada(1, "2023-02-16", 41.0, 1206)])
        assert out.iloc[0]["valo_saidas"] == pytest.approx(100.0)

    def test_todos_os_cfops_de_devolucao_de_venda_abatem(self):
        for cfop in (1201, 1202, 1204, 1410, 1411, 2201, 2202, 2411):
            out = self._build([self._saida(1, "2026-01-10", 100.0)],
                              [self._entrada(1, "2026-01-11", 10.0, cfop)])
            assert out.iloc[0]["valo_saidas"] == pytest.approx(90.0), cfop

    def test_devolucao_em_mes_sem_saida_cria_linha_negativa(self):
        out = self._build([self._saida(1, "2026-01-10", 100.0)],
                          [self._entrada(1, "2026-03-05", 25.0, 2202)])
        assert out.set_index("competencia")["valo_saidas"]["2026-03"] == pytest.approx(-25.0)

    def test_sem_devolucao_fica_igual_regressao_zero(self):
        out = self._build([self._saida(20, "2026-01-10", 84.5)],
                          [self._entrada(20, "2026-01-11", 10.0, 1102)])
        assert out.iloc[0]["valo_saidas"] == pytest.approx(84.5)

    def test_valo_liquido_legacy_nao_conta_a_devolucao_duas_vezes(self):
        out = self._build([self._saida(1, "2026-01-10", 100.0)],
                          [self._entrada(1, "2026-01-11", 30.0, 1202),
                           self._entrada(1, "2026-01-12", 50.0, 1102)])
        # bruto 100 - entradas 80 = 20 (nunca 100 - 30 - 80).
        assert out.iloc[0]["valo_liquido"] == pytest.approx(20.0)

    def test_run_janelado_e_skip_extract_dao_o_mesmo_mes(self):
        # stateless por mes: um run que so ve a janela de fev nao muda fev.
        saidas = [self._saida(1, "2026-01-10", 100.0), self._saida(1, "2026-02-10", 70.0)]
        entradas = [self._entrada(1, "2026-02-05", 30.0, 1202)]
        cheio = self._build(saidas, entradas).set_index("competencia")["valo_saidas"]
        janela = self._build(saidas[1:], entradas).set_index("competencia")["valo_saidas"]
        assert cheio["2026-02"] == janela["2026-02"] == pytest.approx(40.0)


class TestFaturamentoOutrasReceitas:
    """Parcelas de venda de imovel entram no faturamento.

    O VALOR do faturamento inclui os recebimentos do modulo imobiliario do
    Dominio. Nao e NFe nem NFS-e — e regime de CAIXA por
    data_pagamento, e na tela vira a linha "Outras receitas" da tabela pivot.
    """

    @staticmethod
    def _base():
        saidas = pd.DataFrame({"codi_emp": [1], "dsai_sai": ["2025-01-10"],
                               "vcon_sai": [100.0]})
        entradas = pd.DataFrame({"codi_emp": [1], "dent_ent": ["2025-01-05"],
                                 "vcon_ent": [10.0], "codi_nat": [1102]})
        return saidas, entradas

    @staticmethod
    def _imob(origens, valores, datas, emp=1):
        return pd.DataFrame({
            "codi_emp": [emp] * len(valores),
            "data_pagamento": list(datas),
            "valor_pago": list(valores),
            "origem_pagamento": list(origens),
        })

    def test_soma_b_e_l_por_mes_do_pagamento(self):
        saidas, entradas = self._base()
        imob = self._imob(("B", "L"), (300.0, 200.0),
                          ("2025-01-09", "2025-01-30"))
        out = build_faturamento_mensal(saidas, entradas, None, imob)
        assert out.iloc[0]["valo_imobiliario"] == pytest.approx(500.0)

    def test_origem_R_nao_soma(self):
        """A devolucao/rescisao NAO entra, e o teste FABRICA o valor.

        Na origem a linha 'R' sempre vem com valor_pago 0,00 (o dinheiro fica
        em valor_devolucao), entao um teste com dado realista passaria por
        ACIDENTE e nao por regra. Aqui o 'R' leva 999,99 de proposito: se o
        filtro cair, o numero aparece.
        """
        saidas, entradas = self._base()
        imob = self._imob(("B", "R"), (300.0, 999.99),
                          ("2025-01-09", "2025-01-15"))
        out = build_faturamento_mensal(saidas, entradas, None, imob)
        assert out.iloc[0]["valo_imobiliario"] == pytest.approx(300.0)

    def test_origem_fora_do_contrato_levanta(self):
        # Idioma do `prov_desc` da folha por rubrica: valor novo na origem
        # levanta ALTO em vez de entrar calado no faturamento.
        saidas, entradas = self._base()
        imob = self._imob(("B", "X"), (300.0, 50.0),
                          ("2025-01-09", "2025-01-15"))
        with pytest.raises(ValueError, match="origem_pagamento"):
            build_faturamento_mensal(saidas, entradas, None, imob)

    def test_sem_raw_imobiliario_a_coluna_e_zero(self):
        # Retrocompat: os ~45 call sites de fixture chamam com 3 args.
        saidas, entradas = self._base()
        out = build_faturamento_mensal(saidas, entradas, None)
        assert out.iloc[0]["valo_imobiliario"] == pytest.approx(0.0)

    def test_empresa_sem_nota_nenhuma_ganha_linha(self):
        """Empresa com faturamento de NF ZERO que so tem recebimento
        imobiliario. Sem linha no mart ela ficaria fora do faturamento
        inteiro; o merge e outer justamente pra isso.
        """
        vazio_s = pd.DataFrame(columns=["codi_emp", "dsai_sai", "vcon_sai"])
        vazio_e = pd.DataFrame(columns=["codi_emp", "dent_ent", "vcon_ent", "codi_nat"])
        imob = self._imob(("B",), (6_580_000.0,), ("2025-07-11",), emp=9)
        out = build_faturamento_mensal(vazio_s, vazio_e, None, imob)
        assert len(out) == 1
        row = out.iloc[0]
        assert row["codi_emp"] == 9
        assert row["competencia"] == "2025-07"
        assert row["valo_saidas"] == pytest.approx(0.0)
        assert row["valo_servicos"] == pytest.approx(0.0)
        assert row["valo_imobiliario"] == pytest.approx(6_580_000.0)


class TestConstantesEntregaFutura:
    """Álgebra das constantes da família entrega futura.

    Sem estes testes, um edit futuro em CFOPS_RECEITA_NFE vaza calado pra
    dentro da lista derivada.
    """

    def test_emissao_troca_entrega_por_faturamento(self):
        assert CFOPS_ENTREGA_FUTURA_ENTREGA & CFOPS_RECEITA_NFE_EMISSAO == frozenset()
        assert CFOPS_ENTREGA_FUTURA_EMISSAO <= CFOPS_RECEITA_NFE_EMISSAO
        assert (CFOPS_RECEITA_NFE - CFOPS_ENTREGA_FUTURA_ENTREGA) <= CFOPS_RECEITA_NFE_EMISSAO

    def test_entrega_futura_esta_na_lista_base_hoje(self):
        # Se cair, a lista base mudou e a derivada precisa de revisão.
        assert CFOPS_ENTREGA_FUTURA_ENTREGA <= CFOPS_RECEITA_NFE

    def test_emissao_saiu_dos_ambiguos(self):
        # 5922/6922 deixaram de ser ambíguos — a decisão foi tomada.
        assert CFOPS_ENTREGA_FUTURA_EMISSAO & CFOPS_RECEITA_AMBIGUOS == frozenset()

    def test_vigencia_e_fora_sao_disjuntos(self):
        assert set(ENTREGA_FUTURA_VIGENCIA) & EMPRESAS_ENTREGA_FUTURA_FORA == set()

    def test_vigencia_publica_esta_congelada(self):
        # Configuração por empresa: só a empresa sintética 101 emite entrega
        # futura com o critério novo, a partir de mar/2024.
        assert ENTREGA_FUTURA_VIGENCIA == {101: "2024-03"}

    def test_empresa_fora_do_criterio_esta_congelada(self):
        assert EMPRESAS_ENTREGA_FUTURA_FORA == frozenset({103})

    def test_empresa_com_vigencia_nao_esta_na_lista_fora(self):
        assert 101 not in EMPRESAS_ENTREGA_FUTURA_FORA
        assert mappings.ENTREGA_FUTURA_VIGENCIA[101] == "2024-03"

    def test_vigencia_tem_formato_de_competencia(self):
        for emp, comp in ENTREGA_FUTURA_VIGENCIA.items():
            assert isinstance(emp, int)
            assert len(comp) == 7 and comp[4] == "-", f"{emp}: {comp!r}"


class TestFaturamentoFiltroCfop:
    """valo_saidas soma só NFe com CFOP de receita (CFOPS_RECEITA_NFE)."""

    @staticmethod
    def _entradas():
        return pd.DataFrame({
            "codi_emp": [1], "dent_ent": ["2025-01-05"],
            "vcon_ent": [10.0], "codi_nat": [1102],
        })

    def test_so_cfop_de_receita_entra_no_faturamento(self):
        # 5101 venda (entra) / 6151 transferência (fora) / 5908 comodato (fora).
        saidas = pd.DataFrame({
            "codi_emp": [1, 1, 1],
            "dsai_sai": ["2025-01-10", "2025-01-11", "2025-01-12"],
            "vcon_sai": [100.0, 50.0, 30.0],
            "codi_nat": [5101, 6151, 5908],
        })
        out = build_faturamento_mensal(saidas, self._entradas(), None)
        assert out["valo_saidas"].iloc[0] == pytest.approx(100.0)
        assert out["qtd_nfs_saida"].iloc[0] == 1

    def test_sem_a_coluna_soma_tudo_como_antes(self):
        # Raw anterior ao contrato novo (colunas ainda NULL/ausentes): o
        # build NÃO pode zerar faturamento — degrada pro comportamento
        # pré-filtro em vez de mentir.
        saidas = pd.DataFrame({
            "codi_emp": [1, 1],
            "dsai_sai": ["2025-01-10", "2025-01-11"],
            "vcon_sai": [100.0, 100.0],
        })
        out = build_faturamento_mensal(saidas, self._entradas(), None)
        assert out["valo_saidas"].iloc[0] == pytest.approx(200.0)
        assert out["qtd_nfs_saida"].iloc[0] == 2

    def test_cfop_nulo_nao_entra(self):
        # Linha sem CFOP não é classificável como receita -> fica de fora
        # (raw meio-migrado: coluna existe, valor ainda NULL).
        saidas = pd.DataFrame({
            "codi_emp": [1, 1],
            "dsai_sai": ["2025-01-10", "2025-01-11"],
            "vcon_sai": [100.0, 70.0],
            "codi_nat": [5101, None],
        })
        out = build_faturamento_mensal(saidas, self._entradas(), None)
        assert out["valo_saidas"].iloc[0] == pytest.approx(100.0)
        assert out["qtd_nfs_saida"].iloc[0] == 1

    def test_tipo_sai_nao_e_mais_chave_do_grao(self):
        # Ramo morto removido: MART_KEYS["faturamento_mensal"] sempre foi
        # [codi_emp, competencia] e a coluna nunca é extraída. Mesmo se ela
        # aparecer no raw, o grão não muda (senão o upsert colapsaria linhas).
        saidas = pd.DataFrame({
            "codi_emp": [1, 1],
            "dsai_sai": ["2025-01-10", "2025-01-11"],
            "vcon_sai": [100.0, 40.0],
            "codi_nat": [5101, 5102],
            "tipo_sai": ["A", "B"],
        })
        out = build_faturamento_mensal(saidas, self._entradas(), None)
        assert len(out) == 1
        assert "tipo_sai" not in out.columns
        assert out["valo_saidas"].iloc[0] == pytest.approx(140.0)

    def test_mes_so_com_nota_nao_receita_sobrevive_com_zero(self):
        # Sem entrada nem servico no mes, o par (empresa, competencia) sumia
        # INTEIRO do build. E o DELETE-escopado (main.py) so alcanca
        # pares PRESENTES no DataFrame novo -> a linha velha, pre-filtro,
        # ficava no mart pra sempre.
        # O par tem que sobreviver com zero REAL.
        saidas = pd.DataFrame({
            "codi_emp": [1],
            "dsai_sai": ["2025-01-10"],
            "vcon_sai": [14.24],
            "codi_nat": [5949],          # "outra saida" — nao e receita
        })
        entradas = pd.DataFrame({
            "codi_emp": [], "dent_ent": [], "vcon_ent": [], "codi_nat": [],
        })
        out = build_faturamento_mensal(saidas, entradas, None)
        assert len(out) == 1
        row = out.iloc[0]
        assert row["codi_emp"] == 1
        assert row["competencia"] == "2025-01"
        assert row["valo_saidas"] == pytest.approx(0.0)
        assert row["qtd_nfs_saida"] == 0

    def test_qtd_conta_so_as_notas_de_receita(self):
        # A contagem acompanha o valor: nota zerada pelo filtro nao infla
        # qtd_nfs_saida (senao o par sobrevive mentindo "tem 3 notas").
        saidas = pd.DataFrame({
            "codi_emp": [1, 1, 1],
            "dsai_sai": ["2025-01-10", "2025-01-11", "2025-01-12"],
            "vcon_sai": [100.0, 50.0, 30.0],
            "codi_nat": [5101, 6151, 5908],
        })
        out = build_faturamento_mensal(saidas, self._entradas(), None)
        assert out["qtd_nfs_saida"].iloc[0] == 1

    def test_faturamento_zerado_nao_produz_nan_nem_inf(self):
        # 100% das saídas são remessa de comodato ->
        # nenhuma linha sobrevive ao filtro. O mart tem que sair com zero
        # real, não NaN/inf, e sem estourar.
        saidas = pd.DataFrame({
            "codi_emp": [1, 1],
            "dsai_sai": ["2025-01-10", "2025-01-11"],
            "vcon_sai": [35000.0, 29000.0],
            "codi_nat": [5908, 6908],
        })
        servicos = pd.DataFrame({
            "codi_emp": [1], "dser_ser": ["2025-01-15"], "vcon_ser": [500.0],
        })
        out = build_faturamento_mensal(saidas, self._entradas(), servicos)
        assert len(out) == 1
        row = out.iloc[0]
        assert row["valo_saidas"] == pytest.approx(0.0)
        assert row["qtd_nfs_saida"] == 0
        assert row["valo_servicos"] == pytest.approx(500.0)
        for col in ("valo_saidas", "valo_entradas", "valo_devolucoes",
                    "valo_servicos", "valo_imobiliario", "valo_liquido"):
            assert math.isfinite(row[col]), col


@pytest.mark.usefixtures("vigencia_teste")
class TestEntregaFuturaEmissao:
    """5922/6922 viram receita; 5117/6117 saem.

    A vigência é POR EMPRESA (mês da 1ª emissão dela no raw) — ver o bloco
    ENTREGA FUTURA em mappings.py.
    """

    @staticmethod
    def _entradas(emp=201):
        return pd.DataFrame({
            "codi_emp": [emp], "dent_ent": ["2025-01-05"],
            "vcon_ent": [10.0], "codi_nat": [1102],
        })

    @staticmethod
    def _val(out, emp, comp):
        # Seleciona por (codi_emp, competencia) — nunca iloc[0], que depende
        # da ordem do merge quando há mais de uma empresa no frame.
        linha = out[(out["codi_emp"] == emp) & (out["competencia"] == comp)]
        return linha["valo_saidas"].iloc[0]

    def test_emissao_entra_e_entrega_sai_na_vigencia(self):
        saidas = pd.DataFrame({
            "codi_emp": [201, 201],
            "dsai_sai": ["2025-03-01", "2025-03-15"],
            "vcon_sai": [78925.0, 214126.0],
            "codi_nat": [5922, 5117],
        })
        out = build_faturamento_mensal(saidas, self._entradas(), None)
        assert self._val(out, 201, "2025-03") == pytest.approx(78925.0)

    def test_empresa_fora_do_dict_mantem_regra_antiga(self):
        # Empresa ausente da vigência: continua contando a ENTREGA e
        # excluindo a emissão.
        saidas = pd.DataFrame({
            "codi_emp": [17, 17],
            "dsai_sai": ["2025-07-01", "2025-07-02"],
            "vcon_sai": [5950.0, 5950.0],
            "codi_nat": [5117, 5922],
        })
        out = build_faturamento_mensal(saidas, self._entradas(17), None)
        assert self._val(out, 17, "2025-07") == pytest.approx(5950.0)

    def test_205_entrou_no_criterio_novo(self):
        # A emissão da 205 é anterior à base e a contabilidade reconheceu a
        # receita LÁ. A entrega de jul/23 não é receita de 2023.
        saidas = pd.DataFrame({
            "codi_emp": [205, 205],
            "dsai_sai": ["2023-03-30", "2023-07-18"],
            "vcon_sai": [28261.50, 370000.0],
            "codi_nat": [5922, 5117],
        })
        out = build_faturamento_mensal(saidas, self._entradas(205), None)
        assert self._val(out, 205, "2023-03") == pytest.approx(28261.50)
        assert self._val(out, 205, "2023-07") == pytest.approx(0.0)

    def test_205_janeiro_23_nao_e_protegido(self):
        # A 205 NÃO ganha proteção de abertura: a entrega de jan/23 espelha
        # uma emissão anterior cuja receita a contabilidade já reconheceu.
        saidas = pd.DataFrame({
            "codi_emp": [205],
            "dsai_sai": ["2023-01-26"],
            "vcon_sai": [140490.40],
            "codi_nat": [5117],
        })
        out = build_faturamento_mensal(saidas, self._entradas(205), None)
        assert self._val(out, 205, "2023-01") == pytest.approx(0.0)

    def test_antes_da_vigencia_da_empresa_mantem_regra_antiga(self):
        # A 201 tem vigência 2024-11; out/2024 ainda é regra antiga.
        saidas = pd.DataFrame({
            "codi_emp": [201, 201],
            "dsai_sai": ["2024-10-17", "2024-11-27"],
            "vcon_sai": [273629.92, 1157954.92],
            "codi_nat": [5101, 5922],
        })
        out = build_faturamento_mensal(saidas, self._entradas(), None)
        assert self._val(out, 201, "2024-10") == pytest.approx(273629.92)
        assert self._val(out, 201, "2024-11") == pytest.approx(1157954.92)

    def test_entrega_antes_da_vigencia_ainda_conta(self):
        # A abertura do raw: entrega cujo 5922 é anterior (invisível) tem que
        # continuar sendo receita, senão some dinheiro real. A 209 tem vigência
        # 2023-03 — antes disso a entrega conta (regra antiga), depois conta
        # a emissão.
        saidas = pd.DataFrame({
            "codi_emp": [209, 209],
            "dsai_sai": ["2023-01-10", "2023-04-20"],
            "vcon_sai": [5346.0, 4290.0],
            "codi_nat": [5117, 5922],
        })
        out = build_faturamento_mensal(saidas, self._entradas(209), None)
        assert self._val(out, 209, "2023-01") == pytest.approx(5346.0)
        assert self._val(out, 209, "2023-04") == pytest.approx(4290.0)

    def test_emissao_antes_da_vigencia_nao_conta(self):
        # O outro lado da mesma moeda: sob a regra antiga o 5922 é excluído.
        # Sem isto, o mês de abertura contaria as DUAS pernas do mesmo par.
        saidas = pd.DataFrame({
            "codi_emp": [209],
            "dsai_sai": ["2023-02-10"],
            "vcon_sai": [3730.0],
            "codi_nat": [5922],
        })
        out = build_faturamento_mensal(saidas, self._entradas(209), None)
        assert self._val(out, 209, "2023-02") == pytest.approx(0.0)

    def test_206_entrou_por_evidencia_da_origem(self):
        # A 206 tem emissão anterior à base, reconhecida como receita no ano
        # anterior. Logo a entrega de jan/23 NÃO é receita de 2023.
        saidas = pd.DataFrame({
            "codi_emp": [206, 206],
            "dsai_sai": ["2023-01-10", "2023-07-20"],
            "vcon_sai": [304717.62, 56673.60],
            "codi_nat": [6117, 6922],
        })
        out = build_faturamento_mensal(saidas, self._entradas(206), None)
        assert self._val(out, 206, "2023-01") == pytest.approx(0.0)
        assert self._val(out, 206, "2023-07") == pytest.approx(56673.60)

    def test_stateless_janela_nao_muda_o_mes(self):
        # Invariante arquitetural: março tem que dar o MESMO valor com e sem a nota
        # de novembro no frame. Se alguém introduzir pareamento entre meses,
        # este teste cai.
        cheio = pd.DataFrame({
            "codi_emp": [201, 201],
            "dsai_sai": ["2024-11-27", "2025-03-10"],
            "vcon_sai": [1157954.92, 135201.0],
            "codi_nat": [5922, 5117],
        })
        janela = cheio[cheio["dsai_sai"] >= "2025-01-01"]
        a = build_faturamento_mensal(cheio, self._entradas(), None)
        b = build_faturamento_mensal(janela, self._entradas(), None)
        assert (self._val(a, 201, "2025-03")
                == self._val(b, 201, "2025-03") == pytest.approx(0.0))

    def test_empresa_sem_familia_nao_e_afetada(self):
        # Regressão-zero: empresa fora do dict e sem CFOP da família.
        saidas = pd.DataFrame({
            "codi_emp": [203, 203],
            "dsai_sai": ["2025-01-10", "2025-01-11"],
            "vcon_sai": [1000.0, 500.0],
            "codi_nat": [5101, 5949],
        })
        out = build_faturamento_mensal(saidas, self._entradas(203), None)
        assert self._val(out, 203, "2025-01") == pytest.approx(1000.0)


# ── build_balancete_mensal (âncora: encerramento de exercício) ──────────────

class TestBuildBalanceteMensal:
    def test_encerramento_nao_zera_conta_de_resultado(self):
        contas = pd.DataFrame({
            "codi_emp": [1, 1, 1],
            "codi_cta": [10, 30, 90],
            "clas_cta": ["1010101", "3010101", "9000001"],
            "nome_cta": ["Caixa", "Receita Vendas", "ARE"],
        })
        # lancto 1: venda normal (orig_lan=1) deb Caixa / cre Receita.
        # lancto 2: ENCERRAMENTO (orig_lan=2) deb Receita / cre ARE — a perna
        # que toca conta de resultado deve ser EXCLUÍDA; a perna ARE fica.
        lan = pd.DataFrame({
            "codi_emp": [1, 1],
            "data_lan": ["2025-01-15", "2025-12-31"],
            "cdeb_lan": [10, 30],
            "ccre_lan": [30, 90],
            "vlor_lan": [100.0, 100.0],
            "orig_lan": [1, 2],
        })
        out = build_balancete_mensal(lan, contas)

        receita = out[out["clas_cta"] == "3010101"]
        assert receita["competencia"].tolist() == ["2025-01"]  # SEM linha em dez
        assert receita.iloc[0]["valo_saldo"] == pytest.approx(100.0)

        # Perna ARE do encerramento preservada (transferência pro PL intacta).
        are = out[out["clas_cta"] == "9000001"]
        assert are["competencia"].tolist() == ["2025-12"]
        assert are.iloc[0]["valo_cre"] == pytest.approx(100.0)

        # Sinal por natureza: ativo devedor positivo.
        caixa = out[out["clas_cta"] == "1010101"]
        assert caixa.iloc[0]["valo_saldo"] == pytest.approx(100.0)


# ── build_dre_mensal (resultado contabil do razao) ──────────────────────────

class TestBuildDreMensal:
    # Plano: Receita (grdre1, clas 30101), Deducao ICMS (grdre2,
    # clas 30102), CMV + Salarios (clas 4 despesa), Provisao IRPJ (grdre16 /
    # clas 40106), + contas de balanco (Caixa 10, Provisao a Pagar 20).
    def _contas(self, grdre=True):
        c = pd.DataFrame({
            "codi_emp": [1, 1, 1, 1, 1, 1, 1],
            "codi_cta": [10, 20, 224, 232, 242, 315, 357],
            "clas_cta": ["1010101", "2010101", "301010101", "301020104",
                         "401010101", "401030201", "401060101"],
            "nome_cta": ["Caixa", "Prov a Pagar", "Receita Mercadorias",
                         "(-) ICMS", "CMV", "Salarios", "Provisao IRPJ"],
            "grdre_cta": [None, None, 1, 2, 4, 6, 16],
        })
        return c.drop(columns=["grdre_cta"]) if not grdre else c

    def _lan(self):
        # jan: receita 1000 (cre 224), CMV 600 (deb 242), salario 200 (deb 315),
        #      deducao ICMS 100 (deb 232) -> resultado +100, sem provisao.
        # fev: receita 500 (cre 224), provisao IRPJ 80 (deb 357 / cre 20)
        #      -> resultado 420, lair 500, provisao 80.
        # mar: receita 100 (cre 224), CMV 300 (deb 242) -> resultado -200 (PREJUIZO).
        return pd.DataFrame({
            "codi_emp": [1] * 8,
            "data_lan": ["2025-01-10", "2025-01-11", "2025-01-12", "2025-01-13",
                         "2025-02-10", "2025-02-11", "2025-03-10", "2025-03-11"],
            "cdeb_lan": [10, 242, 315, 232, 10, 357, 10, 242],
            "ccre_lan": [224, 10, 10, 10, 224, 20, 224, 10],
            "vlor_lan": [1000.0, 600.0, 200.0, 100.0, 500.0, 80.0, 100.0, 300.0],
            "orig_lan": [1, 1, 1, 1, 1, 1, 1, 1],
        })

    def _row(self, out, comp):
        return out[out["competencia"] == comp].iloc[0]

    def test_resultado_liquido_sinal_receita_mais_despesa_menos(self):
        out = build_dre_mensal(self._lan(), self._contas())
        assert self._row(out, "2025-01")["resultado_liquido"] == pytest.approx(100.0)
        assert self._row(out, "2025-02")["resultado_liquido"] == pytest.approx(420.0)
        # mar: custo > receita -> PREJUIZO (o sinal que o proxy invertia)
        assert self._row(out, "2025-03")["resultado_liquido"] == pytest.approx(-200.0)

    def test_encerramento_orig_lan_2_excluido_do_resultado(self):
        lan = self._lan().copy()
        # dez: encerramento (orig_lan=2) deb Receita 224 / cre ARE — a perna de
        # resultado deve ser EXCLUIDA (nao zera a receita do ano).
        enc = pd.DataFrame({
            "codi_emp": [1], "data_lan": ["2025-12-31"], "cdeb_lan": [224],
            "ccre_lan": [20], "vlor_lan": [1600.0], "orig_lan": [2],
        })
        out = build_dre_mensal(pd.concat([lan, enc], ignore_index=True), self._contas())
        # dez nao gera linha de resultado (a unica perna que toca 3/4 foi filtrada)
        assert "2025-12" not in out["competencia"].tolist()
        # receita anual intacta = 1000+500+100 = 1600 (nao virou 0)
        assert out["receita_bruta"].sum() == pytest.approx(1600.0)

    def test_receita_bruta_via_grdre1(self):
        out = build_dre_mensal(self._lan(), self._contas())
        assert self._row(out, "2025-01")["receita_bruta"] == pytest.approx(1000.0)

    def test_receita_fallback_clas_30101_sem_grdre1_configurado(self):
        # empresa com coluna grdre presente porem sem grdre=1 -> fallback clas 30101
        contas = self._contas()
        contas["grdre_cta"] = None
        out = build_dre_mensal(self._lan(), contas)
        assert self._row(out, "2025-01")["receita_bruta"] == pytest.approx(1000.0)

    def test_grdre_cta_coluna_ausente_cai_no_fallback(self):
        out = build_dre_mensal(self._lan(), self._contas(grdre=False))
        assert self._row(out, "2025-01")["receita_bruta"] == pytest.approx(1000.0)
        assert self._row(out, "2025-01")["deducoes"] == pytest.approx(100.0)

    def test_lair_provisao_split(self):
        out = build_dre_mensal(self._lan(), self._contas())
        fev = self._row(out, "2025-02")
        assert fev["provisao_irpj_csll"] == pytest.approx(80.0)
        assert fev["lair"] == pytest.approx(500.0)
        # invariante: resultado_liquido = lair - provisao (provisao no mes lancado)
        assert fev["resultado_liquido"] == pytest.approx(fev["lair"] - fev["provisao_irpj_csll"])
        # jan sem provisao -> lair == resultado_liquido
        jan = self._row(out, "2025-01")
        assert jan["lair"] == pytest.approx(jan["resultado_liquido"])

    def test_despesas_trigger_clas4_excl_40106(self):
        out = build_dre_mensal(self._lan(), self._contas())
        # jan: CMV 600 + salario 200 = 800 (provisao 40106 fora)
        assert self._row(out, "2025-01")["despesas"] == pytest.approx(800.0)
        # fev: sem despesa clas 4 (so receita + provisao) -> 0 (empresa so-receita no mes)
        assert self._row(out, "2025-02")["despesas"] == pytest.approx(0.0)

    def test_uma_linha_por_emp_competencia_e_sem_mes_vazio(self):
        out = build_dre_mensal(self._lan(), self._contas())
        assert not out.duplicated(["codi_emp", "competencia"]).any()
        # so meses COM movimento de resultado (jan/fev/mar) — nenhum outro
        assert sorted(out["competencia"].tolist()) == ["2025-01", "2025-02", "2025-03"]

    def test_deducoes_e_receita_liquida(self):
        out = build_dre_mensal(self._lan(), self._contas())
        jan = self._row(out, "2025-01")
        assert jan["deducoes"] == pytest.approx(100.0)
        assert jan["receita_liquida"] == pytest.approx(900.0)  # 1000 - 100

    def test_multi_emp_grao_unico(self):
        lan = self._lan()
        lan2 = lan.copy()
        lan2["codi_emp"] = 2
        contas = self._contas()
        contas2 = contas.copy()
        contas2["codi_emp"] = 2
        out = build_dre_mensal(pd.concat([lan, lan2], ignore_index=True),
                               pd.concat([contas, contas2], ignore_index=True))
        assert not out.duplicated(["codi_emp", "competencia"]).any()
        assert set(out["codi_emp"]) == {1, 2}

    def test_classificacao_grdre_janela_independente(self):
        # Emenda: empresa configura grdre=1 no PLANO (conta 224), mas numa janela
        # de 1 mes so move uma conta de receita SEM tag grdre (225, clas 30101).
        # A classificacao segue o PLANO (usa grdre) -> 225 NAO e receita
        # operacional -> receita 0. Se emp_grdre1 viesse do movimento da janela,
        # cairia no fallback (225 conta como receita) e o mesmo mes gravaria
        # receita diferente entre cargas (drift do UPSERT).
        contas = pd.concat([self._contas(), pd.DataFrame({
            "codi_emp": [1], "codi_cta": [225], "clas_cta": ["301010199"],
            "nome_cta": ["Receita nao-operacional"], "grdre_cta": [None],
        })], ignore_index=True)
        lan = pd.DataFrame({
            "codi_emp": [1, 1],
            "data_lan": ["2025-07-10", "2025-07-11"],
            "cdeb_lan": [10, 242],       # 225 creditada (receita), CMV debitada
            "ccre_lan": [225, 10],
            "vlor_lan": [500.0, 300.0],
            "orig_lan": [1, 1],
        })
        out = build_dre_mensal(lan, contas)
        assert self._row(out, "2025-07")["receita_bruta"] == pytest.approx(0.0)

    def test_ctlancto_vazio_retorna_schema_completo(self):
        vazio = pd.DataFrame(columns=["codi_emp", "data_lan", "cdeb_lan",
                                      "ccre_lan", "vlor_lan", "orig_lan"])
        out = build_dre_mensal(vazio, self._contas())
        assert out.empty
        assert list(out.columns) == DRE_COLS


# ── saldo inicial (congela o contrato do saldo inicial) ─────────────────────

class TestBalanceteSaldoInicial:
    def _contas(self):
        return pd.DataFrame({
            "codi_emp": [1, 1, 1],
            "codi_cta": [10, 20, 30],
            "clas_cta": ["101010101", "301010101", "202010101"],
            "nome_cta": ["Caixa", "Receita", "Financiamento"],
        })

    def _lan(self):
        # jan: deb Caixa 100 / cre Receita; fev: deb Caixa 50 / cre Receita
        return pd.DataFrame({
            "codi_emp": [1, 1],
            "data_lan": ["2025-01-15", "2025-02-15"],
            "cdeb_lan": [10, 10],
            "ccre_lan": [20, 20],
            "vlor_lan": [100.0, 50.0],
        })

    def test_saldo_inicial_entra_no_cumsum_da_conta_de_balanco(self):
        si = pd.DataFrame({
            "codi_emp": [1], "codi_cta": [10],
            "valo_deb": [1000.0], "valo_cre": [200.0],
        })
        out = build_balancete_mensal(self._lan(), self._contas(), si)
        caixa = out[out["clas_cta"] == "101010101"].sort_values("competencia")
        # devedora: abertura (1000-200)=800; jan 800+100=900; fev 900+50=950
        assert caixa["valo_saldo"].tolist() == pytest.approx([900.0, 950.0])

    def test_conta_dormente_com_saldo_inicial_gera_phantom_row(self):
        si = pd.DataFrame({
            "codi_emp": [1], "codi_cta": [30],   # sem movimento na janela
            "valo_deb": [0.0], "valo_cre": [5000.0],
        })
        out = build_balancete_mensal(self._lan(), self._contas(), si)
        fin = out[out["clas_cta"] == "202010101"]
        assert len(fin) == 1
        assert fin["competencia"].iloc[0] == "2025-01"  # 1ª competência da empresa
        assert fin["valo_saldo"].iloc[0] == pytest.approx(5000.0)  # credora: (0-5000)*(-1)
        assert fin["valo_deb"].iloc[0] == 0.0 and fin["valo_cre"].iloc[0] == 0.0

    def test_saldo_inicial_nao_toca_conta_de_resultado(self):
        # si numa conta 3xx é ignorado (filtro prefixo 1/2 em transform.py)
        si = pd.DataFrame({
            "codi_emp": [1], "codi_cta": [20],
            "valo_deb": [0.0], "valo_cre": [9999.0],
        })
        out = build_balancete_mensal(self._lan(), self._contas(), si)
        receita = out[out["clas_cta"] == "301010101"].sort_values("competencia")
        assert receita["valo_saldo"].tolist() == pytest.approx([100.0, 50.0])  # só movimento mensal

    def test_conta_com_si_e_primeiro_movimento_tardio_ganha_phantom(self):
        # Financiamento tem si E só se move em fev —
        # phantom em jan (emp_first) com o si puro; linha real de fev intacta.
        lan = pd.DataFrame({
            "codi_emp": [1, 1],
            "data_lan": ["2025-01-15", "2025-02-15"],
            "cdeb_lan": [10, 10],
            "ccre_lan": [20, 30],   # fev credita o Financiamento (codi_cta 30)
            "vlor_lan": [100.0, 50.0],
        })
        si = pd.DataFrame({
            "codi_emp": [1], "codi_cta": [30],
            "valo_deb": [0.0], "valo_cre": [5000.0],
        })
        out = build_balancete_mensal(lan, self._contas(), si)
        fin = out[out["clas_cta"] == "202010101"].sort_values("competencia")
        assert fin["competencia"].tolist() == ["2025-01", "2025-02"]
        # phantom jan: si credor (0-5000)*(-1)=5000; fev real: 5000+50
        assert fin["valo_saldo"].tolist() == pytest.approx([5000.0, 5050.0])
        assert fin["valo_deb"].iloc[0] == 0.0 and fin["valo_cre"].iloc[0] == 0.0
        # grain único — phantom nunca colide com linha real (o grão do mart
        # é por codi_cta; clas_cta pode repetir entre contas irmãs)
        assert not out.duplicated(["codi_emp", "competencia", "codi_cta"]).any()

    def test_conta_com_si_e_movimento_no_primeiro_mes_nao_ganha_phantom(self):
        # Condição estrita acct_first > emp_first: quem já se move na 1ª
        # competência tem o si dentro do cumsum — phantom seria colisão.
        si = pd.DataFrame({
            "codi_emp": [1], "codi_cta": [10],
            "valo_deb": [1000.0], "valo_cre": [200.0],
        })
        out = build_balancete_mensal(self._lan(), self._contas(), si)
        caixa = out[out["clas_cta"] == "101010101"]
        assert len(caixa) == 2  # jan e fev reais, nenhuma phantom extra

    def test_conta_nova_sem_si_nao_ganha_phantom(self):
        # Conta que nasce no meio SEM saldo inicial: nenhuma linha sintética —
        # a série mostra zero antes do nascimento (fix do utils cuida disso).
        lan = pd.DataFrame({
            "codi_emp": [1, 1],
            "data_lan": ["2025-01-15", "2025-02-15"],
            "cdeb_lan": [10, 10],
            "ccre_lan": [20, 30],
            "vlor_lan": [100.0, 50.0],
        })
        out = build_balancete_mensal(lan, self._contas(), None)
        fin = out[out["clas_cta"] == "202010101"]
        assert fin["competencia"].tolist() == ["2025-02"]

    def test_si_abaixo_do_threshold_nao_gera_phantom(self):
        si = pd.DataFrame({
            "codi_emp": [1], "codi_cta": [30],
            "valo_deb": [0.0], "valo_cre": [0.005],   # |si| < 0.01
        })
        out = build_balancete_mensal(self._lan(), self._contas(), si)
        assert out[out["clas_cta"] == "202010101"].empty

    def test_vlor_lan_object_dtype_nao_crasha_cumsum(self):
        # Coluna de valor chegando como object dtype (ex.: Decimal do driver,
        # ou fatia all-NULL via read_sql) com `result` NAO-vazio: irmao nao
        # coberto do guard de `result` vazio. mov_mes
        # herda object -> cumsum("object") -> "cumsum is not supported for
        # object dtype". A coercao a numerico no topo mata o crash e mantem
        # o resultado IDENTICO ao path float.
        lan = self._lan().copy()
        lan["vlor_lan"] = lan["vlor_lan"].astype("object")
        si = pd.DataFrame({
            "codi_emp": [1], "codi_cta": [10],
            "valo_deb": [1000.0], "valo_cre": [200.0],
        })
        out = build_balancete_mensal(lan, self._contas(), si)
        caixa = out[out["clas_cta"] == "101010101"].sort_values("competencia")
        # mesmo resultado do path float: abertura (1000-200)=800; jan 900; fev 950
        assert caixa["valo_saldo"].tolist() == pytest.approx([900.0, 950.0])
        assert pd.api.types.is_float_dtype(out["valo_saldo"])

    def test_janela_sem_lancamento_nao_crasha_e_fica_fora_do_mart(self):
        # ctlancto vazio (empresa dormente na janela) + si presente (o si é
        # extraído SEMPRE) NÃO deve crashar. Há DOIS sítios de crash dependendo
        # do dtype do frame vazio, ambos a JUSANTE do `result` vazio:
        #   - origem devolve 0 linhas com colunas OBJECT -> cumsum object
        #   - frame tipado vazio (.iloc[0:0]) -> comparação str vs NaN na lógica
        #     de phantom
        # O guard de `result` vazio cobre os dois (força o guard CEDO, não um
        # band-aid só no cumsum). Sem linha real a phantom não tem como ancorar
        # (depende de emp_first, a 1ª competência COM movimento) → empresa fora
        # do mart por design; mas o frame vazio devolvido tem o MESMO schema do
        # output normal (senão um consumer do mart quebra calado).
        si = pd.DataFrame({
            "codi_emp": [1], "codi_cta": [10],
            "valo_deb": [1000.0], "valo_cre": [200.0],
        })
        normal = build_balancete_mensal(self._lan(), self._contas(), si)

        cols = ["codi_emp", "data_lan", "cdeb_lan", "ccre_lan", "vlor_lan"]
        lan_object = pd.DataFrame({c: pd.Series(dtype="object") for c in cols})
        lan_tipado = self._lan().iloc[0:0]

        for lan_vazio in (lan_object, lan_tipado):
            out = build_balancete_mensal(lan_vazio, self._contas(), si)
            assert out.empty
            assert set(out.columns) == set(normal.columns)

    def test_phantom_ancora_na_emp_first_de_cada_empresa(self):
        contas = pd.DataFrame({
            "codi_emp": [1, 1, 1, 2, 2, 2],
            "codi_cta": [10, 20, 30] * 2,
            "clas_cta": ["101010101", "301010101", "202010101"] * 2,
            "nome_cta": ["Caixa", "Receita", "Financiamento"] * 2,
        })
        lan = pd.DataFrame({
            "codi_emp": [1, 2],
            "data_lan": ["2025-01-15", "2025-03-15"],  # emp 2 começa em mar
            "cdeb_lan": [10, 10],
            "ccre_lan": [20, 20],
            "vlor_lan": [100.0, 100.0],
        })
        si = pd.DataFrame({
            "codi_emp": [1, 2], "codi_cta": [30, 30],
            "valo_deb": [0.0, 0.0], "valo_cre": [5000.0, 7000.0],
        })
        out = build_balancete_mensal(lan, contas, si)
        f1 = out[(out["codi_emp"] == 1) & (out["clas_cta"] == "202010101")]
        f2 = out[(out["codi_emp"] == 2) & (out["clas_cta"] == "202010101")]
        assert f1["competencia"].tolist() == ["2025-01"]
        assert f2["competencia"].tolist() == ["2025-03"]


# ── identidade da conta é codi_cta; clas_cta/nome_cta são atributos ────────
# Contas DISTINTAS co-classificadas na mesma clas_cta ("Rescisão" +
# "Serviços PJ" na 401020129) colapsavam no keep-last do load (subsoma em
# resultado) e contaminavam o cumsum umas das outras (patrimonial).

class TestBalanceteGraoCodiCta:
    def _contas(self):
        # 10/11: colisão patrimonial (Caixa); 30/31: colisão de balanço
        # credor (Financiamento); 40/41: colisão de resultado; 50/51: mesma
        # clas E mesmo nome ('Ferias').
        return pd.DataFrame({
            "codi_emp": [1] * 8,
            "codi_cta": [10, 11, 20, 30, 31, 40, 41, 50],
            "clas_cta": ["101010101", "101010101", "301010101",
                         "202010101", "202010101",
                         "401020129", "401020129", "402020200"],
            "nome_cta": ["Caixa Matriz", "Caixa Filial", "Receita",
                         "Financiamento A", "Financiamento B",
                         "Rescisao", "Servicos PJ", "Ferias"],
        })

    def test_grao_novo_codi_cta_na_saida_e_unico(self):
        lan = pd.DataFrame({
            "codi_emp": [1, 1],
            "data_lan": ["2025-01-15", "2025-01-20"],
            "cdeb_lan": [10, 11],
            "ccre_lan": [20, 20],
            "vlor_lan": [100.0, 50.0],
        })
        out = build_balancete_mensal(lan, self._contas(), None)
        assert "codi_cta" in out.columns
        assert not out.duplicated(["codi_emp", "competencia", "codi_cta"]).any()

    def test_resultado_co_classificadas_sobrevivem_ao_dedup_do_load(self):
        # O reproducer do colapso: o dedup do load usa MART_KEYS — com a
        # chave em clas_cta, keep-last descarta a co-classificada e a soma
        # da clas cai de 500 pra 200.
        from main import MART_KEYS
        lan = pd.DataFrame({
            "codi_emp": [1, 1],
            "data_lan": ["2025-01-15", "2025-01-20"],
            "cdeb_lan": [40, 41],
            "ccre_lan": [20, 20],
            "vlor_lan": [300.0, 200.0],
        })
        out = build_balancete_mensal(lan, self._contas(), None)
        dedup = out.drop_duplicates(
            subset=MART_KEYS["balancete_mensal"], keep="last")
        soma = dedup[dedup["clas_cta"] == "401020129"]["valo_saldo"].sum()
        assert soma == pytest.approx(500.0)

    def test_mesma_clas_mesmo_nome_vira_duas_linhas(self):
        # Duas contas com mesma clas e MESMO nome 'Ferias' — contas
        # distintas ficam linhas distintas; a soma da clas não muda.
        contas = self._contas().copy()
        contas.loc[len(contas)] = [1, 51, "402020200", "Ferias"]
        lan = pd.DataFrame({
            "codi_emp": [1, 1],
            "data_lan": ["2025-01-15", "2025-01-20"],
            "cdeb_lan": [50, 51],
            "ccre_lan": [20, 20],
            "vlor_lan": [100.0, 60.0],
        })
        out = build_balancete_mensal(lan, contas, None)
        ferias = out[out["clas_cta"] == "402020200"]
        assert len(ferias) == 2
        assert ferias["valo_saldo"].sum() == pytest.approx(160.0)

    def test_cumsum_patrimonial_por_conta_serie_propria(self):
        # Irmãs 1xx movendo em meses alternados: cada conta carrega o
        # PRÓPRIO cumulativo (hoje o cumsum por clas contamina a irmã:
        # a linha de fev da Filial viria 150 = 100 da Matriz + 50 dela).
        lan = pd.DataFrame({
            "codi_emp": [1, 1],
            "data_lan": ["2025-01-15", "2025-02-15"],
            "cdeb_lan": [10, 11],
            "ccre_lan": [20, 20],
            "vlor_lan": [100.0, 50.0],
        })
        out = build_balancete_mensal(lan, self._contas(), None)
        caixa = out[out["clas_cta"] == "101010101"].sort_values("competencia")
        assert caixa["valo_saldo"].tolist() == pytest.approx([100.0, 50.0])

    def test_si_por_conta_entra_no_proprio_cumsum(self):
        # si é por conta no raw ([codi_emp, codi_cta]) — cada cumsum parte
        # do PRÓPRIO si (hoje o si TOTAL da clas, 1000, entrava em ambas).
        lan = pd.DataFrame({
            "codi_emp": [1, 1],
            "data_lan": ["2025-01-15", "2025-02-15"],
            "cdeb_lan": [10, 11],
            "ccre_lan": [20, 20],
            "vlor_lan": [100.0, 50.0],
        })
        si = pd.DataFrame({
            "codi_emp": [1, 1], "codi_cta": [10, 11],
            "valo_deb": [800.0, 200.0], "valo_cre": [0.0, 0.0],
        })
        out = build_balancete_mensal(lan, self._contas(), si)
        caixa = out[out["clas_cta"] == "101010101"]
        matriz = caixa[caixa["nome_cta"] == "Caixa Matriz"]
        filial = caixa[caixa["nome_cta"] == "Caixa Filial"].sort_values("competencia")
        # Matriz jan: 800 + 100 = 900
        assert matriz["valo_saldo"].tolist() == pytest.approx([900.0])
        # Filial: 1º movimento tardio -> phantom POR CONTA em jan (si
        # 200, movimento zero); fev real 200 + 50 = 250. As-of da clas
        # preservado: jan 900+200=1100, fev 900+250=1150 (mundo antigo).
        assert filial["competencia"].tolist() == ["2025-01", "2025-02"]
        assert filial["valo_saldo"].tolist() == pytest.approx([200.0, 250.0])
        assert filial[["valo_deb", "valo_cre"]].iloc[0].tolist() == [0.0, 0.0]

    def test_phantom_per_codi_irma_dormente_preserva_agregado(self):
        # A ativa desde jan (si 1000 + mov 50); B dormente com si 5000.
        # Hoje o si da clas (6000) pega carona nas linhas da A e a B não
        # existe no mart. Com grão por conta, B ganha phantom própria e o
        # agregado as-of da clas se preserva (6050 em jan).
        lan = pd.DataFrame({
            "codi_emp": [1],
            "data_lan": ["2025-01-15"],
            "cdeb_lan": [10],
            "ccre_lan": [30],
            "vlor_lan": [50.0],
        })
        si = pd.DataFrame({
            "codi_emp": [1, 1], "codi_cta": [30, 31],
            "valo_deb": [0.0, 0.0], "valo_cre": [1000.0, 5000.0],
        })
        out = build_balancete_mensal(lan, self._contas(), si)
        fin = out[out["clas_cta"] == "202010101"]
        assert len(fin) == 2
        b = fin[fin["nome_cta"] == "Financiamento B"]
        assert b["competencia"].iloc[0] == "2025-01"
        assert b["valo_saldo"].iloc[0] == pytest.approx(5000.0)
        assert b["valo_deb"].iloc[0] == 0.0 and b["valo_cre"].iloc[0] == 0.0
        a = fin[fin["nome_cta"] == "Financiamento A"]
        assert a["valo_saldo"].iloc[0] == pytest.approx(1050.0)
        assert fin["valo_saldo"].sum() == pytest.approx(6050.0)

    def test_codi_cta_nunca_nulo_no_output(self):
        # codi_cta NULL na chave do mart = ON CONFLICT nunca casa (NULL !=
        # NULL) -> phantom re-inserida a cada run. Phantom incluída no check.
        lan = pd.DataFrame({
            "codi_emp": [1],
            "data_lan": ["2025-01-15"],
            "cdeb_lan": [10],
            "ccre_lan": [20],
            "vlor_lan": [50.0],
        })
        si = pd.DataFrame({
            "codi_emp": [1], "codi_cta": [31],
            "valo_deb": [0.0], "valo_cre": [5000.0],
        })
        out = build_balancete_mensal(lan, self._contas(), si)
        assert "codi_cta" in out.columns
        assert out["codi_cta"].notna().all()

    def test_clas_cta_nan_continua_dropada(self):
        # Congela o dropna do groupby: lançamento em conta órfã (codi_cta
        # fora do plano -> clas NaN no merge left) segue FORA do balancete.
        lan = pd.DataFrame({
            "codi_emp": [1, 1],
            "data_lan": ["2025-01-15", "2025-01-20"],
            "cdeb_lan": [10, 99],   # 99 não existe em ctcontas
            "ccre_lan": [20, 20],
            "vlor_lan": [100.0, 77.0],
        })
        out = build_balancete_mensal(lan, self._contas(), None)
        assert out["clas_cta"].notna().all()
        # o lado órfão (deb 77) sumiu; o crédito da Receita levou os dois
        assert out[out["clas_cta"] == "101010101"]["valo_deb"].sum() == pytest.approx(100.0)


# ── build_funcionarios_mensal (âncoras: headcount tp=11; fluxo
#    folha-ancorado — entradas/saidas por presenca) ─────────────────────────

class TestBuildFuncionariosMensal:
    def test_headcount_tp11_e_fluxo_por_presenca(self):
        # 102 so ENTRA na folha em mar — na semantica velha
        # (evento de admissao) o fluxo contaria em outro mes e este teste
        # FALHA; na convencao B conta quando aparece na folha.
        fobases = pd.DataFrame({
            "codi_emp": [1, 1, 1, 1],
            "competencia": ["2025-01-01", "2025-02-01", "2025-03-01", "2025-03-01"],
            "tipo_process": [11, 11, 11, 11],
            "i_empregados": [101, 101, 101, 102],
        })
        out = build_funcionarios_mensal(fobases, pd.DataFrame(), "2025-01", "2025-03")
        assert out["competencia"].tolist() == ["2025-01", "2025-02", "2025-03"]
        jan, fev, mar = out.iloc[0], out.iloc[1], out.iloc[2]
        # serie nasce em jan: 101 e ENTRADA (0+1-0=1)
        assert (jan["qtd_inicial"], jan["qtd_admissoes"], jan["qtd_rescisoes"], jan["qtd_final"]) == (0, 1, 0, 1)
        assert (fev["qtd_admissoes"], fev["qtd_rescisoes"]) == (0, 0)
        # mar: 102 entra NA FOLHA (nao no mes do evento de cadastro)
        assert (mar["qtd_inicial"], mar["qtd_admissoes"], mar["qtd_rescisoes"], mar["qtd_final"]) == (1, 1, 0, 2)

    def test_tp60_ferias_nao_e_presenca(self):
        # tp=60 (ferias) nao conta presenca: mes so-tp60 nao
        # gera linha nem "saida fantasma" retroativa.
        fobases = pd.DataFrame({
            "codi_emp": [1, 1],
            "competencia": ["2025-01-01", "2025-02-01"],
            "tipo_process": [11, 60],
            "i_empregados": [101, 101],
        })
        out = build_funcionarios_mensal(fobases, pd.DataFrame(), "2025-01", "2025-02")
        assert out["competencia"].tolist() == ["2025-01"]


class TestBuildFuncionariosMensalEntradasSaidas:
    """Convencao B (folha-ancorada): qtd_admissoes/qtd_rescisoes =
    ENTRADAS/SAIDAS da presenca na folha (set-diff vs mes anterior DO
    HISTORICO — a MESMA ancora do qtd_inicial). Identidade
    Inicial + Entradas − Saidas == Final fecha POR CONSTRUCAO em toda linha
    emitida (buraco, nascimento de serie, borda de janela)."""

    def _fb(self, rows, emp=1):  # rows: list[(competencia, i_emp)]
        return pd.DataFrame({
            "codi_emp": [emp] * len(rows),
            "competencia": [c for c, _ in rows],
            "tipo_process": [11] * len(rows),
            "i_empregados": [i for _, i in rows],
        })

    def _row(self, out, comp):
        return out[out["competencia"] == comp].iloc[0]

    def test_entrada_e_saida_basicas(self):
        fb = self._fb([("2025-01-01", 1), ("2025-01-01", 2),
                       ("2025-02-01", 2), ("2025-02-01", 3)])
        out = build_funcionarios_mensal(fb, pd.DataFrame(), "2025-01", "2025-02")
        fev = self._row(out, "2025-02")
        assert (fev["qtd_inicial"], fev["qtd_admissoes"], fev["qtd_rescisoes"], fev["qtd_final"]) == (2, 1, 1, 2)

    def test_demitido_fim_de_mes(self):
        # dez: 15 pessoas; jan: +42/+43 (demitido dia 20/jan AINDA na folha de
        # jan); fev: sai o i_emp 4. AMBAS as linhas fecham — o -1 do demitido
        # materializa em FEV (quando sai do headcount), nao no mes do evento.
        dez = [("2024-12-01", i) for i in range(1, 16)]
        jan = [("2025-01-01", i) for i in range(1, 16)] + [("2025-01-01", 42), ("2025-01-01", 43)]
        fev = [("2025-02-01", i) for i in range(1, 16) if i != 4] + [("2025-02-01", 42), ("2025-02-01", 43)]
        out = build_funcionarios_mensal(self._fb(dez + jan + fev), pd.DataFrame(), "2025-01", "2025-02")
        j, f = self._row(out, "2025-01"), self._row(out, "2025-02")
        assert (j["qtd_inicial"], j["qtd_admissoes"], j["qtd_rescisoes"], j["qtd_final"]) == (15, 2, 0, 17)
        assert (f["qtd_inicial"], f["qtd_admissoes"], f["qtd_rescisoes"], f["qtd_final"]) == (17, 0, 1, 16)

    def test_serie_nova_entradas_cheias(self):
        fb = self._fb([("2025-01-01", i) for i in range(1, 28)])
        out = build_funcionarios_mensal(fb, pd.DataFrame(), "2025-01", "2025-01")
        jan = self._row(out, "2025-01")
        assert (jan["qtd_inicial"], jan["qtd_admissoes"], jan["qtd_rescisoes"], jan["qtd_final"]) == (0, 27, 0, 27)

    def test_reentrada_afastamento_conta_saida_e_entrada(self):
        # presente jan, some fev (afastamento), volta mar: fev sai=1, mar
        # ent=1 — movimento de FOLHA legitimo (rota a), e tudo fecha.
        fb = self._fb([("2025-01-01", 1), ("2025-01-01", 2),
                       ("2025-02-01", 1),
                       ("2025-03-01", 1), ("2025-03-01", 2)])
        out = build_funcionarios_mensal(fb, pd.DataFrame(), "2025-01", "2025-03")
        fev, mar = self._row(out, "2025-02"), self._row(out, "2025-03")
        assert (fev["qtd_rescisoes"], fev["qtd_final"]) == (1, 1)
        assert (mar["qtd_admissoes"], mar["qtd_final"]) == (1, 2)
        assert ((out["qtd_inicial"] + out["qtd_admissoes"] - out["qtd_rescisoes"]) == out["qtd_final"]).all()

    def test_buraco_mid_series_fecha_contra_prev_do_historico(self):
        # sem folha em fev: NENHUMA linha de fev; mar diffs contra JAN (prev
        # do HISTORICO, a mesma ancora do qtd_inicial) e fecha.
        fb = self._fb([("2025-01-01", 1), ("2025-01-01", 2),
                       ("2025-03-01", 2), ("2025-03-01", 3)])
        out = build_funcionarios_mensal(fb, pd.DataFrame(), "2025-01", "2025-03")
        assert "2025-02" not in out["competencia"].tolist()
        mar = self._row(out, "2025-03")
        assert (mar["qtd_inicial"], mar["qtd_admissoes"], mar["qtd_rescisoes"], mar["qtd_final"]) == (2, 1, 1, 2)

    def test_property_identidade_e_sem_linha_morta(self):
        # fixture baguncada multi-emp (reentrada, buraco, NaN de i_empregados
        # — nunique dropava NaN, o set tambem tem que dropar).
        fb = pd.concat([
            self._fb([("2025-01-01", 1), ("2025-01-01", 2), ("2025-02-01", 2),
                      ("2025-02-01", 3), ("2025-04-01", 3), ("2025-04-01", 9)]),
            self._fb([("2025-02-01", 7), ("2025-03-01", 7), ("2025-03-01", 8)], emp=2),
        ], ignore_index=True)
        fb.loc[len(fb)] = {"codi_emp": 1, "competencia": "2025-01-01",
                           "tipo_process": 11, "i_empregados": None}
        out = build_funcionarios_mensal(fb, pd.DataFrame(), "2025-01", "2025-04")
        assert ((out["qtd_inicial"] + out["qtd_admissoes"] - out["qtd_rescisoes"]) == out["qtd_final"]).all()
        assert not ((out["qtd_inicial"] == 0) & (out["qtd_final"] == 0)).any()
        jan1 = out[(out["codi_emp"] == 1) & (out["competencia"] == "2025-01")].iloc[0]
        assert jan1["qtd_final"] == 2           # NaN dropado

    def test_fobases_vazio_mart_vazio_com_contrato(self):
        out = build_funcionarios_mensal(pd.DataFrame(), pd.DataFrame(), "2025-01", "2025-02")
        assert out.empty
        assert list(out.columns) == ["codi_emp", "competencia", "qtd_inicial",
                                     "qtd_admissoes", "qtd_rescisoes",
                                     "qtd_final", "is_partial"]


class TestBuildFuncionariosMensalOverlapJanela:
    """Pina o contrato do build de que a orquestração depende: mês
    pré-`inicio` presente no frame ancora o shift de qtd_inicial (E o
    set-diff de entradas/saidas) e NUNCA vaza pro output (janela
    [inicio, fim] apara)."""

    def test_mes_pre_inicio_ancora_qtd_inicial_e_nao_vaza(self):
        fobases = pd.DataFrame({
            "codi_emp": [1, 1, 1, 1, 1],
            "competencia": ["2024-12-01", "2024-12-01",
                            "2025-01-01", "2025-01-01", "2025-01-01"],
            "tipo_process": [11] * 5,
            "i_empregados": [101, 102, 101, 102, 103],
        })
        out = build_funcionarios_mensal(fobases, pd.DataFrame(), "2025-01", "2025-02")
        jan = out[out["competencia"] == "2025-01"].iloc[0]
        assert jan["qtd_inicial"] == 2          # qtd_final de dez/24
        assert jan["qtd_final"] == 3
        # ent/sai ancoram no MESMO N-1 do shift — jan tem
        # ENT=1 (so o 103), NUNCA 3 (que seria diff contra conjunto vazio).
        assert (jan["qtd_admissoes"], jan["qtd_rescisoes"]) == (1, 0)
        assert "2024-12" not in out["competencia"].tolist()

    def test_primeiro_mes_da_historia_inicial_zero(self):
        fobases = pd.DataFrame({
            "codi_emp": [1, 1],
            "competencia": ["2025-01-01", "2025-01-01"],
            "tipo_process": [11, 11],
            "i_empregados": [101, 102],
        })
        out = build_funcionarios_mensal(fobases, pd.DataFrame(), "2025-01", "2025-02")
        jan = out[out["competencia"] == "2025-01"].iloc[0]
        assert jan["qtd_inicial"] == 0          # sem N−1: serie nasce
        # e a linha FECHA: 0 + 2 entradas - 0 = 2
        assert (jan["qtd_admissoes"], jan["qtd_final"]) == (2, 2)

    def test_overlap_com_demissao_no_mes_n_menos_1(self):
        # a demissao do mes de OVERLAP (dez) tambem entra no frame do build
        # -> ativos(dez) ja liquido -> jan ancora em len(ativos dez)=1 SEM
        # saida-fantasma. Sem o registro do overlap, jan mostraria ini=2/sai=1.
        fobases = pd.DataFrame({
            "codi_emp": [1, 1, 1, 1],
            "competencia": ["2024-12-01", "2024-12-01", "2025-01-01", "2025-01-01"],
            "tipo_process": [11] * 4,
            "i_empregados": [101, 102, 102, 103],
        })
        foresc = pd.DataFrame({
            "codi_emp": [1], "i_calculos": ["g0"], "i_empregados": [101],
            "demissao": ["2024-12-28"], "tipo": [1],
        })
        out = build_funcionarios_mensal(fobases, foresc, "2025-01", "2025-02")
        jan = out[out["competencia"] == "2025-01"].iloc[0]
        assert (jan["qtd_inicial"], jan["qtd_admissoes"], jan["qtd_rescisoes"], jan["qtd_final"]) == (1, 1, 0, 2)
        assert "2024-12" not in out["competencia"].tolist()


# ── build_empresa_regime_periodo (âncora: cascata de detecção) ──────────────

class TestBuildEmpresaRegimePeriodo:
    def test_cascata_das_outros_balancete(self):
        ef = pd.DataFrame({
            "codi_emp": [1, 1],
            "codi_imp": [44, 1],
            "data_sim": ["2025-01-10", "2025-02-10"],
        })
        bal = pd.DataFrame({
            "codi_emp": [2], "competencia": ["2025-01"],
            "clas_cta": ["3010201001"], "valo_deb": [10.0], "valo_cre": [0.0],
        })
        out = build_empresa_regime_periodo(ef, bal)
        got = {
            (r.codi_emp, r.competencia): (r.regime, r.fonte_deteccao)
            for r in out.itertuples()
        }
        assert got[(1, "2025-01")] == ("simples", "efsdoimp_44")
        assert got[(1, "2025-02")] == ("real_presumido", "efsdoimp_outros")
        assert got[(2, "2025-01")] == ("real_presumido", "balancete_inferido")


# ── build_impostos_mensais (âncora: slots de apuração somam) ────────────────

class TestBuildImpostosMensaisSlots:
    """O build SOMA os slots de apuração (pdic_sim) do mesmo trio.
    O groupby por (emp, comp, codi_imp) já somava por design.
    Slots zerados somam R$ 0 (o 'dedup de idênticas' acontece por efeito);
    qtd_docs = nº de slots (sem consumidor hoje; semântica no docstring)."""

    def test_slots_do_mesmo_trio_somam(self):
        # valor no 3º decêndio, slot mensal zerado.
        raw = pd.DataFrame({
            "codi_emp": [1, 1, 1, 1],
            "codi_imp": [16, 16, 16, 16],
            "data_sim": ["2025-06-01"] * 4,
            "pdic_sim": [1, 2, 3, 6],
            "srec_sim": [0.0, 0.0, 12750.0, 0.0],
            "bcal_sim": [0.0, 0.0, 86234.56, 0.0],
        })
        out = build_impostos_mensais(raw)
        assert len(out) == 1
        row = out.iloc[0]
        assert row["competencia"] == "2025-06"
        assert row["srec_sim"] == 12750.0
        assert row["bcal_sim"] == 86234.56
        assert row["qtd_docs"] == 4

    def test_quinzenas_de_valor_igual_nao_colapsam(self):
        # 2 guias reais de mesmo valor (quinzenas idênticas, Lei 10.833)
        # SOMAM — exatamente o caso que um dedup-por-valor destruiria.
        raw = pd.DataFrame({
            "codi_emp": [1, 1], "codi_imp": [25, 25],
            "data_sim": ["2025-06-01"] * 2, "pdic_sim": [4, 5],
            "srec_sim": [500.0, 500.0], "bcal_sim": [10000.0, 10000.0],
        })
        out = build_impostos_mensais(raw)
        assert out.iloc[0]["srec_sim"] == 1000.0
        assert out.iloc[0]["bcal_sim"] == 20000.0


class TestImpostosAdicionalIrpj:
    """IRPJ exibido = principal + adicional (inc1_sim).

    Exemplo trimestral: 15% x 600.000,00 = 90.000,00 de principal e
    10% x (600.000,00 - 60.000) = 54.000,00 de adicional.
    """

    def test_devido_soma_o_adicional(self):
        raw = pd.DataFrame({
            "codi_emp": [201], "data_sim": ["2025-03-01"], "pdic_sim": [7],
            "codi_imp": [7], "srec_sim": [90000.00], "bcal_sim": [600000.00],
            "inc1_sim": [54000.00],
        })
        r = build_impostos_mensais(raw).iloc[0]
        assert r["srec_sim"] == pytest.approx(90000.00)       # principal INTACTO
        assert r["srec_adicional"] == pytest.approx(54000.00)
        assert r["valo_devido"] == pytest.approx(144000.00)

    def test_sem_a_coluna_degrada_pro_principal(self):
        # Raw anterior ao contrato novo: devido = principal, sem crash.
        raw = pd.DataFrame({
            "codi_emp": [201], "data_sim": ["2025-03-01"], "pdic_sim": [7],
            "codi_imp": [7], "srec_sim": [90000.00], "bcal_sim": [600000.00],
        })
        r = build_impostos_mensais(raw).iloc[0]
        assert r["srec_adicional"] == pytest.approx(0.0)
        assert r["valo_devido"] == pytest.approx(90000.00)

    def test_imposto_sem_adicional_nao_se_move(self):
        # CSLL não tem adicional na lei, e o dado reflete.
        raw = pd.DataFrame({
            "codi_emp": [201], "data_sim": ["2025-03-01"], "pdic_sim": [7],
            "codi_imp": [6], "srec_sim": [79210.48], "bcal_sim": [880116.45],
            "inc1_sim": [0.0],
        })
        r = build_impostos_mensais(raw).iloc[0]
        assert r["valo_devido"] == pytest.approx(79210.48)

    def test_slots_somam_com_o_adicional(self):
        # pdic_sim é SLOT de sub-período; os slots somam e o adicional
        # acompanha, sem tratamento especial.
        raw = pd.DataFrame({
            "codi_emp": [1, 1], "data_sim": ["2025-03-01"] * 2,
            "pdic_sim": [1, 2], "codi_imp": [7, 7],
            "srec_sim": [100.0, 200.0], "bcal_sim": [0.0, 0.0],
            "inc1_sim": [10.0, 20.0],
        })
        out = build_impostos_mensais(raw)
        assert len(out) == 1
        assert out.iloc[0]["valo_devido"] == pytest.approx(330.0)

    def test_inc1_nulo_nao_contamina_o_devido(self):
        # NaN no raw viraria NaN no devido e apagaria a linha inteira da tela.
        raw = pd.DataFrame({
            "codi_emp": [1], "data_sim": ["2025-03-01"], "pdic_sim": [7],
            "codi_imp": [7], "srec_sim": [100.0], "bcal_sim": [0.0],
            "inc1_sim": [None],
        })
        r = build_impostos_mensais(raw).iloc[0]
        assert r["valo_devido"] == pytest.approx(100.0)


# ── build_impostos_destacados_mensais (âncora: fonte híbrida) ───────────────

class TestBuildImpostosDestacadosMensais:
    def test_fontes_balancete_efsdoimp_sem_deducao(self):
        regime = pd.DataFrame({
            "codi_emp": [1, 2, 3],
            "competencia": ["2025-01"] * 3,
            "regime": ["real_presumido", "simples", "real_presumido"],
        })
        bal = pd.DataFrame({
            "codi_emp": [1, 1, 3],
            "competencia": ["2025-01"] * 3,
            "clas_cta": ["3010201001", "3010101001", "3010101001"],
            "nome_cta": ["(-) ICMS s/ Vendas", "Receita Industria", "Receita Industria"],
            "valo_deb": [50.0, 0.0, 0.0],
            "valo_cre": [0.0, 1000.0, 500.0],
        })
        imp = pd.DataFrame({
            "codi_emp": [2], "competencia": ["2025-01"],
            "codi_imp": [44], "srec_sim": [30.0], "bcal_sim": [300.0],
        })
        fat = pd.DataFrame({
            "codi_emp": [2], "competencia": ["2025-01"],
            "valo_saidas": [200.0], "valo_servicos": [100.0],
        })
        out = build_impostos_destacados_mensais(bal, imp, regime, fat)
        by_emp = {r.codi_emp: r for r in out.itertuples()}

        # R/P com dedução escriturada -> balancete (pct é FRAÇÃO, não %).
        assert by_emp[1].fonte == "balancete"
        assert by_emp[1].pct_impostos == pytest.approx(0.05)
        # Simples -> efsdoimp; denominador = saidas + servicos.
        assert by_emp[2].fonte == "efsdoimp"
        assert by_emp[2].pct_impostos == pytest.approx(0.10)
        # R/P sem dedução mas com receita -> 'sem_deducao' (NaN, UX mostra '—').
        # Renomeado de 'isenta': o rótulo antigo dizia "imune/isenta" e
        # era aplicado também a quem PAGA imposto sem escriturar a dedução.
        assert by_emp[3].fonte == "sem_deducao"
        assert math.isnan(by_emp[3].valo_impostos)
        assert by_emp[3].base_receita == pytest.approx(500.0)

    # ── retenção de terceiros fora do numerador Simples ─────────────────────

    @staticmethod
    def _simples(imp, fat_saidas=200.0, fat_servicos=100.0, emp=2, comp="2025-01"):
        regime = pd.DataFrame({
            "codi_emp": [emp], "competencia": [comp], "regime": ["simples"],
        })
        fat = pd.DataFrame({
            "codi_emp": [emp], "competencia": [comp],
            "valo_saidas": [fat_saidas], "valo_servicos": [fat_servicos],
        })
        out = build_impostos_destacados_mensais(pd.DataFrame(), imp, regime, fat)
        assert len(out) == 1 and out.iloc[0]["fonte"] == "efsdoimp"
        return out.iloc[0]

    def test_simples_exclui_retencoes_de_terceiros_do_numerador(self):
        # Guia de retenção FEITA (ISS Ret 18 / INSS Ret 26) é imposto de
        # TERCEIROS que a empresa recolhe como tomadora — não é carga dela.
        imp = pd.DataFrame({
            "codi_emp": [2] * 3, "competencia": ["2025-01"] * 3,
            "codi_imp": [44, 18, 26],
            "srec_sim": [30.0, 50.0, 20.0],
            "bcal_sim": [300.0, 500.0, 200.0],
        })
        row = self._simples(imp)
        assert row["valo_impostos"] == pytest.approx(30.0)      # só o DAS
        assert row["pct_impostos"] == pytest.approx(0.10)

    def test_simples_retido_dominante_vira_plausivel(self):
        # retido >> DAS com faturamento baixo dava >100% de "% Impostos" —
        # impossível.
        imp = pd.DataFrame({
            "codi_emp": [7] * 3, "competencia": ["2025-12"] * 3,
            "codi_imp": [44, 26, 18],
            "srec_sim": [2000.0, 61176.50, 31154.25],
            "bcal_sim": [20000.0, 556150.0, 622000.0],
        })
        row = self._simples(imp, fat_saidas=80000.0, fat_servicos=0.0,
                            emp=7, comp="2025-12")
        # antes do fix: 94.330,75/80.000 = 117,9%; depois: 2.000/80.000.
        assert row["valo_impostos"] == pytest.approx(2000.0)
        assert row["pct_impostos"] == pytest.approx(0.025)

    def test_simples_so_das_e_icms_proprio_mantidos(self):
        # Empresa só-DAS + ICMS antecipação (codi_imp=1): imposto PRÓPRIO
        # permanece — a exclusão é só de retenção.
        imp = pd.DataFrame({
            "codi_emp": [2] * 2, "competencia": ["2025-01"] * 2,
            "codi_imp": [44, 1],
            "srec_sim": [30.0, 10.0],
            "bcal_sim": [300.0, 100.0],
        })
        row = self._simples(imp)
        assert row["valo_impostos"] == pytest.approx(40.0)
        assert row["pct_impostos"] == pytest.approx(40.0 / 300.0)

    def test_simples_todos_codigos_da_constante_excluidos(self):
        # Varre CODI_IMP_RETENCAO_TERCEIROS inteira: cada código com valor
        # junto do DAS -> numerador = só o DAS.
        codes = sorted(CODI_IMP_RETENCAO_TERCEIROS)
        n = len(codes)
        imp = pd.DataFrame({
            "codi_emp": [2] * (n + 1), "competencia": ["2025-01"] * (n + 1),
            "codi_imp": codes + [44],
            "srec_sim": [10.0] * n + [30.0],
            "bcal_sim": [100.0] * (n + 1),
        })
        row = self._simples(imp)
        assert row["valo_impostos"] == pytest.approx(30.0)

    def test_real_presumido_imune_a_retidos_no_efsdoimp(self):
        # R/P numera pelo balancete; retidos em efsdoimp não vazam pro card.
        regime = pd.DataFrame({
            "codi_emp": [204], "competencia": ["2025-01"],
            "regime": ["real_presumido"],
        })
        bal = pd.DataFrame({
            "codi_emp": [204] * 2, "competencia": ["2025-01"] * 2,
            "clas_cta": ["3010201001", "3010101001"],
            "nome_cta": ["(-) ICMS s/ Vendas", "Receita Industria"],
            "valo_deb": [50.0, 0.0],
            "valo_cre": [0.0, 1000.0],
        })
        imp = pd.DataFrame({
            "codi_emp": [204] * 2, "competencia": ["2025-01"] * 2,
            "codi_imp": [63, 16], "srec_sim": [999.0, 111.0],
            "bcal_sim": [0.0, 0.0],
        })
        fat = pd.DataFrame({
            "codi_emp": [204], "competencia": ["2025-01"],
            "valo_saidas": [2000.0], "valo_servicos": [0.0],
        })
        out = build_impostos_destacados_mensais(bal, imp, regime, fat)
        row = out.iloc[0]
        assert row["fonte"] == "balancete"
        assert row["valo_impostos"] == pytest.approx(50.0)
        assert row["pct_impostos"] == pytest.approx(0.05)


# ── _mask_prefixo / _norm_chaves_bal (ancora: extracao mecanica) ───────────
# Prova de COMPORTAMENTO-ZERO: o helper produz output IDENTICO ao idioma que as
# 3 copias do balancete faziam inline (extracao sem mudanca de comportamento).

class TestMaskPrefixo:
    @staticmethod
    def _idiom_antigo(series, prefixos):
        # replica EXATA das 3 copias inline: pd.Series(False) + for | startswith
        mask = pd.Series(False, index=series.index)
        for p in prefixos:
            mask = mask | series.str.startswith(p)
        return mask

    # inclui os prefixos REAIS dos 3 call-sites (DEDUCAO/RECEITA) + bordas
    @pytest.mark.parametrize("prefixos", [
        PREFIXOS_DEDUCAO,
        PREFIXOS_RECEITA,
        ("301", "4"),
        ("3010201",),
        (),                       # vazio -> tudo False (for nao roda)
    ])
    def test_equivale_ao_idioma_antigo(self, prefixos):
        s = pd.Series(["3010201", "3010202", "401", "1010102", "30102", "2010"]).astype(str)
        novo = _mask_prefixo(s, prefixos)
        pd.testing.assert_series_equal(novo, self._idiom_antigo(s, prefixos))

    def test_index_de_series_preservado(self):
        # os 3 call-sites passam clas = mart_balancete["clas_cta"].astype(str),
        # cujo index E o do mart_balancete -> helper usa series.index (identico).
        s = pd.Series(["301", "999"], index=[7, 42]).astype(str)
        m = _mask_prefixo(s, ("301",))
        assert list(m.index) == [7, 42]
        assert m.tolist() == [True, False]


class TestNormChavesBal:
    @staticmethod
    def _idiom_antigo(df):
        df["codi_emp"] = df["codi_emp"].astype("int64")
        df["clas_cta"] = df["clas_cta"].astype(str)
        return df

    def test_equivale_ao_idioma_antigo(self):
        base = pd.DataFrame({"codi_emp": [202.0, 12.0],
                             "clas_cta": [3010201, 1010102], "x": [1.5, 2.5]})
        novo = _norm_chaves_bal(base.copy())
        antigo = self._idiom_antigo(base.copy())
        pd.testing.assert_frame_equal(novo, antigo)

    def test_muta_in_place_e_devolve_o_mesmo_objeto(self):
        df = pd.DataFrame({"codi_emp": [202.0], "clas_cta": [3010201]})
        ret = _norm_chaves_bal(df)
        assert ret is df                          # in-place: devolve o MESMO objeto
        assert df["codi_emp"].dtype == "int64"
        assert pd.api.types.is_string_dtype(df["clas_cta"])   # str (object/StringDtype)
        assert df["clas_cta"].iloc[0] == "3010201"


# ── build_encargos_mensais (encargo fiel a GUIA) ────────────────────────────

class TestBuildEncargosMensais:
    def _guias(self):
        inss = pd.DataFrame({
            "codi_emp":     [1, 1, 1, 2],
            "i_guiainss":   [10, 11, 12, 13],
            "competencia":  ["2025-01-01", "2025-01-01", "2025-12-01", "2025-01-01"],
            "tipo_process": [11, 11, 52, 11],
            "empresa":      [100.0, 50.0, 999.0, 0.0],   # emp 2 = Simples (zero REAL)
            # a deducao de salario familia vive em coluna SEPARADA —
            # o `empresa` acima e BRUTO. A emp 2 tem patronal 0,00 e compensacao
            # 60,00 (o caso que impede abater dentro do inss_empresa).
            "deducoes_comp_sf":            [7.0, 3.0, 4.0, 60.0],
            "deducoes_comp_sf_retroativo": [1.5, 0.0, 0.0, 0.0],
        })
        fgts = pd.DataFrame({
            "codi_emp":     [1, 1],
            "i_fgtsfilial": [20, 21],
            "competencia":  ["2025-01-01", "2025-01-01"],
            "tipo_process": [11, 52],
            "total_fgts":   [80.0, 777.0],
        })
        # A GRFC separa BASE de VALOR: `saldo_fins_resc` e a base
        # da multa de 40% — o saldo ja depositado na conta vinculada ao longo
        # dos anos — e NAO e desembolso. O que a empresa paga sao a multa e os
        # tres `*_valor`. Os numeros aqui sao deliberadamente assimetricos
        # (saldo grande, valores pequenos) pra que somar a base de volta
        # quebre o teste em vez de passar por coincidencia.
        grfc = pd.DataFrame({
            "codi_emp":           [1, 3],
            "i_calculos":         ["a", "b"],
            "vencimento":         ["2025-02-10", "2025-01-20"],
            "saldo_fins_resc":    [30.0, 5.0],
            "multa_fgts":         [12.0, 2.0],
            "mes_ant_valor":      [1.0, 0.0],
            "resc_valor":         [2.0, 0.5],
            "aviso_previo_valor": [3.0, 0.0],
        })
        return inss, fgts, grfc

    def test_salario_familia_compensado_vira_coluna_propria(self):
        """A guia vem BRUTA — `empresa` e o patronal ANTES da compensacao do
        salario familia (Lei 8.213 art.68).

        Identidade: `valor_inss = empresa + segurados + acid_trab - deducoes`.

        A compensacao vira COLUNA PROPRIA e NAO abate o `inss_empresa`: na
        maioria das empresas com salario familia o patronal e 0,00 (Simples)
        e a compensacao sai dos SEGURADOS, entao `inss_empresa - SF` ficaria
        NEGATIVO. Quem abate e o consumer, no CUSTO TOTAL. A emp 2 da fixture
        e esse caso.
        """
        inss, fgts, grfc = self._guias()
        out = build_encargos_mensais(inss, fgts, grfc)
        r = out[(out["codi_emp"] == 1) & (out["competencia"] == "2025-01")].iloc[0]
        # tp=11: 7,00 + 3,00 de comp + 1,50 de retroativo
        assert r["sf_compensado"] == pytest.approx(11.50)
        assert r["inss_empresa"] == pytest.approx(150.0)   # segue BRUTO
        # o 13o (tp52) tambem compensa, no mesmo conjunto de tipos do INSS
        d = out[(out["codi_emp"] == 1) & (out["competencia"] == "2025-12")].iloc[0]
        assert d["sf_compensado"] == pytest.approx(4.00)
        # Simples: patronal ZERO e compensacao > 0 — abater aqui deixaria
        # negativo, e e exatamente por isso que a coluna e separada
        s = out[(out["codi_emp"] == 2) & (out["competencia"] == "2025-01")].iloc[0]
        assert s["inss_empresa"] == pytest.approx(0.0)
        assert s["sf_compensado"] == pytest.approx(60.0)

    def test_sem_a_coluna_de_deducao_o_sf_e_NULL_e_nao_zero(self):
        """Raw antigo (pre-contrato) nao tem as colunas de deducao. Nesse caso
        `sf_compensado` e NaN — zero fabricaria "nao houve compensacao", que e
        afirmacao que o dado nao sustenta."""
        inss, fgts, grfc = self._guias()
        out = build_encargos_mensais(
            inss.drop(columns=["deducoes_comp_sf", "deducoes_comp_sf_retroativo"]),
            fgts, grfc)
        r = out[(out["codi_emp"] == 1) & (out["competencia"] == "2025-01")].iloc[0]
        assert pd.isna(r["sf_compensado"])
        assert r["inss_empresa"] == pytest.approx(150.0)

    def test_inclui_encargo_do_13o_junto_com_o_mensal(self):
        # o 13o entra no CUSTO TOTAL e o encargo dele vem junto.
        inss, fgts, grfc = self._guias()
        out = build_encargos_mensais(inss, fgts, grfc)
        r = out[(out["codi_emp"] == 1) & (out["competencia"] == "2025-01")].iloc[0]
        assert r["inss_empresa"] == pytest.approx(150.0)   # tp=11: 100+50
        assert r["fgts"] == pytest.approx(857.0)           # tp=11 (80) + tp=52 (777)
        # mes que so tem guia de 13o PASSA A EXISTIR (guia de 13o sem
        # nenhuma guia mensal)
        d = out[(out["codi_emp"] == 1) & (out["competencia"] == "2025-12")].iloc[0]
        assert d["inss_empresa"] == pytest.approx(999.0)
        assert pd.isna(d["fgts"])                          # sem FGTS em dez -> NULL

    def test_fgts_soma_as_duas_parcelas_do_13o_sem_duplicar(self):
        # A FOLHA estorna a 1a parcela por dentro do tp52 (por isso a linha do
        # 13o usa tp52 sozinho); a GUIA nao — cada uma cobre o que foi PAGO na
        # competencia. tp51 (nov) + tp52 (dez) = 8% do 13o integral. Se
        # duplicasse daria ~16%.
        fgts = pd.DataFrame({
            "codi_emp":     [1, 1],
            "i_fgtsfilial": [1, 2],
            "competencia":  ["2025-11-01", "2025-12-01"],
            "tipo_process": [51, 52],
            "total_fgts":   [17_048.73, 18_258.69],
        })
        out = build_encargos_mensais(pd.DataFrame(), fgts, pd.DataFrame())
        assert float(out["fgts"].sum()) == pytest.approx(35_307.42)

    def test_tp42_continua_fora(self):
        # Exclusao deliberada (materialidade nula). Este teste existe pra que
        # sair dela seja uma ESCOLHA, nunca um efeito colateral de mexer nos
        # tipos.
        inss = pd.DataFrame({
            "codi_emp":     [1, 1],
            "i_guiainss":   [1, 2],
            "competencia":  ["2025-01-01", "2025-01-01"],
            "tipo_process": [11, 42],
            "empresa":      [100.0, 999.0],
        })
        out = build_encargos_mensais(inss, pd.DataFrame(), pd.DataFrame())
        assert float(out["inss_empresa"].iloc[0]) == pytest.approx(100.0)

    def test_grfc_mapeia_vencimento_para_competencia_e_soma_so_o_desembolso(self):
        """`fgts_resc` e DESEMBOLSO, nao base.

        A soma da multa com as parcelas `*_valor` e o "valor declarado" do
        extrato da GRFC. `saldo_fins_resc` e a BASE da multa de 40% e nao
        entra.
        """
        inss, fgts, grfc = self._guias()
        out = build_encargos_mensais(inss, fgts, grfc)
        r = out[(out["codi_emp"] == 1) & (out["competencia"] == "2025-02")].iloc[0]
        # multa 12 + mes_ant 1 + resc 2 + aviso 3 = 18, mes do VENCIMENTO.
        assert r["fgts_resc"] == pytest.approx(18.0)
        assert pd.isna(r["inss_empresa"]) and pd.isna(r["fgts"])  # sem guia -> NULL

    def test_saldo_fins_resc_nao_entra_no_desembolso(self):
        """A base pode ser ordens de grandeza maior que o desembolso e nao pode
        mover o resultado."""
        inss, fgts, grfc = self._guias()
        inflado = grfc.copy()
        inflado["saldo_fins_resc"] = [999_999.0, 888_888.0]
        base = build_encargos_mensais(inss, fgts, grfc)["fgts_resc"]
        infl = build_encargos_mensais(inss, fgts, inflado)["fgts_resc"]
        pd.testing.assert_series_equal(base, infl)

    def test_raw_sem_as_colunas_de_valor_degrada_para_null(self):
        """Raw PRE-contrato: coluna AUSENTE e ausencia de dado, nao zero.

        "NULL = sem guia, NUNCA 0 fabricado". Cair na formula antiga
        esconderia o bug num caminho; somar zero subestimaria em silencio.
        """
        inss, fgts, grfc = self._guias()
        antigo = grfc.drop(columns=["mes_ant_valor", "resc_valor",
                                    "aviso_previo_valor"])
        out = build_encargos_mensais(inss, fgts, antigo)
        assert out["fgts_resc"].isna().all()

    def test_null_por_linha_na_coluna_de_valor_conta_como_zero(self):
        """NULL por LINHA e outra coisa que coluna ausente: a origem manda 0.00,
        entao um NULL isolado e ausencia daquela parcela — nao invalida a guia
        inteira."""
        inss, fgts, grfc = self._guias()
        com_nulo = grfc.copy()
        com_nulo.loc[0, "aviso_previo_valor"] = float("nan")
        out = build_encargos_mensais(inss, fgts, com_nulo)
        r = out[(out["codi_emp"] == 1) & (out["competencia"] == "2025-02")].iloc[0]
        assert r["fgts_resc"] == pytest.approx(15.0)       # 18 - aviso 3

    def test_null_aware_componente_ausente_nunca_zero(self):
        inss, fgts, grfc = self._guias()
        out = build_encargos_mensais(inss, fgts, grfc)
        r1 = out[(out["codi_emp"] == 1) & (out["competencia"] == "2025-01")].iloc[0]
        assert pd.isna(r1["fgts_resc"])                    # emp 1 jan sem GRFC -> NULL
        r3 = out[(out["codi_emp"] == 3) & (out["competencia"] == "2025-01")].iloc[0]
        assert pd.isna(r3["inss_empresa"]) and pd.isna(r3["fgts"])
        assert r3["fgts_resc"] == pytest.approx(2.5)   # multa 2 + resc 0,5

    def test_simples_zero_real_preservado(self):
        inss, fgts, grfc = self._guias()
        out = build_encargos_mensais(inss, fgts, grfc)
        r2 = out[(out["codi_emp"] == 2) & (out["competencia"] == "2025-01")].iloc[0]
        assert r2["inss_empresa"] == pytest.approx(0.0)    # zero da guia != NULL
        assert not pd.isna(r2["inss_empresa"])

    def test_inputs_vazios_devolvem_vazio_com_colunas(self):
        vazio = pd.DataFrame()
        out = build_encargos_mensais(vazio, vazio, vazio)
        assert out.empty
        assert list(out.columns) == ["codi_emp", "competencia",
                                     "inss_empresa", "fgts", "fgts_resc",
                                     "sf_compensado"]


class TestGrfcPorDemissao:
    """GRFC entra por mes da DEMISSAO, nao do vencimento.

    Mecanismo: join `foguiagrfc` x `forescisoes` por [codi_emp, i_calculos]
    (chave natural das duas, RAW_KEYS) -> `demissao` vira a competencia; guia
    SEM rescisao casada cai no fallback pro vencimento. Nao filtra
    forescisoes.tipo.
    """
    @staticmethod
    def _grfc(codi_emp, i_calculos, vencimento, multa=10.0, mes_ant=0.0,
             resc=0.0, aviso=0.0):
        return {"codi_emp": codi_emp, "i_calculos": i_calculos,
                "vencimento": vencimento, "saldo_fins_resc": 999_999.0,
                "multa_fgts": multa, "mes_ant_valor": mes_ant,
                "resc_valor": resc, "aviso_previo_valor": aviso}

    @staticmethod
    def _rescisao(codi_emp, i_calculos, demissao, tipo=1):
        return {"codi_emp": codi_emp, "i_calculos": i_calculos,
                "demissao": demissao, "tipo": tipo}

    def test_competencia_vem_da_demissao_quando_ha_rescisao_casada(self):
        # GRFC vence 2026-03-06, demissao registrada em 2026-02-24 -- o
        # desembolso migra pra fevereiro.
        grfc = pd.DataFrame([self._grfc(1, "x1", "2026-03-06", multa=100.0,
                                        resc=26.79)])
        fr = pd.DataFrame([self._rescisao(1, "x1", "2026-02-24")])
        out = build_encargos_mensais(pd.DataFrame(), pd.DataFrame(), grfc, fr)
        assert out["competencia"].tolist() == ["2026-02"]
        assert out["fgts_resc"].iloc[0] == pytest.approx(126.79)

    def test_sem_rescisao_casada_cai_no_fallback_do_vencimento(self):
        # sem forescisoes casando pela chave, o desembolso continua no mes do
        # VENCIMENTO.
        grfc = pd.DataFrame([self._grfc(1, "orfa", "2025-02-10", multa=18.0)])
        fr = pd.DataFrame([self._rescisao(1, "outra-chave", "2025-05-01")])
        out = build_encargos_mensais(pd.DataFrame(), pd.DataFrame(), grfc, fr)
        assert out["competencia"].tolist() == ["2025-02"]

    def test_sem_forescisoes_nenhuma_comportamento_identico_ao_de_hoje(self):
        # default (parametro omitido) == forescisoes vazio.
        grfc = pd.DataFrame([self._grfc(1, "a", "2025-02-10", multa=18.0)])
        sem_forescisoes = build_encargos_mensais(pd.DataFrame(), pd.DataFrame(), grfc)
        com_forescisoes_vazio = build_encargos_mensais(
            pd.DataFrame(), pd.DataFrame(), grfc, pd.DataFrame())
        assert sem_forescisoes["competencia"].tolist() == ["2025-02"]
        pd.testing.assert_frame_equal(sem_forescisoes, com_forescisoes_vazio)

    def test_atravessa_virada_de_ano(self):
        # GRFC pode cruzar o ano -- vencimento em jan/25, demissao em dez/24.
        grfc = pd.DataFrame([self._grfc(1, "y1", "2025-01-10", multa=50.0)])
        fr = pd.DataFrame([self._rescisao(1, "y1", "2024-12-20")])
        out = build_encargos_mensais(pd.DataFrame(), pd.DataFrame(), grfc, fr)
        assert out["competencia"].tolist() == ["2024-12"]

    def test_join_nao_duplica_quando_ha_mais_de_uma_rescisao_com_mesma_chave(self):
        # Defensivo: o merge nao pode inflar o desembolso se houver duplicata
        # na chave [codi_emp, i_calculos] da forescisoes. Sem dedup, o LEFT
        # JOIN casaria as 2 linhas e o
        # `groupby().sum()` dobraria o fgts_resc.
        grfc = pd.DataFrame([self._grfc(1, "dup", "2025-03-01", multa=40.0)])
        fr = pd.DataFrame([self._rescisao(1, "dup", "2025-02-15"),
                           self._rescisao(1, "dup", "2025-02-20")])
        out = build_encargos_mensais(pd.DataFrame(), pd.DataFrame(), grfc, fr)
        assert out["fgts_resc"].sum() == pytest.approx(40.0)

    def test_demissao_invalida_nao_vira_string_nat_e_cai_no_fallback(self):
        # Armadilha: _to_competencia termina em .astype(str),
        # entao data invalida viraria a STRING "NaT", nao alcancavel por
        # fallback. A guia com demissao imparseavel tem de cair no
        # vencimento, nao numa competencia literal "NaT".
        grfc = pd.DataFrame([self._grfc(1, "z1", "2025-04-15", multa=22.0)])
        fr = pd.DataFrame([self._rescisao(1, "z1", None)])
        out = build_encargos_mensais(pd.DataFrame(), pd.DataFrame(), grfc, fr)
        assert out["competencia"].tolist() == ["2025-04"]
        assert "NaT" not in out["competencia"].tolist()


class TestBuildFuncionariosMensalHibrida:
    """Hibrida V1: ativos(M) = presenca_folha(M) − demitidos tipo=1 do
    PROPRIO mes; demissao conta no mes do EVENTO quando registrada; sem
    registro degrada pra convencao B (sai quando some da folha). Identidade
    Inicial+Entradas−Saidas==Final segue fechando por construcao."""

    def _fb(self, rows, emp=1):
        return pd.DataFrame({
            "codi_emp": [emp] * len(rows),
            "competencia": [c for c, _ in rows],
            "tipo_process": [11] * len(rows),
            "i_empregados": [i for _, i in rows],
        })

    def _fr(self, rows, emp=1, tipo=1):  # rows: list[(demissao, i_emp)]
        return pd.DataFrame({
            "codi_emp": [emp] * len(rows),
            "i_calculos": [f"g{n}" for n in range(len(rows))],
            "i_empregados": [i for _, i in rows],
            "demissao": [d for d, _ in rows],
            "tipo": [tipo] * len(rows),
        })

    def _row(self, out, comp):
        return out[out["competencia"] == comp].iloc[0]

    def _shape_base(self):
        dez = [("2024-12-01", i) for i in range(1, 16)]
        jan = [("2025-01-01", i) for i in range(1, 16)] + [("2025-01-01", 42), ("2025-01-01", 43)]
        fev = [("2025-02-01", i) for i in range(1, 16) if i != 4] + [("2025-02-01", 42), ("2025-02-01", 43)]
        return self._fb(dez + jan + fev)

    def test_demitido_no_mes_sai_do_final(self):
        # demitido de 20/jan (i4) sai do Final de JANEIRO: 15+2-1=16; fev
        # re-ancora com inicial 16 e fecha 16+0-0=16.
        out = build_funcionarios_mensal(self._shape_base(),
                                        self._fr([("2025-01-20", 4)]),
                                        "2025-01", "2025-02")
        j, f = self._row(out, "2025-01"), self._row(out, "2025-02")
        assert (j["qtd_inicial"], j["qtd_admissoes"], j["qtd_rescisoes"], j["qtd_final"]) == (15, 2, 1, 16)
        assert (f["qtd_inicial"], f["qtd_admissoes"], f["qtd_rescisoes"], f["qtd_final"]) == (16, 0, 0, 16)

    def test_demitido_ultimo_dia(self):
        # demitido de 31/jan (i288) sai em JAN (41+0-1=40); fev com 3 novos.
        dez = [("2024-12-01", i) for i in range(1, 41)] + [("2024-12-01", 288)]
        jan = [("2025-01-01", i) for i in range(1, 41)] + [("2025-01-01", 288)]
        fev = [("2025-02-01", i) for i in range(1, 41)] + [("2025-02-01", 301), ("2025-02-01", 302), ("2025-02-01", 303)]
        out = build_funcionarios_mensal(self._fb(dez + jan + fev),
                                        self._fr([("2025-01-31", 288)]),
                                        "2025-01", "2025-02")
        j, f = self._row(out, "2025-01"), self._row(out, "2025-02")
        assert (j["qtd_inicial"], j["qtd_admissoes"], j["qtd_rescisoes"], j["qtd_final"]) == (41, 0, 1, 40)
        assert (f["qtd_inicial"], f["qtd_admissoes"], f["qtd_rescisoes"], f["qtd_final"]) == (40, 3, 0, 43)

    def test_serie_nova_com_demissao_no_1o_mes(self):
        # folha nasce em jan com 27; 1 demitido em 30/jan -> 0+26-0=26 (o
        # demitido nunca entra em ativos).
        fb = self._fb([("2025-01-01", i) for i in range(1, 28)])
        out = build_funcionarios_mensal(fb, self._fr([("2025-01-30", 26)]),
                                        "2025-01", "2025-01")
        jan = self._row(out, "2025-01")
        assert (jan["qtd_inicial"], jan["qtd_admissoes"], jan["qtd_rescisoes"], jan["qtd_final"]) == (0, 26, 0, 26)

    def test_demitido_sem_presenca_na_folha_no_op(self):
        # rescisao de quem NEM ESTA na folha do mes: set-minus e no-op —
        # nenhuma saida, nenhum negativo.
        fb = self._fb([("2025-01-01", 1), ("2025-01-01", 2)])
        out = build_funcionarios_mensal(fb, self._fr([("2025-01-15", 99)]),
                                        "2025-01", "2025-01")
        jan = self._row(out, "2025-01")
        assert (jan["qtd_inicial"], jan["qtd_admissoes"], jan["qtd_rescisoes"], jan["qtd_final"]) == (0, 2, 0, 2)

    def test_sem_registro_degrada_pra_convencao_b(self):
        # forescisoes vazio: o demitido so sai quando SOME da folha (fev),
        # exatamente a convencao B.
        out = build_funcionarios_mensal(self._shape_base(), pd.DataFrame(),
                                        "2025-01", "2025-02")
        j, f = self._row(out, "2025-01"), self._row(out, "2025-02")
        assert (j["qtd_inicial"], j["qtd_admissoes"], j["qtd_rescisoes"], j["qtd_final"]) == (15, 2, 0, 17)
        assert (f["qtd_inicial"], f["qtd_admissoes"], f["qtd_rescisoes"], f["qtd_final"]) == (17, 0, 1, 16)

    def test_tipo2_complementar_nao_exclui(self):
        # tipo=2 (rescisao complementar) NAO tira ninguem dos ativos.
        fb = self._fb([("2024-12-01", 1), ("2024-12-01", 2),
                       ("2025-01-01", 1), ("2025-01-01", 2)])
        out = build_funcionarios_mensal(fb, self._fr([("2025-01-10", 1)], tipo=2),
                                        "2025-01", "2025-01")
        jan = self._row(out, "2025-01")
        assert (jan["qtd_inicial"], jan["qtd_admissoes"], jan["qtd_rescisoes"], jan["qtd_final"]) == (2, 0, 0, 2)

    def test_edge_v1_demitido_ainda_na_folha_seguinte_documentado(self):
        # EDGE V1 (pin consciente): demitido em dez que (sinteticamente) AINDA
        # aparece na folha de jan reaparece como ENTRADA em jan;
        # comportamento V1 aceito e congelado aqui. A identidade fecha nas
        # duas linhas.
        fb = self._fb([("2024-11-01", 1), ("2024-11-01", 2),
                       ("2024-12-01", 1), ("2024-12-01", 2),
                       ("2025-01-01", 1), ("2025-01-01", 2)])
        out = build_funcionarios_mensal(fb, self._fr([("2024-12-20", 1)]),
                                        "2024-12", "2025-01")
        d, j = self._row(out, "2024-12"), self._row(out, "2025-01")
        assert (d["qtd_rescisoes"], d["qtd_final"]) == (1, 1)      # dez: i1 sai
        assert (j["qtd_admissoes"], j["qtd_final"]) == (1, 2)      # jan: i1 "volta"
        assert ((out["qtd_inicial"] + out["qtd_admissoes"] - out["qtd_rescisoes"]) == out["qtd_final"]).all()

    def test_property_identidade_e_is_partial_na_folha_crua(self):
        # is_partial mede a FOLHA CRUA: rotacao por demissao NAO flagra
        # (folha estavel 20/20/20, ativos caem pra 14 em mar); queda REAL da
        # folha (abr = 2 pessoas) flagra como antes. Identidade fecha em tudo.
        meses = {"2025-01": range(1, 21), "2025-02": range(1, 21), "2025-03": range(1, 21)}
        rows = [(f"{m}-01", i) for m, rng in meses.items() for i in rng]
        rows += [("2025-04-01", 1), ("2025-04-01", 2)]
        dem = [("2025-03-10", i) for i in range(1, 7)]              # 6 dem em mar
        out = build_funcionarios_mensal(self._fb(rows), self._fr(dem),
                                        "2025-01", "2025-04")
        mar, abr = self._row(out, "2025-03"), self._row(out, "2025-04")
        assert mar["qtd_final"] == 14 and not mar["is_partial"]     # rotacao nao flagra
        assert abr["is_partial"]                                    # queda real da folha flagra
        assert ((out["qtd_inicial"] + out["qtd_admissoes"] - out["qtd_rescisoes"]) == out["qtd_final"]).all()


class TestGuiaCompletaNumeradorRP:
    """ISS proprio em guia entra no numerador R/P quando NAO ha conta de
    deducao escriturada.

    Antes: R/P sem conta (-)ISS caia em fonte='isenta' (rotulo de imune) mesmo
    pagando ISS.

    Armadilhas que ficam FORA da lista branca:
      - IRPJ/CSLL (6/7): o IR aparece nas DUAS fontes (guia x provisao DRE)
        — importar duplicaria o IR que o card ja soma;
      - ICMS (1): guia e "a recolher" (liquido de credito) e balancete e
        "destacado".
    E o balancete MANDA quando existe: a guia so preenche a lacuna.
    """
    @staticmethod
    def _cenario(bal_rows, imp_rows, emp=1, comp="2025-01", receita=1000.0):
        regime = pd.DataFrame({"codi_emp": [emp], "competencia": [comp],
                               "regime": ["real_presumido"]})
        bal = pd.DataFrame(
            [{"codi_emp": emp, "competencia": comp, "clas_cta": "3010101001",
              "nome_cta": "Receita Servicos", "valo_deb": 0.0, "valo_cre": receita}]
            + bal_rows)
        imp = pd.DataFrame(imp_rows) if imp_rows else pd.DataFrame(
            columns=["codi_emp", "competencia", "codi_imp", "srec_sim", "bcal_sim"])
        fat = pd.DataFrame({"codi_emp": [emp], "competencia": [comp],
                            "valo_saidas": [0.0], "valo_servicos": [receita]})
        out = build_impostos_destacados_mensais(bal, imp, regime, fat)
        assert len(out) == 1
        return out.iloc[0]

    @staticmethod
    def _ded(nome, valor, emp=1, comp="2025-01"):
        return {"codi_emp": emp, "competencia": comp, "clas_cta": "3010201001",
                "nome_cta": nome, "valo_deb": valor, "valo_cre": 0.0}

    @staticmethod
    def _guia(codi_imp, valor, emp=1, comp="2025-01"):
        return {"codi_emp": emp, "competencia": comp, "codi_imp": codi_imp,
                "srec_sim": valor, "bcal_sim": 0.0}

    # ── o fix ───────────────────────────────────────────────────────────
    def test_iss_da_guia_entra_quando_nao_ha_deducao(self):
        row = self._cenario([], [self._guia(3, 37185.50)])
        assert row["valo_impostos"] == pytest.approx(37185.50)
        assert row["fonte"] == "guia"          # balancete zero
        assert row["base_receita"] == pytest.approx(1000.0)

    def test_hibrido_quando_ha_outra_deducao(self):
        # balancete tem ICMS escriturado, ISS so na guia -> soma os dois
        row = self._cenario([self._ded("(-) ICMS s/ Vendas", 100.0)],
                            [self._guia(3, 50.0)])
        assert row["valo_impostos"] == pytest.approx(150.0)
        assert row["fonte"] == "balancete+guia"

    # ── anti dupla contagem ─────────────────────────────────────────────
    def test_com_deducao_de_iss_a_guia_NAO_entra(self):
        # guia e balancete tem o MESMO ISS — somar duplicaria
        row = self._cenario([self._ded("(-) ISS s/ Servicos", 4448.90)],
                            [self._guia(3, 4448.90)])
        assert row["valo_impostos"] == pytest.approx(4448.90)
        assert row["fonte"] == "balancete"

    def test_deducao_de_iss_manda_mesmo_divergindo_da_guia(self):
        # divergente: balancete manda, guia nao completa nem soma
        row = self._cenario([self._ded("(-) ISS s/ Servicos", 274952.35)],
                            [self._guia(3, 114394.26)])
        assert row["valo_impostos"] == pytest.approx(274952.35)
        assert row["fonte"] == "balancete"

    # ── lista branca: as duas armadilhas ────────────────────────────────
    def test_irpj_e_csll_da_guia_NUNCA_entram(self):
        # ortogonalidade do IR-no-card: a provisao da DRE ja soma
        row = self._cenario([], [self._guia(7, 880624.64), self._guia(6, 109532.42)])
        assert math.isnan(row["valo_impostos"])
        assert row["fonte"] == "sem_deducao"

    def test_icms_da_guia_NUNCA_entra(self):
        row = self._cenario([], [self._guia(1, 492726.40)])
        assert math.isnan(row["valo_impostos"])

    # PIS/COFINS estao na lista branca — coberto em TestPisCofinsNaListaBranca.
    # ICMS e IRPJ/CSLL seguem fora.

    # ── rotulo ──────────────────────────────────────────────────────────
    def test_isenta_virou_sem_deducao(self):
        row = self._cenario([], [])
        assert row["fonte"] == "sem_deducao"
        assert math.isnan(row["valo_impostos"])
        assert row["base_receita"] == pytest.approx(1000.0)

    # ── regressao: Simples nao passa por aqui ───────────────────────────
    def test_simples_intocado_pela_regra_nova(self):
        regime = pd.DataFrame({"codi_emp": [2], "competencia": ["2025-01"],
                               "regime": ["simples"]})
        imp = pd.DataFrame([{"codi_emp": 2, "competencia": "2025-01",
                             "codi_imp": 3, "srec_sim": 90.0, "bcal_sim": 0.0},
                            {"codi_emp": 2, "competencia": "2025-01",
                             "codi_imp": 44, "srec_sim": 30.0, "bcal_sim": 0.0}])
        fat = pd.DataFrame({"codi_emp": [2], "competencia": ["2025-01"],
                            "valo_saidas": [0.0], "valo_servicos": [1000.0]})
        out = build_impostos_destacados_mensais(pd.DataFrame(), imp, regime, fat)
        # Simples soma TODAS as guias proprias (inclusive ISS) — inalterado
        assert out.iloc[0]["fonte"] == "efsdoimp"
        assert out.iloc[0]["valo_impostos"] == pytest.approx(120.0)


class TestPisCofinsNaListaBranca:
    """PIS (4) e COFINS (5) entram na lista branca.

    MESMO mecanismo do ISS: a guia so completa o numerador no mes em que a
    empresa NAO escritura a conta de deducao DAQUELE imposto. O portao e POR
    IMPOSTO, entao deducao de ISS nao libera nem bloqueia o PIS.

    Os pares divergentes NAO se movem: onde ha balancete, o balancete manda.
    """
    # staticmethod() e obrigatorio: atribuir a funcao crua faria o Python
    # liga-la como metodo e passar `self` como primeiro argumento.
    _cenario = staticmethod(TestGuiaCompletaNumeradorRP._cenario)
    _ded = staticmethod(TestGuiaCompletaNumeradorRP._ded)
    _guia = staticmethod(TestGuiaCompletaNumeradorRP._guia)

    def test_pis_da_guia_entra_quando_nao_ha_deducao_de_pis(self):
        row = self._cenario([], [self._guia(4, 16417.81)])
        assert row["valo_impostos"] == pytest.approx(16417.81)
        assert row["fonte"] == "guia"

    def test_cofins_da_guia_entra_quando_nao_ha_deducao_de_cofins(self):
        row = self._cenario([], [self._guia(5, 75774.52)])
        assert row["valo_impostos"] == pytest.approx(75774.52)
        assert row["fonte"] == "guia"

    def test_pis_e_cofins_somam_entre_si_quando_nenhum_escriturado(self):
        # os dois so em guia
        row = self._cenario([], [self._guia(4, 16417.81), self._guia(5, 75774.52)])
        assert row["valo_impostos"] == pytest.approx(92192.33)

    # ── anti dupla contagem, POR IMPOSTO ────────────────────────────────
    def test_com_deducao_de_pis_a_guia_de_pis_NAO_entra(self):
        row = self._cenario([self._ded("(-) PIS s/ Vendas", 5000.0)],
                            [self._guia(4, 5000.0)])
        assert row["valo_impostos"] == pytest.approx(5000.0)
        assert row["fonte"] == "balancete"

    def test_deducao_de_pis_manda_mesmo_divergindo_da_guia(self):
        # divergente: balancete manda, guia nao completa
        row = self._cenario([self._ded("(-) PIS s/ Vendas", 32963.02)],
                            [self._guia(4, 30547.79)])
        assert row["valo_impostos"] == pytest.approx(32963.02)
        assert row["fonte"] == "balancete"

    def test_portao_e_por_imposto_deducao_de_iss_nao_bloqueia_pis(self):
        # a conta escriturada e de ISS; o PIS segue sem deducao -> guia entra
        row = self._cenario([self._ded("(-) ISS s/ Servicos", 900.0)],
                            [self._guia(4, 250.0)])
        assert row["valo_impostos"] == pytest.approx(1150.0)
        assert row["fonte"] == "balancete+guia"

    def test_deducao_de_pis_nao_bloqueia_cofins(self):
        row = self._cenario([self._ded("(-) PIS s/ Vendas", 5000.0)],
                            [self._guia(4, 5000.0), self._guia(5, 23000.0)])
        assert row["valo_impostos"] == pytest.approx(28000.0)

    def test_conta_conjunta_pis_cofins_bloqueia_os_dois(self):
        # "(-) PIS/COFINS s/ Vendas" casa os DOIS regex — o valo_deb ja
        # contem os dois tributos, entao nenhuma das guias completa
        row = self._cenario([self._ded("(-) PIS/COFINS s/ Vendas", 28000.0)],
                            [self._guia(4, 5000.0), self._guia(5, 23000.0)])
        assert row["valo_impostos"] == pytest.approx(28000.0)
        assert row["fonte"] == "balancete"

    # ── as armadilhas que continuam de fora ─────────────────────────────
    def test_icms_continua_fora(self):
        row = self._cenario([], [self._guia(1, 492726.40)])
        assert math.isnan(row["valo_impostos"])

    def test_irpj_e_csll_continuam_fora(self):
        row = self._cenario([], [self._guia(7, 880624.64), self._guia(6, 109532.42)])
        assert math.isnan(row["valo_impostos"])

    def test_lista_branca_tem_iss_pis_e_cofins(self):
        from mappings import DEDUCAO_NOME_POR_IMPOSTO, GUIA_IMPORTAVEL_DESTACADOS
        assert GUIA_IMPORTAVEL_DESTACADOS == {3, 4, 5}
        assert set(DEDUCAO_NOME_POR_IMPOSTO) == {3, 4, 5}


class TestEstornoNaContaDeDeducaoLiquido:
    """O destacado e LIQUIDO de estorno.

    build_impostos_destacados_mensais somava valo_deb CRU das contas de
    deducao (3010201xxx), ignorando valo_cre -- estorno lancado a CREDITO
    na propria conta de deducao (ex.: nota cancelada, reclassificacao) nao
    abatia o numerador.

    Exemplo: PIS valo_deb 12.125,65 x valo_cre 84,25; COFINS valo_deb
    55.851,46 x valo_cre 388,08 -- estorno = R$ 472,33.
    """
    @staticmethod
    def _cenario(bal_rows, emp=1, comp="2025-01", receita=1000.0, report=None):
        regime = pd.DataFrame({"codi_emp": [emp], "competencia": [comp],
                               "regime": ["real_presumido"]})
        bal = pd.DataFrame(
            [{"codi_emp": emp, "competencia": comp, "clas_cta": "3010101001",
              "nome_cta": "Receita Servicos", "valo_deb": 0.0, "valo_cre": receita}]
            + bal_rows)
        imp = pd.DataFrame(columns=["codi_emp", "competencia", "codi_imp",
                                    "srec_sim", "bcal_sim"])
        fat = pd.DataFrame({"codi_emp": [emp], "competencia": [comp],
                            "valo_saidas": [0.0], "valo_servicos": [receita]})
        out = build_impostos_destacados_mensais(bal, imp, regime, fat,
                                                 report=report)
        assert len(out) == 1
        return out.iloc[0]

    @staticmethod
    def _ded(nome, valo_deb, valo_cre=0.0, emp=1, comp="2025-01"):
        return {"codi_emp": emp, "competencia": comp, "clas_cta": "3010201001",
                "nome_cta": nome, "valo_deb": valo_deb, "valo_cre": valo_cre}

    def test_numerador_e_liquido_de_estorno(self):
        row = self._cenario([
            self._ded("(-) PIS s/ Vendas", 12125.65, 84.25, emp=204),
            self._ded("(-) COFINS s/ Vendas", 55851.46, 388.08, emp=204),
        ], emp=204)
        assert row["valo_impostos"] == pytest.approx(67504.78)

    def test_sem_estorno_comportamento_identico(self):
        row = self._cenario([self._ded("(-) ICMS s/ Vendas", 100.0)])
        assert row["valo_impostos"] == pytest.approx(100.0)

    def test_delta_por_empresa_vai_pro_report(self):
        report = {}
        self._cenario([
            self._ded("(-) PIS s/ Vendas", 12125.65, 84.25, emp=204),
            self._ded("(-) COFINS s/ Vendas", 55851.46, 388.08, emp=204),
        ], emp=204, report=report)
        ed = report.get("estorno_deducao_delta")
        assert ed is not None, (
            "delta de estorno na deducao nao apareceu no report")
        assert ed["total"] == pytest.approx(472.33)
        assert ed["por_empresa"] == {204: pytest.approx(472.33)}

    def test_sem_estorno_report_fica_limpo(self):
        report = {}
        self._cenario([self._ded("(-) ICMS s/ Vendas", 100.0)], report=report)
        assert "estorno_deducao_delta" not in report

    def test_report_none_nao_quebra(self):
        row = self._cenario([self._ded("(-) ICMS s/ Vendas", 100.0)], report=None)
        assert row["valo_impostos"] == pytest.approx(100.0)


class TestBuildIssRetidoSofrido:
    """ISS retido na fonte pelo TOMADOR sobre servico que a empresa PRESTOU.

    E imposto DELA (so muda quem recolhe) — nao confundir com o retido DE
    TERCEIROS, que vive em efentradaspar / efsdoimp cod 18 / FOGUIA_ISS e
    nunca chega aqui: a fonte e a parcela da nota EMITIDA.

    Competencia = mes da NOTA (dser_ser), NAO do vencimento da parcela:
    `balancete - guia` bate com o retido agregado por data-da-nota; por
    vencimento, o guia implicito de um mes podia ficar NEGATIVO, o que e
    impossivel.
    """
    @staticmethod
    def _nota(emp, ser, data):
        return {"codi_emp": emp, "codi_ser": ser, "dser_ser": data,
                "vcon_ser": 1000.0}

    @staticmethod
    def _parc(emp, ser, parc, val):
        return {"codi_emp": emp, "codi_ser": ser, "parc_serp": parc,
                "vissr_serp": val}

    def test_soma_parcelas_da_mesma_nota(self):
        # duas convencoes reais convivem na base: tudo na 1a parcela ou
        # rateado entre elas. O SUM cobre as duas (razao ~3-5% da nota).
        notas = pd.DataFrame([self._nota(1, 10, "2025-03-15")])
        parc = pd.DataFrame([self._parc(1, 10, 1, 8544.66),
                             self._parc(1, 10, 2, 8544.66)])
        out = build_iss_retido_sofrido(notas, parc)
        assert len(out) == 1
        assert out.iloc[0]["competencia"] == "2025-03"
        assert out.iloc[0]["valo_retido"] == pytest.approx(17089.32)

    def test_competencia_vem_da_nota(self):
        notas = pd.DataFrame([self._nota(1, 10, "2025-03-31")])
        parc = pd.DataFrame([self._parc(1, 10, 1, 100.0)])
        out = build_iss_retido_sofrido(notas, parc)
        assert out.iloc[0]["competencia"] == "2025-03"

    def test_agrega_notas_do_mesmo_mes(self):
        notas = pd.DataFrame([self._nota(1, 10, "2025-03-01"),
                              self._nota(1, 11, "2025-03-28")])
        parc = pd.DataFrame([self._parc(1, 10, 1, 60.0),
                             self._parc(1, 11, 1, 40.0)])
        out = build_iss_retido_sofrido(notas, parc)
        assert len(out) == 1
        assert out.iloc[0]["valo_retido"] == pytest.approx(100.0)

    def test_parcela_orfa_nao_entra(self):
        # parcela sem nota na janela nao tem competencia derivavel
        notas = pd.DataFrame([self._nota(1, 10, "2025-03-01")])
        parc = pd.DataFrame([self._parc(1, 10, 1, 50.0),
                             self._parc(1, 99, 1, 999.0)])
        out = build_iss_retido_sofrido(notas, parc)
        assert len(out) == 1
        assert out.iloc[0]["valo_retido"] == pytest.approx(50.0)

    def test_data_invalida_nao_vira_competencia(self):
        # _to_competencia devolve NaN FLOAT pra data imparseavel -- a linha
        # e descartada ANTES do groupby
        # (descarte explicito) e nao sobra pra virar mes fantasma.
        notas = pd.DataFrame([self._nota(1, 10, None)])
        parc = pd.DataFrame([self._parc(1, 10, 1, 50.0)])
        assert build_iss_retido_sofrido(notas, parc).empty

    def test_zero_nao_vira_linha(self):
        notas = pd.DataFrame([self._nota(1, 10, "2025-03-01")])
        parc = pd.DataFrame([self._parc(1, 10, 1, 0.0)])
        assert build_iss_retido_sofrido(notas, parc).empty

    def test_frames_vazios(self):
        out = build_iss_retido_sofrido(pd.DataFrame(), pd.DataFrame())
        assert out.empty
        assert list(out.columns) == ["codi_emp", "competencia", "valo_retido"]

    def test_empresas_separadas(self):
        notas = pd.DataFrame([self._nota(1, 10, "2025-03-01"),
                              self._nota(2, 10, "2025-03-01")])
        parc = pd.DataFrame([self._parc(1, 10, 1, 10.0),
                             self._parc(2, 10, 1, 20.0)])
        out = build_iss_retido_sofrido(notas, parc).sort_values("codi_emp")
        assert list(out["valo_retido"]) == pytest.approx([10.0, 20.0])


class TestIssRetidoSofridoDescarteReportado:
    """O guard `!= "NaT"` era NO-OP: _to_competencia devolve NaN FLOAT pra
    data imparseavel, nao a string "NaT", entao o filtro nao removia nada e o
    `groupby(dropna=True)` seguinte comia a linha em SILENCIO. Esta classe
    prova que o MECANISMO fecha.
    """
    @staticmethod
    def _nota(emp, ser, data):
        return {"codi_emp": emp, "codi_ser": ser, "dser_ser": data,
                "vcon_ser": 1000.0}

    @staticmethod
    def _parc(emp, ser, parc, val):
        return {"codi_emp": emp, "codi_ser": ser, "parc_serp": parc,
                "vissr_serp": val}

    def test_descarte_e_contado_e_reportado_por_empresa(self):
        """O descarte nao pode ser silencioso. Prova em duas pernas no
        MESMO teste: (a) o NUMERO nao muda (a nota invalida continua fora
        da soma, igual a test_data_invalida_nao_vira_competencia) e (b) o
        descarte agora aparece no report, com contagem, soma e empresa.
        """
        notas = pd.DataFrame([
            self._nota(1, 10, "2025-03-01"),   # valida
            self._nota(1, 11, None),           # imparseavel -- descartada
        ])
        parc = pd.DataFrame([self._parc(1, 10, 1, 60.0),
                             self._parc(1, 11, 1, 90.0)])
        report = {}
        out = build_iss_retido_sofrido(notas, parc, report=report)

        # (a) numero identico ao de antes do fix: so a nota valida soma.
        assert len(out) == 1
        assert out.iloc[0]["valo_retido"] == pytest.approx(60.0)

        # (b) o descarte agora e visivel, com empresa e soma.
        desc = report.get("iss_retido_competencia_invalida")
        assert desc is not None, (
            "descarte de competencia invalida nao apareceu no report -- "
            "perda continua SILENCIOSA")
        assert desc["linhas"] == 1
        assert desc["soma"] == pytest.approx(90.0)
        assert desc["por_empresa"] == {1: pytest.approx(90.0)}

    def test_nota_invalida_sem_parcela_conta_mas_soma_zero(self):
        # nota com data ruim e SEM parcela correspondente: entra na
        # contagem (e uma linha de dado suspeito) mas nao tem R$ pra somar.
        notas = pd.DataFrame([self._nota(1, 10, "2025-03-01"),
                              self._nota(1, 12, "lixo")])
        parc = pd.DataFrame([self._parc(1, 10, 1, 60.0)])
        report = {}
        out = build_iss_retido_sofrido(notas, parc, report=report)
        assert len(out) == 1
        desc = report.get("iss_retido_competencia_invalida")
        assert desc is not None
        assert desc["linhas"] == 1
        assert desc["soma"] == pytest.approx(0.0)
        assert desc["por_empresa"] == {}

    def test_report_none_nao_quebra(self):
        # default (sem report) continua funcionando -- todos os call sites
        # e testes antigos que nao passam `report` seguem intactos.
        notas = pd.DataFrame([self._nota(1, 10, None)])
        parc = pd.DataFrame([self._parc(1, 10, 1, 50.0)])
        assert build_iss_retido_sofrido(notas, parc).empty
        assert build_iss_retido_sofrido(notas, parc, report=None).empty

    def test_sem_competencia_invalida_report_fica_vazio(self):
        # run limpo nao ganha ruido: a chave so aparece no report quando ha
        # descarte de verdade.
        notas = pd.DataFrame([self._nota(1, 10, "2025-03-01")])
        parc = pd.DataFrame([self._parc(1, 10, 1, 50.0)])
        report = {}
        build_iss_retido_sofrido(notas, parc, report=report)
        assert "iss_retido_competencia_invalida" not in report

    def test_caso_limpo_inalterado_byte_a_byte(self):
        """Regressao-zero, LITERAL: pra um cenario sem NaN (varias notas,
        varias empresas, parcelas rateadas), a saida com `report=None`
        (comportamento de sempre) e com `report={}` (novo parametro,
        default) tem de ser BYTE A BYTE identica -- mesmas colunas, mesma
        ordem, mesmos valores, mesmo dtype. Cobre o caminho que
        `pandas.testing.assert_frame_equal` audita de verdade, nao so um
        `==` frouxo."""
        notas = pd.DataFrame([
            self._nota(1, 10, "2025-03-15"),
            self._nota(1, 11, "2025-03-28"),
            self._nota(2, 20, "2025-04-01"),
        ])
        parc = pd.DataFrame([
            self._parc(1, 10, 1, 8544.66), self._parc(1, 10, 2, 8544.66),
            self._parc(1, 11, 1, 40.0),
            self._parc(2, 20, 1, 999.99),
        ])
        sem_report = build_iss_retido_sofrido(notas.copy(), parc.copy())
        com_report_vazio = build_iss_retido_sofrido(
            notas.copy(), parc.copy(), report={})
        pd.testing.assert_frame_equal(sem_report, com_report_vazio)
        # ancora numerica explicita, pra falhar por VALOR se algo mudar:
        assert sem_report["valo_retido"].tolist() == pytest.approx(
            [17129.32, 999.99])


class TestIssRetidoSofridoNoNumerador:
    """O retido sofrido COMPLETA o numerador na mesma regra da guia de ISS.

    guia (cod 3) e retido sofrido sao componentes DISJUNTOS do mesmo total:
    a identidade `balancete = guia + retido` fecha MENSALMENTE. Logo, quando
    o balancete NAO cobre o ISS, os dois entram JUNTOS; quando cobre, NENHUM
    entra — o balancete ja contem os dois.
    """
    @staticmethod
    def _cenario(bal_rows, imp_rows, retido, emp=1, comp="2025-01",
                 receita=1000.0, regime_nome="real_presumido"):
        regime = pd.DataFrame({"codi_emp": [emp], "competencia": [comp],
                               "regime": [regime_nome]})
        bal = pd.DataFrame(
            [{"codi_emp": emp, "competencia": comp, "clas_cta": "3010101001",
              "nome_cta": "Receita Servicos", "valo_deb": 0.0,
              "valo_cre": receita}] + bal_rows)
        imp = pd.DataFrame(imp_rows) if imp_rows else pd.DataFrame(
            columns=["codi_emp", "competencia", "codi_imp", "srec_sim",
                     "bcal_sim"])
        fat = pd.DataFrame({"codi_emp": [emp], "competencia": [comp],
                            "valo_saidas": [0.0], "valo_servicos": [receita]})
        ret = (pd.DataFrame([{"codi_emp": emp, "competencia": comp,
                              "valo_retido": retido}])
               if retido is not None else None)
        out = build_impostos_destacados_mensais(bal, imp, regime, fat, ret)
        assert len(out) == 1
        return out.iloc[0]

    @staticmethod
    def _ded(nome, valor, emp=1, comp="2025-01"):
        return {"codi_emp": emp, "competencia": comp, "clas_cta": "3010201001",
                "nome_cta": nome, "valo_deb": valor, "valo_cre": 0.0}

    @staticmethod
    def _guia(codi_imp, valor, emp=1, comp="2025-01"):
        return {"codi_emp": emp, "competencia": comp, "codi_imp": codi_imp,
                "srec_sim": valor, "bcal_sim": 0.0}

    # ── o fix ───────────────────────────────────────────────────────────
    def test_retido_sozinho_entra_quando_nao_ha_deducao(self):
        row = self._cenario([], [], 1546878.11)
        assert row["valo_impostos"] == pytest.approx(1546878.11)
        assert row["fonte"] == "retido"

    def test_guia_e_retido_somam_quando_nao_ha_deducao(self):
        # os dois componentes disjuntos entram juntos
        row = self._cenario([], [self._guia(3, 588902.95)], 194198.79)
        assert row["valo_impostos"] == pytest.approx(783101.74)
        assert row["fonte"] == "guia+retido"

    def test_outra_deducao_no_balancete_soma_os_tres(self):
        row = self._cenario([self._ded("(-) ICMS s/ Vendas", 100.0)],
                            [self._guia(3, 50.0)], 25.0)
        assert row["valo_impostos"] == pytest.approx(175.0)
        assert row["fonte"] == "balancete+guia+retido"

    def test_balancete_sem_iss_mais_retido(self):
        row = self._cenario([self._ded("(-) PIS s/ Vendas", 80.0)], [], 20.0)
        assert row["valo_impostos"] == pytest.approx(100.0)
        assert row["fonte"] == "balancete+retido"

    # ── anti dupla contagem ─────────────────────────────────────────────
    def test_com_deducao_de_iss_o_retido_NAO_entra(self):
        # o balancete (274.952,35) JA CONTEM guia (114.394,26) + retido
        # (160.541,55). Somar duplicaria.
        row = self._cenario([self._ded("(-) ISS s/ Servicos", 274952.35)],
                            [self._guia(3, 114394.26)], 160541.55)
        assert row["valo_impostos"] == pytest.approx(274952.35)
        assert row["fonte"] == "balancete"

    def test_deducao_de_iss_zerada_ainda_e_deducao(self):
        # a conta existe no mes (valor 0) -> balancete manda, nada completa
        row = self._cenario([self._ded("(-) ISS s/ Servicos", 0.0)], [], 500.0)
        assert row["fonte"] == "sem_deducao"
        assert math.isnan(row["valo_impostos"])

    # ── Simples fica FORA por construcao (ISS dentro do DAS) ────────────
    def test_simples_ignora_o_retido(self):
        # Simples COM retido sofrido. O numerador
        # e so o DAS — somar o retido duplicaria (o ISS ja esta dentro dele).
        row = self._cenario([], [self._guia(44, 300.0, emp=2)], 60395.30,
                            emp=2, regime_nome="simples")
        assert row["fonte"] == "efsdoimp"
        assert row["valo_impostos"] == pytest.approx(300.0)

    # ── regressao: sem retido, comportamento IDENTICO ao da guia ────────
    def test_sem_retido_o_comportamento_e_identico(self):
        row = self._cenario([], [self._guia(3, 37185.50)], None)
        assert row["valo_impostos"] == pytest.approx(37185.50)
        assert row["fonte"] == "guia"

    def test_retido_zero_nao_cria_componente(self):
        row = self._cenario([], [self._guia(3, 10.0)], 0.0)
        assert row["valo_impostos"] == pytest.approx(10.0)
        assert row["fonte"] == "guia"

    def test_terceiros_por_guia_continua_fora(self):
        # codi_imp 18 = "ISS Retido" DE TERCEIROS: nunca entrou, nunca entra
        row = self._cenario([], [self._guia(18, 570244.56)], None)
        assert math.isnan(row["valo_impostos"])
        assert row["fonte"] == "sem_deducao"


class TestValoIssComponente:
    """`valo_iss` = o componente ISS do numerador, exposto pro bloco Impostos.

    O bloco lia SO guias (efsdoimp); empresa com ISS 100% retido na fonte
    — nunca vira guia — aparecia com ZERO de ISS. O valor certo por mes ja
    existia DENTRO do numerador; so nao era separavel (o mart guardava so o
    total).

    Composicao = o MESMO portao do complemento: balancete quando ha conta de
    (-)ISS no mes; senao guia(cod 3) + retido sofrido. So Real/Presumido —
    no Simples o ISS esta dentro do DAS e uma linha propria duplicaria.
    """
    @staticmethod
    def _cenario(bal_rows, imp_rows, retido, emp=1, comp="2025-01",
                 receita=1000.0, regime_nome="real_presumido"):
        regime = pd.DataFrame({"codi_emp": [emp], "competencia": [comp],
                               "regime": [regime_nome]})
        bal = pd.DataFrame(
            [{"codi_emp": emp, "competencia": comp, "clas_cta": "3010101001",
              "nome_cta": "Receita Servicos", "valo_deb": 0.0,
              "valo_cre": receita}] + bal_rows)
        imp = pd.DataFrame(imp_rows) if imp_rows else pd.DataFrame(
            columns=["codi_emp", "competencia", "codi_imp", "srec_sim",
                     "bcal_sim"])
        fat = pd.DataFrame({"codi_emp": [emp], "competencia": [comp],
                            "valo_saidas": [0.0], "valo_servicos": [receita]})
        ret = (pd.DataFrame([{"codi_emp": emp, "competencia": comp,
                              "valo_retido": retido}])
               if retido is not None else None)
        out = build_impostos_destacados_mensais(bal, imp, regime, fat, ret)
        assert len(out) == 1
        return out.iloc[0]

    @staticmethod
    def _ded(nome, valor, emp=1, comp="2025-01"):
        return {"codi_emp": emp, "competencia": comp, "clas_cta": "3010201001",
                "nome_cta": nome, "valo_deb": valor, "valo_cre": 0.0}

    @staticmethod
    def _guia(codi_imp, valor, emp=1, comp="2025-01"):
        return {"codi_emp": emp, "competencia": comp, "codi_imp": codi_imp,
                "srec_sim": valor, "bcal_sim": 0.0}

    def test_coluna_existe(self):
        row = self._cenario([], [], None)
        assert "valo_iss" in row.index

    # ── balancete manda quando ha conta ──────────────────────────────────
    def test_com_conta_o_valo_iss_e_o_balancete(self):
        # guia 114.394,26 + retido 160.541,55, mas a conta
        # existe -> a linha ISS e o balancete, UMA vez so.
        row = self._cenario([self._ded("(-) ISS s/ Servicos", 274952.35)],
                            [self._guia(3, 114394.26)], 160541.55)
        assert row["valo_iss"] == pytest.approx(274952.35)

    def test_conta_zerada_da_iss_zero_nao_guia(self):
        row = self._cenario([self._ded("(-) ISS s/ Servicos", 0.0)],
                            [self._guia(3, 500.0)], 300.0)
        assert row["valo_iss"] == pytest.approx(0.0)

    # ── sem conta: guia + retido ─────────────────────────────────────────
    def test_sem_conta_soma_guia_e_retido(self):
        row = self._cenario([], [self._guia(3, 588902.95)], 194198.79)
        assert row["valo_iss"] == pytest.approx(783101.74)

    def test_sem_conta_so_retido(self):
        # 100% retido na fonte, guia zero
        row = self._cenario([], [], 1546878.11)
        assert row["valo_iss"] == pytest.approx(1546878.11)

    def test_sem_conta_so_guia(self):
        row = self._cenario([], [self._guia(3, 37185.50)], None)
        assert row["valo_iss"] == pytest.approx(37185.50)

    # ── ausencia = NaN (consumer testa notna, sem lixo visual) ───────────
    def test_sem_iss_nenhum_e_nan(self):
        # ICMS/PIS/COFINS sim, ISS nao existe
        row = self._cenario([self._ded("(-) ICMS s/ Vendas", 792614.24)],
                            [self._guia(1, 563015.64)], None)
        assert math.isnan(row["valo_iss"])

    def test_simples_nao_ganha_linha_de_iss(self):
        # empresa do Simples com conta de (-)ISS no balancete; o ISS dela
        # esta DENTRO do DAS exibido na mesma tabela.
        row = self._cenario([self._ded("(-) ISS s/ Servicos", 3717.10)],
                            [self._guia(44, 300.0, emp=2)], 500.0,
                            emp=2, regime_nome="simples")
        assert row["fonte"] == "efsdoimp"
        assert math.isnan(row["valo_iss"])

    # ── invariante: o componente nunca excede o total ────────────────────
    def test_valo_iss_nunca_excede_o_numerador(self):
        row = self._cenario([self._ded("(-) ICMS s/ Vendas", 100.0),
                             self._ded("(-) ISS s/ Servicos", 50.0)], [], None)
        assert row["valo_iss"] == pytest.approx(50.0)
        assert row["valo_iss"] <= row["valo_impostos"]



class TestComponenteIssSoContaOCod3:
    """`valo_iss` e o componente ISS, nao a lista branca inteira.

    Regressao: a lista branca virou {3, 4, 5} e o `complemento_iss` passou a
    somar os tres. Como `tabela_impostos` so tira o `codi_imp=3` das linhas
    de guia, PIS e COFINS apareciam na linha propria E dentro do ISS — o
    TOTAL contava os dois DUAS VEZES. Quando o balancete cobre o ISS, o
    `complemento_iss` nem e lido.
    """
    _cenario = TestIssRetidoSofridoNoNumerador.__dict__["_cenario"]
    _guia = TestIssRetidoSofridoNoNumerador.__dict__["_guia"]
    _ded = TestIssRetidoSofridoNoNumerador.__dict__["_ded"]

    def test_pis_e_cofins_ficam_fora_do_componente_iss(self):
        # guia de PIS+COFINS, ZERO de ISS
        # proprio, mais a unica parcela com retido. O ISS exibido tem que
        # ser so o retido.
        row = self._cenario([], [self._guia(4, 2137.59),
                                 self._guia(5, 9865.80)], 16443.00)
        assert row["valo_iss"] == pytest.approx(16443.00)

    def test_o_numerador_do_card_NAO_muda(self):
        # `valo_impostos` soma a lista branca toda. So o recorte rotulado ISS e que estreita.
        row = self._cenario([], [self._guia(4, 2137.59),
                                 self._guia(5, 9865.80)], 16443.00)
        assert row["valo_impostos"] == pytest.approx(28446.39)
        assert row["fonte"] == "guia+retido"

    def test_so_pis_cofins_sem_iss_nenhum_nao_cria_linha(self):
        # a empresa nao tem ISS em fonte nenhuma, e a
        # linha inteira dela era PIS+COFINS. NaN faz o consumer nao
        # desenhar a linha; 0.0 desenharia "R$ 0,00", que afirma outra
        # coisa ("houve apuracao e deu zero").
        row = self._cenario([], [self._guia(4, 7350.77),
                                 self._guia(5, 33926.62)], None)
        assert math.isnan(row["valo_iss"])

    def test_iss_por_guia_segue_entrando(self):
        # Controle: estreitar nao pode derrubar o cod 3.
        row = self._cenario([], [self._guia(3, 588902.95),
                                 self._guia(4, 100.00)], 194198.79)
        assert row["valo_iss"] == pytest.approx(783101.74)


class TestBuildFolhaRubricasMensais:
    """Fundacao do dash de Detalhamento de Folha.

    Grao por EMPRESA, nao por pessoa. Ancoragem: SUM(FOMOVTO P) ==
    FOBASES.proventos.
    """

    @staticmethod
    def _mov(rows):
        return pd.DataFrame(rows, columns=[
            "codi_emp", "i_empregados", "data", "tipo_proces", "i_eventos",
            "valor_inf", "valor_cal", "prov_desc", "i_calculos"])

    _EVE = pd.DataFrame({"codi_emp": [201, 201], "i_eventos": [1, 998],
                         "nome": ["SALARIO BASE", "I.N.S.S."],
                         "prov_desc": ["P", "D"], "classificacao": [1, 12]})
    _CLS = pd.DataFrame({"i_classificacao": [1, 12],
                         "descricao": ["Salarios", "INSS"]})

    # Dicionario PADRAO do Dominio (bethadba.foeventosdef), SEM codi_emp. A FOEVENTOS e a COPIA por empresa; empresa que nunca
    # materializou a copia resolve o nome aqui, em runtime.
    _DEF = pd.DataFrame({"i_eventos": [1, 998, 937],
                         "nome": ["HORAS NORMAIS", "I.N.S.S.",
                                  "ADIANTAMENTO DE FERIAS"],
                         "classificacao": [1, 12, 11]})

    def test_dicionario_padrao_cobre_empresa_sem_copia(self):
        """Ha empresas sem UMA linha em bethadba.FOEVENTOS — o buraco nao e
        ruido distribuido, sao empresas INTEIRAS mudas (a origem tem
        integridade referencial quebrada). Sem o padrao, toda regra por nome
        ou familia simplesmente nao dispara nessas empresas.
        """
        m = self._mov([[233, 7, "2025-09-01", 11, 1, 220.0, 1000.0, "P", "g1"]])
        r = build_folha_rubricas_mensais(m, self._EVE, self._CLS, self._DEF)
        linha = r.iloc[0]
        assert linha["nome"] == "HORAS NORMAIS"
        assert linha["classificacao"] == 1
        assert linha["desc_classificacao"] == "Salarios"   # familia resolve junto
        assert linha["fonte_dicionario"] == "padrao"

    def test_copia_da_empresa_VENCE_o_padrao(self):
        """PRECEDENCIA: o override por empresa manda, o padrao so preenche
        buraco. O nome diverge por renomeacao cosmetica (a 201 chama de
        'SALARIO BASE' o que o padrao chama de 'HORAS NORMAIS') e trocar
        mudaria numero sem ninguem pedir.
        """
        m = self._mov([[201, 7, "2025-09-01", 11, 1, 220.0, 1000.0, "P", "g1"]])
        r = build_folha_rubricas_mensais(m, self._EVE, self._CLS, self._DEF)
        linha = r.iloc[0]
        assert linha["nome"] == "SALARIO BASE"      # NAO "HORAS NORMAIS"
        assert linha["fonte_dicionario"] == "empresa"

    def test_rubrica_em_nenhum_dos_dois_fica_declarada_como_ausente(self):
        """Residuo declarado: codigos que nao existem nem na copia nem no
        padrao. Eles NAO sao resolvidos por moda de i_eventos — o mesmo
        codigo tem varios nomes distintos entre empresas.
        """
        m = self._mov([[233, 7, "2025-09-01", 11, 4242, 0.0, 50.0, "P", "g1"]])
        r = build_folha_rubricas_mensais(m, self._EVE, self._CLS, self._DEF)
        linha = r.iloc[0]
        assert pd.isna(linha["nome"])
        assert linha["fonte_dicionario"] == "ausente"

    def test_sem_o_padrao_o_build_segue_igual(self):
        """Retrocompat: o parametro e opcional. Sem ele o comportamento e o de
        antes — copia da empresa ou nada."""
        m = self._mov([[233, 7, "2025-09-01", 11, 1, 0.0, 1000.0, "P", "g1"],
                       [201, 7, "2025-09-01", 11, 1, 0.0, 500.0, "P", "g2"]])
        r = build_folha_rubricas_mensais(m, self._EVE, self._CLS)
        cega = r[r.codi_emp == 233].iloc[0]
        assert pd.isna(cega["nome"])
        assert cega["fonte_dicionario"] == "ausente"
        assert r[r.codi_emp == 201].iloc[0]["fonte_dicionario"] == "empresa"

    def test_padrao_nao_vaza_para_rubrica_que_a_empresa_zerou(self):
        """O merge do padrao e por i_eventos SOZINHO (o dicionario e global).
        Isso so pode preencher onde a copia da empresa nao respondeu — nunca
        pode sobrescrever, nem inventar linha nova."""
        m = self._mov([[201, 7, "2025-09-01", 11, 998, 0.0, 90.0, "D", "g1"]])
        antes = build_folha_rubricas_mensais(m, self._EVE, self._CLS)
        depois = build_folha_rubricas_mensais(m, self._EVE, self._CLS, self._DEF)
        assert len(antes) == len(depois) == 1
        assert antes.iloc[0]["nome"] == depois.iloc[0]["nome"] == "I.N.S.S."
        assert float(antes.iloc[0]["valor_d"]) == float(depois.iloc[0]["valor_d"])

    def test_grao_e_por_empresa_sem_empregado(self):
        # LGPD: o grao de pessoa MORRE no raw. Quem precisa do
        # grao fino le raw.fomovto; o mart e por empresa.
        m = self._mov([[201, 7, "2025-09-01", 11, 1, 220.0, 1000.0, "P", "g1"],
                       [201, 8, "2025-09-01", 11, 1, 220.0, 500.0, "P", "g2"]])
        r = build_folha_rubricas_mensais(m, self._EVE, self._CLS)
        assert "i_empregados" not in r.columns
        assert len(r) == 1
        assert float(r.iloc[0]["valor_p"]) == 1500.0

    def test_prov_desc_padded_nao_cria_coluna_fantasma(self):
        # a origem devolve CHAR com padding em alguns caminhos ('P  ')
        m = self._mov([[201, 7, "2025-09-01", 11, 1, 0.0, 1000.0, "P  ", "g1"],
                       [201, 7, "2025-09-01", 11, 998, 0.0, 90.0, "D  ", "g2"]])
        r = build_folha_rubricas_mensais(m, self._EVE, self._CLS)
        assert set(r.columns) >= {"valor_p", "valor_d", "valor_i", "valor_id"}
        assert float(r[r.i_eventos == 1]["valor_p"].iloc[0]) == 1000.0
        assert float(r[r.i_eventos == 998]["valor_d"].iloc[0]) == 90.0

    def test_nada_de_dinheiro_some_no_pivot(self):
        # invariante duro: soma das 4 colunas == soma do valor_cal cru
        m = self._mov([[201, 7, "2025-09-01", 11, 1, 0.0, 1000.0, "P", "g1"],
                       [201, 7, "2025-09-01", 11, 998, 0.0, 90.0, "D", "g2"],
                       [201, 7, "2025-09-01", 11, 996, 0.0, 80.0, "I", "g3"],
                       [201, 7, "2025-09-01", 11, 881, 0.0, 5.0, "ID", "g4"]])
        r = build_folha_rubricas_mensais(m, self._EVE, self._CLS)
        total = float(r[["valor_p", "valor_d", "valor_i", "valor_id"]].sum().sum())
        assert total == pytest.approx(1175.0)

    def test_prov_desc_desconhecido_levanta(self):
        # o out[cols] final DESCARTARIA a coluna nova sem erro —
        # dinheiro sumindo em silencio. Levanta nomeando valor e contagem.
        m = self._mov([[201, 7, "2025-09-01", 11, 1, 0.0, 1000.0, "P", "g1"],
                       [201, 7, "2025-09-01", 11, 2, 0.0, 42.0, "X", "g2"],
                       [201, 7, "2025-09-01", 11, 3, 0.0, 8.0, "X", "g3"]])
        with pytest.raises(ValueError) as exc:
            build_folha_rubricas_mensais(m, self._EVE, self._CLS)
        msg = str(exc.value)
        assert "X" in msg
        assert "2" in msg          # a contagem das linhas desconhecidas

    def test_qtd_informada_tudo_nan_fica_nan(self):
        # min_count=1 — "0 horas" AFIRMA, ausencia DECLARA.
        m = self._mov([[201, 7, "2025-09-01", 11, 1, None, 1000.0, "P", "g1"],
                       [201, 8, "2025-09-01", 11, 1, None, 500.0, "P", "g2"]])
        r = build_folha_rubricas_mensais(m, self._EVE, self._CLS)
        assert math.isnan(float(r.iloc[0]["qtd_informada"]))

    def test_qtd_informada_soma_quando_existe(self):
        m = self._mov([[201, 7, "2025-09-01", 11, 1, 30.0, 1000.0, "P", "g1"],
                       [201, 8, "2025-09-01", 11, 1, None, 500.0, "P", "g2"]])
        r = build_folha_rubricas_mensais(m, self._EVE, self._CLS)
        assert float(r.iloc[0]["qtd_informada"]) == 30.0

    def test_competencia_trunca_data_do_meio_do_mes(self):
        # ferias/rescisao caem no dia do EVENTO, nao no dia 1. Tem que cair
        # no mesmo mes.
        m = self._mov([[201, 7, "2025-09-01", 11, 1, 0.0, 1000.0, "P", "g1"],
                       [201, 7, "2025-09-22", 11, 1, 0.0, 200.0, "P", "g2"]])
        r = build_folha_rubricas_mensais(m, self._EVE, self._CLS)
        assert len(r) == 1
        assert r.iloc[0]["competencia"] == "2025-09"
        assert float(r.iloc[0]["valor_p"]) == 1200.0

    def test_desnormaliza_nome_e_familia_global(self):
        m = self._mov([[201, 7, "2025-09-01", 11, 998, 0.0, 90.0, "D", "g1"]])
        r = build_folha_rubricas_mensais(m, self._EVE, self._CLS)
        assert r.iloc[0]["nome"] == "I.N.S.S."
        assert int(r.iloc[0]["classificacao"]) == 12
        assert r.iloc[0]["desc_classificacao"] == "INSS"

    def test_rubrica_sem_dicionario_nao_derruba_a_linha(self):
        # dicionario faltando (empresa nova) degrada o ROTULO,
        # nunca o dinheiro — mesmo idioma do nome_imp que cai pro numero cru.
        m = self._mov([[201, 7, "2025-09-01", 11, 4242, 0.0, 77.0, "P", "g1"]])
        r = build_folha_rubricas_mensais(m, self._EVE, self._CLS)
        assert len(r) == 1
        assert float(r.iloc[0]["valor_p"]) == 77.0
        assert pd.isna(r.iloc[0]["nome"])

    def test_frame_vazio_nao_crasha(self):
        # raw vazio (antes da 1a carga)
        r = build_folha_rubricas_mensais(pd.DataFrame(), self._EVE, self._CLS)
        assert r.empty
        assert "valor_p" in r.columns


class TestClasDeducaoExata:
    """Contas de deducao de ISS FORA de PREFIXOS_DEDUCAO.

    A `30201010006` e deducao de ISS retido na fonte. Empresas diferentes
    NAO compartilham o codigo — umas usam a `30201010006`, outras a
    `301010602`, em outro ramo do plano de contas.

    CONTA EXATA, nunca prefixo: os dois pais tem contas que nao sao deducao.
    """

    def test_conteudo_congelado(self):
        from mappings import CLAS_DEDUCAO_EXATA
        assert CLAS_DEDUCAO_EXATA == ("30201010006", "301010602")

    def test_nao_colide_com_prefixos_deducao(self):
        # as duas fontes sao DISJUNTAS: nenhuma conta exata comeca por um
        # prefixo ja coberto (senao a mascara nova seria redundante e o
        # comentario da constante estaria mentindo sobre o motivo dela).
        from mappings import CLAS_DEDUCAO_EXATA, PREFIXOS_DEDUCAO
        for clas in CLAS_DEDUCAO_EXATA:
            assert not clas.startswith(PREFIXOS_DEDUCAO)


class TestMaskDeducao:
    """`_mask_deducao` = prefixo(PREFIXOS_DEDUCAO) OR clas EXATA.

    Superset estrito do idioma antigo: tudo que `_mask_prefixo` marcava
    continua marcado (retrocompat por construcao) e as contas exatas entram.
    """

    def _clas(self):
        return pd.Series([
            "3010201001",   # deducao classica (prefixo)
            "30201010006",  # conta exata nova
            "301010602",    # conta exata nova (outro ramo)
            "301010601",    # vizinha: (-)Deducoes e Descontos Incondicionais
            "3010101001",   # receita
            "302010101",    # Receita de Locacao de Stand - NAO entra
        ]).astype(str)

    def test_marca_prefixo_e_conta_exata(self):
        from transform import _mask_deducao
        assert _mask_deducao(self._clas()).tolist() == [
            True, True, True, False, False, False]

    def test_e_superset_do_mask_prefixo(self):
        from transform import _mask_deducao, _mask_prefixo
        from mappings import PREFIXOS_DEDUCAO
        clas = self._clas()
        antigo = _mask_prefixo(clas, PREFIXOS_DEDUCAO)
        novo = _mask_deducao(clas)
        assert (novo | antigo).equals(novo)

    def test_index_preservado(self):
        from transform import _mask_deducao
        s = pd.Series(["30201010006", "999"], index=[7, 42]).astype(str)
        m = _mask_deducao(s)
        assert list(m.index) == [7, 42]
        assert m.tolist() == [True, False]

    def test_prefixo_do_codigo_exato_nao_entra_sozinho(self):
        # o pai `3020101` NAO pode entrar: e "LUCROS RECEBIDOS SCP" numa
        # empresa e "RECEITA DE LOCACAO" noutra. Conta exata, nao prefixo.
        from transform import _mask_deducao
        s = pd.Series(["3020101", "30201", "3010106"]).astype(str)
        assert _mask_deducao(s).tolist() == [False, False, False]


class TestDeducaoArvoreAlternativa:
    """Deducao de ISS fora do 3010201.

    Sem a conta exata, a empresa exibia fonte='guia+retido' com valo_iss =
    retido sofrido (que NAO e o ISS que ela escriturou) e caia em
    'sem_deducao' nos meses sem guia — o rotulo que afirma ausencia de
    deducao em silencio, com valor escriturado no balancete.
    """
    @staticmethod
    def _cenario(bal_rows, imp_rows, retido=None, emp=1, comp="2026-03",
                 receita=903205.61):
        regime = pd.DataFrame({"codi_emp": [emp], "competencia": [comp],
                               "regime": ["real_presumido"]})
        bal = pd.DataFrame(
            [{"codi_emp": emp, "competencia": comp, "clas_cta": "3010101001",
              "nome_cta": "Receita Servicos", "valo_deb": 0.0,
              "valo_cre": receita}] + bal_rows)
        imp = pd.DataFrame(imp_rows) if imp_rows else pd.DataFrame(
            columns=["codi_emp", "competencia", "codi_imp", "srec_sim",
                     "bcal_sim"])
        fat = pd.DataFrame({"codi_emp": [emp], "competencia": [comp],
                            "valo_saidas": [0.0], "valo_servicos": [receita]})
        ret = (pd.DataFrame([{"codi_emp": emp, "competencia": comp,
                              "valo_retido": retido}])
               if retido is not None else None)
        out = build_impostos_destacados_mensais(bal, imp, regime, fat, ret)
        assert len(out) == 1
        return out.iloc[0]

    @staticmethod
    def _ded(clas, nome, valor, emp=1, comp="2026-03"):
        return {"codi_emp": emp, "competencia": comp, "clas_cta": clas,
                "nome_cta": nome, "valo_deb": valor, "valo_cre": 0.0}

    @staticmethod
    def _guia(codi_imp, valor, emp=1, comp="2026-03"):
        return {"codi_emp": emp, "competencia": comp, "codi_imp": codi_imp,
                "srec_sim": valor, "bcal_sim": 0.0}

    # ── conta exata 30201010006 ─────────────────────────────────────────
    def test_conta_exata_vira_o_valo_iss(self):
        # balancete ( - ) ISS 45.160,28 na conta 30201010006, guia de ISS
        # zero, retido sofrido 16.443,00, e as guias de PIS e COFINS que a
        # empresa nao escritura.
        row = self._cenario(
            [self._ded("30201010006", "( - ) ISS", 45160.28)],
            [self._guia(4, 2137.59), self._guia(5, 9865.80)],
            retido=16443.00)
        assert row["valo_iss"] == pytest.approx(45160.28)

    def test_o_retido_sai_quando_a_conta_existe(self):
        # o portao e POR IMPOSTO: com a conta de (-)ISS visivel, guia(3) e
        # retido saem os DOIS. O que sobra no total e balancete + PIS/COFINS
        # de guia (que a empresa nao escritura).
        row = self._cenario(
            [self._ded("30201010006", "( - ) ISS", 45160.28)],
            [self._guia(3, 500.0), self._guia(4, 2137.59),
             self._guia(5, 9865.80)],
            retido=16443.00)
        assert row["valo_impostos"] == pytest.approx(57163.67)
        assert row["fonte"] == "balancete+guia"

    def test_sem_a_conta_exata_seria_o_complemento(self):
        # controle NEGATIVO: a MESMA forma com a conta num clas que ninguem
        # reconhece volta a exibir o complemento — prova que o numero novo
        # vem da mascara, nao de outra mudanca.
        row = self._cenario(
            [self._ded("30209999999", "( - ) ISS", 45160.28)],
            [self._guia(4, 2137.59), self._guia(5, 9865.80)],
            retido=16443.00)
        assert row["valo_iss"] == pytest.approx(16443.00)

    # ── meses que caiam em 'sem_deducao' ────────────────────────────────
    @pytest.mark.parametrize("comp,valor,receita", [
        ("2025-12", 10250.0, 205000.0),
        ("2026-01",  9500.0, 190000.0),
        ("2026-02", 16000.0, 320000.0),
    ])
    def test_sem_deducao_vira_balancete(self, comp, valor, receita):
        row = self._cenario(
            [self._ded("30201010006", "( - ) ISS", valor, comp=comp)],
            [], comp=comp, receita=receita)
        assert row["fonte"] == "balancete"
        assert row["valo_impostos"] == pytest.approx(valor)
        assert row["valo_iss"] == pytest.approx(valor)

    # ── arvore alternativa 301010602 ────────────────────────────────────
    def test_301010602_entra(self):
        # balancete 896,07 e guia 896,07 — as duas fontes batem AO CENTAVO.
        # O que muda e a fonte, nao o numero.
        row = self._cenario(
            [self._ded("301010602", "(-) ISS", 896.07, comp="2023-01")],
            [self._guia(3, 896.07, comp="2023-01")],
            comp="2023-01", receita=17921.47)
        assert row["fonte"] == "balancete"
        assert row["valo_impostos"] == pytest.approx(896.07)
        assert row["valo_iss"] == pytest.approx(896.07)

    def test_301010602_entra_outro_mes(self):
        # mesma forma, 864,00 nas duas fontes
        row = self._cenario(
            [self._ded("301010602", "(-) ISS", 864.0, comp="2024-05")],
            [self._guia(3, 864.0, comp="2024-05")],
            comp="2024-05", receita=20000.0)
        assert row["valo_iss"] == pytest.approx(864.0)
        assert row["fonte"] == "balancete"

    # ── guards: o filtro de NOME e o segundo cinto ──────────────────────
    def test_outras_receitas_no_mesmo_codigo_nao_entra(self):
        # linhas em 301010602 com nome "Outras Receitas". O codigo bate, o
        # nome nao — fica fora do
        # numerador E nao fecha o portao (a guia de ISS continua entrando).
        row = self._cenario(
            [self._ded("301010602", "Outras Receitas", 0.0)],
            [self._guia(3, 1000.0)])
        assert row["fonte"] == "guia"
        assert row["valo_impostos"] == pytest.approx(1000.0)
        assert row["valo_iss"] == pytest.approx(1000.0)

    def test_projeto_social_no_mesmo_codigo_nao_entra(self):
        # conta de projeto social no mesmo 301010602, com valor.
        row = self._cenario(
            [self._ded("301010602", "PROJETO SOCIAL - Convenio Municipal",
                       19.19)],
            [self._guia(3, 1000.0)])
        assert row["fonte"] == "guia"
        assert row["valo_impostos"] == pytest.approx(1000.0)

    def test_conta_pai_nao_entra(self):
        # `3020101` e "LUCROS RECEBIDOS SCP" numa empresa e "RECEITA DE
        # LOCACAO" noutra — se um dia alguem trocar a conta exata por
        # prefixo, este teste cai.
        row = self._cenario([self._ded("3020101", "( - ) ISS", 45160.28)],
                            [self._guia(3, 1000.0)])
        assert row["valo_iss"] == pytest.approx(1000.0)
        assert row["fonte"] == "guia"

    # ── guard de regime: o sinal NAO muda ───────────────────────────────
    def test_regime_nao_usa_as_contas_novas(self):
        # empresa 2 tem SO a conta exata no balancete e nenhuma guia: o
        # sinal de regime nao a alcanca, entao ela nao vira
        # 'balancete_inferido'.
        ef = pd.DataFrame({"codi_emp": [1], "codi_imp": [44],
                           "data_sim": ["2025-01-10"]})
        bal = pd.DataFrame([
            {"codi_emp": 2, "competencia": "2025-01",
             "clas_cta": "30201010006", "nome_cta": "( - ) ISS",
             "valo_deb": 10.0, "valo_cre": 0.0},
            {"codi_emp": 3, "competencia": "2025-01",
             "clas_cta": "301010602", "nome_cta": "(-) ISS",
             "valo_deb": 10.0, "valo_cre": 0.0},
        ])
        out = build_empresa_regime_periodo(ef, bal)
        assert set(out["codi_emp"]) == {1}
