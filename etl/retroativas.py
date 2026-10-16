# ---------------------------------------------------------------------------
# retroativas.py -- "Alteracoes retroativas"
# ---------------------------------------------------------------------------
"""Registra quando um numero de um MES JA FECHADO muda por causa de um run.

Como funciona:
  - dentro do run, ANTES de gravar o mart e DEPOIS, o mesmo leitor calcula os
    6 numeros (NFe, NFS-e, Outras receitas, Total, Impostos, Folha) LENDO O
    BANCO e passando pelo consumer real (etl.metrics.series_mensais);
  - mes fechado = competencia anterior ao mes da carga; diferenca menor que
    R$ 1,00 (em centavos, cada lado arredondado antes) e ignorada;
  - a CAUSA sai do manifesto de geracao do raw (ops.raw_generation*): fatia
    que o run publicou e que ja tinha sido pedida antes = "mudou no Dominio";
    fatia nunca pedida = "carga nova"; nada publicado = "mudou o calculo"
    (commit diferente) ou "inesperado" (mesmo commit);
  - a tabela ops.alteracoes_retroativas e APPEND-ONLY: so INSERT com
    ON CONFLICT DO NOTHING, nunca regrava nem apaga linha.

O passo inteiro e BEST-EFFORT: as funcoes de orquestracao (captura_antes /
fecha_comparacao) nunca levantam -- falha vira chave no report e ALERTA no
resumo, o run segue e o status dele nao muda. Este modulo NAO importa etl.main
(o main e quem importa este modulo; a pagina do app le so o banco, sem importa-lo).
"""
from __future__ import annotations

import re
import sys
import warnings
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

NUMEROS = ("NFe", "NFS-e", "Outras receitas", "Total", "Impostos", "Folha")
NUMEROS_FATURAMENTO = ("NFe", "NFS-e", "Outras receitas")
CAUSA_DOMINIO = "mudou no Dominio"
CAUSA_CARGA_NOVA = "carga nova"
CAUSA_MISTA = "mudou no Dominio + carga nova"
CAUSA_CALCULO = "mudou o calculo"
CAUSA_INESPERADA = "inesperado"
CAUSAS = (CAUSA_DOMINIO, CAUSA_CARGA_NOVA, CAUSA_MISTA, CAUSA_CALCULO, CAUSA_INESPERADA)
LIMIAR_CENTAVOS = 100                      # R$ 1,00

MSG_PULADA_SEM_RUN = "sem run anterior em ops.controle_runs"
MSG_INICIO_ANTES = "retroativas: lendo o mart antes da carga"
MSG_FIM_DEPOIS = "retroativas: comparando com o mart depois da carga"

RAIZ = Path(__file__).resolve().parent.parent

DDL_ALTERACOES = (
    """CREATE TABLE IF NOT EXISTS ops.alteracoes_retroativas (
           id               bigserial PRIMARY KEY,
           run_id           bigint NOT NULL,
           detectado_em     timestamptz NOT NULL DEFAULT now(),
           codi_emp         bigint NOT NULL,
           competencia      text   NOT NULL,
           numero           text   NOT NULL CHECK (numero IN
                              ('NFe','NFS-e','Outras receitas','Total','Impostos','Folha')),
           valor_antes      numeric(18,2),
           valor_depois     numeric(18,2),
           diferenca        numeric(18,2) NOT NULL,
           causa            text   NOT NULL CHECK (causa IN
                              ('mudou no Dominio','carga nova','mudou no Dominio + carga nova',
                               'mudou o calculo','inesperado')),
           parte_dominio    numeric(18,2),
           parte_carga_nova numeric(18,2),
           fontes           text,
           janela_calculo   text   NOT NULL,
           git_commit       text,
           git_commit_r0    text,
           UNIQUE (run_id, codi_emp, competencia, numero))""",
    """CREATE INDEX IF NOT EXISTS alteracoes_retroativas_pagina
           ON ops.alteracoes_retroativas (causa, detectado_em DESC)""",
)


def garante_alteracoes(cur) -> None:
    """DDL aditivo e idempotente da tabela + versao em ops.schema_version.

    Chamado pela guarda de boot (load.ensure_schemas) DEPOIS do garante_ops,
    que cria ops.schema_version. Sem FK pra ops.controle_runs: essa tabela so
    nasce no _abre_run, depois da guarda de boot.
    """
    from config import VERSAO_ALTERACOES_RETROATIVAS

    for ddl in DDL_ALTERACOES:
        cur.execute(ddl)
    cur.execute("INSERT INTO ops.schema_version (versao, detalhe) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (VERSAO_ALTERACOES_RETROATIVAS,
                 "ops.alteracoes_retroativas: aditivo, sem janela de manutencao"))


# ---------------------------------------------------------------------------
# Numeros em centavos, mes da carga, comparacao
# ---------------------------------------------------------------------------

def centavos(v) -> int | None:
    """Valor -> centavos inteiros, meio-centavo pra longe do zero (como o
    numeric do Postgres). None/NaN -> None. Float binario nao serve: round(1.005, 2) = 1.0."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:                                   # NaN
        return None
    return int((Decimal(str(f)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def mes_da_carga(iniciado_em: datetime) -> str:
    """'YYYY-MM' do mes em que o run comecou (hora local)."""
    return f"{iniciado_em.year:04d}-{iniciado_em.month:02d}"


@dataclass(frozen=True)
class Diferenca:
    codi_emp: int
    competencia: str
    numero: str
    antes_c: int | None
    depois_c: int | None

    @property
    def dif_c(self) -> int:
        return (self.depois_c or 0) - (self.antes_c or 0)


def compara(antes: dict, depois: dict, mes_carga: str) -> list[Diferenca]:
    """Diferencas >= R$ 1,00 em competencia anterior ao mes da carga.

    `antes`/`depois`: {(codi_emp, competencia, numero): valor}. Cada lado e
    arredondado a 2 casas ANTES da subtracao, que roda em centavos inteiros
    (10,01 - 9,01 em float da 0,9999999999999996). Lado ausente vale 0 na conta e
    fica None no registro.
    """
    out = []
    for k in sorted(set(antes) | set(depois), key=lambda x: (int(x[0]), x[1], x[2])):
        emp, comp, num = k
        if comp >= mes_carga:
            continue
        a, d = centavos(antes.get(k)), centavos(depois.get(k))
        if abs((d or 0) - (a or 0)) < LIMIAR_CENTAVOS:
            continue
        out.append(Diferenca(int(emp), comp, num, a, d))
    return out


# ---------------------------------------------------------------------------
# Fontes de cada numero e causa
# ---------------------------------------------------------------------------

def _fatia(comp: str) -> int:
    return int(comp[:4]) * 100 + int(comp[5:7])


def _mais(fatia: int, n: int) -> int:
    y, m = divmod(fatia, 100)
    m += n
    return (y + (m - 1) // 12) * 100 + (m - 1) % 12 + 1


def fontes(numero: str, emp: int, comp: str) -> list[tuple]:
    """Fatias do raw que alimentam o numero: (tabela, codi_emp, fatia).

    Mapa escrito a mao das dependencias do metrics/transform:
    numero novo com fonte nova exige atualizar aqui, senao a causa cai em
    'inesperado' -- o sinal alto certo, nao silencio.
    """
    f = _fatia(comp)
    if numero == "NFe":
        # NFe e liquido de devolucao de venda,
        # que vem de efentradas (mes da entrada).
        return [("efsaidas", emp, f), ("efentradas", emp, f)]
    if numero == "NFS-e":
        return [("efservicos", emp, f)]
    if numero == "Outras receitas":
        return [("efimob_pagamentos", emp, f)]
    if numero == "Total":
        return fontes("NFe", emp, comp) + fontes("NFS-e", emp, comp) + fontes("Outras receitas", emp, comp)
    if numero == "Impostos":
        return ([("efsdoimp", emp, f), ("efservicos", emp, f), ("efservicospar", emp, f)]
                + [("ctlancto", emp, _mais(f, i)) for i in range(4)]     # eixo do trimestre do fallback
                + [("ctcontas", emp, 0)])
    if numero == "Folha":
        return ([(t, emp, f) for t in ("fomovto", "foguiainss", "fofgtsfilial", "fobases", "forescisoes")]
                + [("foguiagrfc", emp, f), ("foguiagrfc", emp, _mais(f, 1))]  # GRFC pelo mes da demissao
                + [("foeventos", emp, 0), ("foeventosdef", 0, 0), ("foclassificacoes_eventos", 0, 0)])
    raise ValueError(f"numero desconhecido: {numero!r}")


TABELAS_DAS_FONTES = tuple(sorted({t for n in NUMEROS for t, _, _ in fontes(n, 1, "2025-01")}))


@dataclass(frozen=True)
class Manifesto:
    """Recorte do manifesto de geracao pra classificar causa.

    pub:     (tabela, emp, fatia) -> run_ids que PUBLICARAM a fatia (apos o R0 mais antigo)
    pedidos: (run_id, tabela, codi_emp|None, ini|None, fim|None) -- o que cada run pediu a origem
    legacy:  (tabela, emp, fatia) presentes na geracao 0 (historico anterior a geracao por linha)
    """
    pub: dict
    pedidos: list
    legacy: frozenset


@dataclass(frozen=True)
class Causa:
    causa: str
    parte_dominio_c: int | None
    parte_carga_nova_c: int | None
    fontes: str


@dataclass(frozen=True)
class RunInfo:
    run_id: int
    empresa: str
    inicio: str
    fim: str
    git_commit: str | None


def r0_do_par(runs: list, emp: int, comp: str):
    """R0 = maior run 'ok' (a lista ja vem filtrada) que cobre (emp, competencia)."""
    cobre = [r for r in runs
             if r.empresa in ("all", str(emp)) and r.inicio <= comp <= r.fim]
    return max(cobre, key=lambda r: r.run_id) if cobre else None


def _runs_apos(runs, r0: int) -> list:
    return sorted(r for r in (runs or ()) if r > r0)


def _coberta(chave: tuple, primeiro_run: int, m: Manifesto) -> bool:
    """A origem ja tinha sido PEDIDA pra essa fatia antes do run que a publicou?"""
    if chave in m.legacy:
        return True
    tabela, emp, fatia = chave
    for run, t, e, ini, fim in m.pedidos:
        if (run < primeiro_run and t == tabela and (e is None or e == emp)
                and (ini is None or fatia >= ini) and (fim is None or fatia <= fim)):
            return True
    return False


def _txt_fontes(chaves: list, m: Manifesto, r0: int) -> str:
    partes = []
    for t, e, f in chaves[:6]:
        runs = _runs_apos(m.pub.get((t, e, f)), r0)
        partes.append(f"{t} {e} {f} (run {runs[0]})")
    if len(chaves) > 6:
        partes.append(f"+{len(chaves) - 6}")
    return "; ".join(partes)


def _causa_simples(numero, emp, comp, m: Manifesto, r0: int, commit_atual, commit_r0) -> Causa:
    fs = fontes(numero, emp, comp)
    hit = [k for k in fs if _runs_apos(m.pub.get(k), r0)]
    sufixo = ""
    if not hit:
        # ramo (d): os portoes do metrics sao por JANELA -- fatia das mesmas tabelas
        # publicada em OUTRO mes do mesmo ano da empresa tambem move este mes.
        ano = int(comp[:4])
        tabs = {t for t, e, f in fs if e == emp and f != 0}
        hit = sorted(k for k in m.pub
                     if k[0] in tabs and k[1] == emp and k[2] != 0 and k[2] // 100 == ano
                     and _runs_apos(m.pub[k], r0))
        sufixo = " (outro mes da janela)"
    if hit:
        cob = [_coberta(k, _runs_apos(m.pub[k], r0)[0], m) for k in hit]
        if all(cob):
            causa = CAUSA_DOMINIO
        elif not any(cob):
            causa = CAUSA_CARGA_NOVA
        else:
            causa = CAUSA_MISTA
        return Causa(causa, None, None, _txt_fontes(hit, m, r0) + sufixo)
    if commit_atual is not None and (commit_r0 is None or commit_atual != commit_r0):
        fonte = ("commit anterior desconhecido" if commit_r0 is None
                 else f"commit {commit_r0} -> {commit_atual}")
        return Causa(CAUSA_CALCULO, None, None, fonte)
    return Causa(CAUSA_INESPERADA, None, None, "nenhuma fatia publicada e mesmo commit")


_PRIORIDADE = (CAUSA_DOMINIO, CAUSA_CARGA_NOVA, CAUSA_CALCULO, CAUSA_INESPERADA)


def classifica(numero, emp, comp, dif_c, manifesto: Manifesto, r0, commit_atual, commit_r0,
               componentes: dict | None = None) -> Causa:
    """Causa de UMA diferenca. `r0` = run_id do R0 do par (0 = nenhum).

    Total com `componentes` ({numero: diferenca em centavos}) e classificado
    pelas partes que de fato mudaram: Dominio + carga nova viram
    'mudou no Dominio + carga nova' com as duas partes em centavos.
    """
    r0 = r0 or 0
    if numero != "Total" or componentes is None:
        return _causa_simples(numero, emp, comp, manifesto, r0, commit_atual, commit_r0)
    partes = [(n, c, _causa_simples(n, emp, comp, manifesto, r0, commit_atual, commit_r0))
              for n, c in componentes.items() if n in NUMEROS_FATURAMENTO and c]
    if not partes:
        return _causa_simples(numero, emp, comp, manifesto, r0, commit_atual, commit_r0)
    causas = {p[2].causa for p in partes}
    fontes_txt = "; ".join(f"{n}: {c.fontes}" for n, _, c in partes)
    if {CAUSA_DOMINIO, CAUSA_CARGA_NOVA} <= causas or CAUSA_MISTA in causas:
        dom = sum(v for _, v, c in partes if c.causa == CAUSA_DOMINIO)
        nova = sum(v for _, v, c in partes if c.causa == CAUSA_CARGA_NOVA)
        return Causa(CAUSA_MISTA, dom, nova, fontes_txt)
    causa = next(c for c in _PRIORIDADE if c in causas)
    return Causa(causa, None, None, fontes_txt)


# ---------------------------------------------------------------------------
# Commit do checkout SEM subprocess
# ---------------------------------------------------------------------------

_SHA = re.compile(r"[0-9a-f]{40}")


def git_commit_curto(raiz) -> str | None:
    """SHA curto (7) do HEAD lendo .git direto: HEAD -> ref -> refs/heads/<x>
    ou packed-refs. Sem .git, ref ausente ou qualquer erro -> None (nunca
    derruba o run). Sem sufixo -dirty."""
    try:
        g = Path(raiz) / ".git"
        if not g.is_dir():
            return None
        head = (g / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head[4:].strip()
            arq = g / ref
            if arq.is_file():
                sha = arq.read_text(encoding="utf-8").strip()
            else:
                sha = ""
                packed = g / "packed-refs"
                if packed.is_file():
                    for linha in packed.read_text(encoding="utf-8").splitlines():
                        if linha and linha[0] not in "#^":
                            valor, _, nome = linha.partition(" ")
                            if nome.strip() == ref:
                                sha = valor
                                break
        else:
            sha = head
        return sha[:7] if _SHA.fullmatch(sha) else None
    except Exception:  # noqa: BLE001 -- commit e metadado, nunca derruba o run
        return None


# ---------------------------------------------------------------------------
# Leitura do banco: runs, manifesto, numeros pelo consumer real
# ---------------------------------------------------------------------------

def _tem_coluna(cur, tabela: str, coluna: str) -> bool:
    esquema, nome = tabela.split(".")
    cur.execute("SELECT 1 FROM information_schema.columns WHERE table_schema = %s "
                "AND table_name = %s AND column_name = %s", (esquema, nome, coluna))
    return cur.fetchone() is not None


def le_runs_ok(conn, run_id_atual: int) -> list[RunInfo]:
    """Runs 'ok' anteriores ao atual (git_commit None se a coluna ainda nao existe)."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('ops.controle_runs')")
        if cur.fetchone()[0] is None:
            return []
        col = "git_commit" if _tem_coluna(cur, "ops.controle_runs", "git_commit") else "NULL"
        cur.execute(f"SELECT run_id, empresa, inicio, fim, {col} FROM ops.controle_runs "
                    "WHERE status = 'ok' AND run_id < %s ORDER BY run_id", (run_id_atual,))
        return [RunInfo(int(r[0]), str(r[1]), r[2] or "", r[3] or "", r[4]) for r in cur.fetchall()]


def le_manifesto(conn, r0_min: int, run_id: int, emps) -> Manifesto:
    """Fatias PUBLISHED em (r0_min, run_id], todos os pedidos PUBLISHED e a geracao LEGACY."""
    emps = sorted({int(e) for e in emps} | {0})
    pub: dict = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT g.run_id, s.table_name, s.codi_emp, s.scope_value "
            "FROM ops.raw_generation_scope s JOIN ops.raw_generation g USING (generation_id) "
            "WHERE s.scope_type = 'fatia' AND s.status = 'PUBLISHED' "
            "AND g.run_id > %s AND g.run_id <= %s AND s.table_name = ANY(%s) AND s.codi_emp = ANY(%s)",
            (r0_min, run_id, list(TABELAS_DAS_FONTES), emps))
        for run, tab, emp, fat in cur.fetchall():
            pub.setdefault((tab, int(emp), int(fat)), []).append(int(run))
        cur.execute(
            "SELECT g.run_id, s.table_name, s.codi_emp, s.scope_start, s.scope_end "
            "FROM ops.raw_generation_scope s JOIN ops.raw_generation g USING (generation_id) "
            "WHERE s.scope_type = 'pedido' AND s.status = 'PUBLISHED' AND g.run_id IS NOT NULL "
            "AND s.table_name = ANY(%s)", (list(TABELAS_DAS_FONTES),))
        pedidos = [(int(r), t, None if e is None else int(e), None if i is None else int(i),
                    None if f is None else int(f)) for r, t, e, i, f in cur.fetchall()]
        cur.execute(
            "SELECT s.table_name, s.codi_emp, s.scope_value FROM ops.raw_generation_scope s "
            "WHERE s.generation_id = 0 AND s.scope_type = 'fatia' "
            "AND s.table_name = ANY(%s) AND s.codi_emp = ANY(%s)",
            (list(TABELAS_DAS_FONTES), emps))
        legacy = frozenset((t, int(e), int(f)) for t, e, f in cur.fetchall())
    return Manifesto({k: sorted(v) for k, v in pub.items()}, pedidos, legacy)


def _importa_consumer():
    """etl.metrics / etl.queries sao package-style; o main roda de dentro de etl/."""
    raiz = str(RAIZ)
    if raiz not in sys.path:
        sys.path.insert(0, raiz)
    from etl.metrics import series_mensais
    from etl.queries import params_passivo_ir, SQL_PASSIVO_IRPJ_CSLL
    return series_mensais, params_passivo_ir, SQL_PASSIVO_IRPJ_CSLL


def _mes_anterior(mes: str) -> str:
    y, m = int(mes[:4]), int(mes[5:7])
    return f"{y - 1:04d}-12" if m == 1 else f"{y:04d}-{m - 1:02d}"


def _cents_col(serie):
    import pandas as pd
    return pd.to_numeric(serie, errors="coerce").fillna(0.0).map(lambda v: centavos(v) or 0)


def le_numeros(conn, emps, inicio: str, fim: str, mes_carga: str) -> dict:
    """Os 6 numeros por (codi_emp, competencia) LIDOS DO BANCO, pelo consumer real.

    Janela de calculo = ANO-CALENDARIO, por empresa, so dos anos que tocam
    [inicio, min(fim, mes anterior a carga)]. Impostos = S["imp_pm"] e Folha =
    S["folha_pm"] + S["enc_pm"] (NULL-aware) de etl.metrics.series_mensais com os
    MESMOS DataFrames que o render_dashboard do app monta. NFe/NFS-e/Outras =
    soma dos valores do mart (cada linha em centavos); Total = as tres.
    Devolve {(emp, competencia, numero): float}; par/numero sem dado fica de fora.
    """
    import pandas as pd

    emps = sorted({int(e) for e in emps})
    fim_ef = min(fim, _mes_anterior(mes_carga))
    if not emps or inicio > fim_ef:
        return {}
    series_mensais, params_passivo_ir, SQL_IR = _importa_consumer()
    y0, y1 = int(inicio[:4]), int(fim_ef[:4])
    lo, hi = f"{y0}-01", f"{y1}-12"

    def q(sql, params):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")           # pandas reclama de conn psycopg2 crua
            return pd.read_sql(sql, conn, params=params)

    def existe(tab):
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (f"mart.{tab}",))
            return cur.fetchone()[0] is not None

    def mart(tab):
        if not existe(tab):
            return {}
        df = q(f"SELECT * FROM mart.{tab} WHERE codi_emp = ANY(%s) AND competencia >= %s "
               "AND competencia <= %s", (emps, lo, hi))
        if df.empty:
            return {}
        df["codi_emp"] = df["codi_emp"].astype("int64")
        return dict(tuple(df.groupby("codi_emp")))

    M = {t: mart(t) for t in ("faturamento_mensal", "impostos_mensais", "impostos_destacados_mensais",
                              "folha_mensal", "funcionarios_mensal", "encargos_mensais",
                              "dre_mensal", "folha_rubricas_mensais")}
    for e, df in M["faturamento_mensal"].items():          # TEXT vira numero so na leitura
        for col in ("valo_saidas", "valo_servicos", "valo_imobiliario"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
    if existe("balancete_mensal"):                          # ausente (banco novo/parcial) degrada, nunca levanta
        ir = q(SQL_IR, params_passivo_ir(emps, lo, hi))
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM mart.balancete_mensal LIMIT 0")
            bal_cols = pd.DataFrame(columns=[d[0] for d in cur.description])
    else:
        ir = pd.DataFrame(columns=["codi_emp", "competencia", "clas_cta", "nome_cta", "valo_deb"])
        bal_cols = pd.DataFrame()
    ir_por_emp = dict(tuple(ir.groupby("codi_emp"))) if not ir.empty else {}

    vazio = pd.DataFrame()
    out: dict = {}
    for emp in emps:
        for ano in range(y0, y1 + 1):
            de, ate = f"{ano}-01", f"{ano}-12"
            periods = [str(p) for p in pd.period_range(de, ate, freq="M")]

            def fatia(tab):
                d = M[tab].get(emp)
                return vazio if d is None else d[(d.competencia >= de) & (d.competencia <= ate)]
            fat, imp, impd = fatia("faturamento_mensal"), fatia("impostos_mensais"), fatia("impostos_destacados_mensais")
            folha, func = fatia("folha_mensal"), fatia("funcionarios_mensal")
            enc, dre, rub = fatia("encargos_mensais"), fatia("dre_mensal"), fatia("folha_rubricas_mensais")
            if all(d.empty for d in (fat, imp, impd, folha, enc, rub)):
                continue
            _, de_ir, ate_ir, _ = params_passivo_ir((emp,), de, ate)
            b = ir_por_emp.get(emp)
            bal_ir = (b[(b.competencia >= de_ir) & (b.competencia <= ate_ir)] if b is not None
                      else ir.iloc[0:0])
            S = series_mensais(fat, impd, folha, func, bal_cols, periods, encargos_df=enc,
                               dre_df=dre, rub_df=rub, imp_df=imp, bal_ir_df=bal_ir)
            fol = pd.to_numeric(S["folha_pm"], errors="coerce").astype("float64")
            enc_pm = pd.to_numeric(S["enc_pm"], errors="coerce").astype("float64")
            folenc = fol.fillna(0) + enc_pm.fillna(0)
            folenc[fol.isna() & enc_pm.isna()] = float("nan")
            imp_pm = pd.to_numeric(S["imp_pm"], errors="coerce").astype("float64")
            comps_fat = {}
            if not fat.empty:
                cols = {"NFe": "valo_saidas", "NFS-e": "valo_servicos", "Outras receitas": "valo_imobiliario"}
                for num, col in cols.items():
                    s = _cents_col(fat[col]) if col in fat.columns else pd.Series(0, index=fat.index)
                    comps_fat[num] = s.groupby(fat["competencia"]).sum()
            for p in periods:
                if p >= mes_carga:
                    continue
                if comps_fat and p in comps_fat["NFe"].index:
                    tot = 0
                    for num in NUMEROS_FATURAMENTO:
                        c = int(comps_fat[num].get(p, 0))
                        out[(emp, p, num)] = c / 100
                        tot += c
                    out[(emp, p, "Total")] = tot / 100
                for num, serie in (("Impostos", imp_pm), ("Folha", folenc)):
                    v = serie.get(p)
                    if v is not None and v == v:
                        out[(emp, p, num)] = float(v)
    return out


# ---------------------------------------------------------------------------
# Classificacao em lote, gravacao (append-only) e orquestracao best-effort
# ---------------------------------------------------------------------------

def classifica_diferencas(conn, diffs: list, run_id: int, commit_atual, r0_teto: int | None = None) -> list[dict]:
    """Diferencas -> linhas prontas pra gravar (com causa). R0 de cada par = maior
    run 'ok' anterior a `r0_teto` (default: o proprio run) que cobre a empresa x mes."""
    if not diffs:
        return []
    runs = le_runs_ok(conn, r0_teto or run_id)
    r0s = {(d.codi_emp, d.competencia): r0_do_par(runs, d.codi_emp, d.competencia) for d in diffs}
    r0_min = min((r.run_id if r else 0) for r in r0s.values())
    m = le_manifesto(conn, r0_min, run_id, {d.codi_emp for d in diffs})
    por_par: dict = {}
    for d in diffs:
        por_par.setdefault((d.codi_emp, d.competencia), {})[d.numero] = d.dif_c
    linhas = []
    for d in diffs:
        r0 = r0s[(d.codi_emp, d.competencia)]
        r0_id = r0.run_id if r0 else 0
        c_r0 = r0.git_commit if r0 else None
        comp_fat = {n: por_par[(d.codi_emp, d.competencia)].get(n, 0) for n in NUMEROS_FATURAMENTO}
        causa = classifica(d.numero, d.codi_emp, d.competencia, d.dif_c, m, r0_id, commit_atual, c_r0,
                           componentes=comp_fat if d.numero == "Total" else None)
        ano = d.competencia[:4]
        linhas.append({
            "run_id": run_id, "codi_emp": d.codi_emp, "competencia": d.competencia, "numero": d.numero,
            "valor_antes": None if d.antes_c is None else Decimal(d.antes_c) / 100,
            "valor_depois": None if d.depois_c is None else Decimal(d.depois_c) / 100,
            "diferenca": Decimal(d.dif_c) / 100, "causa": causa.causa,
            "parte_dominio": None if causa.parte_dominio_c is None else Decimal(causa.parte_dominio_c) / 100,
            "parte_carga_nova": None if causa.parte_carga_nova_c is None else Decimal(causa.parte_carga_nova_c) / 100,
            "fontes": causa.fontes, "janela_calculo": f"{ano}-01..{ano}-12",
            "git_commit": commit_atual, "git_commit_r0": c_r0,
        })
    return linhas


_COLS_INSERT = ("run_id", "codi_emp", "competencia", "numero", "valor_antes", "valor_depois", "diferenca",
                "causa", "parte_dominio", "parte_carga_nova", "fontes", "janela_calculo", "git_commit",
                "git_commit_r0")


def grava_alteracoes(conn, linhas: list[dict]) -> None:
    """INSERT ... ON CONFLICT DO NOTHING numa transacao. Historico: nada e regravado."""
    if not linhas:
        return
    from psycopg2.extras import execute_values

    with conn.cursor() as cur:
        execute_values(
            cur,
            f"INSERT INTO ops.alteracoes_retroativas ({', '.join(_COLS_INSERT)}) VALUES %s "
            "ON CONFLICT (run_id, codi_emp, competencia, numero) DO NOTHING",
            [tuple(lin[c] for c in _COLS_INSERT) for lin in linhas], page_size=1000)
    conn.commit()


def sem_ok(txt: str) -> str:
    """O progresso do Atualizar Dados conta linhas com 'OK ' (app.py _OK_LINE_RE)."""
    return re.sub(r"OK ", "Ok ", str(txt))


def emps_do_run(marts: dict) -> list[int]:
    """Empresas que aparecem em QUALQUER mart com competencia do run (o alcance da
    limpeza escopada do mart); mart.empresas fica de fora -- ela nao tem competencia."""
    emps: set = set()
    for df in marts.values():
        if hasattr(df, "columns") and "competencia" in df.columns and "codi_emp" in df.columns and not df.empty:
            emps |= {int(e) for e in df["codi_emp"].dropna().unique()}
    return sorted(emps)


def _conecta():
    from connections import connect_postgres
    return connect_postgres()


def captura_antes(marts: dict, inicio: str, fim: str, run_id: int, iniciado_em: datetime, report: dict,
                  log=lambda m: None):
    """ANTES: os numeros do mart velho (o que a tela mostrava). Nunca levanta."""
    try:
        conn = _conecta()
        try:
            with conn.cursor() as cur:
                aviso = _run_anterior_morto(cur, run_id)
                if aviso:
                    report["retroativas_aviso"] = aviso
                cur.execute("SELECT 1 FROM ops.controle_runs WHERE status = 'ok' AND run_id < %s LIMIT 1",
                            (run_id,))
                tem_anterior = cur.fetchone() is not None
            if not tem_anterior:
                report["retroativas_pulada"] = MSG_PULADA_SEM_RUN
                log(MSG_PULADA_SEM_RUN)
                return None
            emps = emps_do_run(marts)
            if not emps:
                return None
            log(MSG_INICIO_ANTES)
            mes = mes_da_carga(iniciado_em)
            return {"antes": le_numeros(conn, emps, inicio, fim, mes), "emps": emps, "mes": mes}
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 -- best-effort
        report["retroativas_erro"] = sem_ok(f"antes: {exc}")
        return None


def resumo_das_linhas(linhas: list[dict]) -> dict:
    """Contagens pro resumo de fim de run; o Total (derivado) fica de fora."""
    v = [lin for lin in linhas if lin["numero"] != "Total"]
    c = lambda causa: sum(1 for lin in v if lin["causa"] == causa)   # noqa: E731
    return {"total": len(v), "dominio": c(CAUSA_DOMINIO), "calculo": c(CAUSA_CALCULO),
            "inesperado": c(CAUSA_INESPERADA)}


def fecha_comparacao(estado, inicio: str, fim: str, run_id: int, report: dict, commit_atual,
                     r0_teto: int | None = None, log=lambda m: None) -> None:
    """DEPOIS: recalcula, compara com o ANTES, classifica e grava. Nunca levanta."""
    if estado is None:
        return
    try:
        conn = _conecta()
        try:
            log(MSG_FIM_DEPOIS)
            depois = le_numeros(conn, estado["emps"], inicio, fim, estado["mes"])
            diffs = compara(estado["antes"], depois, estado["mes"])
            linhas = classifica_diferencas(conn, diffs, run_id, commit_atual, r0_teto)
            grava_alteracoes(conn, linhas)
            report["retroativas"] = resumo_das_linhas(linhas)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 -- best-effort
        report["retroativas_erro"] = sem_ok(f"depois: {exc}")


def linha_resumo(resumo: dict) -> str:
    """Linha do resumo de fim de run: 'N alteracoes retroativas (X mudou no Dominio)'."""
    return f"{resumo['total']} alteracoes retroativas ({resumo['dominio']} mudou no Dominio)"


def aviso_run_morto(run_id: int, status: str) -> str:
    """Aviso do resumo quando o run ANTERIOR morreu depois de publicar raw."""
    return (f"run {run_id} anterior terminou em '{status}' depois de publicar -- "
            f"alteracoes daquele run podem nao ter sido comparadas")


def _run_anterior_morto(cur, run_id: int) -> str | None:
    """Run imediatamente anterior em 'executando'/'erro' que ja publicou geracao do raw.

    Processo morto no meio da gravacao do mart perde o ANTES (estava em memoria):
    os marts que ele ja gravou entram no ANTES do run seguinte e a mudanca deles
    nao e registrada. Aviso so, sem staging."""
    cur.execute("SELECT run_id, status FROM ops.controle_runs WHERE run_id < %s "
                "ORDER BY run_id DESC LIMIT 1", (run_id,))
    ant = cur.fetchone()
    if ant is None or ant[1] not in ("executando", "erro"):
        return None
    cur.execute("SELECT 1 FROM ops.raw_generation WHERE run_id = %s AND status = 'PUBLISHED' LIMIT 1",
                (ant[0],))
    return aviso_run_morto(int(ant[0]), ant[1]) if cur.fetchone() is not None else None
