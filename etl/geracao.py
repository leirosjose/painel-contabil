# ---------------------------------------------------------------------------
# geracao.py — geracoes do raw: guarda, manifesto, publicacao, detector
# ---------------------------------------------------------------------------
"""Cada carga de uma tabela do raw e uma GERACAO carimbada por linha. So a
geracao PUBLICADA e autoritativa de cada fatia aparece em raw.<tabela> (VIEW).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd
from psycopg2.extras import execute_values

from config import PG_SCHEMA_RAW, PG_SCHEMA_RAW_HIST
from load import _add_missing_columns, _ensure_table, _sanitize_binary_cols, _upsert
from raw_escopo import (EMPRESA_GLOBAL, FATIA_SEM, GLOBAIS, IMPRESSAO_VAZIA, Pedido, empresas,
                        fatias, impressoes)

LEGACY = 0              # historico gravado antes da geracao por linha
# Limiares. TABELA 5%: bem acima da perda natural entre dois snapshots. FATIA 30%:
# o snapshot ruim mais provavel e o backup no meio de um reprocessamento, que tira
# uma FRACAO do mes; o reprocessamento completo mantem a contagem.
# Falso positivo aqui e alto e recuperavel (exit 3 + override); falso negativo e
# mes parcial na tela. Nao mexer sem refazer a medida.
LIMIAR_FATIA = 0.30
PISO_FATIA = 20
LIMIAR_TABELA = 0.05


@dataclass(frozen=True)
class Anterior:
    generation_id: int
    linhas: int
    impressao: str | None


@dataclass
class Veredito:
    tabela_rejeitada: bool
    motivo: str | None
    fatias_rejeitadas: dict = field(default_factory=dict)
    fatias_liberadas: set = field(default_factory=set)
    total_anterior: int = 0
    total_atual: int = 0


def aceita(aceitas: frozenset, tabela: str, codi_emp: int, fatia: int) -> bool:
    """Override explicito: 'tabela', 'tabela:emp' ou 'tabela:emp:fatia'."""
    return (tabela in aceitas or f"{tabela}:{codi_emp}" in aceitas
            or f"{tabela}:{codi_emp}:{fatia}" in aceitas)


def avalia_encolhimento(tabela: str, pedido: Pedido, anterior: dict, atual: dict,
                        aceitas: frozenset = frozenset()) -> Veredito:
    """Compara a geracao nova com a vigente DENTRO do pedido.

    anterior: (emp, fatia) -> Anterior da geracao vigente
    atual:    (emp, fatia) -> linhas da geracao nova (fatia ausente = 0 linhas)
    """
    chaves = {k for k in anterior if pedido.cobre(*k)} | set(atual)
    tot_ant = sum(anterior[k].linhas for k in chaves if k in anterior)
    tot_atu = sum(atual.get(k, 0) for k in chaves)
    v = Veredito(False, None, total_anterior=tot_ant, total_atual=tot_atu)
    for k in sorted(chaves):
        ant = anterior.get(k)
        if ant is None or ant.generation_id == LEGACY or ant.linhas < PISO_FATIA:
            continue
        # (ant - atual) / ant, nunca 1 - atual/ant: no float 1 - 70/100 passa de 0.30.
        perda = (ant.linhas - atual.get(k, 0)) / ant.linhas
        if perda > LIMIAR_FATIA:
            if aceita(aceitas, tabela, *k):
                v.fatias_liberadas.add(k)
            else:
                v.fatias_rejeitadas[k] = f"perdeu {perda:.0%} ({ant.linhas} -> {atual.get(k, 0)})"
    perda_tab = (tot_ant - tot_atu) / tot_ant if tot_ant else 0.0
    if perda_tab > LIMIAR_TABELA and tabela not in aceitas:
        v.tabela_rejeitada = True
        v.motivo = f"pedido perdeu {perda_tab:.1%} ({tot_ant} -> {tot_atu})"
    return v


COLUNAS_TECNICAS = ("_generation_id", "_fatia")

DDL_OPS = (
    "CREATE SCHEMA IF NOT EXISTS ops",
    f"CREATE SCHEMA IF NOT EXISTS {PG_SCHEMA_RAW}",
    f"CREATE SCHEMA IF NOT EXISTS {PG_SCHEMA_RAW_HIST}",
    "CREATE SEQUENCE IF NOT EXISTS ops.raw_generation_seq START 1",
    """CREATE TABLE IF NOT EXISTS ops.schema_version (
           versao      text PRIMARY KEY,
           aplicada_em timestamptz NOT NULL DEFAULT now(),
           detalhe     text)""",
    """CREATE TABLE IF NOT EXISTS ops.raw_generation (
           generation_id bigint PRIMARY KEY,
           run_id        bigint,
           table_name    text NOT NULL,
           started_at    timestamptz NOT NULL DEFAULT now(),
           finished_at   timestamptz,
           status        text NOT NULL
                         CHECK (status IN ('LOADING', 'SUCCESS', 'REJECTED', 'PUBLISHED')),
           row_count     bigint,
           motivo        text)""",
    """CREATE TABLE IF NOT EXISTS ops.raw_generation_scope (
           generation_id   bigint NOT NULL REFERENCES ops.raw_generation (generation_id),
           table_name      text NOT NULL,
           scope_type      text NOT NULL CHECK (scope_type IN ('pedido', 'fatia')),
           codi_emp        bigint,
           scope_value     integer,
           scope_start     integer,
           scope_end       integer,
           authoritative   boolean NOT NULL DEFAULT false,
           row_count       bigint,
           prev_row_count  bigint,
           impressao       text,
           status          text NOT NULL,
           override_motivo text)""",
    """CREATE INDEX IF NOT EXISTS raw_generation_scope_fatia
           ON ops.raw_generation_scope (table_name, codi_emp, scope_value)
           WHERE scope_type = 'fatia'""",
    """CREATE TABLE IF NOT EXISTS ops.raw_slice_vigente (
           table_name    text    NOT NULL,
           codi_emp      bigint  NOT NULL,
           fatia         integer NOT NULL,
           generation_id bigint  NOT NULL REFERENCES ops.raw_generation (generation_id),
           row_count     bigint  NOT NULL,
           impressao     text,
           PRIMARY KEY (table_name, codi_emp, fatia))""",
    """INSERT INTO ops.raw_generation (generation_id, run_id, table_name, status, motivo)
       VALUES (0, NULL, '*', 'PUBLISHED',
               'LEGACY: historico gravado antes da geracao por linha (R2, 10/set/2026)')
       ON CONFLICT (generation_id) DO NOTHING""",
)


def garante_ops(cur) -> None:
    """Cria (idempotente) os schemas, o manifesto e a geracao LEGACY."""
    for s in DDL_OPS:
        cur.execute(s)


def sql_view(tabela: str, colunas: list[str]) -> str:
    """VIEW raw.<tabela> = so as linhas da geracao VIGENTE da fatia, sem as tecnicas."""
    sel = ", ".join(f'h."{c}"' for c in colunas)
    emp = str(EMPRESA_GLOBAL) if tabela in GLOBAIS else 'h."codi_emp"'
    return (f'CREATE OR REPLACE VIEW {PG_SCHEMA_RAW}."{tabela}" AS SELECT {sel} '
            f'FROM {PG_SCHEMA_RAW_HIST}."{tabela}" h '
            f"JOIN ops.raw_slice_vigente v ON v.table_name = '{tabela}' "
            f'AND v.codi_emp = {emp} AND v.fatia = h."_fatia" '
            f'AND v.generation_id = h."_generation_id"')


def garante_view(cur, tabela: str) -> None:
    """(Re)cria a VIEW com as colunas atuais do raw_hist.

    CREATE OR REPLACE VIEW so pode ACRESCENTAR coluna no fim -- e e o que o
    _add_missing_columns faz no raw_hist, entao a VIEW acompanha sem DROP.
    """
    cur.execute("SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                (PG_SCHEMA_RAW_HIST, tabela))
    cols = [c for (c,) in cur.fetchall() if c not in COLUNAS_TECNICAS]
    cur.execute(sql_view(tabela, cols))


@dataclass
class Resultado:
    tabela: str
    generation_id: int
    status: str                  # SUCCESS | REJECTED
    motivo: str | None
    pedido: Pedido
    fatias: pd.DataFrame         # codi_emp, fatia, linhas, impressao, destino, anterior, override
    linhas_gravadas: int
    dedup: int = 0


_COLS_FATIAS = ["codi_emp", "fatia", "linhas", "impressao", "destino", "anterior", "override"]


def nova_geracao(conn, run_id: int, tabela: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT nextval('ops.raw_generation_seq')")
        g = int(cur.fetchone()[0])
        cur.execute("INSERT INTO ops.raw_generation (generation_id, run_id, table_name, status) "
                    "VALUES (%s, %s, %s, 'LOADING')", (g, run_id, tabela))
    conn.commit()
    return g


def estado_vigente(conn, tabela: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT codi_emp, fatia, generation_id, row_count, impressao "
                    "FROM ops.raw_slice_vigente WHERE table_name = %s", (tabela,))
        return {(int(e), int(f)): Anterior(int(g), int(n), i) for e, f, g, n, i in cur.fetchall()}


def grava_geracao(conn, tabela: str, df: pd.DataFrame, chaves: list, gen_id: int,
                  fat: pd.Series) -> int:
    """INSERT das linhas da geracao em raw_hist. Append-only: so conflita dentro
    da propria geracao (chave natural + _generation_id)."""
    if df.empty:
        return 0
    d = df.copy()
    d["_generation_id"] = gen_id
    d["_fatia"] = fat.to_numpy()
    k = list(chaves) + ["_generation_id"]
    _ensure_table(conn, PG_SCHEMA_RAW_HIST, tabela, d, k)
    _add_missing_columns(conn, PG_SCHEMA_RAW_HIST, tabela, d)
    n = _upsert(conn, PG_SCHEMA_RAW_HIST, tabela, d, k)
    with conn.cursor() as cur:
        garante_view(cur, tabela)
    conn.commit()
    return n


def _fora_do_pedido_sql(tabela: str, p: Pedido) -> tuple[str, list]:
    emp = str(EMPRESA_GLOBAL) if tabela in GLOBAIS else '"codi_emp"'
    conds, par = [], []
    if p.codi_emp is not None:
        conds.append(f"{emp} <> %s")
        par.append(p.codi_emp)
    if p.empresas is not None:
        conds.append(f"NOT ({emp} = ANY(%s))")
        par.append(sorted(p.empresas))
    if p.fatia_min is not None:
        conds.append('"_fatia" < %s')
        par.append(p.fatia_min)
    if p.fatia_max is not None:
        conds.append('"_fatia" > %s')
        par.append(p.fatia_max)
    return " OR ".join(conds), par


def _valida_gravacao(conn, tabela: str, chaves: list, gen_id: int, n_gravadas: int,
                     p: Pedido) -> list[str]:
    """Contagem, integridade e janela -- sobre o que FICOU no raw_hist."""
    alvo = f'{PG_SCHEMA_RAW_HIST}."{tabela}"'
    probs = []
    with conn.cursor() as cur:
        cur.execute(f'SELECT count(*) FROM {alvo} WHERE "_generation_id" = %s', (gen_id,))
        n = cur.fetchone()[0]
        if n != n_gravadas:
            probs.append(f"contagem: gravou {n_gravadas}, raw_hist tem {n}")
        if chaves:
            nulas = " OR ".join(f'"{c}" IS NULL' for c in chaves)
            cur.execute(f'SELECT count(*) FROM {alvo} WHERE "_generation_id" = %s AND ({nulas})', (gen_id,))
            k = cur.fetchone()[0]
            if k:
                probs.append(f"integridade: {k} linha(s) com chave NULL")
        cur.execute(f'SELECT count(*) FROM {alvo} WHERE "_generation_id" = %s AND "_fatia" = %s',
                    (gen_id, FATIA_SEM))
        s = cur.fetchone()[0]
        if s:
            probs.append(f"integridade: {s} linha(s) sem fatia (data ilegivel ou parcela sem nota)")
        fora, par = _fora_do_pedido_sql(tabela, p)
        if fora:
            cur.execute(f'SELECT count(*) FROM {alvo} WHERE "_generation_id" = %s '
                        f'AND "_fatia" <> %s AND ({fora})', (gen_id, FATIA_SEM, *par))
            f = cur.fetchone()[0]
            if f:
                probs.append(f"janela: {f} linha(s) fora do pedido")
    return probs


def marca_status(conn, gen_id: int, status: str, motivo: str | None, n: int) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE ops.raw_generation SET status = %s, motivo = %s, row_count = %s, "
                    "finished_at = now() WHERE generation_id = %s", (status, motivo, n, gen_id))
    conn.commit()


def processa_tabela(conn, run_id: int, tabela: str, df: pd.DataFrame, chaves: list, ped: Pedido,
                    notas: pd.DataFrame | None = None, aceitas: frozenset = frozenset(),
                    motivo_override: str | None = None) -> Resultado:
    """Uma geracao de uma tabela: fatias -> guarda -> grava -> valida -> SUCCESS|REJECTED.

    A guarda e aritmetica sobre contagens em memoria, entao decide o destino de
    cada fatia ANTES do INSERT; nada fica vigente antes do publica().
    """
    gen = nova_geracao(conn, run_id, tabela)
    d = _sanitize_binary_cols(df) if not df.empty else df
    fat = fatias(tabela, d, notas)
    com = fat.notna()
    imp = impressoes(tabela, d[com], fat[com])
    atual = {(int(e), int(f)): (int(n), s) for e, f, n, s in imp.itertuples(index=False)}
    anterior = {k: a for k, a in estado_vigente(conn, tabela).items() if ped.cobre(*k)}
    ver = avalia_encolhimento(tabela, ped, anterior, {k: v[0] for k, v in atual.items()}, aceitas)
    linhas = []
    for k in sorted(set(anterior) | set(atual)):
        n, s = atual.get(k, (0, IMPRESSAO_VAZIA))
        a = anterior.get(k)
        if ver.tabela_rejeitada or k in ver.fatias_rejeitadas:
            destino = "REJECTED"
        elif a is not None and a.linhas == n and a.impressao == s:
            destino = "UNCHANGED"
        else:
            destino = "PUBLISHED"
        linhas.append((k[0], k[1], n, s, destino, a.linhas if a else 0,
                       motivo_override if k in ver.fatias_liberadas else None))
    fat_df = pd.DataFrame(linhas, columns=_COLS_FATIAS)
    grava = fat_df.loc[fat_df["destino"] != "UNCHANGED", ["codi_emp", "fatia"]]
    fat_grav = fat.fillna(FATIA_SEM).astype("int64")
    chave = pd.MultiIndex.from_arrays([empresas(tabela, d).to_numpy(), fat_grav.to_numpy()])
    mascara = (~com.to_numpy()) | chave.isin(list(map(tuple, grava.to_numpy().tolist())))
    a_gravar = d[mascara]
    n = grava_geracao(conn, tabela, a_gravar, chaves, gen, fat_grav[mascara])
    probs = _valida_gravacao(conn, tabela, chaves, gen, n, ped) if n else []
    if ver.tabela_rejeitada:
        probs.append(ver.motivo)
    status = "REJECTED" if probs else "SUCCESS"
    motivo = "; ".join(probs) or None
    marca_status(conn, gen, status, motivo, n)
    return Resultado(tabela, gen, status, motivo, ped, fat_df, n, dedup=len(a_gravar) - n)


def publica(conn, resultados: list) -> None:
    """UMA transacao: toda fatia PUBLISHED de geracao SUCCESS vira vigente.

    Fatia UNCHANGED nao gera linha de escopo -- a linha 'pedido' ja registra a
    cobertura, e gravar ~150k linhas inalteradas por run completo so incharia o
    manifesto.
    """
    with conn.cursor() as cur:
        for r in resultados:
            p = r.pedido
            ok = r.status == "SUCCESS"
            cur.execute(
                "INSERT INTO ops.raw_generation_scope (generation_id, table_name, scope_type, "
                "codi_emp, scope_start, scope_end, authoritative, row_count, status) "
                "VALUES (%s, %s, 'pedido', %s, %s, %s, %s, %s, %s)",
                (r.generation_id, r.tabela, p.codi_emp, p.fatia_min, p.fatia_max, ok,
                 r.linhas_gravadas, "PUBLISHED" if ok else "REJECTED"))
            escopo, vigentes = [], []
            for e, f, n, s, destino, ant, ovr in r.fatias.itertuples(index=False):
                dest = destino if ok else "REJECTED"
                if dest == "UNCHANGED":
                    continue
                aut = dest == "PUBLISHED"
                escopo.append((r.generation_id, r.tabela, "fatia", int(e), int(f), aut,
                               int(n), int(ant), s, dest, ovr))
                if aut:
                    vigentes.append((r.tabela, int(e), int(f), r.generation_id, int(n), s))
            if escopo:
                execute_values(cur,
                    "INSERT INTO ops.raw_generation_scope (generation_id, table_name, scope_type, "
                    "codi_emp, scope_value, authoritative, row_count, prev_row_count, impressao, "
                    "status, override_motivo) VALUES %s", escopo, page_size=1000)
            if vigentes:
                execute_values(cur,
                    "INSERT INTO ops.raw_slice_vigente (table_name, codi_emp, fatia, generation_id, "
                    "row_count, impressao) VALUES %s ON CONFLICT (table_name, codi_emp, fatia) "
                    "DO UPDATE SET generation_id = EXCLUDED.generation_id, "
                    "row_count = EXCLUDED.row_count, impressao = EXCLUDED.impressao",
                    vigentes, page_size=1000)
            if ok:
                cur.execute("UPDATE ops.raw_generation SET status = 'PUBLISHED', finished_at = now() "
                            "WHERE generation_id = %s", (r.generation_id,))
    conn.commit()


def verifica_publicacao(conn, resultados: list) -> list[str]:
    """Pra cada fatia publicada: mapa -> (geracao, linhas) e raw_hist com as mesmas linhas."""
    probs = []
    with conn.cursor() as cur:
        for r in resultados:
            if r.status != "SUCCESS":
                continue
            pub = r.fatias[r.fatias["destino"] == "PUBLISHED"]
            if pub.empty:
                continue
            emp = str(EMPRESA_GLOBAL) if r.tabela in GLOBAIS else '"codi_emp"'
            cur.execute(f'SELECT {emp}, "_fatia", count(*) FROM {PG_SCHEMA_RAW_HIST}."{r.tabela}" '
                        'WHERE "_generation_id" = %s GROUP BY 1, 2', (r.generation_id,))
            hist = {(int(e), int(f)): int(n) for e, f, n in cur.fetchall()}
            cur.execute("SELECT codi_emp, fatia, generation_id, row_count FROM ops.raw_slice_vigente "
                        "WHERE table_name = %s", (r.tabela,))
            mapa = {(int(e), int(f)): (int(g), int(n)) for e, f, g, n in cur.fetchall()}
            for e, f, n in pub[["codi_emp", "fatia", "linhas"]].itertuples(index=False):
                k = (int(e), int(f))
                if mapa.get(k) != (r.generation_id, int(n)):
                    probs.append(f"{r.tabela} {k}: mapa {mapa.get(k)} != ({r.generation_id}, {n})")
                elif hist.get(k, 0) != int(n):
                    probs.append(f"{r.tabela} {k}: raw_hist tem {hist.get(k, 0)}, a geracao diz {n}")
    return probs


def detecta_rapido(conn) -> tuple[list[str], list[str]]:
    """Barato, roda no inicio de todo --skip-extract."""
    probs, avisos = [], []
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('ops.raw_slice_vigente')")
        if cur.fetchone()[0] is None:
            return ["manifesto ausente: banco nao migrado pro layout de geracao"], []
        cur.execute("SELECT v.table_name, v.generation_id, g.status FROM ops.raw_slice_vigente v "
                    "LEFT JOIN ops.raw_generation g USING (generation_id) "
                    "WHERE g.status IS DISTINCT FROM 'PUBLISHED' LIMIT 20")
        for t, gid, st in cur.fetchall():
            probs.append(f"fatia vigente de {t} aponta pra geracao {gid} com status {st}")
        cur.execute("SELECT generation_id, table_name, status, started_at FROM ops.raw_generation "
                    "WHERE status IN ('LOADING', 'SUCCESS') ORDER BY 1")
        for gid, t, st, ini in cur.fetchall():
            avisos.append(f"geracao {gid} de {t} parada em {st} desde {ini} (run morto?) -- nunca e lida")
    return probs, avisos


def confere_mapa_completo(conn) -> list[str]:
    """Caro (varre o raw_hist): toda fatia do mapa tem as linhas que diz ter."""
    probs = []
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT table_name FROM ops.raw_slice_vigente")
        for (t,) in cur.fetchall():
            emp = str(EMPRESA_GLOBAL) if t in GLOBAIS else '"codi_emp"'
            cur.execute(f'SELECT {emp}, "_fatia", "_generation_id", count(*) '
                        f'FROM {PG_SCHEMA_RAW_HIST}."{t}" GROUP BY 1, 2, 3')
            hist = {(int(e), int(f), int(g)): int(n) for e, f, g, n in cur.fetchall()}
            cur.execute("SELECT codi_emp, fatia, generation_id, row_count FROM ops.raw_slice_vigente "
                        "WHERE table_name = %s", (t,))
            for e, f, g, n in cur.fetchall():
                achado = hist.get((int(e), int(f), int(g)), 0)
                if achado != int(n):
                    probs.append(f"{t} ({e}, {f}): mapa diz {n} linha(s) da geracao {g}, raw_hist tem {achado}")
    return probs
