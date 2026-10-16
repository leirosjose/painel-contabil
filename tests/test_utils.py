# ---------------------------------------------------------------------------
# Testes de caracterização de etl/utils.py.
# Comportamento esperado derivado RODANDO as funções, não por suposição.
# ---------------------------------------------------------------------------
import math
import threading

import pandas as pd
import pytest

from etl.utils import (
    bal_grupo_serie,
    fmt_brl,
    fmt_brl_curto,
    classify_etl_proc,
    cnpj_filial,
    cnpj_raiz,
    label_empresa,
    marca_inativa,
    consume_role,
    etl_progress_frac,
    luc_nf,
    normaliza_rubrica,
    only_digits,
    regime_dominante,
    rotulo_tipo_process,
    safe_div,
    weighted_pct,
)
from etl.mappings import GRUPOS_BAL

PERIODS = ["2025-01", "2025-02", "2025-03", "2025-04"]


# ── weighted_pct (média ponderada, não média simples) ───────────────────────

class TestWeightedPct:
    def test_e_sum_sobre_sum_nao_media_simples(self):
        # 30/500 = 6.0%. Média simples das pcts mensais seria 7.5%.
        num = pd.Series([10.0, 20.0])
        den = pd.Series([100.0, 400.0])
        assert weighted_pct(num, den) == pytest.approx(6.0)

    def test_denominador_zero_vira_nan(self):
        assert math.isnan(weighted_pct(pd.Series([10.0]), pd.Series([0.0])))

    def test_series_vazias_viram_nan(self):
        assert math.isnan(
            weighted_pct(pd.Series(dtype=float), pd.Series(dtype=float))
        )


# ── safe_div ─────────────────────────────────────────────────────────────────

class TestSafeDiv:
    def test_divide_e_protege_zero_e_nan(self):
        out = safe_div(
            pd.Series([10.0, 5.0, 3.0]), pd.Series([2.0, 0.0, float("nan")])
        )
        assert out.iloc[0] == pytest.approx(5.0)
        assert math.isnan(out.iloc[1])
        assert math.isnan(out.iloc[2])


class TestSafeDivNumeradorNone:
    """Coluna 100% NULL no Postgres chega como object/None via pd.read_sql;
    sum(min_count=1) propaga None até o numerador do safe_div. Contrato:
    None/NaN em QUALQUER lado -> NaN (fmt_pct -> '—'), nunca TypeError."""

    def test_none_no_numerador_vira_nan_nao_typeerror(self):
        # numerador object-None, denominador float valido
        num = pd.Series([None, None], dtype=object)
        den = pd.Series([4117.0, 2680.0])
        out = safe_div(num, den)
        assert math.isnan(out.iloc[0]) and math.isnan(out.iloc[1])

    def test_nan_no_numerador_segue_nan(self):
        out = safe_div(pd.Series([float("nan"), 10.0]), pd.Series([2.0, 2.0]))
        assert math.isnan(out.iloc[0])
        assert out.iloc[1] == pytest.approx(5.0)

    def test_none_no_denominador_segue_nan(self):
        # ja passava (None e falsy no guard de y) — pin do comportamento
        out = safe_div(pd.Series([5.0], dtype=object), pd.Series([None], dtype=object))
        assert math.isnan(out.iloc[0])

    def test_ambos_none_perfil_sem_dado(self):
        # perfil fonte='sem_dado': ambos object-None — pin (ja passava)
        num = pd.Series([None], dtype=object)
        den = pd.Series([None], dtype=object)
        assert math.isnan(safe_div(num, den).iloc[0])


# ── only_digits / cnpj_raiz / cnpj_filial ────────────────────────────────────

class TestCnpjHelpers:
    def test_only_digits_limpa_pontuacao(self):
        assert only_digits("01.464.544/0001-78") == "01464544000178"

    def test_only_digits_none_e_nan_viram_vazio(self):
        assert only_digits(None) == ""
        assert only_digits(float("nan")) == ""

    def test_cnpj_raiz_primeiros_8_digitos(self):
        assert cnpj_raiz("01.464.544/0001-78") == "01464544"
        assert cnpj_raiz("123") is None

    def test_cnpj_filial_digitos_9_a_12(self):
        assert cnpj_filial("01464544000178") == "0001"
        assert cnpj_filial("01464544") is None


# ── label_empresa (rótulo do dropdown; gate da busca por CNPJ) ───────────────

class TestLabelEmpresa:
    def test_preserva_o_nome_legivel_e_os_badges(self):
        lbl = label_empresa("EMPRESA ALFA LTDA", "📊💰", "89039653000198")
        assert lbl.startswith("EMPRESA ALFA LTDA 📊💰")

    def test_label_carrega_os_digitos_crus_do_cnpj(self):
        # `cnpj` é OPCIONAL e hoje nenhum consumer passa: o app voltou ao campo
        # de busca separado, e quem consulta o CNPJ é o predicado Python, que
        # normaliza os dois lados. O parâmetro fica testado pra quem precisar
        # de um rótulo auto-suficiente — ressuscitar o comportamento sem gate
        # é que seria dívida.
        lbl = label_empresa("EMPRESA BETA", "📊", "76352993000122")
        assert "76352993000122" in lbl
        # e o fragmento também casa, que é como se digita na prática
        assert "76352993" in lbl

    def test_cnpj_formatado_na_entrada_vira_digitos_crus(self):
        # A origem entrega cru, mas o helper não pode depender disso: se um dia
        # o rótulo voltar a ser a superfície de busca, pontuação nele quebraria
        # quem digita só números.
        lbl = label_empresa("EMPRESA X", "", "76.352.993/0001-22")
        assert "76352993000122" in lbl
        assert "/" not in lbl and "." not in lbl

    def test_homonimas_produzem_labels_distintos(self):
        # Empresas homônimas aparecem no dropdown com texto idêntico. Isso NÃO
        # quebra seleção (a identidade da opção é a tupla de codi_emp), mas
        # com o CNPJ o rótulo desambigua.
        a = label_empresa("EMPRESA GAMA", "📊", "56806966000176")
        b = label_empresa("EMPRESA GAMA", "📊", "28492943000107")
        assert a != b

    def test_cnpj_ausente_nao_deixa_espaco_sobrando(self):
        for vazio in (None, float("nan"), "", "---"):
            lbl = label_empresa("EMPRESA SEM CNPJ", "⚠️", vazio)
            assert lbl == "EMPRESA SEM CNPJ ⚠️"

    def test_sem_badges_nao_deixa_espaco_duplo(self):
        assert label_empresa("EMPRESA Y", "", "49275198000106") == \
            "EMPRESA Y 49275198000106"

    def test_indentacao_da_filial_e_preservada(self):
        # A indentação à esquerda é semântica: desenha a árvore grupo -> filial
        # no dropdown. strip() nos dois lados achataria a hierarquia.
        lbl = label_empresa("   └ Filial 0002", "📊", "89039653000279")
        assert lbl == "   └ Filial 0002 📊 89039653000279"


# ── marca_inativa (a ausência DECLARA) ──────────────────────────────────────

class TestMarcaInativa:
    def test_ativa_nao_ganha_marca(self):
        assert marca_inativa(True) == ""

    def test_inativa_ganha_a_marca_ascii(self):
        # ASCII de propósito: o dropdown já carrega 5 emoji de badge, e um
        # sexto símbolo competiria com eles em vez de qualificar o nome.
        assert marca_inativa(False) == " [INATIVA]"

    def test_marca_compoe_com_label_empresa_sem_tocar_nos_badges(self):
        # É assim que o app usa: a marca entra pelo `nome`, antes dos badges.
        lbl = label_empresa(f"EMPRESA ALFA LTDA{marca_inativa(False)}", "📊")
        assert lbl == "EMPRESA ALFA LTDA [INATIVA] 📊"

    def test_a_marca_nao_sobrevive_a_safe_filename(self):
        # O contrato que protege o nome do arquivo: se um dia a marca vazar
        # pro `nome` que alimenta o filename, ela vira "_INATIVA" no .pdf.
        # Este teste documenta POR QUE ela viaja em campo separado.
        from pdf_generator import _safe_filename
        assert _safe_filename(f"EMPRESA ALFA{marca_inativa(False)}") == \
            "EMPRESA_ALFA_INATIVA"


# ── bal_grupo_serie (sem bfill de saldo retroativo) ──────────────────────────

class TestBalGrupoSerie:
    def test_conta_de_balanco_carrega_saldo_em_mes_sem_movimento(self):
        # ffill é o comportamento LEGÍTIMO: saldo conhecido persiste nos
        # meses seguintes sem lançamento.
        bal = pd.DataFrame({
            "competencia": ["2025-01", "2025-03"],
            "clas_cta": ["2010101", "2010101"],
            "valo_saldo": [1000.0, 1200.0],
        })
        out = bal_grupo_serie(bal, PERIODS, "201")
        assert out.tolist() == [1000.0, 1000.0, 1200.0, 1200.0]

    def test_conta_de_resultado_e_movimento_mensal_sem_fill(self):
        res = pd.DataFrame({
            "competencia": ["2025-01", "2025-03"],
            "clas_cta": ["3010101", "3010101"],
            "valo_saldo": [100.0, 300.0],
        })
        out = bal_grupo_serie(res, PERIODS, "301")
        assert out.tolist() == [100.0, 0.0, 300.0, 0.0]

    def test_df_vazio_vira_serie_de_zeros(self):
        out = bal_grupo_serie(pd.DataFrame(), PERIODS, "201")
        assert out.tolist() == [0.0, 0.0, 0.0, 0.0]

    def test_conta_que_nasce_no_meio_nao_retroage_saldo(self):
        # Conta 2010101 presente o período todo (1000); conta 2010199 nasce
        # em mar (500).
        # CORRETO: jan/fev = 1000 (a conta nova não existia).
        bal = pd.DataFrame({
            "competencia": ["2025-01", "2025-02", "2025-03", "2025-04",
                            "2025-03", "2025-04"],
            "clas_cta": ["2010101"] * 4 + ["2010199"] * 2,
            "valo_saldo": [1000.0] * 4 + [500.0, 500.0],
        })
        out = bal_grupo_serie(bal, PERIODS, "201")
        assert out.tolist()[:2] == [1000.0, 1000.0]
        assert out.tolist()[2:] == [1500.0, 1500.0]

    def test_seed_pre_janela_carrega_forward(self):
        # Linha anterior a periods[0] (seed de carry-in injetado
        # pelos consumers) ancora o ffill pela UNIÃO do índice — nunca bfill.
        bal = pd.DataFrame({
            "competencia": ["2024-11", "2025-02"],
            "clas_cta": ["2010101", "2010101"],
            "valo_saldo": [800.0, 1200.0],
        })
        out = bal_grupo_serie(bal, PERIODS, "201")
        assert out.tolist() == [800.0, 1200.0, 1200.0, 1200.0]

    def test_conta_que_so_existe_no_seed_vira_serie_constante(self):
        # Conta sem movimento na janela mas com saldo real pré-janela:
        # hoje some (reindex dropa a linha); com o fix, série constante.
        bal = pd.DataFrame({
            "competencia": ["2024-10"],
            "clas_cta": ["2010101"],
            "valo_saldo": [700.0],
        })
        out = bal_grupo_serie(bal, PERIODS, "201")
        assert out.tolist() == [700.0, 700.0, 700.0, 700.0]

    def test_consolidacao_ffill_por_empresa_nao_mistura_filiais(self):
        # Consolidação CNPJ raiz: pivot por (codi_emp, clas_cta). Hoje o pivot
        # só por clas_cta faz a filial 1 sumir no mês em que só a 2 tem linha.
        bal = pd.DataFrame({
            "codi_emp": [1, 2],
            "competencia": ["2025-01", "2025-02"],
            "clas_cta": ["2010101", "2010101"],
            "valo_saldo": [1000.0, 500.0],
        })
        out = bal_grupo_serie(bal, PERIODS, "201")
        assert out.tolist() == [1000.0, 1500.0, 1500.0, 1500.0]

    def test_bal_grupo_serie_ancora_por_conta(self):
        # Com codi_cta no grão, contas irmãs da MESMA clas_cta que
        # se movem em meses diferentes ancoram cada uma o próprio ffill.
        # Sem codi_cta no pivot, a célula (comp, clas) de fev veria só a
        # conta 8 (50) e o total despencaria de 150 pra 50.
        bal = pd.DataFrame({
            "codi_emp": [1, 1],
            "competencia": ["2025-01", "2025-02"],
            "clas_cta": ["2010101", "2010101"],
            "codi_cta": [7, 8],
            "valo_saldo": [100.0, 50.0],
        })
        out = bal_grupo_serie(bal, PERIODS, "201")
        assert out.tolist() == [100.0, 150.0, 150.0, 150.0]

    def test_bal_grupo_serie_sem_codi_cta_fallback_antigo(self):
        # Compat viva: DataFrame sem codi_cta (fixtures/mart antigo) segue
        # no pivot por (codi_emp, clas_cta) — mesma conta, saldo substitui.
        bal = pd.DataFrame({
            "codi_emp": [1, 1],
            "competencia": ["2025-01", "2025-02"],
            "clas_cta": ["2010101", "2010101"],
            "valo_saldo": [100.0, 50.0],
        })
        out = bal_grupo_serie(bal, PERIODS, "201")
        assert out.tolist() == [100.0, 50.0, 50.0, 50.0]

    def test_resultado_ignora_linha_fora_da_janela(self):
        # Congela: caminho de resultado (3/4) NÃO recebe carry-in — linha
        # fora de periods é descartada pelo reindex (seed nunca vaza).
        res = pd.DataFrame({
            "competencia": ["2024-12", "2025-03"],
            "clas_cta": ["3010101", "3010101"],
            "valo_saldo": [999.0, 300.0],
        })
        out = bal_grupo_serie(res, PERIODS, "301")
        assert out.tolist() == [0.0, 0.0, 300.0, 0.0]

    def test_bancos_e_aplicacoes_soma_cc_e_aplicacoes_irmas(self):
        # Card soma c/c (1010102) + aplicacoes (1010103), subgrupo
        # IRMAO. Caixa (1010101) fica FORA (= E40 do Excel).
        bal = pd.DataFrame({
            "competencia": ["2025-01", "2025-01", "2025-01"],
            "clas_cta":    ["1010101", "1010102", "1010103"],
            "valo_saldo":  [500.0,     100.0,     1200.0],
        })
        out = bal_grupo_serie(bal, PERIODS, GRUPOS_BAL["Bancos e Aplicações"])
        assert out.tolist() == [1300.0, 1300.0, 1300.0, 1300.0]

    def test_bancos_legada_continua_so_conta_corrente(self):
        # Regressao: GRUPOS_BAL["Bancos"] NAO muda (desacopla do BAL_VISUAL).
        bal = pd.DataFrame({
            "competencia": ["2025-01", "2025-01", "2025-01"],
            "clas_cta":    ["1010101", "1010102", "1010103"],
            "valo_saldo":  [500.0,     100.0,     1200.0],
        })
        out = bal_grupo_serie(bal, PERIODS, GRUPOS_BAL["Bancos"])
        assert out.tolist() == [100.0, 100.0, 100.0, 100.0]

    def test_sem_aplicacoes_tupla_igual_a_so_bancos(self):
        # Regressao-zero: empresa SEM 1010103 -> a tupla
        # ("1010102","1010103") da serie IDENTICA a so "1010102". Card nao muda.
        bal = pd.DataFrame({
            "competencia": ["2025-01", "2025-02", "2025-02"],
            "clas_cta":    ["1010102", "1010101", "1010102"],
            "valo_saldo":  [300.0,     900.0,     350.0],
        })
        so_bancos = bal_grupo_serie(bal, PERIODS, GRUPOS_BAL["Bancos"])
        tupla     = bal_grupo_serie(bal, PERIODS, GRUPOS_BAL["Bancos e Aplicações"])
        assert tupla.equals(so_bancos)

    def test_aplicacoes_captura_so_1010103(self):
        # Linha nova "Aplicacoes" do Streamlit = so subgrupo 1010103.
        bal = pd.DataFrame({
            "competencia": ["2025-01", "2025-01", "2025-01"],
            "clas_cta":    ["1010101", "1010102", "1010103"],
            "valo_saldo":  [500.0,     100.0,     1200.0],
        })
        out = bal_grupo_serie(bal, PERIODS, GRUPOS_BAL["Aplicações"])
        assert out.tolist() == [1200.0, 1200.0, 1200.0, 1200.0]


# ── classify_etl_proc (guard de processo do Atualizar) ──────────────────────

class _FakeProc:
    """Duck-type mínimo de subprocess.Popen: só .poll()."""
    def __init__(self, poll_result):
        self._poll_result = poll_result

    def poll(self):
        return self._poll_result


class TestClassifyEtlProc:
    def test_sem_processo_registrado_e_absent(self):
        assert classify_etl_proc(None) == "absent"

    def test_processo_vivo_e_alive(self):
        # Popen.poll() devolve None enquanto o processo roda.
        assert classify_etl_proc(_FakeProc(None)) == "alive"

    def test_processo_terminado_com_sucesso_e_finished(self):
        assert classify_etl_proc(_FakeProc(0)) == "finished"

    def test_processo_terminado_por_sinal_e_finished(self):
        # rc -15 = SIGTERM (terminate de cancel/timeout de run anterior).
        assert classify_etl_proc(_FakeProc(-15)) == "finished"


# ── consume_role / etl_progress_frac (consumidor único) ──────────────────────

class TestConsumeRole:
    # re-attach NAO-consome: consume_role faz try-lock NAO-bloqueante no
    # slot["lock"] (o mesmo do guard de processo). Quem adquire e o consumidor UNICO
    # ("owner"); quem nao adquire e "spectator" (so renderiza o snapshot, sem
    # tocar a Queue). Garante 1 consumidor -> fecha as 4 janelas do race.
    def test_lock_livre_vira_owner_e_adquire(self):
        slot = {"lock": threading.Lock()}
        assert consume_role(slot) == "owner"
        assert slot["lock"].locked()       # adquiriu; owner solta no finally

    def test_lock_segurado_vira_spectator(self):
        slot = {"lock": threading.Lock()}
        slot["lock"].acquire()
        assert consume_role(slot) == "spectator"

    def test_sem_lock_vira_spectator(self):   # defensivo
        assert consume_role({}) == "spectator"


class TestEtlProgressFrac:
    def test_zero(self):
        assert etl_progress_frac(0, 38) == 0.0

    def test_teto_095(self):
        assert etl_progress_frac(38, 38) == 0.95

    def test_meio(self):
        assert etl_progress_frac(19, 38) == 0.5


# ── luc_nf (numerador NF-based da lucratividade) ─────────────────────────────

class TestLucNf:
    def test_subtrai_despesa_e_imposto_do_faturamento(self):
        # NF-based: fat - desp - imp (NAO receita_contabil - desp).
        fat = pd.Series([1000.0, 1000.0])
        desp = pd.Series([200.0, 200.0])
        imp = pd.Series([100.0, 100.0])
        out = luc_nf(fat, desp, imp)
        assert out.tolist() == [700.0, 700.0]

    def test_imposto_nan_subtrai_zero(self):
        # mes sem dado de imposto subtrai 0 (consistente com fat fill_value=0).
        out = luc_nf(pd.Series([1000.0]), pd.Series([200.0]), pd.Series([float("nan")]))
        assert out.tolist() == [800.0]


# ── regime_dominante (supressao da lucratividade p/ Simples) ─────────────────

class TestRegimeDominante:
    def test_simples_maioria(self):
        df = pd.DataFrame({"regime": ["simples", "simples", "real_presumido"]})
        assert regime_dominante(df) == "simples"

    def test_real_presumido_maioria(self):
        df = pd.DataFrame({"regime": ["real_presumido", "real_presumido", "simples"]})
        assert regime_dominante(df) == "real_presumido"

    def test_empate_vira_none(self):
        df = pd.DataFrame({"regime": ["simples", "real_presumido"]})
        assert regime_dominante(df) is None

    def test_vazio_vira_none(self):
        assert regime_dominante(pd.DataFrame()) is None
        assert regime_dominante(None) is None


class TestRotuloTipoProcess:
    """Rotulo dos tipos de processo da folha.

    A origem NAO tem catalogo: o rotulo vive na camada de aplicacao do
    Dominio. Exibe SO O NOME: o codigo e jargao de sistema e nao ajuda quem
    le o relatorio; ele so aparece quando NAO ha nome pro codigo.
    """
    def test_os_oito_conhecidos(self):
        assert rotulo_tipo_process(11) == "Folha Mensal"
        assert rotulo_tipo_process(41) == "Adiantamento"
        assert rotulo_tipo_process(42) == "Rescisão Complementar"
        assert rotulo_tipo_process(51) == "13º Salário (1ª parcela)"
        assert rotulo_tipo_process(52) == "13º Salário (2ª parcela)"
        assert rotulo_tipo_process(60) == "Férias"
        assert rotulo_tipo_process(70) == "PLR"
        assert rotulo_tipo_process(92) == "Recibos de Autônomos (RPA)"

    def test_codigo_desconhecido_mostra_o_numero(self):
        # tipo novo do Dominio NUNCA pode quebrar a tela; aqui o numero e a
        # unica informacao disponivel, entao ele aparece
        assert rotulo_tipo_process(99) == "Tipo 99"

    def test_aceita_string_e_float_do_dataframe(self):
        # a coluna vem do mart podendo ser int64/float/str conforme o path
        assert rotulo_tipo_process("11") == "Folha Mensal"
        assert rotulo_tipo_process(11.0) == "Folha Mensal"

    def test_valor_nao_numerico_nao_quebra(self):
        assert rotulo_tipo_process(None) == "—"
        assert rotulo_tipo_process("xx") == "xx"

    def test_nan_vira_travessao(self):
        # a docstring promete NaN -> "—" e o guard usa math.isnan; sem o
        # import math (utils.py) isso seria NameError em producao
        assert rotulo_tipo_process(float("nan")) == "—"

    def test_coluna_pandas_com_buraco(self):
        # caso REAL do app: folha_df["tipo_process"].map(rotulo_tipo_process)
        # numa coluna com NULL -> o np.float64('nan') chega na funcao
        col = pd.Series([11, None, 92], dtype="float64")
        assert list(col.map(rotulo_tipo_process)) == [
            "Folha Mensal", "—", "Recibos de Autônomos (RPA)"]

    def test_dicionario_cobre_o_universo_medido(self):
        from etl.mappings import TIPO_PROCESS_NOMES
        # os 8 da FOBASES na historia COMPLETA da origem
        assert set(TIPO_PROCESS_NOMES) == {11, 41, 42, 51, 52, 60, 70, 92}


class TestFmtBrlZeroNegativo:
    """'R$ -0,00' na tela.

    O saldo do grupo dava algo como -0,004 e o arredondamento de exibicao
    produzia 'R$ -0,00', que le como valor negativo e nao existe em
    contabilidade.

    Normalizacao na FONTE UNICA (fmt_brl): so o que ARREDONDA pra zero vira
    zero. Valor negativo REAL nao e tocado — a nota de saldo invertido
    depende de o negativo aparecer cru.
    """
    def test_zero_negativo_vira_zero(self):
        assert fmt_brl(-0.0) == "R$ 0,00"

    def test_residuo_que_arredonda_pra_zero_vira_zero(self):
        # o caso real: -0,004 exibia "R$ -0,00"
        assert fmt_brl(-0.004) == "R$ 0,00"
        assert fmt_brl(-0.0049) == "R$ 0,00"

    def test_zero_positivo_intocado(self):
        assert fmt_brl(0.0) == "R$ 0,00"
        assert fmt_brl(0.004) == "R$ 0,00"

    def test_negativo_REAL_permanece_negativo(self):
        # O card de Ativo Total negativo tem que exibir o numero CRU pra
        # ganhar a nota de saldo invertido.
        assert fmt_brl(-0.01) == "R$ -0,01"
        assert fmt_brl(-10_840_952.47) == "R$ -10.840.952,47"

    def test_positivo_intocado(self):
        assert fmt_brl(1234.56) == "R$ 1.234,56"
        assert fmt_brl(38_835_513.03) == "R$ 38.835.513,03"

    def test_nan_e_none_seguem_travessao(self):
        assert fmt_brl(None) == "—"
        assert fmt_brl(float("nan")) == "—"
        assert fmt_brl(float("inf")) == "—"


class TestFmtBrlCurto:
    """Rotulo abreviado das barras. Fonte UNICA app + PDF."""

    def test_milhao_com_uma_casa(self):
        assert fmt_brl_curto(1234567.89) == "1,2M"

    def test_mil_arredonda_pra_inteiro(self):
        # " mil" por extenso: o "K"
        # e jargao de planilha, e o rotulo e pra reuniao com o cliente.
        assert fmt_brl_curto(987654) == "988 mil"

    def test_o_sufixo_K_nao_sobrevive_em_lugar_nenhum(self):
        # guarda de regressao: qualquer caminho que volte a emitir
        # "K" reintroduz o jargao numa faixa so do chart.
        for v in (1_000, 2_500, 987_654, -2_500, 999_999, 1_234_567.89, 850):
            assert "K" not in fmt_brl_curto(v)

    def test_o_separador_do_sufixo_e_espaco(self):
        # "988mil" colado le como palavra unica; o espaco e o que faz o rotulo
        # virar leitura ("988 mil") em vez de codigo.
        assert fmt_brl_curto(1_000) == "1 mil"
        assert fmt_brl_curto(12_345) == "12 mil"

    def test_abaixo_de_mil_sai_inteiro(self):
        assert fmt_brl_curto(850) == "850"

    def test_zero_nao_ganha_rotulo(self):
        # barra de altura zero com "0" em cima e ruido
        assert fmt_brl_curto(0) == ""
        assert fmt_brl_curto(0.4) == ""

    def test_sem_dado_nao_ganha_rotulo(self):
        assert fmt_brl_curto(None) == ""
        assert fmt_brl_curto(float("nan")) == ""
        assert fmt_brl_curto(float("inf")) == ""

    def test_borda_de_1000_mil_promove_pra_M(self):
        # 999.999 arredondaria pra "1000 mil" — mais largo que a faixa e feio
        # ao lado de "1,0M" do mes seguinte. O "M" FICA (so o "K" virou
        # palavra): "1,2 milhoes" dobraria a largura do rotulo mais comum.
        assert fmt_brl_curto(999999) == "1,0M"
        assert fmt_brl_curto(999499) == "999 mil"

    def test_negativo_mantem_o_sinal(self):
        assert fmt_brl_curto(-1500000) == "-1,5M"
        assert fmt_brl_curto(-2500) == "-3 mil"

    def test_separador_decimal_e_virgula(self):
        # padrao BR: nunca ponto decimal no rotulo
        assert "." not in fmt_brl_curto(1234567.89)
        assert fmt_brl_curto(1500000) == "1,5M"

    def test_arredondamento_e_half_up_nao_half_even(self):
        # o `.0f` nativo do Python daria "2 mil" pra 2.500 e "4 mil" pra 3.500 —
        # os dois lado a lado num chart parecem defeito.
        assert fmt_brl_curto(2500) == "3 mil"
        assert fmt_brl_curto(3500) == "4 mil"


class TestNormalizaRubrica:
    """Criterio por nome de rubrica normaliza acentuacao antes de casar."""

    def test_tira_acento_de_rubrica(self):
        s = pd.Series(["SALARIO FAMÍLIA", "SALÁRIO FAMILIA", "SALARIO FAMILIA",
                       "SUSPENSÃO", "RESCISÃO"])
        out = normaliza_rubrica(s)
        assert list(out) == ["SALARIO FAMILIA", "SALARIO FAMILIA",
                             "SALARIO FAMILIA", "SUSPENSAO", "RESCISAO"]

    def test_o_regex_ascii_ingenuo_perde_sem_a_normalizacao(self):
        # a prova de que a regra e necessaria, nao preferencia de estilo:
        # "SALARIO FAMÍLIA" tem I ACENTUADO e nao casa 'FAMIL' cru
        import re as _re
        cru = "SALARIO FAMÍLIA"
        assert _re.search("FAMIL", cru) is None
        assert _re.search("FAMIL", normaliza_rubrica(cru)) is not None
        # e "SUSPENSÃO" (familia Falta Integral) so existe acentuada
        assert _re.search("SUSPENSAO", "SUSPENSÃO") is None
        assert _re.search("SUSPENSAO", normaliza_rubrica("SUSPENSÃO")) is not None

    def test_escalar_e_serie_devolvem_o_mesmo_formato(self):
        assert normaliza_rubrica("ferias") == "FERIAS"
        assert isinstance(normaliza_rubrica(pd.Series(["a"])), pd.Series)

    def test_nome_ausente_nao_explode(self):
        out = normaliza_rubrica(pd.Series([None, float("nan"), "OK"]))
        assert list(out)[-1] == "OK"
