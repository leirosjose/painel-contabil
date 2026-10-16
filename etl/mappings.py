# ---------------------------------------------------------------------------
# mappings.py — Constantes de display compartilhadas entre app.py e pdf_generator.py
# ---------------------------------------------------------------------------
# Fonte única para impostos, grupos contábeis e nomes de meses, evitando
# divergência entre app.py e pdf_generator.py.
# ---------------------------------------------------------------------------

import re

NOMES_MES = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun",
             "Jul", "Ago", "Set", "Out", "Nov", "Dez"]

# ── Recebimento de venda de imovel (3a origem do faturamento) ──────────────
# `raw.efimob_pagamentos.origem_pagamento` e char(1) NOT NULL, e a origem usa
# TRES valores:
#   B  recebimento
#   L  recebimento (outra origem de digitacao)
#   R  DEVOLUCAO/rescisao: valor_pago 0,00 e o dinheiro em valor_devolucao --
#      nao e receita e nao pode somar
# Somar `valor_pago` ja exclui o R por construcao (ele contribui zero), mas o
# filtro e EXPLICITO: o dia em que a origem puser valor no R, o silencio viraria
# receita fabricada.
ORIGENS_RECEBIMENTO_IMOBILIARIO = ("B", "L")
ORIGENS_IMOBILIARIO_CONHECIDAS = frozenset({"B", "L", "R"})

# ── Dash de folha por RUBRICA ───────────────────────────────────────────────
# O DSR nao e familia propria no classificador GLOBAL do Dominio: os reflexos
# se espalham por varias familias ("Salários", "Outros Proventos",
# "Comissão"...). Por isso o criterio da linha DSR e por NOME, e ele mora aqui,
# nomeado, em vez de ficar solto num regex inline no consumer.
#
# O regex casa os reflexos (REFLEXO EXTRAS DSR, REFLEXO ADIC. NOTURNO DSR,
# REFLEXO COMISSOES DSR) e NAO pega "FALTAS DSR" (desconto) nem
# "ESTORNO DIAS FALTAS DSR", que nao sao reflexo.
#
# CONSEQUENCIA DE ORDEM: como o reflexo de comissao vem classificado como
# "Comissão" pela origem, a linha DSR tem que ter PRECEDENCIA sobre a de
# Comissao no particionamento — senao o mesmo dinheiro conta 2x. Vide
# etl.metrics.folha_por_linha.
RE_REFLEXO_DSR = re.compile(r"REFLEXO.*DSR", re.IGNORECASE)

# Verba de RESCISAO que a ORIGEM classificou numa familia nao-rescisoria.
# O classificador global e bom mas nao e infalivel: quem cadastra a rubrica
# escolhe a familia, e nas verbas de acerto rescisorio ela erra com frequencia
# (ex.: "13 SALARIO INTEGRAL RESCISAO" na familia "13º Salário", "ESTOURO
# RESCISAO" em "Fechamento"). Sem este criterio a linha Rescisao fica
# SUBESTIMADA e "Outros" inflado no mesmo tanto: o codigo/familia crua nao
# basta quando quem configurou a rubrica pode ter errado.
#
# LIMITE DECLARADO do criterio por nome: a "MULTA ESTABILIDADE Art. 479/CLT"
# e CONCEITUALMENTE rescisoria — indenizacao por quebra de contrato por prazo
# determinado — mas o nome nao diz RESCISAO e ela FICA em Outros. Alargar o
# regex pra pegar multa/indenizacao traria falso-positivo (multa de transito
# descontada, multa de contrato de aluguel...). Preferimos subestimar de forma
# DECLARADA a inflar por heuristica frouxa.
#
# Os matches com RESCIS que nao sao rescisao sao descontos (D) ou
# informativos (I), que nao entram na visao de custo bruto (so valor_p).
RE_RESCISAO = re.compile(r"RESCIS[AÃ]O", re.IGNORECASE)

# Familias do classificador GLOBAL (FOCLASSIFICACOES_EVENTOS) que compoem cada
# linha do dash. So as linhas cujo criterio E por familia entram aqui: DSR sai
# por nome (acima) e 13o Salario sai por tipo_process == 52 (o corte por
# familia duplicaria — ela aparece em 4 tps distintos).
FAMILIAS_LINHA_FOLHA: dict[str, frozenset[str]] = {
    "Folha":   frozenset({"Salários", "Insalubridade", "Periculosidade",
                          "Adicional Noturno", "Gratificação"}),
    "Férias":  frozenset({"Férias Gozadas", "Abono Pecuniário",
                          "Férias gozadas em dobro"}),
    "Rescisão": frozenset({"13º Rescisão", "Férias Rescisão", "Aviso Prévio"}),
    "Hora Extra": frozenset({"Horas Extras"}),
    "Comissão":   frozenset({"Comissão"}),
}

# tipo_process que sao ANTECIPACAO/DUPLICATA do mesmo dinheiro e por isso NAO
# viram linha somavel — viram NOTA:
# - 41 = adiantamento salarial: pago no tp=41 e RECUPERADO 1:1 como desconto
#   dentro do tp=11 do MESMO mes, que ja contem o salario integral; somar os
#   dois tps duplica o adiantamento.
# O 51 NAO esta aqui — ver RE_ESTORNO_13O logo abaixo. A 1a parcela do 13o e
# desembolso REAL no mes em que sai, e quem neutraliza a duplicidade e o
# estorno que a origem ja lanca, nao a exclusao do processo inteiro.
# - 92 = RPA/autonomo: o tp=92 e o DETALHE (um registro por recibo emitido) e
#   o tp=11 e a CONSOLIDACAO MENSAL do mesmo dinheiro. As rubricas sao
#   IDENTICAS na origem (SERVICOS FRETE AUTONOMO, INSS TRANSPORTADOR AUTONOMO,
#   IRRF AUTONOMO) e os N recibos somam o registro unico do tp=11 ao centavo.
#   Exclui-se o DETALHE, NUNCA o consolidado — o dinheiro do autonomo segue
#   contado uma vez pelo tp=11, entao o rotulo "inclui autonomos (RPA)"
#   continua verdadeiro.
# - 60 = recibo de FERIAS — MESMA SIMETRIA dos acima: o recibo antecipa em
#   caixa o que o mensal reconhece por competencia, e o tp11 desconta o que ja
#   foi pago pelo evento de ADIANTAMENTO DE FERIAS (espelho LIQUIDO do INSS e
#   do IRRF de ferias). Somar os dois conta a ferias 2x. Sai o RECIBO e fica o
#   MENSAL — o custo de ferias passa a ser o da competencia do gozo, que e onde
#   a despesa pertence.
# O 52 NAO esta aqui: o 13o integral e custo REAL da empresa, entra na tabela
# como linha propria e no CUSTO TOTAL ("nao existem dois custos totais").
# Consumo via etl.metrics.folha_custo(); views de DETALHE por tp seguem
# mostrando todos os tipos.
TIPOS_PROCESS_ANTECIPACAO: frozenset[int] = frozenset({41, 60, 92})

# ── Estorno da 1a parcela do 13o ──────────────────────────────────────────
# O tp51 e custo no mes pago e o ESTORNO abate. Sem isso o 13o inteiro
# apareceria no mes do FECHAMENTO: quem paga a 1a parcela fora de novembro
# veria o mes do desembolso sem 13o nenhum e dezembro carregando o ano.
# Pareamento:
#   tp51 "13 SALARIO ADIANTADO"       <-> tp52 "ADIANTAMENTO 13 SALARIO"
#        "MEDIA VALOR 13o ADIANTADO"  <->      "ADIANTAMENTO 13o MEDIA VALOR"
#        "VANTAGENS 13o ADIANTADO"    <->      "ADIANTAMENTO 13o VANTAGENS"
#        "MEDIA HORAS 13o ADIANTADO"  <->      "ADIANTAMENTO 13o MEDIA HORAS"
#
# O padrao NAO exclui RESCISAO: "ADIANTAMENTO 13 SALARIO RESCISAO" (tp11) E o
# estorno — o que a origem lanca no ACERTO quando o empregado sai entre as
# parcelas. Excluir deixaria essa 1a parcela contada DUAS vezes.
#
# LIMITE DECLARADO: casa sobre nome NORMALIZADO (o "13º" da origem vira "13O"
# no NFKD, entao um padrao cobre as duas grafias) e SO sobre valor_d. Falso
# positivo existente e inofensivo: "TROCO DO ADIANTAMENTO 13o" (tp51) e
# PROVENTO, e o filtro de desconto ja o exclui.
#
# A identidade NAO e contabil, e regularidade forte: pode sobrar residuo
# pequeno entre 1a parcela e estorno.
RE_ESTORNO_13O = re.compile(r"ADIANTAMENTO\s+13")

# ── Descontos-ESPELHO: o que o provento diz que foi pago e nao foi ─────────
# Os dois criterios abaixo casam sobre o nome NORMALIZADO (sem acento, caixa
# alta — `etl.utils.normaliza_rubrica`) e SO sobre `valor_d`. A polaridade e
# load-bearing: "AFAST" aparece tanto em PROVENTO quanto em DESCONTO, e
# aplicar o padrao sem filtrar o lado apagaria salario legitimo.
#
# ABATE-SE O DESCONTO, nunca se exclui o provento. Duas razoes: (a) onde o
# provento e MAIOR que o desconto a sobra sao os 15 primeiros dias de
# afastamento, que sao custo legal da empresa (Lei 8.213 art.60 par.3) e TEM
# de ficar; (b) o gemeo do "DESCONTO HORAS AFASTADAS" nao e so um evento —
# inclui "HORAS APOSENT. INVALIDEZ", que nao casa 'AFAST'. Abatendo o
# desconto, nenhuma das duas exige identificar o provento.
#
# Afastamento acima de 15 dias e bancado pelo INSS; a origem lanca o provento
# ("DIAS AFAST.INSS") e anula com o desconto ("DESCONTO DIAS AFASTADAS") no
# mesmo valor. O afastamento ate 15 dias NAO tem espelho e por isso continua
# no custo — o dado sabe a diferenca sozinho, sem regra de dia.
# LIMITE DECLARADO: pega tambem os descontos de afastamento que a origem poe
# na familia "13o Salario" — sao espelho igual, so classificados noutro lugar.
# Falsos positivos no lado PROVENTO existem e sao inofensivos porque o filtro
# e `valor_d > 0`: "QUINQUENIO AFASTAMENTO" (adicional por tempo de servico) e
# "AFASTAMENTO CARCERE".
RE_AFASTAMENTO_ESPELHO = re.compile(r"AFAST")

# Falta e dia nao trabalhado: o provento vem cheio e a origem desconta.
# O criterio e a FAMILIA do dicionario global — mas ela sozinha pega so
# parte: boa fatia mora na familia generica "Descontos", sobretudo
# "FALTAS DSR", que e o reflexo do DSR perdido pela falta. Por isso familia
# UNIAO nome.
# LIMITE DECLARADO: a familia pega "SUSPENSAO" e "DIAS SUSPENSOS", que sao
# falta pela natureza e estao dentro de "Falta Integral" — a acentuacao de
# SUSPENSAO seria perdida por um regex ASCII, e por isso o nome so e
# consultado sobre a forma normalizada. A familia 48 "Suspensao", SEPARADA,
# fica de FORA: e outra decisao, nao esta em 26/27. Existe provento que casa
# 'FALTA' (estorno, reembolso, "FALTAS INDEVIDAS"), mas o filtro
# `valor_d > 0` ja o exclui. Falso positivo real: "AULAS FALTA", que e
# desconto de salario de professor.
FAMILIAS_FALTA: frozenset[str] = frozenset({"Falta Integral", "Falta Parcial"})
RE_FALTA_ESPELHO = re.compile(r"FALTA")

# FOBASES.tipo_process -> rotulo exibido. A origem NAO tem catalogo
# codigo->descricao (a coluna existe em varias tabelas, sempre como CHAVE).
# O rotulo vive na camada de aplicacao do Dominio — dai o dicionario estatico
# aqui. Reforco empirico: 51 concentra em NOVEMBRO e 52 em DEZEMBRO — 1a e 2a
# parcela do 13o; 70 concentra em dezembro/junho (PLR); 42 pinga em todos os
# meses com volume baixo (rescisao complementar).
# O 92 e o DETALHE (recibo de RPA) consolidado no 11 — por isso esta em
# TIPOS_PROCESS_ANTECIPACAO, mas segue com nome proprio nas views de
# DETALHE por tipo, que mostram todos.
# Cobre os 8 codigos que existem na FOBASES na historia COMPLETA da origem;
# codigo novo cai no fallback de rotulo_tipo_process (nunca quebra a tela).
TIPO_PROCESS_NOMES: dict[int, str] = {
    11: "Folha Mensal",
    41: "Adiantamento",
    42: "Rescisão Complementar",
    51: "13º Salário (1ª parcela)",
    52: "13º Salário (2ª parcela)",
    60: "Férias",
    70: "PLR",
    92: "Recibos de Autônomos (RPA)",
}

IMP_NOMES = {
    1: "ICMS", 2: "IPI", 3: "ISS", 4: "PIS", 5: "COFINS",
    6: "CSLL", 7: "IRPJ", 8: "DIFAL", 9: "Subst. Tributária",
    16: "IRRF", 17: "PIS (não cumulativo)", 18: "ISS Retido",
    19: "COFINS (não cumulativa)", 22: "PIS (Retido)", 23: "COFINS (Retido)",
    24: "CSLL (Retida)", 25: "Contrib. Retida na Fonte", 26: "INSS Retido",
    33: "IRPJ postergado", 38: "Contrib. Retidas - Órgãos Públicos",
    39: "IRRF Propaganda/PF", 44: "Simples Nacional", 55: "FECOP/FUNCEP",
    63: "IRRF aluguéis PF", 67: "PIS - ST", 68: "COFINS - ST",
    83: "RET", 88: "PMCMV", 103: "CPRB", 108: "SCP (PIS)", 109: "SCP (COFINS)",
    110: "SCP (CSLL)", 111: "SCP (IRPJ)", 145: "ICMS DIFAL Antecipado",
    146: "FECOP", 183: "IBS", 184: "CBS",
}

GRUPOS_BAL = {
    "Caixa":              "1010101",
    "Bancos":             "1010102",
    "Aplicações":         "1010103",
    "Bancos e Aplicações": ("1010102", "1010103"),
    "Ativo Circulante":   "101",
    "Estoque":            "10103",
    "Passivo Circulante": "201",
    "Exigível LP":        "202",
    "Patrimônio Líquido": "204",
    "Receitas":           ("301", "310"),
    "Despesas":           ("401", "402", "403", "404", "405", "406"),
    # Paridade com a planilha de referência: a linha "DESPESAS E CUSTOS" do
    # DashBoard Contabil EXCLUI o grupo 406 (IRPJ/CSLL, que so movimenta em
    # dezembro). "Despesas" (401-406) segue como base da lucratividade (que
    # INCLUI 406, igual ao GASTOS E TRIBUTOS da planilha).
    "Despesas e Custos":  ("401", "402", "403", "404", "405"),
    "Ativo Total":        "1",
}

# codi_imp de RETENÇÃO DE TERCEIROS em efsdoimp: guias que a empresa recolhe
# como TOMADORA/fonte pagadora (ISS/INSS/CSRF de serviços tomados, IRRF de
# aluguéis PF, folha). Não são carga tributária própria — ficam FORA do
# numerador Simples do "% Impostos" (build_impostos_destacados_mensais).
# 12/20 = variantes custom de "ISS Retido" no geimposto (mesma semântica). ICMS-ST
# (11) e ICMS antecipação (1) FICAM — imposto próprio da empresa.
# Real/Presumido não passa por aqui (numerador via balancete 3010201%).
CODI_IMP_RETENCAO_TERCEIROS: set[int] = {
    12, 16, 18, 20, 22, 23, 24, 25, 26, 38, 39, 63, 132, 137,
}

# SCP — Sociedade em Conta de Participacao (108 PIS, 109 COFINS, 110 CSLL,
# 111 IRPJ): a SCP e EQUIPARADA A PJ e apura resultado
# destacadamente; o DARF sai em nome do socio ostensivo apenas como VEICULO
# de recolhimento — a incidencia economica e da SCP, nao da empresa que
# aparece na guia. Logo o tributo nao e carga tributaria PROPRIA dela e sai
# da tabela de impostos exibida.
# Constante SEPARADA da de retencao DE PROPOSITO: mesmo destino (sair da
# tabela), motivos juridicos diferentes — juntar apagaria o porque.
CODI_IMP_SCP: set[int] = {108, 109, 110, 111}

# IRPJ (7, + 33 postergado) e CSLL (6) — a linha "IRPJ/CSLL do periodo" do card
# soma o que foi RECOLHIDO por guia, nao o provisionado. Os SCP
# (110/111) NAO entram: sao tributo de outra entidade e ja saem por
# `CODI_IMP_SCP`. As "Retidas" (24) tambem nao — imposto de terceiro.
CODI_IMP_IRPJ_CSLL: set[int] = {6, 7, 33}

# ── Fallback de IRPJ/CSLL pela CONTABILIDADE ─────────────────────────────────
#
# Empresa que apura e PAGA IRPJ/CSLL mas nao emite a guia pelo modulo do
# Dominio ficaria invisivel no card, que mede a guia recolhida. O fallback le a
# LIQUIDACAO no passivo — dinheiro que SAIU —, coerente com o criterio "o
# numero e o que pagou". Mesmo principio de `GUIA_IMPORTAVEL_DESTACADOS` —
# entra SO onde a fonte primaria e vazia, nunca soma por cima.
#
# **NAO usar a provisao (`clas 40106`): provisao e COMPETENCIA, nao
# desembolso.**
#
# **O `clas_cta` sozinho NAO SERVE.** O plano de contas nao e padronizado
# nesse nivel: o `201010207` e "CSLL" na maioria, mas e "COFINS Nao
# Cumulativa" e "IRRF S/ ALUGUEIS" em outras empresas — o codigo e por
# empresa. Por isso o criterio e clas INTERSECAO nome.
#
# `RE_PASSIVO_FORA` tira o que casa por nome sem ser o imposto corrente:
# "Parc Simplificado IRPJ" (PARCELAMENTO, divida antiga renegociada) e
# "Retencao de Contribuicao Social" (imposto de TERCEIRO).
#
# Nas empresas que tem passivo E guia o portao bloqueia o fallback.
CLAS_PASSIVO_IRPJ_CSLL: tuple[str, ...] = ("201010206", "201010207")
RE_PASSIVO_IRPJ = r"\bIRPJ\b|IMPOSTO\s+DE\s+RENDA"
RE_PASSIVO_CSLL = r"\bCSLL\b|CONTRIBUI\w*\s+SOCIAL"
RE_PASSIVO_FORA = r"\bPARC\w*\b|RETEN|JUROS"

# Impostos que PODEM ser importados da GUIA (efsdoimp) pro numerador
# Real/Presumido quando a empresa nao escritura a conta de deducao
# correspondente. LISTA BRANCA estreita, e a estreiteza e o ponto: a uniao
# ingenua "balancete U guia" DUPLICA. As duas armadilhas:
#   - IRPJ (7) e CSLL (6): o card ja soma a provisao do IR, e importar da
#     guia contaria o IR duas vezes. A ortogonalidade NAO e automatica:
#     depende desta exclusao.
#   - ICMS (1): a guia traz o SALDO A RECOLHER (liquido de credito de
#     entrada) e o balancete traz o DESTACADO na nota — grandezas
#     diferentes. Misturar trocaria a semantica do numerador.
# O ISS entra porque e o caso do problema: empresa que paga ISS em guia e
# nao usa conta de deducao ficaria com o card vazio (fonte 'sem_deducao').
# PIS (4) e COFINS (5) entram pelo mesmo motivo.
#
# Onde ha balancete o balancete MANDA (o portao); a guia so preenche o
# `so_guia`.
#
# RESSALVA DECLARADA (risco aceito): em empresa de regime nao-cumulativo a
# guia de PIS/COFINS e "a recolher" liquido de credito — a mesma base trocada
# que mantem o ICMS fora.
GUIA_IMPORTAVEL_DESTACADOS: set[int] = {3, 4, 5}

# O `codi_imp` do ISS. CONSTANTE porque a regra e PAREADA e mora em dois
# arquivos: `transform` SOMA este codigo no componente `valo_iss` e `metrics`
# SUBTRAI este codigo das linhas de guia da tabela de Impostos (senao o mesmo
# ISS apareceria duas vezes). Escrever `3` na mao dos dois lados desalinha o
# par em silencio quando GUIA_IMPORTAVEL_DESTACADOS muda, e o TOTAL passa a
# contar tributos DUAS VEZES.
CODI_IMP_ISS: int = 3

# Regex do nome da conta de deducao POR imposto importavel — usado pra saber
# se o balancete ja escritura aquele imposto no mes (se escritura, o
# balancete MANDA e o complemento nao entra: anti dupla contagem).
# O MESMO portao vale pros DOIS componentes do complemento: a guia e o ISS
# retido SOFRIDO (efservicospar). Eles sao DISJUNTOS — a identidade
# `balancete = guia(cod 3) + retido` fecha mensalmente —, entao somam entre si
# quando o balancete nao cobre e nenhum entra quando ele cobre.
# O portao e POR IMPOSTO: deducao de ISS escriturada nao bloqueia a guia de
# PIS, e vice-versa. Uma conta CONJUNTA ("(-) PIS/COFINS s/ Vendas") casa os
# dois regex e bloqueia os dois — correto, porque o valo_deb dela ja contem
# os dois tributos. `\bPIS\b` nao casa dentro de "COFINS" (a sigla nao
# contem PIS), entao os dois regex sao mutuamente seguros.
DEDUCAO_NOME_POR_IMPOSTO: dict[int, str] = {
    3: r"\bISS\b",
    4: r"\bPIS\b",
    5: r"\bCOFINS\b",
}

# Contas de dedução tributária no balancete.
# Regex pra filtrar nome_cta em contas com clas_cta começando com '3':
# match das contas "(- ) ICMS s/ Vendas", "(- ) PIS s/ Vendas", etc.
# `\b...\b` evita falso positivo em palavras tipo "retorno", "retencao",
# "retenida" (RET como palavra inteira; ICMS, PIS, COFINS, IPI, ISS são
# acrônimos já delimitados naturalmente).
RE_IMPOSTOS_DESTACADOS = r"\b(?:ICMS|PIS|COFINS|IPI|ISS|RET)\b"

# Prefixos de clas_cta em balancete pro "% Impostos".
# `PREFIXOS_DEDUCAO` — contas onde o destaque tributário é escriturado
# (NF emitida -> débito na conta de dedução de receita).
# `PREFIXOS_RECEITA` — Receita Bruta DRE clássica (denominador do
# "% Impostos"); cobre indústria (3010101), serviços (3010102) e
# outras op. (3010103).
PREFIXOS_DEDUCAO: tuple[str, ...] = ("3010201",)
PREFIXOS_RECEITA: tuple[str, ...] = ("3010101", "3010102", "3010103")

# `CLAS_DEDUCAO_EXATA` — contas de deducao que vivem FORA de
# `PREFIXOS_DEDUCAO`, na ARVORE ALTERNATIVA do plano de contas.
# Sao contas de deducao de ISS (retido na fonte) usadas por planos de contas
# diferentes: `30201010006`, sob `30201` DEDUCOES E ABATIMENTOS DAS VENDAS, e
# `301010602`, sob `3010106` (DEDUCOES DA RECEITA BRUTA).
#
# **CONTA EXATA, NUNCA PREFIXO.** O `clas_cta` nao e padronizado nesse nivel;
# alargar para o pai traria RECEITA para dentro de um conjunto que significa
# DEDUCAO (em algumas empresas o mesmo codigo e "LUCROS RECEBIDOS SCP",
# "RECEITA DE LOCACAO" ou "Outras Receitas"). O codigo e por empresa. A regra
# que fica: **conta EXATA como primeiro cinto, filtro de NOME como segundo**
# (`RE_IMPOSTOS_DESTACADOS` no numerador, `DEDUCAO_NOME_POR_IMPOSTO` no
# portao).
#
# **Constante SEPARADA, e nao alargamento de `PREFIXOS_DEDUCAO`, de
# proposito:** `PREFIXOS_DEDUCAO` tem TRES consumidores e um deles —
# `build_empresa_regime_periodo` — NAO filtra por nome ("tem conta de
# deducao, logo e Real/Presumido"). Incluir a `301010602` la seria aceitar
# receita como prova de deducao — a escolha e pela regra mais estreita que
# resolve.
CLAS_DEDUCAO_EXATA: tuple[str, ...] = ("30201010006", "301010602")

# CFOPs de DEVOLUÇÃO DE VENDA — entradas (efentradas.codi_nat) cuja natureza
# é uma devolução de uma saída anterior. É a lista de ABATIMENTO do
# faturamento NFe: o `valo_saidas` do mart é o bruto de receita MENOS `valo_devolucoes`
# (a soma destes CFOPs), abatido no MÊS DA ENTRADA (dent_ent), sem piso —
# como o Relatório de Faturamento do Domínio. UMA constante, um lugar só:
# `valo_devolucoes` (o que a tela mostra) e o abatimento leem a MESMA lista.
# Restrita a códigos com "Devolução de venda" no nome oficial (CONFAZ, Ajuste
# SINIEF 07/01) cuja venda par está em CFOPS_RECEITA_NFE. Inclui pares
# estaduais (1xxx) e interestaduais (2xxx).
#
# FORA DA LISTA, POR DECISÃO (nada disso abate):
#   - 1208/1209/2208/2209 — devolução de TRANSFERÊNCIA entre filiais. A
#     transferência nunca foi receita (5151/5152 estão fora de
#     CFOPS_RECEITA_NFE), então desfazê-la não pode reduzir faturamento.
#   - 1553/2553 — devolução de venda de ATIVO IMOBILIZADO (a venda 5551/6551
#     não é receita): NÃO abate.
#   - 1205-1207/2205-2207 — anulação de prestação de serviço de transporte:
#     NÃO abate.
#   - 1414/1415/2414/2415 — retorno de remessa; 1904/2904 retorno de remessa
#     pra venda fora; 1918/2918 e 1919/2919 consignação. Não são "devolução de
#     venda" no sentido SEFAZ.
# Se algum destes aparecer como hipótese de reconciliação, expandir
# EXPLICITAMENTE, com decisão registrada.
#
# Códigos com uso frequente marcados [USADO]; o resto fica na lista pra
# cobrir aparições futuras.
CFOPS_DEVOLUCAO_VENDA: set[int] = {
    # Grupo 12xx — devoluções de venda DENTRO do estado
    1201,  # [USADO] Devolução de venda de produção do estabelecimento
    1202,  # [USADO] Devolução de venda de mercadoria adquirida ou recebida (DOMINANTE)
    1203,  #         Devolução de venda de produção em ZFM/ALC — não usado
    1204,  # [USADO] Devolução de venda de mercadoria adquirida em ZFM/ALC
    1410,  # [USADO] Devolução de venda de produção em ST
    1411,  # [USADO] Devolução de venda de mercadoria adquirida em ST
    1660,  #         Devolução de venda de combustível p/ industrialização — não usado
    1661,  #         Devolução de venda de combustível p/ comercialização — não usado
    1662,  #         Devolução de venda de combustível p/ consumidor final — não usado

    # Grupo 22xx — devoluções de venda INTERESTADUAIS (espelho das 12xx)
    2201,  # [USADO] Devolução interestadual de venda de produção
    2202,  # [USADO] Devolução interestadual de venda de mercadoria adquirida
    2203,  #         Devolução interestadual de venda de produção em ZFM/ALC — não usado
    2204,  #         Devolução interestadual de venda de mercadoria em ZFM/ALC — não usado
    2410,  #         Devolução interestadual de venda em ST — não usado
    2411,  # [USADO] Devolução interestadual de venda de mercadoria em ST
    2660,  #         Devolução interestadual de venda de combustível — não usado
    2661,  #         idem — não usado
    2662,  #         idem — não usado
}


# ---------------------------------------------------------------------------
# CFOPs de RECEITA nas SAÍDAS (efsaidas.codi_nat) — o que compõe faturamento.
#
# A lista reconcilia com a receita bruta da DRE. Sem o filtro, transferência
# entre filiais é contada duas vezes: a matriz transfere pra filial e a filial
# revende, e as DUAS pernas viram faturamento. As famílias ambíguas estão em
# CFOPS_RECEITA_AMBIGUOS abaixo.
#
# A lista é por FAMÍLIA do padrão SEFAZ, não pelos códigos que uma empresa
# usa. Isso não é preciosismo: uma lista restrita zeraria o faturamento de
# transportadora (CT-e entra em efsaidas via codi_esp=37, e o CFOP dele é
# 535x/635x/5932/6932 — nada parecido com venda de mercadoria).
#
# LER A efnatureza PELA versao_nat=2. A tabela guarda DUAS versões do plano
# de CFOP e a versao 1 (legada) usa alguns códigos como TÍTULO DE GRUPO em
# CAPS — 5110 aparece lá como "VENDAS DE PRODUCAO DO ESTABELECIMENTO" e na
# versao 2 é "Venda de mercadoria adquirida ou recebida de terceiros,
# destinada à ZFM/ALC". Agregar a tabela com MAX(nome_nat) mistura as duas
# e disfarça operação real de cabeçalho (ex.: 5110/5120/6110/6120).
# Cabeçalhos DE VERDADE (CAPS também na versao 2): x.100, x.150, x.200,
# x.250, x.350, x.400, x.500 — mais 7110/7120, que só existem na versao 1.
CFOPS_RECEITA_NFE: frozenset[int] = frozenset({
    # ── Vendas dentro do estado (5.1xx) ────────────────────────────────────
    5101,  # [USADO] Venda de produção do estabelecimento (DOMINANTE)
    5102,  # [USADO] Venda de mercadoria adquirida ou recebida de terceiros
    5103,  #         Venda de produção efetuada fora do estabelecimento
    5104,  #         Venda de mercadoria de terceiros efetuada fora do estab.
    5105,  #         Venda de produção que não deva por ele transitar
    5106,  #         Venda de mercadoria de terceiros que não deva transitar
    5109,  #         Venda de produção destinada à ZFM/ALC
    5110,  #         Venda de mercadoria de terceiros destinada à ZFM/ALC
    5111,  #         Venda de produção remetida antes em consignação industrial
    5112,  #         Venda de mercadoria remetida antes em consignação industrial
    5113,  #         Venda de produção remetida antes em consignação mercantil
    5114,  #         Venda de mercadoria remetida antes em consignação mercantil
    5115,  # [USADO] Venda de mercadoria recebida antes em consignação mercantil
    5116,  #         Venda de produção originada de encomenda p/ entrega futura
    5117,  # [USADO] Venda de mercadoria originada de encomenda p/ entrega futura
    5118,  #         Venda de produção entregue por conta e ordem (venda à ordem)
    5119,  #         Venda de mercadoria entregue por conta e ordem (venda à ordem)
    5120,  # [USADO] Venda de mercadoria entregue pelo vendedor remetente, venda à ordem
    5122,  #         Venda de produção remetida p/ industrialização por conta e ordem
    5123,  # [USADO] Venda de mercadoria remetida p/ industrialização por conta e ordem
    5124,  #         Industrialização efetuada para outra empresa
    5125,  #         Industrialização p/ outra empresa sem trânsito pelo adquirente
    5129,  #         Venda de insumo importado/industrializado sob RECOF

    # ── Vendas interestaduais (6.1xx — espelho das 5.1xx) ──────────────────
    6101,  # [USADO] Venda de produção do estabelecimento
    6102,  # [USADO] Venda de mercadoria adquirida ou recebida de terceiros
    6103,  #         Venda de produção efetuada fora do estabelecimento
    6104,  #         Venda de mercadoria de terceiros efetuada fora do estab.
    6105,  #         Venda de produção armazenada em depósito fechado/armazém geral
    6106,  #         Venda de mercadoria de terceiros que não deva transitar
    6107,  # [USADO] Venda de produção destinada a não contribuinte
    6108,  # [USADO] Venda de mercadoria de terceiros destinada a não contribuinte
    6109,  #         Venda de produção destinada à ZFM/ALC
    6110,  #         Venda de mercadoria de terceiros destinada à ZFM/ALC
    6111,  #         Venda de produção remetida antes em consignação industrial
    6112,  #         Venda de mercadoria remetida antes em consignação industrial
    6113,  #         Venda de produção remetida antes em consignação mercantil
    6114,  # [USADO] Venda de mercadoria remetida antes em consignação mercantil
    6115,  #         Venda de mercadoria recebida antes em consignação mercantil
    6116,  #         Venda de produção originada de encomenda p/ entrega futura
    6117,  # [USADO] Venda de mercadoria originada de encomenda p/ entrega futura
    6118,  #         Venda de produção entregue por conta e ordem (venda à ordem)
    6119,  #         Venda de mercadoria entregue por conta e ordem (venda à ordem)
    6120,  # [USADO] Venda de mercadoria entregue pelo vendedor remetente, venda à ordem
    6122,  # [USADO] Venda de produção remetida p/ industrialização por conta e ordem
    6123,  # [USADO] Venda de mercadoria remetida p/ industrialização por conta e ordem
    6124,  # [USADO] Industrialização efetuada para outra empresa
    6125,  #         Industrialização p/ outra empresa sem trânsito pelo adquirente
    6129,  #         Venda de insumo importado/industrializado sob RECOF

    # ── Vendas para o exterior (7.1xx) ─────────────────────────────────────
    7101,  # [USADO] Venda de produção do estabelecimento
    7102,  # [USADO] Venda de mercadoria adquirida ou recebida de terceiros
    7105,  #         Venda de produção armazenada em depósito fechado
    7106,  #         Venda de mercadoria que não deva por ele transitar
    7127,  #         Venda de produção sob o regime de drawback
    7129,  #         Venda ao mercado externo sob RECOF
    7950,  #         Venda da produção do estabelecimento (código próprio do
           #         Domínio na versao_nat=2; zero uso na carteira hoje)

    # ── Venda de ENERGIA ELÉTRICA (5.25x / 6.25x / 7.251) ──────────────────
    # Zero uso na carteira hoje, mas é receita por construção: distribuidora
    # de energia fatura por aqui.
    5251, 5252, 5253, 5254, 5255, 5256, 5257, 5258,
    6251, 6252, 6253, 6254, 6255, 6256, 6257, 6258,
    7251,

    # ── Venda de COMBUSTÍVEL / LUBRIFICANTE (5.65x / 6.65x / 7.65x) ────────
    # Família com uso pesado em postos de combustível. Só as VENDAS
    # entram — remessa (5657), transferência
    # (5659), devolução (5661/5662) e retorno (5664/5665) ficam de fora.
    5651, 5652, 5653, 5654, 5655, 5656, 5667,
    6651, 6652, 6653, 6654, 6655, 6656, 6667,
    7651, 7654, 7667,

    # ── Prestação de serviço de COMUNICAÇÃO (5.30x / 6.30x) ────────────────
    5301, 5302, 5303, 5304, 5305, 5306, 5307,
    6301, 6302, 6303, 6304, 6305, 6306, 6307,

    # ── Vendas em substituição tributária (5.4xx / 6.4xx) ──────────────────
    5401,  # [USADO] Venda de produção em ST, contribuinte substituto
    5402,  #         Venda de produção em ST entre substitutos do mesmo produto
    5403,  #         Venda de mercadoria em ST, contribuinte substituto
    5404,  #         Venda de mercadoria em ST com imposto já retido
    5405,  # [USADO] Venda de mercadoria em ST, contribuinte substituído
    6401,  # [USADO] Venda de produção em ST, contribuinte substituto
    6402,  #         Venda de produção em ST entre substitutos do mesmo produto
    6403,  # [USADO] Venda de mercadoria em ST, contribuinte substituto
    6404,  #         Venda de mercadoria em ST com imposto já retido

    # ── Prestação de serviço de TRANSPORTE — receita do CT-e (codi_esp=37) ──
    # Sem esta família a receita de transportadora some.
    5351,  #         Transporte p/ execução de serviço da mesma natureza
    5352,  # [USADO] Transporte a estabelecimento industrial
    5353,  # [USADO] Transporte a estabelecimento comercial
    5354,  #         Transporte a prestador de serviço de comunicação
    5355,  #         Transporte a geradora/distribuidora de energia elétrica
    5356,  #         Transporte a produtor rural
    5357,  #         Transporte a não contribuinte
    5359,  #         Transporte de mercadoria sob regime de ST
    5360,  #         Transporte a contribuinte substituto do próprio frete
    5932,  # [USADO] Transporte iniciado em UF diversa da inscrição do prestador
    6351,  #         Transporte p/ execução de serviço da mesma natureza
    6352,  # [USADO] Transporte a estabelecimento industrial
    6353,  # [USADO] Transporte a estabelecimento comercial
    6354,  #         Transporte a prestador de serviço de comunicação
    6355,  #         Transporte a geradora/distribuidora de energia elétrica
    6356,  #         Transporte a produtor rural
    6357,  #         Transporte a não contribuinte
    6359,  #         Transporte de mercadoria sob regime de ST
    6360,  #         Transporte a contribuinte substituto do próprio frete
    6932,  # [USADO] Transporte iniciado em UF diversa da inscrição do prestador
    7358,  #         Transporte com destino ao exterior

    # ── Remessa com fim específico de exportação (5.50x / 6.50x) ───────────
    # Tem cara de remessa mas É receita: a venda ocorre via trading — o nome
    # do CFOP não decide, a DRE decide.
    5501,  #         Remessa de produção com fim específico de exportação
    5502,  #         Remessa de mercadoria de terceiros com fim de exportação
    6501,  # [USADO] Remessa de produção com fim específico de exportação
    6502,  #         Remessa de mercadoria de terceiros com fim de exportação
})


# ── ENTREGA FUTURA: a receita é reconhecida na EMISSÃO ──────────────────────
#
# O número certo é o do Domínio, que reconhece no SIMPLES FATURAMENTO
# (5922/6922) e não na entrega (5116/5117/6116/6117). Exceção à regra geral de
# CFOPs, restrita a esta família.
#
# POR QUÊ: contar a entrega evita dupla contagem, mas tem LAG — um lote
# emitido num ano e entregue no seguinte joga a receita no ano errado.
#
# POR QUE STATELESS: a tentação é parear nota a nota (FIFO por data/codi_sai) e
# tirar do card só a entrega PAREADA. Não pode. As duas rotas de execução
# alimentam o transform com frames diferentes — o run completo extrai janelado
# (`extract_efsaidas(empresa, inicio, fim)`) e o `--skip-extract` lê a história
# inteira (`SELECT * FROM raw.efsaidas`, sem filtro de data). Pareamento com
# estado entre meses leria fluxos diferentes e daria números diferentes pros
# MESMOS meses. O efeito de reconhecer na emissão é, por mês,
# `+5922/6922 − 5117/6117` — reclassificação pura de CFOP.
CFOPS_ENTREGA_FUTURA_EMISSAO: frozenset[int] = frozenset({5922, 6922})
CFOPS_ENTREGA_FUTURA_ENTREGA: frozenset[int] = frozenset({5116, 5117, 6116, 6117})

# DERIVADA, nunca copiada: a lista base tem 40+ códigos e duplicá-la convida
# divergência silenciosa. Coberta por TestConstantesEntregaFutura.
CFOPS_RECEITA_NFE_EMISSAO: frozenset[int] = (
    (CFOPS_RECEITA_NFE - CFOPS_ENTREGA_FUTURA_ENTREGA) | CFOPS_ENTREGA_FUTURA_EMISSAO
)

# Vigência POR EMPRESA: competência a partir da qual a emissão de entrega
# futura (5922/6922) conta como receita daquela empresa. Por padrão é o mês da
# 1ª emissão da empresa no raw.
#
# A base tem um mês de abertura: a entrega escriturada nos primeiros meses
# espelha emissão anterior, que não existe no banco. Aplicar a regra nova ali
# apagaria receita real. Um corte global também não serve: ele PARTE pares
# cujas duas pernas estão no dado (emissão antes do corte, entrega depois), e aí
# o dinheiro some dos DOIS lados. Ancorando na 1ª emissão da própria empresa,
# nenhum par pode ter emissão anterior à vigência: pares partidos = 0 POR
# CONSTRUÇÃO.
#
# Quando a 1ª emissão cai NA ABERTURA da base e ainda há entrega órfã a partir
# dela, a vigência é ADIADA até a próxima emissão da própria empresa. O
# adiamento pousa sempre num mês COM emissão, o que preserva "pares cortados
# = 0 por construção". A vigência protege a abertura; ela não é botão de
# ajuste fino.
#
# Empresa AUSENTE deste dict segue na regra ANTIGA.
ENTREGA_FUTURA_VIGENCIA: dict[int, str] = {101: "2024-03"}

# Empresas fora do critério de entrega futura DE PROPÓSITO. Existe pra
# distinguir "excluída com causa" de "empresa nova ainda não medida" — só a
# segunda é motivo de alarme.
EMPRESAS_ENTREGA_FUTURA_FORA: frozenset[int] = frozenset({103})


# CFOPs que PARECEM receita mas NÃO entram. NÃO é toggle: é documentação da
# convenção adotada.
#
# 5922/6922 NÃO estão aqui: entrega futura é reconhecida na emissão. Ver o
# bloco ENTREGA FUTURA acima.
CFOPS_RECEITA_AMBIGUOS: frozenset[int] = frozenset({
    # Nota espelho de operação já acobertada por cupom fiscal (ECF). Contar
    # duplicaria o varejo.
    5929, 6929,
    # "Outra saída não especificada": a DRE manda excluir.
    5949, 6949,
    # Remessa em comodato/locação: é a movimentação física do bem alugado,
    # não uma venda: só os recibos de locação entram.
    5908, 6908,
    # Serviço tributado pelo ISSQN dentro da NFe. É receita de serviço, mas a
    # MESMA receita tende a chegar por efservicos (NFS-e) -> entrar aqui
    # arriscaria contar duas vezes.
    5933, 6933,
})
