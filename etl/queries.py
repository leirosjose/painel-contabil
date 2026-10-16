"""SQL queries reutilizáveis contra o schema mart no Postgres.

Separado de etl/utils.py: utilitários puros não dependem de Postgres.
Separado de etl/extract.py: aquele extrai de SAP SQL Anywhere (bethadba.*);
este consulta o destino (mart.*).
"""


# Dígitos de um documento VÁLIDO: 11 = CPF, 14 = CNPJ. Qualquer outro
# comprimento (NULL, 12 dígitos) é registro estruturalmente inválido.
TAMANHOS_DOCUMENTO_VALIDO = (11, 14)


SQL_COBERTURA_EMPRESAS = """
SELECT
  e.codi_emp,
  e.nome_emp,
  e.cgce_emp,
  (e.stat_emp IS DISTINCT FROM 'I') AS ativa,
  EXISTS(SELECT 1 FROM mart.faturamento_mensal m
         WHERE m.codi_emp=e.codi_emp AND m.competencia BETWEEN %s AND %s) AS tem_fat,
  EXISTS(SELECT 1 FROM mart.impostos_mensais m
         WHERE m.codi_emp=e.codi_emp AND m.competencia BETWEEN %s AND %s) AS tem_imp,
  EXISTS(SELECT 1 FROM mart.funcionarios_mensal m
         WHERE m.codi_emp=e.codi_emp AND m.competencia BETWEEN %s AND %s) AS tem_func,
  EXISTS(SELECT 1 FROM mart.folha_mensal m
         WHERE m.codi_emp=e.codi_emp AND m.competencia BETWEEN %s AND %s) AS tem_folha,
  EXISTS(SELECT 1 FROM mart.balancete_mensal m
         WHERE m.codi_emp=e.codi_emp AND m.competencia BETWEEN %s AND %s) AS tem_bal
FROM mart.empresas e
ORDER BY e.nome_emp
"""
# O SQL devolve a carteira INTEIRA + o flag `ativa`; quem decide a
# visibilidade é `carteira_visivel`, abaixo — função pura, testável sem banco.

# Flags de cobertura por mart, na ordem em que o SQL as produz. Quem monta
# badge no consumer usa a MESMA lista.
FLAGS_COBERTURA = ("tem_fat", "tem_imp", "tem_func", "tem_folha", "tem_bal")


def params_cobertura(inicio: str, fim: str) -> tuple:
    """Tupla de placeholders pra SQL_COBERTURA_EMPRESAS.

    Conta dinamicamente os `%s` no SQL pra derivar quantos pares (inicio, fim)
    são necessários — elimina o magic number `* 5` que era frágil a edits.
    """
    n_pairs = SQL_COBERTURA_EMPRESAS.count("%s") // 2
    return (inicio, fim) * n_pairs


def carteira_visivel(df):
    """Filtra a carteira exibida: `ativa OR (dado na janela AND documento)`.

    **Fonte ÚNICA do critério** — `app.py` (dropdown) e `pdf_generator.py`
    (unidades do lote) chamam este helper logo depois do `read_sql`. Consumer
    que esquecer a chamada exibe a carteira inteira; o guard de grep na suite
    é o que pega isso.

    **Por que o critério existe:** inativação é fato cadastral de HOJE e não
    apaga o faturamento de ontem. Esconder inativa incondicionalmente faz uma
    filial inativada sumir do dropdown e do total do grupo consolidado,
    mudando a série histórica retroativamente.

    **O documento válido** é definição de carteira, não materialidade — não há
    limiar em reais. Registro sem documento (`cgce_emp` NULL ou com
    comprimento inválido) é barrado; CPF (11 dígitos) e CNPJ (14 dígitos)
    entram.

    O `only_digits` normaliza os dois lados, mesma régua que a busca por CNPJ
    do dropdown usa — CNPJ pontuado conta igual. Ele entra por import LOCAL,
    pelo mesmo motivo que o `CLAS_PASSIVO_IRPJ_CSLL` em `params_passivo_ir`:
    `etl.queries` não passa a depender de outro módulo `etl` no topo.
    """
    from etl.utils import only_digits

    if df.empty:
        return df
    tem_dado = df[list(FLAGS_COBERTURA)].any(axis=1)
    documento = df["cgce_emp"].apply(
        lambda v: len(only_digits(v)) in TAMANHOS_DOCUMENTO_VALIDO
    )
    return df[df["ativa"] | (tem_dado & documento)]


# Passivo de IRPJ/CSLL — fonte do fallback para empresa que APURA e PAGA mas
# nao emite a guia pelo modulo do Dominio. Le o DEBITO nas contas de
# `CLAS_PASSIVO_IRPJ_CSLL`, coerente com "o numero e o que pagou".
#
# Vive aqui, e nao em cada consumer, porque o `app.py` e o `pdf_generator.py`
# precisam da MESMA carga.
SQL_PASSIVO_IRPJ_CSLL = """
SELECT codi_emp, competencia, clas_cta, nome_cta, valo_deb
FROM mart.balancete_mensal
WHERE codi_emp = ANY(%s) AND competencia >= %s AND competencia <= %s
  AND clas_cta = ANY(%s)
"""


def params_passivo_ir(emp_ids, inicio: str, fim: str) -> tuple:
    """Tupla de placeholders pra SQL_PASSIVO_IRPJ_CSLL, com a janela ALARGADA.

    O `fim` sai daqui somado de **3 meses**, e esse alargamento e a razao de o
    helper existir. O eixo do tempo do fallback e a COMPETENCIA DO TRIMESTRE:
    a quota de jan-mar liquida o T4 do ano ANTERIOR, entao sem os 3 meses
    seguintes o dezembro da janela sai vazio. Sem o `+3` o valor se perde em
    silencio (o resultado continua plausivel, so menor), por isso o
    alargamento e do helper e nao do call site.

    `emp_ids` vai como LISTA (psycopg2 mapeia pra ARRAY no `= ANY(%s)`), e
    `CLAS_PASSIVO_IRPJ_CSLL` idem — import local pra `etl.queries` nao passar a
    depender de `etl.mappings` no topo.
    """
    from etl.mappings import CLAS_PASSIVO_IRPJ_CSLL

    y, m = int(fim[:4]), int(fim[5:7])
    m += 3
    y, m = (y + (m - 1) // 12, (m - 1) % 12 + 1)
    fim3 = f"{y:04d}-{m:02d}"
    ids = [int(e) for e in ([emp_ids] if isinstance(emp_ids, int) else emp_ids)]
    return (ids, inicio, fim3, list(CLAS_PASSIVO_IRPJ_CSLL))
