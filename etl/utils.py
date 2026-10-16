"""Utilitários puros compartilhados entre app.py e pdf_generator.py.

Funções de formatação (período, BRL, %, indicador), divisão segura, média
robusta a inf/-inf, helpers de CNPJ e soma agregada de balancete por prefixo
de clas_cta. Sem dependências de Streamlit, Plotly ou Jinja2 — só pandas +
mappings (+ psycopg2 exclusivamente em `get_regime`).

Nota: `etl/extract.py` mantém limpeza de CNPJ via `.str.replace` vectorizado
(C-level) por performance; `only_digits` daqui é a versão escalar.
"""

import math
import re
import unicodedata

import pandas as pd

from etl.mappings import NOMES_MES, TIPO_PROCESS_NOMES


def p_label(yyyymm):
    y, m = yyyymm.split("-")
    return NOMES_MES[int(m) - 1] + "/" + y[2:]


def rotulo_tipo_process(tp):
    """FOBASES.tipo_process -> "Folha Mensal" (so o nome).

    O codigo NAO entra no rotulo: e jargao de sistema e nao ajuda quem le o
    relatorio. Ele so aparece quando NAO ha
    nome pro codigo — "Tipo NN" —, caso em que o numero e a unica informacao
    disponivel; mesmo espirito do nome_imp, que degrada pro numero cru:
    tipo novo do Dominio nunca pode quebrar a tela. Valor nao numerico
    (None/NaN/lixo) sai como esta (ou "—" se vazio), nunca levanta.
    """
    if tp is None or (isinstance(tp, float) and math.isnan(tp)):
        return "—"
    try:
        codigo = int(float(tp))
    except (TypeError, ValueError):
        return str(tp)
    return TIPO_PROCESS_NOMES.get(codigo, f"Tipo {codigo}")


def periods_in_range(de_str, ate_str):
    return [str(p) for p in pd.period_range(de_str, ate_str, freq="M")]


def safe_div(a, b):
    # Guard nos DOIS lados: None/NaN no numerador (coluna 100%-NULL do
    # Postgres vira object/None no read_sql) retornava TypeError — o
    # contrato e NaN -> fmt '—'.
    return pd.Series(
        [x / y if (y and not pd.isna(y) and not pd.isna(x)) else float("nan")
         for x, y in zip(a, b)],
        index=a.index,
    )


def clean_mean(s):
    """Média ignorando inf/-inf — mesmo helper usado no pdf_generator."""
    return s.replace([float("inf"), float("-inf")], float("nan")).mean()


def weighted_pct(num, den):
    """Média ponderada de pct ao agregar série mensal num único valor.

    Equivalente a SUM(num)/SUM(den)*100. Substitui
    clean_mean(safe_div(num, den)*100) nos KPIs médios. O clean_mean da
    série de pcts mensais é média SIMPLES — invisível quando num e den são
    proporcionais, mas distorce magnitude quando eles divergem.
    """
    total_n = num.sum(min_count=1)
    total_d = den.sum(min_count=1)
    if pd.isna(total_d) or total_d <= 0:
        return float("nan")
    return total_n / total_d * 100


def luc_nf(fat, desp, imp):
    """Numerador NF-based da lucratividade: fat - desp - imp.fillna(0).

    O lucro do card da planilha de referência e
    (Faturamento_NF - Despesas e Custos - Impostos), NAO o resultado
    contabil (receita 3xx - despesa). fat/desp/imp sao Series por
    competencia; imp = impostos destacados, com mes sem dado subtraindo 0
    (consistente com fat fill_value=0). Fonte unica pros 2 consumers
    (app.py + pdf_generator.py).
    """
    return fat - desp - imp.fillna(0)


def fmt_brl(v):
    if v is None or (isinstance(v, float) and (pd.isna(v) or math.isinf(v))):
        return "—"
    # Evita "R$ -0,00": saldo de -0,004 que o arredondamento de exibição
    # transformaria num negativo que não existe. Só o que ARREDONDA pra zero
    # é normalizado: negativo REAL segue cru, porque a nota de saldo invertido
    # depende de ele aparecer.
    if abs(v) < 0.005:
        v = 0.0
    return f"R$ {v:_.2f}".replace(".", ",").replace("_", ".")


def fmt_brl_curto(v):
    """Valor abreviado pra RÓTULO DE BARRA: "1,2M", "988 mil", "850".

    O milhar é " mil" POR EXTENSO: o "K" é jargão de planilha e o rótulo é
    lido em reunião com o cliente. O "M" do milhão FICA — é a abreviação que o próprio leitor usa
    falando, e "1,2 milhões" dobraria a largura justo no rótulo mais comum.

    Fonte ÚNICA dos dois renders (Streamlit e PDF) — abreviar de cada lado
    produziria dois formatos pro mesmo número na mesma apresentação.

    Devolve **string vazia** quando não há rótulo a exibir: None, NaN, inf ou
    valor que arredonda pra zero. Barra de altura zero com "0" em cima é
    ruído — o mês sem movimento já se lê pela ausência da barra.

    Sem "R$": o eixo e o título do chart já dizem que é dinheiro, e o prefixo
    dobraria a largura do rótulo justamente onde o espaço é escasso (janela de
    36-48 meses).
    """
    if v is None or (isinstance(v, float) and (pd.isna(v) or math.isinf(v))):
        return ""
    v = float(v)
    if abs(v) < 0.5:
        return ""
    sinal = "-" if v < 0 else ""
    a = abs(v)

    # Arredondamento HALF-UP, não o padrão do Python (half-to-even): o `.0f`
    # nativo transforma 2.500 em "2K" e 3.500 em "4K", que ao lado um do outro
    # parece defeito. Em rótulo de dinheiro a expectativa é a da calculadora.
    def meio_pra_cima(x, casas=0):
        f = 10 ** casas
        return math.floor(x * f + 0.5) / f

    # 999.999 arredondaria pra "1000 mil": promove pra M antes de formatar,
    # senão o rótulo fica mais largo que a faixa que ele deveria caber.
    if a >= 1_000_000 or meio_pra_cima(a / 1_000) >= 1_000:
        return sinal + f"{meio_pra_cima(a / 1_000_000, 1):.1f}".replace(".", ",") + "M"
    if a >= 1_000:
        return f"{sinal}{meio_pra_cima(a / 1_000):.0f} mil"
    return f"{sinal}{meio_pra_cima(a):.0f}"


def fmt_pct(v):
    if v is None or (isinstance(v, float) and (pd.isna(v) or math.isinf(v))):
        return "—"
    return f"{v:.1f}%".replace(".", ",")


def fmt_ind(v):
    if v is None or (isinstance(v, float) and (pd.isna(v) or math.isinf(v))):
        return "—"
    return f"{v:.2f}".replace(".", ",")


def fmt_int(v):
    """Inteiro com separador de milhar."""
    if v is None or (isinstance(v, float) and (pd.isna(v) or math.isinf(v))):
        return "—"
    return f"{int(v):_}".replace("_", ".")


def normaliza_rubrica(nomes):
    """Nome de rubrica sem acento e em CAIXA ALTA, pro regex casar.

    Todo criterio novo por nome de rubrica normaliza ANTES de casar. A origem
    escreve a MESMA verba de mais de um jeito e a diferenca e so o acento:
      - "SALARIO FAMILIA" e "SALARIO FAMÍLIA" convivem na carteira — um regex
        `FAMIL` ASCII pega a primeira e perde a segunda;
      - "SUSPENSÃO" (familia Falta Integral) so existe ACENTUADA — um regex
        `SUSPENSAO` nao casa nunca.
    Normalizar de uma vez e melhor que espalhar `[AÁ]`/`[IÍ]` por cada padrao:
    a classe de caractere resolve a letra que alguem lembrou, e a proxima
    rubrica acentuada volta a escapar calada.

    Aceita Series ou escalar; devolve o mesmo formato.
    """
    if isinstance(nomes, pd.Series):
        return (nomes.astype(str)
                .str.normalize("NFKD")
                .str.encode("ascii", "ignore").str.decode("ascii")
                .str.upper())
    return (unicodedata.normalize("NFKD", str(nomes))
            .encode("ascii", "ignore").decode("ascii").upper())


def only_digits(value) -> str:
    """Extrai só dígitos de qualquer valor. Retorna '' para None/NaN/inválido."""
    return re.sub(r"\D", "", str(value or ""))


def cnpj_raiz(value):
    """Primeiros 8 dígitos do CNPJ (raiz). None se < 8 dígitos válidos."""
    digits = only_digits(value)
    return digits[:8] if len(digits) >= 8 else None


def cnpj_filial(value):
    """Dígitos 9-12 do CNPJ (sufixo de filial). None se < 12 dígitos."""
    digits = only_digits(value)
    return digits[8:12] if len(digits) >= 12 else None


MARCA_INATIVA = " [INATIVA]"


def marca_inativa(ativa) -> str:
    """Sufixo `" [INATIVA]"` pra empresa que só está na carteira pelo histórico.

    `etl.queries.carteira_visivel` mantém no dropdown (e no
    lote de PDF) a empresa inativada que ainda tem dado na janela — inativação
    não apaga o faturamento de ontem. Sem esta marca ela reapareceria CALADA, e
    o operador não teria como saber que o número é de uma empresa já baixada.

    ASCII de propósito: o rótulo já carrega até 5 emoji de badge de cobertura, e
    um sexto símbolo competiria com eles em vez de qualificar o nome.

    ⚠️ Isto é ROTULO, nunca identidade nem `nome`. O `nome` da empresa alimenta
    o filename do PDF (`Dashboard_{empresa_nome}_...pdf` no app e
    `_safe_filename` no lote), e `_safe_filename` reduz `[` e `]` a nada — a
    marca vazaria como `NOME_INATIVA.pdf`. Ela viaja em campo separado; há
    teste cravando esse contrato.
    """
    return "" if ativa else MARCA_INATIVA


def label_empresa(nome, badges="", cnpj=None):
    """Rótulo de UMA opção do dropdown de empresa: nome + badges de cobertura.

    O `nome` sofre `rstrip`, nunca `strip`: a INDENTAÇÃO À ESQUERDA é semântica
    no dropdown (a filial entra como `"   └ Filial 0002"`, aninhada sob o item
    do grupo). Limpar os dois lados achataria a árvore.

    O rótulo NÃO é identidade — a identidade da opção é a tupla de `codi_emp`,
    justamente pra rótulo repetido entre homônimas nunca colapsar seleção.

    `cnpj` é OPCIONAL e hoje **nenhum consumer passa**: a busca por CNPJ é
    feita por um predicado separado, que normaliza os DOIS lados. O parâmetro
    fica, com teste, pra quem precisar de um rótulo auto-suficiente.
    """
    digits = only_digits(cnpj)
    partes = [str(nome).rstrip(), str(badges).strip(), digits]
    return " ".join(p for p in partes if p.strip())


def regime_dominante(regime_df):
    """Regime mais frequente em regime_df (linhas empresa×competencia da
    janela carregada de mart.empresa_regime_periodo). Retorna
    'simples' | 'real_presumido' | None (vazio ou empate -> None, nao
    suprime — conservador: so suprime a lucratividade com confianca).

    Usado p/ suprimir o card de Lucratividade em Simples (espelha a planilha
    de referência, que nao tem card de lucratividade pra Simples).
    """
    if regime_df is None or getattr(regime_df, "empty", True):
        return None
    if "regime" not in regime_df.columns:
        return None
    vc = regime_df["regime"].value_counts()
    if vc.empty:
        return None
    if len(vc) > 1 and vc.iloc[0] == vc.iloc[1]:
        return None
    return vc.index[0]


def bal_grupo_serie(bal_df, periods, prefixes):
    """Soma valo_saldo para contas cujo clas_cta começa com algum dos prefixos.

    Contas de balanço (prefixo 1/2): saldo no mart é acumulado e só existe em
    mês com movimento (+ phantom de saldo inicial na 1ª competência da
    empresa). A série carrega o último saldo conhecido PARA FRENTE (ffill) —
    nunca para trás (bfill inventaria saldo retroativo de conta que nasce no
    meio da janela). O reindex usa a UNIÃO (índice real ∪ periods):
    linhas anteriores a periods[0] (seed de carry-in injetado pelos consumers)
    ancoram o saldo que vem de antes da janela. Pivot por (codi_emp, clas_cta,
    codi_cta) conforme as colunas existam — o ffill é POR CONTA (contas irmãs da mesma clas_cta ancoram cada uma a própria série; na
    consolidação por CNPJ raiz, por conta de cada filial). DataFrame sem
    codi_cta (fixtures/mart antigo) cai no fallback por clas_cta.
    Contas de resultado (3/4): movimento mensal, sem fill.
    """
    if bal_df.empty:
        return pd.Series(0.0, index=periods)
    if isinstance(prefixes, str):
        prefixes = (prefixes,)
    clas = bal_df["clas_cta"].astype(str)
    mask = pd.Series(False, index=bal_df.index)
    for p in prefixes:
        mask = mask | clas.str.startswith(p)
    df = bal_df[mask]
    if df.empty:
        return pd.Series(0.0, index=periods)
    first_prefix = str(prefixes[0])[:1] if prefixes else ""
    is_balance = first_prefix in ("1", "2")
    if is_balance:
        cols = [c for c in ("codi_emp", "clas_cta", "codi_cta") if c in df.columns]
        piv = df.pivot_table(index="competencia", columns=cols,
                             values="valo_saldo", aggfunc="sum")
        full = piv.index.union(pd.Index(periods))
        piv = piv.reindex(full).ffill()
        return piv.loc[periods].sum(axis=1).fillna(0.0)
    return df.groupby("competencia")["valo_saldo"].sum().reindex(periods, fill_value=0)


def classify_etl_proc(proc):
    """Classifica o subprocess do ETL guardado entre reruns.

    Retorna:
      "absent"   — sem processo registrado (proc is None);
      "alive"    — processo vivo (poll() is None);
      "finished" — processo terminou (poll() devolveu returncode).

    Recebe o objeto Popen (duck-typed: só usa .poll()), não PID cru —
    imune a reuso de PID e portável Windows/macOS. Único helper deste
    módulo que não é de formatação/agregação: vive aqui pra ser testável
    sem importar app.py (importar app.py executa o script Streamlit).
    """
    if proc is None:
        return "absent"
    if proc.poll() is None:
        return "alive"
    return "finished"


def consume_role(slot):
    """Papel desta sessão no consumo do pipeline ETL.

    try-lock NÃO-bloqueante no slot["lock"]: quem adquire é o
    consumidor ÚNICO ("owner") e DEVE soltar no finally; quem não adquire
    ("spectator") só renderiza o snapshot do slot, sem tocar a Queue. Garante 1
    consumidor — fecha as 4 janelas do race (steal da Queue, n_ok perdido,
    truncamento, KeyError no cleanup). Vive aqui (não em app.py) pra ser testável
    sem importar o script Streamlit, como classify_etl_proc.

    NÃO-bloqueante de propósito: o consumo dura minutos; bloquear/esperar o lock
    congelaria a aba espectadora.
    """
    lock = slot.get("lock")
    if lock is not None and lock.acquire(blocking=False):
        return "owner"
    return "spectator"


def etl_progress_frac(n_ok, total):
    """Fração da barra de progresso do ETL: n_ok/total com teto 0.95 (só o
    desfecho final crava 1.0). Pura/testável; reusada pelo owner e pelo
    espectador no render do snapshot."""
    return min(n_ok / total, 0.95)
