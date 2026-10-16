# ---------------------------------------------------------------------------
# Guard de janela do run_transform: raw.ctlancto pode ser
# mais largo que a janela do run (upsert do raw nunca deleta). Lançamento fora
# de [inicio, fim] dupla-conta com o si do corte e retroage phantom.
# ---------------------------------------------------------------------------
from types import SimpleNamespace

import pandas as pd
import pytest

from main import (
    SaldoInicialTotalmenteDescartado,
    valida_flags,
    MART_KEYS,
    RAW_KEYS,
    _mes_anterior,
    alem_do_declarado,
    clamp_janela_competencia,
    contar_anos_suspeitos_ctlancto,
    data_alem_do_declarado,
    clamp_janela_ctlancto,
    decide_janela_v4,
    format_run_report,
    mes_declarado,
    run_read_raw,
    run_transform,
    trim_fobases_overlap,
    valida_janela,
    watermark_do_run,
)
from transform import DRE_COLS, build_folha_mensal


class TestContarAnosSuspeitosCtlancto:
    """Alerta de sanidade, zero mudanca de numero.

    Typos de milenio no raw.ctlancto: `data_lan` fora de [2000, 2100]
    (padrao de digito trocado, ex. "8021" pra 2021). O clamp de janela ja
    confina o ctlancto a janela do run -- este alerta so CONTA e EXPOE,
    nunca filtra nem muda o dado que os builds consomem.
    """

    def test_conta_anos_fora_do_intervalo(self):
        lan = pd.DataFrame({
            "codi_emp": [1, 1, 1],
            "data_lan": ["2025-03-01", "8021-02-05", "2089-01-01"],
        })
        n, (ano_min, ano_max) = contar_anos_suspeitos_ctlancto(lan)
        assert n == 1
        assert (ano_min, ano_max) == (8021, 8021)

    def test_dentro_do_intervalo_nao_conta(self):
        lan = pd.DataFrame({
            "codi_emp": [1, 1],
            "data_lan": ["2000-01-01", "2100-12-31"],
        })
        n, anos = contar_anos_suspeitos_ctlancto(lan)
        assert n == 0
        assert anos is None

    def test_df_vazio_e_noop(self):
        n, anos = contar_anos_suspeitos_ctlancto(pd.DataFrame())
        assert n == 0 and anos is None

    def test_sem_coluna_data_lan_e_noop(self):
        n, anos = contar_anos_suspeitos_ctlancto(pd.DataFrame({"codi_emp": [1]}))
        assert n == 0 and anos is None


class TestClampJanelaCtlancto:
    def test_remove_lancamentos_fora_da_janela(self):
        lan = pd.DataFrame({
            "codi_emp": [1, 1, 1, 1],
            "data_lan": ["2022-12-31", "2023-01-15", "2026-04-30", "2026-05-01"],
            "cdeb_lan": [10] * 4, "ccre_lan": [20] * 4,
            "vlor_lan": [1.0, 2.0, 3.0, 4.0],
        })
        out, n_fora = clamp_janela_ctlancto(lan, "2023-01", "2026-04")
        assert n_fora == 2
        assert out["data_lan"].tolist() == ["2023-01-15", "2026-04-30"]

    def test_raw_limpo_e_noop(self):
        lan = pd.DataFrame({
            "codi_emp": [1], "data_lan": ["2024-06-10"],
            "cdeb_lan": [10], "ccre_lan": [20], "vlor_lan": [1.0],
        })
        out, n_fora = clamp_janela_ctlancto(lan, "2023-01", "2026-04")
        assert n_fora == 0
        assert out is lan  # zero efeito no estado vigente

    def test_df_vazio_e_noop(self):
        out, n_fora = clamp_janela_ctlancto(pd.DataFrame(), "2023-01", "2026-04")
        assert n_fora == 0 and out.empty


# ---------------------------------------------------------------------------
# Run janelado: pra nao zerar qtd_inicial do 1º mês, a orquestração estende
# a extração de fobases 1 mês pra trás (âncora do shift) e apara o overlap
# antes do upsert de raw e do build da folha. Só o build de funcionarios vê N−1.
# ---------------------------------------------------------------------------


class TestMesAnterior:
    def test_mes_anterior_simples(self):
        assert _mes_anterior("2025-02") == "2025-01"

    def test_rollover_de_janeiro(self):
        assert _mes_anterior("2026-01") == "2025-12"

    def test_permissivo_passthrough(self):
        # mesmo contrato permissivo de _period_start: formato inesperado
        # (ou falsy) passa como veio, sem levantar.
        assert _mes_anterior(None) is None
        assert _mes_anterior("") == ""
        assert _mes_anterior("2025-01-01") == "2025-01-01"


class TestTrimFobasesOverlap:
    def test_remove_linhas_pre_janela(self):
        fb = pd.DataFrame({
            "codi_emp": [1, 1, 1],
            "competencia": ["2024-12-01", "2025-01-01", "2025-02-01"],
            "tipo_process": [11, 11, 11],
        })
        out, n_pre = trim_fobases_overlap(fb, "2025-01")
        assert n_pre == 1
        assert out["competencia"].tolist() == ["2025-01-01", "2025-02-01"]

    def test_sem_overlap_e_noop(self):
        fb = pd.DataFrame({
            "codi_emp": [1], "competencia": ["2025-01-01"], "tipo_process": [11],
        })
        out, n_pre = trim_fobases_overlap(fb, "2025-01")
        assert n_pre == 0
        assert out is fb

    def test_df_vazio_e_noop(self):
        out, n_pre = trim_fobases_overlap(pd.DataFrame(), "2025-01")
        assert n_pre == 0 and out.empty


class TestRunExtractOverlapFobases:
    def test_fobases_extrai_com_um_mes_de_overlap(self):
        import main as m
        calls: dict = {}

        class _Fonte:
            def ler(self, tabela, empresa=None, inicio=None, fim=None):
                calls[tabela] = (empresa, inicio, fim)
                return pd.DataFrame()

            def saldo_inicial(self, empresa, inicio):
                return pd.DataFrame()

            def max_datas(self):
                return {}

        m.run_extract(1, "2025-01", "2025-03", fonte=_Fonte())

        # fobases E forescisoes: janela estendida 1 mês pra trás (a
        # demissão de N−1 liquida os ativos do mês-âncora).
        # As demais leituras janeladas seguem [inicio, fim].
        assert calls["fobases"] == (1, "2024-12", "2025-03")
        assert calls["ctlancto"] == (1, "2025-01", "2025-03")
        assert calls["foempregados"] == (1, "2025-01", "2025-03")
        assert calls["forescisoes"] == (1, "2024-12", "2025-03")


class TestRunTransformFobasesFuncionarios:
    def _raw(self):
        fob_full = pd.DataFrame({
            "codi_emp": [1] * 8,
            "competencia": ["2024-12-01"] * 2 + ["2025-01-01"] * 3
                           + ["2025-02-01"] * 3,
            "tipo_process": [11] * 8,
            "i_empregados": [101, 102, 101, 102, 103, 101, 102, 103],
            "proventos": [100.0] * 8,
            "descontos": [10.0] * 8,
        })
        fob_trim = fob_full[fob_full["competencia"] >= "2025-01-01"].reset_index(drop=True)
        foemp = pd.DataFrame({
            "codi_emp": [1, 1, 1],
            "admissao": ["2024-11-01", "2024-11-01", "2025-01-15"],
            "i_empregados": [101, 102, 103],
        })
        raw = {"foempregados": foemp, "fobases": fob_trim,
               "forescisoes": pd.DataFrame()}
        return raw, fob_full

    def test_funcionarios_ancora_no_overlap_sem_vazar_mes_pre_janela(self):
        raw, fob_full = self._raw()
        marts = run_transform(raw, "2025-01", "2025-03",
                              fobases_funcionarios=fob_full)
        func = marts["funcionarios_mensal"]
        jan = func[func["competencia"] == "2025-01"].iloc[0]
        assert jan["qtd_inicial"] == 2          # qtd_final de dez/24 (overlap)
        # ent/sai ancoram no MESMO N-1 — jan ENT=1 (so o 103), nao 3.
        assert (jan["qtd_admissoes"], jan["qtd_rescisoes"]) == (1, 0)
        assert "2024-12" not in func["competencia"].tolist()

    def test_folha_nao_ve_o_mes_de_overlap(self):
        raw, fob_full = self._raw()
        marts = run_transform(raw, "2025-01", "2025-03",
                              fobases_funcionarios=fob_full)
        assert "2024-12" not in marts["folha_mensal"]["competencia"].tolist()

    def test_sem_kwarg_usa_raw_como_hoje(self):
        # caminho skip-extract com raw trimado (banco novo, 1o run): sem N-1
        # a serie NASCE em jan — inicial=0, ent=3 e a linha FECHA (0+3-0=3).
        # Pina o display do 1o run em banco novo.
        raw, _ = self._raw()
        marts = run_transform(raw, "2025-01", "2025-03")
        jan = marts["funcionarios_mensal"]
        jan = jan[jan["competencia"] == "2025-01"].iloc[0]
        assert jan["qtd_inicial"] == 0          # raw trimado, sem N−1
        assert (jan["qtd_admissoes"], jan["qtd_rescisoes"], jan["qtd_final"]) == (3, 0, 3)

    def test_forescisoes_overlap_ancora_ativos_do_mes_n_menos_1(self):
        # Demissao registrada no mes de OVERLAP (dez) so
        # chega ao build via kwarg forescisoes_funcionarios — ativos(dez)
        # liquido -> jan inicial=1 SEM saida-fantasma, IGUAL ao caminho
        # raw-full (determinismo extract == skip-extract).
        raw, fob_full = self._raw()
        fr_full = pd.DataFrame({
            "codi_emp": [1], "i_calculos": ["g0"], "i_empregados": [102],
            "demissao": ["2024-12-28"], "tipo": [1],
        })
        marts = run_transform(raw, "2025-01", "2025-03",
                              fobases_funcionarios=fob_full,
                              forescisoes_funcionarios=fr_full)
        func = marts["funcionarios_mensal"]
        jan = func[func["competencia"] == "2025-01"].iloc[0]
        # dez presenca {101,102}; ativos dez {101}; jan presenca {101,102,103}
        # -> inicial=1, entradas={102,103}=2, saidas=0, final=3 (fecha).
        assert (jan["qtd_inicial"], jan["qtd_admissoes"], jan["qtd_rescisoes"], jan["qtd_final"]) == (1, 2, 0, 3)


class TestFobasesGraoDeterminismo:
    # Com a chave 5-col (grao real do calculo), o dedup do load
    # (load.py::_upsert, keep="last") vira no-op para recibos irmaos
    # (mesmo grao 4-col, i_calculos distinto) e o build soma os dois --
    # extract == --skip-extract por construcao (mesmo caso do pdic_sim).
    def test_dedup_preserva_recibos_irmaos_e_build_soma(self):
        fb = pd.DataFrame({
            "codi_emp": [201, 201],
            "i_empregados": [703, 703],
            "competencia": ["2024-10-01", "2024-10-01"],
            "tipo_process": [92, 92],
            "proventos": [756.72, 1732.85],
            "descontos": [20.43, 46.77],
            "i_calculos": ["guid-a", "guid-b"],
        })
        pos_dedup = fb.drop_duplicates(subset=RAW_KEYS["fobases"], keep="last")
        assert len(pos_dedup) == 2  # no-op: nenhuma linha comida

        out = build_folha_mensal(pos_dedup)
        assert len(out) == 1
        assert out.loc[0, "valo_bruto"] == pytest.approx(2489.57)
        assert out.loc[0, "valo_descontos"] == pytest.approx(67.20)


class TestFormatRunReport:
    # Resumo de fim de run (observabilidade): funcao PURA main.format_run_report.
    # Compacto (<= ~18 linhas: o Streamlit exibe o tail de 20 linhas do stdout)
    # e com ALERTAS por ultimo.
    def _report_limpo(self):
        return {
            "args": {"empresa": "201", "inicio": "2025-01", "fim": "2025-03",
                     "modo": "completo"},
            "extract": {"fobases": 1947}, "extract_errors": [],
            "raw_upsert": {"fobases": 1166}, "dedup_delta": {},
            "overlap_trim": 781, "clamp_fora": 0,
            "si_descartado": {"linhas": 0, "empresas": 0, "corte": "2025-01-01"},
            "si_zumbi_emps": [], "sem_si_com_lancto": False,
            "marts": {"folha_mensal": 10}, "mart_errors": [],
        }

    def test_estado_limpo_sem_alertas(self):
        import main as m
        out = m.format_run_report(self._report_limpo())
        assert "ALERTA" not in out
        assert "sem alertas" in out.lower()
        assert "overlap fobases: 781" in out

    def test_si_descartado_com_lancto_e_alerta_destacado(self):
        import main as m
        r = self._report_limpo()
        r["si_descartado"] = {"linhas": 3158, "empresas": 1, "corte": "2025-01-01"}
        r["si_zumbi_emps"] = [202]
        out = m.format_run_report(r)
        assert "ALERTA" in out
        assert "202" in out
        assert "zero-base" in out          # nomeia o vetor do zumbi
        # alertas ficam no FIM do bloco (tail do stdout na UI)
        pos_alerta = out.index("ALERTA")
        assert "marts:" in out[:pos_alerta]

    def test_dedup_nao_noop_e_alerta(self):
        import main as m
        r = self._report_limpo()
        r["dedup_delta"] = {"fobases": 108}
        out = m.format_run_report(r)
        assert "ALERTA" in out and "fobases" in out and "108" in out


class TestRunTransformPopulaReport:
    # O bloco de stale-check do si POPULA o report:
    # linhas descartadas + empresas com si 100% descartado E lancamentos
    # na janela (= candidatas a balancete zero-base).
    #
    # So e ALERTA quem tem lancamento ANTES do corte
    # (min(data_lan) < inicio) e mesmo assim ficou sem si no corte deste
    # run. Empresa que abriu DENTRO da janela (nada antes do corte pra
    # carregar) e zero-base CORRETO, nao alerta.
    def test_si_stale_sem_lancto_pre_corte_nao_alerta_falso_positivo(self):
        si = pd.DataFrame({
            "codi_emp": [1, 2], "codi_cta": [10, 10],
            "valo_deb": [5.0, 5.0], "valo_cre": [0.0, 0.0],
            "data_corte": ["2024-01-01", "2025-01-01"],
        })
        lan = pd.DataFrame({
            "codi_emp": [1], "data_lan": ["2025-02-10"],  # dentro da janela
            "cdeb_lan": [10], "ccre_lan": [20], "vlor_lan": [1.0],
        })
        raw = {"ctsaldo_inicial": si, "ctlancto": lan,
               "ctcontas": pd.DataFrame({"codi_emp": [1], "codi_cta": [10],
                                         "clas_cta": ["101"]})}
        report = {}
        run_transform(raw, "2025-01", "2025-03", report=report)
        assert report["si_descartado"]["linhas"] == 1
        assert report["si_zumbi_emps"] == []

    def test_si_stale_com_lancto_pre_corte_alerta_caso_legitimo(self):
        si = pd.DataFrame({
            "codi_emp": [1, 2], "codi_cta": [10, 10],
            "valo_deb": [5.0, 5.0], "valo_cre": [0.0, 0.0],
            "data_corte": ["2024-01-01", "2025-01-01"],
        })
        lan = pd.DataFrame({
            "codi_emp": [1], "data_lan": ["2024-06-15"],  # antes do corte
            "cdeb_lan": [10], "ccre_lan": [20], "vlor_lan": [1.0],
        })
        raw = {"ctsaldo_inicial": si, "ctlancto": lan,
               "ctcontas": pd.DataFrame({"codi_emp": [1], "codi_cta": [10],
                                         "clas_cta": ["101"]})}
        report = {}
        run_transform(raw, "2025-01", "2025-03", report=report)
        assert report["si_descartado"]["linhas"] == 1
        assert report["si_zumbi_emps"] == [1]


# ---------------------------------------------------------------------------
# Skip-extract janelado: o raw entra FULL e o balancete sai clampado
# → par R/P fora da janela entra no grid da cascata com numerador
# truncado ('sem_deducao'/NULL) e o DELETE-escopado grava o lixo por cima
# do par bom no mart.
# O clamp da cascata confina regime/destacados a [inicio, fim].
# ---------------------------------------------------------------------------

class TestClampCascataJanela:
    def _raw_skip_janelado(self):
        # raw FULL (caminho skip-extract): sinal R/P em 2024-06 (FORA da
        # janela) e 2025-02 (dentro); efsaidas de 2024-06 dá den_fat > 0 —
        # assinatura 'sem_deducao'.
        efsdoimp = pd.DataFrame({
            "codi_emp": [201, 201],
            "codi_imp": [1, 1],
            "data_sim": ["2024-06-20", "2025-02-20"],
            "pdic_sim": [6, 6],
            "srec_sim": [100.0, 120.0],
            "bcal_sim": [1000.0, 1200.0],
        })
        efsaidas = pd.DataFrame({
            "codi_emp": [201, 201],
            "codi_sai": [1, 2],
            "dsai_sai": ["2024-06-10", "2025-02-10"],
            "vcon_sai": [5000.0, 6000.0],
        })
        efentradas = pd.DataFrame(columns=["codi_emp", "dent_ent", "vcon_ent"])
        return {"efsdoimp": efsdoimp, "efsaidas": efsaidas,
                "efentradas": efentradas}

    def test_cascata_nao_contem_pares_fora_da_janela(self):
        marts = run_transform(self._raw_skip_janelado(), "2025-01", "2025-03")
        reg = marts["empresa_regime_periodo"]
        dest = marts["impostos_destacados_mensais"]
        for nome, df in (("regime", reg), ("destacados", dest)):
            comp = df["competencia"]
            dentro = (comp >= "2025-01") & (comp <= "2025-03")
            assert dentro.all(), (
                f"{nome} contém par fora da janela: "
                f"{sorted(comp[~dentro].tolist())}"
            )
        # o par de dentro da janela segue íntegro
        assert reg.loc[reg["competencia"] == "2025-02", "regime"].tolist() \
            == ["real_presumido"]
        assert "2025-02" in dest["competencia"].tolist()

    def test_report_e_resumo_registram_o_clamp(self):
        report = {"args": {"empresa": "201", "inicio": "2025-01",
                           "fim": "2025-03", "modo": "skip-extract"}}
        run_transform(self._raw_skip_janelado(), "2025-01", "2025-03",
                      report=report)
        # o clamp no regime já confina o grid herdado dos destacados —
        # só o regime conta pares fora.
        assert report["cascata_clamp"] == {"empresa_regime_periodo": 1}
        assert "cascata clampada a janela" in format_run_report(report)


class TestClampJanelaCompetencia:
    def _df(self):
        return pd.DataFrame({
            "codi_emp": [1] * 4,
            "competencia": ["2024-12", "2025-01", "2025-03", "2025-04"],
        })

    def test_bordas_inclusivas(self):
        out, n_fora = clamp_janela_competencia(self._df(), "2025-01", "2025-03")
        assert n_fora == 2
        assert out["competencia"].tolist() == ["2025-01", "2025-03"]

    def test_janela_cobre_tudo_e_noop_mesmo_objeto(self):
        df = self._df()
        out, n_fora = clamp_janela_competencia(df, "2024-01", "2026-12")
        assert n_fora == 0
        assert out is df  # zero efeito nos caminhos full/extract janelado

    def test_janela_sem_intersecao_esvazia_e_conta_tudo(self):
        df = self._df()
        out, n_fora = clamp_janela_competencia(df, "2020-01", "2020-12")
        assert out.empty and n_fora == len(df)

    def test_df_vazio_e_noop(self):
        out, n_fora = clamp_janela_competencia(pd.DataFrame(), "2025-01", "2025-03")
        assert n_fora == 0 and out.empty


# ---------------------------------------------------------------------------
# Guard de janela invertida: inicio > fim processaria NADA e sairia
# "sem alertas". Falha cedo, com mensagem acionável, dentro do try do
# main() (vira ERRO FATAL + exit 1).
# ---------------------------------------------------------------------------

class TestValidaJanela:
    def test_janela_invertida_levanta_erro_com_mensagem_acionavel(self):
        with pytest.raises(ValueError, match="janela invertida"):
            valida_janela("2026-12", "2026-07")

    def test_janela_valida_nao_levanta(self):
        valida_janela("2023-01", "2026-12")

    def test_mes_unico_e_valido(self):
        valida_janela("2026-07", "2026-07")


# ---------------------------------------------------------------------------
# Janela incremental = watermark (ops.controle_runs) x fim DECLARADO da
# origem (mês do MAX(dsai_sai) da efsaidas — documento fiscal, sem datas
# futuras; ctlancto tem typos de milênio e recorrências, FOBASES tem
# adiantamento sistemático).
# ---------------------------------------------------------------------------

class TestMesDeclarado:
    def test_data_vira_mes(self):
        assert mes_declarado("2026-03-31", "2026-07") == "2026-03"

    def test_date_obj_vira_mes(self):
        from datetime import date as _d
        assert mes_declarado(_d(2026, 3, 31), "2026-07") == "2026-03"

    def test_typo_futuro_clampa_no_mes_de_hoje(self):
        # cinto: efsaidas hoje tem ZERO datas futuras, mas um typo (família
        # dos 8021 do ctlancto) não pode virar fim declarado
        assert mes_declarado("2027-09-05", "2026-07") == "2026-07"

    def test_origem_vazia_vira_none(self):
        assert mes_declarado(None, "2026-07") is None


class TestDecideJanelaV4:
    def test_origem_a_frente_roda_do_watermark_inclusivo(self):
        d = decide_janela_v4("2026-03", "2026-05")
        assert (d["acao"], d["inicio"], d["fim"]) == ("run", "2026-03", "2026-05")

    def test_origem_igual_roda_growth_intra_mes(self):
        # ratificado: snapshot re-atualizado dentro do mês é caso real
        d = decide_janela_v4("2026-03", "2026-03")
        assert (d["acao"], d["inicio"], d["fim"]) == ("run", "2026-03", "2026-03")

    def test_origem_atras_e_noop_com_mensagem_explicita(self):
        # nunca verde silencioso
        d = decide_janela_v4("2026-05", "2026-03")
        assert d["acao"] == "noop"
        assert "não tem períodos novos" in d["mensagem"]
        assert "2026-05" in d["mensagem"]

    def test_watermark_ausente_e_janela_completa_canonica(self):
        # 1º run / banco novo: full window desde o corte canônico
        d = decide_janela_v4(None, "2026-03")
        assert (d["acao"], d["inicio"], d["fim"]) == ("run", "2023-01", "2026-03")

    def test_origem_sem_documentos_e_noop(self):
        d = decide_janela_v4("2026-03", None)
        assert d["acao"] == "noop"


class TestWatermarkDoRun:
    def test_fim_alem_do_declarado_grava_o_declarado(self):
        # run com fim 2026-12 grava 2026-03, não dez/26
        assert watermark_do_run("2026-12", "2026-03") == "2026-03"

    def test_fim_aquem_do_declarado_grava_o_fim(self):
        assert watermark_do_run("2026-03", "2026-05") == "2026-03"

    def test_sem_fim_declarado_nao_avanca(self):
        # skip-extract não consulta origem -> watermark NULL (não avança)
        assert watermark_do_run("2026-03", None) is None


class TestResumoOrigemDeclarava:
    def test_resumo_imprime_fim_declarado(self):
        # auditoria da decisão de janela — "o que a origem
        # dizia quando rodou" fica respondida no registro
        r = format_run_report({"args": {"empresa": "all", "inicio": "2026-03",
                                        "fim": "2026-03", "modo": "completo"},
                               "fim_declarado": "2026-03"})
        assert "origem declarava até 2026-03" in r

    def test_resumo_sem_fim_declarado_omite_linha(self):
        r = format_run_report({"args": {"empresa": "all", "inicio": "2026-01",
                                        "fim": "2026-03", "modo": "skip-extract"}})
        assert "origem declarava" not in r


class TestAlertaGrfcParcial:
    """raw.foguiagrfc PARCIAL nao pode passar calado.

    Coluna de valor AUSENTE ja degrada pra fgts_resc NULL dentro do build, que
    e alto e visivel. O caso mudo e o raw parcial: um re-extract janelado CRIA
    a coluna, mas as linhas antigas ficam com os tres NULL e somam zero — o
    encargo rescisorio sai subestimado sem nada na tela.
    """

    def test_grfc_parcial_vira_alerta(self):
        r = format_run_report({"args": {"empresa": "all", "inicio": "2023-01",
                                        "fim": "2026-12", "modo": "completo"},
                               "grfc_parcial": 42})
        assert "raw.foguiagrfc PARCIAL" in r
        assert "42 linha(s)" in r
        assert "SUBESTIMADO" in r

    def test_sem_grfc_parcial_nao_alerta(self):
        r = format_run_report({"args": {"empresa": "all", "inicio": "2023-01",
                                        "fim": "2026-12", "modo": "completo"}})
        assert "foguiagrfc" not in r
        assert "sem alertas" in r

    def test_iss_retido_competencia_invalida_vira_alerta(self):
        # Mesmo canal dos alertas acima: build_iss_retido_sofrido descarta
        # competencia invalida com contagem EXPLICITA; esta e a renderizacao
        # dessa contagem no resumo.
        r = format_run_report({
            "args": {"empresa": "all", "inicio": "2023-01",
                     "fim": "2026-12", "modo": "completo"},
            "iss_retido_competencia_invalida": {
                "linhas": 3, "soma": 90.0, "por_empresa": {1: 90.0}},
        })
        assert "ISS retido sofrido" in r
        assert "3 nota(s)" in r
        assert "90.00" in r
        assert "{1: 90.0}" in r

    def test_sem_iss_retido_competencia_invalida_nao_alerta(self):
        r = format_run_report({"args": {"empresa": "all", "inicio": "2023-01",
                                        "fim": "2026-12", "modo": "completo"}})
        assert "ISS retido sofrido" not in r
        assert "sem alertas" in r

    def test_estorno_deducao_delta_aparece_no_resumo(self):
        # O destacado e liquido de estorno; o delta
        # (bruto - liquido) aparece no resumo do run, por empresa.
        r = format_run_report({
            "args": {"empresa": "all", "inicio": "2023-01",
                     "fim": "2026-12", "modo": "completo"},
            "estorno_deducao_delta": {
                "total": 472.33, "por_empresa": {204: 472.33}},
        })
        assert "estorno na conta de dedução" in r
        assert "472.33" in r
        assert "1 empresa(s)" in r

    def test_sem_estorno_deducao_delta_nao_aparece(self):
        r = format_run_report({"args": {"empresa": "all", "inicio": "2023-01",
                                        "fim": "2026-12", "modo": "completo"}})
        assert "estorno na conta de dedução" not in r

    def test_anos_suspeitos_ctlancto_aparece_no_resumo(self):
        # Alerta de sanidade, zero mudanca de numero.
        r = format_run_report({
            "args": {"empresa": "all", "inicio": "2023-01",
                     "fim": "2026-12", "modo": "completo"},
            "ctlancto_anos_suspeitos": {"linhas": 66, "anos": (2089, 8021)},
        })
        assert "ano fora de" in r
        assert "66 lancamento(s)" in r
        assert "2089" in r and "8021" in r

    def test_sem_anos_suspeitos_ctlancto_nao_alerta(self):
        r = format_run_report({"args": {"empresa": "all", "inicio": "2023-01",
                                        "fim": "2026-12", "modo": "completo"}})
        assert "ano fora de" not in r
        assert "sem alertas" in r


# ---------------------------------------------------------------------------
# Run que termina com "ALERTA mart FALHOU" (report["mart_errors"]) nao pode
# gravar status='ok' em ops.controle_runs: main.py:_le_watermark e o app
# filtram status='ok' pra decidir o watermark e o texto do sidebar, e um run
# com mart quebrado avancaria os dois calado. A regra vive so no status
# gravado no fecho do run.
# ---------------------------------------------------------------------------
class TestStatusFinalDoRun:
    def _report_mart_falhou(self):
        return {
            "args": {"empresa": "all", "inicio": "2026-01", "fim": "2026-01",
                     "modo": "completo"},
            "fim_declarado": "2026-01",
            "mart_errors": ["load impostos_mensais"],
        }

    def test_mart_falhou_vira_parcial(self):
        from main import _status_final
        assert _status_final(self._report_mart_falhou()) == "parcial"

    def test_sem_erro_continua_ok(self):
        from main import _status_final
        assert _status_final({}) == "ok"

    def test_rejeitado_tem_precedencia_sobre_mart_falhou(self):
        from main import _status_final
        r = self._report_mart_falhou()
        r["geracoes_rejeitadas"] = ["ctlancto"]
        assert _status_final(r) == "rejeitado"

    @pytest.mark.db
    def test_run_com_mart_falhou_por_contraste(self, banco_scratch):
        """Prova por CONTRASTE dentro do mesmo teste (sem stash/import
        error): fecha dois runs equivalentes no MESMO banco_scratch, um com o
        status que a expressao literal ANTIGA gravava e outro com
        `_status_final`. Se alguem
        quebrar a logica de `_status_final` (ex.: parar de checar mart_errors),
        a asserção (b) falha por VALOR ('ok' != 'parcial'/watermark avancado),
        nunca por ImportError.

        ORDEM (b) antes de (a), deliberada: `_le_watermark` agrega
        MAX(watermark) sobre TODAS as linhas com status='ok' de
        ops.controle_runs, sem filtrar por run_id. Se (a) rodasse primeiro, a
        linha 'ok' que ele grava ficaria no banco e (b) veria o watermark JA
        avancado por (a) -- nunca None. Fechar o run 'parcial' ANTES do run
        'ok' e a unica ordem em que as duas asserções valem no mesmo
        banco_scratch, sem precisar de um segundo banco ou de stash.
        """
        from datetime import datetime
        from main import _abre_run, _fecha_run, _le_watermark, _status_final
        report = self._report_mart_falhou()

        # (b) FIX: _status_final da 'parcial' pro mesmo report, e o watermark
        # (ainda sem nenhuma linha 'ok' no banco) fica parado.
        run_id_fix = _abre_run(report, datetime.now())
        status_fix = _status_final(report)
        assert status_fix == "parcial"
        _fecha_run(run_id_fix, report, status_fix)
        assert _le_watermark() is None, (
            "fix: watermark nao pode avancar com mart FALHOU")

        # (a) a expressao literal antiga -- pro mesmo report (rejeitado=False,
        # mart_errors presente) -- dava 'ok', e e exatamente esse 'ok' que
        # fazia o watermark avancar calado com o mart quebrado.
        rejeitado = bool(report.get("geracoes_rejeitadas") or report.get("fatias_rejeitadas"))
        status_antigo = "rejeitado" if rejeitado else "ok"
        assert status_antigo == "ok"
        run_id_bug = _abre_run(report, datetime.now())
        _fecha_run(run_id_bug, report, status_antigo)
        assert _le_watermark() == "2026-01", (
            "bug reproduzido: o status antigo ('ok') avanca o watermark "
            "mesmo com mart FALHOU")


class TestAlemDoDeclarado:
    # Texto explicativo da janela: quando a origem tem
    # dado PARCIAL além do mês declarado (fluxos contínuos — efservicos/
    # efentradas andam à frente da escrituração fiscal), o painel explica
    # por que a janela para no declarado.
    def test_spillover_alem_do_declarado_e_reportado(self):
        # fiscal até mar, serviços 07/abr, entradas 10/abr
        assert alem_do_declarado(
            ["2026-04-07", "2026-04-10"], "2026-03", "2026-07") == "2026-04"

    def test_nada_alem_do_declarado_vira_none(self):
        # setembro: tudo dentro do mês declarado
        assert alem_do_declarado(
            ["2026-08-15", "2026-08-20"], "2026-08", "2026-09") is None

    def test_typo_futuro_ignorado_pelo_cinto(self):
        # família dos lançamentos 8021: mês além de hoje não vira "parcial"
        assert alem_do_declarado(
            ["2027-09-05"], "2026-03", "2026-07") is None

    def test_none_e_lista_vazia_viram_none(self):
        assert alem_do_declarado([None], "2026-03", "2026-07") is None
        assert alem_do_declarado([], "2026-03", "2026-07") is None


class TestDataAlemDoDeclarado:
    # par do alem_do_declarado: a DATA exata do documento mais avançado além
    # do declarado — alimenta o aviso "se quiser incluir os dados que já
    # temos até DD/MM/AAAA, rode com Período customizado".
    def test_data_exata_do_spillover(self):
        assert data_alem_do_declarado(
            ["2026-04-07", "2026-04-10"], "2026-03", "2026-07") == "2026-04-10"

    def test_nada_alem_vira_none(self):
        assert data_alem_do_declarado(
            ["2026-08-20"], "2026-08", "2026-09") is None

    def test_typo_futuro_ignorado(self):
        assert data_alem_do_declarado(
            ["2027-09-05"], "2026-03", "2026-07") is None


class TestAdaptividadeDoAvisoPorPeriodo:
    # A mensagem se adapta ao período —
    # em setembro, fiscal até ago + spillover até 05/set mostraria
    # "dados de Set/26 parciais ... rodando até Ago/26 ... até 05/09/2026".
    # Tudo função pura de (maxes da origem, fim declarado, mês de hoje).
    def test_cenario_setembro_mostra_agosto_e_setembro(self):
        spill = ["2026-09-05"]
        assert mes_declarado("2026-08-20", "2026-09") == "2026-08"
        assert alem_do_declarado(spill, "2026-08", "2026-09") == "2026-09"
        assert data_alem_do_declarado(spill, "2026-08", "2026-09") == "2026-09-05"

    def test_cenario_dezembro_virada_de_ano(self):
        # borda lexicográfica da virada: fiscal dez/26, spillover jan/27
        spill = ["2027-01-08"]
        assert alem_do_declarado(spill, "2026-12", "2027-01") == "2027-01"
        assert data_alem_do_declarado(spill, "2026-12", "2027-01") == "2027-01-08"


class TestFolhaRubricasWiring:
    """Folha por rubrica: 3 tabelas raw + 1 mart."""

    def test_raw_keys_fomovto_inclui_i_calculos(self):
        # A origem NAO declara indice unico na FOMOVTO: sem i_calculos o
        # dedup do load derruba linhas reais.
        assert RAW_KEYS["fomovto"] == ["codi_emp", "i_empregados", "data",
                                       "tipo_proces", "i_eventos", "i_calculos"]

    def test_raw_keys_dos_dicionarios(self):
        assert RAW_KEYS["foeventos"] == ["codi_emp", "i_eventos"]
        # dicionario GLOBAL, sem empresa na chave
        assert RAW_KEYS["foclassificacoes_eventos"] == ["i_classificacao"]

    def test_extract_jobs_registrados(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "etl" / "main.py").read_text(
            encoding="utf-8")
        for nome in ("fomovto", "foeventos", "foclassificacoes_eventos"):
            assert f'("{nome}",' in src, f"job de extract ausente: {nome}"

    def test_mart_keys_grao_por_rubrica(self):
        # grao (empresa, mes, tipo_process, rubrica) — SEM empregado (LGPD)
        assert MART_KEYS["folha_rubricas_mensais"] == [
            "codi_emp", "competencia", "tipo_process", "i_eventos"]
        assert "i_empregados" not in MART_KEYS["folha_rubricas_mensais"]

    def test_transform_job_registrado(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent / "etl" / "main.py").read_text(
            encoding="utf-8")
        assert "build_folha_rubricas_mensais" in src

    def test_indice_emp_competencia_registrado(self):
        import inspect

        import load
        assert "folha_rubricas_mensais" in inspect.getsource(load.ensure_schemas)

    def test_run_transform_produz_o_mart(self):
        raw = {
            "fomovto": pd.DataFrame({
                "codi_emp": [201], "i_empregados": [7], "data": ["2025-09-01"],
                "tipo_proces": [11], "i_eventos": [1], "valor_inf": [220.0],
                "valor_cal": [1000.0], "prov_desc": ["P"], "i_calculos": ["g1"]}),
            "foeventos": pd.DataFrame({
                "codi_emp": [201], "i_eventos": [1], "nome": ["SALARIO BASE"],
                "prov_desc": ["P"], "classificacao": [1]}),
            "foclassificacoes_eventos": pd.DataFrame({
                "i_classificacao": [1], "descricao": ["Salarios"]}),
        }
        marts = run_transform(raw, "2025-09", "2025-09")
        m = marts["folha_rubricas_mensais"]
        assert len(m) == 1
        assert float(m.iloc[0]["valor_p"]) == 1000.0
        assert m.iloc[0]["desc_classificacao"] == "Salarios"
        assert "i_empregados" not in m.columns


class TestMartKeysBalancete:
    def test_mart_keys_balancete_tem_codi_cta(self):
        # A identidade da conta é codi_cta — clas_cta é atributo de
        # classificação (colide entre contas distintas) e nome_cta é rótulo.
        from main import MART_KEYS
        assert "codi_cta" in MART_KEYS["balancete_mensal"]


class TestDreMensalWiring:
    # mart.dre_mensal plugado no pipeline (MART_KEYS + job + indice).
    def test_mart_keys_grao_colunas(self):
        # grao (codi_emp, competencia) -> 1 linha/emp/mes; DELETE-escopado
        # (competencia in keys) sem risco de "menos linhas".
        assert MART_KEYS["dre_mensal"] == ["codi_emp", "competencia"]

    def test_indice_emp_competencia_registrado(self):
        import inspect

        import load
        assert "dre_mensal" in inspect.getsource(load.ensure_schemas)

    def test_run_transform_produz_dre_mensal(self):
        raw = {
            "ctcontas": pd.DataFrame({
                "codi_emp": [1, 1, 1],
                "codi_cta": [10, 224, 242],
                "clas_cta": ["1010101", "301010101", "401010101"],
                "nome_cta": ["Caixa", "Receita", "CMV"],
                "grdre_cta": [None, 1, 4],
            }),
            "ctlancto": pd.DataFrame({
                "codi_emp": [1, 1],
                "data_lan": ["2025-01-10", "2025-01-11"],
                "cdeb_lan": [10, 242],
                "ccre_lan": [224, 10],
                "vlor_lan": [1000.0, 600.0],
                "orig_lan": [1, 1],
            }),
        }
        marts = run_transform(raw, "2025-01", "2025-01")
        assert "dre_mensal" in marts
        dre = marts["dre_mensal"]
        assert list(dre.columns) == DRE_COLS
        row = dre[dre["competencia"] == "2025-01"].iloc[0]
        assert row["receita_bruta"] == pytest.approx(1000.0)
        assert row["resultado_liquido"] == pytest.approx(400.0)  # 1000 - 600


# ── (f) --skip-extract vence --saldo-inicial em silencio ────────────────────

class TestSkipExtractNaoAceitaSaldoInicial:
    """A combinacao promete re-extrair o si e NAO re-extrai.

    A precedencia nao esta escrita em
    lugar nenhum: ela EMERGE de onde a flag e lida — `args.saldo_inicial`
    aparece UMA vez, dentro do ramo `else` do skip, e `run_read_raw` nem
    tem o parametro. Aceitar a flag e pior que recusar, porque quem a
    passou fica achando que o saldo inicial foi renovado.
    """

    @staticmethod
    def _args(**kw):
        base = dict(skip_extract=True, saldo_inicial="always")
        base.update(kw)
        return SimpleNamespace(**base)

    def test_always_com_skip_extract_sai_2(self):
        with pytest.raises(SystemExit) as e:
            valida_flags(self._args())
        assert e.value.code == 2

    def test_never_com_skip_extract_tambem_sai_2(self):
        # `never` e igualmente inerte no skip: quem digitou espera efeito.
        with pytest.raises(SystemExit) as e:
            valida_flags(self._args(saldo_inicial="never"))
        assert e.value.code == 2

    def test_auto_default_passa(self):
        # Isento de proposito: `make etl`, os scripts e o botao do painel
        # nunca passam a flag, e o default nao pode virar erro.
        valida_flags(self._args(saldo_inicial="auto"))

    def test_sem_skip_extract_qualquer_valor_passa(self):
        valida_flags(self._args(skip_extract=False, saldo_inicial="always"))
        valida_flags(self._args(skip_extract=False, saldo_inicial="never"))


# ── (e) si 100% descartado com lancamento na janela FALHA ALTO ─────────────

class TestSiTotalmenteDescartadoFalhaAlto:
    """O run que le o raw envenenado escrevia zero-base e saia 0.

    Cenario: o si inteiro em corte
    2026-01-01 e um `--skip-extract --inicio 2023-01` por cima. O descarte
    por corte ja avisava (`si_zumbi_emps` no resumo), mas o run seguia,
    gravava `mart.balancete_mensal` com cumsum de zero na carteira inteira
    e saia com codigo 0 — verde na tela, carteira destruida no mart.
    """

    @staticmethod
    def _raw(cortes):
        return {
            "ctsaldo_inicial": pd.DataFrame({
                "codi_emp": [1] * len(cortes),
                "codi_cta": list(range(10, 10 + len(cortes))),
                "valo_deb": [100.0] * len(cortes),
                "valo_cre": [0.0] * len(cortes),
                "data_corte": list(cortes)}),
            "ctlancto": pd.DataFrame({
                "codi_emp": [1], "nume_lan": [1],
                "data_lan": ["2023-05-10"], "cdeb_lan": [10],
                "ccre_lan": [11], "vlor_lan": [50.0], "orig_lan": [1]}),
            "ctcontas": pd.DataFrame({
                "codi_emp": [1, 1], "codi_cta": [10, 11],
                "clas_cta": ["1010101", "2010101"],
                "nome_cta": ["Caixa", "Fornecedores"]}),
        }

    def test_si_todo_stale_com_lancto_na_janela_levanta(self):
        with pytest.raises(SaldoInicialTotalmenteDescartado):
            run_transform(self._raw(["2026-01-01", "2026-01-01"]),
                          "2023-01", "2023-12")

    def test_descarte_PARCIAL_segue_avisando_sem_levantar(self):
        # Descarte parcial NAO pode virar exit != 0: o run canonico precisa
        # seguir rodando pra CURAR.
        out = run_transform(self._raw(["2023-01-01", "2026-01-01"]),
                            "2023-01", "2023-12")
        assert isinstance(out, dict)

    def test_si_todo_stale_SEM_lancto_na_janela_nao_levanta(self):
        # Empresa dormente: sem movimento, nao ha balancete a destruir.
        raw = self._raw(["2026-01-01"])
        raw["ctlancto"] = raw["ctlancto"].iloc[0:0]
        out = run_transform(raw, "2023-01", "2023-12")
        assert isinstance(out, dict)

    def test_si_vazio_nao_levanta(self):
        # Sem si NENHUM no raw nao ha DESCARTE — e outro aviso, que ja
        # existe ("balancete sera construido SEM saldo inicial").
        raw = self._raw(["2023-01-01"])
        raw["ctsaldo_inicial"] = raw["ctsaldo_inicial"].iloc[0:0]
        out = run_transform(raw, "2023-01", "2023-12")
        assert isinstance(out, dict)


class _CurFake:
    def __init__(self, sqls, tabelas):
        self.sqls, self.tabelas, self._ult = sqls, tabelas, ""
        self.description = [("codi_emp",)]

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        self._ult = sql

    def fetchall(self):
        return [(t,) for t in self.tabelas] if "information_schema" in self._ult else []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _ConnFake:
    def __init__(self, sqls, tabelas):
        self.sqls, self.tabelas = sqls, tabelas

    def set_session(self, **kw):
        self.sessao = kw

    def cursor(self):
        return _CurFake(self.sqls, self.tabelas)

    def close(self):
        pass


class TestRunReadRawVigente:
    def _roda(self, monkeypatch, empresa):
        import main
        sqls = []
        conn = _ConnFake(sqls, list(main.RAW_KEYS))
        monkeypatch.setattr(main, "connect_postgres", lambda: conn)
        run_read_raw(empresa, report={})
        return sqls, conn

    def test_dicionario_global_nao_leva_filtro_de_empresa(self, monkeypatch):
        # Com filtro, o skip-extract por empresa perdia o dicionario.
        sqls, _ = self._roda(monkeypatch, 925)
        glob = [s for s in sqls if '"foeventosdef"' in s or '"foclassificacoes_eventos"' in s]
        assert glob and all("codi_emp" not in s for s in glob)
        assert any('"ctlancto"' in s and '"codi_emp" = 925' in s for s in sqls)

    def test_le_um_instante_so(self, monkeypatch):
        _, conn = self._roda(monkeypatch, None)
        assert conn.sessao == {"isolation_level": "REPEATABLE READ", "readonly": True}


class TestFlagsEncolhimento:
    @staticmethod
    def _args(**kw):
        base = dict(skip_extract=False, saldo_inicial="auto", aceitar_encolhimento=[], motivo=None)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_aceitar_sem_motivo_sai_2(self):
        with pytest.raises(SystemExit) as e:
            valida_flags(self._args(aceitar_encolhimento=["efsdoimp"]))
        assert e.value.code == 2

    def test_aceitar_com_skip_extract_sai_2(self):
        with pytest.raises(SystemExit) as e:
            valida_flags(self._args(skip_extract=True, aceitar_encolhimento=["efsdoimp"], motivo="x"))
        assert e.value.code == 2

    def test_aceitar_com_motivo_passa(self):
        valida_flags(self._args(aceitar_encolhimento=["efsdoimp:207"], motivo="guias recalculadas"))


class TestResumoGeracao:
    def test_geracao_rejeitada_vira_alerta(self):
        r = format_run_report({"args": {}, "geracoes_rejeitadas": ["raw.efsdoimp: pedido perdeu 100.0% (3 -> 0)"],
                               "fatias_rejeitadas": ["raw.ctlancto: 2 fatia(s), ex. [[265, 202606]]"]})
        assert "ALERTA geracao REJEITADA: raw.efsdoimp" in r
        assert "ALERTA encolhimento: raw.ctlancto" in r
        assert "sem alertas" not in r


class TestClampaMarts:
    def test_corta_fora_da_janela_e_preserva_mart_sem_competencia(self):
        import main
        marts = {
            "faturamento_mensal": pd.DataFrame({"codi_emp": [1, 1, 1],
                                                "competencia": ["2026-06", "2026-08", "2026-09"]}),
            "empresas": pd.DataFrame({"codi_emp": [1, 2]}),
        }
        out = main._clampa_marts(marts, "2026-08", "2026-08")
        assert out["faturamento_mensal"]["competencia"].tolist() == ["2026-08"]
        assert len(out["empresas"]) == 2
