# ---------------------------------------------------------------------------
# Testes de caracterização de pdf_generator.py.
# Só funções puras — nada de Postgres/kaleido/playwright nos testes.
# ---------------------------------------------------------------------------
import pathlib
import math

import pandas as pd
import pytest

from etl.metrics import (
    LINHA_PCT_IMPOSTOS as metrics_LINHA_PCT,
    LINHA_FOLHA_ENCARGOS, LINHA_PCT_FAT,
    RUBRICAS_DESTAQUE_FOLHA, TITULO_DESTAQUES_FOLHA, SUBTITULO_DESTAQUES_FOLHA,
)
from pdf_generator import _passa_filtro, _safe_filename


# ── _safe_filename (âncora: path traversal no batch de PDFs) ────────────────

class TestSafeFilename:
    def test_normaliza_acentos_e_pontuacao(self):
        assert _safe_filename("Empresa Çãé Ltda.") == "Empresa_Cae_Ltda"

    def test_bloqueia_path_traversal(self):
        out = _safe_filename("../../etc/passwd")
        assert ".." not in out and "/" not in out
        assert out == "etcpasswd"

    def test_vazio_vira_placeholder(self):
        assert _safe_filename("") == "empresa"
        assert _safe_filename(None) == "empresa"

    def test_trunca_a_80_chars(self):
        assert len(_safe_filename("A" * 200)) == 80


# ── _passa_filtro (filtros do batch de PDFs) ────────────────────────────────

class TestPassaFiltro:
    COV_SO_FAT = {"tem_fat": True, "tem_imp": False, "tem_func": False,
                  "tem_folha": False, "tem_bal": False}
    COV_NADA = {"tem_fat": False, "tem_imp": False, "tem_func": False,
                "tem_folha": False, "tem_bal": False}

    def test_fat_or_bal(self):
        assert _passa_filtro(self.COV_SO_FAT, 1, "fat_or_bal") is True
        assert _passa_filtro(self.COV_NADA, 0, "fat_or_bal") is False

    def test_complete_exige_4_marts(self):
        assert _passa_filtro(self.COV_SO_FAT, 4, "complete") is True
        assert _passa_filtro(self.COV_SO_FAT, 3, "complete") is False

    def test_all_aceita_tudo(self):
        assert _passa_filtro(self.COV_NADA, 0, "all") is True


# ── card "% Folha + Encargos" fiel a GUIA ──────

class TestCardFolhaEncargosGuia:
    """O card hero fiscal vira '% Folha + Encargos' com numerador de
    encargos vindo do mart.encargos_mensais (GUIA mensal).
    O estoque de passivo 2xx do balancete NUNCA entra no numerador;
    sem guia -> 'encargos: —'."""

    def _base(self, monkeypatch):
        import pdf_generator
        # evita kaleido nos charts/sparks — o card nao depende do render real
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda *a, **k: "")
        periods = ["2025-01", "2025-02", "2025-03"]
        fat = pd.DataFrame({"competencia": periods,
                            "valo_saidas": [1000.0, 1000.0, 1000.0],
                            "valo_servicos": [0.0, 0.0, 0.0]})
        folha = pd.DataFrame({"competencia": periods,
                              "valo_bruto": [100.0, 100.0, 100.0],
                              "tipo_process": [11, 11, 11]})
        # PASSIVO 2xx "INSS a Recolher" — estoque acumulado, NAO e encargo do mes
        bal = pd.DataFrame({"competencia": periods,
                            "clas_cta": ["201010401", "201010401", "201010401"],
                            "nome_cta": ["INSS a Recolher"] * 3,
                            "valo_saldo": [5000.0, 6000.0, 7000.0]})
        return pdf_generator, periods, fat, folha, bal

    def test_sem_guia_card_e_folha_only_com_travessao(self, monkeypatch):
        pdf_generator, periods, fat, folha, bal = self._base(monkeypatch)
        empty = pd.DataFrame()
        spark = {"fat": fat, "imp": empty, "folha": folha, "func": empty,
                 "bal": bal, "encargos": empty}
        ctx = pdf_generator._build_context(
            fat, empty, folha, empty, bal, spark,
            periods, periods, "Teste", "00", "2025-01", "2025-03",
            bal_serie_df=bal, encargos_df=empty)
        card = next(c for c in ctx["hero_fiscal"] if c["key"] == "folenc")
        assert card["label"] == "% Folha + Encargos"
        # 300/3000 = 10,0% — estoque 2xx (18.000 acumulado) NUNCA no numerador
        assert card["value"] == "10,0%"
        # A sub-linha e SO o fragmento YoY. O breakdown em R$ e a
        # regua do universo ("inclui 13o e autonomos") viviam aqui e morreram
        # — o detalhe vive na tabela da propria pagina de folha.
        assert "encargos: —" not in card["context"]
        assert "de folha" not in card["context"]
        assert "inclui 13º e autônomos (RPA)" not in card["context"]

    def test_com_guias_soma_numerador_e_decompoe_contexto(self, monkeypatch):
        pdf_generator, periods, fat, folha, bal = self._base(monkeypatch)
        empty = pd.DataFrame()
        enc = pd.DataFrame({
            "codi_emp":     [1, 1, 1],
            "competencia":  periods,
            "inss_empresa": [10.0, 10.0, 10.0],
            "fgts":         [5.0, 5.0, 5.0],
            "fgts_resc":    [float("nan")] * 3,
        })
        spark = {"fat": fat, "imp": empty, "folha": folha, "func": empty,
                 "bal": bal, "encargos": enc}
        ctx = pdf_generator._build_context(
            fat, empty, folha, empty, bal, spark,
            periods, periods, "Teste", "00", "2025-01", "2025-03",
            bal_serie_df=bal, encargos_df=enc)
        card = next(c for c in ctx["hero_fiscal"] if c["key"] == "folenc")
        assert card["label"] == "% Folha + Encargos"
        # (300 + 45)/3000 = 11,5%
        assert card["value"] == "11,5%"
        # O VALOR do card segue sendo o mesmo; o que saiu foi a
        # decomposicao textual embaixo dele.
        assert "de encargos" not in card["context"]
        assert "45,00" not in card["context"]
        assert "inclui 13º e autônomos (RPA)" not in card["context"]


# ── Lucratividade contábil (DRE) + supressão Simples ────────────────

class TestLucratividadeContabilPDF:
    """A lucratividade do PDF vem do mart.dre_mensal (resultado liquido /
    receita contabil), NAO do proxy NF. Supressao SO por dado (DESPESA da DRE);
    a supressao por regime (Simples) morreu."""

    def _ctx(self, monkeypatch, dre=None, regime_df=None):
        import pdf_generator
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda *a, **k: "")
        periods = ["2025-01", "2025-02", "2025-03"]
        fat = pd.DataFrame({"competencia": periods,
                            "valo_saidas": [1000.0] * 3, "valo_servicos": [0.0] * 3})
        bal = pd.DataFrame({"competencia": periods,
                            "clas_cta": ["3010101"] * 3, "nome_cta": ["Receita"] * 3,
                            "valo_saldo": [1200.0] * 3})
        impd = pd.DataFrame({"competencia": periods,
                             "valo_impostos": [100.0] * 3, "base_receita": [1000.0] * 3})
        empty = pd.DataFrame()
        spark = {"fat": fat, "imp": impd, "folha": empty, "func": empty, "bal": bal}
        kw = {"dre_df": dre if dre is not None else pd.DataFrame()}
        if regime_df is not None:
            kw["regime_df"] = regime_df
        return pdf_generator._build_context(
            fat, impd, empty, empty, bal, spark,
            periods, periods, "Teste", "00", "2025-01", "2025-03",
            bal_serie_df=bal, **kw)

    def _dre(self, receita, despesas, liquido):
        periods = ["2025-01", "2025-02", "2025-03"]
        return pd.DataFrame({
            "codi_emp": [1] * 3, "competencia": periods,
            "receita_bruta": [receita] * 3, "despesas": [despesas] * 3,
            # provisao zero -> lair == liquido (o card usa o LAIR)
            "lair": [liquido] * 3, "provisao_irpj_csll": [0.0] * 3,
            "resultado_liquido": [liquido] * 3,
        })

    def _card(self, ctx):
        return next(c for c in ctx["hero_contabil"] if c["label"] == "Lucratividade Média")

    def test_card_liquido_sobre_receita_contabil(self, monkeypatch):
        # DRE: receita 1000, resultado liquido 200 /mes -> 200/1000 = 20,0%
        # (contabil, NAO o proxy NF).
        ctx = self._ctx(monkeypatch, dre=self._dre(1000.0, 800.0, 200.0))
        assert self._card(ctx)["value"] == "20,0%"

    def test_prejuizo_contabil_mostra_negativo(self, monkeypatch):
        # prejuizo contabil -> card NEGATIVO (nao lucro).
        ctx = self._ctx(monkeypatch, dre=self._dre(1000.0, 1100.0, -100.0))
        val = self._card(ctx)["value"]
        assert "10,0%" in val and val[0] in "-−"   # -100*3 / 1000*3 = -10,0%

    def test_simples_com_escrituracao_mostra_numero(self, monkeypatch):
        # Simples NAO e mais suprimido por regime — com
        # despesa escriturada na DRE, mostra a lucratividade real (20,0%).
        regime = pd.DataFrame({"codi_emp": [1, 1, 1],
                               "competencia": ["2025-01", "2025-02", "2025-03"],
                               "regime": ["simples", "simples", "simples"]})
        card = self._card(self._ctx(monkeypatch, dre=self._dre(1000.0, 800.0, 200.0), regime_df=regime))
        assert card["value"] == "20,0%"

    def test_sem_despesa_na_dre_suprime_com_rotulo(self, monkeypatch):
        # nao-Simples so-receita (despesas 0): trigger dispara -> card "—" e o
        # rotulo unificado, sem mencionar regime.
        from etl.metrics import LUC_SEM_REGISTROS
        ctx = self._ctx(monkeypatch, dre=self._dre(1000.0, 0.0, 1000.0))
        assert self._card(ctx)["value"] == "—"
        assert ctx["luc_empty_msg"] == LUC_SEM_REGISTROS
        assert "Simples" not in ctx["luc_empty_msg"]

    def test_sem_dre_primeiro_deploy_degrada(self, monkeypatch):
        # 1o deploy (mart ausente -> dre_df vazio): card "—" com rotulo (transitorio).
        from etl.metrics import LUC_SEM_REGISTROS
        ctx = self._ctx(monkeypatch, dre=pd.DataFrame())
        assert self._card(ctx)["value"] == "—"
        assert ctx["luc_empty_msg"] == LUC_SEM_REGISTROS

    def test_receita_incompleta_fallback_generico(self, monkeypatch):
        # Guard de cobertura: receita contabil ~1% do fat -> card "—"
        # + fallback GENERICO no chart (nunca o rotulo de despesas — o que
        # falta e a receita).
        ctx = self._ctx(monkeypatch, dre=self._dre(10.0, 200.0, -190.0))
        assert self._card(ctx)["value"] == "—"
        assert ctx["luc_empty_msg"] == "⚠️ Sem dados para calcular lucratividade"

    def test_fallback_generico_quando_causa_nao_e_despesa(self, monkeypatch):
        # despesa presente mas receita 0 (nao e supressao por despesa): pct_luc
        # NaN por denominador 0 -> luc_empty_msg cai no fallback generico, NAO
        # no rotulo de "sem registros de despesas".
        ctx = self._ctx(monkeypatch, dre=self._dre(0.0, 200.0, -200.0))
        assert self._card(ctx)["value"] == "—"
        assert ctx["luc_empty_msg"] == "⚠️ Sem dados para calcular lucratividade"


class TestBlocosParidadeContabil:
    """Paridade Excel modelo: pagina contabil ganha o chart de Caixa
    (irmao do Bancos), o chart Despesas e Custos (401-405, SEM IRPJ/CSLL do
    406) e o bloco de valores do balanco
    (5 cards kpi + nota muted com as formulas dos indicadores)."""

    def _ctx(self, monkeypatch, com_bal=True):
        import pdf_generator
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda *a, **k: "B64")
        periods = ["2025-01", "2025-02"]
        fat = pd.DataFrame({"competencia": periods,
                            "valo_saidas": [1000.0, 1000.0],
                            "valo_servicos": [0.0, 0.0]})
        empty = pd.DataFrame()
        if com_bal:
            bal = pd.DataFrame({
                "codi_emp": [1] * 8,
                "competencia": ["2025-01"] * 7 + ["2025-02"],
                "clas_cta": ["1010101001", "1010301001", "1020101001",
                             "2010101001", "2020101001", "4010101001",
                             "4060101001", "4010101001"],
                "nome_cta": ["Caixa", "Estoque", "Imobilizado", "Fornecedores",
                             "Financiamento LP", "Despesa", "IRPJ", "Despesa"],
                "valo_saldo": [500.0, 300.0, 200.0, 400.0,
                               100.0, 50.0, 30.0, 60.0],
            })
        else:
            bal = empty
        spark = {"fat": fat, "imp": empty, "folha": empty, "func": empty, "bal": bal}
        return pdf_generator._build_context(
            fat, empty, empty, empty, bal, spark,
            periods, periods, "Teste", "00", "2025-01", "2025-02",
            bal_serie_df=bal)

    def test_charts_caixa_e_despesas_presentes(self, monkeypatch):
        ctx = self._ctx(monkeypatch)
        assert ctx["charts"]["caixa"] == "B64"
        assert ctx["charts"]["despesas"] == "B64"

    def test_fat_gastos_so_tem_a_versao_flat_da_contabil(self, monkeypatch):
        # O render DUPLO existia porque as duas paginas usavam o combinado e o
        # aspecto raso da contabil nao podia vazar pra fiscal.
        # Depois o combinado morreu no fiscal, que passou a exibir
        # Faturamento SOZINHO em faixa full-width: sobrou UM consumidor, entao
        # o render 2:1 morreu junto. Nao ha mais duplicacao pra proteger — o
        # teste vigia o inverso.
        ctx = self._ctx(monkeypatch)
        assert ctx["charts"]["fat_gastos_flat"] == "B64"
        assert "fat_gastos" not in ctx["charts"]
        assert ctx["charts"]["fat"] == "B64"      # o novo, so do fiscal

    def test_sem_balancete_charts_suprimidos(self, monkeypatch):
        ctx = self._ctx(monkeypatch, com_bal=False)
        assert ctx["charts"]["caixa"] == ""
        assert ctx["charts"]["despesas"] == ""

    def test_balanco_cards_valores_do_ultimo_mes(self, monkeypatch):
        ctx = self._ctx(monkeypatch)
        cards = ctx["balanco_cards"]
        assert [c["label"] for c in cards] == [
            "Ativo Circulante", "Ativo Total", "Estoque",
            "Exigível LP", "Passivo Circulante"]
        vals = {c["label"]: c["value"] for c in cards}
        # ffill: fev carrega o saldo de jan (patrimonial). AC = caixa 500 +
        # estoque 300 = 800; AT = AC + imobilizado 200 = 1.000.
        assert "800,00" in vals["Ativo Circulante"]
        assert "1.000,00" in vals["Ativo Total"]
        assert "300,00" in vals["Estoque"]
        assert "100,00" in vals["Exigível LP"]
        assert "400,00" in vals["Passivo Circulante"]
        assert all(c["key"] == "neutro" for c in cards)

    def test_sem_balancete_cards_travessao(self, monkeypatch):
        ctx = self._ctx(monkeypatch, com_bal=False)
        assert all(c["value"] == "—" for c in ctx["balanco_cards"])

    def test_balanco_note_tem_formulas(self, monkeypatch):
        # "/" no lugar de "÷" — a extracao de texto de
        # PDF corrompia o "÷" em "+" ("LC = AC + PC"); "/" sobrevive a
        # qualquer extracao e le igual.
        note = self._ctx(monkeypatch)["balanco_note"]
        assert note.startswith("Valores do balancete")
        assert "LC = Ativo Circulante / Passivo Circulante" in note
        assert "LS = (Ativo Circulante − Estoque) / Passivo Circulante" in note
        assert "Endividamento = (Passivo Circulante + Exigível LP) / Ativo Total" in note
        assert "÷" not in note


class TestPolishLayout:
    def _ctx(self, monkeypatch, func=None, cnpj="", dre=None):
        import pdf_generator
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda *a, **k: "")
        periods = ["2025-01", "2025-02"]
        n = len(periods)
        fat = pd.DataFrame({"competencia": periods,
                            "valo_saidas": [1000.0] * n, "valo_servicos": [0.0] * n})
        empty = pd.DataFrame()
        func_df = func if func is not None else empty
        spark = {"fat": fat, "imp": empty, "folha": empty, "func": func_df,
                 "bal": empty}
        kw = {"bal_serie_df": empty}
        if dre is not None:
            kw["dre_df"] = dre
        return pdf_generator._build_context(
            fat, empty, empty, func_df, empty, spark,
            periods, periods, "Teste", cnpj, "2025-01", "2025-02", **kw)

    def _func(self, partial_flags):
        periods = ["2025-01", "2025-02"]
        return pd.DataFrame({"competencia": periods,
                             "codi_emp": [1, 1],
                             "qtd_inicial": [5, 5], "qtd_admissoes": [0, 0],
                             "qtd_rescisoes": [0, 0], "qtd_final": [5, 5],
                             "is_partial": partial_flags})

    # item 1 — separador decimal pt-BR no fragmento "último mês"
    def test_ultimo_mes_usa_virgula(self, monkeypatch):
        periods = ["2025-01", "2025-02"]
        dre = pd.DataFrame({"codi_emp": [1, 1], "competencia": periods,
                            "receita_bruta": [1000.0, 1000.0],
                            "despesas": [700.0, 700.0],
                            "lair": [123.4, 123.4],
                            "provisao_irpj_csll": [0.0, 0.0],
                            "resultado_liquido": [123.4, 123.4]})
        ctx = self._ctx(monkeypatch, dre=dre)
        luc = next(c for c in ctx["hero_contabil"]
                   if c["label"] == "Lucratividade Média")
        assert "último mês: +12,3%" in luc["context"]
        assert "12.3" not in luc["context"]

    # item 2 — nota do (*) condicional a existencia de mes parcial
    def test_flag_func_tem_parcial_false_sem_parcial(self, monkeypatch):
        ctx = self._ctx(monkeypatch, func=self._func([False, False]))
        assert ctx["func_tem_parcial"] is False

    def test_flag_func_tem_parcial_true_com_parcial(self, monkeypatch):
        ctx = self._ctx(monkeypatch, func=self._func([False, True]))
        assert ctx["func_tem_parcial"] is True

    def test_template_fiscal_condiciona_nota_do_asterisco(self):
        src = open("templates/fiscal.html", encoding="utf-8").read()
        assert "{% if func_tem_parcial %}" in src

    # item 4 — header de grupo sem "CNPJ: CNPJ raiz"
    def test_cnpj_label_grupo_sem_prefixo_duplicado(self, monkeypatch):
        ctx = self._ctx(monkeypatch, cnpj="CNPJ raiz 91634207 (4 filiais)")
        assert ctx["cnpj_label"] == "CNPJ raiz: 91634207 (4 filiais)"

    def test_cnpj_label_singleton_prefixo_normal(self, monkeypatch):
        ctx = self._ctx(monkeypatch, cnpj="98110021000164")
        assert ctx["cnpj_label"] == "CNPJ: 98110021000164"

    # item 5 — legenda do Faturamento x Gastos + definicao de Gastos
    def test_grouped_bar_mantem_legenda_apos_layout(self):
        import pdf_generator
        fig = pdf_generator._make_grouped_bar(
            ["Jan"], [("Faturamento", [1.0], "#123456"),
                      ("Compras", [2.0], "#654321")])
        assert fig.layout.showlegend is True

    def test_fat_gastos_note_define_composicao(self, monkeypatch):
        ctx = self._ctx(monkeypatch)
        assert "notas de entrada" in ctx["fat_gastos_note"]
        # A devolucao de venda reduz o Faturamento E
        # segue dentro de Gastos no mesmo chart -- a nota declara os dois.
        assert "descontadas do Faturamento" in ctx["fat_gastos_note"]
        # O combinado morreu no fiscal (la o Faturamento vai sozinho e
        # Gastos nao aparece), entao a nota so tem sentido na contabil — a
        # unica pagina onde a serie de Gastos ainda vive.
        src = open("templates/contabil.html", encoding="utf-8").read()
        assert "note=fat_gastos_note" in src
        fiscal = open("templates/fiscal.html", encoding="utf-8").read()
        assert "fat_gastos" not in fiscal

    # item 7 — cap de escala [-100, +100] na lucratividade
    def test_luc_bar_clipa_e_anota_valor_real(self):
        import pdf_generator
        fig = pdf_generator._make_luc_bar(["Out", "Nov"], [50.0, -330.4])
        ys = list(fig.data[0].y)
        assert ys[0] == 50.0
        assert ys[1] == -100.0                       # barra clipada no cap
        anns = [a.text for a in fig.layout.annotations]
        assert "-330%" in anns                       # valor real vira rotulo
        assert not any("50" in a for a in anns)      # nao-clipado sem rotulo

    def test_luc_bar_sem_clip_sem_anotacao(self):
        import pdf_generator
        fig = pdf_generator._make_luc_bar(["Jan"], [42.0])
        assert list(fig.data[0].y) == [42.0]
        assert len(fig.layout.annotations) == 0

    def test_luc_bar_nan_sobrevive(self):
        import pdf_generator
        fig = pdf_generator._make_luc_bar(["Jan", "Fev"], [float("nan"), 120.0])
        ys = list(fig.data[0].y)
        assert math.isnan(ys[0])
        assert ys[1] == 100.0
        assert "120%" in [a.text for a in fig.layout.annotations]


class TestImpCtxCoerencia:
    """Coerencia do card % Impostos: rotulo UNICO nos 3 regimes
    ("de impostos sobre vendas" — 'destacados em nota'/'em guia' morreram
    como termo primario), frase-regua do IRPJ/CSLL, e o % divide pelo fat EXIBIDO (o
    par R$/% fecha na calculadora do leitor)."""

    def _card(self, fonte, monkeypatch, com_fat=True, key="imp", regime=None,
              provisao=0.0):
        import pdf_generator
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda *a, **k: "")
        periods = ["2025-01", "2025-02"]
        fat = pd.DataFrame({"competencia": periods,
                            "valo_saidas": [1000.0, 1000.0],
                            "valo_servicos": [0.0, 0.0]}) if com_fat else pd.DataFrame()
        if fonte is None:
            impd = pd.DataFrame()
        else:
            # base_receita 800/mes DIVERGE do fat 1000/mes de proposito:
            # pina o denominador fat-based (200/2000 = 10%, nao 200/1600).
            impd = pd.DataFrame({"competencia": periods,
                                 "valo_impostos": [100.0, 100.0],
                                 "base_receita": [800.0, 800.0],
                                 "fonte": [fonte] * 2})
        empty = pd.DataFrame()
        # O numerador do card e a GUIA. A fixture deriva a guia do
        # proprio `impd` (mesmos valores, um tributo so) porque o que estes
        # testes exercitam e o TEXTO do card e o denominador, nao a
        # composicao do numerador.
        imp = (pd.DataFrame({"competencia": periods, "codi_imp": 1,
                             "valo_devido": impd["valo_impostos"]})
               if not impd.empty else empty)
        spark = {"fat": fat, "imp": impd, "folha": empty, "func": empty,
                 "bal": empty, "imp_guia": imp}
        reg = (pd.DataFrame({"codi_emp": [1] * 2, "competencia": periods,
                             "regime": [regime] * 2})
               if regime else None)
        dre = (pd.DataFrame({"competencia": periods,
                             "provisao_irpj_csll": [provisao / 2] * 2,
                             "receita_bruta": [0.0, 0.0], "despesas": [0.0, 0.0],
                             "lair": [0.0, 0.0], "resultado_liquido": [0.0, 0.0]})
               if provisao else empty)
        ctx = pdf_generator._build_context(
            fat, impd, empty, empty, empty, spark,
            periods, periods, "Teste", "00", "2025-01", "2025-02",
            bal_serie_df=empty, regime_df=reg, dre_df=dre, imp_df=imp)
        return next(c for c in ctx["hero_fiscal"] if c["key"] == key)

    def test_rp_rotulo_unificado_e_pct_pelo_fat_exibido(self, monkeypatch):
        card = self._card("balancete", monkeypatch)
        # O R$ do periodo saiu da sub-linha (a tabela do
        # Detalhamento de Impostos, na mesma pagina, ja o da por tributo).
        assert "de impostos do período" not in card["context"]
        assert "sobre vendas" not in card["context"]
        assert "destacados em nota" not in card["context"]
        assert "em guia" not in card["context"]
        assert card["value"] == "10,0%"          # 200/2000 (fat), nao 200/1600 (base)

    def test_simples_mesmo_rotulo_unificado(self, monkeypatch):
        card = self._card("efsdoimp", monkeypatch)
        assert "de impostos do período" not in card["context"]
        assert "em guia" not in card["context"]

    def test_a_regua_do_irpj_csll_MORREU_no_card_do_PDF(self, monkeypatch):
        """Sub-linha de card e so o fragmento YoY.

        A regua ("inclui IRPJ/CSLL sobre o lucro") e a linha do R$ da provisao
        sairam do PDF. O CRITERIO que as gerava NAO morreu — `_impostos_sem_irpj_proprio`
        segue no metrics e o APP segue exibindo as duas no tooltip, que e onde
        cabe texto longo. Aqui era sub-linha de card de cliente.
        """
        for regime in (None, "simples", "real_presumido"):
            card = self._card("balancete", monkeypatch, regime=regime)
            assert "inclui IRPJ/CSLL sobre o lucro" not in card["context"]
            assert "não inclui IRPJ/CSLL" not in card["context"]
        card = self._card("efsdoimp", monkeypatch, regime="simples", provisao=50.0)
        assert "IRPJ/CSLL do período" not in card["context"]
        # O numerador e a GUIA, entao a PROVISAO nao entra mais no
        # valor — 200/2000 = 10,0%, nao 12,5%. Provisao e competencia; o card
        # mede o que foi PAGO. A frase-regua segue fora do PDF, como acima.
        assert card["value"] == "10,0%"

    def test_sem_impostos_o_valor_e_travessao_e_a_sub_linha_some(self, monkeypatch):
        # impd vazio: % "—". O "R$ 0,00 de impostos do periodo" saiu; sem YoY
        # e sem frase de ausencia que se aplique, o card fica LIMPO.
        card = self._card(None, monkeypatch)
        assert card["value"] == "—"
        assert "de impostos do período" not in card["context"]

    def test_imposto_sem_fat_nf_ganha_label_explicativo(self, monkeypatch):
        # Borda declarada do fix: imposto > 0 e fat NF zero -> "—" COM label,
        # nunca mudo (padrao da casa).
        card = self._card("balancete", monkeypatch, com_fat=False)
        assert card["value"] == "—"
        assert "sem faturamento NF no período pra basear o %" in card["context"]


class TestHeroCaudaAberta:
    """Cards leem o ultimo mes FECHADO — contexto honesto quando a
    cauda foi aparada, e quando NAO sobrou mes nenhum."""

    def _ctx(self, monkeypatch, comps_com_dado, periods):
        import pdf_generator
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda *a, **k: "")
        n = len(comps_com_dado)
        fat = (pd.DataFrame({"competencia": comps_com_dado,
                             "valo_saidas": [1000.0] * n, "valo_servicos": [0.0] * n})
               if n else pd.DataFrame())
        func = (pd.DataFrame({"competencia": comps_com_dado, "codi_emp": [1] * n,
                              "qtd_final": [10] * n, "qtd_inicial": [10] * n,
                              "qtd_admissoes": [0] * n, "qtd_rescisoes": [0] * n,
                              "is_partial": [False] * n})
                if n else pd.DataFrame())
        e = pd.DataFrame()
        spark = {"fat": fat, "imp": e, "folha": e, "func": func, "bal": e}
        return pdf_generator._build_context(
            fat, e, e, func, e, spark, periods, periods, "Teste", "00",
            periods[0], periods[-1], bal_serie_df=e)

    def _card(self, ctx, label):
        return next(c for c in ctx["hero_fiscal"] if c["label"] == label)

    def test_cauda_aparada_rotula_ate_o_ultimo_fechado(self, monkeypatch):
        ctx = self._ctx(monkeypatch, ["2026-01", "2026-02", "2026-03"],
                        ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05"])
        fat = self._card(ctx, "Faturamento")
        # O rotulo de corte e a media sairam da sub-linha do
        # Faturamento. O VALOR segue aparado pela janela efetiva (3 meses, nao
        # 5) — o que mudou foi o texto, nao a conta.
        assert fat["value"] == "R$ 3.000,00"
        assert "dados até Mar/26" not in fat["context"]
        assert "média mensal" not in fat["context"]
        # e o card de Funcionarios ficou INTOCADO, rotulo de corte inclusive:
        # e o unico que ainda declara ate onde o numero vale.
        func = self._card(ctx, "Funcionários")
        assert func["value"] == "10"
        assert "dados até Mar/26" in func["context"]

    def test_range_todo_aberto_nao_mente_o_contexto(self, monkeypatch):
        # nenhum mes fechado: card "—" e contexto honesto — NUNCA "1 mês
        # isolado" (o range tem 3) nem "R$ 0,00" (nao vendeu != sem dado)
        ctx = self._ctx(monkeypatch, [], ["2026-06", "2026-07", "2026-08"])
        fat = self._card(ctx, "Faturamento")
        assert fat["value"] == "—"
        assert "1 mês isolado" not in fat["context"]
        assert "sem dados fechados no período" in fat["context"]


class TestEscapePontual:
    """Dado escapado com html.escape neutraliza HTML injetado,
    SEM quebrar entidades passadas como argumento de macro (autoescape
    segue False por design)."""

    def test_dado_escapado_neutraliza_html_e_entidade_de_macro_sobrevive(self):
        import html
        from pdf_generator import _jinja_env

        tpl = _jinja_env.from_string(
            '{% from "_macros.html" import page_header %}'
            '{{ page_header("Dashboard Cont&aacute;bil", nome, cnpj, "Jan/25 a Dez/25") }}'
        )
        out = tpl.render(
            nome=html.escape("M&C <SCRIPT>alert(1)</SCRIPT> LTDA"),
            cnpj=html.escape("49.275.198/0001-06"),
        )
        assert "M&amp;C" in out                      # & do dado escapado
        assert "<SCRIPT>" not in out                  # tag nao chega crua ao HTML
        assert "&lt;SCRIPT&gt;" in out                # ...chega neutralizada
        assert "Cont&aacute;bil" in out               # entidade de ARGUMENTO de macro intacta
        assert "&nbsp;" in out                        # entidade literal do template intacta


# ── Card no-data (balancete ausente -> valor "—", nao "0,00") ──

class TestCardNoDataBalancete:
    """Empresa SEM balancete -> card contabil de liquidez/endividamento
    mostra valor '—' (NaN honesto), nao '0,00' fabricado.

    `series_mensais` distingue "sem balancete NA JANELA" (isto aqui;
    zero linha de conta 1/2 dentro de `periods`) de "balanco quebrado"
    (flags_inconsistencia, badge 'balancete em revisao'): sem NENHUM dado
    no periodo, ac/pc/pl/at saem NaN diretos de series_mensais, entao
    flags_inconsistencia nem dispara -- o contexto cai no fallback generico
    de valor ausente ('—'), o MESMO idioma ja usado noutros cards sem dado
    (ex.: luc_ctx). "Zero afirma, ausencia declara"."""

    def test_sem_balancete_valor_card_vira_travessao(self, monkeypatch):
        import pdf_generator
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda *a, **k: "")
        periods = ["2025-01", "2025-02", "2025-03"]
        fat = pd.DataFrame({"competencia": periods,
                            "valo_saidas": [1000.0, 1000.0, 1000.0],
                            "valo_servicos": [0.0, 0.0, 0.0]})
        empty = pd.DataFrame()
        spark = {"fat": fat, "imp": empty, "folha": empty, "func": empty, "bal": empty}
        ctx = pdf_generator._build_context(
            fat, empty, empty, empty, empty, spark,
            periods, periods, "Teste", "00", "2025-01", "2025-03",
            bal_serie_df=empty)
        cards = {c["label"]: c for c in ctx["hero_contabil"]}
        for label in ("Liquidez Corrente", "Liquidez Seca", "Endividamento Total"):
            assert cards[label]["value"] == "—", f"{label} valor={cards[label]['value']!r}"
            # Ausencia pura (sem balancete NA JANELA) nao e mais rotulada
            # como 'inconsistente' -- vira o mesmo '—' generico de valor ausente.
            assert cards[label]["context"] == "—", f"{label} context={cards[label]['context']!r}"

    def test_balanco_note_declara_ausencia_com_bal_serie_vazio(self, monkeypatch):
        # balanco_note nao nomeia o mes de labels[-1] quando nao ha
        # NENHUM dado de balanco na janela -- "zero afirma, ausencia declara".
        import pdf_generator
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda *a, **k: "")
        periods = ["2025-01", "2025-02", "2025-03"]
        fat = pd.DataFrame({"competencia": periods,
                            "valo_saidas": [1000.0, 1000.0, 1000.0],
                            "valo_servicos": [0.0, 0.0, 0.0]})
        empty = pd.DataFrame()
        spark = {"fat": fat, "imp": empty, "folha": empty, "func": empty, "bal": empty}
        ctx = pdf_generator._build_context(
            fat, empty, empty, empty, empty, spark,
            periods, periods, "Teste", "00", "2025-01", "2025-03",
            bal_serie_df=empty)
        assert ctx["balanco_note"] == pdf_generator.NOTA_BAL_SEM_DADO_PERIODO

    def test_balanco_note_declara_ausencia_com_carry_in_fora_da_janela(self, monkeypatch):
        # bal_serie_df NAO vazio (tem seed) mas as linhas caem
        # todas ANTES de periods[0] -- e o mesmo "so fora da janela" que o
        # card ja passou a declarar. balanco_note segue a mesma regra.
        import pdf_generator
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda *a, **k: "")
        periods = ["2026-01", "2026-02", "2026-03"]
        fat = pd.DataFrame({"competencia": periods,
                            "valo_saidas": [1000.0, 1000.0, 1000.0],
                            "valo_servicos": [0.0, 0.0, 0.0]})
        empty = pd.DataFrame()
        seed = pd.DataFrame({"codi_emp": [1, 1], "competencia": ["2025-06", "2025-06"],
                             "clas_cta": ["10101", "20101"],
                             "valo_saldo": [100000.0, 40000.0]})
        spark = {"fat": fat, "imp": empty, "folha": empty, "func": empty, "bal": empty}
        ctx = pdf_generator._build_context(
            fat, empty, empty, empty, empty, spark,
            periods, periods, "Teste", "00", "2026-01", "2026-03",
            bal_serie_df=seed)
        assert ctx["balanco_note"] == pdf_generator.NOTA_BAL_SEM_DADO_PERIODO


class TestBalancoNoteSemMovimento:
    """HA dado real na janela, so nao no ULTIMO mes -- balanco_note MANTEM o
    "Valores do balancete em {mes}" (o numero segue exibido, "so exibicao")
    mas ganha a declaracao de que o ultimo mes nao teve movimento."""

    def test_balanco_note_declara_sem_movimento_no_ultimo_mes(self, monkeypatch):
        import pdf_generator
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda *a, **k: "")
        periods = ["2026-01", "2026-02", "2026-03"]
        fat = pd.DataFrame({"competencia": periods,
                            "valo_saidas": [1000.0, 1000.0, 1000.0],
                            "valo_servicos": [0.0, 0.0, 0.0]})
        empty = pd.DataFrame()
        # movimento SO em Jan (dentro da janela); Fev/Mar ffill -- ultimo mes
        # (Mar) sem linha real.
        bal = pd.DataFrame({"codi_emp": [1, 1], "competencia": ["2026-01", "2026-01"],
                            "clas_cta": ["10101", "20101"],
                            "valo_saldo": [100000.0, 40000.0]})
        spark = {"fat": fat, "imp": empty, "folha": empty, "func": empty, "bal": bal}
        ctx = pdf_generator._build_context(
            fat, empty, empty, empty, bal, spark,
            periods, periods, "Teste", "00", "2026-01", "2026-03",
            bal_serie_df=bal)
        assert pdf_generator.NOTA_BAL_SEM_MOVIMENTO_PERIODO in ctx["balanco_note"]
        assert "Valores do balancete em Mar/26" in ctx["balanco_note"]
        # "so exibicao": o card CONTINUA mostrando o saldo carregado, nao "—".
        cards = {c["label"]: c for c in ctx["balanco_cards"]}
        assert cards["Ativo Circulante"]["value"] == "R$ 100.000,00"

    def test_balanco_note_sem_a_declaracao_quando_ultimo_mes_tem_movimento(self, monkeypatch):
        import pdf_generator
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda *a, **k: "")
        periods = ["2026-01", "2026-02", "2026-03"]
        fat = pd.DataFrame({"competencia": periods,
                            "valo_saidas": [1000.0, 1000.0, 1000.0],
                            "valo_servicos": [0.0, 0.0, 0.0]})
        bal = pd.DataFrame({"codi_emp": [1, 1], "competencia": ["2026-03", "2026-03"],
                            "clas_cta": ["10101", "20101"],
                            "valo_saldo": [100000.0, 40000.0]})
        empty = pd.DataFrame()
        spark = {"fat": fat, "imp": empty, "folha": empty, "func": empty, "bal": bal}
        ctx = pdf_generator._build_context(
            fat, empty, empty, empty, bal, spark,
            periods, periods, "Teste", "00", "2026-01", "2026-03",
            bal_serie_df=bal)
        assert pdf_generator.NOTA_BAL_SEM_MOVIMENTO_PERIODO not in ctx["balanco_note"]


class TestLucratividadeJanelaCurta:
    """"NAO clampar; ganha selo de janela curta" (decisao: numero feio e
    honesto > numero bonito). Janela <= LUC_JANELA_CURTA_MESES concentra o
    LAIR de poucos meses no card -- o numero pode sair extremo, e a resposta
    e CONTEXTUALIZAR (fragmento no context do card), nunca capar o valor."""

    def _dre(self, periods, receita, despesas, liquido):
        n = len(periods)
        return pd.DataFrame({
            "codi_emp": [1] * n, "competencia": periods,
            "receita_bruta": [receita] * n, "despesas": [despesas] * n,
            "lair": [liquido] * n, "provisao_irpj_csll": [0.0] * n,
            "resultado_liquido": [liquido] * n,
        })

    def _ctx(self, monkeypatch, periods, dre):
        import pdf_generator
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda *a, **k: "")
        fat = pd.DataFrame({"competencia": periods,
                            "valo_saidas": [1000.0] * len(periods),
                            "valo_servicos": [0.0] * len(periods)})
        empty = pd.DataFrame()
        spark = {"fat": fat, "imp": empty, "folha": empty, "func": empty, "bal": empty}
        return pdf_generator._build_context(
            fat, empty, empty, empty, empty, spark,
            periods, periods, "Teste", "00", periods[0], periods[-1],
            bal_serie_df=empty, dre_df=dre)

    def _luc(self, ctx):
        return next(c for c in ctx["hero_contabil"] if c["label"] == "Lucratividade Média")

    def test_janela_curta_ganha_o_fragmento_sem_capar_o_valor(self, monkeypatch):
        import pdf_generator
        periods = ["2026-01", "2026-02"]   # 2 meses <= LUC_JANELA_CURTA_MESES (3)
        # receita pequena, LAIR grande -> razao EXTREMA de proposito
        # -- e o VALOR nao pode ser capado.
        dre = self._dre(periods, receita=1000.0, despesas=-4000.0, liquido=5000.0)
        ctx = self._ctx(monkeypatch, periods, dre)
        card = self._luc(ctx)
        assert card["value"] == "500,0%"   # 5000/1000 -- feio, honesto, NAO capado
        assert pdf_generator.LUC_JANELA_CURTA_FRAG in card["context"]

    def test_janela_normal_nao_ganha_o_fragmento(self, monkeypatch):
        import pdf_generator
        periods = [f"2026-{m:02d}" for m in range(1, 13)]  # 12 meses
        dre = self._dre(periods, receita=1000.0, despesas=800.0, liquido=200.0)
        ctx = self._ctx(monkeypatch, periods, dre)
        card = self._luc(ctx)
        assert pdf_generator.LUC_JANELA_CURTA_FRAG not in card["context"]


# ── Card valor enganoso (flag inconsistente -> valor "—", mesmo padrao do no-data) ──

class TestCardValorEnganosoSuprimido:
    """Razao neg/neg (AC e PC negativos -> liq_c positivo SEM sentido, ex. 12 -> 7,18)
    + flags_inconsistencia==True -> valor do card vira "—" (suprime o numero mentiroso).

    Badge "balancete em revisão" -- a causa provavel e defeito de
    ESCRITURACAO na origem, nao do sistema."""

    def test_flag_true_negneg_suprime_valor(self, monkeypatch):
        import pdf_generator
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda *a, **k: "")
        periods = ["2025-01", "2025-02", "2025-03"]
        fat = pd.DataFrame({"competencia": periods, "valo_saidas": [1000.0] * 3,
                            "valo_servicos": [0.0] * 3})
        bal = pd.DataFrame({                       # AC(101) e PC(201) ambos NEGATIVOS
            "competencia": periods * 2,
            "clas_cta": ["10101"] * 3 + ["20101"] * 3,
            "valo_saldo": [-100000.0] * 3 + [-40000.0] * 3,
        })
        empty = pd.DataFrame()
        spark = {"fat": fat, "imp": empty, "folha": empty, "func": empty, "bal": bal}
        ctx = pdf_generator._build_context(fat, empty, empty, empty, bal, spark,
                                           periods, periods, "Teste", "00",
                                           "2025-01", "2025-03", bal_serie_df=bal)
        cards = {c["label"]: c for c in ctx["hero_contabil"]}
        for label in ("Liquidez Corrente", "Liquidez Seca", "Endividamento Total"):
            assert cards[label]["value"] == "—", f"{label}={cards[label]['value']!r}"   # 7,18/40,0% -> "—"
            assert cards[label]["context"] == "balancete em revisão"


# ── _ensure_kaleido_server (1 Chrome por processo) ────
# Contrato: o server persistente do kaleido liga 1x por processo (lazy, no
# 1o chart) e NUNCA e parado pelo pdf_generator (stop escopado mataria o
# render de outra aba Streamlit — singleton de processo). Sem Chrome real
# aqui: start_sync_server mockado (convencao da suite).

class TestEnsureKaleidoServer:
    def _arm(self, monkeypatch, calls, delay=0.0):
        import time as _t

        import kaleido

        import pdf_generator as PG
        monkeypatch.setattr(PG, "_kaleido_server_ok", False)
        # Smoke do start: sem este patch ele renderiza de
        # verdade e sobe Chrome real na suite -- e no CI, que nao tem Chrome.
        monkeypatch.setattr(PG, "_kaleido_smoke", lambda *a, **k: None)

        def fake_start(**kw):
            if delay:
                _t.sleep(delay)
            calls.append(kw)
        monkeypatch.setattr(kaleido, "start_sync_server", fake_start)

    def test_inicia_uma_vez_com_silence_warnings(self, monkeypatch):
        calls = []
        self._arm(monkeypatch, calls)
        from pdf_generator import _ensure_kaleido_server
        _ensure_kaleido_server()
        _ensure_kaleido_server()   # idempotente: 2a chamada nao re-inicia
        assert len(calls) == 1
        assert calls[0].get("silence_warnings") is True

    def test_concorrente_inicia_uma_vez(self, monkeypatch):
        # 2 abas Streamlit renderizando ao mesmo tempo: start unico sob lock.
        import threading
        calls = []
        self._arm(monkeypatch, calls, delay=0.05)
        from pdf_generator import _ensure_kaleido_server
        threads = [threading.Thread(target=_ensure_kaleido_server)
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(calls) == 1

    def test_fig_to_b64_garante_server_antes_do_render(self, monkeypatch):
        import plotly.graph_objects as go

        import pdf_generator as PG
        calls = []
        self._arm(monkeypatch, calls)
        ordem = []
        monkeypatch.setattr(
            PG, "_ensure_kaleido_server", lambda: ordem.append("server"))
        monkeypatch.setattr(
            PG.pio, "to_image",
            lambda *a, **kw: ordem.append("render") or b"png")
        out = PG._fig_to_b64(go.Figure(go.Bar(y=[1])))
        assert ordem == ["server", "render"]
        assert out  # base64 do png mockado


# ── Blindagem do kaleido ────────────
# Contratos SEM Chrome (convencao da suite; o CI nao tem Chrome). A prova
# ponta a ponta com Chrome real mora em tests/test_pdf_generator_chrome.py
# (marker `chrome`). Watchdogs encolhidos pra a suite nao esperar 45s.

@pytest.fixture
def kst(monkeypatch):
    import types

    import kaleido

    import pdf_generator as PG
    estado = [
        ("_kaleido_server_ok", False), ("_kaleido_modo", "server"),
        ("_kaleido_motivo", ""), ("_kaleido_desde", None),
        ("_kaleido_ultima_tentativa", 0.0), ("_kaleido_ultimo_restart", None),
        ("_kaleido_server_ja_subiu", False), ("_kaleido_singleton_preso", False),
        ("KALEIDO_N_START_S", 0.5), ("KALEIDO_M_RENDER_S", 0.5),
        ("KALEIDO_N_ONESHOT_S", 0.5), ("KALEIDO_T_STOP_S", 0.3),
    ]
    for nome, valor in estado:
        monkeypatch.setattr(PG, nome, valor, raising=False)
    s = types.SimpleNamespace(PG=PG, starts=0, stops=0, oneshots=0)

    def fake_start(**kw):
        s.starts += 1

    def fake_stop(**kw):
        s.stops += 1

    def fake_oneshot(fig, width, height):
        s.oneshots += 1
        return b"ONESHOT"

    monkeypatch.setattr(kaleido, "start_sync_server", fake_start)
    monkeypatch.setattr(kaleido, "stop_sync_server", fake_stop)
    # nunca Chrome real: o degrau oneshot e falso por default
    monkeypatch.setattr(PG, "_render_oneshot", fake_oneshot, raising=False)
    return s


def _fig_minima():
    import plotly.graph_objects as go
    return go.Figure(go.Bar(y=[1]))


def _roda_com_limite(fn, limite=5.0):
    import threading
    res = {}
    t = threading.Thread(target=lambda: res.setdefault("v", fn()), daemon=True)
    t.start()
    t.join(limite)
    return (not t.is_alive()), res.get("v")


class TestBlindagemKaleido:
    def test_render_serializado_entre_threads(self, kst, monkeypatch):
        # A race vive no put -> join -> get da lib: so nao troca PNG se
        # NUNCA houver dois renders em voo ao mesmo tempo.
        import threading
        import time as _t
        PG = kst.PG
        cont = {"ativos": 0, "max": 0}
        trava = threading.Lock()

        def fake_to_image(fig, **kw):
            with trava:
                cont["ativos"] += 1
                cont["max"] = max(cont["max"], cont["ativos"])
            _t.sleep(0.005)
            with trava:
                cont["ativos"] -= 1
            return b"png"
        monkeypatch.setattr(PG.pio, "to_image", fake_to_image)
        fig = _fig_minima()
        ts = [threading.Thread(target=lambda: [PG._fig_to_b64(fig) for _ in range(5)],
                               daemon=True) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        assert not any(t.is_alive() for t in ts)
        assert cont["max"] == 1

    def test_chrome_que_nao_sobe_nao_trava(self, kst, monkeypatch):
        import threading
        PG = kst.PG
        solta = threading.Event()

        def to_image_preso(fig, **kw):
            solta.wait()
            return b""

        def sem_chrome(fig, width, height):
            raise RuntimeError("sem chrome")
        monkeypatch.setattr(PG.pio, "to_image", to_image_preso)
        monkeypatch.setattr(PG, "_render_oneshot", sem_chrome, raising=False)
        voltou, out = _roda_com_limite(lambda: PG._fig_to_b64(_fig_minima()))
        solta.set()
        assert voltou, "_fig_to_b64 travou com o Chrome que nao sobe"
        assert out == ""
        assert PG._kaleido_server_ok is False
        assert PG._kaleido_modo == "off"
        assert kst.starts == 1      # Chrome que nunca subiu nao ganha restart

    def test_chrome_morto_derruba_o_server_e_a_figura_sai_no_oneshot(
            self, kst, monkeypatch):
        import base64
        PG = kst.PG
        morto = {"v": False}

        def to_image(fig, **kw):
            if morto["v"]:
                raise RuntimeError("Executor is closed.")
            return b"png"
        monkeypatch.setattr(PG.pio, "to_image", to_image)
        assert PG._fig_to_b64(_fig_minima())
        assert PG._kaleido_server_ok is True
        morto["v"] = True
        out = PG._fig_to_b64(_fig_minima())
        assert PG._kaleido_server_ok is False
        assert PG._kaleido_modo == "oneshot"
        assert kst.starts == 2      # 1 restart (instancia ja tinha subido), reprovado
        assert base64.b64decode(out) == b"ONESHOT"   # a figura nao se perdeu

    def test_erro_da_figura_nao_derruba_o_server(self, kst, monkeypatch):
        PG = kst.PG
        ruim = _fig_minima()

        def to_image(fig, **kw):
            if fig is ruim:
                raise ValueError("figura invalida")
            return b"png"
        monkeypatch.setattr(PG.pio, "to_image", to_image)
        assert PG._fig_to_b64(_fig_minima())
        assert PG._fig_to_b64(ruim) == ""
        assert PG._kaleido_modo == "server" and PG._kaleido_server_ok is True
        assert kst.stops == 0 and kst.oneshots == 0

    def test_render_travado_descarta_sem_smoke_e_marca_preso(self, kst, monkeypatch):
        import threading

        import kaleido
        PG = kst.PG
        solta = threading.Event()
        estado = {"travar": False, "chamadas_travado": 0}

        def to_image(fig, **kw):
            if estado["travar"]:
                estado["chamadas_travado"] += 1
                solta.wait()
            return b"png"

        def stop_preso(**kw):
            solta.wait()
        monkeypatch.setattr(PG.pio, "to_image", to_image)
        monkeypatch.setattr(kaleido, "stop_sync_server", stop_preso)
        assert PG._fig_to_b64(_fig_minima())
        estado["travar"] = True
        voltou, out = _roda_com_limite(lambda: PG._fig_to_b64(_fig_minima()))
        chamadas = estado["chamadas_travado"]
        solta.set()
        assert voltou
        assert chamadas == 1        # timeout nao ganha smoke: a fila da lib esta suja
        assert PG._kaleido_singleton_preso is True
        assert PG._kaleido_modo == "oneshot"
        assert out

    def test_oneshot_que_falha_vai_pra_off_e_off_nao_renderiza(self, kst, monkeypatch):
        PG = kst.PG
        monkeypatch.setattr(PG, "_kaleido_modo", "oneshot")
        cont = {"n": 0}

        def falha(fig, width, height):
            cont["n"] += 1
            raise RuntimeError("chrome sumiu")
        monkeypatch.setattr(PG, "_render_oneshot", falha)
        assert PG._fig_to_b64(_fig_minima()) == ""
        assert PG._kaleido_modo == "off"
        assert cont["n"] == 2       # render + smoke
        assert PG._fig_to_b64(_fig_minima()) == ""
        assert cont["n"] == 2       # off devolve "" sem tocar no Chrome

    def test_resondar_respeita_cooldown(self, kst, monkeypatch):
        import time as _t
        PG = kst.PG
        monkeypatch.setattr(PG, "_kaleido_modo", "off")
        monkeypatch.setattr(PG, "_kaleido_ultima_tentativa", _t.monotonic())
        PG._kaleido_resondar()
        assert PG._kaleido_modo == "off" and kst.oneshots == 0
        monkeypatch.setattr(PG, "_kaleido_ultima_tentativa",
                            _t.monotonic() - PG.KALEIDO_COOLDOWN_S - 1)
        PG._kaleido_resondar()
        assert PG._kaleido_modo == "oneshot" and kst.oneshots == 1

    def test_resondar_sobe_pro_server_so_se_o_singleton_nao_ficou_preso(
            self, kst, monkeypatch):
        import time as _t
        PG = kst.PG
        monkeypatch.setattr(PG.pio, "to_image", lambda fig, **kw: b"png")
        monkeypatch.setattr(PG, "_kaleido_modo", "oneshot")
        monkeypatch.setattr(PG, "_kaleido_singleton_preso", True)
        monkeypatch.setattr(PG, "_kaleido_ultima_tentativa",
                            _t.monotonic() - PG.KALEIDO_COOLDOWN_S - 1)
        PG._kaleido_resondar()
        assert PG._kaleido_modo == "oneshot" and kst.starts == 0
        monkeypatch.setattr(PG, "_kaleido_singleton_preso", False)
        PG._kaleido_resondar()
        assert PG._kaleido_modo == "server" and PG._kaleido_server_ok is True
        assert kst.starts == 1

    def test_ensure_com_render_lock_seguro_pela_mesma_thread_nao_trava(
            self, kst, monkeypatch):
        # O smoke roda DENTRO do lock de render; se ele reentrasse no
        # _fig_to_b64, o Lock nao reentrante travaria pra sempre.
        PG = kst.PG
        monkeypatch.setattr(PG.pio, "to_image", lambda fig, **kw: b"png")

        def alvo():
            with PG._kaleido_render_lock:
                PG._ensure_kaleido_server()
            return True
        voltou, ok = _roda_com_limite(alvo)
        assert voltou and ok
        assert PG._kaleido_server_ok is True

    def test_start_loga_o_tempo_do_chrome_frio(self, kst, monkeypatch, capsys):
        # Regua que recalibra KALEIDO_*_S com dado real: sai a cada boot.
        import re
        PG = kst.PG
        monkeypatch.setattr(PG.pio, "to_image", lambda fig, **kw: b"png")
        PG._ensure_kaleido_server()
        err = capsys.readouterr().err
        assert "[kaleido] chrome:" in err
        assert re.search(r"\[kaleido\] frio: \d+\.\d{2}s", err)

    def test_n_start_calibrado_pelo_campo_30s(self):
        # Sem `kst`: a fixture troca a constante; aqui vale o valor do modulo.
        import pdf_generator as PG
        assert PG.KALEIDO_N_START_S == 30.0

    def test_transicao_imprime_linha_e_estado_publico(self, kst, capsys):
        PG = kst.PG
        PG._kaleido_transicao("oneshot", "motivo X")
        assert "[kaleido] server -> oneshot: motivo X" in capsys.readouterr().err
        e = PG.kaleido_estado()
        assert e["modo"] == "oneshot" and e["motivo"] == "motivo X" and e["desde"]


# ── Um chart pode falhar
# SOZINHO -- o smoke confirma que a infra esta saudavel (`_kaleido_render`
# faz `raise` de volta, sem derrubar `_kaleido_modo`) -- e o PDF saia com o
# buraco e rc=0, contado como sucesso. "Charts esperados" NAO e numero
# cravado: e o proprio total de chamadas a `_fig_to_b64` que a pagina fez
# (so ha chamada quando existe fig valida pra desenhar).
class TestPdfContraditorioNaoContaComoSucesso:
    def _monta_fake_build(self, monkeypatch, PG, n_falhas, n_ok):
        """Fake `_build_html_for_empresa` que chama o `_fig_to_b64` REAL
        (com a contagem instrumentada) `n_falhas + n_ok` vezes -- simula
        charts que a pagina decidiu desenhar, alguns falhando."""
        respostas = [RuntimeError("figura ruim")] * n_falhas + [b"PNGBYTES"] * n_ok

        def fake_render(fig, width, height):
            r = respostas.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        monkeypatch.setattr(PG, "_kaleido_render", fake_render)

        def fake_build(ids, inicio, fim, conn, nome="", cnpj="", modelos=None):
            saidas = [PG._fig_to_b64(_fig_minima())
                      for _ in range(n_falhas + n_ok)]
            return f"<html>{''.join(saidas)}</html>"
        monkeypatch.setattr(PG, "_build_html_for_empresa", fake_build)

    def test_contador_de_charts_e_isolado_por_thread(self, kst, monkeypatch):
        # `_contador_charts_atual` nao pode ser um global SIMPLES, so
        # thread-local — concorrencia de threads nesta area e fato
        # (test_render_serializado_entre_threads
        # ja prova que dois RENDERS concorrentes existem). O
        # save/restore do "anterior" protege ANINHAMENTO (nao existe hoje,
        # por construcao) mas NAO protege duas sessoes concorrentes: uma
        # pode herdar/poluir a contagem da outra.
        #
        # Interlace DETERMINISTICO (Event, sem sleep): A abre o contexto,
        # espera B TAMBEM abrir (contexto de B fica ATIVO), so ENTAO A
        # registra 3 chamadas — com o bug, o global aponta pro contador de
        # B nesse instante, entao as 3 chamadas de A vazam pra B.
        import threading

        PG = kst.PG
        monkeypatch.setattr(
            PG, "_kaleido_render",
            lambda fig, width, height: (_ for _ in ()).throw(RuntimeError("x")))

        a_entrou = threading.Event()
        b_entrou = threading.Event()
        a_registrou = threading.Event()
        resultados = {}

        def thread_a():
            with PG._contando_charts() as contador:
                a_entrou.set()
                assert b_entrou.wait(timeout=5), "B nao entrou a tempo"
                for _ in range(3):
                    PG._fig_to_b64(_fig_minima())
                a_registrou.set()
            resultados["A"] = (contador.tentativas, contador.falhas)

        def thread_b():
            assert a_entrou.wait(timeout=5), "A nao entrou a tempo"
            with PG._contando_charts() as contador:
                b_entrou.set()
                assert a_registrou.wait(timeout=5), "A nao registrou a tempo"
                for _ in range(5):
                    PG._fig_to_b64(_fig_minima())
            resultados["B"] = (contador.tentativas, contador.falhas)

        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)
        ta.start()
        tb.start()
        ta.join(timeout=10)
        tb.join(timeout=10)
        assert not ta.is_alive() and not tb.is_alive(), "thread travada"

        assert resultados.get("A") == (3, 3), resultados
        assert resultados.get("B") == (5, 5), resultados

    def test_pdf_individual_levanta_erro_com_modo_ainda_saudavel(
            self, kst, monkeypatch):
        # RED por VALOR: 1 de 2 charts falha, o modo geral CONTINUA "server"
        # (a origem so faz `raise` quando o smoke confirmou infra saudavel) --
        # antes do fix, isso virava PDF sem 1 imagem e rc=0.
        PG = kst.PG
        monkeypatch.setattr(PG, "_kaleido_resondar", lambda: None)
        self._monta_fake_build(monkeypatch, PG, n_falhas=1, n_ok=1)
        with pytest.raises(RuntimeError) as exc:
            PG.gerar_pdf_empresa([1], "2025-01", "2025-01", None)
        assert "1 de 2" in str(exc.value)
        assert PG._kaleido_modo == "server"   # infra reportada saudavel

    def test_pdf_individual_sadio_nao_levanta(self, kst, monkeypatch):
        # Regressao inversa: zero falha continua gerando normal.
        PG = kst.PG
        monkeypatch.setattr(PG, "_kaleido_resondar", lambda: None)
        self._monta_fake_build(monkeypatch, PG, n_falhas=0, n_ok=2)
        monkeypatch.setattr(PG, "_html_to_pdf", lambda h: b"%PDF-1.4 fake")
        assert PG.gerar_pdf_empresa([1], "2025-01", "2025-01", None) == b"%PDF-1.4 fake"

    def test_pdf_individual_em_modo_off_nao_levanta_e_degradacao_ja_decidida(
            self, kst, monkeypatch):
        # Modo "off" e degradacao JA COMUNICADA por
        # `kaleido_estado()` (quem chama avisa) -- o fix NAO pode virar isso
        # em erro novo, mesmo que TODOS os charts venham vazios.
        PG = kst.PG
        monkeypatch.setattr(PG, "_kaleido_modo", "off")
        monkeypatch.setattr(PG, "_kaleido_resondar", lambda: None)
        monkeypatch.setattr(PG, "_build_html_for_empresa",
                            lambda *a, **k: "<html>sem grafico</html>")
        monkeypatch.setattr(PG, "_html_to_pdf", lambda h: b"%PDF-1.4 fake")
        assert PG.gerar_pdf_empresa([1], "2025-01", "2025-01", None) == b"%PDF-1.4 fake"

    def test_lote_unidade_com_chart_falho_vai_pra_erros_nao_conta_sucesso(
            self, kst, monkeypatch):
        # Mesmo RED do individual, pro lote: a unidade com o chart falho
        # tem que sair de "sucessos" e entrar em "erros" -- nao pode virar
        # "Gerados" com um PDF furado.
        PG = kst.PG
        monkeypatch.setattr(PG, "_kaleido_resondar", lambda: None)
        self._monta_fake_build(monkeypatch, PG, n_falhas=1, n_ok=0)
        monkeypatch.setattr(
            PG, "_html_to_pdf_batch",
            lambda items: [(lbl, b"%PDF-1.4 fake", None) for _, lbl in items])
        unidades = [{"ids": [1], "nome": "EMP_A", "cnpj": "", "filename": "A.pdf"}]
        zip_bytes, sucessos, erros = PG.gerar_todos_pdfs(
            "2025-01", "2025-01", None, unidades=unidades)
        assert sucessos == 0
        assert len(erros) == 1 and "EMP_A" in erros[0] and "1 de 1" in erros[0]
        assert zip_bytes is None
        assert PG._kaleido_modo == "server"

    def test_lote_unidade_sadia_conta_normal(self, kst, monkeypatch):
        # Regressao inversa: zero falha continua contando normal (nao pode
        # o fix virar guarda cega que rejeita PDF bom).
        PG = kst.PG
        monkeypatch.setattr(PG, "_kaleido_resondar", lambda: None)
        self._monta_fake_build(monkeypatch, PG, n_falhas=0, n_ok=2)
        monkeypatch.setattr(
            PG, "_html_to_pdf_batch",
            lambda items: [(lbl, b"%PDF-1.4 fake", None) for _, lbl in items])
        unidades = [{"ids": [1], "nome": "EMP_A", "cnpj": "", "filename": "A.pdf"}]
        zip_bytes, sucessos, erros = PG.gerar_todos_pdfs(
            "2025-01", "2025-01", None, unidades=unidades)
        assert sucessos == 1 and erros == [] and zip_bytes is not None

    def test_lote_unidade_que_cai_em_off_no_meio_do_build_nao_vira_erro_novo(
            self, kst, monkeypatch):
        # A degradacao PRE-EXISTENTE (modo cai pra "off" DENTRO do build desta
        # unidade) continua tratada pelo `abortado` de sempre -- meu check
        # novo nao pode disputar com ela nem duplicar a contagem.
        PG = kst.PG
        monkeypatch.setattr(PG, "_kaleido_resondar", lambda: None)

        def fake_build(ids, inicio, fim, conn, nome="", cnpj="", modelos=None):
            PG._kaleido_modo = "off"
            return "<html>sem grafico</html>"
        monkeypatch.setattr(PG, "_build_html_for_empresa", fake_build)
        monkeypatch.setattr(
            PG, "_html_to_pdf_batch",
            lambda items: [(lbl, b"%PDF-1.4 fake", None) for _, lbl in items])
        unidades = [{"ids": [1], "nome": "EMP_A", "cnpj": "", "filename": "A.pdf"},
                    {"ids": [2], "nome": "EMP_B", "cnpj": "", "filename": "B.pdf"}]
        with pytest.raises(PG.LoteAbortadoKaleido) as exc:
            PG.gerar_todos_pdfs("2025-01", "2025-01", None, unidades=unidades)
        e = exc.value
        assert e.modo == "off" and e.sucessos == 0
        assert e.pendentes == ["EMP_A", "EMP_B"]   # a que caiu em off NAO entra


class TestAvisoKaleido:
    # Texto do aviso de modo dos graficos. O app.py so exibe.
    # Quem le e CONTADOR, so com navegador: o texto diz o que houve, se o PDF
    # serve e o que fazer -- sem termo tecnico e sem pedir reinicio de servico.
    MOT = "render falhou (Executor is closed.) e o smoke tambem (sem resposta em 30s)"
    TECNICOS = ("motivo", "kaleido", "chrome", "smoke", "server", "oneshot",
                "executor", "reinici", "serviço", "servico", "processo")

    def _todos(self):
        from pdf_generator import aviso_kaleido
        est = {"modo": "oneshot", "motivo": self.MOT}
        off = {"modo": "off", "motivo": self.MOT}
        return [aviso_kaleido(est), aviso_kaleido(off),
                aviso_kaleido(est, lote=(1, 0, 243)), aviso_kaleido(off, lote=(0, 0, 2))]

    def test_server_ou_sem_estado_nao_avisa(self):
        from pdf_generator import aviso_kaleido, detalhe_kaleido
        assert aviso_kaleido({"modo": "server", "motivo": ""}) is None
        assert aviso_kaleido(None) is None
        assert detalhe_kaleido({"modo": "server", "motivo": "x"}) is None
        assert detalhe_kaleido(None) is None

    def test_oneshot_e_warning_diz_que_o_pdf_serve(self):
        nivel, texto = self._todos()[0]
        assert nivel == "warning"
        assert "modo lento" in texto and "podem ser usados normalmente" in texto
        assert "Avise o responsável pelo sistema" in texto

    def test_off_e_error_diz_pra_nao_usar_o_arquivo(self):
        nivel, texto = self._todos()[1]
        assert nivel == "error"
        assert "saiu sem eles" in texto and "Não use este arquivo com o cliente" in texto
        assert "Avise o responsável pelo sistema" in texto

    def test_lote_mantem_a_contagem_com_pendentes_separadas(self):
        nivel, texto = self._todos()[2]
        assert nivel == "warning"
        assert "1 gerado, 0 com erro, 243 não gerados" in texto
        assert "Avise o responsável pelo sistema" in texto
        nivel, texto = self._todos()[3]
        assert nivel == "error"
        assert "0 gerados, 0 com erro, 2 não gerados" in texto
        assert "ZIP" not in texto          # nada saiu: nao ha ZIP pra mencionar

    def test_nenhum_texto_de_tela_tem_termo_tecnico_nem_pede_reinicio(self):
        for nivel, texto in self._todos():
            baixo = texto.lower()
            assert not [t for t in self.TECNICOS if t in baixo], texto
            assert self.MOT not in texto

    def test_detalhe_tecnico_sai_curto_e_sem_duplicar(self):
        # Modo off: a mensagem inteira do kaleido, 2x.
        from pdf_generator import detalhe_kaleido
        exc = ("Kaleido v1 and later requires Chrome to be installed. To install "
               "Chrome, use the CLI command `kaleido_get_chrome`, or from Python, "
               "use either `await kaleido.get_chrome()` or "
               "`kaleido.get_chrome_sync()`.")
        det = detalhe_kaleido({"modo": "off",
                               "motivo": f"oneshot falhou ({exc}) e o smoke tambem ({exc})"})
        assert det.count("Kaleido v1") == 1
        assert "smoke tambem" not in det
        assert len(det) <= 125
        assert det.count("(") == det.count(")")

    def test_detalhe_curto_da_casa_passa_inteiro(self):
        from pdf_generator import detalhe_kaleido
        motivo = self.MOT + "; server preso ate reiniciar o processo"
        assert detalhe_kaleido({"modo": "oneshot", "motivo": motivo}) == motivo


class TestFioDreNoFluxoReal:
    """Os testes de _build_context direto nao cobrem o
    CALL-SITE real — dre_df tem de chegar na chamada de _build_context dentro de
    _build_html_for_empresa (default None -> card suprimido em TODO PDF real).
    Este teste percorre o fluxo real (_build_html_for_
    empresa -> _build_context -> series_mensais -> template) com _load_data
    fake e afere o HTML final."""

    def test_build_html_leva_dre_ate_o_card(self, monkeypatch):
        import pdf_generator
        from etl.metrics import LUC_SEM_REGISTROS
        periods = ["2025-01", "2025-02", "2025-03"]
        fat = pd.DataFrame({"competencia": periods,
                            "valo_saidas": [1000.0] * 3, "valo_servicos": [0.0] * 3})
        impd = pd.DataFrame({"competencia": periods,
                             "valo_impostos": [100.0] * 3, "base_receita": [1000.0] * 3})
        bal = pd.DataFrame({"competencia": periods,
                            "clas_cta": ["3010101"] * 3, "nome_cta": ["Receita"] * 3,
                            "valo_saldo": [1200.0] * 3})
        dre = pd.DataFrame({"codi_emp": [1] * 3, "competencia": periods,
                            "receita_bruta": [1000.0] * 3, "despesas": [800.0] * 3,
                            "lair": [200.0] * 3, "provisao_irpj_csll": [0.0] * 3,
                            "resultado_liquido": [200.0] * 3})
        empty = pd.DataFrame()

        def fake_load_data(conn, emp_ids, de, ate):
            # 10a posicao: mart.impostos_mensais, insumo do modelo
            # de Detalhamento de Impostos. 7a: bal_ir_df, o passivo
            # de IRPJ/CSLL na janela +3m (fallback da excecao).
            return (fat, impd, empty, empty, bal, bal, empty, empty, empty,
                    dre, empty, empty)

        def fake_spark(conn, emp_ids, de, ate, *a, **k):
            return {"fat": fat, "imp": impd, "folha": empty, "func": empty,
                    "bal": bal, "encargos": empty}

        monkeypatch.setattr(pdf_generator, "_load_data", fake_load_data)
        monkeypatch.setattr(pdf_generator, "_load_spark_data", fake_spark)
        # fluxo real agora carrega a janela YoY tambem — fake vazio
        monkeypatch.setattr(pdf_generator, "_load_data_yoy",
                            lambda *a, **k: {})
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda *a, **k: "")
        html = pdf_generator._build_html_for_empresa([1], "2025-01", "2025-03", None)
        # card real da DRE (200/1000 = 20,0%), NUNCA o rotulo de supressao
        assert "20,0%" in html
        assert LUC_SEM_REGISTROS not in html


class TestRotulosMovimentacaoFolha:
    """A tabela de Movimentacao exibe Entradas/Saidas (fluxo da
    FOLHA, convencao B) e a nota antiga ("admissoes em fim de mes...") morre
    — era a explicacao errada do nao-fechamento. Sobrevive so a frase do
    dados-em-fechamento + o one-liner de semantica."""

    def test_fiscal_renderiza_entradas_saidas_e_nota_nova(self, monkeypatch):
        import pdf_generator
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda *a, **k: "")
        periods = ["2025-01", "2025-02", "2025-03"]
        fat = pd.DataFrame({"competencia": periods,
                            "valo_saidas": [1000.0] * 3, "valo_servicos": [0.0] * 3})
        impd = pd.DataFrame({"competencia": periods,
                             "valo_impostos": [100.0] * 3, "base_receita": [1000.0] * 3})
        bal = pd.DataFrame({"competencia": periods,
                            "clas_cta": ["3010101"] * 3, "nome_cta": ["Receita"] * 3,
                            "valo_saldo": [1200.0] * 3})
        # A nota do (*) virou CONDICIONAL — este teste
        # cobre o lado COM mes parcial (nota renderiza); o lado sem parcial
        # vive no teste irmao abaixo e em TestPolishLayout.
        func = pd.DataFrame({"competencia": periods,
                             "qtd_inicial": [15, 17, 16], "qtd_admissoes": [2, 0, 1],
                             "qtd_rescisoes": [0, 1, 0], "qtd_final": [17, 16, 17],
                             "is_partial": [False, False, True]})
        empty = pd.DataFrame()
        spark = {"fat": fat, "imp": impd, "folha": empty, "func": func,
                 "bal": bal, "encargos": empty}
        ctx = pdf_generator._build_context(
            fat, impd, empty, func, bal, spark,
            periods, periods, "Teste", "00", "2025-01", "2025-03",
            bal_serie_df=bal, dre_df=empty)
        html = pdf_generator._render_html(ctx)
        # rotulos novos (headers da tabela; entidade &iacute; e como o template escreve)
        assert "Entradas" in html and "Sa&iacute;das" in html
        assert ">Adm<" not in html and ">Dem<" not in html
        # nota antiga MORTA (fragmento cobre a forma com entidades)
        assert "em fim de m" not in html
        # Frase semantica tambem MORTA; sobrevive so a
        # legenda do marcador (*)
        assert "afastamentos e retornos" not in html
        assert "dados em fechamento" in html


class TestRotuloAtivosFimHibrida:
    """O headcount do PDF vira 'Ativos (fim)'
    (unifica com o app); SEM frase adicional no rodape."""

    def test_fiscal_renderiza_ativos_fim_sem_final(self, monkeypatch):
        import pdf_generator
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda *a, **k: "")
        periods = ["2025-01", "2025-02", "2025-03"]
        fat = pd.DataFrame({"competencia": periods,
                            "valo_saidas": [1000.0] * 3, "valo_servicos": [0.0] * 3})
        impd = pd.DataFrame({"competencia": periods,
                             "valo_impostos": [100.0] * 3, "base_receita": [1000.0] * 3})
        bal = pd.DataFrame({"competencia": periods,
                            "clas_cta": ["3010101"] * 3, "nome_cta": ["Receita"] * 3,
                            "valo_saldo": [1200.0] * 3})
        func = pd.DataFrame({"competencia": periods,
                             "qtd_inicial": [15, 16, 16], "qtd_admissoes": [2, 0, 0],
                             "qtd_rescisoes": [1, 0, 0], "qtd_final": [16, 16, 16],
                             "is_partial": [False, False, False]})
        empty = pd.DataFrame()
        spark = {"fat": fat, "imp": impd, "folha": empty, "func": func,
                 "bal": bal, "encargos": empty}
        ctx = pdf_generator._build_context(
            fat, impd, empty, func, bal, spark,
            periods, periods, "Teste", "00", "2025-01", "2025-03",
            bal_serie_df=bal, dre_df=empty)
        html = pdf_generator._render_html(ctx)
        assert "Ativos (fim)" in html
        assert ">Final<" not in html                    # header antigo morto
        # rodape SEM frase semantica (so a legenda
        # do marcador) e sem frase de demitidos
        assert "afastamentos e retornos" not in html
        assert "ainda os remunera" not in html
        # Fixture SEM mes parcial -> nota do (*) some
        # (era orfa; o lado com parcial e coberto no teste acima).
        assert "dados em fechamento" not in html


class TestYoYNosCardsPDF:
    """YoY delta-no-card: o contexto dos cards compara com o
    MESMO periodo do ano anterior (kpis_yoy, janela -12m); o "vs inicio"
    morre. Label "sem dados de {ano}" NO LUGAR do delta quando o anterior
    e incomputavel; atual sem KPI nao ganha ruido; func (movimentacao da
    janela) fica como esta."""

    PERIODS = ["2025-01", "2025-02", "2025-03"]
    PREV = ["2024-01", "2024-02", "2024-03"]

    def _dfs(self, periods, fat_mes=1200.0, imp_mes=144.0, folha_mes=144.0,
             desp_mes=880.0, liq_mes=120.0):
        # defaults ATUAIS calibrados pra 12% nos 3 cards de razao:
        # imp 144/1200 (fat NF exibido) | folenc 144/1200
        # (fat) | luc 120/1000. prev (fixture) usa 10% nos tres -> delta
        # +2,0 p.p. (prev imp 100/1000 = 10% tambem por fat).
        n = len(periods)
        fat = pd.DataFrame({"competencia": periods,
                            "valo_saidas": [fat_mes] * n,
                            "valo_servicos": [0.0] * n})
        impd = pd.DataFrame({"competencia": periods,
                             "valo_impostos": [imp_mes] * n,
                             "base_receita": [1000.0] * n})
        folha = pd.DataFrame({"competencia": periods,
                              "valo_bruto": [folha_mes] * n,
                              "tipo_process": [11] * n})
        dre = pd.DataFrame({"codi_emp": [1] * n, "competencia": periods,
                            "receita_bruta": [1000.0] * n,
                            "despesas": [desp_mes] * n,
                            "lair": [liq_mes] * n,
                            "provisao_irpj_csll": [0.0] * n,
                            "resultado_liquido": [liq_mes] * n})
        # O numerador do card e a GUIA. A fixture deriva a guia do
        # `impd` (mesmos valores, um tributo so) — o que estes testes travam e
        # o PAREAMENTO das janelas, e os dois lados tem de ler a MESMA base.
        imp = pd.DataFrame({"competencia": periods, "codi_imp": [1] * n,
                            "valo_devido": [imp_mes] * n})
        return fat, impd, folha, dre, imp

    def _ctx(self, monkeypatch, yoy_dfs="default", dre=None, func=None):
        import pdf_generator
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda *a, **k: "")
        fat, impd, folha, dre_cur, imp = self._dfs(self.PERIODS)
        if dre is not None:
            dre_cur = dre
        empty = pd.DataFrame()
        func_df = func if func is not None else empty
        if yoy_dfs == "default":
            p_fat, p_impd, p_folha, p_dre, p_imp = self._dfs(
                self.PREV, fat_mes=1000.0, imp_mes=100.0, folha_mes=100.0,
                desp_mes=900.0, liq_mes=100.0)   # 10% nos 3 cards de razao
            yoy_dfs = {"fat": p_fat, "impd": p_impd, "folha": p_folha,
                       "encargos": empty, "dre": p_dre, "imp": p_imp}
        spark = {"fat": fat, "imp": impd, "folha": folha, "func": func_df,
                 "bal": empty, "encargos": empty, "imp_guia": imp}
        kw = {"bal_serie_df": empty, "dre_df": dre_cur, "imp_df": imp}
        if yoy_dfs is not None:
            kw["yoy_dfs"] = yoy_dfs
        return pdf_generator._build_context(
            fat, impd, folha, func_df, empty, spark,
            self.PERIODS, self.PERIODS, "Teste", "00", "2025-01", "2025-03",
            **kw)

    def _card(self, ctx, key):
        return next(c for c in ctx["hero_fiscal"] if c["key"] == key)

    def test_fat_context_vs_mesmo_periodo_substitui_vs_inicio(self, monkeypatch):
        # 3600 vs 3000 = +20,0%
        ctx = self._ctx(monkeypatch)
        card = self._card(ctx, "fat")
        assert "em&nbsp;relação ao mesmo período/2024: ↑20,0%" in card["context"]
        assert "vs início" not in card["context"]
        # A media saiu — sobra SO o fragmento YoY, sem separador orfao
        assert "média mensal" not in card["context"]
        assert card["context"].startswith("em&nbsp;relação")
        assert " · " not in card["context"]

    def test_fat_context_queda_usa_seta_baixo(self, monkeypatch):
        empty = pd.DataFrame()
        p_fat, p_impd, p_folha, p_dre, _ = self._dfs(self.PREV, fat_mes=2000.0)
        ctx = self._ctx(monkeypatch, yoy_dfs={"fat": p_fat, "impd": empty,
                                              "folha": empty, "encargos": empty,
                                              "dre": empty})
        card = self._card(ctx, "fat")
        # 3600 vs 6000 = -40,0%
        assert "em&nbsp;relação ao mesmo período/2024: ↓40,0%" in card["context"]

    def test_imp_e_folenc_ganham_sufixo_pp(self, monkeypatch):
        ctx = self._ctx(monkeypatch)
        imp = self._card(ctx, "imp")
        folenc = self._card(ctx, "folenc")
        # 12% vs 10% = +2,0 p.p. nos dois
        assert "em&nbsp;relação ao mesmo período/2024: ↑2,0 p.p." in imp["context"]
        assert "em&nbsp;relação ao mesmo período/2024: ↑2,0 p.p." in folenc["context"]
        assert "inclui 13º e autônomos (RPA)" not in folenc["context"]
        # os dois ficam SO com o fragmento: nada antes, nada depois
        assert " · " not in imp["context"] and " · " not in folenc["context"]

    def test_luc_hero_contabil_ganha_sufixo_pp(self, monkeypatch):
        ctx = self._ctx(monkeypatch)
        card = next(c for c in ctx["hero_contabil"]
                    if c["label"] == "Lucratividade Média")
        assert "último mês:" in card["context"]
        assert "em&nbsp;relação ao mesmo período/2024: ↑2,0 p.p." in card["context"]

    def test_prev_vazio_label_no_lugar_do_delta(self, monkeypatch):
        empty = pd.DataFrame()
        ctx = self._ctx(monkeypatch, yoy_dfs={"fat": empty, "impd": empty,
                                              "folha": empty, "encargos": empty,
                                              "dre": empty})
        card = self._card(ctx, "fat")
        assert "sem dados de 2024" in card["context"]
        assert "mesmo período" not in card["context"]

    def test_func_context_inalterado(self, monkeypatch):
        func = pd.DataFrame({"competencia": self.PERIODS,
                             "qtd_inicial": [10, 10, 11],
                             "qtd_admissoes": [0, 1, 1],
                             "qtd_rescisoes": [0, 0, 0],
                             "qtd_final": [10, 11, 12],
                             "is_partial": [False] * 3})
        ctx = self._ctx(monkeypatch, func=func)
        card = self._card(ctx, "func")
        assert "início: 10" in card["context"]
        assert "mesmo período" not in card["context"]
        assert "sem dados de" not in card["context"]

    def test_luc_suprimida_atual_sem_ruido_yoy(self, monkeypatch):
        # atual sem despesa na DRE -> card "—" com rotulo de supressao; o YoY
        # nao adiciona nada (nem delta nem "sem dados de").
        n = len(self.PERIODS)
        dre_sem_desp = pd.DataFrame({"codi_emp": [1] * n,
                                     "competencia": self.PERIODS,
                                     "receita_bruta": [1000.0] * n,
                                     "despesas": [0.0] * n,
                                     "lair": [1000.0] * n,
                                     "provisao_irpj_csll": [0.0] * n,
                                     "resultado_liquido": [1000.0] * n})
        ctx = self._ctx(monkeypatch, dre=dre_sem_desp)
        card = next(c for c in ctx["hero_contabil"]
                    if c["label"] == "Lucratividade Média")
        assert "mesmo período" not in card["context"]
        assert "sem dados de" not in card["context"]

    def test_yoy_dfs_none_degrada_para_label(self, monkeypatch):
        # default None (call site sem o kwarg) == prev vazio -> label VISIVEL,
        # nunca supressao muda.
        ctx = self._ctx(monkeypatch, yoy_dfs=None)
        card = self._card(ctx, "fat")
        assert "sem dados de 2024" in card["context"]


class TestFioYoYNoFluxoReal:
    """Espelho do TestFioDreNoFluxoReal: o call-site real
    (_build_html_for_empresa) TEM que carregar a janela -12m e passar
    yoy_dfs pro _build_context — default esquecido viraria label em todo
    PDF (visivel, mas errado quando HA dado do ano anterior)."""

    def test_build_html_carrega_janela_prev_e_leva_yoy_ao_card(self, monkeypatch):
        import pdf_generator
        periods = ["2025-01", "2025-02", "2025-03"]
        prev = ["2024-01", "2024-02", "2024-03"]
        fat = pd.DataFrame({"competencia": periods,
                            "valo_saidas": [1200.0] * 3, "valo_servicos": [0.0] * 3})
        impd = pd.DataFrame({"competencia": periods,
                             "valo_impostos": [100.0] * 3, "base_receita": [1000.0] * 3})
        bal = pd.DataFrame({"competencia": periods,
                            "clas_cta": ["3010101"] * 3, "nome_cta": ["Receita"] * 3,
                            "valo_saldo": [1200.0] * 3})
        empty = pd.DataFrame()
        p_fat = pd.DataFrame({"competencia": prev,
                              "valo_saidas": [1000.0] * 3, "valo_servicos": [0.0] * 3})
        captured = {}

        def fake_load_data(conn, emp_ids, de, ate):
            return (fat, impd, empty, empty, bal, bal, empty, empty, empty,
                    empty, empty, empty)

        def fake_spark(conn, emp_ids, de, ate, *a, **k):
            return {"fat": fat, "imp": impd, "folha": empty, "func": empty,
                    "bal": bal, "encargos": empty}

        def fake_yoy(conn, emp_ids, de_prev, ate_prev):
            captured["janela"] = (de_prev, ate_prev)
            return {"fat": p_fat, "impd": empty, "folha": empty,
                    "encargos": empty, "dre": empty}

        monkeypatch.setattr(pdf_generator, "_load_data", fake_load_data)
        monkeypatch.setattr(pdf_generator, "_load_spark_data", fake_spark)
        monkeypatch.setattr(pdf_generator, "_load_data_yoy", fake_yoy)
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda *a, **k: "")
        html = pdf_generator._build_html_for_empresa([1], "2025-01", "2025-03", None)
        assert captured["janela"] == ("2024-01", "2024-03")
        # 3600 vs 3000 = +20,0% chega ao HTML final
        assert "em&nbsp;relação ao mesmo período/2024: ↑20,0%" in html


class TestSaldoInvertidoNosCards:
    """Card de balanco negativo sai CRU + nota no context."""

    def _cards(self, monkeypatch, ac_val):
        import pdf_generator
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda *a, **k: "")
        periods = ["2025-01"]
        fat = pd.DataFrame({"competencia": periods, "valo_saidas": [1_000_000.0],
                            "valo_servicos": [0.0]})
        # balancete: Ativo Circulante (101) com o sinal do cenario
        bal = pd.DataFrame({
            "codi_emp": [1, 1], "competencia": periods * 2,
            "clas_cta": ["1010101", "2010101"], "codi_cta": [1, 2],
            "nome_cta": ["Caixa", "Fornecedores"],
            "valo_saldo": [ac_val, 5_000.0],
            "valo_deb": [0.0, 0.0], "valo_cre": [0.0, 0.0]})
        empty = pd.DataFrame()
        spark = {"fat": fat, "imp": empty, "folha": empty, "func": empty,
                 "bal": bal, "dre": empty}
        ctx = pdf_generator._build_context(
            fat, empty, empty, empty, bal, spark, periods, periods,
            "Teste", "00", "2025-01", "2025-01", bal_serie_df=bal,
            dre_df=empty)
        return {c["label"]: c for c in ctx["balanco_cards"]}

    def test_ativo_negativo_valor_CRU_e_nota(self, monkeypatch):
        c = self._cards(monkeypatch, -10_840_952.47)["Ativo Circulante"]
        # o numero NAO e escondido nem absolutizado — a evidencia fica
        assert "-10.840.952,47" in c["value"]
        assert c["context"] == "saldo invertido na escrituração"

    def test_empresa_saudavel_sem_nota(self, monkeypatch):
        c = self._cards(monkeypatch, 38_070_948.23)["Ativo Circulante"]
        assert "38.070.948,23" in c["value"]
        assert c["context"] == ""

    def test_so_o_card_negativo_ganha_a_nota(self, monkeypatch):
        cards = self._cards(monkeypatch, -1_000.0)
        assert cards["Ativo Circulante"]["context"] != ""
        assert cards["Passivo Circulante"]["context"] == ""


class TestRegistroDeModelos:
    """Registro unico de modelos + kwarg `modelos` no funil.

    Ate aqui _render_html hardcodava fiscal+contabil, e ligar um seletor
    exigiria uma lista por ponto de geracao (botao individual, selecao,
    lote, CLI). Com o registro os 4 pontos passam o MESMO kwarg pelo funil
    unico e nenhum deles carrega logica propria.

    Renderiza os templates REAIS com um ctx minimo: `charts` como dict (as
    chaves ausentes viram Undefined falsy -> o macro cai no empty_msg) e os
    4 iteraveis das paginas como listas vazias.
    """
    CTX = {"charts": {}, "hero_fiscal": [], "hero_contabil": [],
           "func_rows": [], "balanco_cards": []}

    def _paginas(self, html):
        return html.count('<div class="page"')

    def test_default_e_o_registro_inteiro_na_ordem(self):
        import pdf_generator
        html = pdf_generator._render_html(dict(self.CTX))
        assert self._paginas(html) == len(pdf_generator.MODELOS)
        # ordem do registro: Fiscal antes de Contabil
        assert html.find("Dashboard Fiscal") < html.find("Dashboard Cont")

    def test_subconjunto_rende_so_o_pedido(self):
        import pdf_generator
        html = pdf_generator._render_html(dict(self.CTX), modelos=["fiscal"])
        assert self._paginas(html) == 1
        assert "Dashboard Fiscal" in html
        assert "Dashboard Cont" not in html

    def test_ordem_do_call_site_e_ignorada(self):
        # a sequencia das paginas e contrato do produto, nao do call site:
        # dois botoes nunca podem produzir a mesma selecao em ordens
        # diferentes.
        import pdf_generator
        html = pdf_generator._render_html(dict(self.CTX),
                                          modelos=["contabil", "fiscal"])
        assert html.find("Dashboard Fiscal") < html.find("Dashboard Cont")

    def test_chave_desconhecida_levanta_nomeando_as_validas(self):
        # degradar em silencio produziria um PDF sem a pagina pedida,
        # descoberto so na frente do cliente.
        import pdf_generator
        with pytest.raises(ValueError) as e:
            pdf_generator._render_html(dict(self.CTX), modelos=["fiscaal"])
        assert "fiscaal" in str(e.value)
        for k in pdf_generator.MODELOS_KEYS:
            assert k in str(e.value)

    def test_selecao_vazia_levanta(self):
        import pdf_generator
        with pytest.raises(ValueError):
            pdf_generator._render_html(dict(self.CTX), modelos=[])

    def test_documento_tem_um_unico_head(self):
        # o <head> sai do base.html UMA vez e vale pro documento todo — e
        # por isso que todo template de modelo TEM que estender base.html.
        import pdf_generator
        html = pdf_generator._render_html(dict(self.CTX))
        assert html.count("<head>") == 1 and html.count("</head>") == 1
        assert html.count("<body>") == 1 and html.count("</body>") == 1

    def test_registro_tem_chaves_unicas_e_templates_existentes(self):
        import pdf_generator
        keys = [m["key"] for m in pdf_generator.MODELOS]
        assert len(keys) == len(set(keys))
        for m in pdf_generator.MODELOS:
            assert (pdf_generator.TEMPLATES_DIR / m["template"]).exists()
            assert m["label"]

    def test_funil_propaga_modelos_ate_render_html(self, monkeypatch):
        # o FIO: gerar_pdf_empresa -> _build_html_for_empresa -> _render_html.
        # Sem esse teste, um kwarg esquecido no meio degradaria em silencio
        # pro default.
        import pdf_generator
        visto = {}
        monkeypatch.setattr(pdf_generator, "_load_data",
                            lambda *a, **k: tuple(pd.DataFrame() for _ in range(12)))
        monkeypatch.setattr(pdf_generator, "_load_spark_data", lambda *a, **k: {})
        monkeypatch.setattr(pdf_generator, "_load_data_yoy", lambda *a, **k: {})
        monkeypatch.setattr(pdf_generator, "_build_context", lambda *a, **k: {})
        monkeypatch.setattr(pdf_generator, "_html_to_pdf", lambda h: b"%PDF")

        def _spy(ctx, modelos=None):
            visto["modelos"] = modelos
            return "<html></html>"
        monkeypatch.setattr(pdf_generator, "_render_html", _spy)

        pdf_generator.gerar_pdf_empresa([1], "2025-01", "2025-02", None,
                                        modelos=["fiscal"])
        assert visto["modelos"] == ["fiscal"]


class TestModeloDetalhamentoImpostos:
    """Modelo de PDF de Detalhamento de Impostos.

    Universo CARGA, montado pelo MESMO helper do bloco Impostos do Streamlit
    (etl.metrics.tabela_impostos) — o comportamento do helper e cravado em
    TestTabelaImpostos; aqui fica a FIACAO: o mart chega, o ctx sai, e o card
    % Impostos nao e recalculado.
    """
    P = ["2025-01", "2025-02"]

    def _ctx(self, monkeypatch, imp_df=None, impd_df=None):
        import pdf_generator
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda *a, **k: "")
        empty = pd.DataFrame()
        fat = pd.DataFrame({"competencia": self.P,
                            "valo_saidas": [100000.0, 100000.0],
                            "valo_servicos": [0.0, 0.0]})
        impd = empty if impd_df is None else impd_df
        spark = {"fat": fat, "imp": impd, "folha": empty, "func": empty,
                 "bal": empty, "encargos": empty, "dre": empty}
        return pdf_generator._build_context(
            fat, impd, empty, empty, empty, spark, self.P, self.P,
            "Teste", "00", self.P[0], self.P[-1], bal_serie_df=empty,
            imp_df=empty if imp_df is None else imp_df)

    def test_modelo_esta_no_registro_com_template_existente(self):
        import pdf_generator
        m = next(m for m in pdf_generator.MODELOS if m["key"] == "impostos")
        assert m["template"] == "impostos.html"
        assert (pdf_generator.TEMPLATES_DIR / m["template"]).exists()

    def test_card_de_impostos_e_o_MESMO_objeto_do_hero_fiscal(self, monkeypatch):
        # o card nao e recalculado no modelo novo: sem isso, dois numeros com
        # o mesmo nome poderiam divergir entre paginas do MESMO PDF.
        ctx = self._ctx(monkeypatch)
        card_fiscal = next(c for c in ctx["hero_fiscal"] if c["key"] == "imp")
        assert ctx["hero_impostos"] == [card_fiscal]
        assert ctx["hero_impostos"][0] is card_fiscal

    def test_tabela_chega_formatada_com_total_e_percentual(self, monkeypatch):
        imp = pd.DataFrame({"codi_emp": [1, 1], "competencia": self.P,
                            "codi_imp": [1, 1], "srec_sim": [1000.0, 2000.0]})
        ctx = self._ctx(monkeypatch, imp_df=imp)
        assert ctx["imp_tem_dado"] is True
        rows = {r["label"]: r for r in ctx["imp_rows"]}
        assert rows["ICMS"]["values"] == ["R$ 1.000,00", "R$ 2.000,00"]
        assert rows["TOTAL"]["destaque"] is True
        # a linha de % NUNCA sai formatada como R$ (casamento por NOME, a
        # mesma regra do styler do app)
        assert rows[metrics_LINHA_PCT]["values"] == ["1,0%", "2,0%"]
        assert rows[metrics_LINHA_PCT]["destaque"] is True

    def test_com_ISS_o_PDF_nao_carrega_a_nota_de_fonte(self, monkeypatch):
        # As notas explicativas sairam do PDF. O que
        # protege o leitor sao os ROTULOS, visiveis ao lado do numero — o
        # rodape so competia com eles. No APP as notas continuam (la o leitor opera).
        # A proveniencia do ISS segue MEDIDA (iss_fontes no metrics); o que
        # saiu foi a exibicao.
        impd = pd.DataFrame({"codi_emp": [1, 1], "competencia": self.P,
                             "fonte": ["balancete", "guia+retido"],
                             "valo_impostos": [100.0, 200.0],
                             "base_receita": [1000.0, 2000.0],
                             "valo_iss": [100.0, 200.0]})
        ctx = self._ctx(monkeypatch, impd_df=impd)
        assert "ISS pela carga tributária" not in ctx.get("imp_note", "")
        assert "imp_iss_note" not in ctx
        # o template tambem nao referencia mais o subtitulo de fonte do chart
        src = open("templates/impostos.html", encoding="utf-8").read()
        assert "imp_iss_note" not in src

    def test_sem_ISS_o_PDF_tambem_nao_carrega_a_caption_de_guia(self, monkeypatch):
        # a condicao ESPELHO: sem componente de ISS a caption era outra
        # ("impostos que a empresa recolheu..."). As duas sumiram.
        imp = pd.DataFrame({"codi_emp": [1], "competencia": ["2025-01"],
                            "codi_imp": [1], "srec_sim": [500.0]})
        ctx = self._ctx(monkeypatch, imp_df=imp)
        assert "recolheu no período" not in ctx.get("imp_note", "")
        assert "retenções de terceiros" not in ctx.get("imp_note", "")

    def test_so_retencao_tem_mensagem_propria_nao_o_generico(self, monkeypatch):
        # guias EXISTEM, so nao ha imposto proprio — "sem dados" mentiria.
        imp = pd.DataFrame({"codi_emp": [1], "competencia": ["2025-01"],
                            "codi_imp": [18], "srec_sim": [800.0]})
        ctx = self._ctx(monkeypatch, imp_df=imp)
        assert ctx["imp_tem_dado"] is False
        assert "apenas retenções de terceiros" in ctx["imp_empty_msg"]

    def test_sem_dado_nenhum_cai_no_generico(self, monkeypatch):
        ctx = self._ctx(monkeypatch)
        assert ctx["imp_tem_dado"] is False
        assert "Sem dados de impostos" in ctx["imp_empty_msg"]

    def test_a_pagina_nomeia_UM_universo_so(self, monkeypatch):
        """Card, chart e tabela medem a GUIA — nao ha mais dois.

        A `NOTA_UNIVERSO_IMPOSTOS` existia pra declarar a separacao e MORREU
        com ela: nota que afirma uma divergencia inexistente e pior que nota
        nenhuma. O qualificador do titulo do chart saiu pelo mesmo motivo — ele
        NOMEAVA ERRADO o que o chart passou a mostrar.
        """
        import pdf_generator
        imp = pd.DataFrame({"codi_emp": [1], "competencia": ["2025-01"],
                            "codi_imp": [1], "srec_sim": [500.0]})
        ctx = self._ctx(monkeypatch, imp_df=imp)
        assert not hasattr(pdf_generator, "NOTA_UNIVERSO_IMPOSTOS")
        src = open("templates/impostos.html", encoding="utf-8").read()
        # No TITULO, nao no arquivo: o comentario acima do bloco cita o rotulo
        # antigo de proposito, pra explicar POR QUE ele morreu.
        titulo = next(ln for ln in src.splitlines() if "chart_block(" in ln)
        assert "carga destacada + IRPJ/CSLL" not in titulo   # rotulo que mentia
        assert "Impostos recolhidos por m" in titulo         # o universo unico
        # a linha de % da tabela segue nomeando o universo dela
        rows = {r["label"] for r in ctx["imp_rows"]}
        assert metrics_LINHA_PCT in rows

    def test_sem_tabela_tambem_nao_ha_nota(self, monkeypatch):
        # a condicao ESPELHO: sem tabela o rodape ja era vazio, e continua —
        # guard contra nota que volte a aparecer por um caminho so.
        import pdf_generator
        ctx = self._ctx(monkeypatch)
        assert ctx["imp_tem_dado"] is False
        assert not hasattr(pdf_generator, "NOTA_UNIVERSO_IMPOSTOS")
        assert ctx["imp_note"] == ""

    def test_pagina_renderiza_isolada(self, monkeypatch):
        import pdf_generator
        imp = pd.DataFrame({"codi_emp": [1, 1], "competencia": self.P,
                            "codi_imp": [1, 1], "srec_sim": [1000.0, 2000.0]})
        ctx = self._ctx(monkeypatch, imp_df=imp)
        html = pdf_generator._render_html(ctx, modelos=["impostos"])
        assert html.count('<div class="page"') == 1
        assert "Detalhamento de Impostos" in html
        assert "ICMS" in html and "R$ 1.000,00" in html


class TestModeloDetalhamentoFolha:
    """Modelo de PDF de Detalhamento de Folha.

    Contrato = a TELA: mesmo helper (etl.metrics.folha_por_linha), mesma
    tabela unica de custo, mesmo chart empilhado. UM custo, UM percentual.
    Aqui ficam o contrato do ctx e as bordas.
    """
    P = ["2025-11", "2025-12"]

    def _rub(self, rows):
        return pd.DataFrame(rows, columns=[
            "codi_emp", "competencia", "tipo_process", "i_eventos", "nome",
            "classificacao", "desc_classificacao", "valor_p", "valor_d",
            "valor_i", "valor_id", "qtd_informada"])

    def _yoy_prev(self, folha=700.0, inss=60.0, fgts=0.0):
        """Janela -12m com folha e guia — o que `_load_data_yoy` traz.

        `folha_pm` do ano anterior degrada pro `mart.folha_mensal` quando nao
        ha rubrica carregada (retrocompat de `custo_folha_pm`), que e o caso
        de empresa antiga sem o mart novo naquela janela.
        """
        empty = pd.DataFrame()
        prev = [str(pd.Period(p, freq="M") - 12) for p in self.P]
        return {
            "fat": empty, "impd": empty, "rub": empty, "dre": empty,
            "folha": pd.DataFrame({"competencia": prev, "tipo_process": [11, 11],
                                   "valo_bruto": [folha, folha]}),
            "encargos": pd.DataFrame({"codi_emp": [1, 1], "competencia": prev,
                                      "inss_empresa": [inss, inss],
                                      "fgts": [fgts, fgts],
                                      "fgts_resc": [0.0, 0.0]}),
        }

    def _ctx(self, monkeypatch, rub=None, enc=None, regime=None, yoy=None):
        import pdf_generator
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda *a, **k: "")
        empty = pd.DataFrame()
        fat = pd.DataFrame({"competencia": self.P,
                            "valo_saidas": [100000.0, 100000.0],
                            "valo_servicos": [0.0, 0.0]})
        folha = pd.DataFrame({"competencia": self.P, "tipo_process": [11, 11],
                              "valo_bruto": [1000.0, 1000.0]})
        enc = empty if enc is None else enc
        spark = {"fat": fat, "imp": empty, "folha": folha, "func": empty,
                 "bal": empty, "encargos": enc, "dre": empty}
        return pdf_generator._build_context(
            fat, empty, folha, empty, empty, spark, self.P, self.P,
            "Teste", "00", self.P[0], self.P[-1], bal_serie_df=empty,
            encargos_df=enc, regime_df=empty if regime is None else regime,
            rub_df=empty if rub is None else rub, yoy_dfs=yoy)

    def _cenario(self):
        rub = self._rub([
            [201, "2025-11", 11, 1, "SALARIO BASE", 1, "Salários", 1000.0, 0, 0, 0, None],
            [201, "2025-12", 11, 1, "SALARIO BASE", 1, "Salários", 1000.0, 0, 0, 0, None],
            [201, "2025-12", 52, 12, "13 SALARIO INTEGRAL", 4, "13º Salário",
             800.0, 0, 0, 0, None],
            [201, "2025-11", 11, 202, "HORA EXTRA A 60%", 3, "Horas Extras",
             500.0, 0, 0, 0, 40.0],
            [201, "2025-12", 11, 202, "HORA EXTRA A 60%", 3, "Horas Extras",
             300.0, 0, 0, 0, 0.0],
        ])
        enc = pd.DataFrame({"codi_emp": [201, 201], "competencia": self.P,
                            "inss_empresa": [200.0, 250.0], "fgts": [80.0, 90.0],
                            "fgts_resc": [0.0, 0.0]})
        return rub, enc

    def test_modelo_esta_no_registro_com_template_existente(self):
        import pdf_generator
        m = next(m for m in pdf_generator.MODELOS if m["key"] == "folha")
        assert m["template"] == "folha.html"
        assert (pdf_generator.TEMPLATES_DIR / m["template"]).exists()

    def test_card_folenc_e_o_MESMO_objeto_do_hero_fiscal(self, monkeypatch):
        # o card e o PAR da linha "% sobre Faturamento": os dois
        # usam a MESMA base. Recalcular abriria espaco pros dois divergirem.
        rub, enc = self._cenario()
        ctx = self._ctx(monkeypatch, rub=rub, enc=enc)
        card = next(c for c in ctx["hero_fiscal"] if c["key"] == "folenc")
        # A faixa virou CUSTO TOTAL -> % -> ENCARGOS -> FUNCIONARIOS,
        # entao o card de % saiu da cabeca pra 2a posicao. O que o teste trava
        # nao e a posicao — e a IDENTIDADE DE OBJETO: as duas paginas exibem
        # literalmente o mesmo dict, e nao ha como divergirem.
        assert ctx["hero_folha"][1] is card
        # e o de Funcionarios idem, pelo mesmo motivo
        card_f = next(c for c in ctx["hero_fiscal"] if c["key"] == "func")
        assert ctx["hero_folha"][3] is card_f

    def test_a_tabela_de_cima_e_SO_o_total_e_o_percentual(self, monkeypatch):
        """Em cima fica o total, nada mais.

        A decomposicao em 8 linhas saiu do topo — quem le quer o custo do mes
        e o quanto ele pesa, e antes precisava somar a coluna de cabeca. As
        rubricas que sobrevivem estao na tabela de baixo, ja CONTIDAS neste
        numero.
        """
        rub, enc = self._cenario()
        ctx = self._ctx(monkeypatch, rub=rub, enc=enc)
        labels = [r["label"] for r in ctx["folha_rows"]]
        assert labels == [LINHA_FOLHA_ENCARGOS, LINHA_PCT_FAT]
        rows = {r["label"]: r for r in ctx["folha_rows"]}
        # dez: 1000 salario + 800 13o + 300 HE + 340 guia = 2440.
        assert rows[LINHA_FOLHA_ENCARGOS]["values"][1] == "R$ 2.440,00"
        assert rows[LINHA_FOLHA_ENCARGOS]["destaque"] is True

    def test_principais_custos_saem_embaixo_contidos_no_total(self, monkeypatch):
        # o cenario tem Hora Extra (500 em nov, 300 em dez) — destaque; e
        # Folha/13o/Encargos, que NAO sao destaque (sao o proprio total).
        rub, enc = self._cenario()
        ctx = self._ctx(monkeypatch, rub=rub, enc=enc)
        labels = [r["label"] for r in ctx["folha_destaque_rows"]]
        assert labels == ["Hora Extra"]
        rows = {r["label"]: r for r in ctx["folha_destaque_rows"]}
        assert rows["Hora Extra"]["values"] == ["R$ 500,00", "R$ 300,00"]
        # CONTIDO: a linha de destaque e menor que o total da mesma coluna
        assert rows["Hora Extra"]["destaque"] is False

    def test_a_ordem_dos_destaques_e_a_da_MESA(self, monkeypatch):
        rub = self._rub([
            [201, self.P[0], 11, 37, "COMISSOES", 23, "Comissão", 300.0, 0, 0, 0, None],
            [201, self.P[0], 11, 202, "HORA EXTRA", 3, "Horas Extras", 200.0, 0, 0, 0, 8.0],
            [201, self.P[0], 11, 5, "FERIAS GOZADAS", 6, "Férias Gozadas",
             100.0, 0, 0, 0, None],
        ])
        ctx = self._ctx(monkeypatch, rub=rub)
        assert [r["label"] for r in ctx["folha_destaque_rows"]] == [
            "Férias", "Hora Extra", "Comissão"]

    def test_rubrica_ausente_no_periodo_NAO_vira_linha(self, monkeypatch):
        # empresa que so paga salario nao carrega cinco linhas de zeros.
        rub = self._rub([[201, self.P[0], 11, 1, "SALARIO BASE", 1, "Salários",
                          1000.0, 0, 0, 0, None]])
        ctx = self._ctx(monkeypatch, rub=rub)
        assert ctx["folha_destaque_rows"] == []
        # e o bloco inteiro nao renderiza: titulo de contencao sem linha e ruido
        assert ctx["folha_tem_destaque"] is False

    def test_o_titulo_do_bloco_de_destaques_vai_PRO_TEMPLATE(self):
        """No PDF o subtitulo de contencao saiu.

        O subtitulo era descrito como a UNICA protecao contra somar
        o detalhe com o total. No PDF ela passou a ser o TITULO do bloco --
        "Principais custos da folha" nao promete totalidade, ao contrario de um
        "Composicao" ou "Detalhamento" -- somado a POSICAO, abaixo do total.
        No APP o subtitulo continua, e a constante segue no
        metrics servindo aquele consumer.
        """
        import pathlib
        tpl = (pathlib.Path(__file__).resolve().parent.parent / "templates"
               / "folha.html").read_text(encoding="utf-8")
        assert "folha_destaque_titulo" in tpl
        assert "folha_destaque_subtitulo" not in tpl
        assert TITULO_DESTAQUES_FOLHA == "Principais custos da folha"
        assert SUBTITULO_DESTAQUES_FOLHA == "valores já incluídos em Folha + Encargos"

    def test_o_pdf_nao_reimplementa_a_selecao(self):
        # o corte mora no metrics (fonte unica com o app); o PDF so renderiza.
        import pdf_generator
        src = open(pdf_generator.__file__, encoding="utf-8").read()
        assert "_folha_destaques(" in src
        assert "RUBRICAS_DESTAQUE_FOLHA" not in src
        assert len(RUBRICAS_DESTAQUE_FOLHA) == 5

    # ── YoY nos charts de folha ──────────────────────
    def test_os_dois_charts_de_folha_entram_no_YoY(self, monkeypatch):
        """O padrao YoY vale pros charts de folha do PDF.

        O chart do fiscal compara `folha_pm` (folha de custo, sem encargos) e
        o do Detalhamento compara o CUSTO TOTAL. Nenhum numero novo aparece: a
        serie do ano anterior e a MESMA que ja alimenta o delta YoY do card
        (`S_prev`, de `_load_data_yoy`). Uma fonte so pro mesmo numero — card e
        chart nao podem divergir com o mesmo nome na tela.
        """
        import pdf_generator
        chamadas = []
        orig = pdf_generator._make_bar_yoy

        def espia(labels, atual, anterior, *a, **k):
            chamadas.append({"atual": [float(v) for v in atual],
                             "anterior": [float(v) for v in anterior],
                             "kw": dict(k)})
            return orig(labels, atual, anterior, *a, **k)

        monkeypatch.setattr(pdf_generator, "_make_bar_yoy", espia)
        rub, enc = self._cenario()
        self._ctx(monkeypatch, rub=rub, enc=enc, yoy=self._yoy_prev())
        # 4 charts YoY, na ordem de construcao: faturamento, impostos, folha
        # do fiscal e folha do detalhamento
        assert len(chamadas) == 4
        # fiscal: folha de CUSTO (sem encargos). nov = 1000 salario + 500 HE;
        # dez = 1000 + 800 (13o) + 300 HE. Anterior = 700 em cada mes.
        assert chamadas[2]["atual"] == [1500.0, 2100.0]
        assert chamadas[2]["anterior"] == [700.0, 700.0]
        # detalhamento: CUSTO TOTAL = folha + guia, nos DOIS anos
        # (nov 1500 + 280; dez 2100 + 340 | anterior 700 + 60)
        assert chamadas[3]["atual"] == [1780.0, 2440.0]
        assert chamadas[3]["anterior"] == [760.0, 760.0]

    def test_o_chart_do_detalhamento_E_a_linha_da_tabela(self, monkeypatch):
        """A barra azul e a linha "Folha + Encargos" — por CONSTRUCAO.

        Se o chart recalcular o total por outro caminho, a pagina passa a ter
        dois numeros com o mesmo nome (a licao do ISS). Nao crava valor: le a
        LINHA que o PDF exibe e exige a serie identica.
        """
        import pdf_generator
        chamadas = []
        orig = pdf_generator._make_bar_yoy

        def espia(labels, atual, anterior, *a, **k):
            chamadas.append([float(v) for v in atual])
            return orig(labels, atual, anterior, *a, **k)

        monkeypatch.setattr(pdf_generator, "_make_bar_yoy", espia)
        rub, enc = self._cenario()
        ctx = self._ctx(monkeypatch, rub=rub, enc=enc, yoy=self._yoy_prev())
        linha = next(r for r in ctx["folha_rows"]
                     if r["label"] == LINHA_FOLHA_ENCARGOS)
        assert [pdf_generator._fmt_brl(v) for v in chamadas[3]] == linha["values"]

    def test_empresa_sem_ano_anterior_degrada_para_serie_unica(self, monkeypatch):
        """Guard: empresa nova nao ganha barra vermelha de R$ 0,00.

        Ano ausente nao vira barra de altura zero nem chip na legenda — ele
        simplesmente nao existe no chart, e a pagina le como o chart de sempre.
        """
        import pdf_generator
        kw = []
        orig = pdf_generator._make_bar_yoy
        monkeypatch.setattr(
            pdf_generator, "_make_bar_yoy",
            lambda *a, **k: (kw.append(dict(k)) or orig(*a, **k)))
        rub, enc = self._cenario()
        self._ctx(monkeypatch, rub=rub, enc=enc)      # sem janela -12m
        assert kw[2]["tem_anterior"] is False
        assert kw[3]["tem_anterior"] is False
        # e o chart NAO some por isso: o ano corrente sozinho segue desenhando
        assert kw[2]["tem_atual"] is True and kw[3]["tem_atual"] is True

    def test_guard_dos_dois_anos_no_chart_do_fiscal(self, monkeypatch):
        """So o ANO ANTERIOR tem folha: a barra vermelha tem de aparecer.

        Sem rubrica na janela nao ha tabela nem chart de
        detalhamento — mas o chart do FISCAL ainda tem o ano anterior a
        mostrar, e apaga-lo esconderia dado real.
        """
        import pdf_generator
        kw = []
        orig = pdf_generator._make_bar_yoy
        monkeypatch.setattr(
            pdf_generator, "_make_bar_yoy",
            lambda *a, **k: (kw.append(dict(k)) or orig(*a, **k)))
        ctx = self._ctx(monkeypatch, yoy=self._yoy_prev())
        assert ctx["folha_tem_rubrica"] is False
        assert kw[2]["tem_anterior"] is True
        assert kw[2]["tem_atual"] is True     # o _ctx sempre passa folha_df

    def test_hora_extra_exibe_SO_o_valor_pago(self, monkeypatch):
        # A qtd de horas sai da
        # celula. A tabela e de CUSTO — a coluna mostra dinheiro, e a hora
        # tinha um formato so dela no meio de 10 linhas em R$. Contrato de
        # PARIDADE: vale nos dois lados (app e PDF).
        rub, enc = self._cenario()
        ctx = self._ctx(monkeypatch, rub=rub, enc=enc)
        # a Hora Extra e uma das cinco de DESTAQUE — mudou de
        # tabela, nao de formato: continua sendo dinheiro, sem a qtd de horas.
        he = next(r for r in ctx["folha_destaque_rows"]
                  if r["label"] == "Hora Extra")
        assert he["values"] == ["R$ 500,00", "R$ 300,00"]
        assert not any("h ·" in v for v in he["values"])

    def test_linha_opcional_sem_movimento_nao_aparece(self, monkeypatch):
        # o corte mora no helper — o PDF herda identico, sem decidir nada.
        # Ele se ve na tabela de DESTAQUES: o cenario
        # tem Hora Extra e nao tem Comissao.
        rub, enc = self._cenario()
        ctx = self._ctx(monkeypatch, rub=rub, enc=enc)
        labels = [r["label"] for r in ctx["folha_destaque_rows"]]
        assert "Comissão" not in labels and "Hora Extra" in labels

    def test_percentual_acima_de_100_aparece_como_esta(self, monkeypatch):
        # Acima de 100% e VERDADE quando a folha consome mais que
        # a receita de nota. Nunca capar em 100% nem esconder.
        rub, enc = self._cenario()
        fat_baixo = pd.DataFrame({"competencia": self.P,
                                  "valo_saidas": [1000.0, 1000.0],
                                  "valo_servicos": [0.0, 0.0]})
        import pdf_generator
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda *a, **k: "")
        empty = pd.DataFrame()
        folha = pd.DataFrame({"competencia": self.P, "tipo_process": [11, 11],
                              "valo_bruto": [1000.0, 1000.0]})
        ctx = pdf_generator._build_context(
            fat_baixo, empty, folha, empty, empty,
            {"fat": fat_baixo, "imp": empty, "folha": folha, "func": empty,
             "bal": empty, "encargos": enc, "dre": empty},
            self.P, self.P, "T", "0", self.P[0], self.P[-1],
            bal_serie_df=empty, encargos_df=enc, rub_df=rub)
        pct = next(r for r in ctx["folha_rows"] if r["label"] == LINHA_PCT_FAT)
        assert pct["values"][1] == "244,0%"          # 2440/1000

    def test_nota_do_simples_nao_vai_pro_PDF_mas_o_predicado_vive(self):
        # A nota do Simples ficou SO no app — o predicado
        # encargos_sem_inss_patronal segue intacto no metrics, e o app o consome.
        import pdf_generator
        from etl.metrics import encargos_sem_inss_patronal
        src = open(pdf_generator.__file__, encoding="utf-8").read()
        assert "encargos_sem_inss_patronal" not in src
        assert "NOTA_ENCARGOS_SIMPLES" not in src
        assert callable(encargos_sem_inss_patronal)
        app = open("app.py", encoding="utf-8").read()
        assert "NOTA_ENCARGOS_GUIA" in app          # o app MANTEM as notas

    def test_sem_rubrica_degrada_pro_aviso_sem_crash(self, monkeypatch):
        # empresa sem carga do mart: tabela so de encargos NAO e a
        # visao de folha.
        ctx = self._ctx(monkeypatch)
        assert ctx["folha_tem_rubrica"] is False
        assert ctx["folha_rows"] == [] and ctx["folha_notas"] == ""
        # texto NEUTRO: o PDF vai pro cliente, entao nada de imperativo de
        # operador ("rode o ETL") — esse fica no app, onde o leitor opera.
        assert ctx["folha_empty_msg"] == "Sem folha por rubrica no período."
        assert "ETL" not in ctx["folha_empty_msg"]

    def test_pagina_renderiza_isolada(self, monkeypatch):
        import pdf_generator
        rub, enc = self._cenario()
        ctx = self._ctx(monkeypatch, rub=rub, enc=enc)
        html = pdf_generator._render_html(ctx, modelos=["folha"])
        assert html.count('<div class="page"') == 1
        assert "Detalhamento de Folha" in html
        assert "R$ 2.440,00" in html          # CUSTO TOTAL de dez
        assert "R$ 500,00" in html            # Hora Extra: SO o valor pago
        assert "40h" not in html              # a qtd saiu da celula


class TestLayoutFiscal:
    """Novo layout do fiscal (movimento MINIMO).

    O combinado Faturamento x Gastos morre e no lugar entra Faturamento
    SOZINHO em faixa full-width; "Impostos por mes" sai da pagina e passa a
    viver so no Detalhamento. Faixa de cards, Folha e Movimentacao INTOCADAS.
    """
    def test_faixa_segue_com_os_quatro_cards(self, monkeypatch):
        import pdf_generator
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda *a, **k: "")
        empty = pd.DataFrame()
        per = ["2025-01", "2025-02"]
        fat = pd.DataFrame({"competencia": per, "valo_saidas": [1000.0, 1000.0],
                            "valo_servicos": [0.0, 0.0]})
        ctx = pdf_generator._build_context(
            fat, empty, empty, empty, empty,
            {"fat": fat, "imp": empty, "folha": empty, "func": empty,
             "bal": empty, "encargos": empty, "dre": empty},
            per, per, "T", "0", per[0], per[-1], bal_serie_df=empty)
        assert [c["key"] for c in ctx["hero_fiscal"]] == [
            "fat", "imp", "folenc", "func"]
        # o card de impostos NAO migrou: aparece nas duas paginas, e e o
        # MESMO objeto — duas paginas do mesmo PDF nunca podem divergir.
        assert ctx["hero_impostos"][0] is next(
            c for c in ctx["hero_fiscal"] if c["key"] == "imp")

    def test_fiscal_nao_referencia_gastos_nem_impostos(self):
        src = open("templates/fiscal.html", encoding="utf-8").read()
        assert "charts.fat_gastos" not in src
        assert "charts.impostos" not in src
        assert "charts.fat" in src
        # Folha e Movimentacao NAO se movem
        assert "charts.folha" in src and "Movimenta" in src

    def test_impostos_por_mes_ganhou_casa_no_detalhamento(self):
        src = open("templates/impostos.html", encoding="utf-8").read()
        assert "charts.impostos" in src        # a serie do CARD (destacado)

    def test_o_chart_de_guias_MORREU_e_a_tabela_ficou(self):
        """Um chart so, em YoY.

        O par "carga destacada" x "guias recolhidas" era DUPLICACAO no Simples.
        O universo GUIA nao se perdeu: ele E a tabela da secao.
        """
        src = open("templates/impostos.html", encoding="utf-8").read()
        assert "charts.imp_detalhe" not in src
        # UM chart renderizado na pagina. Conta a CHAMADA da macro em vez de
        # banir o titulo por string: o comentario do template cita o chart
        # morto pra explicar por que ele saiu, e um grep de texto nao sabe
        # separar documentacao de render. Contar chamadas mede o que importa
        # e nao briga com doc.
        assert src.count("{{ chart_block(") == 1
        assert "imp_rows" in src                    # a tabela por tributo FICA
        # o titulo da tabela declara o universo dela (vem do ctx, nao e
        # generico)
        assert "{{ imp_titulo }}" in src
        # e o vazio que era do chart morto virou o vazio da TABELA — sem isso
        # o `so_retencao` ficaria sem superficie nenhuma
        assert "imp_empty_msg" in src

    def test_bargap_mantem_a_barra_estreita_em_janela_curta(self):
        # 1 mes na faixa full-width viraria uma barra-paredao de 85% da
        # largura. A regra fixa a fracao da faixa que a barra ocupa (~7%, o
        # que 12 meses produzem naturalmente) e deriva o bargap.
        import pdf_generator

        def largura(n):
            return 1.0 - pdf_generator._bargap_faixa(n)

        # 1 mes ocupa a MESMA fracao da faixa que 1 de 12 ocuparia
        assert largura(1) == pytest.approx(largura(12) / 12)
        assert largura(3) == pytest.approx(largura(12) / 4)
        # janela cheia cai no comportamento HISTORICO (bargap 0.15), entao o
        # PDF de 12 meses nao muda em nada
        assert pdf_generator._bargap_faixa(12) == pytest.approx(0.15)
        assert pdf_generator._bargap_faixa(24) == pytest.approx(0.15)

    def test_make_bar_aplica_a_regra_em_TODO_chart_de_barra(self):
        # o defeito e da JANELA, nao do slot: sem isso o fiscal de 1 mes teria
        # o Faturamento corrigido ao lado de uma Folha solida.
        import pdf_generator
        um = pdf_generator._make_bar(["Jan/25"], [100.0], "#000")
        doze = pdf_generator._make_bar([f"M{i}" for i in range(12)],
                                       [100.0] * 12, "#000")
        assert um.layout.bargap == pytest.approx(pdf_generator._bargap_faixa(1))
        # janela cheia = bargap HISTORICO, byte a byte
        assert doze.layout.bargap == pytest.approx(0.15)

    def test_titulos_dos_dois_charts_de_impostos_nomeiam_a_base(self):
        # Sao DOIS numeros de impostos por mes na mesma pagina. Titulo quase
        # igual com numero diferente E o defeito: o leitor compara
        # os charts antes de chegar na nota sob a tabela.
        from etl.metrics import TITULO_IMPOSTOS_GUIAS
        src = open("templates/impostos.html", encoding="utf-8").read()
        assert 'chart_block("Impostos recolhidos por m' in src   # base do CARD
        assert "{{ imp_titulo }}" in src                         # base da TABELA
        assert "guias recolhidas" in TITULO_IMPOSTOS_GUIAS.lower()

    def test_paleta_de_series_nao_cicla_em_uso_real(self):
        # com 4 cores e 6 tributos, COFINS e ISS saiam da MESMA cor no mesmo
        # grafico — duas series indistinguiveis. A tabela
        # de folha chega a 11 linhas e a de tributos passou de 6.
        import pdf_generator
        assert len(pdf_generator.PALETA_SERIES) >= 11
        assert len(set(pdf_generator.PALETA_SERIES)) == len(pdf_generator.PALETA_SERIES)

    def test_faixa_curta_encolhe_o_bloco_e_o_render_acompanha(self, monkeypatch):
        # 1-3 meses -> bloco em meia
        # largura, centrado. O limiar mora no ctx e o template CONSOME: se o
        # template re-decidisse, um render 1000x270 poderia cair num slot de
        # meia largura e letterboxar.
        import pdf_generator
        tamanhos = {}
        monkeypatch.setattr(pdf_generator, "_fig_to_b64",
                            lambda fig, **k: tamanhos.setdefault(k.get("width"), 0) or "B64")
        empty = pd.DataFrame()

        def ctx_de(n):
            per = [f"2025-{m:02d}" for m in range(1, n + 1)]
            fat = pd.DataFrame({"competencia": per,
                                "valo_saidas": [1000.0] * n,
                                "valo_servicos": [0.0] * n})
            return pdf_generator._build_context(
                fat, empty, empty, empty, empty,
                {"fat": fat, "imp": empty, "folha": empty, "func": empty,
                 "bal": empty, "encargos": empty, "dre": empty},
                per, per, "T", "0", per[0], per[-1], bal_serie_df=empty)

        assert ctx_de(1)["fat_faixa_curta"] is True
        assert ctx_de(3)["fat_faixa_curta"] is True
        assert ctx_de(4)["fat_faixa_curta"] is False
        assert ctx_de(12)["fat_faixa_curta"] is False
        # o template le o flag, nao recalcula o limiar
        src = open("templates/fiscal.html", encoding="utf-8").read()
        assert "fat_faixa_curta" in src
        assert "n_months <= 3" not in src


class TestFaixasPadronizadas:
    """Esqueleto UNICO dos dois detalhamentos:
    FAIXA (4 cards) -> GRAFICO -> DETALHAMENTO (tabela).

    A faixa e o card de % (o MESMO dict do hero fiscal) + os 3 maiores itens
    do periodo, lidos da PROPRIA tabela exibida abaixo.
    """
    P = ["2025-01", "2025-02"]

    def _ctx(self, monkeypatch, imp_df=None, rub=None, enc=None):
        import pdf_generator
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda *a, **k: "B64")
        empty = pd.DataFrame()
        fat = pd.DataFrame({"competencia": self.P,
                            "valo_saidas": [100000.0, 100000.0],
                            "valo_servicos": [0.0, 0.0]})
        folha = pd.DataFrame({"competencia": self.P, "tipo_process": [11, 11],
                              "valo_bruto": [1000.0, 1000.0]})
        enc = empty if enc is None else enc
        return pdf_generator._build_context(
            fat, empty, folha, empty, empty,
            {"fat": fat, "imp": empty, "folha": folha, "func": empty,
             "bal": empty, "encargos": enc, "dre": empty},
            self.P, self.P, "T", "0", self.P[0], self.P[-1], bal_serie_df=empty,
            encargos_df=enc, imp_df=empty if imp_df is None else imp_df,
            rub_df=empty if rub is None else rub)

    def test_faixa_de_impostos_e_card_mais_os_3_maiores(self, monkeypatch):
        imp = pd.DataFrame({
            "codi_emp": [1] * 8, "competencia": self.P * 4,
            # IMP_NOMES: 1=ICMS, 5=COFINS, 6=CSLL, 7=IRPJ
            "codi_imp": [1, 1, 5, 5, 6, 6, 7, 7],
            "srec_sim": [100.0, 100.0, 900.0, 900.0, 500.0, 500.0, 50.0, 50.0]})
        ctx = self._ctx(monkeypatch, imp_df=imp)
        assert ctx["hero_impostos"][0]["key"] == "imp"
        destaques = [c for c in ctx["hero_impostos"] if c["key"] == "destaque"]
        assert [d["label"] for d in destaques] == ["COFINS", "CSLL", "ICMS"]
        assert destaques[0]["value"] == "R$ 1.800,00"
        # o destaque nao tem spark; o CONTEXTO declara o ranking, senao
        # "FOLHA R$ 5,8M" ficava ao lado de "R$ 9,0M de folha" — a mesma
        # palavra com dois sentidos na mesma faixa.
        assert all(d["spark"] == "" for d in destaques)
        assert [d["context"] for d in destaques] == [
            "maior item do período", "2º maior item", "3º maior item"]

    def test_menos_de_3_tributos_rende_menos_cards_nunca_enchimento(self, monkeypatch):
        # o caso Simples: UM tributo com valor.
        imp = pd.DataFrame({"codi_emp": [1, 1], "competencia": self.P,
                            "codi_imp": [44, 44], "srec_sim": [500.0, 500.0]})
        ctx = self._ctx(monkeypatch, imp_df=imp)
        destaques = [c for c in ctx["hero_impostos"] if c["key"] == "destaque"]
        assert [d["label"] for d in destaques] == ["Simples Nacional"]

    def test_faixa_de_folha_e_o_espelho(self, monkeypatch):
        rub = pd.DataFrame([
            [1, self.P[0], 11, 1, "SALARIO", 1, "Salários", 900.0, 0, 0, 0, None],
            [1, self.P[1], 11, 1, "SALARIO", 1, "Salários", 900.0, 0, 0, 0, None],
            [1, self.P[0], 11, 9, "FERIAS", 9, "Férias Gozadas", 300.0, 0, 0, 0, None],
        ], columns=["codi_emp", "competencia", "tipo_process", "i_eventos", "nome",
                    "classificacao", "desc_classificacao", "valor_p", "valor_d",
                    "valor_i", "valor_id", "qtd_informada"])
        enc = pd.DataFrame({"codi_emp": [1, 1], "competencia": self.P,
                            "inss_empresa": [50.0, 50.0], "fgts": [20.0, 20.0],
                            "fgts_resc": [0.0, 0.0]})
        ctx = self._ctx(monkeypatch, rub=rub, enc=enc)
        # O ranking das 3 maiores linhas MORREU.
        # Ele repetia a tabela e obrigava o card a se explicar ("2o maior
        # item") pra nao ser lido como total. No lugar, os quatro numeros que
        # respondem "quanto custou" de cima pra baixo.
        assert [c["key"] for c in ctx["hero_folha"]] == [
            "custo_total", "folenc", "encargos", "func"]
        # UM NOME SO: o card, o titulo do chart, o titulo do
        # bloco e a linha da tabela dizem todos "Folha + Encargos". Antes o
        # card dizia "Custo Total" ao lado de uma linha com outro nome e o
        # mesmo numero.
        assert ctx["hero_folha"][0]["label"] == LINHA_FOLHA_ENCARGOS
        assert [c for c in ctx["hero_folha"] if c["key"] == "destaque"] == []
        # CARD == TABELA por construcao: os dois cards novos saem do MESMO
        # objeto que a tabela renderiza, entao nao ha como divergirem.
        _tot = ctx["hero_folha"][0]["value"]
        _enc = ctx["hero_folha"][2]["value"]
        # 900 + 900 de Folha + 300 de Ferias + 140 de guia (50+50 INSS, 20+20 FGTS)
        assert _tot == "R$ 2.240,00"
        assert _enc == "R$ 140,00"

    def test_card_da_faixa_E_a_linha_da_tabela_somada(self, monkeypatch):
        """CARD == TABELA por CONSTRUCAO, nao por coincidencia.

        Os dois cards novos (CUSTO TOTAL e ENCARGOS) saem do mesmo objeto que
        renderiza a tabela. Este teste nao crava numero: ele soma a LINHA que
        o PDF exibe e exige o card identico — se alguem recalcular o card por
        outro caminho, quebra aqui, que e o ponto.
        """
        rub = pd.DataFrame([
            [1, self.P[0], 11, 1, "SALARIO", 1, "Salários", 1234.56, 0, 0, 0, None],
            [1, self.P[1], 11, 1, "SALARIO", 1, "Salários", 765.44, 0, 0, 0, None],
            [1, self.P[0], 11, 9, "FERIAS", 9, "Férias Gozadas", 111.11, 0, 0, 0, None],
        ], columns=["codi_emp", "competencia", "tipo_process", "i_eventos", "nome",
                    "classificacao", "desc_classificacao", "valor_p", "valor_d",
                    "valor_i", "valor_id", "qtd_informada"])
        enc = pd.DataFrame({"codi_emp": [1, 1], "competencia": self.P,
                            "inss_empresa": [33.33, 44.44], "fgts": [11.11, 22.22],
                            "fgts_resc": [0.0, 0.0]})
        ctx = self._ctx(monkeypatch, rub=rub, enc=enc)
        import pdf_generator
        _brl = pdf_generator._fmt_brl

        def soma_linha(rotulo):
            row = next(r for r in ctx["folha_rows"] if r["label"] == rotulo)
            return sum(
                float(v.replace("R$", "").replace(".", "").replace(",", ".").strip())
                for v in row["values"])

        card = {c["key"]: c for c in ctx["hero_folha"]}
        # o card do total e a linha do total sao o MESMO numero — e
        # tambem o mesmo NOME.
        assert card["custo_total"]["label"] == LINHA_FOLHA_ENCARGOS
        assert card["custo_total"]["value"] == _brl(
            soma_linha(LINHA_FOLHA_ENCARGOS))
        # os encargos deixaram de ser LINHA (estao DENTRO do total)
        # e seguem como CARD. O que resta e o que importa: o card
        # reproduz a GUIA que entrou, sem recalcular por outro caminho.
        esperado = float((enc["inss_empresa"] + enc["fgts"]
                          + enc["fgts_resc"]).sum())
        assert card["encargos"]["value"] == _brl(esperado)
        # e o total CONTEM os encargos: card do total > card de encargos
        assert soma_linha(LINHA_FOLHA_ENCARGOS) > esperado

    def test_sem_tabela_nao_ha_card_fantasma(self, monkeypatch):
        """Empresa sem folha na janela: os cards que saem da tabela NAO
        entram. R$ 0,00 afirmaria que a folha custou zero, quando o que houve
        foi ausencia de dado — a ausencia declara."""
        ctx = self._ctx(monkeypatch)
        assert [c for c in ctx["hero_impostos"] if c["key"] == "destaque"] == []
        assert len(ctx["hero_impostos"]) == 1
        # na folha sobram os dois cards do hero fiscal, que nao dependem da
        # tabela; custo_total e encargos ficam de fora
        assert [c["key"] for c in ctx["hero_folha"]] == ["folenc", "func"]

    def test_as_duas_paginas_tem_a_MESMA_ordem_de_blocos(self):
        # FAIXA -> GRAFICO -> DETALHAMENTO, identico nas duas paginas.
        # O titulo da tabela de impostos deixou de ser literal:
        # ele passou a DECLARAR o universo (`imp_titulo` — apurado no geral,
        # guias no Simples), porque "Valores por tributo" era generico ao lado
        # de um card de outra base.
        for tpl, hero, chart, tabela in (
                ("templates/impostos.html", "hero_grid(hero_impostos)",
                 "chart_block(", "IMP_ROWS"),
                ("templates/folha.html", "hero_grid(hero_folha)",
                 "chart_block(", "FOLHA_ROWS")):
            src = open(tpl, encoding="utf-8").read().upper()
            i_hero = src.find(hero.upper())
            i_chart = src.find(chart.upper())
            i_tab = src.find(tabela)
            assert -1 < i_hero < i_chart < i_tab, tpl

    def test_titulo_da_tabela_de_impostos_nao_e_literal(self):
        """O titulo DECLARA o universo, entao nao pode voltar a ser texto fixo.

        A pagina exibe apurado no geral e guias no Simples; um literal no
        template diria a mesma coisa nos dois casos
        — rotulo generico ao lado de numero de outra base.
        """
        src = open("templates/impostos.html", encoding="utf-8").read()
        assert "{{ imp_titulo }}" in src
        for morto in ("Valores por tributo", "Guias recolhidas por tributo"):
            assert morto not in src, morto

    def test_na_folha_o_detalhe_vem_DEPOIS_do_total(self):
        """O total em CIMA, os principais custos EMBAIXO.

        A ordem e o que torna a leitura possivel — o detalhe so faz sentido
        depois de o leitor ter visto o numero em que ele esta contido. Invertida,
        a pagina abre com cinco rubricas soltas.
        """
        folha = open("templates/folha.html", encoding="utf-8").read()
        assert folha.find("folha_rows") < folha.find("folha_destaque_rows")
        # e o subtitulo de contencao vem ANTES da tabela que ele qualifica
        assert (folha.find("folha_destaque_subtitulo")
                < folha.find("folha_destaque_rows"))

    def test_charts_dos_detalhamentos_usam_constante_de_SLOT(self):
        # o chart de folha reusava a constante nomeada pro slot de IMPOSTOS:
        # mexer no layout de uma pagina mudava a outra em silencio, e o par de
        # charts de impostos renderizava em tamanhos diferentes (fontes
        # aparentes desiguais lado a lado).
        import pdf_generator
        src = open(pdf_generator.__file__, encoding="utf-8").read()
        assert "_IMP_CHART" not in src
        assert "_FOLHA_SLOT" not in src           # nem nomeada pela de folha
        assert pdf_generator._CHART_DETALHE_MEIO["width"] == 500
        assert pdf_generator._CHART_DETALHE_CHEIO["width"] == 1000

    def test_chart_de_folha_do_fiscal_tem_constante_de_slot_propria(self):
        """Ele era o UNICO chart do PDF sem constante.

        Caia no default do `_fig_to_b64` (500x250), e 500 de largura LOGICA
        nao cabe 24 barras rotuladas (12 meses x 2 anos, no YoY).
        Quando o rotulo nao cabe, o Plotly ENCOLHE o texto sozinho.

        O aspecto MEDIDO do slot e 1,93 (533 x 276, estavel em 4 empresas
        porque a row do fiscal e fixa). A regra das FORMAS DE SLOT manda o PNG
        nao ser mais ESTREITO que o slot, senao a sobra vira margem lateral.
        """
        import pdf_generator
        c = pdf_generator._CHART_MEIA_LARGURA
        assert c["width"] == 1000, "largura LOGICA e o que descomprime o texto"
        assert c["width"] / c["height"] >= 1.93 - 0.01
        src = open(pdf_generator.__file__, encoding="utf-8").read()
        assert "_fig_to_b64(_fig_folha)" not in src, "voltou pro default"
        assert "_CHART_FOLHA_FISCAL" not in src, "nome por FORMA, nao por pagina"

    def test_o_PNG_nunca_e_mais_estreito_que_o_slot_que_ocupa(self):
        """A regra das FORMAS DE SLOT, em numero (medido com Playwright).

        Com `object-fit: contain`, PNG com aspecto >= o do slot e limitado pela
        LARGURA e a sobra vira folga em cima e embaixo. PNG mais ESTREITO que o
        slot vira margem LATERAL — chart flutuando, que le como desalinhamento.

        Aspectos MEDIDOS no HTML real (`.block-chart`, 3 empresas):
          Detalhamento de IMPOSTOS, faixa cheia: 2,38 (23) .. 2,82 (201)
          Detalhamento de FOLHA,    faixa cheia: 2,93 (201) .. 3,06 (23)
        A folha e mais BAIXA porque tem DUAS tabelas abaixo do chart,
        contra uma so na de impostos — sao formas diferentes, e
        por isso constantes diferentes.
        """
        import pdf_generator as PG

        def asp(c):
            return c["width"] / c["height"]

        assert asp(PG._CHART_DETALHE_FAIXA) >= 2.82
        assert asp(PG._CHART_DETALHE_FAIXA_BAIXA) >= 3.06
        # "LIGEIRAMENTE acima": folga vertical de sobra vira oceano vazio
        assert asp(PG._CHART_DETALHE_FAIXA) < 2.82 * 1.10
        assert asp(PG._CHART_DETALHE_FAIXA_BAIXA) < 3.06 * 1.10
        # e a de meia largura cobre o slot curto medido (1,20 e 1,55)
        assert asp(PG._CHART_DETALHE_MEIO) >= 1.55


def test_pdf_folha_NAO_tem_mais_o_agrupamento_outros():
    """A caixa-preta saiu do PDF de folha.

    Historia da legenda: bloco proprio depois da tabela -> legenda embaixo do
    grafico -> colada na tabela -> MORTA. Ela
    existia pra explicar um numero grande sem nome; com o detalhamento
    CONTIDO — total em cima, cinco rubricas nomeadas embaixo — nao ha mais o
    que agregar, entao nao ha mais o que explicar.

    O helper `etl.metrics.legenda_outros` segue vivo e com teste, so nao e
    renderizado — mesmo precedente do `legenda_abatimentos` logo abaixo.
    """
    import pathlib
    raiz = pathlib.Path(__file__).resolve().parent.parent
    tpl = (raiz / "templates" / "folha.html").read_text(encoding="utf-8")
    src = (raiz / "pdf_generator.py").read_text(encoding="utf-8")
    assert "outros-cols" not in tpl
    assert "O que h&aacute; em" not in tpl
    assert "folha_outros_legenda" not in tpl
    assert "folha_outros_legenda" not in src
    assert "_legenda_outros(" not in src
    assert "legenda_outros as _legenda_outros" not in src
    # o mecanismo continua vivo do lado do metrics
    from etl.metrics import legenda_outros      # noqa: F401


def test_pdf_de_folha_NAO_declara_os_abatimentos():
    """"Fora do custo no periodo" nao vai pro PDF.

    Este PDF vai pro CLIENTE, e a lista de exclusoes e ruido pra quem so quer
    saber quanto custou. A legenda vive no APP, que e onde a
    CONCILIACAO acontece. O MECANISMO fica: `legenda_abatimentos` segue no
    metrics, com teste proprio, e o app segue renderizando.
    """
    import pathlib
    raiz = pathlib.Path(__file__).resolve().parent.parent
    tpl = (raiz / "templates" / "folha.html").read_text(encoding="utf-8")
    src = (raiz / "pdf_generator.py").read_text(encoding="utf-8")
    app = (raiz / "app.py").read_text(encoding="utf-8")
    assert "folha_abat_legenda" not in tpl
    # a CHAMADA e o import morreram; a mencao no comentario que explica a
    # decisao pode (e deve) ficar
    assert "_legenda_abatimentos(" not in src
    assert "legenda_abatimentos as _legenda_abatimentos" not in src
    # o mecanismo continua vivo do outro lado
    assert "legenda_abatimentos" in app
    from etl.metrics import legenda_abatimentos      # noqa: F401


class TestCardFolhaLeAMesmaBaseDaTabela:
    """A unificacao da base do custo de folha tem TRES fios no
    PDF, e os tres tem de mudar juntos. Se um ficar pra tras, o card mostra um
    numero e a sparkline (ou o delta YoY) mostra outro, na mesma pagina.

    Teste no FONTE porque os tres sao call sites DB-bound: o default None do
    kwarg compila e passa em teste de unidade sem provar fio nenhum.
    """
    SRC = (pathlib.Path(__file__).resolve().parents[1] / "pdf_generator.py").read_text()

    def test_o_hero_le_o_rub(self):
        assert "rub_df=rub_df" in self.SRC

    def test_o_yoy_le_o_rub_com_guard_de_1o_deploy(self):
        assert 'rub_df=yd.get("rub", _e)' in self.SRC
        # o _load_data_yoy NAO tinha guard de information_schema pra nenhum
        # mart novo antes disso — sem ele, 1o deploy quebra
        assert "table_name='folha_rubricas_mensais'" in self.SRC

    def test_a_sparkline_le_o_rub_na_janela_ESTENDIDA(self):
        """Range < 6 meses alarga a janela do spark em 5 meses. O rub tem de
        vir alargado junto, senao a sparkline conta outra base."""
        assert "custo_folha_pm(spark_dfs[\"folha\"], spark_dfs.get(\"rub\")" in self.SRC
        assert '"rub": (pd.read_sql(f"SELECT * FROM mart.folha_rubricas_mensais {flt}"' in self.SRC


class TestSubLinhaDeCardSoYoY:
    """Sub-linha de card do PDF vira SO o fragmento YoY.

    Os kpi-context tinham acumulado camadas (media mensal, decomposicao em R$,
    reguas de universo, R$ do IRPJ/CSLL, rotulo de corte). Card e leitura
    rapida; o detalhe vive na TABELA da propria pagina. PDF ONLY — o app fica,
    porque la o leitor e operador e o tooltip tem espaco pra frase longa.

    Tres excecoes NOMEADAS sobrevivem ao lado do YoY, todas da mesma classe:
    frase que explica POR QUE o valor e "—". Sem elas o travessao vira mudo e
    le como falta de dado, que e o oposto do que houve.
    """
    P = ["2025-01", "2025-02"]

    def _ctx(self, monkeypatch, rub=None, enc=None, yoy=None, fat_val=100_000.0):
        import pdf_generator
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda *a, **k: "")
        e = pd.DataFrame()
        fat = pd.DataFrame({"competencia": self.P,
                            "valo_saidas": [fat_val] * 2,
                            "valo_servicos": [0.0, 0.0]})
        folha = pd.DataFrame({"competencia": self.P, "tipo_process": [11, 11],
                              "valo_bruto": [100.0, 100.0]})
        enc = e if enc is None else enc
        spark = {"fat": fat, "imp": e, "folha": folha, "func": e, "bal": e,
                 "encargos": enc, "dre": e}
        return pdf_generator._build_context(
            fat, e, folha, e, e, spark, self.P, self.P, "T", "0",
            self.P[0], self.P[-1], bal_serie_df=e, encargos_df=enc,
            rub_df=e if rub is None else rub, yoy_dfs=yoy)

    def _prev(self, fat_mes=80_000.0):
        e = pd.DataFrame()
        prev = [str(pd.Period(x, freq="M") - 12) for x in self.P]
        return {"fat": pd.DataFrame({"competencia": prev,
                                     "valo_saidas": [fat_mes] * 2,
                                     "valo_servicos": [0.0, 0.0]}),
                "impd": e, "folha": e, "encargos": e, "dre": e, "rub": e}

    def _card(self, ctx, hero, key):
        return next(c for c in ctx[hero] if c["key"] == key)

    # ── os 4 cards do fiscal ────────────────────────────────────────────
    def test_faturamento_fica_so_com_o_fragmento(self, monkeypatch):
        ctx = self._ctx(monkeypatch, yoy=self._prev())
        assert self._card(ctx, "hero_fiscal", "fat")["context"] == (
            "em&nbsp;relação ao mesmo período/2024: ↑25,0%")

    def test_funcionarios_segue_INTOCADO(self, monkeypatch):
        # sem folha carregada o card ja dizia isso, e continua dizendo: ele
        # esta FORA do veredito, por decisao.
        ctx = self._ctx(monkeypatch, yoy=self._prev())
        assert self._card(ctx, "hero_fiscal", "func")["context"] != ""

    # ── os 2 cards proprios do Detalhamento de Folha ────────────────────
    def test_cards_do_detalhamento_de_folha_ficam_sem_sub_linha(self, monkeypatch):
        rub = pd.DataFrame([
            [1, self.P[0], 11, 1, "SALARIO", 1, "Salários", 900.0, 0, 0, 0, None],
            [1, self.P[1], 11, 1, "SALARIO", 1, "Salários", 900.0, 0, 0, 0, None],
        ], columns=["codi_emp", "competencia", "tipo_process", "i_eventos",
                    "nome", "classificacao", "desc_classificacao", "valor_p",
                    "valor_d", "valor_i", "valor_id", "qtd_informada"])
        enc = pd.DataFrame({"codi_emp": [1, 1], "competencia": self.P,
                            "inss_empresa": [50.0, 50.0], "fgts": [20.0, 20.0],
                            "fgts_resc": [0.0, 0.0]})
        ctx = self._ctx(monkeypatch, rub=rub, enc=enc)
        # "no periodo · N meses" e "INSS patronal e FGTS recolhidos" morreram:
        # o primeiro repetia o cabecalho da pagina, o segundo repetia o proprio
        # rotulo do card ("Encargos (INSS + FGTS, guia)").
        assert self._card(ctx, "hero_folha", "custo_total")["context"] == ""
        assert self._card(ctx, "hero_folha", "encargos")["context"] == ""
        # e os VALORES nao se moveram
        assert self._card(ctx, "hero_folha", "custo_total")["value"] == "R$ 1.940,00"

    def test_ranking_de_impostos_segue_declarando_que_e_ranking(self, monkeypatch):
        # excecao nomeada: sem "maior item do periodo" o numero da faixa e
        # confundido com o total (foi por isso que o contexto existe).
        import pdf_generator
        assert pdf_generator._RANKING_DESTAQUE[0] == "maior item do período"
        assert pdf_generator._card_destaque("ICMS", 10.0, 0)["context"] == (
            "maior item do período")

    # ── excecoes: a frase que explica o "—" ─────────────────────────────
    def test_frase_de_denominador_imaterial_SOBREVIVE(self, monkeypatch):
        # Faturamento POSITIVO porem imaterial. "—" mudo faria o
        # leitor achar que falta dado, quando o que falta e BASE.
        ctx = self._ctx(monkeypatch, yoy=self._prev(fat_mes=1.0), fat_val=1.0)
        for key in ("imp", "folenc"):
            c = self._card(ctx, "hero_fiscal", key)["context"]
            assert "faturamento insuficiente para o indicador no período" in c

    def test_o_label_do_yoy_FICA(self, monkeypatch):
        """Empresa sem ano anterior DECLARA a ausencia, nao fica muda.

        Preserva o invariante `delta XOR label` do metrics.
        """
        e = pd.DataFrame()
        ctx = self._ctx(monkeypatch, yoy={"fat": e, "impd": e, "folha": e,
                                          "encargos": e, "dre": e, "rub": e})
        assert self._card(ctx, "hero_fiscal", "fat")["context"] == "sem dados de 2024"

    # ── item 4: zero separador orfao, zero div vazia ────────────────────
    def test_nenhum_context_de_PDF_tem_separador_orfao(self, monkeypatch):
        ctx = self._ctx(monkeypatch, yoy=self._prev())
        for hero in ("hero_fiscal", "hero_impostos", "hero_folha"):
            for c in ctx[hero]:
                t = c["context"]
                assert not t.startswith(" · ") and not t.endswith(" · "), c
                assert " ·  · " not in t, c

    def test_macro_nao_renderiza_div_de_context_vazio(self):
        # com sub-linha vazia o `<div class="kpi-context">` virava uma div
        # vazia ocupando altura no card.
        import pathlib as _p
        tpl = (_p.Path(__file__).resolve().parents[1] / "templates"
               / "_macros.html").read_text(encoding="utf-8")
        assert "{% if card.context %}" in tpl

    def test_subtitulo_de_contencao_sai_do_PDF_e_FICA_no_metrics(self):
        """No PDF a protecao do subtitulo saiu.

        A constante segue viva: o `app.py` consome.
        No PDF a contencao passa a ser dita pelo TITULO do bloco ("Principais
        custos da folha", que nao promete totalidade) e pela posicao, abaixo
        do total.
        """
        import pathlib as _p
        raiz = _p.Path(__file__).resolve().parents[1]
        tpl = (raiz / "templates" / "folha.html").read_text(encoding="utf-8")
        src = (raiz / "pdf_generator.py").read_text(encoding="utf-8")
        app = (raiz / "app.py").read_text(encoding="utf-8")
        assert "folha_destaque_subtitulo" not in tpl
        assert "folha_destaque_subtitulo" not in src
        assert "SUBTITULO_DESTAQUES_FOLHA" not in src
        # o titulo, que e a protecao que sobra, continua
        assert "folha_destaque_titulo" in tpl
        # e o mecanismo segue vivo do lado do app
        assert "SUBTITULO_DESTAQUES_FOLHA" in app
        from etl.metrics import SUBTITULO_DESTAQUES_FOLHA      # noqa: F401


class TestChartFaturamentoYoY:
    """Chart de Faturamento do fiscal em YoY intercalado.

    Barras agrupadas por
    mes, ano anterior em vermelho e ano da janela em azul, tick so com o mes
    porque o ano vive na legenda. Substituicao direta — nao ha toggle.
    """
    EMPTY = pd.DataFrame()

    def _fat(self, periods, valor=1000.0):
        return pd.DataFrame({"competencia": list(periods),
                             "valo_saidas": [valor] * len(periods),
                             "valo_servicos": [0.0] * len(periods)})

    def _ctx(self, periods, fat=None, prev_fat=None, **kw):
        import pdf_generator
        e = self.EMPTY
        fat = self._fat(periods) if fat is None else fat
        yoy = None if prev_fat is None else {
            "fat": prev_fat, "impd": e, "folha": e, "encargos": e,
            "dre": e, "rub": e}
        return pdf_generator._build_context(
            fat, e, e, e, e,
            {"fat": fat, "imp": e, "folha": e, "func": e, "bal": e,
             "encargos": e, "dre": e},
            list(periods), list(periods), "T", "0",
            periods[0], periods[-1], bal_serie_df=e, yoy_dfs=yoy, **kw)

    # ── rotulos ──────────────────────────────────────────────────────────
    def test_tick_traz_so_o_mes_e_nunca_repete_categoria(self):
        """O tick fica centrado no PAR, entao "Jan/25" mentiria sobre a barra
        vermelha (que e Jan/24). Nome repetido colapsaria duas categorias do
        plotly numa — o cap de 12 meses do PDF e o que garante unicidade."""
        import pdf_generator
        per = [f"2025-{m:02d}" for m in range(1, 13)]
        lbl = pdf_generator._make_labels_mes(per)
        assert lbl[0] == "Jan" and lbl[-1] == "Dez"
        assert not any("/" in x for x in lbl)
        # qualquer janela de 12 meses dentro do cap segue com ticks unicos
        for inicio in ("2025-01", "2025-04", "2025-07"):
            p0 = pd.Period(inicio, freq="M")
            janela = [str(p0 + i) for i in range(12)]
            assert len(set(pdf_generator._make_labels_mes(janela))) == 12

    def test_janela_cross_year_marca_o_ano_no_1o_mes_de_cada_ano(self):
        """Sem isso nada na faixa diria que Jan e de 2026 — e ali a legenda
        tambem nao pode dizer, porque nomeia intervalos."""
        import pdf_generator
        lbl = pdf_generator._make_labels_mes(
            ["2025-10", "2025-11", "2025-12", "2026-01", "2026-02"])
        assert lbl == ["Out/25", "Nov", "Dez", "Jan/26", "Fev"]

    def test_legenda_usa_ano_limpo_e_intervalo_so_no_cruzamento(self):
        import pdf_generator
        assert pdf_generator._rotulos_anos(["2025-01", "2025-12"]) == ("2025", "2024")
        assert pdf_generator._rotulos_anos(["2025-10", "2026-03"]) == (
            "Out/25 a Mar/26", "Out/24 a Mar/25")

    def test_serie_anterior_alinha_por_PERIODO_nao_por_posicao(self):
        """`pp` e a pagina; a serie prev vive no indice da janela -12m
        inteira. Alinhar por posicao trocaria os meses de lugar."""
        import pdf_generator
        prev = pd.Series({"2024-01": 10.0, "2024-03": 30.0})
        assert pdf_generator._serie_anterior_alinhada(
            prev, ["2025-01", "2025-02", "2025-03"]) == [10.0, 0.0, 30.0]

    # ── largura de barra ────────────────────────────────────────────────
    def test_par_e_serie_unica_mantem_a_MESMA_largura_de_barra(self):
        """O bargap olha n_series * n_labels. Passar 2n sempre daria a
        empresa SEM ano anterior barra com o dobro da largura e um vao
        fantasma no lugar da vermelha que nao veio."""
        import pdf_generator
        par = pdf_generator._make_bar_yoy(["Jan"], [1.0], [2.0], "2025", "2024")
        una = pdf_generator._make_bar_yoy(["Jan"], [1.0], [0.0], "2025", "2024",
                                          tem_anterior=False)
        assert par.layout.bargap == pytest.approx(pdf_generator._bargap_faixa(2))
        assert una.layout.bargap == pytest.approx(pdf_generator._bargap_faixa(1))
        # serie unica reproduz o chart antigo, que usava _bargap_faixa(n)
        assert una.layout.bargap == pytest.approx(
            pdf_generator._make_bar(["Jan"], [1.0], "#000").layout.bargap)

    def test_serie_ausente_nao_vira_barra_zero_nem_chip_na_legenda(self):
        import pdf_generator
        una = pdf_generator._make_bar_yoy(["Jan"], [1.0], [0.0], "2025", "2024",
                                          tem_anterior=False)
        assert [t.name for t in una.data] == ["2025"]
        par = pdf_generator._make_bar_yoy(["Jan"], [1.0], [2.0], "2025", "2024")
        # anterior primeiro = barra da esquerda (leitura cronologica)
        assert [t.name for t in par.data] == ["2024", "2025"]
        assert [t.marker.color for t in par.data] == [
            pdf_generator.COR_FAT_ANTERIOR, pdf_generator.COR_FAT]
        assert par.layout.showlegend is True

    # ── guard dos dois anos ─────────────────────────────────────────────
    def test_guard_suprime_so_quando_os_DOIS_anos_estao_vazios(self, monkeypatch):
        """O guard olhava so a janela: mes sem faturamento apagava
        tambem a barra do ano anterior."""
        import pdf_generator
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda fig, **k: "B64")
        per, prev = ["2026-06"], ["2025-06"]
        e = self.EMPTY
        cheio, p_cheio = self._fat(per), self._fat(prev)
        assert self._ctx(per, cheio, p_cheio)["charts"]["fat"] == "B64"
        assert self._ctx(per, cheio, e)["charts"]["fat"] == "B64"
        # o caso NOVO: janela vazia, ano anterior cheio
        assert self._ctx(per, e, p_cheio)["charts"]["fat"] == "B64"
        # nenhum dos dois -> unico caso de "Sem dados de faturamento"
        assert self._ctx(per, e, e)["charts"]["fat"] == ""

    # ── invariantes de nao-regressao ────────────────────────────────────
    def test_valores_da_janela_sao_OS_MESMOS_do_chart_antigo(self, monkeypatch):
        """A mudanca e de APRESENTACAO: a serie azul continua sendo
        vals(fat_pm), o mesmo argumento que o `_make_bar` recebia."""
        import pdf_generator
        chamadas = []
        orig = pdf_generator._make_bar_yoy

        def espia(labels, atual, anterior, *a, **k):
            chamadas.append({"atual": list(atual), "anterior": list(anterior)})
            return orig(labels, atual, anterior, *a, **k)

        monkeypatch.setattr(pdf_generator, "_make_bar_yoy", espia)
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda fig, **k: "B64")
        per, prev = ["2025-01", "2025-02"], ["2024-01", "2024-02"]
        self._ctx(per, self._fat(per, 1500.0), self._fat(prev, 900.0))
        # QUATRO call sites, nesta ordem de construcao:
        # [0] Faturamento (fiscal), [1] Impostos (detalhamento), [2] Folha
        # (fiscal) e [3] Folha (detalhamento). O assert do numero e
        # deliberado: um 5o consumidor aparecendo em silencio quebra aqui, e e
        # exatamente o que o discriminador estrutural do builder existe pra
        # impedir (vide o teste do call site proprio, logo abaixo).
        assert len(chamadas) == 4
        assert chamadas[0]["atual"] == [1500.0, 1500.0]
        assert chamadas[0]["anterior"] == [900.0, 900.0]

    def test_o_yoy_entra_por_CALL_SITE_nunca_por_sniff_de_parametro(self):
        """O discriminador e ESTRUTURAL.

        Ate aqui o `_make_bar` era o builder do Detalhamento de Folha, e a
        tentacao era ligar o YoY quando `rotulos=True`. Com a folha migrada
        a tentacao inverte: ligar o YoY dentro do `_make_bar` pra
        "todo mundo ganhar". Seria pior — o `_make_bar` ainda serve chart que
        NAO tem serie do ano anterior carregada (Despesas e Custos le o
        balancete, que a janela -12m nao carrega, de proposito), e ele passaria
        a comparar contra uma serie vazia, em silencio.

        `_make_bar` segue o builder simples: uma serie, sem legenda.
        """
        import pdf_generator
        fig = pdf_generator._make_bar(["Jan", "Fev"], [1.0, 2.0],
                                      pdf_generator.COR_FOLENC, 0)
        assert len(fig.data) == 1
        assert fig.layout.showlegend is False
        src = pathlib.Path(pdf_generator.__file__).read_text(encoding="utf-8")
        # cada chart YoY tem call site PROPRIO: faturamento, impostos e os
        # dois de folha (1 def + 4 call sites)
        assert src.count("_make_bar_yoy(") >= 5
        assert "def _make_bar(" in src

    def test_build_context_sem_yoy_dfs_degrada_para_serie_unica(self, monkeypatch):
        """Retrocompat por construcao: call site que nao carrega a janela
        -12m ve o chart de antes, nunca um par com o ano anterior zerado."""
        import pdf_generator
        chamadas = []
        orig = pdf_generator._make_bar_yoy

        def espia(*a, **k):
            chamadas.append(dict(k))
            return orig(*a, **k)

        monkeypatch.setattr(pdf_generator, "_make_bar_yoy", espia)
        monkeypatch.setattr(pdf_generator, "_fig_to_b64", lambda fig, **k: "B64")
        self._ctx(["2025-01", "2025-02"])
        # chamadas[0] = Faturamento; o degrade vale pros DOIS charts, mas quem
        # este teste protege e o retrocompat do fiscal.
        assert chamadas[0]["tem_anterior"] is False
        assert chamadas[0]["tem_atual"] is True


# ── _resolver_chrome (kaleido pinado) ─────────────────────────
# O kaleido caia no Chrome do SISTEMA (typical_paths do choreographer) e o
# auto-update do Google quebrava o PDF. O resolver aponta pro
# chromium do playwright, que ja e pinado pelo requirements.

class TestResolverChrome:
    def _arvore_playwright(self, raiz, rev, *, win=True):
        """Reproduz o layout que `playwright install` cria."""
        if win:
            exe = raiz / f"chromium-{rev}" / "chrome-win" / "chrome.exe"
        else:
            exe = (raiz / f"chromium-{rev}" / "chrome-linux" / "chrome")
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("")
        return exe

    def test_env_manual_manda(self, tmp_path, monkeypatch):
        from pdf_generator import _resolver_chrome
        monkeypatch.setenv("CHROME_PATH", r"C:\painel\chrome\chrome.exe")
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        self._arvore_playwright(tmp_path, 1217)
        assert _resolver_chrome() == r"C:\painel\chrome\chrome.exe"

    def test_glob_do_playwright(self, tmp_path, monkeypatch):
        from pdf_generator import _resolver_chrome
        monkeypatch.delenv("CHROME_PATH", raising=False)
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        exe = self._arvore_playwright(tmp_path, 1217)
        assert _resolver_chrome() == str(exe)

    def test_pega_a_revisao_PINADA_nao_a_maior(self, tmp_path, monkeypatch):
        # `playwright install` deixa revisoes antigas e
        # NOVAS lado a lado (ex.: 1217 pinado pelo requirements + 1228 de um
        # `playwright install` avulso de outra versao). O resolver tem que
        # respeitar a revisao PINADA (lida do proprio browsers.json do
        # pacote instalado), nunca a maior presente no disco.
        from pdf_generator import _resolver_chrome
        monkeypatch.delenv("CHROME_PATH", raising=False)
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        pinada = self._arvore_playwright(tmp_path, 1217)
        self._arvore_playwright(tmp_path, 1228)
        assert _resolver_chrome() == str(pinada)

    def test_ignora_o_headless_shell(self, tmp_path, monkeypatch):
        # chromium_headless_shell-* nao fala o CDP que o choreographer usa.
        from pdf_generator import _resolver_chrome
        monkeypatch.delenv("CHROME_PATH", raising=False)
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        shell = tmp_path / "chromium_headless_shell-9999" / "chrome-win" / "chrome.exe"
        shell.parent.mkdir(parents=True, exist_ok=True)
        shell.write_text("")
        exe = self._arvore_playwright(tmp_path, 1217)
        assert _resolver_chrome() == str(exe)

    def test_sem_env_nenhuma_devolve_None(self, monkeypatch):
        # Sem a variavel: cai no find_browser do choreographer.
        from pdf_generator import _resolver_chrome
        monkeypatch.delenv("CHROME_PATH", raising=False)
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
        assert _resolver_chrome() is None

    def test_raiz_inexistente_devolve_None(self, tmp_path, monkeypatch):
        from pdf_generator import _resolver_chrome
        monkeypatch.delenv("CHROME_PATH", raising=False)
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "nao_existe"))
        assert _resolver_chrome() is None

    def test_raiz_sem_chromium_devolve_None(self, tmp_path, monkeypatch):
        from pdf_generator import _resolver_chrome
        monkeypatch.delenv("CHROME_PATH", raising=False)
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        assert _resolver_chrome() is None

    def test_layout_linux_tambem_resolve(self, tmp_path, monkeypatch):
        from pdf_generator import _resolver_chrome
        monkeypatch.delenv("CHROME_PATH", raising=False)
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        exe = self._arvore_playwright(tmp_path, 1217, win=False)
        assert _resolver_chrome() == str(exe)
