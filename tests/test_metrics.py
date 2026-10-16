# ---------------------------------------------------------------------------
# Testes de etl/metrics.py — orquestração unificada dos KPIs app × pdf.
# last_valid -> NaN; flags_inconsistencia c/ guard pd.isna.
# ---------------------------------------------------------------------------
import math

import pandas as pd
import pytest

from etl.metrics import (
    last_valid, series_mensais, kpis_agregados, flags_inconsistencia,
    folha_custo, impostos_exibidos, kpis_yoy, iss_carga_mensal,
    PISO_DENOMINADOR_MENSAL, piso_denominador,
    cards_saldo_invertido, NOTA_SALDO_INVERTIDO,
    folha_por_linha,
    legenda_abatimentos, LINHAS_FOLHA_RUBRICA, LINHAS_FOLHA_SEMPRE, NOTA_LINHA_13O,
    LINHA_ENCARGOS, LINHA_PCT_FAT, encargos_por_mes,
    tabela_impostos, LINHA_PCT_IMPOSTOS, LINHA_TOTAL_IMPOSTOS,
    encargos_sem_inss_patronal, NOTA_ENCARGOS_GUIA, NOTA_ENCARGOS_SIMPLES,
    impostos_sem_irpj_proprio,
    maiores_linhas, OUTROS_TOP_N, NOTA_TRAVESSAO_PCT, legenda_travessao,
    LINHA_13O,
    RUBRICAS_DESTAQUE_FOLHA, TITULO_DESTAQUES_FOLHA,
    SUBTITULO_DESTAQUES_FOLHA, LINHA_FOLHA_ENCARGOS, folha_destaques,
)

PERIODS = ["2025-01", "2025-02", "2025-03"]


class TestLastValid:
    def test_vazia_vira_nan(self):
        assert math.isnan(last_valid(pd.Series([], dtype=float)))

    def test_all_nan_vira_nan(self):
        assert math.isnan(last_valid(pd.Series([float("nan")] * 3)))

    def test_ultimo_real(self):
        assert last_valid(pd.Series([1.0, 2.0, float("nan")])) == 2.0

    def test_remove_inf(self):
        assert last_valid(pd.Series([1.0, float("inf")])) == 1.0

    def test_so_inf_vira_nan(self):
        assert math.isnan(last_valid(pd.Series([float("inf")])))


class TestFlagsInconsistencia:
    def test_bal_vazio_dispara_ambos(self):             # ac=pc=pl=at=0.0 -> (True, True)
        z = pd.Series(0.0, index=PERIODS)
        assert flags_inconsistencia(z, z, z, z) == (True, True)

    def test_saudavel_nao_dispara(self):
        ac = pd.Series([0, 0, 100000.0], index=PERIODS)
        pc = pd.Series([0, 0, 40000.0], index=PERIODS)
        pl = pd.Series([0, 0, 60000.0], index=PERIODS)
        at = pd.Series([0, 0, 100000.0], index=PERIODS)
        assert flags_inconsistencia(ac, pc, pl, at) == (False, False)

    def test_pl_ausente_simples_nao_conta_como_insolvencia(self):
        z = pd.Series(0.0, index=PERIODS)               # pl all-zero -> pl_extraido False
        at = pd.Series([0, 0, 100000.0], index=PERIODS)
        ac = pd.Series([0, 0, 50000.0], index=PERIODS)
        pc = pd.Series([0, 0, 30000.0], index=PERIODS)
        assert flags_inconsistencia(ac, pc, z, at) == (False, False)


class TestKpisAgregados:                                # SUM/SUM
    def test_pct_imp_med_e_sum_sobre_sum(self):
        s = {"fat_pm": pd.Series([100_000.0, 400_000.0]),
             "imp_pm": pd.Series([10_000.0, 20_000.0]),
             "rec_imp_pm": pd.Series([100_000.0, 400_000.0]),
             "folha_pm": pd.Series([0.0, 0.0]), "lair_pm": pd.Series([0.0, 0.0])}
        assert kpis_agregados(s)["pct_imp_med"] == pytest.approx(6.0)   # 30/500, não média 7.5

    def test_pct_imp_med_den_zero_vira_nan(self):
        s = {"fat_pm": pd.Series([0.0]), "imp_pm": pd.Series([float("nan")]),
             "rec_imp_pm": pd.Series([float("nan")]), "folha_pm": pd.Series([0.0]),
             "lair_pm": pd.Series([0.0])}
        assert math.isnan(kpis_agregados(s)["pct_imp_med"])


class TestFallbackIrpjContabil:
    """Empresa que PAGA IRPJ/CSLL mas nao emite a guia pelo modulo do
    Dominio exibe o valor pela CONTABILIDADE.

    Precedente: `GUIA_IMPORTAVEL_DESTACADOS` — o fallback entra SO
    onde a fonte primaria e vazia, nunca soma por cima.
    """
    # A janela e o ANO: com o eixo em competencia de trimestre, o debito de
    # abr-jun pousa em mar, jul-set em jun, out-dez em set e jan-mar no dez do
    # ano ANTERIOR. Fixture de 2 meses nao consegue exercitar isso.
    P = [f"2025-{m:02d}" for m in range(1, 13)]

    @staticmethod
    def _bal(rows):
        # `valo_saldo`/`codi_cta` sao contrato do balancete que OUTRAS series
        # do metrics consomem; aqui so precisam existir.
        d = pd.DataFrame(rows, columns=["competencia", "clas_cta",
                                        "nome_cta", "valo_deb"])
        d["valo_saldo"] = 0.0
        d["valo_cre"] = 0.0
        d["codi_emp"] = 1
        d["codi_cta"] = range(1, len(d) + 1)
        return d

    @staticmethod
    def _s(bal, imp=None, periods=None):
        e = pd.DataFrame()
        per = list(periods or TestFallbackIrpjContabil.P)
        fat = pd.DataFrame({"competencia": per,
                            "valo_saidas": [500_000.0] * len(per),
                            "valo_servicos": [0.0] * len(per)})
        return series_mensais(fat, e, e, e, bal, per,
                              imp_df=imp if imp is not None else e)

    def test_sem_guia_o_passivo_entra_em_DUAS_linhas(self):
        """Sem qualificador: "IRPJ" e "CSLL", identicas em FORMA as linhas de
        guia. O contador le um tributo, nao a proveniencia dele."""
        bal = self._bal([("2025-04", "201010206", "IRPJ", 60_000.0),
                         ("2025-05", "201010207", "CSLL", 40_000.0)])
        s = self._s(bal)
        assert set(s["irpj_contabil"]) == {"IRPJ", "CSLL"}
        assert float(s["irpj_contabil"]["IRPJ"].fillna(0).sum()) == pytest.approx(60_000.0)
        assert float(s["irpj_contabil"]["CSLL"].fillna(0).sum()) == pytest.approx(40_000.0)
        k = kpis_agregados(s)
        assert k["imp_total"] == pytest.approx(100_000.0)
        assert k["irpj_total"] == pytest.approx(100_000.0)

    def test_portao_guia_vence_o_periodo_inteiro(self):
        """Guia em UM mes basta pra bloquear o fallback nos 12.

        O Presumido apura por TRIMESTRE, entao ha guia em so 3-4 dos 12
        meses. Costurar mes a mes poria contabilidade nos meses sem guia e
        inflaria o numerador — o oposto do que a excecao quer.
        """
        bal = self._bal([("2025-04", "201010206", "IRPJ", 60_000.0),
                         ("2025-05", "201010207", "CSLL", 40_000.0)])
        imp = pd.DataFrame({"competencia": ["2025-01"], "codi_imp": [7],
                            "valo_devido": [10_000.0]})
        k = kpis_agregados(self._s(bal, imp=imp))
        assert k["imp_total"] == pytest.approx(10_000.0)   # so a guia
        assert k["irpj_total"] == pytest.approx(10_000.0)

    def test_parcelamento_e_retencao_ficam_de_fora(self):
        """Casam por NOME sem ser o imposto corrente: "Parc Simplificado
        IRPJ" (divida antiga renegociada) e "Retencao de Contribuicao Social"
        (imposto de TERCEIRO)."""
        bal = self._bal([("2025-04", "201010206", "Parc Simplificado IRPJ", 90_000.0),
                         ("2025-04", "201010207", "Retenção de Contribuição Social", 70_000.0),
                         ("2025-04", "201010206", "IRPJ", 5_000.0)])
        k = kpis_agregados(self._s(bal))
        assert k["imp_total"] == pytest.approx(5_000.0)

    def test_clas_certo_com_nome_de_OUTRO_tributo_nao_entra(self):
        """O `clas_cta` sozinho NAO serve: o `201010207` e "CSLL" na maioria
        mas e "COFINS Nao Cumulativa" e "IRRF S/ ALUGUEIS" em outras. O
        codigo e por empresa."""
        bal = self._bal([("2025-04", "201010207", "COFINS Não Cumulativa", 90_000.0),
                         ("2025-04", "201010207", "IRRF S/ ALUGUEIS", 5_749.29)])
        s = self._s(bal)
        assert s["irpj_contabil"] == {}

    def test_eixo_do_tempo_e_a_competencia_do_trimestre(self):
        """O IRPJ/CSLL do Presumido e apurado por TRIMESTRE e pago em ate tres
        quotas no trimestre SEGUINTE. Exibir no mes do desembolso espalharia o
        imposto por meses que nao o geraram — e a guia (`efsdoimp`) ja aparece
        na competencia, entao a mesma tabela teria dois eixos de tempo.

        As quotas de jul/ago/set pousam em JUN, as de out/nov/dez em SET, e
        as de jan/fev/mar de 2026 em DEZ/25 — por isso a janela precisa do
        balancete alargado em +3 meses.
        """
        bal = self._bal([("2025-07", "201010206", "IRPJ", 10.0),
                         ("2025-08", "201010206", "IRPJ", 10.0),
                         ("2025-09", "201010206", "IRPJ", 10.0),
                         ("2025-10", "201010207", "CSLL", 20.0),
                         ("2026-01", "201010206", "IRPJ", 70.0)])
        s = self._s(bal)
        ir = s["irpj_contabil"]["IRPJ"]
        cs = s["irpj_contabil"]["CSLL"]
        assert ir["2025-06"] == pytest.approx(30.0)    # jul+ago+set -> jun
        assert ir["2025-12"] == pytest.approx(70.0)    # jan/26 -> dez/25
        assert cs["2025-09"] == pytest.approx(20.0)    # out -> set
        assert pd.isna(ir["2025-07"]) and pd.isna(ir["2025-01"])

    def test_quota_de_janeiro_pertence_ao_ano_ANTERIOR(self):
        """Borda declarada: a janela de 12 meses passa a mostrar o ANO FISCAL,
        nao o ano de caixa. A quota paga em jan/25 liquida o 4o trimestre de
        2024: ela sai da janela de 2025.
        """
        bal = self._bal([("2025-01", "201010206", "IRPJ", 97_006.33)])
        s = self._s(bal)
        assert s["irpj_contabil"] == {}

    def test_provisao_nao_e_a_fonte(self):
        """`clas 40106` e COMPETENCIA. Com provisao maior que o pago, o
        criterio manda o pago."""
        bal = self._bal([("2025-04", "4010601", "IRPJ", 248_275.93),
                         ("2025-04", "201010206", "IRPJ", 140_181.40)])
        k = kpis_agregados(self._s(bal))
        assert k["imp_total"] == pytest.approx(140_181.40)


class TestNumeradorGuia:
    """O % Impostos mede a GUIA RECOLHIDA: "o numero e o que pagou".

    O IRPJ/CSLL PROVISIONADO nao entra no numerador — provisao e
    competencia, nao pagamento.
    """
    P = ["2025-01", "2025-02"]

    @staticmethod
    def _s(imp=None, impd=None, fat=None, dre=None, periods=None):
        e = pd.DataFrame()
        return series_mensais(
            fat if fat is not None else e, impd if impd is not None else e,
            e, e, e, list(periods or TestNumeradorGuia.P),
            dre_df=dre, imp_df=imp if imp is not None else e)

    @staticmethod
    def _fat(periods, v):
        return pd.DataFrame({"competencia": periods,
                             "valo_saidas": [v] * len(periods),
                             "valo_servicos": [0.0] * len(periods)})

    def test_numerador_e_a_guia_propria(self):
        # Escala em MILHARES por causa do piso de materialidade do
        # denominador (PISO_DENOMINADOR_MENSAL x janela): fixture pequena e
        # exatamente a empresa que o piso existe pra suprimir.
        imp = pd.DataFrame({"competencia": self.P, "codi_imp": [1, 1],
                            "valo_devido": [60_000.0, 40_000.0]})
        k = kpis_agregados(self._s(imp=imp, fat=self._fat(self.P, 500_000.0)))
        assert k["imp_total"] == pytest.approx(100_000.0)
        assert k["pct_imp_med"] == pytest.approx(10.0)

    def test_provisao_de_irpj_NAO_entra_mais(self):
        """O numerador nao soma `dre_mensal.provisao_irpj_csll`. Provisao e
        competencia — o card mede desembolso.
        """
        imp = pd.DataFrame({"competencia": self.P, "codi_imp": [1, 1],
                            "valo_devido": [60.0, 40.0]})
        dre = pd.DataFrame({"codi_emp": [1, 1], "competencia": self.P,
                            "receita_bruta": [0.0, 0.0], "despesas": [0.0, 0.0],
                            "lair": [0.0, 0.0], "resultado_liquido": [0.0, 0.0],
                            "provisao_irpj_csll": [900.0, 0.0]})
        k = kpis_agregados(self._s(imp=imp, fat=self._fat(self.P, 500.0), dre=dre))
        assert k["imp_total"] == pytest.approx(100.0)   # sem os 900

    def test_retencao_de_terceiro_fica_fora(self):
        """Guia da empresa, imposto de TERCEIRO. O 18 e ISS Retido."""
        imp = pd.DataFrame({"competencia": ["2025-01", "2025-01"],
                            "codi_imp": [1, 18], "valo_devido": [60.0, 999.0]})
        k = kpis_agregados(self._s(imp=imp, fat=self._fat(["2025-01"], 600.0),
                                   periods=["2025-01"]))
        assert k["imp_total"] == pytest.approx(60.0)

    def test_iss_retido_na_fonte_entra_pela_carga(self):
        """ISS 100% retido pelo tomador nunca vira guia da empresa, mas o
        imposto E dela. Ler guia PURA o tiraria do numerador."""
        imp = pd.DataFrame({"competencia": ["2025-01"], "codi_imp": [1],
                            "valo_devido": [100_000.0]})
        impd = pd.DataFrame({"competencia": ["2025-01"], "valo_impostos": [0.0],
                             "base_receita": [0.0],
                             "valo_iss": [400_000.0], "fonte": ["retido"]})
        k = kpis_agregados(self._s(imp=imp, impd=impd,
                                   fat=self._fat(["2025-01"], 1_000_000.0),
                                   periods=["2025-01"]))
        assert k["imp_total"] == pytest.approx(500_000.0)

    def test_so_retencao_declara_em_vez_de_afirmar_zero(self):
        """Empresa com guias TODAS de imposto de terceiro (so o codi_imp 18,
        ISS Retido). Ela escritura deducao no balancete, entao 0,00% seria
        FALSO.
        """
        imp = pd.DataFrame({"competencia": ["2025-01"], "codi_imp": [18],
                            "valo_devido": [500.0]})
        s = self._s(imp=imp, fat=self._fat(["2025-01"], 1000.0),
                    periods=["2025-01"])
        k = kpis_agregados(s)
        assert s["imp_so_retencao"] is True
        assert math.isnan(k["pct_imp_med"])      # nunca 0,00%
        assert k["imp_sem_dado"] is False        # a frase e a OUTRA

    def test_irpj_total_soma_a_guia_e_nao_a_provisao(self):
        """A linha "IRPJ/CSLL do periodo" do card. Em empresa que recolhe por
        DARF fora do modulo de guias do Dominio ela some — custo DECLARADO."""
        imp = pd.DataFrame({"competencia": ["2025-01"] * 3,
                            "codi_imp": [1, 7, 6],
                            "valo_devido": [10.0, 70.0, 30.0]})
        dre = pd.DataFrame({"codi_emp": [1], "competencia": ["2025-01"],
                            "receita_bruta": [0.0], "despesas": [0.0],
                            "lair": [0.0], "resultado_liquido": [0.0],
                            "provisao_irpj_csll": [555.0]})
        k = kpis_agregados(self._s(imp=imp, fat=self._fat(["2025-01"], 1000.0),
                                   dre=dre, periods=["2025-01"]))
        assert k["irpj_total"] == pytest.approx(100.0)   # 70+30, nao 555

    def test_sem_guia_nenhuma_e_ausencia_nao_zero(self):
        s = self._s(fat=self._fat(["2025-01"], 1000.0), periods=["2025-01"])
        k = kpis_agregados(s)
        assert math.isnan(k["pct_imp_med"])
        assert k["imp_sem_dado"] is True
        assert s["imp_so_retencao"] is False


class TestCoerenciaCardImpostos:
    # Coerência do card % Impostos: o par exibido (R$ do subtítulo e %) DEVE
    # fechar na calculadora do leitor. Denominador do % = fat_pm (o
    # faturamento QUE O CARD EXIBE, NF), nunca base_receita (que pra R/P é
    # receita CONTÁBIL). Cobre os 3 regimes; vale pros 2 consumidores por
    # construção: app (st.metric) e pdf (hero context) consomem o MESMO
    # K["pct_imp_med"]/K["imp_total"] de kpis_agregados — fonte única.
    def _series(self, impd, fat, periods=("2025-01", "2025-02"), imp=None):
        """O numerador e a GUIA (`imp_df`), nao o destacado.

        O par R$/% fecha e o denominador e o `fat_pm` EXIBIDO. `impd`
        continua entrando: e dele que vem o ISS pela carga e o `fonte` que
        separa o Simples.
        """
        vazio = pd.DataFrame()
        return series_mensais(fat, impd, vazio, vazio, vazio, list(periods),
                              imp_df=imp if imp is not None else vazio)

    @staticmethod
    def _guia(rows):
        """Guia propria minima: [(competencia, codi_imp, valo_devido), ...]."""
        return pd.DataFrame(rows, columns=["competencia", "codi_imp",
                                           "valo_devido"])

    def _assert_par_fecha(self, k, s):
        # invariante permanente: R$ exibido / faturamento exibido == % exibido
        assert k["pct_imp_med"] == pytest.approx(
            k["imp_total"] / float(s["fat_pm"].sum()) * 100)

    def test_rp_par_fecha_mesmo_com_base_contabil_divergente(self):
        # R/P (fonte=balancete): base_receita contábil 100 != fat NF 120 ->
        # o % divide pelo fat exibido, não pela base do mart.
        fat = pd.DataFrame({"competencia": ["2025-01", "2025-02"],
                            "valo_saidas": [120_000.0, 0.0], "valo_servicos": [0.0, 0.0]})
        impd = pd.DataFrame({"competencia": ["2025-01"],
                             "valo_impostos": [15_000.0], "base_receita": [100_000.0]})
        imp = self._guia([("2025-01", 1, 15_000.0)])
        s = self._series(impd, fat, imp=imp)
        k = kpis_agregados(s)
        assert k["pct_imp_med"] == pytest.approx(15.0 / 120.0 * 100)   # razao intacta
        self._assert_par_fecha(k, s)

    def test_simples_par_fecha(self):
        # Simples (fonte=efsdoimp): base == fat por construção — segue fechando.
        fat = pd.DataFrame({"competencia": ["2025-01", "2025-02"],
                            "valo_saidas": [150_000.0, 50_000.0], "valo_servicos": [0.0, 0.0]})
        impd = pd.DataFrame({"competencia": ["2025-01", "2025-02"],
                             "valo_impostos": [9_000.0, 3_000.0],
                             "base_receita": [150_000.0, 50_000.0]})
        imp = self._guia([("2025-01", 44, 9_000.0), ("2025-02", 44, 3_000.0)])
        s = self._series(impd, fat, imp=imp)
        k = kpis_agregados(s)
        assert k["pct_imp_med"] == pytest.approx(6.0)
        self._assert_par_fecha(k, s)

    def test_sem_deducao_sem_numerador_vira_nan_sem_flag(self):
        # fonte='sem_deducao': valo_impostos NULL -> card "—", sem flag de fat-zero.
        fat = pd.DataFrame({"competencia": ["2025-01"],
                            "valo_saidas": [100.0], "valo_servicos": [0.0]})
        impd = pd.DataFrame({"competencia": ["2025-01"],
                             "valo_impostos": [float("nan")], "base_receita": [100.0]})
        s = self._series(impd, fat, periods=("2025-01",))
        k = kpis_agregados(s)
        assert math.isnan(k["pct_imp_med"])
        assert math.isnan(k["imp_total"])
        assert k["imp_sem_fat"] is False

    def _impd_sem_numerador(self, fontes, periods=("2025-01", "2025-02")):
        return pd.DataFrame({"codi_emp": [1] * len(fontes),
                             "competencia": list(periods),
                             "valo_impostos": [float("nan")] * len(fontes),
                             "base_receita": [float("nan")] * len(fontes),
                             "fonte": list(fontes)})

    def _fat_2m(self):
        return pd.DataFrame({"competencia": ["2025-01", "2025-02"],
                             "valo_saidas": [120_000.0, 0.0],
                             "valo_servicos": [0.0, 0.0]})

    def test_imp_sem_dado_distingue_ausencia_de_zero_afirmado(self):
        # "Se nao tem informacao aparece sem informacao": o card nao pode
        # AFIRMAR "R$ 0,00 de impostos" em cima de numerador ausente.
        k =kpis_agregados(self._series(
            self._impd_sem_numerador(["sem_dado", "sem_dado"]), self._fat_2m()))
        assert math.isnan(k["imp_total"])
        assert k["imp_sem_dado"] is True

    def test_guia_declarada_e_zerada_mantem_o_zero_afirmado(self):
        """No universo da guia, o zero afirmado exige DECLARAR — ter linha de
        guia propria, ainda que zerada. Quem declarou e nao deveu nada AFIRMA
        R$ 0,00; quem nao tem linha nenhuma nao tem o que afirmar.
        """
        imp = self._guia([("2025-01", 1, 0.0), ("2025-02", 1, 0.0)])
        k = kpis_agregados(self._series(
            self._impd_sem_numerador(["sem_deducao", "sem_dado"]),
            self._fat_2m(), imp=imp))
        assert k["imp_sem_dado"] is False

    def test_dict_minimo_sem_a_serie_preserva_o_comportamento_antigo(self):
        # Fallback CONSERVADOR (idioma do enc_pm/rec_cont): chave ausente NAO
        # pode inverter exibicao — sem a serie, assume escriturado -> R$ 0,00.
        s = {"fat_pm": pd.Series([100.0], index=["2025-01"]),
             "imp_pm": pd.Series([float("nan")], index=["2025-01"]),
             "folha_pm": pd.Series([0.0], index=["2025-01"]),
             "lair_pm": pd.Series([0.0], index=["2025-01"])}
        assert kpis_agregados(s)["imp_sem_dado"] is False

    def test_imposto_sem_faturamento_nf_vira_nan_com_flag(self):
        # Borda declarada do fix: imposto > 0 com fat NF ZERO (contabilidade na
        # matriz) -> % incalculável ("—") + flag pro label curto dos consumers.
        fat = pd.DataFrame()
        impd = pd.DataFrame({"competencia": ["2025-01"],
                             "valo_impostos": [50.0], "base_receita": [80.0]})
        imp = self._guia([("2025-01", 1, 50.0)])
        s = self._series(impd, fat, periods=("2025-01",), imp=imp)
        k = kpis_agregados(s)
        assert math.isnan(k["pct_imp_med"])
        assert k["imp_total"] == pytest.approx(50.0)
        assert k["imp_sem_fat"] is True

    def test_serie_mensal_divide_pelo_fat(self):
        # pct_imp_pm (chart/spark) usa a MESMA base do card — sem drift série×hero.
        fat = pd.DataFrame({"competencia": ["2025-01", "2025-02"],
                            "valo_saidas": [200.0, 100.0], "valo_servicos": [0.0, 0.0]})
        impd = pd.DataFrame({"competencia": ["2025-01", "2025-02"],
                             "valo_impostos": [10.0, 8.0], "base_receita": [170.0, 90.0]})
        imp = self._guia([("2025-01", 1, 10.0), ("2025-02", 1, 8.0)])
        s = self._series(impd, fat, imp=imp)
        assert s["pct_imp_pm"]["2025-01"] == pytest.approx(5.0)
        assert s["pct_imp_pm"]["2025-02"] == pytest.approx(8.0)


class TestSeriesMensais:
    def test_valo_total_soma_as_tres_origens(self):
        # A 3a origem (parcelas imobiliarias) entra no MESMO
        # numero que o card exibe. Nao ha "faturamento de NF" e "faturamento
        # total" como dois universos -- e um so.
        fat = pd.DataFrame({"competencia": ["2025-01"], "valo_saidas": [100.0],
                            "valo_servicos": [40.0], "valo_imobiliario": [25.0]})
        vazio = pd.DataFrame()
        s = series_mensais(fat, vazio, vazio, vazio, vazio, ["2025-01"])
        assert s["fat_pm"].iloc[0] == pytest.approx(165.0)

    def test_valo_imobiliario_ausente_degrada_pra_zero(self):
        # Banco PRE-CARGA: o mart ainda nao tem a coluna. Guard igual ao do valo_servicos -- nunca KeyError.
        fat = pd.DataFrame({"competencia": ["2025-01"], "valo_saidas": [100.0],
                            "valo_servicos": [40.0]})
        vazio = pd.DataFrame()
        s = series_mensais(fat, vazio, vazio, vazio, vazio, ["2025-01"])
        assert s["fat_pm"].iloc[0] == pytest.approx(140.0)

    def test_bal_vazio_liq_c_vira_nan(self):            # base do card no-data -> "—"
        vazio = pd.DataFrame()
        s = series_mensais(vazio, vazio, vazio, vazio, vazio, PERIODS)
        assert s["liq_c"].isna().all()


class TestBalanceteSoForaDaJanela:
    def test_carry_in_puro_sem_linha_na_janela_vira_ausencia(self):
        # bal_serie_df JA vem seedado com a ultima linha PRE-janela
        # (competencia < periods[0]) pra ancorar o ffill. Se NENHUMA linha de
        # conta de balanco (clas 1/2) cai DENTRO da janela, o numero que
        # ac/pc/... exibiriam seria 100% herdado de fora do periodo -- "zero
        # afirma, ausencia declara": vira NaN, nao o saldo emprestado.
        periods = ["2026-01", "2026-02", "2026-03"]
        bal = pd.DataFrame({
            "codi_emp": [1, 1],
            "competencia": ["2025-06", "2025-06"],   # SO fora da janela (seed)
            "clas_cta": ["10101", "20101"],
            "valo_saldo": [100000.0, 40000.0],
        })
        s = series_mensais(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
                           pd.DataFrame(), bal, periods)
        assert s["ac"].isna().all()
        assert s["pc"].isna().all()
        assert s["est"].isna().all()
        assert s["elp"].isna().all()
        assert s["pl"].isna().all()
        assert s["at"].isna().all()
        assert s["liq_c"].isna().all()
        assert s["liq_s"].isna().all()
        assert s["end_p"].isna().all()
        assert s["bal_tem_dado_periodo"] is False

    def test_com_pelo_menos_1_linha_na_janela_mantem_o_numero(self):
        # Contraste: 1 linha DENTRO da janela basta pra nao disparar --
        # regressao-zero do caso normal (empresa em dia).
        periods = ["2026-01", "2026-02", "2026-03"]
        bal = pd.DataFrame({
            "codi_emp": [1, 1],
            "competencia": ["2026-01", "2026-01"],
            "clas_cta": ["10101", "20101"],
            "valo_saldo": [100000.0, 40000.0],
        })
        s = series_mensais(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
                           pd.DataFrame(), bal, periods)
        assert s["ac"].iloc[-1] == pytest.approx(100000.0)
        assert s["bal_tem_dado_periodo"] is True


class TestBalanceteSemMovimentoNoPeriodo:
    def test_ultimo_mes_sem_linha_real_mantem_valor_e_flag(self):
        # "So exibicao": HA linha real dentro da janela (Jan), so NAO no
        # ultimo mes (Mar, ffilled) -- MANTEM o saldo carregado (patrimonial
        # e cumulativo, apagar seria errado) e liga a flag pro consumer
        # declarar "sem movimento no periodo".
        periods = ["2026-01", "2026-02", "2026-03"]
        bal = pd.DataFrame({
            "codi_emp": [1, 1],
            "competencia": ["2026-01", "2026-01"],
            "clas_cta": ["10101", "20101"],
            "valo_saldo": [100000.0, 40000.0],
        })
        s = series_mensais(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
                           pd.DataFrame(), bal, periods)
        assert s["ac"].iloc[-1] == pytest.approx(100000.0)   # ffill, NAO NaN
        assert s["bal_ultimo_periodo_sem_movimento"] is True

    def test_ultimo_mes_com_linha_real_nao_dispara(self):
        # Contraste: movimento no ULTIMO mes -> flag False (caso normal).
        periods = ["2026-01", "2026-02", "2026-03"]
        bal = pd.DataFrame({
            "codi_emp": [1, 1],
            "competencia": ["2026-03", "2026-03"],
            "clas_cta": ["10101", "20101"],
            "valo_saldo": [100000.0, 40000.0],
        })
        s = series_mensais(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
                           pd.DataFrame(), bal, periods)
        assert s["bal_ultimo_periodo_sem_movimento"] is False

    def test_sem_dado_nenhum_na_janela_nao_dispara_F2(self):
        # As duas flags sao MUTUAMENTE EXCLUSIVAS: sem NENHUM dado na janela
        # quem dispara e bal_tem_dado_periodo=False; "sem movimento" exige
        # que HAJA dado real na janela, so nao no ultimo mes.
        periods = ["2026-01", "2026-02", "2026-03"]
        bal = pd.DataFrame({
            "codi_emp": [1, 1],
            "competencia": ["2025-06", "2025-06"],
            "clas_cta": ["10101", "20101"],
            "valo_saldo": [100000.0, 40000.0],
        })
        s = series_mensais(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
                           pd.DataFrame(), bal, periods)
        assert s["bal_tem_dado_periodo"] is False
        assert s["bal_ultimo_periodo_sem_movimento"] is False


class TestNaoMutacao:                                   # blinda invariante "não muta o caller"
    def _inputs(self):
        fat = pd.DataFrame({"competencia": ["2025-01", "2025-02"],
                            "valo_saidas": [100.0, 200.0], "valo_servicos": [10.0, 20.0]})
        impd = pd.DataFrame({"competencia": ["2025-01"], "valo_impostos": [5.0], "base_receita": [100.0]})
        folha = pd.DataFrame({"competencia": ["2025-01"], "valo_bruto": [30.0]})
        func = pd.DataFrame({"competencia": ["2025-01"], "is_partial": [False]})
        bal = pd.DataFrame({"codi_emp": [1], "competencia": ["2025-01"],
                            "clas_cta": ["10101"], "valo_saldo": [100.0]})
        return fat, impd, folha, func, bal

    def test_series_mensais_nao_muta_nenhum_input(self):
        # Hoje só garantido pelo .copy() em _fat_pm — quebraria calado num refactor.
        fat, impd, folha, func, bal = self._inputs()
        befores = [d.copy(deep=True) for d in (fat, impd, folha, func, bal)]
        series_mensais(fat, impd, folha, func, bal, ["2025-01", "2025-02"])
        for before, after, nome in zip(befores, (fat, impd, folha, func, bal),
                                       ["fat", "impd", "folha", "func", "bal"]):
            pd.testing.assert_frame_equal(before, after, obj=nome)


class TestFlagsTrailingGapCarryForward:                 # ffill do ultimo mes
    def test_balancete_ate_penultimo_nao_dispara(self):
        # Empresa SAUDAVEL com balancete só até o PENÚLTIMO mês (último ausente):
        # bal_grupo_serie ffill carrega o saldo de Fev pro Mar -> last_valid lê o
        # saldo real (não 0.0) -> flag NÃO dispara (empresa só atrasada não vira
        # "inconsistente"). O caso oposto (último mês realmente 0.0) seria finding.
        periods = ["2025-01", "2025-02", "2025-03"]
        bal = pd.DataFrame({
            "codi_emp": [1] * 6,
            "competencia": ["2025-01", "2025-02"] * 3,
            "clas_cta": ["10101", "10101", "20101", "20101", "20401", "20401"],
            "valo_saldo": [100000.0, 100000.0, 40000.0, 40000.0, 60000.0, 60000.0],
        })
        s = series_mensais(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), bal, periods)
        assert s["ac"].iloc[-1] == 100000.0    # Mar ffilled de Fev (não 0.0)
        assert flags_inconsistencia(s["ac"], s["pc"], s["pl"], s["at"]) == (False, False)


class TestLucratividadeContabil:                       # resultado (DRE) / receita contabil
    # O numerador e o LAIR (antes do IRPJ/CSLL) — ver TestIRMudaDeCasa. As
    # fixturas daqui tem provisao ZERO, entao lair == resultado_liquido.
    def _fat(self, periods, valor=1000.0):
        return pd.DataFrame({"competencia": periods,
                             "valo_saidas": [valor] * len(periods),
                             "valo_servicos": [0.0] * len(periods)})

    def _dre(self, comps, receita, despesas, resultado, emp=1):
        return pd.DataFrame({
            "codi_emp": [emp] * len(comps), "competencia": comps,
            "receita_bruta": receita, "despesas": despesas,
            "lair": resultado, "provisao_irpj_csll": [0.0] * len(comps),
            "resultado_liquido": resultado,
        })

    def _s(self, periods, dre=None, fat_mes=1000.0):
        return series_mensais(self._fat(periods, fat_mes), pd.DataFrame(),
                              pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
                              periods, dre_df=dre)

    def test_pct_luc_e_liquido_sobre_receita_contabil(self):
        # denominador = receita CONTABIL (dre), NAO fat_NF.
        periods = ["2025-01", "2025-02"]
        s = self._s(periods, self._dre(periods, [1000.0, 1000.0], [800.0, 800.0], [200.0, 100.0]))
        assert s["pct_luc_pm"].tolist() == pytest.approx([20.0, 10.0])
        assert kpis_agregados(s)["pct_luc_med"] == pytest.approx(15.0)   # 300/2000

    def test_sinal_negativo_em_mes_de_prejuizo(self):
        # mes de prejuizo mostra NEGATIVO (nao lucro). Escala em milhoes por
        # causa do piso mensal do denominador.
        periods = ["2025-03", "2025-04"]
        s = self._s(periods,
                    self._dre(periods, [700_000.0, 950_000.0],
                              [730_000.0, 960_000.0], [-24_000.0, -1_000.0]),
                    fat_mes=1_000_000.0)
        assert (s["pct_luc_pm"] < 0).all()

    def test_dre_none_all_nan_sem_crash(self):
        # retrocompat: caller antigo sem dre_df -> series all-NaN, sem crash.
        s = self._s(["2025-01"])
        assert s["pct_luc_pm"].isna().all()
        assert s["luc_sem_despesas"] is True
        assert math.isnan(kpis_agregados(s)["pct_luc_med"])

    def test_dre_vazio_dispara_trigger(self):
        s = self._s(["2025-01"], pd.DataFrame())
        assert s["luc_sem_despesas"] is True
        assert s["pct_luc_pm"].isna().all()

    def test_sem_despesa_suprime_mesmo_com_receita(self):
        # empresa so-receita (despesas 0): trigger dispara -> pct_luc NaN (evita
        # ~100% falso), mesmo com receita e resultado presentes.
        s = self._s(["2025-01"], self._dre(["2025-01"], [1000.0], [0.0], [1000.0]))
        assert s["luc_sem_despesas"] is True
        assert s["pct_luc_pm"].isna().all()

    def test_prejuizo_contabil_preserva_negativo(self):
        # despesa real e resultado LIQUIDO negativo: trigger NAO dispara -> pct_luc
        # negativo preservado (o sinal que o proxy NF invertia). Receita 200 vs
        # fat 1000 = 20% de cobertura (acima do guard de 10%).
        periods = ["2025-01", "2025-02"]
        s = self._s(periods, self._dre(periods, [200_000.0, 200_000.0],
                                       [300_000.0, 300_000.0],
                                       [-100_000.0, -100_000.0]))
        assert s["luc_sem_despesas"] is False
        assert (s["pct_luc_pm"] < 0).all()              # -100/200 cada mes
        assert kpis_agregados(s)["pct_luc_med"] == pytest.approx(-50.0)

    def test_receita_zero_com_despesa_cai_no_guard_de_cobertura(self):
        # borda: despesa presente mas receita 0 -> guard de cobertura dispara
        # (0 < 10% do fat) -> pct NaN + fallback GENERICO nos consumers (nao o
        # rotulo de despesas — luc_sem_despesas segue False).
        s = self._s(["2025-01"], self._dre(["2025-01"], [0.0], [50.0], [-50.0]))
        assert s["luc_sem_despesas"] is False
        assert s["luc_receita_incompleta"] is True
        assert s["pct_luc_pm"].isna().all()

    def test_cobertura_receita_1pct_suprime(self):
        # receita contabil ~1% do fat_NF (honorarios vivem no modulo hr, fora
        # do razao) -> a margem sairia absurda. Guard de cobertura: pct NaN,
        # flag propria, e NAO o rotulo de despesas.
        periods = ["2025-01", "2025-02", "2025-03"]
        s = self._s(periods, self._dre(periods, [10.0] * 3, [200.0] * 3, [-190.0] * 3))
        assert s["luc_receita_incompleta"] is True
        assert s["luc_sem_despesas"] is False
        assert s["pct_luc_pm"].isna().all()
        assert math.isnan(kpis_agregados(s)["pct_luc_med"])

    def test_cobertura_parcial_5pct_suprime(self):
        # cobertura ~5% (escrituracao parcial de resultado) ->
        # suprime pelo mesmo guard.
        s = self._s(["2025-01"], self._dre(["2025-01"], [50.0], [30.0], [20.0]))
        assert s["luc_receita_incompleta"] is True
        assert s["pct_luc_pm"].isna().all()

    def test_cobertura_completa_91pct_mostra(self):
        # empresa com escrituracao completa (~91%): guard NAO dispara.
        s = self._s(["2025-01"],
                    self._dre(["2025-01"], [910_000.0], [700_000.0], [210_000.0]),
                    fat_mes=1_000_000.0)          # cobertura segue 91%
        assert s["luc_receita_incompleta"] is False
        assert s["pct_luc_pm"].iloc[0] == pytest.approx(210.0 / 910.0 * 100)

    def test_consolidacao_multi_emp_por_competencia(self):
        # matriz+filial: series_mensais agrupa por competencia (soma as empresas).
        periods = ["2025-01"]
        dre = pd.concat([
            self._dre(periods, [600.0], [400.0], [200.0], emp=1),
            self._dre(periods, [400.0], [300.0], [100.0], emp=2),
        ], ignore_index=True)
        s = self._s(periods, dre)
        assert s["pct_luc_pm"].iloc[0] == pytest.approx(30.0)   # 300/1000


class TestFolhaCusto:
    """UM custo: a folha de CUSTO exclui SO antecipacao (tp 41/51/92) e
    INCLUI o 13o integral (tp52). Mes que pagou 13o sobe.
    """
    def _folha_todos_tps(self):
        # tp 11 mensal + 41 adiantamento + 51/52 13o + 60 ferias, 2 meses
        return pd.DataFrame({
            "competencia": ["2025-01"] * 5 + ["2025-02"] * 5,
            "tipo_process": [11, 41, 51, 52, 60] * 2,
            "valo_bruto": [1000.0, 100.0, 300.0, 500.0, 50.0,
                           2000.0, 100.0, 0.0, 700.0, 80.0],
        })

    def test_folha_pm_inclui_13o_e_exclui_antecipacao(self):
        periods = ["2025-01", "2025-02"]
        s = series_mensais(pd.DataFrame(), pd.DataFrame(), self._folha_todos_tps(),
                           pd.DataFrame(), pd.DataFrame(), periods)
        # 1000 tp11 + 300 tp51 + 500 tp52 (fora: 100 tp41 e 50 tp60)
        # o tp60 (recibo de ferias) fica fora e o tp51 entra — a 1a
        # parcela do 13o e desembolso real no mes em que sai, e quem neutraliza
        # a duplicidade e o estorno abatido no mes do fechamento.
        assert s["folha_pm"]["2025-01"] == pytest.approx(1800.0)
        assert s["folha_pm"]["2025-02"] == pytest.approx(2700.0)

    def test_o_13o_integral_e_custo_e_o_mes_de_dezembro_sobe(self):
        # nov/dez sobem por definicao. O tp51
        # (1a parcela) NAO sobe junto — ele e estornado dentro do tp52.
        periods = ["2025-01", "2025-02"]
        s = series_mensais(pd.DataFrame(), pd.DataFrame(), self._folha_todos_tps(),
                           pd.DataFrame(), pd.DataFrame(), periods)
        out = folha_custo(self._folha_todos_tps())
        assert set(out["tipo_process"]) == {11, 51, 52}
        assert s["folha_pm"]["2025-01"] - 1300.0 == pytest.approx(500.0)

    def test_pct_fol_med_usa_a_folha_de_custo(self):
        periods = ["2025-01", "2025-02"]
        fat = pd.DataFrame({"competencia": periods,
                            "valo_saidas": [10000.0, 10000.0], "valo_servicos": [0.0, 0.0]})
        s = series_mensais(fat, pd.DataFrame(), self._folha_todos_tps(),
                           pd.DataFrame(), pd.DataFrame(), periods)
        k = kpis_agregados(s)
        assert k["pct_fol_med"] == pytest.approx((1800.0 + 2700.0) / 20000.0 * 100)

    def test_recibo_de_ferias_sai_e_o_custo_vira_o_do_mensal(self):
        """A dupla contagem de ferias.

        O recibo (tp60) paga adiantado e o mensal (tp11) reconhece a mesma
        ferias por competencia, descontando o ja pago. Somar os dois conta 2x.

        A fixture reproduz o mecanismo: recibo de 300 em janeiro, parcelas de
        200 e 100 no mensal de jan e fev. O custo tem de ver 300 no total —
        nao 600.
        """
        folha = pd.DataFrame({
            "competencia": ["2025-01", "2025-01", "2025-02"],
            "tipo_process": [60, 11, 11],
            "valo_bruto": [300.0, 200.0, 100.0]})
        periods = ["2025-01", "2025-02"]
        s = series_mensais(pd.DataFrame(), pd.DataFrame(), folha,
                           pd.DataFrame(), pd.DataFrame(), periods)
        assert s["folha_pm"]["2025-01"] == pytest.approx(200.0)
        assert s["folha_pm"]["2025-02"] == pytest.approx(100.0)
        assert float(s["folha_pm"].sum()) == pytest.approx(300.0)

    def test_sem_coluna_tipo_process_passa_inalterado(self):
        # compat: consumers/testes que montam folha JA agregada (sem tipo_process)
        folha = pd.DataFrame({"competencia": ["2025-01"], "valo_bruto": [30.0]})
        s = series_mensais(pd.DataFrame(), pd.DataFrame(), folha,
                           pd.DataFrame(), pd.DataFrame(), ["2025-01"])
        assert s["folha_pm"]["2025-01"] == pytest.approx(30.0)
        assert folha_custo(folha) is folha              # sem coluna -> mesmo objeto

    def test_folha_custo_nao_muta_input(self):
        folha = self._folha_todos_tps()
        before = folha.copy(deep=True)
        out = folha_custo(folha)
        pd.testing.assert_frame_equal(folha, before)
        assert 41 not in out["tipo_process"].values
        assert 52 in out["tipo_process"].values       # 13o integral e CUSTO
        # a 1a parcela tambem e CUSTO, no mes em que e paga
        assert 51 in out["tipo_process"].values


class TestNoDataMinCount:                               # min_count=1: sem dado -> NaN, não 0.0
    def test_impd_vazio_imp_e_rec_viram_nan(self):
        periods = ["2025-01", "2025-02"]
        s = series_mensais(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
                           pd.DataFrame(), pd.DataFrame(), periods)
        assert s["imp_pm"].isna().all()
        assert s["rec_imp_pm"].isna().all()

    def test_impd_grupo_todo_nan_preserva_nan(self):
        # impd NÃO-vazio mas com mês de valores all-NaN: min_count=1 preserva NaN
        # (groupby.sum sem min_count viraria 0.0 = imposto/receita fabricado).
        periods = ["2025-01", "2025-02"]
        impd = pd.DataFrame({"competencia": ["2025-01", "2025-01"],
                             "valo_impostos": [float("nan"), float("nan")],
                             "base_receita": [float("nan"), float("nan")]})
        s = series_mensais(pd.DataFrame(), impd, pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), periods)
        assert math.isnan(s["imp_pm"]["2025-01"])
        assert math.isnan(s["rec_imp_pm"]["2025-01"])


class TestSeriesParidadeContabil:                       # caixa + despesas e custos
    def _bal(self):
        # caixa (1010101x) com saldo so em jan -> ffill carrega fev/mar;
        # banco (1010102x) NAO entra no caixa (armadilha "Caixa Economica");
        # despesas 401/405 = movimento mensal; 406 (IRPJ/CSLL) so em fev —
        # entra em despesas_pm (401-406) mas NAO em desp_custos_pm (401-405),
        # espelho da linha DESPESAS E CUSTOS.
        return pd.DataFrame({
            "codi_emp":    [1] * 6,
            "competencia": ["2025-01", "2025-01", "2025-01", "2025-01",
                            "2025-02", "2025-02"],
            "clas_cta":    ["1010101001", "1010102001", "4010101001",
                            "4050101001", "4010101001", "4060101001"],
            "valo_saldo":  [100.0, 999.0, 50.0, 10.0, 60.0, 30.0],
        })

    def test_caixa_pm_ffill_patrimonial(self):
        s = series_mensais(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
                           pd.DataFrame(), self._bal(), PERIODS)
        assert s["caixa_pm"]["2025-01"] == pytest.approx(100.0)
        assert s["caixa_pm"]["2025-02"] == pytest.approx(100.0)   # ffill, nao 0
        assert s["caixa_pm"]["2025-03"] == pytest.approx(100.0)

    def test_caixa_pm_exclui_bancos(self):
        s = series_mensais(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
                           pd.DataFrame(), self._bal(), PERIODS)
        assert s["caixa_pm"]["2025-01"] < 999.0                   # 1010102 fora

    def test_desp_custos_pm_exclui_406(self):
        s = series_mensais(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
                           pd.DataFrame(), self._bal(), PERIODS)
        assert s["desp_custos_pm"]["2025-01"] == pytest.approx(60.0)   # 50+10
        assert s["desp_custos_pm"]["2025-02"] == pytest.approx(60.0)   # SEM os 30 do 406
        assert s["desp_custos_pm"]["2025-03"] == pytest.approx(0.0)    # resultado: sem ffill
        assert s["despesas_pm"]["2025-02"] == pytest.approx(90.0)      # 401-406 mantem o 406

    def test_bal_vazio_series_zeradas(self):
        vazio = pd.DataFrame()
        s = series_mensais(vazio, vazio, vazio, vazio, vazio, PERIODS)
        assert (s["caixa_pm"] == 0.0).all()                       # chart suprimido no consumer
        assert (s["desp_custos_pm"] == 0.0).all()

    def test_grupos_bal_tem_chaves_novas(self):
        from etl.mappings import GRUPOS_BAL
        assert GRUPOS_BAL["Despesas e Custos"] == ("401", "402", "403", "404", "405")
        assert GRUPOS_BAL["Ativo Total"] == "1"


class TestEncargosFielAGuia:                            # enc_pm / % Folha + Encargos
    def _fat(self, periods):
        return pd.DataFrame({"competencia": periods,
                             "valo_saidas": [10000.0] * len(periods),
                             "valo_servicos": [0.0] * len(periods)})

    def _folha(self, periods):
        return pd.DataFrame({"competencia": periods,
                             "tipo_process": [11] * len(periods),
                             "valo_bruto": [1000.0] * len(periods)})

    def _enc(self):
        # jan: 3 componentes parciais (100 inss + 40 fgts + 10 resc = 150);
        # fev: so fgts (50); mar: SEM guia (nenhuma linha).
        return pd.DataFrame({
            "codi_emp":     [1, 2, 1],
            "competencia":  ["2025-01", "2025-01", "2025-02"],
            "inss_empresa": [100.0, float("nan"), float("nan")],
            "fgts":         [40.0, float("nan"), 50.0],
            "fgts_resc":    [float("nan"), 10.0, float("nan")],
        })

    def test_enc_pm_null_aware(self):
        periods = ["2025-01", "2025-02", "2025-03"]
        s = series_mensais(self._fat(periods), pd.DataFrame(), self._folha(periods),
                           pd.DataFrame(), pd.DataFrame(), periods,
                           encargos_df=self._enc())
        assert s["enc_pm"]["2025-01"] == pytest.approx(150.0)   # componente NULL nao soma
        assert s["enc_pm"]["2025-02"] == pytest.approx(50.0)
        assert pd.isna(s["enc_pm"]["2025-03"])                  # mes sem guia -> NaN

    def test_pct_folenc_mes_sem_guia_e_folha_only(self):
        periods = ["2025-01", "2025-02", "2025-03"]
        s = series_mensais(self._fat(periods), pd.DataFrame(), self._folha(periods),
                           pd.DataFrame(), pd.DataFrame(), periods,
                           encargos_df=self._enc())
        assert s["pct_folenc_pm"]["2025-01"] == pytest.approx(11.5)   # (1000+150)/10000
        assert s["pct_folenc_pm"]["2025-03"] == pytest.approx(10.0)   # folha-only

    def test_kpis_enc_total_e_pct_folenc_med(self):
        periods = ["2025-01", "2025-02", "2025-03"]
        s = series_mensais(self._fat(periods), pd.DataFrame(), self._folha(periods),
                           pd.DataFrame(), pd.DataFrame(), periods,
                           encargos_df=self._enc())
        k = kpis_agregados(s)
        assert k["enc_total"] == pytest.approx(200.0)
        assert k["pct_folenc_med"] == pytest.approx((3000.0 + 200.0) / 30000.0 * 100)

    def test_retrocompat_sem_encargos_df(self):
        periods = ["2025-01", "2025-02"]
        s = series_mensais(self._fat(periods), pd.DataFrame(), self._folha(periods),
                           pd.DataFrame(), pd.DataFrame(), periods)
        k = kpis_agregados(s)
        assert s["enc_pm"].isna().all()                          # sem guia nenhuma
        assert math.isnan(k["enc_total"])                        # contexto -> "encargos: —"
        assert k["pct_folenc_med"] == pytest.approx(k["pct_fol_med"])


class TestLucSemDespesas:                               # trigger na DESPESA da DRE
    """Flag `luc_sem_despesas`: True quando o periodo nao tem movimento em contas
    de DESPESA da DRE (dre_mensal.despesas = clas 4 excl provisao 40106). Empresa
    so-receita cai aqui (evita ~100% de margem falsa). Consumers usam a flag pro
    rotulo unificado "Sem registros de despesas no periodo"."""

    def _dre(self, despesas):
        return pd.DataFrame({"codi_emp": [1], "competencia": ["2025-01"],
                             "receita_bruta": [1000.0], "despesas": [despesas],
                             "lair": [1000.0 - despesas],
                             "provisao_irpj_csll": [0.0],
                             "resultado_liquido": [1000.0 - despesas]})

    def test_flag_true_sem_despesa_na_dre(self):
        s = series_mensais(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
                           pd.DataFrame(), pd.DataFrame(), ["2025-01"], dre_df=self._dre(0.0))
        assert s["luc_sem_despesas"] is True
        assert s["lair_pm"].isna().all()

    def test_flag_false_com_despesa_na_dre(self):
        s = series_mensais(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
                           pd.DataFrame(), pd.DataFrame(), ["2025-01"], dre_df=self._dre(200.0))
        assert s["luc_sem_despesas"] is False


class TestKpisYoy:                                      # YoY delta-no-card
    """Comparativo "vs mesmo periodo do ano anterior".

    Pareamento POSICIONAL de janelas inteiras ([inicio,fim] vs [-12m]) e
    agregacao identica via kpis_agregados nos dois lados (SUM/SUM).
    SEM mascara por valor: pro fat, zero e dado real (atual zerado = queda
    -100%, nunca aparado). Invariante por
    card: delta XOR label "sem dados de {ref}", ou ambos None quando o
    PROPRIO atual nao tem o KPI.
    """
    CUR3 = ["2025-01", "2025-02", "2025-03"]
    PREV3 = ["2024-01", "2024-02", "2024-03"]

    @staticmethod
    def _fat(periods, valor):
        return pd.DataFrame({"competencia": periods,
                             "valo_saidas": [valor] * len(periods),
                             "valo_servicos": [0.0] * len(periods)})

    @staticmethod
    def _impd(periods, imp, rec):
        return pd.DataFrame({"competencia": periods,
                             "valo_impostos": imp, "base_receita": rec})

    @staticmethod
    def _folha(periods, valor):
        return pd.DataFrame({"competencia": periods,
                             "valo_bruto": [valor] * len(periods)})

    @staticmethod
    def _dre(periods, receita, despesas, resultado):
        # provisao zero -> lair == resultado_liquido (o YoY nao depende do IR)
        n = len(periods)
        return pd.DataFrame({"codi_emp": [1] * n, "competencia": periods,
                             "receita_bruta": receita, "despesas": despesas,
                             "lair": resultado, "provisao_irpj_csll": [0.0] * n,
                             "resultado_liquido": resultado})

    @staticmethod
    def _s(periods, fat=None, impd=None, folha=None, dre=None):
        """O numerador do % Impostos e a GUIA.

        A fixture deriva a guia do proprio `impd` (mesmos valores, um tributo
        so) porque o que estes testes exercitam e o PAREAMENTO das janelas, nao
        a composicao do numerador — e o YoY tem de ler a MESMA base dos dois
        lados.
        """
        e = pd.DataFrame()
        imp = e
        if impd is not None and not impd.empty:
            imp = pd.DataFrame({"competencia": impd["competencia"],
                                "codi_imp": 1,
                                "valo_devido": impd["valo_impostos"]})
        return series_mensais(fat if fat is not None else e,
                              impd if impd is not None else e,
                              folha if folha is not None else e,
                              e, e, periods, dre_df=dre, imp_df=imp)

    def _s_completo(self, periods, fat_mes=1_000_000.0):
        # atual com os 4 KPIs validos (fat, imp, folenc, luc com cobertura ok).
        # Escala em MILHOES: o piso de materialidade do denominador
        # (PISO_DENOMINADOR_MENSAL x janela) suprime razao sobre denominador
        # irrisorio, e fixture de R$ 1.000/mes representa exatamente a empresa
        # que o piso existe pra suprimir.
        n = len(periods)
        return self._s(periods,
                       fat=self._fat(periods, fat_mes),
                       impd=self._impd(periods, [100_000.0] * n, [1_000_000.0] * n),
                       folha=self._folha(periods, 100_000.0),
                       dre=self._dre(periods, [900_000.0] * n, [700_000.0] * n,
                                     [120_000.0] * n))

    def test_fat_delta_relativo_percentual(self):
        yoy = kpis_yoy(self._s(self.CUR3, fat=self._fat(self.CUR3, 1200.0)),
                       self._s(self.PREV3, fat=self._fat(self.PREV3, 1000.0)))
        assert yoy["ref"] == "2024"
        assert yoy["fat"]["delta"] == pytest.approx(20.0)
        assert yoy["fat"]["label"] is None
        assert yoy["fat"]["prev"] == pytest.approx(3000.0)

    def test_pct_cards_delta_em_pontos_percentuais(self):
        # 12% atual vs 10% anterior -> +2.0 p.p. (NUNCA +20 "% de %")
        cur = self._s(self.CUR3,
                      fat=self._fat(self.CUR3, 1000.0),
                      impd=self._impd(self.CUR3, [120.0] * 3, [1000.0] * 3),
                      folha=self._folha(self.CUR3, 120.0),
                      dre=self._dre(self.CUR3, [1000.0] * 3, [880.0] * 3, [120.0] * 3))
        prev = self._s(self.PREV3,
                       fat=self._fat(self.PREV3, 1000.0),
                       impd=self._impd(self.PREV3, [100.0] * 3, [1000.0] * 3),
                       folha=self._folha(self.PREV3, 100.0),
                       dre=self._dre(self.PREV3, [1000.0] * 3, [900.0] * 3, [100.0] * 3))
        yoy = kpis_yoy(cur, prev)
        for card in ("imp", "folenc", "luc"):
            assert yoy[card]["delta"] == pytest.approx(2.0), card
            assert yoy[card]["label"] is None, card

    def test_prev_ponderado_sum_sum_nao_media_simples(self):
        # prev: meses 10% (10/100) e 50% (200/400) -> ponderado 210/500 = 42%,
        # nao media simples 30%. cur 12% -> delta -30.0 p.p. (denominador =
        # fat NF exibido -> fixture ganha fat espelhando a base).
        fat_cur = pd.DataFrame({"competencia": self.CUR3[:2],
                                "valo_saidas": [100_000.0, 100_000.0],
                                "valo_servicos": [0.0, 0.0]})
        fat_prev = pd.DataFrame({"competencia": self.PREV3[:2],
                                 "valo_saidas": [100_000.0, 400_000.0],
                                 "valo_servicos": [0.0, 0.0]})
        cur = self._s(self.CUR3[:2], fat=fat_cur,
                      impd=self._impd(self.CUR3[:2], [12_000.0, 12_000.0],
                                      [100_000.0, 100_000.0]))
        prev = self._s(self.PREV3[:2], fat=fat_prev,
                       impd=self._impd(self.PREV3[:2], [10_000.0, 200_000.0],
                                       [100_000.0, 400_000.0]))
        yoy = kpis_yoy(cur, prev)
        assert yoy["imp"]["prev"] == pytest.approx(42.0)
        assert yoy["imp"]["delta"] == pytest.approx(12.0 - 42.0)

    def test_prev_vazio_todos_os_cards_com_label(self):
        yoy = kpis_yoy(self._s_completo(self.CUR3), self._s(self.PREV3))
        for card in ("fat", "imp", "folenc", "luc"):
            assert yoy[card]["label"] == "sem dados de 2024", card
            assert yoy[card]["delta"] is None, card

    def test_fat_prev_zero_vira_label_nunca_inf(self):
        yoy = kpis_yoy(self._s(self.CUR3, fat=self._fat(self.CUR3, 1200.0)),
                       self._s(self.PREV3, fat=self._fat(self.PREV3, 0.0)))
        assert yoy["fat"]["delta"] is None
        assert yoy["fat"]["label"] == "sem dados de 2024"

    def test_luc_suprimida_no_prev_vira_label(self):
        # prev sem despesa escriturada -> luc_sem_despesas -> pct NaN -> label
        prev = self._s(self.PREV3,
                       dre=self._dre(self.PREV3, [1_000_000.0] * 3, [0.0] * 3,
                                     [1_000_000.0] * 3))
        yoy = kpis_yoy(self._s_completo(self.CUR3), prev)
        assert yoy["luc"]["delta"] is None
        assert yoy["luc"]["label"] == "sem dados de 2024"

    def test_atual_sem_kpi_nem_delta_nem_label(self):
        # atual sem dre (card "—") -> nem delta nem label (ruido zero)
        cur = self._s(self.CUR3, fat=self._fat(self.CUR3, 1000.0))
        yoy = kpis_yoy(cur, self._s_completo(self.PREV3))
        assert yoy["luc"]["delta"] is None
        assert yoy["luc"]["label"] is None

    def test_atual_zerado_e_queda_100(self):
        # zero atual e DADO (nunca aparado): queda real de -100%.
        yoy = kpis_yoy(self._s(self.CUR3, fat=self._fat(self.CUR3, 0.0)),
                       self._s(self.PREV3, fat=self._fat(self.PREV3, 1000.0)))
        assert yoy["fat"]["delta"] == pytest.approx(-100.0)
        assert yoy["fat"]["label"] is None

    def test_ref_janela_cruzando_ano(self):
        cur = [str(p) for p in pd.period_range("2025-07", "2026-06", freq="M")]
        prev = [str(p) for p in pd.period_range("2024-07", "2025-06", freq="M")]
        yoy = kpis_yoy(self._s(cur, fat=self._fat(cur, 100.0)), self._s(prev))
        assert yoy["ref"] == "2024-25"
        assert yoy["fat"]["label"] == "sem dados de 2024-25"

    def test_pareamento_posicional_range_parcial(self):
        cur = [str(p) for p in pd.period_range("2026-01", "2026-06", freq="M")]
        prev = [str(p) for p in pd.period_range("2025-01", "2025-06", freq="M")]
        yoy = kpis_yoy(self._s(cur, fat=self._fat(cur, 1100.0)),
                       self._s(prev, fat=self._fat(prev, 1000.0)))
        assert yoy["fat"]["delta"] == pytest.approx(10.0)

    def test_prev_com_cobertura_parcial_soma_janela_calendario(self):
        # prev com dado em SO 3 dos 6 meses: soma o que existe (sem mascara) —
        # afirmacao verdadeira "vs mesmo periodo-calendario".
        cur = [str(p) for p in pd.period_range("2026-01", "2026-06", freq="M")]
        prev_fat = pd.DataFrame({"competencia": ["2025-04", "2025-05", "2025-06"],
                                 "valo_saidas": [1_000_000.0] * 3,
                                 "valo_servicos": [0.0] * 3})
        prev_periods = [str(p) for p in pd.period_range("2025-01", "2025-06", freq="M")]
        yoy = kpis_yoy(self._s(cur, fat=self._fat(cur, 1_000_000.0)),
                       self._s(prev_periods, fat=prev_fat))
        assert yoy["fat"]["prev"] == pytest.approx(3_000_000.0)
        assert yoy["fat"]["delta"] == pytest.approx(100.0)   # 6000 vs 3000


# ---------------------------------------------------------------------------
# IR muda de casa
# ---------------------------------------------------------------------------

class TestTp92ConsolidadoNoTp11:
    """tp=92 (RPA) sai da folha exibida.

    O tp=92 e o DETALHE (um registro por recibo de RPA emitido) e o tp=11 e a
    CONSOLIDACAO MENSAL do mesmo dinheiro: as rubricas sao identicas na origem
    (FOMOVTO x foeventos: SERVICOS FRETE AUTONOMO, INSS TRANSPORTADOR
    AUTONOMO, IRRF AUTONOMO...) e os N recibos somam o registro unico do tp=11.

    Excluir o DETALHE, nunca o consolidado: o dinheiro do autonomo segue
    contado UMA vez pelo tp=11 — por isso o rotulo "inclui autonomos (RPA)"
    continua verdadeiro.
    """
    PER = ["2025-01", "2025-02"]

    def _par_11_92(self):
        # mesma pessoa/mes, mesmo valor nos dois tps
        return pd.DataFrame({
            "competencia": ["2025-01", "2025-01", "2025-02", "2025-02"],
            "tipo_process": [11, 92, 11, 92],
            "valo_bruto": [36849.78, 36849.78, 23400.0, 23400.0],
        })

    def test_par_11_92_conta_uma_vez(self):
        s = series_mensais(pd.DataFrame(), pd.DataFrame(), self._par_11_92(),
                           pd.DataFrame(), pd.DataFrame(), self.PER)
        assert s["folha_pm"]["2025-01"] == pytest.approx(36849.78)
        assert s["folha_pm"]["2025-02"] == pytest.approx(23400.0)

    def test_valor_do_autonomo_nao_some_vem_pelo_tp11(self):
        # invariante do rotulo "inclui autonomos (RPA)": o dinheiro CONTINUA
        # na folha exibida (via consolidado), so deixa de entrar duas vezes.
        folha = self._par_11_92()
        out = folha_custo(folha)
        assert float(out["valo_bruto"].sum()) == pytest.approx(60249.78)
        assert set(out["tipo_process"]) == {11}

    def test_92_excluido_incondicionalmente(self):
        # sem gemeo na fixture: mesmo assim sai (o filtro e por tipo, nunca
        # por pareamento — pareamento seria heuristica fragil).
        folha = pd.DataFrame({"competencia": ["2025-01"], "tipo_process": [92],
                              "valo_bruto": [5000.0]})
        assert folha_custo(folha).empty

    def test_92_no_conjunto_canonico(self):
        from etl.mappings import TIPOS_PROCESS_ANTECIPACAO
        assert 92 in TIPOS_PROCESS_ANTECIPACAO
        # trava a composicao INTEIRA, membro a membro.
        assert TIPOS_PROCESS_ANTECIPACAO == frozenset({41, 60, 92})
        # os tps de fluxo real seguem DENTRO da folha de custo: o 52 (o 13o
        # integral e custo) e o 51 (a 1a parcela e desembolso real no mes
        # pago; quem neutraliza a duplicidade e o estorno abatido, nao a
        # exclusao do processo).
        for tp in (11, 42, 51, 52, 70):
            assert tp not in TIPOS_PROCESS_ANTECIPACAO
        # o 60 fica fora do custo: o recibo de ferias antecipa o que o
        # mensal reconhece por competencia
        assert 60 in TIPOS_PROCESS_ANTECIPACAO

    def test_pct_folha_cai_com_a_exclusao(self):
        fat = pd.DataFrame({"competencia": self.PER,
                            "valo_saidas": [100000.0, 100000.0],
                            "valo_servicos": [0.0, 0.0]})
        s = series_mensais(fat, pd.DataFrame(), self._par_11_92(),
                           pd.DataFrame(), pd.DataFrame(), self.PER)
        assert kpis_agregados(s)["pct_fol_med"] == pytest.approx(60249.78 / 200000.0 * 100)


class TestImpostosExibidos:
    """Tabela de impostos do app: so imposto PROPRIO da empresa.

    Dois universos saem, por motivos DIFERENTES e com constantes separadas:
    retencao de terceiros (o imposto e de outro) e SCP (o tributo e
    da sociedade em conta de participacao — o DARF em nome do ostensivo e
    veiculo de recolhimento, nao incidencia dele).
    O 83 "RET" FICA: o nome engana, mas e regime proprio.
    """
    def _imp(self, codigos, valor=100.0):
        return pd.DataFrame({
            "codi_emp": [1] * len(codigos),
            "competencia": ["2025-01"] * len(codigos),
            "codi_imp": codigos,
            "srec_sim": [valor] * len(codigos),
        })

    def test_retencao_de_terceiros_sai(self):
        out = impostos_exibidos(self._imp([44, 16, 18, 25, 26]))
        assert set(out["codi_imp"]) == {44}

    def test_scp_sai(self):
        out = impostos_exibidos(self._imp([44, 108, 109, 110, 111]))
        assert set(out["codi_imp"]) == {44}

    def test_scp_leva_o_adicional_de_irpj_junto(self):
        # `inc1_sim` tem valor em DOIS codigos — 7 (IRPJ) e 111 (SCP-IRPJ).
        # O 111 sai daqui por CODI_IMP_SCP, e o filtro e por codi_imp, entao
        # o adicional dele sai junto com o principal, sem codigo novo. Se
        # alguem trocar o filtro por valor, este teste cai.
        imp = pd.DataFrame({
            "codi_emp": [1, 1], "competencia": ["2025-01"] * 2,
            "codi_imp": [111, 7], "srec_sim": [1000.0, 89362.65],
            "srec_adicional": [500.0, 52752.06],
            "valo_devido": [1500.0, 142114.71],
        })
        out = impostos_exibidos(imp)
        assert set(out["codi_imp"]) == {7}
        assert float(out["srec_adicional"].sum()) == pytest.approx(52752.06)

    def test_83_ret_permanece(self):
        # regressao deliberada: o nome "RET" atrai "consertos" futuros
        out = impostos_exibidos(self._imp([83, 18]))
        assert set(out["codi_imp"]) == {83}

    def test_proprios_classicos_permanecem(self):
        codigos = [1, 2, 3, 4, 5, 6, 7, 9, 44, 88, 83]
        out = impostos_exibidos(self._imp(codigos))
        assert set(out["codi_imp"]) == set(codigos)

    def test_cinco_linhas_viram_uma(self):
        # 97% do exibido era imposto de terceiro; sobra o Simples.
        imp = pd.DataFrame({
            "codi_emp": [1] * 5, "competencia": ["2025-01"] * 5,
            "codi_imp": [26, 18, 44, 25, 16],
            "srec_sim": [63327.80, 37089.11, 3226.68, 1369.20, 432.00],
        })
        out = impostos_exibidos(imp)
        assert list(out["codi_imp"]) == [44]
        assert float(out["srec_sim"].sum()) == pytest.approx(3226.68)

    def test_so_retencao_fica_vazio_e_e_distinguivel(self):
        # empresa que so e agente de retencao: o consumer precisa
        # distinguir "tinha linha e o filtro zerou" de "nunca teve dado".
        imp = self._imp([18, 26])
        out = impostos_exibidos(imp)
        assert out.empty and not imp.empty

    def test_df_vazio_e_sem_coluna_passam_inalterados(self):
        vazio = pd.DataFrame()
        assert impostos_exibidos(vazio) is vazio
        sem_col = pd.DataFrame({"competencia": ["2025-01"], "srec_sim": [1.0]})
        assert impostos_exibidos(sem_col) is sem_col

    def test_nao_muta_o_input(self):
        imp = self._imp([44, 18])
        antes = imp.copy(deep=True)
        impostos_exibidos(imp)
        pd.testing.assert_frame_equal(imp, antes)

    def test_constantes_separadas_mesmo_destino(self):
        from etl.mappings import CODI_IMP_RETENCAO_TERCEIROS, CODI_IMP_SCP
        assert CODI_IMP_SCP == {108, 109, 110, 111}
        assert not (CODI_IMP_SCP & CODI_IMP_RETENCAO_TERCEIROS)   # motivos distintos


class TestSoRetencaoPredicado:
    """Predicado do label "so retencoes de terceiros".

    A visibilidade real da tabela e "proprio E com valor": o app ja escondia
    linha toda-zero (abs>0). Empresa pode ter PIS/COFINS/ISS PROPRIOS
    zerados junto das retencoes com valor — checar so "sobrou linha propria"
    dava falso-negativo e a tabela renderizava VAZIA em vez do label.
    """
    def _imp(self, pares):
        return pd.DataFrame({
            "codi_emp": [1] * len(pares), "competencia": ["2025-01"] * len(pares),
            "codi_imp": [c for c, _ in pares], "srec_sim": [v for _, v in pares],
        })

    def _so_retencao(self, imp_df):
        # espelho do predicado do app.py (mesma ordem: proprios -> com valor)
        proprios = impostos_exibidos(imp_df)
        com_valor = proprios[proprios["srec_sim"].abs() > 0] if not proprios.empty else proprios
        tinha_valor = (not imp_df.empty) and float(imp_df["srec_sim"].abs().sum()) > 0
        return tinha_valor and com_valor.empty

    def test_proprios_zerados_ainda_e_so_retencao(self):
        # retencoes COM valor + proprios ZERADOS -> label, nunca tabela vazia
        imp = self._imp([(25, 21394.09), (18, 17128.33), (38, 13783.84),
                         (4, 0.0), (5, 0.0), (3, 0.0), (183, 0.0)])
        assert self._so_retencao(imp) is True

    def test_com_proprio_de_valor_nao_dispara(self):
        imp = self._imp([(44, 3226.68), (26, 63327.80)])
        assert self._so_retencao(imp) is False

    def test_tudo_zerado_nao_e_so_retencao(self):
        # guias existem mas todas zeradas: e "sem dados", nao "so retencao"
        imp = self._imp([(18, 0.0), (3, 0.0)])
        assert self._so_retencao(imp) is False


class TestHeroUltimoMesFechado:
    """Cards do hero leem o ultimo mes FECHADO, nao o ultimo do range.

    Cenario: jan..mai/26, PDF gerado no inicio de mai: abr em fechamento
    (folha parcial, is_partial=True) e mai sem carga nenhuma. Os cards liam `.iloc[-1]` e somavam o range inteiro -> Funcionarios "0"
    com "-168 no periodo" (ausencia de dado virando demissao em massa) e
    media mensal de faturamento dividida por 5 meses com dado em 3.

    Regra (uma so pro hero inteiro): mes e ABERTO quando is_partial OU quando
    nao ha linha em NENHUM mart de fluxo do mes. Apara-se so o SUFIXO — buraco
    no meio nao reancora. Charts e tabelas seguem mostrando o range pedido.
    """
    PER5 = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05"]

    def _fat(self, comps, valor=1_000_000.0):
        # Escala em milhoes: o piso de materialidade suprimiria a janela prev
        # aparada (3 meses) desta classe.
        return pd.DataFrame({"competencia": comps, "valo_saidas": [valor] * len(comps),
                             "valo_servicos": [0.0] * len(comps)})

    def _func(self, comps, final, parcial=None):
        n = len(comps)
        return pd.DataFrame({
            "competencia": comps, "qtd_final": final,
            "qtd_inicial": [f - 1 for f in final],
            "qtd_admissoes": [1] * n, "qtd_rescisoes": [0] * n,
            "is_partial": parcial if parcial is not None else [False] * n,
        })

    def _s(self, periods, fat=None, func=None, folha=None):
        e = pd.DataFrame()
        return series_mensais(fat if fat is not None else e, e,
                              folha if folha is not None else e,
                              func if func is not None else e, e, periods)

    # ── cenario 1: cauda aberta ─────────────────────────────────────────
    def _cauda_aberta(self):
        fechados = ["2026-01", "2026-02", "2026-03"]
        folha = pd.DataFrame({"competencia": fechados + ["2026-04"],
                              "tipo_process": [11] * 4,
                              "valo_bruto": [400.0, 400.0, 400.0, 13.8]})
        func = self._func(fechados + ["2026-04"], [168, 167, 168, 0],
                          parcial=[False, False, False, True])
        return self._s(self.PER5, fat=self._fat(fechados), func=func, folha=folha)

    def test_apara_a_cauda_aberta(self):
        s = self._cauda_aberta()
        assert s["periods_efetivos"] == ["2026-01", "2026-02", "2026-03"]
        assert s["cauda_aberta"] == ["2026-04", "2026-05"]

    def test_card_funcionarios_le_o_ultimo_fechado(self):
        k = kpis_agregados(self._cauda_aberta())
        assert k["func_final"] == 168        # mar, nao o 0 de abr/mai
        # inicial = ATIVOS do 1o mes (mesma medida do final: fim-de-mes x
        # fim-de-mes). Trocar pra qtd_inicial mudaria range FECHADO.
        assert k["func_inicial"] == 168

    def test_media_mensal_divide_pelos_meses_com_dado(self):
        k = kpis_agregados(self._cauda_aberta())
        assert k["fat_total"] == pytest.approx(3_000_000.0)
        assert k["fat_media"] == pytest.approx(1_000_000.0)   # /3, nunca /5

    def test_series_cheias_pro_chart_e_tabela(self):
        # o corte e SO dos numeros de card: chart e movimentacao seguem
        # mostrando abr*/mai (o marcador de parcial ja existe)
        s = self._cauda_aberta()
        assert len(s["atv_pm"]) == 5
        assert len(s["fat_pm"]) == 5
        assert bool(s["is_partial_pm"]["2026-04"]) is True

    def test_rotulo_do_corte_gerado_no_metrics(self):
        # verbatim pros 2 consumers (mesmo padrao do label do YoY)
        assert self._cauda_aberta()["label_corte"] == "dados até Mar/26"

    def test_folha_avulsa_nao_fecha_o_mes(self):
        # mes com SO ferias adiantadas (tp=60) e nada mais — nenhuma folha mensal,
        # nenhum faturamento, nenhuma linha de funcionarios. "Tem linha de
        # folha" nao pode fechar o mes.
        fechados = ["2026-01", "2026-02", "2026-03"]
        folha = pd.DataFrame({"competencia": fechados + ["2026-05"],
                              "tipo_process": [11, 11, 11, 60],
                              "valo_bruto": [400.0, 400.0, 400.0, 38730.77]})
        s = self._s(["2026-01", "2026-02", "2026-03", "2026-05"],
                    fat=self._fat(fechados), func=self._func(fechados, [168, 167, 168]),
                    folha=folha)
        assert s["periods_efetivos"] == fechados
        assert s["cauda_aberta"] == ["2026-05"]

    # ── cenario 2: range todo fechado -> NADA muda ──────────────────────
    def test_range_fechado_nao_apara_e_nao_rotula(self):
        comps = ["2025-01", "2025-02", "2025-03"]
        s = self._s(comps, fat=self._fat(comps), func=self._func(comps, [10, 11, 12]))
        assert s["periods_efetivos"] == comps
        assert s["cauda_aberta"] == []
        assert s["label_corte"] is None          # sem ruido no range fechado
        k = kpis_agregados(s)
        assert k["fat_media"] == pytest.approx(1_000_000.0)
        assert k["func_final"] == 12

    # ── cenario 3: tudo aberto -> "—", nunca 0 ──────────────────────────
    def test_tudo_aberto_vira_nan_nunca_zero(self):
        s = self._s(self.PER5)                    # nenhum mart com linha
        assert s["periods_efetivos"] == []
        k = kpis_agregados(s)
        # janela VAZIA != faturamento zero: o card mostra "—", nunca "R$ 0,00"
        # (zero seria afirmar que a empresa nao vendeu).
        assert math.isnan(k["fat_total"])
        assert math.isnan(k["fat_media"])
        assert k["func_final"] is None            # card sem numero
        assert math.isnan(k["pct_imp_med"])

    def test_range_fechado_com_venda_zero_continua_zero(self):
        # a borda oposta: mes FECHADO sem venda e zero REAL, e o card deve
        # dizer "R$ 0,00" — nao pode virar "—" por tabela.
        comps = ["2025-01", "2025-02"]
        s = self._s(comps, fat=self._fat(comps, 0.0), func=self._func(comps, [5, 5]))
        assert s["periods_efetivos"] == comps
        assert kpis_agregados(s)["fat_total"] == 0.0

    def test_tudo_aberto_nao_inventa_yoy(self):
        # sem KPI no atual, o YoY nao pode dizer "-100%": delta e label ambos
        # None (invariante do kpis_yoy pros outros cards).
        cur = self._s(self.PER5)
        prev_p = ["2025-01", "2025-02", "2025-03", "2025-04", "2025-05"]
        prev = self._s(prev_p, fat=self._fat(prev_p), func=self._func(prev_p, [9] * 5))
        yoy = kpis_yoy(cur, prev)
        assert yoy["fat"]["delta"] is None
        assert yoy["fat"]["label"] is None

    # ── cenario 4: buraco NO MEIO nao reancora ──────────────────────────
    def test_buraco_no_meio_nao_reancora(self):
        comps = ["2025-01", "2025-03"]            # fev sem dado nenhum
        periods = ["2025-01", "2025-02", "2025-03"]
        s = self._s(periods, fat=self._fat(comps), func=self._func(comps, [10, 12]))
        assert s["periods_efetivos"] == periods   # so a CAUDA conta
        assert s["cauda_aberta"] == []
        assert kpis_agregados(s)["func_final"] == 12

    # ── YoY acompanha o corte ───────────────────────────────────────────
    def test_yoy_compara_janelas_do_mesmo_tamanho(self):
        cur = self._cauda_aberta()                       # efetivo = 3 meses
        prev_p = ["2025-01", "2025-02", "2025-03", "2025-04", "2025-05"]
        prev = self._s(prev_p, fat=self._fat(prev_p, 500_000.0),
                       func=self._func(prev_p, [100] * 5))
        yoy = kpis_yoy(cur, prev)
        # 3000 (3 meses) vs 1500 (os 3 PRIMEIROS de 2025), nao vs 2500
        assert yoy["fat"]["prev"] == pytest.approx(1_500_000.0)
        assert yoy["fat"]["delta"] == pytest.approx(100.0)


class TestIssCargaMensal:
    """Serie do componente ISS pro bloco Impostos.

    Lendo SO guias, empresa com ISS 100% retido na fonte exibiria ZERO.
    `valo_iss` do mart ja traz o valor certo por mes (balancete OU
    guia+retido); este helper so o
    prepara pro pivot, e e a fonte UNICA de app e pdf.

    PRESENCA e decidida por notna(), NUNCA por > 0: conta escriturada com
    ZERO significa "nao houve ISS no mes", e isso e informacao — a linha
    aparece e renderiza R$ 0,00 como as demais.
    """
    P = ["2025-01", "2025-02", "2025-03"]

    @staticmethod
    def _df(vals):
        return pd.DataFrame({
            "codi_emp": [1] * len(vals),
            "competencia": ["2025-01", "2025-02", "2025-03"][:len(vals)],
            "valo_iss": vals,
        })

    def test_serie_alinhada_aos_periodos(self):
        s = iss_carga_mensal(self._df([10.0, 20.0, 30.0]), self.P)
        assert list(s.index) == self.P
        assert list(s.values) == pytest.approx([10.0, 20.0, 30.0])

    def test_zero_e_valor_nao_ausencia(self):
        # conta de (-)ISS escriturada com 0: a linha EXISTE e mostra 0,00
        s = iss_carga_mensal(self._df([0.0, 0.0, 0.0]), self.P)
        assert s is not None
        assert list(s.values) == pytest.approx([0.0, 0.0, 0.0])

    def test_mes_sem_iss_vira_zero_na_serie(self):
        s = iss_carga_mensal(self._df([10.0, float("nan"), 30.0]), self.P)
        assert list(s.values) == pytest.approx([10.0, 0.0, 30.0])

    def test_all_nan_devolve_none(self):
        # Simples: nao ha componente ISS -> sem linha
        assert iss_carga_mensal(self._df([float("nan")] * 3), self.P) is None

    def test_df_vazio_devolve_none(self):
        assert iss_carga_mensal(pd.DataFrame(), self.P) is None

    def test_sem_a_coluna_devolve_none(self):
        # guard de 1o deploy: mart antigo, antes do rebuild com valo_iss
        df = pd.DataFrame({"codi_emp": [1], "competencia": ["2025-01"]})
        assert iss_carga_mensal(df, self.P) is None

    def test_agrega_filiais_do_mesmo_mes(self):
        df = pd.DataFrame({
            "codi_emp": [1, 2], "competencia": ["2025-01", "2025-01"],
            "valo_iss": [10.0, 5.0]})
        s = iss_carga_mensal(df, ["2025-01"])
        assert s.iloc[0] == pytest.approx(15.0)

    def test_periodo_fora_do_df_vira_zero(self):
        s = iss_carga_mensal(self._df([10.0]), self.P)
        assert list(s.values) == pytest.approx([10.0, 0.0, 0.0])


class TestPisoDenominador:
    """Razao CERTA sobre denominador desprezivel.

    Numero absurdo na tela — nao por erro de conta, mas por dividir por um
    denominador irrisorio. Os guards existentes barram denominador == 0
    (vira "—"); faltava barrar denominador IMATERIAL.

    Piso = PISO_DENOMINADOR_MENSAL x len(janela), pra 1 mes e 12 meses
    exigirem a mesma materialidade POR MES.
    """
    @staticmethod
    def _s(fat, folha=None, enc=None, lair=None, rec=None, n=12):
        per = [f"2025-{m:02d}" for m in range(1, n + 1)]

        def mk(v):
            return pd.Series([v / n] * n, index=per)

        return {
            "fat_pm": mk(fat),
            "imp_pm": mk(fat * 0.10),
            "rec_imp_pm": mk(fat),
            "folha_pm": mk(folha if folha is not None else 0.0),
            "enc_pm": mk(enc if enc is not None else 0.0),
            "lair_pm": mk(lair if lair is not None else 0.0),
            "receita_contabil_pm": mk(rec if rec is not None else fat),
            "periods_efetivos": per,
        }

    # ── o piso ───────────────────────────────────────────────────────────
    def test_piso_escala_com_a_janela(self):
        assert piso_denominador(1) == pytest.approx(PISO_DENOMINADOR_MENSAL)
        assert piso_denominador(12) == pytest.approx(PISO_DENOMINADOR_MENSAL * 12)

    def test_janela_vazia_nao_zera_o_piso(self):
        # len 0 nao pode virar piso 0 (deixaria tudo passar)
        assert piso_denominador(0) >= PISO_DENOMINADOR_MENSAL

    # ── casos nominais ───────────────────────────────────────────────────
    def test_receita_irrisoria_lucratividade_vira_traco(self):
        # receita bruta do ANO = R$ 299,86 contra LAIR de -483.841,69
        # -> exibia -161.355,9%
        k = kpis_agregados(self._s(fat=299.86, lair=-483841.69, rec=299.86))
        assert math.isnan(k["pct_luc_med"])
        assert k["luc_den_insuf"] is True

    def test_fat_irrisorio_pct_folenc_vira_traco(self):
        # folha 446.721,65 sobre faturamento 4.070,00 -> exibia 12.066,3%
        k = kpis_agregados(self._s(fat=4070.00, folha=446721.65))
        assert math.isnan(k["pct_folenc_med"])
        assert k["fat_den_insuf"] is True

    def test_fat_irrisorio_pct_impostos_vira_traco(self):
        # % Impostos > 100% (583,6% sobre fat 11.738,62)
        k = kpis_agregados(self._s(fat=11738.62))
        assert math.isnan(k["pct_imp_med"])
        assert k["fat_den_insuf"] is True

    # ── invariante: denominador saudavel NAO muda ────────────────────────
    def test_denominador_acima_do_piso_intocado(self):
        # faturamento de 38,8M em 12 meses
        k = kpis_agregados(self._s(fat=38_835_513.03, folha=8_560_539.29))
        assert k["pct_imp_med"] == pytest.approx(10.0)
        assert k["fat_den_insuf"] is False
        assert not math.isnan(k["pct_folenc_med"])

    def test_mes_isolado_com_faturamento_material_passa(self):
        # 1 mes, fat 4.021.065,73 -> piso e 1.000
        k = kpis_agregados(self._s(fat=4_021_065.73, n=1))
        assert not math.isnan(k["pct_imp_med"])
        assert k["fat_den_insuf"] is False

    def test_exatamente_no_piso_passa(self):
        # fronteira: >= piso passa, so ABAIXO suprime
        k = kpis_agregados(self._s(fat=PISO_DENOMINADOR_MENSAL * 12))
        assert not math.isnan(k["pct_imp_med"])
        assert k["fat_den_insuf"] is False

    # ── denominador zero segue com o comportamento antigo ────────────────
    def test_denominador_zero_continua_nan_sem_flag_de_piso(self):
        # zero ja e "—" sem o piso; a flag do piso e pra denominador
        # POSITIVO porem imaterial, senao o rotulo mentiria
        k = kpis_agregados(self._s(fat=0.0))
        assert math.isnan(k["pct_imp_med"])
        assert k["fat_den_insuf"] is False


class TestPisoDenominadorYoY:
    """YoY de faturamento com base abaixo do piso -> sem delta, com label."""
    @staticmethod
    def _s(fat, n=12, ano="2025"):
        per = [f"{ano}-{m:02d}" for m in range(1, n + 1)]

        def mk(v):
            return pd.Series([v / n] * n, index=per)

        return {
            "fat_pm": mk(fat), "imp_pm": mk(0.0), "rec_imp_pm": mk(fat),
            "folha_pm": mk(0.0), "enc_pm": mk(0.0), "lair_pm": mk(0.0),
            "receita_contabil_pm": mk(fat), "periods_efetivos": per,
        }

    def test_base_de_5_reais_suprime_o_delta(self):
        # delta de milhoes de % com base de R$ 5,00
        y = kpis_yoy(self._s(2_877_432.89, ano="2025"),
                     self._s(5.00, ano="2024"))
        assert y["fat"]["delta"] is None
        assert y["fat"]["label"] == "sem base comparável em 2024"

    def test_base_abaixo_do_piso_suprime(self):
        # DECISAO CONSCIENTE: crescimento de 39x e REAL, mas base de
        # 3.816,69 em 12 meses (piso 12.000) nao sustenta um delta de
        # 3.796,1% na tela.
        y = kpis_yoy(self._s(148_700.45, ano="2025"),
                     self._s(3_816.69, ano="2024"))
        assert y["fat"]["delta"] is None
        assert y["fat"]["label"] == "sem base comparável em 2024"

    def test_base_material_mantem_o_delta(self):
        y = kpis_yoy(self._s(120.0 * 12 * 1000, ano="2025"),
                     self._s(100.0 * 12 * 1000, ano="2024"))
        assert y["fat"]["delta"] == pytest.approx(20.0)
        assert y["fat"]["label"] is None

    def test_base_zero_mantem_o_rotulo_antigo(self):
        # "sem dados de {ref}" continua sendo o rotulo de AUSENCIA; o novo
        # e so pra base que EXISTE e e imaterial
        y = kpis_yoy(self._s(1000.0 * 12, ano="2025"), self._s(0.0, ano="2024"))
        assert y["fat"]["delta"] is None
        assert y["fat"]["label"] == "sem dados de 2024"

    def test_pp_cascateia_quando_o_prev_e_suprimido(self):
        # razao do prev vira NaN pelo piso -> delta de p.p. nao existe
        y = kpis_yoy(self._s(5_000_000.0, ano="2025"), self._s(500.0, ano="2024"))
        assert y["folenc"]["delta"] is None
        assert y["luc"]["delta"] is None


class TestPisoSerieMensalLucratividade:
    """O MESMO piso do card agregado, agora ponto a ponto.

    Sem ele, o card diria "receita insuficiente para o indicador" e o
    grafico da MESMA pagina mostraria uma barra rotulada -16012%.

    Criterio: receita contabil DO MES < piso_denominador(1) -> ponto NaN.
    NaN e nao zero — barra zero AFIRMARIA resultado nulo, que e outra mentira.
    Perda REAL com receita material FICA: -331% e informacao, nao absurdo.
    """
    @staticmethod
    def _s(receita, lair, periods=None):
        periods = periods or [f"2025-{m:02d}" for m in range(1, len(receita) + 1)]
        n = len(periods)
        # fat NF ESPELHA a receita contabil de proposito: senao o guard de
        # cobertura (LUC_COBERTURA_RECEITA_MIN, receita < 10% do fat) dispara
        # e mascara a serie inteira — o teste passaria pelo motivo ERRADO,
        # sem exercitar o piso mensal.
        fat = pd.DataFrame({"competencia": periods,
                            "valo_saidas": list(receita),
                            "valo_servicos": [0.0] * n})
        dre = pd.DataFrame({"codi_emp": [1] * n, "competencia": periods,
                            "receita_bruta": receita,
                            "despesas": [500_000.0] * n,
                            "lair": lair,
                            "provisao_irpj_csll": [0.0] * n,
                            "resultado_liquido": lair})
        vazio = pd.DataFrame()
        return series_mensais(fat, vazio, vazio, vazio, vazio, periods,
                              dre_df=dre)

    def test_receita_irrisoria_no_mes_suprimida(self):
        # receita 299,86 com LAIR -48.014,31 -> -16.012% na tela
        s = self._s([299.86], [-48014.31])
        assert math.isnan(s["pct_luc_pm"].iloc[0])

    def test_receita_de_10_reais_suprimida(self):
        # receita de R$ 10,00 -> -297.026%
        s = self._s([10.00], [-29702.63])
        assert math.isnan(s["pct_luc_pm"].iloc[0])

    def test_perda_real_com_receita_material_FICA(self):
        # O CONTRASTE que define o criterio: -120,9% / -157,9% / -331,4% sao
        # prejuizo REAL sobre receita de milhoes. Suprimir isso esconderia a
        # informacao mais importante do periodo.
        s = self._s([1_133_389.05, 1_050_957.27, 2_212_579.96],
                    [-1_369_863.04, -1_659_225.38, -7_332_979.25])
        v = s["pct_luc_pm"].tolist()
        assert v == pytest.approx([-120.86, -157.88, -331.42], abs=0.05)
        assert not any(math.isnan(x) for x in v)

    def test_mes_isolado_material_fica(self):
        s = self._s([4_021_065.73], [2_235_495.85], periods=["2024-10"])
        assert not math.isnan(s["pct_luc_pm"].iloc[0])

    def test_mes_imaterial_no_meio_da_serie_vira_nan_nao_zero(self):
        # o mes ruim some da serie; os vizinhos saudaveis permanecem
        s = self._s([1_000_000.0, 500.0, 1_000_000.0],
                    [100_000.0, -50_000.0, 120_000.0])
        v = s["pct_luc_pm"]
        assert v.iloc[0] == pytest.approx(10.0)
        assert math.isnan(v.iloc[1])
        assert v.iloc[2] == pytest.approx(12.0)

    def test_exatamente_no_piso_fica(self):
        from etl.metrics import PISO_DENOMINADOR_MENSAL
        s = self._s([PISO_DENOMINADOR_MENSAL], [100.0])
        assert not math.isnan(s["pct_luc_pm"].iloc[0])

    def test_receita_zero_continua_nan(self):
        s = self._s([0.0], [-1000.0])
        assert math.isnan(s["pct_luc_pm"].iloc[0])


class TestCardsSaldoInvertido:
    """Cards de balanco NEGATIVOS ganham anotacao.

    O valor exibido NAO muda — nunca abs(), nunca esconder. A causa e
    provavel defeito de ORIGEM, entao esconder o numero apagaria a
    evidencia que o contador precisa levar pro cliente. A tela ANOTA.

    Limiar -0,005: o que arredonda pra zero nao e negativo.
    """
    @staticmethod
    def _s(v):
        return pd.Series([v], index=["2025-01"])

    def test_ativo_negativo_ganha_nota(self):
        r = cards_saldo_invertido({
            "Ativo Circulante": self._s(-10_840_952.47),
            "Ativo Total": self._s(-10_840_952.47),
            "Estoque": self._s(0.0),
            "Exigível LP": self._s(0.0),
            "Passivo Circulante": self._s(1_000.0)})
        assert r == ["Ativo Circulante", "Ativo Total"]

    def test_empresa_saudavel_nao_ganha_nota(self):
        # tudo positivo -> lista vazia -> consumer nao anota
        r = cards_saldo_invertido({
            "Ativo Circulante": self._s(38_070_948.23),
            "Ativo Total": self._s(57_073_699.22),
            "Estoque": self._s(0.0),
            "Exigível LP": self._s(13_484_549.90),
            "Passivo Circulante": self._s(16_890_445.80)})
        assert r == []

    def test_zero_negativo_nao_conta(self):
        # mesmo limiar: -0,004 arredonda pra R$ 0,00 e NAO e invertido
        assert cards_saldo_invertido({"Estoque": self._s(-0.004)}) == []

    def test_um_centavo_negativo_conta(self):
        assert cards_saldo_invertido({"Estoque": self._s(-0.01)}) == ["Estoque"]

    def test_serie_usa_o_ULTIMO_valor_valido(self):
        # o card exibe last_valid; a nota tem que olhar o MESMO ponto
        s = pd.Series([100.0, float("nan"), -50.0],
                      index=["2025-01", "2025-02", "2025-03"])
        assert cards_saldo_invertido({"Ativo Total": s}) == ["Ativo Total"]

    def test_serie_vazia_ou_all_nan_nao_conta(self):
        assert cards_saldo_invertido({"Ativo Total": pd.Series([], dtype=float)}) == []
        assert cards_saldo_invertido({"Ativo Total": self._s(float("nan"))}) == []

    def test_ordem_segue_a_dos_cards(self):
        # a nota lista na MESMA ordem dos cards da tela
        r = cards_saldo_invertido({
            "Passivo Circulante": self._s(-1.0), "Ativo Total": self._s(-2.0)})
        assert r == ["Passivo Circulante", "Ativo Total"]

    def test_texto_da_nota_e_contrato_de_ui(self):
        # app (caption) e PDF (context do card) exibem esta frase VERBATIM —
        # fonte unica pra os dois nao divergirem no vocabulario.
        assert NOTA_SALDO_INVERTIDO == "saldo invertido na escrituração"




class _RubBase:
    """Fixture compartilhada do mart.folha_rubricas_mensais."""
    P = ["2025-06", "2025-07"]

    @staticmethod
    def _rub(rows):
        return pd.DataFrame(rows, columns=[
            "codi_emp", "competencia", "tipo_process", "i_eventos", "nome",
            "classificacao", "desc_classificacao", "valor_p", "valor_d",
            "valor_i", "valor_id", "qtd_informada"])

    @staticmethod
    def _enc(rows):
        return pd.DataFrame(rows, columns=[
            "codi_emp", "competencia", "inss_empresa", "fgts", "fgts_resc"])

    @classmethod
    def _linha(cls, res, nome, comp):
        return float(res["tabela"].loc[nome, comp])

    @staticmethod
    def _presentes(res):
        return list(res["tabela"].index)


class TestFolhaPorLinha(_RubBase):
    """A tabela UNICA de custo de folha.

    Particao por PRECEDENCIA: a primeira regra que casa vence, entao cada linha
    do mart cai em exatamente um balde e a soma fecha por construcao.
    """

    def test_ordem_das_linhas_e_fixa(self):
        # contrato de UI: a ordem e fixa, nao e
        # alfabetica nem por valor. O 13o fecha as linhas de rubrica.
        assert LINHAS_FOLHA_RUBRICA == (
            "Folha", "Férias", "Rescisão", "Hora Extra",
            "Comissão", "DSR", "Outros", LINHA_13O)

    def test_encargos_e_a_ultima_linha_depois_do_13o(self):
        # a linha de encargos vem DEPOIS das de rubrica: e outra natureza
        # (guia recolhida), e o rotulo declara a fonte.
        r = self._rub([[201, "2025-06", 11, 1, "SALARIO BASE", 1, "Salários",
                        1000.0, 0, 0, 0, None]])
        res = folha_por_linha(r, ["2025-06"],
                              encargos_df=self._enc([[201, "2025-06", 300.0, 80.0, 0.0]]))
        assert self._presentes(res)[-1] == LINHA_ENCARGOS
        assert LINHA_ENCARGOS == "Encargos (INSS + FGTS, guia)"

    def test_dsr_tem_precedencia_sobre_comissao(self):
        # O CASO QUE DEFINE A PRECEDENCIA: o REFLEXO COMISSOES DSR vem
        # classificado como "Comissão" pela origem. Sem a precedencia ele seria contado na linha Comissao E na DSR.
        r = self._rub([
            [215, "2025-06", 11, 37, "COMISSOES", 23, "Comissão", 1000.0, 0, 0, 0, None],
            [215, "2025-06", 11, 853, "REFLEXO COMISSOES DSR", 23, "Comissão",
             200.0, 0, 0, 0, None],
        ])
        res = folha_por_linha(r, ["2025-06"])
        assert self._linha(res, "Comissão", "2025-06") == 1000.0
        assert self._linha(res, "DSR", "2025-06") == 200.0

    def test_dsr_tambem_sai_de_outros_proventos(self):
        # REFLEXO EXTRAS DSR (evt 250) vem de "Outros Proventos" — a linha
        # DSR junta as duas origens.
        r = self._rub([
            [201, "2025-06", 11, 250, "REFLEXO EXTRAS DSR", 2, "Outros Proventos",
             300.0, 0, 0, 0, None],
        ])
        res = folha_por_linha(r, ["2025-06"])
        assert self._linha(res, "DSR", "2025-06") == 300.0
        assert "Outros" not in self._presentes(res)     # nada sobrou pra Outros

    def test_regex_do_dsr_nao_pega_falso_positivo(self):
        # "FALTAS DSR" e "ESTORNO DIAS FALTAS DSR" existem e NAO sao reflexo.
        r = self._rub([
            [201, "2025-06", 11, 999, "ESTORNO DIAS FALTAS DSR", 2,
             "Outros Proventos", 175.89, 0, 0, 0, None],
        ])
        res = folha_por_linha(r, ["2025-06"])
        assert "DSR" not in self._presentes(res)
        assert self._linha(res, "Outros", "2025-06") == 175.89

    def test_13o_e_so_tp52(self):
        # a familia "13º Salário" aparece em varios tps; so o tp52 traz o
        # INTEGRAL. Somar tp51+tp52 contaria quase 2x.
        r = self._rub([
            [201, "2025-12", 52, 12, "13 SALARIO INTEGRAL", 4, "13º Salário",
             443976.17, 280041.87, 0, 0, None],
            [201, "2025-12", 11, 800, "MEDIA HORAS 13o", 4, "13º Salário",
             5000.0, 0, 0, 0, None],
        ])
        res = folha_por_linha(r, ["2025-12"])
        assert self._linha(res, LINHA_13O, "2025-12") == 443976.17
        # o 13o pago DENTRO do mensal pousa em Outros — e a nota declara isso
        assert self._linha(res, "Outros", "2025-12") == 5000.0

    def test_o_13o_entra_no_custo_total(self):
        # nao ha "(-) 13o" nem segundo total. O mes
        # que pagou 13o custa mais, e o CUSTO TOTAL diz isso.
        r = self._rub([
            [201, "2025-12", 11, 1, "SALARIO BASE", 1, "Salários", 1000.0, 0, 0, 0, None],
            [201, "2025-12", 52, 12, "13 SALARIO INTEGRAL", 4, "13º Salário",
             400.0, 0, 0, 0, None],
        ])
        res = folha_por_linha(r, ["2025-12"])
        assert float(res["custo"]["2025-12"]) == 1400.0
        assert float(res["custo_rubricas"]["2025-12"]) == 1400.0
        assert float(res["decimo_terceiro"]["2025-12"]) == 400.0

    def test_antecipacoes_ficam_fora_das_linhas(self):
        # tp 41/51/92 sao antecipacao/duplicata: NUNCA linha-irma somavel.
        r = self._rub([
            [201, "2025-06", 11, 1, "SALARIO BASE", 1, "Salários", 1000.0, 0, 0, 0, None],
            [201, "2025-06", 41, 980, "ADIANTAMENTO SALARIAL", 2,
             "Outros Proventos", 300.0, 0, 0, 0, None],
            [201, "2025-06", 92, 9452, "SERVICOS FRETE AUTONOMO", 61,
             "Serviços Frete", 50.0, 0, 0, 0, None],
        ])
        res = folha_por_linha(r, ["2025-06"])
        assert float(res["custo"]["2025-06"]) == 1000.0
        assert float(res["antecipacoes"]["2025-06"]) == 350.0
        assert "Outros" not in self._presentes(res)

    def test_soma_das_linhas_e_o_custo_total(self):
        r = self._rub([
            [201, "2025-06", 11, 1, "SALARIO BASE", 1, "Salários", 1000.0, 0, 0, 0, None],
            # a ferias que CONTA e a do mensal (competencia do gozo); o
            # recibo tp60 vira nota, junto com os outros tps de antecipacao
            [201, "2025-06", 11, 3, "FERIAS", 5, "Férias Gozadas", 200.0, 0, 0, 0, None],
            [201, "2025-06", 11, 202, "HORA EXTRA A 60%", 3, "Horas Extras",
             100.0, 0, 0, 0, 40.0],
            [201, "2025-06", 11, 700, "AVISO PREVIO", 9, "Aviso Prévio", 50.0, 0, 0, 0, None],
            [201, "2025-06", 11, 4242, "VALE ALGUMA COISA", 2, "Outros Proventos",
             7.0, 0, 0, 0, None],
        ])
        res = folha_por_linha(r, ["2025-06"],
                              encargos_df=self._enc([[201, "2025-06", 300.0, 80.0, 20.0]]))
        # a soma da COLUNA INTEIRA (rubricas + encargos) e o CUSTO TOTAL
        assert float(res["tabela"]["2025-06"].sum()) == pytest.approx(1757.0)
        assert float(res["custo"]["2025-06"]) == pytest.approx(1757.0)
        assert float(res["custo_rubricas"]["2025-06"]) == pytest.approx(1357.0)

    def test_qtd_de_hora_extra_vem_separada(self):
        r = self._rub([
            [201, "2025-06", 11, 202, "HORA EXTRA A 60%", 3, "Horas Extras",
             27846.16, 0, 0, 0, 1340.0],
            [201, "2025-06", 11, 214, "HORAS EXTRAS 120%", 3, "Horas Extras",
             3002.75, 0, 0, 0, 115.0],
        ])
        res = folha_por_linha(r, ["2025-06"])
        assert float(res["qtd_he"]["2025-06"]) == 1455.0
        assert self._linha(res, "Hora Extra", "2025-06") == pytest.approx(30848.91)

    def test_mes_sem_dado_nao_vira_coluna_de_zero(self):
        # regra herdada: mes sem dado = coluna AUSENTE, nao zero.
        r = self._rub([
            [201, "2025-06", 11, 1, "SALARIO BASE", 1, "Salários", 1000.0, 0, 0, 0, None],
        ])
        res = folha_por_linha(r, self.P)
        assert list(res["tabela"].columns) == ["2025-06"]

    def test_so_valor_p_entra(self):
        # a visao e de CUSTO BRUTO: desconto e informativo (FGTS por rubrica)
        # nao somam — o FGTS entra UMA vez, pela guia.
        r = self._rub([
            [201, "2025-06", 11, 1, "SALARIO BASE", 1, "Salários", 1000.0, 0, 0, 0, None],
            [201, "2025-06", 11, 998, "I.N.S.S.", 12, "INSS", 0.0, 90.0, 0, 0, None],
            [201, "2025-06", 11, 996, "F.G.T.S  DO MES", 14, "FGTS", 0.0, 0, 80.0, 0, None],
        ])
        res = folha_por_linha(r, ["2025-06"])
        assert float(res["custo"]["2025-06"]) == 1000.0

    def test_frame_vazio_nao_crasha(self):
        res = folha_por_linha(pd.DataFrame(), self.P)
        assert res["tabela"].empty
        assert float(res["custo"].sum()) == 0.0

    def test_sem_rubricas_a_tabela_nao_vira_so_encargos(self):
        # empresa fora do mart de rubricas degrada pro aviso do consumer — uma
        # tabela so de encargos nao e a visao de folha.
        res = folha_por_linha(pd.DataFrame(), self.P,
                              encargos_df=self._enc([[201, "2025-06", 300.0, 80.0, 0.0]]))
        assert res["tabela"].empty

    def test_nota_do_13o_e_contrato_de_ui(self):
        # app (caption) e PDF exibem VERBATIM — fonte unica. A parcela
        # aparece no mes do pagamento.
        assert NOTA_LINHA_13O == ("cada parcela no mês do pagamento; "
                                  "reflexos de 13º pagos dentro do mensal "
                                  "constam em Outros")

    def test_nota_do_13o_nao_vaza_codigo_de_sistema(self):
        # "tp52" nao aparece no texto exibido. O leitor do relatorio nao
        # tem contexto pro codigo — mesma regua do rotulo_tipo_process.
        assert "tp" not in NOTA_LINHA_13O.lower()
        assert "evt" not in NOTA_LINHA_13O.lower()

    def test_rotulos_de_total_e_pct_sao_contrato_de_ui(self):
        # os dois consumers escrevem o MESMO rotulo — e o rotulo do % NAO tem
        # qualificador de exclusao, porque o criterio nao exclui nada.
        # "Folha + Encargos": o card do hero, o titulo do chart, o titulo do
        # bloco e a linha da tabela dizem a MESMA coisa. Dois nomes pro mesmo
        # universo na mesma pagina seriam defeito.
        assert LINHA_FOLHA_ENCARGOS == "Folha + Encargos"
        assert LINHA_PCT_FAT == "% sobre Faturamento"

    def test_rescisao_por_NOME_resgata_o_que_a_familia_errou(self):
        # a origem classifica verba rescisoria em "13º Salário" e
        # "Fechamento". Sem o criterio de nome elas cairiam em Outros.
        r = self._rub([
            [201, "2025-06", 11, 8300, "13 SALARIO INTEGRAL RESCISAO", 4,
             "13º Salário", 58495.52, 0, 0, 0, None],
            [201, "2025-06", 11, 8301, "ESTOURO RESCISAO", 15, "Fechamento",
             2684.77, 0, 0, 0, None],
        ])
        res = folha_por_linha(r, ["2025-06"])
        assert self._linha(res, "Rescisão", "2025-06") == pytest.approx(61180.29)
        assert "Outros" not in self._presentes(res)
        assert "13º Salário" not in self._presentes(res)

    def test_multa_de_estabilidade_FICA_em_outros_por_decisao(self):
        # LIMITE DECLARADO: a multa do Art. 479/CLT e conceitualmente
        # rescisoria mas o nome nao diz RESCISAO.
        # Alargar o regex pra multa/indenizacao traria falso-positivo — a
        # subestimacao e DECLARADA, nao acidental.
        r = self._rub([[201, "2025-06", 11, 8302,
                        "MULTA ESTABILIDADE Art. 479/CLT", 2,
                        "Outros Proventos", 8501.52, 0, 0, 0, None]])
        res = folha_por_linha(r, ["2025-06"])
        assert self._linha(res, "Outros", "2025-06") == pytest.approx(8501.52)
        assert "Rescisão" not in self._presentes(res)

    def test_rescisao_casa_com_e_sem_til(self):
        for nome in ("FERIAS RESCISAO CCT", "ABONO FERIAS RESCISÃO CCT"):
            r = self._rub([[201, "2025-06", 11, 1, nome, 2, "Outros Proventos",
                            10.0, 0, 0, 0, None]])
            res = folha_por_linha(r, ["2025-06"])
            assert self._linha(res, "Rescisão", "2025-06") == 10.0, nome

    def test_tp52_tem_precedencia_sobre_o_nome_rescisao(self):
        # O tp52 e o degrau que reconcilia com a folha do mart — nada pode
        # vazar dele.
        r = self._rub([
            [201, "2025-12", 52, 12, "13 SALARIO INTEGRAL RESCISAO", 4,
             "13º Salário", 400.0, 0, 0, 0, None],
        ])
        res = folha_por_linha(r, ["2025-12"])
        # a rubrica traz "13º Salário" como FAMILIA da origem e o nome casa o
        # regex de RESCISAO; o tp52 vence os dois e manda pra LINHA_13O.
        assert self._linha(res, LINHA_13O, "2025-12") == 400.0
        assert "Rescisão" not in self._presentes(res)

    def test_familia_rescisoria_continua_valendo_sem_o_nome(self):
        # o criterio de familia NAO morreu — "AVISO PREVIO" nao tem RESCISAO
        # no nome e continua entrando pela familia 9.
        r = self._rub([[201, "2025-06", 11, 700, "AVISO PREVIO INDENIZADO", 9,
                        "Aviso Prévio", 50.0, 0, 0, 0, None]])
        res = folha_por_linha(r, ["2025-06"])
        assert self._linha(res, "Rescisão", "2025-06") == 50.0


class TestLinhasOpcionaisSoComMovimento(_RubBase):
    """Linha opcional zerada na janela inteira NAO entra.

    Empresa que nunca pagou comissao nao carrega uma linha de zeros; dentro da
    janela, mes zerado de uma linha PRESENTE mostra R$ 0,00 normalmente — ali o
    zero e informacao. O criterio mora no metrics pro PDF herdar identico.
    """
    def _folha_mais(self, extra=()):
        base = [[201, "2025-06", 11, 1, "SALARIO BASE", 1, "Salários",
                 1000.0, 0, 0, 0, None],
                [201, "2025-07", 11, 1, "SALARIO BASE", 1, "Salários",
                 1100.0, 0, 0, 0, None]]
        return self._rub(base + list(extra))

    def test_sem_comissao_a_linha_some(self):
        res = folha_por_linha(self._folha_mais(), self.P)
        assert "Comissão" not in self._presentes(res)
        assert self._presentes(res) == ["Folha", LINHA_ENCARGOS]

    def test_com_comissao_a_linha_aparece(self):
        res = folha_por_linha(self._folha_mais([
            [201, "2025-06", 11, 37, "COMISSOES", 23, "Comissão",
             500.0, 0, 0, 0, None]]), self.P)
        assert "Comissão" in self._presentes(res)

    def test_movimento_em_um_mes_mantem_zero_nos_outros(self):
        # o zero DENTRO de uma linha presente e informacao ("nao pagou
        # comissao em julho") — nao vira ausencia.
        res = folha_por_linha(self._folha_mais([
            [201, "2025-06", 11, 37, "COMISSOES", 23, "Comissão",
             500.0, 0, 0, 0, None]]), self.P)
        assert self._linha(res, "Comissão", "2025-06") == 500.0
        assert self._linha(res, "Comissão", "2025-07") == 0.0

    def test_janela_que_exclui_o_unico_mes_com_movimento_derruba_a_linha(self):
        # o criterio e sobre a janela EXIBIDA, nao sobre a historia da empresa.
        r = self._folha_mais([[201, "2025-06", 11, 37, "COMISSOES", 23,
                               "Comissão", 500.0, 0, 0, 0, None]])
        res = folha_por_linha(r, ["2025-07"])
        assert "Comissão" not in self._presentes(res)
        assert self._linha(res, "Folha", "2025-07") == 1100.0

    def test_folha_e_encargos_sao_estruturais_e_ficam_mesmo_zerados(self):
        # sem eles a tabela perderia o esqueleto: "quanto custou a folha" tem
        # que ter resposta em todo mes exibido.
        r = self._rub([[201, "2025-06", 11, 1, "SALARIO BASE", 1, "Salários",
                        0.0, 0, 0, 0, None]])
        res = folha_por_linha(r, ["2025-06"])
        assert self._presentes(res) == ["Folha", LINHA_ENCARGOS]
        assert LINHAS_FOLHA_SEMPRE == frozenset({"Folha"})

    def test_valor_imaterial_nao_cria_linha(self):
        # o limiar e o MESMO do fmt_brl: o que arredonda pra R$ 0,00 na
        # exibicao nao e movimento — a linha mostraria zero em todo mes.
        res = folha_por_linha(self._folha_mais([
            [201, "2025-06", 11, 37, "COMISSOES", 23, "Comissão",
             0.004, 0, 0, 0, None]]), self.P)
        assert "Comissão" not in self._presentes(res)

    def test_o_corte_nao_muda_o_custo_total(self):
        # invariante: o que sai e zero em todos os meses, entao a soma fecha
        # igual com ou sem as linhas cortadas.
        res = folha_por_linha(self._folha_mais(), self.P)
        assert float(res["custo"]["2025-06"]) == 1000.0
        assert float(res["tabela"]["2025-06"].sum()) == 1000.0


class TestDestaquesDaFolha(_RubBase):
    """A lista FIXA de principais custos.

    O detalhamento passou a ser CONTIDO no total, nao aditivo: em cima fica a
    linha unica "Folha + Encargos" + o percentual, e embaixo estas cinco
    rubricas, que ja estao DENTRO dela. Por isso nao existe — e nao pode
    existir — verificacao de "soma dos destaques == total".
    """

    def test_a_lista_e_fixa_e_ordenada(self):
        # ordem fixa, nao alfabetica nem por valor.
        assert RUBRICAS_DESTAQUE_FOLHA == (
            "Férias", "Rescisão", "Hora Extra", "Comissão", "DSR")

    def test_todo_destaque_e_uma_linha_QUE_O_PIPELINE_PRODUZ(self):
        """O que prova "match pela CLASSIFICACAO, nunca i_eventos cru".

        Os cinco nomes sao saidas de `_linha_da_rubrica`, que decide por
        `desc_classificacao` (+ os regexes de nome de RESCISAO/DSR, onde a
        familia erra). Se alguem inventar um nome aqui — ou renomear uma linha
        no pipeline sem renomear aqui — o destaque vira uma TAXONOMIA PARALELA
        e a linha some da tela em silencio. Este teste e o que impede.
        """
        assert set(RUBRICAS_DESTAQUE_FOLHA) <= set(LINHAS_FOLHA_RUBRICA)

    def test_o_total_e_o_agregado_NAO_entram_no_detalhe(self):
        # "Folha" e o grosso do total, "Outros" e a caixa-preta, o 13o e os encargos ja estao no total. Nenhum e "principal
        # custo" — sao o proprio total, decomposto.
        for fora in ("Folha", "Outros", LINHA_13O, LINHA_ENCARGOS):
            assert fora not in RUBRICAS_DESTAQUE_FOLHA

    def test_devolve_as_linhas_na_ordem_da_constante(self):
        # a ordem e a da constante, nao a que a tabela por acaso tiver.
        r = self._rub([
            [201, "2025-06", 11, 37, "COMISSOES", 23, "Comissão", 300.0, 0, 0, 0, None],
            [201, "2025-06", 11, 202, "HORA EXTRA A 60%", 3, "Horas Extras",
             200.0, 0, 0, 0, 10.0],
            [201, "2025-06", 11, 5, "FERIAS GOZADAS", 6, "Férias Gozadas",
             100.0, 0, 0, 0, None],
        ])
        res = folha_por_linha(r, ["2025-06"])
        assert list(folha_destaques(res["tabela"]).index) == [
            "Férias", "Hora Extra", "Comissão"]

    def test_rubrica_sem_valor_no_periodo_NAO_aparece(self):
        # some a linha inteira, nao vira linha de zeros: empresa que nunca
        # pagou comissao nao carrega uma linha vazia na tela.
        r = self._rub([[201, "2025-06", 11, 5, "FERIAS GOZADAS", 6,
                        "Férias Gozadas", 100.0, 0, 0, 0, None]])
        res = folha_por_linha(r, ["2025-06"])
        d = folha_destaques(res["tabela"])
        assert list(d.index) == ["Férias"]
        assert "Comissão" not in d.index

    def test_mes_zerado_de_linha_PRESENTE_continua_exibido(self):
        # dentro da janela, o zero de uma linha presente e INFORMACAO ("nao
        # pagou comissao em julho"). O corte e da linha inteira, nunca da
        # celula.
        r = self._rub([
            [201, "2025-06", 11, 37, "COMISSOES", 23, "Comissão", 300.0, 0, 0, 0, None],
            [201, "2025-07", 11, 1, "SALARIO BASE", 1, "Salários", 900.0, 0, 0, 0, None],
        ])
        res = folha_por_linha(r, self.P)
        d = folha_destaques(res["tabela"])
        assert float(d.loc["Comissão", "2025-07"]) == 0.0

    def test_o_detalhe_e_CONTIDO_no_total(self):
        # a soma dos destaques e MENOR que o CUSTO TOTAL — e isso e o
        # contrato, nao um defeito. Quem declara a contencao e o subtitulo.
        r = self._rub([
            [201, "2025-06", 11, 1, "SALARIO BASE", 1, "Salários", 1000.0, 0, 0, 0, None],
            [201, "2025-06", 11, 37, "COMISSOES", 23, "Comissão", 300.0, 0, 0, 0, None],
        ])
        res = folha_por_linha(r, ["2025-06"])
        d = folha_destaques(res["tabela"])
        assert float(d["2025-06"].sum()) == 300.0
        assert float(res["custo"]["2025-06"]) == 1300.0

    def test_tabela_vazia_devolve_vazio_sem_estourar(self):
        vazio = folha_por_linha(pd.DataFrame(), ["2025-06"])
        assert folha_destaques(vazio["tabela"]).empty
        assert folha_destaques(None).empty

    def test_o_subtitulo_DECLARA_a_contencao(self):
        # sem esta frase o leitor soma o detalhe com o total e conta 2x. E a
        # unica protecao que existe (nao ha verificacao aritmetica possivel aqui).
        assert TITULO_DESTAQUES_FOLHA == "Principais custos da folha"
        assert SUBTITULO_DESTAQUES_FOLHA == (
            "valores já incluídos em Folha + Encargos")


class TestEncargosNaTabelaDeFolha(_RubBase):
    """Encargos entram como LINHA da tabela e no CUSTO TOTAL.

    Fonte e natureza diferentes das rubricas (guia recolhida) — dai o
    rotulo declarar "(guia)". Mas o custo e UM so, entao a linha soma.
    """
    def _r(self):
        return self._rub([[201, "2025-06", 11, 1, "SALARIO BASE", 1, "Salários",
                           1000.0, 0, 0, 0, None]])

    def test_linha_soma_inss_fgts_e_grfc(self):
        res = folha_por_linha(self._r(), ["2025-06"],
                              encargos_df=self._enc([[201, "2025-06", 300.0, 80.0, 20.0]]))
        assert self._linha(res, LINHA_ENCARGOS, "2025-06") == pytest.approx(400.0)
        assert float(res["custo"]["2025-06"]) == pytest.approx(1400.0)

    def test_mes_sem_guia_exibe_zero_e_nao_nan(self):
        # Simples aparece com INSS patronal R$ 0,00 (o INSS esta no DAS) — e
        # dado, nao defeito. NaN na tabela viraria "—" e o leitor somaria errado.
        res = folha_por_linha(self._r(), ["2025-06"], encargos_df=pd.DataFrame())
        assert self._linha(res, LINHA_ENCARGOS, "2025-06") == 0.0
        assert float(res["custo"]["2025-06"]) == 1000.0

    def test_encargos_por_mes_e_null_aware_pro_card(self):
        # o card do hero precisa distinguir "sem guia" de "guia zerada" — a
        # serie crua preserva NaN; a TABELA e que escolhe exibir 0,00.
        s = encargos_por_mes(pd.DataFrame(), ["2025-06"])
        assert pd.isna(s["2025-06"])
        s2 = encargos_por_mes(self._enc([[201, "2025-06", 300.0, None, None]]),
                              ["2025-06"])
        assert float(s2["2025-06"]) == 300.0


class TestPercentualSobreFaturamento(_RubBase):
    """UM percentual: CUSTO TOTAL / faturamento do mes, sem qualificador."""
    def _r(self, v=1000.0):
        return self._rub([[201, "2025-06", 11, 1, "SALARIO BASE", 1, "Salários",
                           v, 0, 0, 0, None]])

    def test_divide_o_custo_total_pelo_faturamento(self):
        res = folha_por_linha(self._r(), ["2025-06"],
                              encargos_df=self._enc([[201, "2025-06", 400.0, 0.0, 0.0]]),
                              fat_pm=pd.Series({"2025-06": 10000.0}))
        assert float(res["pct_fat"]["2025-06"]) == pytest.approx(14.0)

    def test_sem_faturamento_o_percentual_e_nan(self):
        res = folha_por_linha(self._r(), ["2025-06"], fat_pm=None)
        assert pd.isna(res["pct_fat"]["2025-06"])

    def test_faturamento_imaterial_suprime_o_percentual(self):
        # piso ponto a ponto: base POSITIVA porem imaterial nao sustenta
        # razao. Sem isso a linha gritaria 20.000% onde o card exibe "—".
        res = folha_por_linha(self._r(), ["2025-06"],
                              fat_pm=pd.Series({"2025-06": 5.0}))
        assert pd.isna(res["pct_fat"]["2025-06"])


class TestOutrosDetalhe(_RubBase):
    """A caixa-preta do "Outros" tem que abrir — senao e um numero sem nome."""
    def test_lista_as_rubricas_por_valor(self):
        r = self._rub([
            [201, "2025-06", 11, 1, "PRO-LABORE", 2, "Outros Proventos",
             9000.0, 0, 0, 0, None],
            [201, "2025-06", 11, 2, "AUXILIO TRANSPORTE", 2, "Outros Proventos",
             300.0, 0, 0, 0, None],
            [201, "2025-07", 11, 1, "PRO-LABORE", 2, "Outros Proventos",
             1000.0, 0, 0, 0, None],
        ])
        res = folha_por_linha(r, self.P)
        assert list(res["outros_top"].index) == ["PRO-LABORE", "AUXILIO TRANSPORTE"]
        assert float(res["outros_top"]["PRO-LABORE"]) == 10000.0   # agrega a janela

    def test_top_n_com_resto_agregado(self):
        # o corte vem da CONSTANTE (OUTROS_TOP_N): os dois consumers exibem a
        # mesma lista, e corte diferente de cada lado seria dois "Outros" com
        # o mesmo nome.
        n = OUTROS_TOP_N
        rows = [[201, "2025-06", 11, i, f"RUBRICA {i:02d}", 2, "Outros Proventos",
                 float(100 - i), 0, 0, 0, None] for i in range(n + 2)]
        res = folha_por_linha(self._rub(rows), ["2025-06"])
        assert len(res["outros_top"]) == n
        # os 2 que sobraram sao os DOIS menores da lista ordenada desc
        assert res["outros_resto"] == pytest.approx((100 - n) + (100 - n - 1))

    def test_sem_outros_a_lista_e_vazia(self):
        r = self._rub([[201, "2025-06", 11, 1, "SALARIO BASE", 1, "Salários",
                        1000.0, 0, 0, 0, None]])
        res = folha_por_linha(r, ["2025-06"])
        assert len(res["outros_top"]) == 0
        assert res["outros_resto"] == 0.0


class TestAbatimentoDeEspelhos(_RubBase):
    """Desconto que espelha provento nao desembolsado ABATE o custo.
    Abate-se o DESCONTO, nunca se exclui o provento."""
    PER = ["2025-03"]

    def _cenario(self):
        # espelho EXATO de afastamento (evt 5 == evt 988 ao centavo) + os
        # 15 primeiros dias, que FICAM,
        # + falta na familia e o vazamento do DSR na familia generica
        return self._rub([
            [201, "2025-03", 11, 1, "SALARIO BASE", 1, "Salários", 10000.0, 0, 0, 0, None],
            [201, "2025-03", 11, 5, "DIAS AFAST.INSS (P/DOENC", 1, "Salários",
             378.23, 0, 0, 0, None],
            [201, "2025-03", 11, 988, "DESCONTO DIAS AFASTADAS", 11, "Descontos",
             0, 378.23, 0, 0, None],
            [201, "2025-03", 11, 9524, "DIAS AFAST. P/DOENCA IGUAL/INF 15 DIAS", 1,
             "Salários", 810.50, 0, 0, 0, None],
            [201, "2025-03", 11, 40, "FALTAS", 26, "Falta Integral", 0, 100.0, 0, 0, None],
            [201, "2025-03", 11, 42, "FALTAS DSR", 11, "Descontos", 0, 25.0, 0, 0, None],
            [201, "2025-03", 11, 998, "I.N.S.S.", 12, "INSS", 0, 900.0, 0, 0, None],
        ])

    def test_afastamento_do_inss_sai_e_os_15_primeiros_dias_ficam(self):
        r = folha_por_linha(self._cenario(), self.PER)
        ab = r["abatimentos"]
        assert float(ab["afastamento"]["2025-03"]) == pytest.approx(378.23)
        # o 9524 nao tem espelho: os 15 primeiros dias sao da empresa (Lei
        # 8.213 art.60 par.3) e continuam no custo
        assert float(r["tabela"].loc["Folha", "2025-03"]) == pytest.approx(
            10000.0 + 378.23 + 810.50 - 378.23 - 125.0)

    def test_falta_soma_familia_E_o_vazamento_do_dsr(self):
        """A familia sozinha pega so metade: parte do ajuste mora na familia
        generica "Descontos", sobretudo FALTAS DSR."""
        r = folha_por_linha(self._cenario(), self.PER)
        assert float(r["abatimentos"]["falta"]["2025-03"]) == pytest.approx(125.0)

    def test_retencao_do_empregado_NAO_abate(self):
        """INSS/IRRF do empregado saem do bruto que a empresa JA pagou —
        subtrair contaria a MENOS. So espelho abate."""
        r = folha_por_linha(self._cenario(), self.PER)
        assert float(r["abatimentos"]["total"]["2025-03"]) == pytest.approx(503.23)
        assert 900.0 not in list(r["abatimentos"]["total"])

    def test_rubrica_ACENTUADA_e_alcancada(self):
        """O criterio normaliza antes de casar. "SUSPENSÃO" acentuada
        (familia Falta Integral) seria perdida calada por um regex ASCII."""
        r = folha_por_linha(self._rub([
            [201, "2025-03", 11, 1, "SALARIO BASE", 1, "Salários", 5000.0, 0, 0, 0, None],
            [201, "2025-03", 11, 238, "SUSPENSÃO", 26, "Falta Integral",
             0, 300.0, 0, 0, None]]), self.PER)
        assert float(r["abatimentos"]["falta"]["2025-03"]) == pytest.approx(300.0)

    def test_o_card_abate_a_MESMA_serie_que_a_tabela(self):
        """O contrato: card e tabela subtraem a mesma coisa, senao o
        '% Folha + Encargos' e o '% sobre Faturamento' divergem na tela."""
        rub = self._cenario()
        s = series_mensais(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
                           pd.DataFrame(), pd.DataFrame(), self.PER, rub_df=rub)
        r = folha_por_linha(rub, self.PER)
        assert s["folha_pm"]["2025-03"] == pytest.approx(float(r["custo_rubricas"]["2025-03"]))

    def test_a_soma_da_coluna_continua_sendo_o_custo_total(self):
        """Invariante: o abatimento pousa DENTRO de uma linha, entao a
        aritmetica da tabela nao muda de forma."""
        r = folha_por_linha(self._cenario(), self.PER,
                            encargos_df=self._enc([[201, "2025-03", 300.0, 80.0, 0.0]]))
        assert float(r["tabela"]["2025-03"].sum()) == pytest.approx(float(r["custo"]["2025-03"]))


class TestSalarioFamiliaCompensado(_RubBase):
    """A guia vem BRUTA. O provento de salario familia FICA no
    custo (a empresa desembolsou) e o que abate e a compensacao RECEBIDA."""
    PER = ["2025-03"]

    def _rub_sf(self):
        return self._rub([
            [201, "2025-03", 11, 1, "SALARIO BASE", 1, "Salários", 5000.0, 0, 0, 0, None],
            [201, "2025-03", 11, 995, "SALARIO FAMÍLIA", 2, "Outros Proventos",
             135.08, 0, 0, 0, None]])

    def _enc_sf(self, sf):
        e = self._enc([[201, "2025-03", 1000.0, 400.0, 0.0]])
        e["sf_compensado"] = [sf]
        return e

    def test_o_provento_fica_e_a_compensacao_abate_o_total(self):
        r = folha_por_linha(self._rub_sf(), self.PER, encargos_df=self._enc_sf(135.08))
        # o abatimento pousa na linha do provento que ele espelha: "Outros", que
        # e onde a origem classifica o salario familia. A soma da coluna
        # continua sendo o CUSTO TOTAL.
        assert float(r["tabela"].loc["Outros", "2025-03"]) == pytest.approx(0.0)
        # a linha de Encargos segue FIEL A GUIA, bruta
        assert float(r["tabela"].loc[LINHA_ENCARGOS, "2025-03"]) == pytest.approx(1400.0)
        # e o CUSTO TOTAL e o desembolso: 5135,08 + 1400 - 135,08
        assert float(r["custo"]["2025-03"]) == pytest.approx(6400.0)

    def test_nao_abate_dentro_do_inss_patronal(self):
        """O patronal menos o SF ficaria NEGATIVO com frequencia, porque em
        empresa do Simples o patronal e 0,00 e a compensacao sai dos
        segurados."""
        e = self._enc([[201, "2025-03", 0.0, 400.0, 0.0]])   # Simples
        e["sf_compensado"] = [300.0]
        r = folha_por_linha(self._rub_sf(), self.PER, encargos_df=e)
        assert float(r["tabela"].loc[LINHA_ENCARGOS, "2025-03"]) == pytest.approx(400.0)
        assert float(r["custo"]["2025-03"]) == pytest.approx(5135.08 + 400.0 - 300.0)

    def test_mart_sem_a_coluna_nao_abate_nada(self):
        r = folha_por_linha(self._rub_sf(), self.PER,
                            encargos_df=self._enc([[201, "2025-03", 1000.0, 400.0, 0.0]]))
        assert float(r["custo"]["2025-03"]) == pytest.approx(5135.08 + 1400.0)

    def test_o_card_desconta_o_mesmo_sf_que_a_tabela(self):
        fat = pd.DataFrame({"competencia": self.PER, "valo_saidas": [10000.0],
                            "valo_servicos": [0.0]})
        enc = self._enc_sf(135.08)
        s = series_mensais(fat, pd.DataFrame(), pd.DataFrame(), pd.DataFrame(),
                           pd.DataFrame(), self.PER, encargos_df=enc,
                           rub_df=self._rub_sf())
        k = kpis_agregados(s)
        r = folha_por_linha(self._rub_sf(), self.PER, encargos_df=enc,
                            fat_pm=s["fat_pm"])
        assert k["pct_folenc_med"] == pytest.approx(
            float(r["custo"].sum()) / float(s["fat_pm"].sum()) * 100)


class TestLegendaAbatimentos:
    """A ponte pra conciliar: o numero cai E a tela diz por que."""

    def test_nomeia_cada_ajuste_com_valor(self):
        ab = {"afastamento": pd.Series([378.23]), "falta": pd.Series([125.0]),
              "salario_familia": pd.Series([135.08])}
        txt = legenda_abatimentos(ab)
        assert "afastamento pago pelo INSS" in txt and "378,23" in txt
        assert "faltas" in txt and "125,00" in txt
        assert "salário família compensado na guia" in txt and "135,08" in txt

    def test_so_entra_o_que_teve_movimento(self):
        ab = {"afastamento": pd.Series([378.23]), "falta": pd.Series([0.0]),
              "salario_familia": pd.Series([0.0])}
        txt = legenda_abatimentos(ab)
        assert "afastamento" in txt
        assert "faltas" not in txt and "família" not in txt

    def test_sem_abatimento_nao_renderiza_nada(self):
        assert legenda_abatimentos(None) == ""
        assert legenda_abatimentos({"afastamento": pd.Series([0.0])}) == ""


class TestCoerenciaCardTabelaFolha(_RubBase):
    """CONTRATO: o card "% Folha + Encargos" e a linha
    "% sobre Faturamento" da tabela sao O MESMO NUMERO na mesma janela.

    A licao do ISS: nunca dois universos com o mesmo nome na mesma tela. O
    card le mart.folha_mensal e a tabela le mart.folha_rubricas_mensais — as
    duas fontes reconciliam (SUM(valor_p) == valo_bruto), entao o criterio TEM que ser o mesmo dos dois lados.
    """
    PER = ["2025-11", "2025-12"]

    def _cenario(self):
        # inclui um mes COM 13o (o caso que separava os dois numeros antes)
        folha = pd.DataFrame({
            "competencia": ["2025-11", "2025-11", "2025-12", "2025-12", "2025-12"],
            "tipo_process": [11, 51, 11, 52, 41],
            "valo_bruto": [1000.0, 400.0, 1200.0, 800.0, 300.0]})
        rub = self._rub([
            [201, "2025-11", 11, 1, "SALARIO BASE", 1, "Salários", 1000.0, 0, 0, 0, None],
            [201, "2025-11", 51, 50, "ADIANT 13o", 4, "13º Salário", 400.0, 0, 0, 0, None],
            [201, "2025-12", 11, 1, "SALARIO BASE", 1, "Salários", 1200.0, 0, 0, 0, None],
            [201, "2025-12", 52, 12, "13 SALARIO INTEGRAL", 4, "13º Salário",
             800.0, 0, 0, 0, None],
            [201, "2025-12", 41, 980, "ADIANTAMENTO", 2, "Outros Proventos",
             300.0, 0, 0, 0, None],
        ])
        enc = self._enc([[201, "2025-11", 200.0, 80.0, 0.0],
                         [201, "2025-12", 250.0, 90.0, 10.0]])
        fat = pd.DataFrame({"competencia": self.PER,
                            "valo_saidas": [20000.0, 30000.0],
                            "valo_servicos": [0.0, 0.0]})
        return folha, rub, enc, fat

    def test_com_rub_df_a_igualdade_vira_ESTRUTURAL(self):
        """O card le a MESMA base da tabela.

        Sem isso as duas fontes batem so por COINCIDENCIA: `mart.folha_mensal`
        (FOBASES) x `mart.folha_rubricas_mensais` (FOMOVTO) divergem na
        familia "Simples domestico", que a FOMOVTO lanca como provento e a
        FOBASES nao poe em `proventos`.

        A fixture reproduz a divergencia de proposito: o `folha_mensal` diz
        1000,00 em nov e o `rub` diz 1150,00. Com `rub_df`, o card segue o
        rub — e a tabela tambem, entao os dois sao o mesmo numero SEMPRE.
        """
        folha, rub, enc, fat = self._cenario()
        folha = folha.copy()
        rub = pd.concat([rub, self._rub([
            [201, "2025-11", 11, 700, "CONTRIBUICAO SOCIAL MENSAL DOMESTICO",
             54, "Simples doméstico", 150.0, 0, 0, 0, None]])], ignore_index=True)
        s = series_mensais(fat, pd.DataFrame(), folha, pd.DataFrame(),
                           pd.DataFrame(), self.PER, encargos_df=enc, rub_df=rub)
        res = folha_por_linha(rub, self.PER, encargos_df=enc, fat_pm=s["fat_pm"])
        for p in self.PER:
            assert s["folha_pm"][p] == pytest.approx(float(res["custo_rubricas"][p]))
        # 1000 salario + 150 "Simples domestico" + 400 da 1a parcela do 13o,
        # que e custo no mes em que foi paga
        assert s["folha_pm"]["2025-11"] == pytest.approx(1550.0)

    def test_sem_rub_df_degrada_pro_folha_mensal_de_antes(self):
        """Retrocompat: empresa/janela sem carga do mart de rubricas segue
        exibindo o numero de antes, em vez de zerar o card."""
        folha, rub, enc, fat = self._cenario()
        s = series_mensais(fat, pd.DataFrame(), folha, pd.DataFrame(),
                           pd.DataFrame(), self.PER, encargos_df=enc)
        # 1000 tp11 + 400 tp51 (a 1a parcela e custo)
        assert s["folha_pm"]["2025-11"] == pytest.approx(1400.0)
        assert s["folha_pm"]["2025-12"] == pytest.approx(2000.0)   # tp41 fora

    def test_o_percentual_do_card_e_o_da_tabela(self):
        folha, rub, enc, fat = self._cenario()
        s = series_mensais(fat, pd.DataFrame(), folha, pd.DataFrame(),
                           pd.DataFrame(), self.PER, encargos_df=enc)
        k = kpis_agregados(s)
        res = folha_por_linha(rub, self.PER, encargos_df=enc, fat_pm=s["fat_pm"])
        ponderado = float(res["custo"].sum()) / float(s["fat_pm"].sum()) * 100
        assert k["pct_folenc_med"] == pytest.approx(ponderado)

    def test_a_folha_do_card_e_o_custo_de_rubrica_da_tabela(self):
        # o degrau que reconcilia as duas fontes: mesma folha, mesmos tps fora.
        folha, rub, enc, fat = self._cenario()
        s = series_mensais(fat, pd.DataFrame(), folha, pd.DataFrame(),
                           pd.DataFrame(), self.PER, encargos_df=enc)
        res = folha_por_linha(rub, self.PER, encargos_df=enc, fat_pm=s["fat_pm"])
        for p in self.PER:
            assert float(s["folha_pm"][p]) == pytest.approx(
                float(res["custo_rubricas"][p]))

    def test_o_mes_do_13o_sobe_nos_dois_lados_juntos(self):
        # dez sobe R$ 800,00 no card E na
        # tabela. Se um lado subisse sozinho, seriam dois custos de novo.
        folha, rub, enc, fat = self._cenario()
        s = series_mensais(fat, pd.DataFrame(), folha, pd.DataFrame(),
                           pd.DataFrame(), self.PER, encargos_df=enc)
        res = folha_por_linha(rub, self.PER, encargos_df=enc, fat_pm=s["fat_pm"])
        assert float(s["folha_pm"]["2025-12"]) == 2000.0        # 1200 + 800
        assert float(res["custo"]["2025-12"]) == 2350.0         # + 350 de guia
        assert float(res["pct_fat"]["2025-12"]) == pytest.approx(2350.0 / 30000.0 * 100)


class TestTabelaImpostos:
    """Bloco Impostos no metrics.

    Universo CARGA: guia por codi_imp + ISS pela carga tributaria. O PDF de
    Detalhamento de Impostos consome o MESMO helper — reimplementar criaria
    dois universos com o mesmo nome.
    """
    P = ["2025-01", "2025-02"]

    @staticmethod
    def _imp(rows):
        return pd.DataFrame(rows, columns=["codi_emp", "competencia",
                                           "codi_imp", "srec_sim"])

    @staticmethod
    def _impd(rows):
        return pd.DataFrame(rows, columns=["codi_emp", "competencia",
                                           "fonte", "valo_iss"])

    def _fat(self, a=100000.0, b=100000.0):
        return pd.Series([a, b], index=self.P)

    def test_rotulo_da_linha_de_percentual_e_o_texto_exato(self):
        # o styler do app casa a linha por NOME — trocar o texto sem trocar
        # o consumer faz a linha perder cor e formatacao, em silencio.
        assert LINHA_PCT_IMPOSTOS == "% sobre Faturamento (tributos exibidos)"
        assert LINHA_TOTAL_IMPOSTOS == "TOTAL"

    def test_colunas_sao_periods_CRUS(self):
        # mesmo contrato de folha_por_linha: rotular e do consumer, senao o
        # PDF herdaria os rotulos do Streamlit.
        r = tabela_impostos(self._imp([[1, "2025-01", 1, 500.0]]),
                            pd.DataFrame(), self.P, self._fat())
        assert list(r["tabela"].columns) == self.P

    def test_linha_de_irpj_exibe_o_devido_com_adicional(self):
        # A guia traz principal e adicional; a tabela tem que mostrar o DEVIDO.
        imp = pd.DataFrame({
            "codi_emp": [1, 1], "competencia": ["2025-01"] * 2,
            "codi_imp": [7, 6], "srec_sim": [89362.65, 79210.48],
            "srec_adicional": [52752.06, 0.0],
            "valo_devido": [142114.71, 79210.48],
        })
        r = tabela_impostos(imp, pd.DataFrame(), self.P, self._fat())
        assert float(r["tabela"].loc["IRPJ", "2025-01"]) == pytest.approx(142114.71)
        assert float(r["tabela"].loc["CSLL", "2025-01"]) == pytest.approx(79210.48)

    def test_mart_sem_valo_devido_cai_pro_principal(self):
        # GUARD DE 1o DEPLOY: antes da carga o mart ainda nao tem a coluna.
        # Tem que exibir o principal, nunca estourar KeyError na tela.
        r = tabela_impostos(self._imp([[1, "2025-01", 7, 89362.65]]),
                            pd.DataFrame(), self.P, self._fat())
        assert float(r["tabela"].loc["IRPJ", "2025-01"]) == pytest.approx(89362.65)

    def test_irpj_so_com_adicional_nao_some_da_tabela(self):
        # Borda: principal zerado e adicional com valor. Se o filtro de
        # "linha ativa" olhasse so o principal, a linha sumiria com dinheiro
        # dentro.
        imp = pd.DataFrame({
            "codi_emp": [1], "competencia": ["2025-01"], "codi_imp": [7],
            "srec_sim": [0.0], "srec_adicional": [500.0], "valo_devido": [500.0],
        })
        r = tabela_impostos(imp, pd.DataFrame(), self.P, self._fat())
        assert float(r["tabela"].loc["IRPJ", "2025-01"]) == pytest.approx(500.0)

    def test_iss_da_carga_desloca_a_guia_cod_3(self):
        # a linha ISS e UNA: com o componente de carga presente, o cod 3 SAI
        # das linhas de guia — senao guia + balancete somariam o mesmo ISS
        # duas vezes no TOTAL.
        imp = self._imp([[1, "2025-01", 3, 114394.26],
                         [1, "2025-01", 1, 1000.0]])
        impd = self._impd([[1, "2025-01", "balancete", 274952.35],
                           [1, "2025-02", "balancete", 0.0]])
        r = tabela_impostos(imp, impd, self.P, self._fat())
        assert float(r["tabela"].loc["ISS", "2025-01"]) == 274952.35
        assert float(r["tabela"].loc[LINHA_TOTAL_IMPOSTOS, "2025-01"]) == 275952.35

    def test_sem_componente_de_iss_a_guia_cod_3_permanece(self):
        imp = self._imp([[1, "2025-01", 3, 900.0]])
        r = tabela_impostos(imp, pd.DataFrame(), self.P, self._fat())
        assert float(r["tabela"].loc["ISS", "2025-01"]) == 900.0
        assert r["iss_fontes"] == ()

    def test_tributo_zerado_sai_do_chart_mas_a_linha_iss_zerada_fica(self):
        # barra de altura 0 so ocuparia a legenda. Da TABELA nunca sai: conta
        # de (-)ISS escriturada com zero e informacao ("nao houve ISS no
        # mes"), nao ausencia.
        imp = self._imp([[1, "2025-01", 1, 500.0], [1, "2025-02", 1, 500.0]])
        impd = self._impd([[1, "2025-01", "balancete", 0.0],
                           [1, "2025-02", "balancete", 0.0]])
        r = tabela_impostos(imp, impd, self.P, self._fat())
        assert "ISS" in r["tabela"].index
        assert "ISS" not in r["series_chart"]
        assert "ICMS" in r["series_chart"]

    def test_pct_mensal_com_faturamento_nao_positivo_e_travessao(self):
        # NFe liquida de devolucao pode fechar
        # o mes NEGATIVO (sem piso). A razao com denominador <= 0 nao existe --
        # mesma regua do % Folha e dos cards ("—"), nunca "-1,35%" nem "-0,0".
        imp = self._imp([[1, "2025-01", 1, 500.0], [1, "2025-02", 1, 500.0]])
        r = tabela_impostos(imp, pd.DataFrame(), self.P, self._fat(-860.0, 0.0))
        pct = r["tabela"].loc[LINHA_PCT_IMPOSTOS]
        assert pd.isna(pct["2025-01"])      # negativo
        assert pd.isna(pct["2025-02"])      # zero (regra que ja existia)

    def test_pct_mensal_com_faturamento_positivo_segue_igual(self):
        imp = self._imp([[1, "2025-01", 1, 500.0]])
        r = tabela_impostos(imp, pd.DataFrame(), self.P, self._fat(100000.0, 100000.0))
        assert float(r["tabela"].loc[LINHA_PCT_IMPOSTOS, "2025-01"]) == pytest.approx(0.5)

    def test_ordem_alfabetica_compartilhada_entre_tabela_e_chart(self):
        # o loop do chart usava ordem de APARICAO e ja divergia da tabela
        # antes do ISS entrar.
        imp = self._imp([[1, "2025-01", 5, 10.0],    # PIS
                         [1, "2025-01", 1, 20.0]])   # ICMS
        r = tabela_impostos(imp, pd.DataFrame(), self.P, self._fat())
        tributos = [i for i in r["tabela"].index
                    if i not in (LINHA_TOTAL_IMPOSTOS, LINHA_PCT_IMPOSTOS)]
        assert tributos == sorted(tributos)
        assert list(r["series_chart"]) == tributos

    def test_empresa_com_iss_e_sem_guia_propria_tem_dado(self):
        # ISS pela carga sem guia propria: nao pode cair no ramo
        # "Sem dados de impostos" nem renderizar chart vazio.
        impd = self._impd([[1, "2025-01", "guia+retido", 5000.0],
                           [1, "2025-02", "guia+retido", 3000.0]])
        r = tabela_impostos(pd.DataFrame(), impd, self.P, self._fat())
        assert r["tem_dado"] and list(r["tabela"].index)[0] == "ISS"
        assert "ISS" in r["series_chart"]

    def test_so_retencao_nao_e_o_mesmo_que_sem_dado(self):
        # empresa que so e agente de retencao merece a frase
        # honesta: guias EXISTEM, so nao ha imposto proprio.
        imp = self._imp([[1, "2025-01", 18, 800.0]])   # INSS Ret (terceiro)
        r = tabela_impostos(imp, pd.DataFrame(), self.P, self._fat())
        assert r["so_retencao"] is True and r["tem_dado"] is False
        r2 = tabela_impostos(pd.DataFrame(), pd.DataFrame(), self.P, self._fat())
        assert r2["so_retencao"] is False and r2["tem_dado"] is False

    def test_iss_fontes_declara_proveniencia_sem_ratear(self):
        # dentro de 'guia+retido' o mart guarda so o valo_iss COMBINADO —
        # o helper NOMEIA as fontes, nunca inventa o split em R$.
        impd = self._impd([[1, "2025-01", "balancete", 100.0],
                           [1, "2025-02", "guia+retido", 200.0]])
        r = tabela_impostos(pd.DataFrame(), impd, self.P, self._fat())
        assert r["iss_fontes"] == ("balancete", "guia+retido")

    def test_percentual_e_a_soma_do_que_esta_na_tabela(self):
        imp = self._imp([[1, "2025-01", 1, 1000.0], [1, "2025-02", 1, 2000.0]])
        r = tabela_impostos(imp, pd.DataFrame(), self.P, self._fat(100000.0, 50000.0))
        assert float(r["tabela"].loc[LINHA_PCT_IMPOSTOS, "2025-01"]) == pytest.approx(1.0)
        assert float(r["tabela"].loc[LINHA_PCT_IMPOSTOS, "2025-02"]) == pytest.approx(4.0)

    def test_mes_sem_faturamento_vira_NaN_nunca_zero(self):
        # zero afirmaria carga tributaria nula; "—" declara que falta base.
        imp = self._imp([[1, "2025-01", 1, 1000.0]])
        r = tabela_impostos(imp, pd.DataFrame(), self.P, self._fat(0.0, 100000.0))
        assert pd.isna(r["tabela"].loc[LINHA_PCT_IMPOSTOS, "2025-01"])

    def test_nao_muta_o_dataframe_do_caller(self):
        # o app montava nome_imp IN PLACE no imp_df; com dois consumers isso
        # viraria efeito colateral cruzado.
        imp = self._imp([[1, "2025-01", 1, 500.0]])
        tabela_impostos(imp, pd.DataFrame(), self.P, self._fat())
        assert "nome_imp" not in imp.columns


class TestEncargosSemInssPatronal:
    """Predicado da nota do Simples — regime E dado, nunca so regime.

    A linha de encargos continua UMA so (o CUSTO TOTAL nao muda) e o "—"
    vira NOTA. O predicado que liga a nota tem o `AND` load-bearing: ha
    empresa do Simples com INSS patronal > 0 — construcao civil e
    sociedades de advogados, o perfil do Anexo IV, que recolhe INSS patronal
    FORA do DAS. Regime sozinho poria a nota falsa nesses casos.
    """
    P = ["2025-01", "2025-02"]

    def _reg(self, regimes):
        return pd.DataFrame({"codi_emp": [1] * len(regimes),
                             "competencia": self.P[:len(regimes)],
                             "regime": regimes})

    def _enc(self, inss, fgts=None):
        return pd.DataFrame({"codi_emp": [1] * len(inss),
                             "competencia": self.P[:len(inss)],
                             "inss_empresa": inss,
                             "fgts": fgts or [100.0] * len(inss),
                             "fgts_resc": [0.0] * len(inss)})

    def test_simples_com_inss_zero_ativa_a_nota(self):
        # guia existe, INSS patronal 0,00 REAL, FGTS com valor
        assert encargos_sem_inss_patronal(
            self._reg(["simples", "simples"]), self._enc([0.0, 0.0]), self.P) is True

    def test_simples_ANEXO_IV_com_inss_patronal_NAO_ativa(self):
        # Simples que recolhe INSS patronal em guia.
        # Dizer "esta no DAS" seria FALSO — este assert e a razao do AND.
        assert encargos_sem_inss_patronal(
            self._reg(["simples", "simples"]), self._enc([4862.42, 4862.42]),
            self.P) is False

    def test_real_presumido_nunca_ativa(self):
        assert encargos_sem_inss_patronal(
            self._reg(["real_presumido"] * 2), self._enc([0.0, 0.0]), self.P) is False

    def test_sem_regime_no_mart_nao_afirma(self):
        # sem saber o regime a nota nao pode ser afirmada — False, nao True.
        assert encargos_sem_inss_patronal(pd.DataFrame(), self._enc([0.0]), self.P) is False

    def test_sem_guia_nenhuma_ativa_no_simples(self):
        # nao ha INSS patronal porque nao ha guia: a afirmacao segue verdadeira
        assert encargos_sem_inss_patronal(
            self._reg(["simples"]), pd.DataFrame(), self.P) is True

    def test_olha_so_a_janela_pedida(self):
        # INSS patronal FORA da janela nao pode desligar a nota da janela
        reg = pd.DataFrame({"codi_emp": [1, 1], "competencia": ["2025-01", "2024-01"],
                            "regime": ["simples", "simples"]})
        enc = pd.DataFrame({"codi_emp": [1, 1], "competencia": ["2025-01", "2024-01"],
                            "inss_empresa": [0.0, 5000.0], "fgts": [10.0, 10.0],
                            "fgts_resc": [0.0, 0.0]})
        assert encargos_sem_inss_patronal(reg, enc, self.P) is True

    def test_um_mes_de_simples_basta_mesmo_em_janela_mista(self):
        # regime costuma ser estavel, mas o criterio nao exige unanimidade
        assert encargos_sem_inss_patronal(
            self._reg(["real_presumido", "simples"]), self._enc([0.0, 0.0]),
            self.P) is True

    def test_as_duas_notas_sao_complementares_e_nomeiam_a_fonte(self):
        # a nota padrao DECLARA a fonte (guia) porque encargo e natureza
        # diferente das rubricas; a do Simples explica por que so ha FGTS.
        # Constantes no metrics: sao consumidas pelo app E pelo PDF.
        assert "guias de INSS e FGTS" in NOTA_ENCARGOS_GUIA
        assert "DAS" in NOTA_ENCARGOS_SIMPLES and "FGTS" in NOTA_ENCARGOS_SIMPLES
        assert NOTA_ENCARGOS_GUIA != NOTA_ENCARGOS_SIMPLES


class TestImpostosSemIrpjProprio:
    """A regua "inclui IRPJ/CSLL sobre o lucro" se aplica?

    Empresa do Simples NAO tem IRPJ/CSLL proprio — ele esta dentro do DAS.
    A frase, que existe pra declarar o universo do numerador, e FALSA pra ela.

    O `AND` com a provisao e coerencia do CARD, nao adivinhacao: o card exibe
    "IRPJ/CSLL do periodo: R$ X" sempre que |irpj_total| >= 0,005. Esconder a
    regua e mostrar o valor deixaria um numero sem explicacao na mesma tela.
    E o mesmo idioma do `encargos_sem_inss_patronal` logo acima.
    """
    P = ["2025-01", "2025-02"]

    def _reg(self, regimes):
        return pd.DataFrame({"codi_emp": [1] * len(regimes),
                             "competencia": self.P[:len(regimes)],
                             "regime": regimes})

    def test_simples_sem_provisao_esconde_a_frase(self):
        # 12 meses de Simples, provisao 0,00
        assert impostos_sem_irpj_proprio(
            self._reg(["simples", "simples"]), self.P, 0.0) is True

    def test_real_presumido_mantem_a_frase(self):
        # o numerador inclui IRPJ/CSLL por definicao
        assert impostos_sem_irpj_proprio(
            self._reg(["real_presumido"] * 2), self.P, 990157.06) is False

    def test_real_presumido_SEM_provisao_no_periodo_mantem_a_frase(self):
        # a frase e regua do UNIVERSO do numerador, nao afirmacao sobre o
        # valor: R/P com prejuizo fiscal segue sendo um regime que apura
        # IRPJ/CSLL.
        assert impostos_sem_irpj_proprio(
            self._reg(["real_presumido"] * 2), self.P, 0.0) is False

    def test_simples_COM_provisao_mantem_a_frase(self):
        # o AND: se o card vai exibir "IRPJ/CSLL do periodo: R$ X", esconder a
        # regua deixa o valor orfao. Coerencia do card ganha do regime.
        assert impostos_sem_irpj_proprio(
            self._reg(["simples", "simples"]), self.P, 1234.56) is False
        # e o limiar e o MESMO do consumer (0,005), nao um proprio
        assert impostos_sem_irpj_proprio(
            self._reg(["simples"]), self.P, 0.004) is True

    def test_sem_regime_no_mart_mantem_a_frase(self):
        # sem saber o regime nao se pode afirmar "nao tem IRPJ proprio" —
        # o default preserva o legado (mesmo recorte do irmao acima).
        assert impostos_sem_irpj_proprio(None, self.P, 0.0) is False
        assert impostos_sem_irpj_proprio(pd.DataFrame(), self.P, 0.0) is False
        assert impostos_sem_irpj_proprio(
            pd.DataFrame({"codi_emp": [1], "competencia": ["2025-01"]}),
            self.P, 0.0) is False

    def test_olha_so_a_janela_pedida(self):
        # Simples FORA da janela nao pode esconder a frase da janela
        reg = pd.DataFrame({"codi_emp": [1, 1], "competencia": ["2024-01", "2024-02"],
                            "regime": ["simples", "simples"]})
        assert impostos_sem_irpj_proprio(reg, self.P, 0.0) is False

    def test_janela_mista_sem_provisao_esconde(self):
        # transicao de regime no meio do ano: sem
        # provisao NENHUMA na janela, nao ha IRPJ pra declarar. `any` de
        # Simples basta, igual ao irmao — e o AND cobre o caso com dinheiro.
        assert impostos_sem_irpj_proprio(
            self._reg(["simples", "real_presumido"]), self.P, 0.0) is True
        assert impostos_sem_irpj_proprio(
            self._reg(["simples", "real_presumido"]), self.P, 500.0) is False

    def test_irpj_total_NaN_nao_mantem_a_frase_por_acidente(self):
        # NaN nao e "tem provisao": `abs(nan) >= 0.005` e False em Python, mas
        # o assert existe pra travar a semantica, nao o acaso da implementacao.
        assert impostos_sem_irpj_proprio(
            self._reg(["simples"]), self.P, float("nan")) is True


class TestMaioresLinhas:
    """Cards de destaque das faixas dos detalhamentos.

    Leem o MESMO DataFrame que a tabela exibe — o card e a linha tem que ser o
    mesmo numero, e a unica forma de garantir e nao recalcular.
    """
    def test_ordena_por_soma_no_periodo_e_corta_em_n(self):
        t = pd.DataFrame({"2025-01": [10.0, 50.0, 30.0, 5.0],
                          "2025-02": [10.0, 5.0, 30.0, 1.0]},
                         index=["A", "B", "C", "D"])
        assert maiores_linhas(t, 3) == [("C", 60.0), ("B", 55.0), ("A", 20.0)]

    def test_exclui_linhas_de_total_e_percentual(self):
        # TOTAL venceria qualquer ranking; a linha de % nem e um item.
        t = pd.DataFrame({"2025-01": [10.0, 50.0, 60.0, 8.6]},
                         index=["A", "B", LINHA_TOTAL_IMPOSTOS, LINHA_PCT_IMPOSTOS])
        r = maiores_linhas(t, 3, excluir=(LINHA_TOTAL_IMPOSTOS, LINHA_PCT_IMPOSTOS))
        assert [n for n, _ in r] == ["B", "A"]

    def test_menos_de_n_itens_com_valor_rende_menos_cards(self):
        # nunca card de enchimento: o Simples tem UM tributo so.
        t = pd.DataFrame({"2025-01": [100.0, 0.0, 0.0]}, index=["Simples", "B", "C"])
        assert maiores_linhas(t, 3) == [("Simples", 100.0)]

    def test_linha_que_arredonda_pra_zero_nao_e_item(self):
        # mesmo limiar do fmt_brl: o que exibe "R$ 0,00" nao e movimento.
        t = pd.DataFrame({"2025-01": [100.0, 0.004]}, index=["A", "B"])
        assert [n for n, _ in maiores_linhas(t, 3)] == ["A"]

    def test_tabela_vazia_nao_estoura(self):
        assert maiores_linhas(pd.DataFrame(), 3) == []
        assert maiores_linhas(None, 3) == []


class TestLegendaTravessao:
    """Legenda do "—" na linha de percentual.

    So aparece quando ha travessao — nota orfa confunde (a mesma licao do
    marcador (*) de is_partial). Fonte unica dos dois consumers.
    """
    def test_sem_travessao_nao_ha_legenda(self):
        assert legenda_travessao([10.0, 20.0, 0.0]) == ""

    def test_com_NaN_ganha_a_legenda(self):
        assert legenda_travessao([10.0, float("nan")]) == NOTA_TRAVESSAO_PCT

    def test_infinito_conta_como_travessao(self):
        # fmt_pct mapeia inf pra "—" tambem
        assert legenda_travessao([float("inf"), 1.0]) == NOTA_TRAVESSAO_PCT

    def test_serie_vazia_ou_None_nao_estoura(self):
        assert legenda_travessao([]) == ""
        assert legenda_travessao(None) == ""

    def test_legenda_cobre_as_DUAS_causas_sem_mentir(self):
        # denominador ZERO e denominador positivo porem IMATERIAL (piso)
        # produzem o MESMO travessao — a frase nao pode afirmar so uma delas.
        assert "sirva de base" in NOTA_TRAVESSAO_PCT
        assert "imaterial" not in NOTA_TRAVESSAO_PCT   # nao afirma a causa

    def test_rotulo_do_13o_perdeu_o_qualificador_por_DECISAO(self):
        """O rotulo nao leva qualificador "(integral, no fechamento)".

        A 1a parcela aparece no mes em que e paga, entao o qualificador
        MENTIRIA (a coluna de novembro mostra so a 1a parcela, e a de
        dezembro mostra o integral MENOS o estorno).

        Distinguir a LINHA da FAMILIA homonima "13º Salário" do dicionario da
        origem (cujos reflexos pagos no mensal pousam em "Outros") e trabalho
        da NOTA — por isso este teste exige a nota junto.
        """
        assert LINHA_13O == "13º Salário"
        assert LINHA_13O in LINHAS_FOLHA_RUBRICA
        # a clausula dos reflexos tem de estar coberta pela nota
        assert "Outros" in NOTA_LINHA_13O


class TestLegendaOutros:
    """A lista do "Outros" e LEGENDA -- helper unico dos 2 lados.

    O conteudo (top OUTROS_TOP_N + "Demais") sai numa linha corrida embaixo
    do grafico de composicao.
    """

    def test_formata_rubricas_em_linha_corrida(self):
        from etl.metrics import legenda_outros
        top = pd.Series({"PRO-LABORE": 10000.0, "AUXILIO TRANSPORTE": 2500.0})
        out = legenda_outros(top, 0.0)
        assert out.startswith("Outros: ")
        assert "PRO-LABORE R$ 10.000,00" in out
        assert "AUXILIO TRANSPORTE R$ 2.500,00" in out
        assert " · " in out                      # separador pedido

    def test_demais_entra_no_fim_quando_ha_resto(self):
        from etl.metrics import legenda_outros
        out = legenda_outros(pd.Series({"PRO-LABORE": 10.0}), 1234.56)
        assert out.endswith("Demais R$ 1.234,56")

    def test_sem_resto_nao_inventa_demais(self):
        from etl.metrics import legenda_outros
        assert "Demais" not in legenda_outros(pd.Series({"X": 10.0}), 0.0)

    def test_vazio_devolve_string_vazia(self):
        from etl.metrics import legenda_outros
        assert legenda_outros(pd.Series(dtype=float), 0.0) == ""

    def test_nomes_das_rubricas_nao_sao_traduzidos(self):
        from etl.metrics import legenda_outros
        out = legenda_outros(pd.Series({"MULTA ART. 479/CLT": 500.0}), 0.0)
        assert "MULTA ART. 479/CLT" in out


class TestMesSoComAntecipacaoNaoViraColunaDeZeros(_RubBase):
    """Mes sem dado de CUSTO = coluna AUSENTE, nunca zero.

    Com o tp60 fora do custo, mes SO com recibo de ferias exibiria uma
    coluna inteira de R$ 0,00, com CUSTO TOTAL R$ 0,00 e barra vazia no
    chart. Zero ali afirma "a folha custou nada"; o que houve foi folha ainda nao
    processada. O corte de `presentes` e sobre as linhas de CUSTO, nao sobre o
    frame cru.
    """
    PER = ["2026-03", "2026-04"]

    def test_mes_so_com_recibo_de_ferias_some_da_tabela(self):
        r = folha_por_linha(self._rub([
            [203, "2026-03", 11, 1, "SALARIO BASE", 1, "Salários",
             1000.0, 0, 0, 0, None],
            [203, "2026-04", 60, 3, "FERIAS", 5, "Férias Gozadas",
             5477.78, 0, 0, 0, None]]), self.PER)
        assert list(r["tabela"].columns) == ["2026-03"]
        assert "2026-04" not in list(r["custo"].index)
        # e o dinheiro do recibo continua VISIVEL como nota, nao sumiu
        assert float(r["antecipacoes"].sum()) == pytest.approx(5477.78)

    def test_mes_com_custo_E_antecipacao_continua_na_tabela(self):
        r = folha_por_linha(self._rub([
            [203, "2026-03", 11, 1, "SALARIO BASE", 1, "Salários",
             1000.0, 0, 0, 0, None],
            [203, "2026-03", 60, 3, "FERIAS", 5, "Férias Gozadas",
             500.0, 0, 0, 0, None]]), ["2026-03"])
        assert list(r["tabela"].columns) == ["2026-03"]
        assert float(r["custo"]["2026-03"]) == pytest.approx(1000.0)


class TestDecimoTerceiroPorParcela(_RubBase):
    """Cada parcela do 13o no mes em que foi paga.

    Se o tp51 fosse antecipacao fora do custo, o 13o inteiro pousaria no mes
    do fechamento, e quem paga a 1a parcela em junho veria junho sem 13o
    nenhum.
    """
    PER = ["2025-06", "2025-12"]

    def _cenario(self):
        return self._rub([
            [1, "2025-06", 11, 1, "SALARIO BASE", 1, "Salários", 5000.0, 0, 0, 0, None],
            [1, "2025-06", 51, 13, "13 SALARIO ADIANTADO", 4, "13º Salário",
             800.0, 0, 0, 0, None],
            [1, "2025-12", 11, 1, "SALARIO BASE", 1, "Salários", 5000.0, 0, 0, 0, None],
            [1, "2025-12", 52, 12, "13 SALARIO INTEGRAL", 4, "13º Salário",
             1600.0, 0, 0, 0, None],
            [1, "2025-12", 52, 50, "ADIANTAMENTO 13 SALARIO", 4, "13º Salário",
             0, 800.0, 0, 0, None],
        ])

    def test_a_1a_parcela_aparece_no_mes_pago(self):
        r = folha_por_linha(self._cenario(), self.PER)
        assert float(r["tabela"].loc[LINHA_13O, "2025-06"]) == pytest.approx(800.0)

    def test_o_estorno_abate_o_mes_do_fechamento(self):
        """O tp52 traz o INTEGRAL; sem abater o estorno, a 1a parcela contaria
        duas vezes."""
        r = folha_por_linha(self._cenario(), self.PER)
        assert float(r["tabela"].loc[LINHA_13O, "2025-12"]) == pytest.approx(800.0)
        assert float(r["abatimentos"]["estorno_13o"]["2025-12"]) == pytest.approx(800.0)

    def test_a_soma_do_ano_nao_se_move(self):
        """O ponto da mudanca: muda o TIMING, nao o total. 1600 de 13o no ano,
        distribuidos 800 em junho e 800 em dezembro."""
        r = folha_por_linha(self._cenario(), self.PER)
        assert float(r["tabela"].loc[LINHA_13O].sum()) == pytest.approx(1600.0)
        assert float(r["custo"].sum()) == pytest.approx(11600.0)

    def test_estorno_do_ACERTO_abate_a_linha_de_rescisao(self):
        """Quando o empregado sai entre as parcelas, a origem lanca o estorno
        no tp11, e o provento que ele espelha e o 13o proporcional, que a
        precedencia de nome manda pra "Rescisão" — nao pra linha do 13o.
        Apontar tudo pro 13o estouraria a linha; o destino fica amarrado ao
        processo de origem.
        """
        r = folha_por_linha(self._rub([
            [1, "2025-06", 11, 1, "SALARIO BASE", 1, "Salários", 5000.0, 0, 0, 0, None],
            [1, "2025-06", 11, 8550, "13 SALARIO INTEGRAL RESCISAO", 4,
             "13º Salário", 900.0, 0, 0, 0, None],
            [1, "2025-06", 11, 8566, "ADIANTAMENTO 13 SALARIO RESCISAO", 4,
             "13º Salário", 0, 400.0, 0, 0, None]]), ["2025-06"])
        assert float(r["abatimentos"]["estorno_13o_resc"]["2025-06"]) == pytest.approx(400.0)
        assert float(r["tabela"].loc["Rescisão", "2025-06"]) == pytest.approx(500.0)
        assert LINHA_13O not in r["tabela"].index      # nao ha 13o proprio no mes

    def test_a_variante_rescisoria_NAO_e_excluida_do_padrao(self):
        """O evt 8566 E o estorno — excluir RESCISAO do padrao deixaria a 1a
        parcela contada duas vezes."""
        from etl.mappings import RE_ESTORNO_13O
        from etl.utils import normaliza_rubrica
        assert RE_ESTORNO_13O.search(
            normaliza_rubrica("ADIANTAMENTO 13 SALARIO RESCISAO"))
        assert RE_ESTORNO_13O.search(normaliza_rubrica("ADIANTAMENTO 13º MEDIA VALOR"))

    def test_o_estorno_NAO_entra_na_legenda_de_abatimentos(self):
        """A legenda abre com "Fora do custo no periodo:", e o estorno e o
        oposto disso: dinheiro que FOI desembolsado, so em outro mes."""
        txt = legenda_abatimentos({"estorno_13o": pd.Series([800.0]),
                                   "afastamento": pd.Series([100.0])})
        assert "afastamento" in txt
        assert "13" not in txt and "estorno" not in txt.lower()
