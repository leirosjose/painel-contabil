"""Guarda de regressao para a superficie Streamlit.

app.py nao e import-safe (st.set_page_config + DB no top-level), logo os
guards sao source-level. O painel auxiliar 'Encargos (FGTS / INSS)' e a
captura viciada via RE_CONTAS_ENCARGOS (bal_df por nome, sem filtro de
clas_cta, somando estoque de passivo 2xx + INSS retido) NAO podem reaparecer."""
import textwrap
from pathlib import Path

APP_SRC = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
# APP_SRC SEM as linhas de comentario. Guard que BANE um token tem de olhar o
# CODIGO, nao a mencao: os comentarios do app.py podem explicar justamente por
# que um token nao volta, e um `not in APP_SRC` cru reprovaria a documentacao.
# So linhas 100% comentario saem — o CSS vive dentro de string triplo-aspas e
# continua visivel, que e o ponto.
APP_CODE = "\n".join(
    ln for ln in APP_SRC.splitlines() if not ln.strip().startswith("#"))

# Rotulos que os DOIS consumers exibem vivem no metrics (fonte unica): quando
# a guarda e sobre o texto, e la que ela tem que ser pinada.
METRICS_SRC = (Path(__file__).resolve().parent.parent / "etl" / "metrics.py"
               ).read_text(encoding="utf-8")


def test_streamlit_nao_usa_constante_de_encargos_viciada():
    # regex FGTS|INSS sem filtro de classe — capturava passivo 2xx + INSS retido
    assert "RE_CONTAS_ENCARGOS" not in APP_SRC


def test_streamlit_nao_exibe_painel_de_encargos():
    # painel auxiliar (estoque 2xx viciado) segue MORTO; o encargo entra pelo
    # card do Resumo Executivo, nao por painel proprio
    assert "Encargos (FGTS / INSS)" not in APP_SRC


def test_streamlit_card_folha_encargos_fiel_a_guia():
    # Card do Resumo Executivo: "% Folha + Encargos" com numerador do
    # mart.encargos_mensais; guard de tabela ausente (1o deploy antes do 1o
    # ETL) via information_schema.
    assert '"% Folha + Encargos"' in APP_SRC
    assert 'st.metric("% Folha"' not in APP_SRC          # label antigo morreu
    assert "mart.encargos_mensais" in APP_SRC
    assert "information_schema.tables" in APP_SRC        # guard de 1o deploy


def test_streamlit_expoe_linha_aplicacoes_separada():
    # Balancete do Streamlit ganha linha "Aplicacoes" (1010103), separada de
    # "Bancos" (1010102 so c/c). Card do PDF soma os dois.
    assert 'GRUPOS_BAL["Aplicações"]' in APP_SRC


def test_streamlit_folha_tem_um_total_so():
    # UM custo: "nao existem dois custos totais". A ponte de 3 degraus e a
    # tabela paralela por tipo_process MORRERAM — o app renderiza UMA tabela,
    # com UM fundo (CUSTO TOTAL) e UM percentual.
    assert "folha_exibida" not in APP_SRC            # criterio antigo morreu
    assert "piv_folha" not in APP_SRC                # tabela paralela morreu
    assert "LINHA_FOLHA_ENCARGOS" in APP_SRC
    assert "LINHA_PCT_FAT" in APP_SRC
    assert "TOTAL exibido" not in APP_SRC
    assert "TOTAL de custo" not in APP_SRC


def test_streamlit_consumidor_unico_via_consume_role():
    # Race na Queue: re-attach NAO consome. Crava a fiacao do consumidor unico
    # no source (app.py nao e import-safe). consume_role decide
    # owner/spectator; o owner solta o lock no finally; cancel cross-tab chega
    # no owner via slot["cancel_req"].
    assert "consume_role(slot)" in APP_SRC
    assert '== "spectator"' in APP_SRC
    assert 'slot["lock"].release()' in APP_SRC
    assert 'slot["cancel_req"] = True' in APP_SRC     # botao seta (qualquer aba)
    assert 'slot.get("cancel_req")' in APP_SRC         # owner checa no loop


def test_streamlit_incremental_decide_pela_origem_nao_por_mart():
    # O incremental consulta watermark (ops.controle_runs) × fim DECLARADO da
    # origem via subprocess `--probe-janela`, e a janela decidida é exibida
    # ANTES do run. Nada de adivinhar pelo mart.
    fn_src = APP_SRC.split("def _etl_periods")[1].split("\ndef ")[0]
    assert "SELECT MAX(competencia)" not in fn_src   # adivinhação morta
    assert "--probe-janela" in APP_SRC
    assert "ops.controle_runs" in APP_SRC            # watermark visível na UI


def test_streamlit_incremental_origem_atras_para_com_aviso_nunca_verde():
    # origem sem períodos novos = st.warning âmbar explícito e NÃO roda —
    # nunca verde silencioso.
    assert '"noop"' in APP_SRC
    assert "sidebar.warning" in APP_SRC


def test_streamlit_incremental_explica_dado_parcial_alem_do_declarado():
    # Quando a origem tem dado parcial além do mês declarado, o painel do run
    # EXPLICA por que a janela para ali (ex.: "dados de Abr/26 ainda parciais
    # ... rodando até Mar/26"). O contexto persiste no SLOT (sobrevive ao
    # rerun e a re-attach de outra aba).
    assert "alem_declarado" in APP_SRC
    assert '"janela_ctx"' in APP_SRC
    assert "parciais na origem" in APP_SRC
    # O painel renderiza ANTES do spawn popular o slot — sem o fallback o
    # caption só aparece após o primeiro rerun de polling. A mensagem deve
    # estar visível desde o primeiro render (mesmo fallback da linha Período).
    assert ('slot.get("janela_ctx") or st.session_state.get("etl_janela_ctx")'
            in APP_SRC)


def test_streamlit_tabela_impostos_rotula_universo_guia():
    # A linha % da tabela ganha o rotulo do UNIVERSO + caption muted enxuto; o
    # rotulo antigo sem qualificacao morre. Pra RP o card e a tabela medem
    # universos diferentes — a tela NOMEIA isso. A tabela tem a linha ISS pela
    # carga (balancete/guia/retido), entao o % e a soma DO QUE ESTA NA TABELA,
    # e continua != do card por ICMS a-recolher x destacado e IRPJ/CSLL guia x
    # provisao. O texto e a constante LINHA_PCT_IMPOSTOS no metrics — o styler
    # casa a linha por NOME, e com PDF e app lendo a mesma tabela um literal
    # duplicado faria a linha perder cor/percentual em um dos dois, em
    # silencio. O texto exato e cravado em TestTabelaImpostos (metrics).
    assert "LINHA_PCT_IMPOSTOS" in APP_SRC
    assert '"% sobre Faturamento (impostos em guia)"' not in APP_SRC
    # label antigo morreu NA TABELA DE IMPOSTOS (a de folha tem teste proprio:
    # test_streamlit_tabela_folha_rotula_universo).
    assert 'piv_imp.loc["% sobre Faturamento"]' not in APP_SRC
    assert 'style_com_pct_row(piv_imp, "% sobre Faturamento",' not in APP_SRC


def test_streamlit_card_impostos_tem_help_de_fonte():
    # O card % Impostos ganha help= (tooltip) com o vocabulario unificado, a
    # frase-regua do IRPJ/CSLL e o detalhe tecnico da fonte. O numerador e o
    # MESMO total que a tabela da secao Impostos exibe, fonte unica pros dois
    # regimes (ver test_streamlit_card_impostos_nao_chama_a_tabela_de_outro_universo).
    assert "help=_imp_help" in APP_SRC
    assert "de impostos do " in APP_SRC
    assert "IRPJ/CSLL sobre o " in APP_SRC
    assert "recolheu em guia no período" in APP_SRC


def test_streamlit_card_impostos_nao_chama_a_tabela_de_outro_universo():
    # O numerador do card e o MESMO total que a linha TOTAL da tabela da secao
    # Impostos exibe (guia + ISS pela carga) -- card e tabela sao o MESMO
    # universo, nunca "outro". Checa no CODIGO (comentarios fora, `APP_CODE`)
    # para nao reprovar a documentacao.
    assert "impostos destacados nas notas (balancete)" not in APP_CODE
    assert "Impostos lista outro universo (guias)" not in APP_CODE
    # Ramo `imp_sem_dado`: nenhuma ocorrencia de "outro universo" sobra no
    # CODIGO do app inteiro.
    assert "Não há imposto destacado nem" not in APP_CODE
    assert "outro universo" not in APP_CODE


def test_streamlit_ir_muda_de_casa_para_o_card_de_impostos():
    # IR muda de casa: a frase-regua INVERTE (o card INCLUI o IRPJ/CSLL) e a
    # linha "IRPJ/CSLL do periodo: R$ X" MIGRA do tooltip da Lucratividade pro
    # do % Impostos, com a MESMA regra de omissao. Fonte unica =
    # K["irpj_total"] do metrics (o sum inline do dre_df morreu).
    assert "inclui IRPJ/CSLL sobre o lucro" in APP_SRC
    assert "não entram aqui" not in APP_SRC          # regua antiga morreu
    assert "já estão descontados na" not in APP_SRC
    assert "(já descontados neste resultado)" not in APP_SRC
    assert 'K["irpj_total"]' in APP_SRC
    assert 'dre_df["provisao_irpj_csll"].sum()' not in APP_SRC
    # tooltip da Lucratividade declara a base nova
    assert "antes do IRPJ/CSLL" in APP_SRC


def test_streamlit_escapa_cifrao_nos_tooltips_com_valor():
    # O `help=` do st.metric e markdown igual ao st.caption — DOIS "R$" na
    # mesma string fecham par e o Streamlit renderiza o trecho entre eles como
    # formula, comendo os cifroes. O tooltip de % Impostos tem a linha
    # "IRPJ/CSLL do periodo: R$ X"; o de % Folha + Encargos tem folha +
    # encargos na mesma string.
    #
    # O escape mora no RENDER, nunca no builder: `_imp_help` e `_fol_help`
    # sao montados com fmt_brl, que e compartilhado com o PDF (HTML, sem o
    # problema) — escapar na fonte sujaria o PDF. MESMO idioma que as
    # captions de legenda_outros/legenda_abatimentos ja usam.
    assert r'help=_imp_help.replace("$", "\\$")' in APP_SRC
    assert r'help=_fol_help.replace("$", "\\$")' in APP_SRC
    # o tooltip de Lucratividade NAO carrega valor em R$ hoje (3 ramos, zero
    # fmt_brl) — sem cifrao nao ha par, e escapar ali seria ruido.
    assert "help=\"  \\n\".join(_luc_help_parts)" in APP_SRC


def test_streamlit_regua_do_irpj_sai_no_simples():
    # A frase so aparece quando a
    # empresa APURA IRPJ/CSLL. O criterio e o helper do metrics (fonte unica
    # com o PDF) — o app nao pode ter regra propria de regime, senao as duas
    # telas divergem com a mesma frase.
    assert "impostos_sem_irpj_proprio" in APP_SRC
    assert "impostos_sem_irpj_proprio" in METRICS_SRC
    assert "_regua_ir" in APP_SRC
    # a frase segue existindo — o que mudou e QUANDO ela sai
    assert "inclui IRPJ/CSLL sobre o lucro" in APP_SRC
    # o resto do tooltip NAO some junto: a explicacao de onde vem o numero
    # continua la (fonte UNICA pros dois regimes).
    assert "recolheu em guia no período" in APP_SRC


def test_streamlit_balancete_usa_despesas_e_custos_401_405():
    # Paridade Excel: a linha do balancete vira "Despesas e Custos" (401-405,
    # rotulo do Excel — SEM IRPJ/CSLL do 406). A antiga "Despesas" (401-406)
    # SAI do chart/tabela; o 406 segue visivel nos impostos e dentro da
    # lucratividade.
    assert 'GRUPOS_BAL["Despesas e Custos"]' in APP_SRC
    assert '"Despesas":' not in APP_SRC                  # linha antiga morreu


def test_streamlit_exibe_valores_do_balanco_com_formulas():
    # Paridade Excel: secao Indicadores ganha os 5 valores-insumo
    # (st.metric, espelho dos cards do PDF) + formulas dos indicadores por
    # extenso como caption muted (padrao das notas tipo "saldo credor").
    assert 'st.metric("Ativo Circulante"' in APP_SRC
    assert 'st.metric("Ativo Total"' in APP_SRC
    assert 'st.metric("Estoque"' in APP_SRC
    assert 'st.metric("Exigível LP"' in APP_SRC
    assert 'st.metric("Passivo Circulante"' in APP_SRC
    assert "LC = Ativo Circulante ÷ Passivo Circulante" in APP_SRC
    assert "LS = (Ativo Circulante − Estoque) ÷ Passivo Circulante" in APP_SRC
    assert "Endividamento = (Passivo Circulante + Exigível LP) ÷ Ativo Total" in APP_SRC


def test_streamlit_incremental_avisa_como_incluir_o_parcial_via_custom():
    # Junto do texto explicativo, o aviso ACIONÁVEL — a data exata até onde há
    # dados e o caminho ("rode com Período customizado") pra quem quiser
    # incluí-los já.
    assert "alem_declarado_data" in APP_SRC
    assert "rode com" in APP_SRC


def test_streamlit_lucratividade_le_mart_dre_mensal():
    # A lucratividade le mart.dre_mensal (resultado contabil) com guard de 1o
    # deploy e injeta dre_df no metrics; o proxy NF-based morre.
    assert "mart.dre_mensal" in APP_SRC
    assert "table_name='dre_mensal'" in APP_SRC          # guard de 1o deploy
    assert "dre_df=dre_df" in APP_SRC                     # injetado em series_mensais
    assert "Lucratividade NF-based" not in APP_SRC        # comentario/proxy antigo morreu


def test_streamlit_lucratividade_nao_suprime_por_regime():
    # A supressao por regime (Simples em bloco) morreu; a supressao passa a
    # ser SO por dado (despesa da DRE via luc_sem_despesas).
    assert "luc_simples" not in APP_SRC
    assert "regime_dominante" not in APP_SRC              # import/uso removidos
    assert "luc_sem_registros = S[" in APP_SRC            # so o trigger de dado


def test_streamlit_lucratividade_fallback_generico_sem_serie():
    # Guard de cobertura de receita: serie toda NaN sem supressao por
    # despesas -> fallback generico, nunca chart vazio mudo.
    assert "Sem dados para calcular lucratividade" in APP_SRC


def test_streamlit_movimentacao_rotulos_entradas_saidas():
    # Fluxo folha-ancorado — traces/tabela viram "Entradas"/"Saídas", a
    # caption antiga (admissoes em fim de mes = explicacao errada) morre e
    # entra o one-liner de semantica + frase do ⚠️.
    assert 'name="Entradas"' in APP_SRC
    assert 'name="Saídas"' in APP_SRC
    assert '"Admissões"' not in APP_SRC
    assert '"Rescisões"' not in APP_SRC
    assert "admissões em fim de mês" not in APP_SRC
    # frase semantica saiu tambem do app (espelho do PDF); fica so a legenda
    # do marcador de fechamento.
    assert "afastamentos e retornos" not in APP_SRC
    assert "dados em fechamento" in APP_SRC


def test_streamlit_cards_yoy_substitui_vs_inicio():
    # YoY delta-no-card: os 4 deltas do Resumo Executivo viram "vs mesmo
    # periodo do ano anterior" via kpis_yoy (fonte unica metrics).
    assert "kpis_yoy" in APP_SRC
    # deltas inline ultimo-vs-primeiro-mes MORTOS:
    assert "fat_por_mes.iloc[-1] - fat_por_mes.iloc[0]" not in APP_SRC
    assert "pct_imp_pm.iloc[-1]" not in APP_SRC
    assert "pct_folenc_pm.iloc[-1]" not in APP_SRC
    assert "pct_luc_pm.iloc[-1]" not in APP_SRC
    # frase e unidade explicita (fat em %, razoes em p.p.):
    # "vs" morreu nos comparativos — padrao de relatorio financeiro BR e
    # "em relacao ao mesmo periodo".
    assert "em relação ao mesmo período/" in APP_SRC
    assert "vs mesmo período" not in APP_SRC
    assert " p.p." in APP_SRC
    # o label "sem dados de {ano}" NUNCA e hardcoded no consumer — vem
    # verbatim do card["label"] do metrics (fonte unica):
    assert '"sem dados de' not in APP_SRC
    # fallback do label/sem-delta usa cor neutra:
    assert '"off"' in APP_SRC


def test_streamlit_yoy_limite_12_meses_sem_balancete_prev():
    # range > 12 meses: janela -12m sobrepoe a atual -> SEM YoY (nem label);
    # e a janela prev NAO carrega balancete (mart pesado; nenhum card YoY usa).
    assert "len(periods) <= 12" in APP_SRC
    # 3 queries legitimas: loader principal, seed de carry-in e o UNION do
    # selectbox de competencias. A janela YoY nao pode adicionar uma 4a
    # (balancete e o mart pesado).
    assert APP_SRC.count("FROM mart.balancete_mensal") == 3


def test_streamlit_tabela_impostos_so_proprios():
    # A tabela de guias mostra so imposto PROPRIO (sem retencao de terceiros
    # nem SCP), via o filtro canonico do metrics — nada de lista inline no
    # app. O app chama tabela_impostos, que chama impostos_exibidos. O PDF de
    # Detalhamento de Impostos consome o MESMO helper.
    assert "tabela_impostos(imp_df, impd_df, periods, fat_por_mes," in APP_SRC
    assert "impostos_exibidos(" not in APP_SRC            # nao reimplementa
    assert "CODI_IMP_RETENCAO_TERCEIROS" not in APP_SRC   # sem lista inline
    assert "CODI_IMP_SCP" not in APP_SRC


def test_streamlit_so_retencao_tem_label_proprio():
    # empresa que so e agente de retencao: frase honesta, NUNCA o generico
    # "Sem dados de impostos" (que mentiria — guias existem).
    assert "Sem impostos próprios no período (apenas retenções de " in APP_SRC
    assert "so_retencao" in APP_SRC
    # o warning generico sobrevive pro caso legitimo (zero guia no periodo)
    assert "⚠️ Sem dados de impostos para o período selecionado" in APP_SRC


def test_streamlit_caption_da_tabela_declara_o_recorte_novo():
    # caption enxuta — diz o que a tabela E (o que a empresa recolheu) e o
    # que fica fora; o contraste tecnico com o card/destacado vive no tooltip
    # do card.
    assert "impostos que a empresa recolheu no período" in APP_SRC
    assert "retenções de terceiros e tributos de SCP ficam fora" in APP_SRC
    assert "impostos apurados em guia (DAS/ISS/retenções)" not in APP_SRC
    assert "o card % Impostos mede a carga destacada" not in APP_SRC


def test_streamlit_folha_rotula_tipo_process():
    # O numero cru do tipo_process sai da tela — vira o nome na legenda do
    # chart. Helper canonico em etl.utils (dicionario em mappings; a origem
    # nao tem catalogo), nunca dict inline no app. O rotulo vive SO no chart
    # de degradacao (empresa sem carga do mart de rubricas).
    assert "rotulo_tipo_process" in APP_SRC
    assert "TIPO_PROCESS_NOMES" not in APP_SRC              # sem dict inline
    assert "go.Bar(name=rotulo_tipo_process(tp)" in APP_SRC  # legenda do chart
    assert 'go.Bar(name=str(tp)' not in APP_SRC             # rotulo cru morreu


def test_streamlit_bloco_impostos_tem_linha_iss_da_carga():
    # O bloco lia SO guias e empresa com ISS 100% retido na fonte exibia
    # ZERO. A linha ISS vem do componente `valo_iss` do mart, via fonte unica
    # em metrics (tabela_impostos: de-dup do ISS, guard do ramo vazio e
    # montagem da linha), pro PDF consumir o MESMO codigo. O COMPORTAMENTO e
    # cravado em TestTabelaImpostos; aqui fica a fronteira: o app nao pode
    # reimplementar nada disso.
    assert "impd_df, periods, fat_por_mes" in APP_SRC     # a fonte do ISS entra
    assert 'imp_proprios["codi_imp"] != 3' not in APP_SRC  # de-dup nao vaza
    assert 'piv_imp.loc["ISS"]' not in APP_SRC
    # empresa com ISS e SEM guia propria no mes nao pode cair no ramo "Sem
    # dados de impostos".
    assert 'if _I["tem_dado"]:' in APP_SRC
    # a caption declara a composicao hibrida
    assert "ISS pela carga tributária" in APP_SRC


def test_streamlit_chart_impostos_tem_barra_de_iss():
    # o ISS entra no GRAFICO tambem, nao so na tabela — senao a empresa com
    # ISS e nenhuma guia propria no mes renderiza chart VAZIO.
    # Quem decide QUAIS series o chart recebe (soma zero fica fora, ordem
    # alfabetica compartilhada com a tabela) e o metrics — o app so itera.
    # Sem isso o chart podia voltar a divergir da tabela ao lado.
    assert 'for i, nome in enumerate(_I["series_chart"]):' in APP_SRC
    assert 'series_imp["ISS"] = iss_serie' not in APP_SRC
    assert "if float(vals.abs().sum()) == 0:" not in APP_SRC


def test_streamlit_tabela_folha_rotula_universo():
    # O card do hero e a linha usam a MESMA base (custo total com 13o e
    # encargos), entao um qualificador de exclusao ("folha, sem encargos")
    # MENTIRIA. O rotulo e simples e a coerencia e garantida por
    # TestCoerenciaCardTabelaFolha (metrics).
    assert 'LINHA_PCT_FAT = "% sobre Faturamento"' in METRICS_SRC
    assert "(folha, sem encargos)" not in APP_SRC


def test_streamlit_indicadores_declaram_o_mes_do_balancete():
    # O bloco de Indicadores NOMEIA o mes do balancete (como o balanco_note
    # do PDF): empresa que para de escriturar antes do fim da janela exibe o
    # ffill do ultimo saldo conhecido, e "fim do periodo" nao diz de quando
    # ele e. O mes e col_lbls[-1] porque e o MESMO que o last_valid escolhe
    # nos 5 cards. A f-string carrega `{_sem_mov}` colado ao mes ("sem
    # movimento no ultimo mes") e e condicional a `bal_tem_dado_periodo`
    # ("so fora da janela" degrada pra NOTA_BAL_SEM_DADO_PERIODO em vez de
    # nomear um mes emprestado).
    assert 'f"Valores do balancete em {col_lbls[-1]}{_sem_mov} · "' in APP_SRC
    assert "Valores do balancete no fim do período" not in APP_SRC
    assert "NOTA_BAL_SEM_DADO_PERIODO" in APP_SRC
    assert "NOTA_BAL_SEM_MOVIMENTO_PERIODO" in APP_SRC


def test_streamlit_cards_de_balanco_anotam_saldo_invertido():
    # "Ativo Total" negativo cru ganha a MESMA anotacao que Bancos usa. O
    # valor segue CRU — esconder apagaria a evidencia de escrituracao
    # invertida.
    assert "cards_saldo_invertido" in APP_SRC
    assert "NOTA_SALDO_INVERTIDO" in APP_SRC
    assert "_bal_inv" in APP_SRC
    # nunca abs()/ocultacao no valor dos cards de balanco
    assert "abs(_bal_val" not in APP_SRC


class TestBlocoFolhaPorRubrica:
    """Bloco de folha por rubrica.

    app.py nao e import-safe, entao o guard e source-level (padrao do arquivo).
    """

    def test_le_o_mart_de_rubricas_com_guard_de_1o_deploy(self):
        # mesmo idioma do dre_mensal/encargos_mensais: o mart pode nao existir
        # antes da 1a carga com extract — degrada, nunca lanca.
        assert "mart.folha_rubricas_mensais" in APP_SRC
        assert "table_name='folha_rubricas_mensais'" in APP_SRC

    def test_usa_o_helper_canonico_e_nao_reimplementa(self):
        # fonte unica com o PDF: a particao das linhas mora no metrics, o app so
        # renderiza.
        assert "folha_por_linha" in APP_SRC
        assert "RE_REFLEXO_DSR" not in APP_SRC     # criterio nao vaza pro app
        assert "RE_RESCISAO" not in APP_SRC

    def test_o_card_le_a_mesma_base_da_tabela(self):
        """`rub_df` desce pro `series_mensais`, senao o card e a
        tabela voltam a ser dois universos com o mesmo nome na tela."""
        assert "rub_df=rub_df" in APP_SRC

    def test_o_yoy_do_card_le_a_mesma_base(self):
        """O FIO REAL que o default None do kwarg nao cobre: sem `rub_prev` o
        delta compararia o custo novo contra o antigo e viraria ruido."""
        assert "rub_prev" in APP_SRC
        assert "rub_df=rub_prev" in APP_SRC
        # e com o MESMO guard de 1o deploy da janela atual
        assert "if _rub_exists else pd.DataFrame()" in APP_SRC

    def test_a_ponte_de_tres_degraus_morreu(self):
        # A ponte mostrava dois totais com nomes parecidos e obrigava o leitor a
        # escolher um. Sobrou UM fundo de tabela.
        assert "(-) 13º Salário (processado à parte)" not in APP_SRC
        assert 'LINHA_FOLHA_ENCARGOS] = [fmt_brl(v) for v in _R["custo"]]' in APP_SRC
        assert 'LINHA_PCT_FAT] = [fmt_pct(v) for v in _R["pct_fat"]]' in APP_SRC

    def test_linha_13o_declara_onde_o_resto_pousa(self):
        # O corte por tp52 isola o integral e nao conta 2x, mas joga a
        # familia-13o paga DENTRO do mensal (tp11: medias, vantagens,
        # diferencas) pra linha Outros. Sem a nota, quem abrir "13o Salario"
        # em julho ve R$ 0,00 e um "Outros" inflado.
        assert "NOTA_LINHA_13O" in APP_SRC

    def test_encargos_declaram_a_fonte_guia(self):
        # encargo vem de OUTRA fonte (mart.encargos_mensais) e tem outra
        # natureza — dai o rotulo declarar "(guia)". Mas o custo e UM so, entao
        # a linha SOMA: ela e montada no metrics e chega pronta na tabela.
        assert 'LINHA_ENCARGOS = "Encargos (INSS + FGTS, guia)"' in METRICS_SRC
        assert "encargos_df=encargos_df" in APP_SRC
        assert "(+) INSS patronal (guia)" not in APP_SRC   # linha separada morreu
        assert "CUSTO TOTAL COM ENCARGOS" not in APP_SRC   # segundo total morreu

    def test_encargos_saem_do_mesmo_frame_do_card_do_hero(self):
        # coerencia: o bloco novo e o card "% Folha + Encargos" leem o MESMO
        # encargos_df, mesma janela — nao pode divergir por fonte.
        assert "encargos_df" in APP_SRC
        assert "SELECT * FROM mart.encargos_mensais" in APP_SRC

    def test_visao_anterior_morreu(self):
        # Manter uma segunda tabela com outro criterio e exatamente o que a regra
        # ("nao existem dois custos totais") veio matar.
        assert 'st.expander("Visão anterior (por tipo de processo)"' not in APP_SRC

    def test_o_agrupamento_outros_MORREU(self):
        """A caixa-preta saiu do detalhamento de folha.

        "Outros" era um numero grande sem nome que precisava de uma legenda pra
        se explicar. Com o detalhamento CONTIDO — total em cima, cinco rubricas
        nomeadas embaixo — nao ha mais o que agregar: o que nao esta nas cinco
        esta no total, e o total esta na tela.

        A legenda cai junto (era o unico consumer). O helper
        `etl.metrics.legenda_outros` segue vivo e com teste, so nao e mais
        renderizado — mesmo precedente do `legenda_abatimentos` no PDF.
        """
        assert '_R["outros_top"]' not in APP_SRC
        assert '_R["outros_resto"]' not in APP_SRC
        assert "legenda_outros(" not in APP_SRC
        assert 'st.expander("O que há em' not in APP_SRC

    def test_chart_e_uma_barra_do_total(self):
        """A pilha morreu, o chart e o TOTAL.

        A pilha empilhava as linhas da tabela — inclusive a faixa "Outros", que
        saiu. Deixar a faixa no grafico manteria vivo no visual exatamente o
        agrupamento que saiu da tabela, e chart e tabela deixariam de contar a
        mesma historia.

        A altura da barra continua sendo o CUSTO TOTAL — muda so a
        decomposicao em cores, que sai. Streamlit NAO ganha YoY (isso e do
        PDF): aqui a janela vai a 48 meses e o leitor e operador.
        """
        assert 'for i, linha in enumerate(_R["tabela"].index)' not in APP_SRC
        assert '_R["custo"]' in APP_SRC
        # o chart de DEGRADACAO (empresa sem carga do mart de rubricas) segue
        # empilhando por tipo_process — e a rede que impede a secao ficar sem
        # visual nenhum, e la nao ha tabela pra contradizer.
        assert "name=rotulo_tipo_process(tp)" in APP_SRC
        # um nome so pro mesmo universo: o titulo do chart sai da MESMA
        # constante que rotula a linha da tabela, nunca de um literal proprio
        # (literal duplicado diverge em silencio na proxima renomeacao).
        assert '**Custo Total de Folha**' not in APP_SRC
        assert 'f"**{LINHA_FOLHA_ENCARGOS}**"' in APP_SRC

    def test_o_detalhe_e_contido_e_DECLARA_isso(self):
        """A regiao de baixo e CONTIDA no total, nao aditiva.

        Sem o subtitulo o leitor soma "Folha + Encargos" com as cinco rubricas
        e conta o mesmo dinheiro duas vezes. Nao ha checagem aritmetica possivel
        aqui (a contencao nao e uma identidade) — a protecao e o texto, e ele
        vem do metrics pra nao divergir do PDF.
        """
        assert "folha_destaques(" in APP_SRC
        assert "TITULO_DESTAQUES_FOLHA" in APP_SRC
        assert "SUBTITULO_DESTAQUES_FOLHA" in APP_SRC
        # os textos moram no metrics; o app nao pode ter literal proprio
        assert "Principais custos da folha" not in APP_SRC
        assert "já incluídos em" not in APP_SRC

    def test_a_tabela_de_cima_e_so_o_total_e_o_percentual(self):
        # a decomposicao em 8 linhas saiu: em cima ficam DUAS linhas. As
        # rubricas que sobrevivem vao pra tabela de baixo, ja contidas.
        assert "RUBRICAS_DESTAQUE_FOLHA" not in APP_SRC   # o app nao filtra
        assert "folha_destaques(" in APP_SRC              # ele consome


def test_streamlit_hora_extra_exibe_so_o_valor():
    # A qtd de horas SAI da celula — a tabela e de CUSTO e a coluna mostra
    # dinheiro. Contrato de PARIDADE: o PDF de Detalhamento de Folha faz o
    # mesmo (TestModeloDetalhamentoFolha).
    assert '"Hora Extra" in _fmt.index' not in APP_SRC
    assert 'h · ' not in APP_SRC
    # a serie qtd_he continua VIVA no helper — o que saiu foi so o render.
    assert 'qtd_he' not in APP_SRC


def test_streamlit_seletor_de_modelos_vale_pros_quatro_pontos():
    # Um widget, um kwarg. A escolha desce pelo funil unico do pdf_generator
    # (gerar_pdf_empresa / gerar_todos_pdfs -> _build_html_for_empresa ->
    # _render_html), entao NENHUM botao carrega logica propria.
    assert 'key="pdf_modelos"' in APP_SRC
    # os call sites do app passam o MESMO valor (o outro ponto e a CLI,
    # coberta em TestRegistroDeModelos)
    assert APP_SRC.count("modelos=modelos_sel") == 4   # 1 individual + 2 selecao + 1 lote


def test_streamlit_opcoes_de_modelo_vem_do_REGISTRO():
    # o app NAO lista modelos: le pdf_generator.MODELOS. Modelo novo aparece
    # no seletor sem tocar aqui — foi pra isso que o registro existe.
    assert "from pdf_generator import MODELOS" in APP_SRC
    assert '"fiscal"' not in APP_SRC.split("Modelos do PDF")[0][-2000:]


def test_streamlit_selecao_vazia_desabilita_os_tres_botoes():
    # selecao vazia geraria PDF sem pagina nenhuma; o pdf_generator levanta
    # ValueError, entao o botao para ANTES, com o motivo na tela.
    assert "sem_modelo = not modelos_sel" in APP_SRC
    assert APP_SRC.count("or sem_modelo") == 3
    assert "Selecione ao menos um modelo" in APP_SRC


def test_streamlit_legenda_do_travessao_vem_do_metrics():
    # a linha de % exibe "—" quando nao ha faturamento que sirva de base. A
    # legenda e CONSTANTE no metrics e os dois consumers usam a MESMA — texto
    # duplicado divergiria em silencio. So renderiza quando ha travessao.
    assert "legenda_travessao" in APP_SRC
    assert '"—" = mês sem faturamento' not in APP_SRC   # sem literal inline


def test_streamlit_nao_corta_rubrica_por_conta_propria():
    # o corte de exibicao mora SEMPRE no metrics (fonte unica com o PDF) —
    # hoje o unico corte e a selecao dos destaques.
    assert ".head(10)" not in APP_SRC
    assert "outros_top" not in APP_SRC


def test_streamlit_indice_da_folha_cabe_o_rotulo_do_13o():
    # O rotulo qualificado tem 37 chars e o width="medium" TRUNCAVA na tela
    # ("13º Salário (integral, no fechame"). Rotulo cortado e informacao
    # perdida. Alargar, nunca cortar.
    assert 'st.column_config.Column(width="large")' in APP_SRC
    assert 'st.column_config.Column(width="medium")' not in APP_SRC


def test_streamlit_escapa_cifrao_na_legenda_multi_valor():
    """`$` PAREADO vira matematica LaTeX no markdown do Streamlit.

    Uma legenda com um "R$" por valor faz o Streamlit parear os cifroes e
    renderizar trechos como formula, SEM os cifroes. Captions de um "R$" so
    nao sofrem (cifrao impar nao fecha par).

    A legenda multi-valor do bloco de folha e a de abatimentos, que lista
    tres naturezas com valor. A licao e do RENDER: o mesmo texto vai pro
    PDF, que e HTML e nao tem esse problema.
    """
    assert "legenda_abatimentos(" in APP_SRC
    assert 'st.caption(_leg_abat.replace("$", "\\\\$"))' in APP_SRC, (
        "a legenda precisa escapar o cifrao antes do st.caption")


def test_streamlit_busca_de_empresa_e_campo_separado_com_dropdown_mudo():
    # DESENHO VIGENTE (hibrido): campo de busca SEPARADO filtrando em Python +
    # dropdown que so ESCOLHE. Duas coisas ficam banidas:
    #  - CSS que bloqueia digitacao, porque e divida acoplada a VERSAO do
    #    Streamlit. Quem desliga a digitacao e o `filter_mode=None`, nativo.
    #  - o `filter_mode="contains"`, porque com a busca nativa clicar na caixa
    #    entra em modo de edicao do valor atual e obriga a apagar o rotulo
    #    inteiro antes de procurar outra empresa.
    assert "pointer-events" not in APP_CODE       # CSS acoplado a versao
    assert "data-baseweb" not in APP_CODE
    assert "filter_mode=None" in APP_SRC         # dropdown mudo, via nativo
    assert 'filter_mode="contains"' not in APP_CODE
    assert 'key="busca_emp"' in APP_SRC          # campo de busca separado
    assert "_match_busca" in APP_SRC             # predicado nome OU CNPJ


def test_streamlit_selectbox_de_empresa_tem_identidade_por_ids():
    # A identidade da opcao e a TUPLA DE IDS, nunca o indice posicional nem o
    # rotulo.
    assert "emp_baseline" not in APP_CODE
    assert "init_idx" not in APP_CODE
    assert "range(len(labels_dropdown))" not in APP_CODE
    assert "_ids_vis = [it[3] for it in itens_visiveis]" in APP_CODE


def test_streamlit_rotulo_de_empresa_nunca_se_repete():
    # No navegador o valor do selectbox trafega pelo ROTULO: com dois iguais,
    # escolher o primeiro abria o ultimo (matriz -> filial homonima com a
    # consolidacao desligada). Homonimas ganham o sufixo de filial e a rede de
    # seguranca garante unicidade. Prova em navegador:
    # tests/test_app_troca_empresa_chrome.py.
    assert "_homonimas[nome.lower()] > 1" in APP_CODE
    assert 'f" — Filial {fnum}"' in APP_CODE
    assert "rotulos_unicos([(it[3], it[2]) for it in itens])" in APP_CODE


def test_streamlit_estado_da_empresa_nao_mora_na_chave_do_widget():
    # ARMADILHA: o estado do selectbox trafega do browser como INDICE, e o
    # Streamlit resolve esse indice contra a lista NOVA **antes** do script
    # rodar. Com a lista mudando de tamanho (consolidacao ligada/desligada), a
    # chave do widget ja chega apontando pra outra empresa e nao ha o que
    # "pinar" depois.
    #
    # Por isso o estado vive em `emp_atual`, chave NOSSA, e o widget recebe
    # `index=` explicito. Passar `key=` aqui reintroduziria a corrupcao, entao
    # o guard bane a chave no widget de empresa.
    assert 'st.session_state["emp_atual"] = emp_ids' in APP_CODE
    assert "index=_ids_vis.index(_sel)" in APP_CODE
    assert 'key="emp_sel"' not in APP_CODE
    # `key="emp_box"` e do CONTAINER (ancora do CSS de cursor), nao do widget:
    # container nao carrega estado de selecao.
    assert 'st.container(key="emp_box")' in APP_CODE


def test_streamlit_busca_que_casa_auto_seleciona_o_primeiro():
    # Digitar uma busca que CASA ja e a intencao de trocar. A decisao
    # acontece ANTES do widget, e o `index=` a aplica no MESMO rerun - senao o
    # operador ve a busca "nao fazer nada".
    assert "_sel = itens_visiveis[0][3]" in APP_CODE
    # `busca and` no guard: sem ele, LIMPAR a busca cairia no mesmo ramo e
    # jogaria o operador pra primeira empresa da carteira.
    assert "elif busca and _sel is not None" in APP_CODE


def test_streamlit_filtro_sem_resultado_nunca_derruba_a_empresa_aberta():
    # O caso perigoso: options VAZIAS fazem o Streamlit descartar o valor e
    # reabrir na primeira da carteira. Avisa e cai na carteira cheia, que ja
    # contem a empresa aberta - por isso a reinsercao-no-fim MORREU aqui.
    assert "Nenhuma empresa encontrada com o filtro atual." in APP_SRC
    assert "itens_visiveis = itens" in APP_CODE
    assert "itens_visiveis + [next(" not in APP_CODE      # reinsercao morta


def test_streamlit_busca_por_cnpj_vive_no_predicado_nao_no_rotulo():
    # O predicado normaliza os DOIS lados (only_digits), entao CNPJ pontuado
    # acha. Por isso o rotulo NAO carrega digitos: `label_empresa` e chamada
    # sem o argumento de CNPJ nos 3 pontos de montagem.
    assert "only_digits(item[5])" in APP_SRC
    assert "label_empresa" in APP_SRC
    assert 'f"{nome} {badges}"' not in APP_CODE   # rotulo montado a mao
    assert "label_empresa(nome, badges, cnpj)" not in APP_CODE


def test_streamlit_css_do_sidebar_e_cursor_only_e_escopado():
    # CONTRATO: CSS de CURSOR e permitido; CSS que muda COMPORTAMENTO nao.
    # `pointer-events: none` bloqueava a digitacao pelo markup do BaseWeb,
    # obrigava o clique a atravessar o input, e quebrava calado quando a
    # versao do Streamlit mudava. Quem bloqueia a digitacao e o `filter_mode`,
    # que e API.
    #
    # `data-baseweb` segue banido junto: era o seletor acoplado ao markup. O
    # CSS mira a classe `st-key-` do container, que e contrato do Streamlit e
    # nao muda com o tema nem com o BaseWeb.
    assert "pointer-events" not in APP_CODE
    assert "data-baseweb" not in APP_CODE
    assert ".st-key-emp_box input" in APP_SRC
    assert "cursor: pointer" in APP_SRC
    assert 'st.container(key="emp_box")' in APP_SRC


def test_streamlit_cursor_css_nao_vaza_pros_selects_que_aceitam_digitacao():
    # SO o selectbox de empresa e readonly (filter_mode=None). Os outros 4 do
    # sidebar ficam no default e ACEITAM digitacao - medido no browser -, entao
    # neles o I-beam esta certo e o caret tem de aparecer. Seletor generico de
    # sidebar mentiria em 4 widgets pra consertar 1.
    assert 'caret-color: transparent' in APP_SRC
    for generico in ('[data-testid="stSidebar"] [data-testid="stSelectbox"] input',
                     '[data-testid="stSidebar"] input'):
        assert generico not in APP_CODE


def test_streamlit_tabela_impostos_traz_o_fallback_de_irpj_csll():
    # IRPJ/CSLL pela CONTABILIDADE quando a empresa apura e paga mas nao
    # emite guia pelo modulo do Dominio: o navegador mostra as mesmas linhas
    # que o PDF.
    #
    # A `linha_extra` sai da MESMA serie que o series_mensais usou pro
    # numerador do card - nao ha segunda derivacao, entao card e tabela sao o
    # mesmo numero por construcao. Espelho literal de `pdf_generator._extra`.
    assert 'linha_extra=S.get("irpj_contabil") or None' in APP_CODE
    assert "bal_ir_df=bal_ir_df" in APP_CODE
    # A carga vem do helper compartilhado, nunca de SQL inline: o alargamento
    # de +3 meses e o que se esquece ao copiar, e esquece-lo subconta calado.
    assert "params_passivo_ir(emp_ids, de_str, ate_str)" in APP_CODE
    assert "SQL_PASSIVO_IRPJ_CSLL" in APP_CODE


def test_streamlit_yoy_carrega_o_fallback_nos_dois_anos():
    # Carregar so num lado faz o delta comparar numerador COM fallback contra
    # numerador SEM - degrau artificial, nao variacao do negocio.
    assert "params_passivo_ir(emp_ids, de_prev, ate_prev)" in APP_CODE
    assert "bal_ir_df=bal_ir_prev" in APP_CODE


def test_streamlit_limiar_do_alerta_de_impostos_e_unico():
    # O card e a tabela usam o MESMO limiar: um 25.0 HARDCODED na tabela
    # faria a MESMA tela mostrar ⚠️ no card e verde na linha. O valor vive
    # no metrics e os dois derivam dele.
    assert "ALERTA_PCT_IMPOSTOS" in APP_CODE
    assert "pct_imp_med > ALERTA_PCT_IMPOSTOS" in APP_CODE
    assert "25.0" not in APP_CODE


def test_streamlit_pdf_individual_persiste_em_session_state():
    # O download_button nascendo DENTRO do `if pdf_btn:` existiria so no
    # rerun do clique e qualquer outra interacao sumiria com o PDF ja gerado.
    assert 'st.session_state["pdfi_bytes"] = pdf_bytes' in APP_CODE
    assert 'if st.session_state.get("pdfi_bytes") is not None:' in APP_CODE
    # o botao tem de estar FORA do `if pdf_btn:` - e o que o faz sobreviver
    _i_btn = APP_CODE.index("if pdf_btn:")
    _i_guard = APP_CODE.index('if st.session_state.get("pdfi_bytes") is not None:')
    assert _i_guard > _i_btn


def test_streamlit_pdf_individual_invalida_quando_dessincroniza_da_tela():
    # REGRA: o link existe SE E SOMENTE SE descreve a tela atual. Por isso o
    # fingerprint tem os QUATRO insumos do PDF - trocar de empresa, de periodo
    # ou de modelos dessincroniza igual. A comparacao usa o
    # `_fp_desatualizado` compartilhado (mesmo mecanismo do lote/selecao, ver
    # test_fp_desatualizado_e_pura_e_reusavel).
    assert "_pdfi_fp = (tuple(emp_ids), de_str, ate_str, tuple(modelos_sel))" in APP_CODE
    assert 'if _fp_desatualizado(st.session_state, "pdfi_fp", _pdfi_fp):' in APP_CODE
    assert 'st.session_state.pop(_k, None)' in APP_CODE


# ── O ZIP do lote e o PDF/ZIP da selecao invalidam na troca de periodo ──────
# Os testes abaixo EXECUTAM trechos reais de app.py (extraidos por texto e
# rodados via exec, ja que o modulo nao e import-safe), nao so grep: quebram
# por VALOR (estado montado, janela trocada, artefato velho ainda presente),
# nao por ausencia de simbolo.

class _Mudo:
    """Absorve qualquer atributo/chamada sem fazer nada -- estagio do `st`
    do Streamlit nos testes que executam um TRECHO real de app.py (aqui so
    interessa a mutacao do session_state, nunca o que seria desenhado)."""

    def __getattr__(self, _nome):
        return self

    def __call__(self, *a, **k):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _StFake:
    def __init__(self, session_state):
        self.session_state = session_state
        self.sidebar = _Mudo()

    def __getattr__(self, _nome):
        return _Mudo()


def _exec_definicao(nome):
    """Extrai a definicao top-level de UMA funcao PURA do app.py (sem import
    de terceiro por dentro) e a executa via exec, pra testar comportamento
    de verdade sem importar o modulo inteiro."""
    ini = APP_SRC.index(f"def {nome}(")
    fim = APP_SRC.index("\n\n\n", ini)
    ns = {}
    exec(compile(APP_SRC[ini:fim], f"<{nome}>", "exec"), ns)
    return ns[nome]


def test_fp_desatualizado_e_pura_e_reusavel():
    # Um mecanismo so, chamado pelos tres consumidores (PDF individual,
    # selecao, lote), nao um fingerprint por consumidor.
    f = _exec_definicao("_fp_desatualizado")
    assert f({"x_fp": (1, "2025-01")}, "x_fp", (1, "2025-01")) is False
    assert f({"x_fp": (1, "2025-01")}, "x_fp", (1, "2025-02")) is True
    # Nunca gravado (chave ausente) TAMBEM conta como desatualizado -- e
    # responsabilidade de quem chama nunca perguntar sem ter certeza que o
    # artefato existe (os tres call sites so chamam depois de checar
    # bytes/zip is not None).
    assert f({}, "x_fp", (1, "2025-01")) is True


def _fp_desatualizado_ref(session_state, fp_key, fp_atual):
    """Implementacao de REFERENCIA (NAO extraida do app.py) do mesmo
    contrato de `_fp_desatualizado`, usada so pra alimentar o exec dos
    trechos de lote/selpdf: o teste prova o comportamento do trecho, nao a
    existencia do helper."""
    return session_state.get(fp_key) != fp_atual


def _trecho_lote_done():
    ini = APP_SRC.index('if st.session_state.get("lote_stage") == "done":')
    return APP_SRC[ini:]  # e o ULTIMO bloco do arquivo -- vai ate o fim


def _roda_lote_done(session_state, *, de_str, ate_str, filtro_lote, consolidar,
                    modelos_sel, fp_helper):
    ns = {
        "st": _StFake(session_state),
        "_fp_desatualizado": fp_helper,
        "_mostra_aviso_kaleido": lambda *a, **k: None,
        "de_str": de_str, "ate_str": ate_str,
        "filtro_lote": filtro_lote, "consolidar": consolidar,
        "modelos_sel": modelos_sel,
    }
    exec(compile(_trecho_lote_done(), "<lote_done>", "exec"), ns)


def test_lote_zip_e_descartado_quando_o_periodo_muda():
    # Monta o estado como se o ZIP tivesse sido gerado pra 2023, TROCA a
    # janela ao vivo pra 2024 e EXECUTA o trecho real do app.py: `lote_zip`
    # tem que sumir do session_state.
    fp_helper = _fp_desatualizado_ref
    session_state = {
        "lote_stage": "done",
        "lote_zip": b"zip-de-2023-velho",
        "lote_zip_name": "Dashboards_2023-01_2023-12.zip",
        "lote_out_fp": ("2023-01", "2023-12", "fat_or_bal", False, ("fiscal",)),
        "lote_sucessos": 10,
        "lote_erros": [],
    }
    _roda_lote_done(session_state, de_str="2024-01", ate_str="2024-12",
                    filtro_lote="fat_or_bal", consolidar=False,
                    modelos_sel=["fiscal"], fp_helper=fp_helper)
    assert "lote_zip" not in session_state
    assert session_state.get("lote_stage") == "idle"


def test_lote_zip_sobrevive_quando_a_tela_nao_mudou():
    # Regressao inversa: mesma janela/filtro/consolidar/modelos do que
    # gerou o ZIP -- ele TEM que continuar disponivel (senao a regra vira
    # invalidacao cega, que apaga resultado bom).
    fp_helper = _fp_desatualizado_ref
    session_state = {
        "lote_stage": "done",
        "lote_zip": b"zip-de-2023",
        "lote_zip_name": "Dashboards_2023-01_2023-12.zip",
        "lote_out_fp": ("2023-01", "2023-12", "fat_or_bal", False, ("fiscal",)),
        "lote_sucessos": 10,
        "lote_erros": [],
    }
    _roda_lote_done(session_state, de_str="2023-01", ate_str="2023-12",
                    filtro_lote="fat_or_bal", consolidar=False,
                    modelos_sel=["fiscal"], fp_helper=fp_helper)
    assert session_state.get("lote_zip") == b"zip-de-2023"
    assert session_state.get("lote_stage") == "done"


def _trecho_selpdf_done():
    ini = APP_SRC.index('if st.session_state.get("selpdf_stage") == "done":')
    fim = APP_SRC.index(
        '\n        else:\n            st.info("Nenhuma empresa cadastrada.")', ini)
    return textwrap.dedent(APP_SRC[ini:fim])


def _roda_selpdf_done(session_state, *, escolhidas, de_str, ate_str, modelos_sel,
                      fp_helper):
    ns = {
        "st": _StFake(session_state),
        "_fp_desatualizado": fp_helper,
        "_mostra_aviso_kaleido": lambda *a, **k: None,
        "escolhidas": escolhidas, "de_str": de_str, "ate_str": ate_str,
        "modelos_sel": modelos_sel,
    }
    exec(compile(_trecho_selpdf_done(), "<selpdf_done>", "exec"), ns)


def test_selpdf_zip_e_descartado_quando_o_periodo_muda():
    # Mesmo caso do lote, pro fluxo de PDF por selecao de empresas.
    fp_helper = _fp_desatualizado_ref
    session_state = {
        "selpdf_stage": "done",
        "selpdf_zip": b"zip-selecao-2023-velho",
        "selpdf_zip_name": "Dashboards_sel_2023-01_2023-12.zip",
        "selpdf_out_fp": (((1, 2),), "2023-01", "2023-12", ("fiscal",)),
        "selpdf_sucessos": 2,
        "selpdf_erros": [],
    }
    _roda_selpdf_done(session_state, escolhidas=[(1, 2)],
                      de_str="2024-01", ate_str="2024-12",
                      modelos_sel=["fiscal"], fp_helper=fp_helper)
    assert "selpdf_zip" not in session_state
    assert session_state.get("selpdf_stage") == "idle"


def test_selpdf_zip_sobrevive_quando_a_tela_nao_mudou():
    fp_helper = _fp_desatualizado_ref
    session_state = {
        "selpdf_stage": "done",
        "selpdf_zip": b"zip-selecao-2023",
        "selpdf_zip_name": "Dashboards_sel_2023-01_2023-12.zip",
        "selpdf_out_fp": (((1, 2),), "2023-01", "2023-12", ("fiscal",)),
        "selpdf_sucessos": 2,
        "selpdf_erros": [],
    }
    _roda_selpdf_done(session_state, escolhidas=[(1, 2)],
                      de_str="2023-01", ate_str="2023-12",
                      modelos_sel=["fiscal"], fp_helper=fp_helper)
    assert session_state.get("selpdf_zip") == b"zip-selecao-2023"
    assert session_state.get("selpdf_stage") == "done"


def test_streamlit_lote_e_selpdf_gravam_o_fingerprint_na_geracao():
    # Guarda de FIACAO (source-level): a existencia do guard nao basta se
    # ninguem gravar o fingerprint no momento em que o artefato nasce --
    # os testes de execucao acima so provam o COMPARADOR, nao a FIACAO.
    assert APP_CODE.count('st.session_state["lote_out_fp"] = _lote_out_fp') == 2
    assert APP_CODE.count('st.session_state["selpdf_out_fp"] = _selpdf_out_fp') == 3


def test_streamlit_nao_forca_largura_de_indice_fora_da_folha():
    # A folha precisa de `large` porque tinha um `width="medium"` EXPLICITO,
    # e width explicito CAPA a largura. As outras tabelas nao tem width
    # nenhum, entao o Streamlit auto-dimensiona ao conteudo. Forcar "large"
    # na tabela de impostos alargaria a coluna e comeria UM MES de tela pra
    # consertar um problema inexistente.
    #
    # Este guard trava a volta do cargo-cult: `large` fica SO onde ha causa.
    assert APP_CODE.count('st.column_config.Column(width="large")') == 1
    assert 'st.column_config.Column(width="medium")' not in APP_CODE
    assert "height=(len(piv_bal)" not in APP_CODE


def test_streamlit_nao_usa_o_kwarg_de_largura_deprecado():
    # O Streamlit anuncia remocao do kwarg antigo de largura; todos os usos
    # viraram `width=`. A traducao e 1:1 mesmo onde e REDUNDANTE (dataframe e
    # plotly_chart ja tem 'stretch' como default): nao depende de um default
    # continuar o que e hoje. Nos BOTOES ela nao e redundante - o default
    # deles e 'content', entao apagar o kwarg encolheria todos.
    _kwarg_velho = "use_container" + "_width"
    assert _kwarg_velho not in APP_CODE
    _pdf = (Path(__file__).resolve().parent.parent / "pdf_generator.py"
            ).read_text(encoding="utf-8")
    assert _kwarg_velho not in _pdf


# ── Blindagem do kaleido: aviso de modo + ZIP do lote abortado ───────────────
# Source-level (app.py nao e import-safe). O TEXTO dos avisos vive em
# pdf_generator.aviso_kaleido e e testado la; aqui trava so o FIO.

def _ocorrencias(token, src=APP_CODE):
    pos, i = [], src.find(token)
    while i != -1:
        pos.append(i)
        i = src.find(token, i + 1)
    return pos


def _bloco_abort(p):
    ini = APP_CODE.find("except LoteAbortadoKaleido as e:", p)
    fim = APP_CODE.find("except Exception as e:", ini)
    return APP_CODE[ini:fim]


def test_lote_abortado_capturado_antes_do_generico_nos_dois_fluxos():
    # Sem o except dedicado o lote abortado cai no `except Exception`: mensagem
    # certa, ZIP parcial PERDIDO. E o ImportError vem ANTES: o nome
    # LoteAbortadoKaleido e importado dentro do try, e avaliar a clausula dele
    # com o import falho levantaria NameError em cima do erro real.
    chamadas = _ocorrencias("gerar_todos_pdfs(")
    assert len(chamadas) == 2
    for p in chamadas:
        i_imp = APP_CODE.find("except ImportError as e:", p)
        i_lote = APP_CODE.find("except LoteAbortadoKaleido as e:", p)
        i_gen = APP_CODE.find("except Exception as e:", p)
        assert -1 < i_imp < i_lote < i_gen


def test_abort_limpa_a_barra_de_progresso_antes_de_pintar():
    # Barra congelada no meio com erro embaixo le como travamento -- a percepcao
    # que a blindagem existe pra eliminar.
    for p in _ocorrencias("gerar_todos_pdfs("):
        bloco = _bloco_abort(p)
        assert "progress.empty()" in bloco
        assert bloco.find("progress.empty()") < bloco.find("st.rerun()")


def test_pendentes_do_lote_abortado_nao_viram_erros():
    # 400 unidades abortadas na 20a: somar as pendentes em `erros` pintaria 380
    # linhas vermelhas e enterraria os erros reais.
    for p in _ocorrencias("gerar_todos_pdfs("):
        bloco = _bloco_abort(p)
        assert '_erros"] = e.erros' in bloco
        assert "e.erros +" not in bloco and "+ e.pendentes" not in bloco
    assert "lote abortado)" not in APP_CODE   # a contagem vive no aviso_kaleido


def test_pendentes_listadas_em_expander_fechado():
    assert APP_CODE.count('expander(f"Não gerados') == 2
    for p in _ocorrencias('expander(f"Não gerados'):
        assert "expanded=False" in APP_CODE[p:APP_CODE.find("\n", p)]


def test_estado_do_kaleido_gravado_na_geracao_e_zerado_junto():
    # Snapshot do momento da geracao, nunca relido da tela: o aviso descreve o
    # PDF que saiu, e sobrevive ao st.rerun().
    # Grava-se o ESTADO (modo + motivo); o texto e o detalhe saem dele no render.
    assert APP_CODE.count('_kaleido"] = kaleido_estado()') == 2
    assert '("pdfi_bytes", "pdfi_name", "pdfi_fp", "pdfi_kaleido")' in APP_CODE
    assert '"selpdf_kaleido", "selpdf_abort"' in APP_CODE
    assert 'st.session_state.pop("lote_abort", None)' in APP_CODE


def test_aviso_do_kaleido_escapa_cifrao_no_render():
    assert "def _mostra_aviso_kaleido(" in APP_CODE
    # O motivo vem de str(e) de TERCEIRO: crase vira codigo e $ vira LaTeX no
    # markdown do Streamlit. Neutraliza o markdown inteiro no render, nao so o
    # cifrao -- no aviso E no detalhe tecnico.
    assert r'_MD_ESPECIAIS = re.compile(r"([\\`*_{}\[\]<>#|~$])")' in APP_CODE
    assert "_escapa_markdown(texto)" in APP_CODE
    assert "_escapa_markdown(detalhe)" in APP_CODE
    assert 'texto.replace("$"' not in APP_CODE
    # individual + selecao (1 unidade) + selecao abortada + lote abortado
    assert len(_ocorrencias("_mostra_aviso_kaleido(")) >= 5


def test_motivo_tecnico_fica_em_expander_fechado_e_ninguem_manda_reiniciar():
    # Quem le a tela e contador, so com navegador: o motivo tecnico (str(e) do
    # kaleido) sai do corpo do aviso e fica em "Detalhes técnicos" FECHADO, e
    # nenhuma tela pede pra reiniciar o servico -- instrucao pra quem tem a
    # maquina vive no gen_pdf.py e no stderr.
    linhas = [ln for ln in APP_CODE.splitlines() if 'expander("Detalhes técnicos"' in ln]
    assert len(linhas) == 1 and "expanded=False" in linhas[0]
    assert "reinici" not in APP_CODE.lower()


def test_help_devolucoes_de_venda_nao_diz_mais_que_nao_e_subtraida():
    # O NFe do painel e LIQUIDO da devolucao de venda (mes da entrada). O help
    # antigo ("Nao e subtraida do Faturamento Total") nao pode voltar.
    assert "Não é subtraída do Faturamento Total" not in APP_CODE
    assert "Já abatidas do Faturamento" in APP_CODE
