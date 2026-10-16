# ---------------------------------------------------------------------------
# load.py — Carga de DataFrames no PostgreSQL (schemas raw e mart)
# ---------------------------------------------------------------------------

import logging

import pandas as pd
from psycopg2.extras import execute_values

from connections import connect_postgres
from config import PG_SCHEMA_RAW, PG_SCHEMA_MART, PG_SCHEMA_RAW_HIST

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mapeamento pandas dtype → PostgreSQL
# ---------------------------------------------------------------------------

_DTYPE_MAP = {
    "int64":          "BIGINT",
    "int32":          "INTEGER",
    "int16":          "SMALLINT",
    "float64":        "DOUBLE PRECISION",
    "float32":        "REAL",
    "bool":           "BOOLEAN",
    "datetime64[ns]": "TIMESTAMP",
    "date":           "DATE",
}


# Colunas cujo tipo NAO se infere do dado — declaradas.
# codi_emp e o EIXO de todo join do projeto: chave de upsert em 18 das 20
# tabelas de raw (as 2 de fora sao dicionarios GLOBAIS — foeventosdef e
# foclassificacoes_eventos) e em todos os marts. Inferir o tipo dele do lote
# pode criar a coluna como TEXT: ela nasce BIGINT por DECISAO, nao por sorte
# do DataFrame que criou a tabela. Seguro no INSERT mesmo se a origem entregar
# texto — psycopg2 manda str como literal unknown e o Postgres coage
# ('1' = 1::bigint -> t).
#
# Risco residual que ESTE mapa nao cobre: coluna NAO-chave que venha toda
# NULL num lote fossiliza TEXT do mesmo jeito. O guard de df vazio no
# upsert_raw fecha o caso do DataFrame INTEIRO vazio; coluna vazia dentro de
# df populado segue em aberto.
_TIPO_DECLARADO: dict[str, str] = {
    "codi_emp": "BIGINT",
    # Colunas tecnicas da geracao do raw. BIGINT e nao
    # UUID no id (custo em ~10M linhas); a fatia cabe em INTEGER (AAAAMMDD).
    "_generation_id": "BIGINT",
    "_fatia": "INTEGER",
}


def _pg_type(dtype, col: str | None = None) -> str:
    if col is not None and col in _TIPO_DECLARADO:
        return _TIPO_DECLARADO[col]
    return _DTYPE_MAP.get(str(dtype), "TEXT")


def _to_py(v):
    """Converte valores numpy/pandas para tipos nativos do Python.

    Ausências (None/NaN/NaT/pd.NA) viram None via pd.isna — guard
    ESCALAR: as células vêm de itertuples em _df_to_rows; um
    array aqui estouraria o if, e deve estourar. date/datetime e
    demais nativos passam direto (psycopg2 adapta).
    """
    if pd.isna(v):
        return None
    if hasattr(v, "item"):          # numpy scalar
        return v.item()
    return v


def _is_bytes_like(x) -> bool:
    return isinstance(x, (bytes, bytearray)) or "byte" in type(x).__name__.lower()


def _sanitize_binary_cols(df: pd.DataFrame) -> pd.DataFrame:
    """Remove colunas que contenham valores binários (bytes, bytearray, Java byte[]).

    psycopg2 não consegue adaptar tipos binários para TEXT. GUIDs e outros
    campos binários do SQL Anywhere são descartados antes do INSERT.
    """
    object_cols = [col for col in df.columns if df[col].dtype == object]
    if object_cols:
        logger.debug("[sanitize] colunas dtype=object antes da sanitização: %s", object_cols)
    cols_to_drop = [
        col for col in object_cols
        if df[col].dropna().apply(_is_bytes_like).any()
    ]
    if cols_to_drop:
        logger.debug("[sanitize] removendo colunas binárias: %s", cols_to_drop)
        df = df.drop(columns=cols_to_drop)
    return df


def _df_to_rows(df: pd.DataFrame) -> list[tuple]:
    return [tuple(_to_py(v) for v in row) for row in df.itertuples(index=False)]


# ---------------------------------------------------------------------------
# Infraestrutura de schemas e tabelas
# ---------------------------------------------------------------------------

class LayoutRawAntigo(RuntimeError):
    """Banco no layout pre-geracao (raw ainda e tabela) ou em estado desconhecido."""


def ensure_schemas() -> None:
    """Schemas, manifesto de geracao e indices do mart -- e a GUARDA DE BOOT.

    Banco com raw.<t> ainda TABELA = layout antigo: aborta mandando migrar
    (codigo novo nunca grava por cima de layout antigo). Banco NOVO (raw vazio)
    ja nasce no layout de geracao e registra a versao. VIEW sem a versao
    registrada = estado desconhecido: aborta. Codigo e schema se conferem no boot.

    Cria também índices em (codi_emp, competencia) nos marts principais —
    crucial para a query EXISTS de cobertura no app.py (5× por empresa) e
    para os SELECTs filtrados por (codi_emp, competencia) do dashboard.
    """
    from geracao import garante_ops                      # import local: evita ciclo load<->geracao
    from retroativas import garante_alteracoes           # tabela aditiva de alteracoes retroativas
    from config import VERSAO_SCHEMA_GERACAO

    conn = connect_postgres()
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {PG_SCHEMA_RAW}")
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {PG_SCHEMA_MART}")
            cur.execute("SELECT table_name, table_type FROM information_schema.tables "
                        "WHERE table_schema = %s", (PG_SCHEMA_RAW,))
            tipos = dict(cur.fetchall())
            antigas = sorted(t for t, ty in tipos.items() if ty == "BASE TABLE")
            if antigas:
                raise LayoutRawAntigo(
                    f"raw no layout ANTIGO ({len(antigas)} tabela(s), ex. {antigas[:3]}): "
                    f"rode scripts/migra_geracao_raw.py com o servico parado (RUNBOOK_DEPLOY secao 8)")
            garante_ops(cur)
            garante_alteracoes(cur)
            if tipos:
                cur.execute("SELECT 1 FROM ops.schema_version WHERE versao = %s", (VERSAO_SCHEMA_GERACAO,))
                if cur.fetchone() is None:
                    raise LayoutRawAntigo("raw tem VIEW mas ops.schema_version nao registra a "
                                          "migracao de geracao: estado desconhecido")
            else:
                cur.execute("INSERT INTO ops.schema_version (versao, detalhe) VALUES "
                            "(%s, 'banco novo: nasceu no layout de geracao') ON CONFLICT DO NOTHING",
                            (VERSAO_SCHEMA_GERACAO,))
            for tabela in (
                "faturamento_mensal", "impostos_mensais",
                "funcionarios_mensal", "folha_mensal", "balancete_mensal",
                "empresa_regime_periodo", "impostos_destacados_mensais",
                "encargos_mensais", "dre_mensal", "folha_rubricas_mensais",
            ):
                cur.execute(f"""
                    DO $$ BEGIN
                        IF EXISTS (SELECT 1 FROM information_schema.tables
                                   WHERE table_schema='{PG_SCHEMA_MART}' AND table_name='{tabela}') THEN
                            EXECUTE 'CREATE INDEX IF NOT EXISTS idx_{tabela}_emp_comp '
                                 || 'ON {PG_SCHEMA_MART}."{tabela}" (codi_emp, competencia)';
                        END IF;
                    END $$;
                """)
        conn.commit()
    finally:
        conn.close()


def _ensure_table(
    conn,
    schema: str,
    table: str,
    df: pd.DataFrame,
    key_cols: list[str],
) -> None:
    """Cria a tabela se não existir, inferindo tipos das colunas do DataFrame."""
    col_defs = ", ".join(f'"{c}" {_pg_type(df[c].dtype, c)}' for c in df.columns)
    constraint = ""
    if key_cols:
        key_sql = ", ".join(f'"{c}"' for c in key_cols)
        constraint = f", CONSTRAINT {table}_uq UNIQUE ({key_sql})"
    sql = f'CREATE TABLE IF NOT EXISTS {schema}."{table}" ({col_defs}{constraint})'
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def _add_missing_columns(
    conn,
    schema: str,
    table: str,
    df: pd.DataFrame,
) -> None:
    """Adiciona colunas novas ao schema caso o DataFrame tenha evoluído."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            """,
            (schema, table),
        )
        existing = {row[0] for row in cur.fetchall()}

    new_cols = [c for c in df.columns if c not in existing]
    if not new_cols:
        return
    with conn.cursor() as cur:
        for col in new_cols:
            cur.execute(
                f'ALTER TABLE {schema}."{table}" ADD COLUMN IF NOT EXISTS '
                f'"{col}" {_pg_type(df[col].dtype, col)}'
            )
    conn.commit()


# ---------------------------------------------------------------------------
# Upsert genérico
# ---------------------------------------------------------------------------

def _upsert(
    conn,
    schema: str,
    table: str,
    df: pd.DataFrame,
    key_cols: list[str],
    scope_cols: list[str] | None = None,
) -> int:
    """INSERT ON CONFLICT no Postgres. Se scope_cols (ex.: [codi_emp, competencia]),
    faz DELETE-escopado dos pares presentes no df ANTES do insert, na MESMA transacao
    -> limpa orfas tipo A. Pressuposto + rationale do no-guard no
    comentario ao lado do bloco DELETE."""
    if df.empty:
        return 0

    if key_cols:
        df = df.drop_duplicates(subset=key_cols, keep="last")

    cols = list(df.columns)
    cols_sql = ", ".join(f'"{c}"' for c in cols)

    update_cols = [c for c in cols if c not in key_cols]
    if update_cols:
        update_sql = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in update_cols)
        conflict_action = f"DO UPDATE SET {update_sql}"
    else:
        conflict_action = "DO NOTHING"

    conflict_sql = ", ".join(f'"{c}"' for c in key_cols) if key_cols else ""
    on_conflict = f"ON CONFLICT ({conflict_sql}) {conflict_action}" if key_cols else ""

    insert_sql = (
        f'INSERT INTO {schema}."{table}" ({cols_sql}) VALUES %s {on_conflict}'
    )
    rows = _df_to_rows(df)
    with conn.cursor() as cur:
        if scope_cols and all(c in df.columns for c in scope_cols):
            # PRESSUPOSTO: o df e COMPLETO para cada par (scope_cols) que contem.
            # O DELETE apaga TODAS as linhas do par e re-insere SO as do df -> um
            # transform bugado que produza um par PRESENTE porem com MENOS linhas que
            # o correto perde as boas, silencioso (difere do tipo B: par AUSENTE do df
            # nao e deletado e sobrevive). Recuperavel: raw nunca e deletado (re-run
            # reconstroi). SEM guard de sanidade de proposito: ler o estado anterior
            # quebraria a pureza do _upsert.
            # psycopg2 NAO adapta numpy.int64 -> _to_py em cada celula do par.
            pairs = [
                tuple(_to_py(v) for v in r)
                for r in df[scope_cols].drop_duplicates().itertuples(index=False, name=None)
            ]
            scope_sql = ", ".join(f'"{c}"' for c in scope_cols)
            execute_values(
                cur,
                f'DELETE FROM {schema}."{table}" WHERE ({scope_sql}) IN (VALUES %s)',
                pairs,
            )
        execute_values(cur, insert_sql, rows)
    conn.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def upsert_raw(table: str, df: pd.DataFrame, key_cols: list[str]) -> int:
    """Insere ou atualiza registros no schema raw.

    DataFrame VAZIO não cria tabela. O tipo de cada coluna sai do dtype
    (`_pg_type`), e num df sem linhas TODA coluna é `object` -> a tabela
    nasceria com tudo TEXT e o `CREATE TABLE IF NOT EXISTS` congelaria isso
    pra sempre (não há um único ALTER TYPE no codebase).

    O --skip-extract não precisa da estrutura da tabela vazia — `run_read_raw`
    já degrada por try/except e `run_transform._get` já tolera chave ausente.
    Tabela nunca criada rende ALERTA no resumo (extracao FALHOU + mart FALHOU
    nos builds que validam colunas), em vez de passar por "vazia" em silêncio.

    Args:
        table:    nome da tabela sem schema prefix.
        df:       DataFrame retornado pelo extract.py.
        key_cols: colunas que formam a chave de conflito.

    Returns:
        Número de linhas processadas.
    """
    if df.columns.empty or df.empty:
        return 0
    df = _sanitize_binary_cols(df)
    conn = connect_postgres()
    try:
        _ensure_table(conn, PG_SCHEMA_RAW, table, df, key_cols)
        _add_missing_columns(conn, PG_SCHEMA_RAW, table, df)
        return _upsert(conn, PG_SCHEMA_RAW, table, df, key_cols)
    finally:
        conn.close()


def expurga_geracoes_si(manter: list[str]) -> dict:
    """Apaga as geracoes de `raw_hist.ctsaldo_inicial` que nao sao mais uteis.

    Com `data_corte` na chave as geracoes deixam de se
    sobrescrever — o que resolve a perda do canonico, mas faz o raw crescer
    ~15 MB por janela distinta ja rodada. O expurgo mantem o estado estavel
    em DUAS geracoes: a canonica e a deste run.

    Este e o UNICO DELETE em `raw_hist` do pipeline (o `raw` e VIEW), e a regra "raw nunca e deletado por run" nao e arranhada porque uma
    geracao de si nao e dado
    da ORIGEM: e artefato de extracao NOSSO, parametrizado pela janela do
    run que o produziu. O que a origem tem sao os lancamentos, e eles
    continuam intactos — qualquer geracao se reconstroi re-extraindo.

    `manter` nunca pode chegar vazio, e o canonico tem que estar nele: a
    protecao esta no CALL SITE, mas o assert aqui e o cinto, porque o modo
    de falha e apagar exatamente aquilo que o fix veio preservar.

    Linhas com `data_corte` NULL NAO sao tocadas: `NOT IN` nunca
    casa NULL, e o que fazer com elas e decisao da migracao, com o numero
    na mesa. Sao contadas e devolvidas pra virar aviso.
    """
    assert manter, "expurgo de si com lista de cortes a manter VAZIA"
    conn = connect_postgres()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f'DELETE FROM {PG_SCHEMA_RAW_HIST}."ctsaldo_inicial" '
                "WHERE data_corte IS NOT NULL AND data_corte <> ALL(%s) "
                "RETURNING data_corte",
                (list(manter),),
            )
            apagados = [r[0] for r in cur.fetchall()]
            cur.execute(
                f'SELECT count(*) FROM {PG_SCHEMA_RAW_HIST}."ctsaldo_inicial" '
                "WHERE data_corte IS NULL"
            )
            nulos = cur.fetchone()[0]
            # A fatia do si e AAAAMMDD do corte; fatia expurgada sai do mapa,
            # senao o detector acusaria mapa apontando pra linha que nao existe.
            fatias_mantidas = [int(c.replace("-", "")) for c in manter]
            cur.execute("DELETE FROM ops.raw_slice_vigente WHERE table_name = 'ctsaldo_inicial' "
                        "AND NOT (fatia = ANY(%s))", (fatias_mantidas,))
        conn.commit()
    finally:
        conn.close()
    return {"linhas": len(apagados),
            "cortes": sorted(set(apagados)),
            "nulos": int(nulos)}


def upsert_mart(table: str, df: pd.DataFrame, key_cols: list[str],
                scope_cols: list[str] | None = None) -> int:
    """Insere ou atualiza registros no schema mart.

    Args:
        table:      nome da tabela sem schema prefix.
        df:         DataFrame retornado pelo transform.py.
        key_cols:   colunas que formam a chave de conflito.
        scope_cols: se dado, DELETE-escopado desses pares antes do insert.

    Returns:
        Número de linhas processadas.
    """
    if df.empty:
        return 0
    df = _sanitize_binary_cols(df)
    conn = connect_postgres()
    try:
        _ensure_table(conn, PG_SCHEMA_MART, table, df, key_cols)
        _add_missing_columns(conn, PG_SCHEMA_MART, table, df)
        return _upsert(conn, PG_SCHEMA_MART, table, df, key_cols, scope_cols=scope_cols)
    finally:
        conn.close()
