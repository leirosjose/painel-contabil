import pandas as pd

import main as M
import raw_escopo as E


class TestRegistroCobreTodoRaw:
    def test_toda_tabela_do_raw_tem_escopo(self):
        # Tabela nova no RAW_KEYS sem escopo = a geracao dela nao teria regra
        # de autoridade. Falha alta aqui e melhor que fatia errada em producao.
        assert set(E.ESCOPOS) == set(M.RAW_KEYS)

    def test_globais_sao_os_dois_dicionarios(self):
        assert E.GLOBAIS == {"foeventosdef", "foclassificacoes_eventos"}


class TestEscopoImobiliario:
    """Recebimento de parcela de venda de imovel.

    Segue o padrao das ef* com data propria — janela pelo mes da coluna de
    data. Nada de novo no mecanismo: o que este bloco trava e que o escopo NAO
    foi declarado errado (fatia por empresa, ou `inteira`, que e o default
    silencioso de quem esquece).
    """

    def test_escopo_e_janela_por_data_pagamento(self):
        assert E.ESCOPOS["efimob_pagamentos"] == E.Escopo("janela", "data_pagamento")

    def test_fatia_e_o_mes_do_pagamento(self):
        df = pd.DataFrame({"codi_emp": [207, 207],
                           "data_pagamento": ["2026-01-09", "2026-03-30"]})
        assert E.fatias("efimob_pagamentos", df).tolist() == [202601, 202603]

    def test_pedido_cobre_so_a_janela_do_run(self):
        p = E.pedido("efimob_pagamentos", 207, "2026-01", "2026-03")
        assert p.cobre(207, 202601) and p.cobre(207, 202603)
        assert not p.cobre(207, 202512) and not p.cobre(207, 202604)
        assert not p.cobre(678, 202602)

    def test_typo_de_milenio_cai_fora_do_pedido(self):
        """Data de pagamento com typo de milenio (2201-11-14).

        A defesa e TRIPLA: o extractor
        filtra por data e ela nem chega ao raw; se chegasse, a fatia 220111
        excede o fatia_max e `_valida_gravacao` REJEITA a geracao (exit 3,
        ruidoso e bloqueante, nao silencioso); e o `_clampa_marts` ainda
        aparia a competencia do mart.
        """
        df = pd.DataFrame({"codi_emp": [228], "data_pagamento": ["2201-11-14"]})
        assert E.fatias("efimob_pagamentos", df).tolist() == [220111]
        p = E.pedido("efimob_pagamentos", None, "2023-01", "2026-03")
        assert not p.cobre(228, 220111)


class TestFatias:
    def test_janela_vira_aaaamm(self):
        df = pd.DataFrame({"codi_emp": [1, 1], "data_lan": ["2026-06-30", "2026-07-01"]})
        assert E.fatias("ctlancto", df).tolist() == [202606, 202607]

    def test_timestamp_com_hora_usa_so_o_mes(self):
        df = pd.DataFrame({"codi_emp": [1], "data_faturamento": ["2026-04-29 13:03:57.325983"]})
        assert E.fatias("hrfaturamento", df).tolist() == [202604]

    def test_data_ilegivel_ou_nula_vira_na(self):
        df = pd.DataFrame({"codi_emp": [1, 1], "dsai_sai": ["31/02/2025", None]})
        assert E.fatias("efsaidas", df).isna().all()

    def test_ate_fim_tambem_e_mes(self):
        df = pd.DataFrame({"codi_emp": [1], "admissao": ["1999-12-01"]})
        assert E.fatias("foempregados", df).tolist() == [199912]

    def test_corte_vira_aaaammdd(self):
        df = pd.DataFrame({"codi_emp": [1], "data_corte": ["2023-01-01"]})
        assert E.fatias("ctsaldo_inicial", df).tolist() == [20230101]

    def test_parcela_herda_o_mes_da_nota_mae(self):
        notas = pd.DataFrame({"codi_emp": [1, 1], "codi_ser": [10, 11],
                              "dser_ser": ["2026-03-15", "2026-04-02"]})
        par = pd.DataFrame({"codi_emp": [1, 1, 1], "codi_ser": [11, 10, 98],
                            "parc_serp": [1, 1, 1]})
        out = E.fatias("efservicospar", par, notas)
        assert out.iloc[0] == 202604 and out.iloc[1] == 202603
        assert pd.isna(out.iloc[2])                      # parcela sem nota: sem fatia

    def test_inteira_via_fomovto_e_global_ocupam_a_fatia_zero(self):
        g = pd.DataFrame({"codi_emp": [1, 2]})
        assert E.fatias("geempre", g).tolist() == [0, 0]
        assert E.fatias("foeventos", g).tolist() == [0, 0]
        d = pd.DataFrame({"i_eventos": [1]})
        assert E.fatias("foeventosdef", d).tolist() == [0]
        assert E.empresas("foeventosdef", d).tolist() == [0]


class TestPedido:
    def test_janela_cobre_so_o_intervalo_e_a_empresa(self):
        p = E.pedido("ctlancto", 201, "2025-01", "2025-12")
        assert p.cobre(201, 202501) and p.cobre(201, 202512)
        assert not p.cobre(201, 202412) and not p.cobre(201, 202601)
        assert not p.cobre(256, 202506)

    def test_todas_as_empresas(self):
        assert E.pedido("efsaidas", None, "2023-01", "2026-08").cobre(9999, 202403)

    def test_ate_fim_nao_tem_limite_inferior(self):
        p = E.pedido("foempregados", None, "2025-01", "2025-12")
        assert p.cobre(1, 199001) and not p.cobre(1, 202601)

    def test_corte_e_uma_fatia_so(self):
        p = E.pedido("ctsaldo_inicial", None, "2023-01", "2026-08")
        assert p.cobre(1, 20230101) and not p.cobre(1, 20260101)

    def test_via_fomovto_so_as_empresas_com_movimento(self):
        p = E.pedido("foeventos", None, "2026-01", "2026-03", empresas_fomovto={10, 20})
        assert p.cobre(10, 0) and not p.cobre(30, 0)

    def test_global_ignora_a_empresa_do_run(self):
        p = E.pedido("foeventosdef", 925, "2023-01", "2026-06")
        assert p.codi_emp is None and p.cobre(0, 0)


class TestImpressoes:
    @staticmethod
    def _df():
        return pd.DataFrame({"codi_emp": [1, 1, 1, 2],
                             "data_lan": ["2026-01-05", "2026-01-09", "2026-02-01", "2026-01-05"],
                             "vlor_lan": [10.0, 20.0, 5.0, 7.0], "nume_lan": [1, 2, 3, 4]})

    def test_uma_linha_por_fatia_com_a_contagem(self):
        df = self._df()
        imp = E.impressoes("ctlancto", df, E.fatias("ctlancto", df))
        got = {(r.codi_emp, r.fatia): r.linhas for r in imp.itertuples()}
        assert got == {(1, 202601): 2, (1, 202602): 1, (2, 202601): 1}

    def test_independe_da_ordem_das_linhas(self):
        df = self._df()
        a = E.impressoes("ctlancto", df, E.fatias("ctlancto", df))
        inv = df.iloc[::-1].reset_index(drop=True)
        b = E.impressoes("ctlancto", inv, E.fatias("ctlancto", inv))
        assert a.sort_values(["codi_emp", "fatia"]).impressao.tolist() == \
            b.sort_values(["codi_emp", "fatia"]).impressao.tolist()

    def test_muda_quando_um_valor_muda(self):
        df = self._df()
        a = E.impressoes("ctlancto", df, E.fatias("ctlancto", df))
        df2 = df.copy()
        df2.loc[0, "vlor_lan"] = 10.01
        b = E.impressoes("ctlancto", df2, E.fatias("ctlancto", df2))
        ia = dict(zip(zip(a.codi_emp, a.fatia), a.impressao))
        ib = dict(zip(zip(b.codi_emp, b.fatia), b.impressao))
        assert ia[(1, 202601)] != ib[(1, 202601)]
        assert ia[(1, 202602)] == ib[(1, 202602)]            # a outra fatia nao muda

    def test_df_vazio_devolve_frame_vazio(self):
        vazio = pd.DataFrame(columns=["codi_emp", "data_lan"])
        out = E.impressoes("ctlancto", vazio, E.fatias("ctlancto", vazio))
        assert out.empty and list(out.columns) == ["codi_emp", "fatia", "linhas", "impressao"]
