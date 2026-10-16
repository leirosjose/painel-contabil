# ---------------------------------------------------------------------------
# main.py -Orquestrador do ETL Contabilidade -> PostgreSQL
#
# Uso:
#   python main.py                          # todas as empresas, últimos 12 meses
#   python main.py --empresa 1              # empresa específica
#   python main.py --inicio 2025-01 --fim 2025-12
#   python main.py --skip-extract           # lê raw do PostgreSQL, pula a origem
# ---------------------------------------------------------------------------

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta

from fonte.sintetica import FonteSintetica
from fonte.base import Fonte
import pandas as pd

from load import ensure_schemas, expurga_geracoes_si, upsert_mart
import geracao as G
import retroativas as RET
from raw_escopo import ESCOPOS, GLOBAIS, pedido
from config import CORTE_CANONICO, PG_SCHEMA_RAW
from connections import connect_postgres
from raw_schema import RAW_KEYS, inicio_do_mes
from transform import (
    _to_competencia,
    build_balancete_mensal,
    build_dre_mensal,
    build_empresa_regime_periodo,
    build_empresas,
    build_encargos_mensais,
    build_faturamento_mensal,
    build_folha_mensal,
    build_folha_rubricas_mensais,
    build_funcionarios_mensal,
    build_impostos_destacados_mensais,
    build_iss_retido_sofrido,
    build_impostos_mensais,
)

# ---------------------------------------------------------------------------
# Chaves de conflito para upsert
# Ajuste se as PKs reais do seu schema diferirem.
# ---------------------------------------------------------------------------


MART_KEYS: dict[str, list[str]] = {
    "faturamento_mensal":     ["codi_emp", "competencia"],
    "impostos_mensais":       ["codi_emp", "competencia", "codi_imp"],
    "folha_mensal":           ["codi_emp", "tipo_process", "competencia"],
    "funcionarios_mensal":    ["codi_emp", "competencia"],
    # Identidade da conta é codi_cta (clas_cta colide entre contas
    # distintas e ficou como atributo — reclassificação vira UPDATE in-place)
    "balancete_mensal":       ["codi_emp", "competencia", "codi_cta"],
    # DRE / resultado contabil (grao COLUNAS, 1 linha/emp/mes ->
    # DELETE-escopado sem risco de "menos linhas")
    "dre_mensal":             ["codi_emp", "competencia"],
    "empresas":                   ["codi_emp"],
    "empresa_regime_periodo":     ["codi_emp", "competencia"],
    "impostos_destacados_mensais": ["codi_emp", "competencia"],
    "encargos_mensais":           ["codi_emp", "competencia"],
    # Folha por rubrica: base do dash de Detalhamento. Grao
    # (empresa, mes, tipo_process, rubrica) — SEM empregado, por LGPD; o grao
    # de pessoa fica no raw.fomovto.
    "folha_rubricas_mensais":     ["codi_emp", "competencia", "tipo_process",
                                   "i_eventos"],
}

# ---------------------------------------------------------------------------
# Utilitários de progresso
# ---------------------------------------------------------------------------

_t0 = time.monotonic()


def _log(msg: str) -> None:
    elapsed = time.monotonic() - _t0
    print(f"  [{elapsed:6.1f}s] {msg}", flush=True)


def _step(label: str) -> None:
    print(f"\n{'-' * 60}", flush=True)
    print(f"  {label}", flush=True)
    print(f"{'-' * 60}", flush=True)


# ---------------------------------------------------------------------------
# Resumo de fim de run (observabilidade)
# ---------------------------------------------------------------------------

def format_run_report(report: dict) -> str:
    """Resumo de fim de run — bloco compacto com ALERTAS por ultimo.

    O resumo torna descarte/anomalia impossivel de nao ver no meio do log,
    sem mudar dado nem exit code.

    Compacto de proposito (~<= 18 linhas no caso tipico): o Streamlit exibe
    o TAIL de 20 linhas do stdout no Atualizar Dados — o resumo e a ultima
    coisa impressa, entao e o que o usuario ve. Funcao PURA (report -> str),
    testavel sem I/O.
    """
    a = report.get("args", {})
    L = ["", "=" * 60,
         f"  RESUMO DO RUN  empresa={a.get('empresa')} "
         f"janela={a.get('inicio')}..{a.get('fim')} modo={a.get('modo')}"]
    if report.get("fim_declarado"):
        L.append(f"  origem declarava até {report['fim_declarado']}")
    ups = report.get("raw_upsert", {})
    ext = report.get("extract", {})
    if ups:
        L.append(f"  raw: {len(ups)} tabela(s), "
                 f"{sum(ups.values())} linha(s) upsertada(s)")
    elif ext:
        L.append(f"  raw lido: {len(ext)} tabela(s), {sum(ext.values())} linha(s)")
    if report.get("overlap_trim"):
        L.append(f"  overlap fobases: {report['overlap_trim']} linha(s) "
                 f"aparada(s) (mes anterior a janela, esperado)")
    if report.get("overlap_trim_foresc"):
        L.append(f"  overlap forescisoes: {report['overlap_trim_foresc']} linha(s) "
                 f"aparada(s) (mes anterior a janela, esperado)")
    if report.get("clamp_fora"):
        L.append(f"  ctlancto fora da janela ignorado no transform: "
                 f"{report['clamp_fora']} linha(s)")
    cc = report.get("cascata_clamp", {})
    if cc:
        L.append("  cascata clampada a janela: "
                 + ", ".join(f"{k}={v}" for k, v in cc.items())
                 + " par(es) fora nao tocado(s) no mart")
    sd = report.get("si_descartado", {})
    if sd.get("linhas"):
        L.append(f"  saldo inicial descartado por corte != {sd.get('corte')}: "
                 f"{sd['linhas']} linha(s) de {sd.get('empresas', '?')} empresa(s)")
    ed = report.get("estorno_deducao_delta")
    if ed:
        L.append(f"  estorno na conta de dedução (líquido aplicado): "
                 f"R$ {ed['total']:.2f} abatido(s) do numerador em "
                 f"{len(ed.get('por_empresa', {}))} empresa(s)")
    m = report.get("marts", {})
    if m:
        L.append(f"  marts: {len(m)} gravado(s), {sum(m.values())} linha(s)")
    ret = report.get("retroativas")
    if ret:
        L.append(f"  {RET.linha_resumo(ret)}")
    if report.get("retroativas_pulada"):
        L.append(f"  retroativas: comparacao pulada ({RET.sem_ok(report['retroativas_pulada'])})")

    alertas = []
    for tab, d in sorted(report.get("dedup_delta", {}).items()):
        alertas.append(f"ALERTA dedup nao-no-op em raw.{tab}: {d} linha(s) "
                       f"comida(s) — grao violado, investigar (esperado 0)")
    if report.get("si_zumbi_emps"):
        e = report["si_zumbi_emps"]
        alertas.append(f"ALERTA {len(e)} empresa(s) com si 100% DESCARTADO por "
                       f"corte E lancamentos na janela -> balancete zero-base"
                       f": {e[:10]}")
    if report.get("grfc_parcial"):
        alertas.append(f"ALERTA raw.foguiagrfc PARCIAL: "
                       f"{report['grfc_parcial']} linha(s) sem nenhum dos "
                       f"valores de desembolso (mes_ant/resc/aviso_previo) — "
                       f"encargo rescisorio SUBESTIMADO nesses meses; "
                       f"re-extrair a foguiagrfc na janela cheia")
    if report.get("sem_si_com_lancto"):
        alertas.append("ALERTA balancete construido SEM saldo inicial "
                       "(cumsum parte de zero)")
    cas = report.get("ctlancto_anos_suspeitos")
    if cas:
        amin, amax = cas["anos"]
        alertas.append(f"ALERTA raw.ctlancto: {cas['linhas']} lancamento(s) "
                       f"com ano fora de [{ANO_SUSPEITO_MIN}, {ANO_SUSPEITO_MAX}] "
                       f"(anos {amin}-{amax}, typo de milenio na origem — "
                       f"zero efeito no mart, so higiene da origem)")
    ird = report.get("iss_retido_competencia_invalida")
    if ird:
        alertas.append(f"ALERTA ISS retido sofrido: {ird['linhas']} nota(s) "
                       f"com competencia invalida (dser_ser imparseavel) "
                       f"descartada(s), R$ {ird['soma']:.2f} fora do "
                       f"numerador; empresas: {ird['por_empresa']}")
    for x in report.get("geracoes_rejeitadas", []):
        alertas.append(f"ALERTA geracao REJEITADA: {x} -- o vigente segue a geracao "
                       f"anterior; o run sai com codigo 3")
    for x in report.get("fatias_rejeitadas", []):
        alertas.append(f"ALERTA encolhimento: {x} -- seguem na geracao anterior "
                       f"(liberar: --aceitar-encolhimento TABELA[:EMP[:FATIA]] --motivo ...)")
    for t in report.get("extract_errors", []):
        alertas.append(f"ALERTA extracao FALHOU: {t}")
    for t in report.get("mart_errors", []):
        alertas.append(f"ALERTA mart FALHOU: {t}")
    if ret and ret.get("inesperado"):
        alertas.append(f"ALERTA retroativas: {ret['inesperado']} alteracao(oes) 'inesperado' "
                       f"(nenhuma fatia publicada e mesmo commit) -- investigar")
    if report.get("retroativas_aviso"):
        alertas.append(f"ALERTA retroativas: {RET.sem_ok(report['retroativas_aviso'])}")
    if report.get("retroativas_erro"):
        alertas.append(f"ALERTA alteracoes retroativas: comparacao FALHOU ({RET.sem_ok(report['retroativas_erro'])}); "
                       f"o run segue")
    if alertas:
        L.append("  " + "-" * 56)
        L.extend(f"  {x}" for x in alertas)
    else:
        L.append("  sem alertas")
    L.append("=" * 60)
    return "\n".join(L)


def _espelha_log(resumo: str) -> None:
    """Append do resumo em logs/etl_runs.log (raiz do projeto) — memoria
    forense de runs. Path ancorado
    em __file__, NAO na cwd: o Popen do Streamlit pode rodar com cwd
    diferente e o log sumiria calado. Best-effort: log nunca derruba run."""
    try:
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "etl_runs.log"), "a",
                  encoding="utf-8") as f:
            f.write(f"\n[{datetime.now().isoformat(timespec='seconds')}]"
                    f"{resumo}\n")
    except OSError as exc:
        print(f"  AVISO: espelho do resumo em logs/ falhou: {exc}", flush=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    hoje = date.today()
    doze_meses_atras = (hoje.replace(day=1) - timedelta(days=365)).strftime("%Y-%m")
    mes_atual = hoje.strftime("%Y-%m")

    parser = argparse.ArgumentParser(description="ETL Contabilidade -> PostgreSQL")
    parser.add_argument(
        "--empresa",
        default="all",
        help="Código da empresa (int) ou 'all' para todas (default: all)",
    )
    parser.add_argument(
        "--inicio",
        default=doze_meses_atras,
        metavar="YYYY-MM",
        help="Mês inicial do período (default: 12 meses atrás)",
    )
    parser.add_argument(
        "--fim",
        default=mes_atual,
        metavar="YYYY-MM",
        help="Mês final do período (default: mês atual)",
    )
    parser.add_argument(
        "--skip-extract",
        action="store_true",
        help="Pula a leitura da origem e lê direto do schema raw do PostgreSQL",
    )
    parser.add_argument(
        "--saldo-inicial",
        choices=["auto", "always", "never"],
        default="auto",
        help=("Extração de ctsaldo_inicial: 'auto' e 'always' extraem SEMPRE "
              "(o saldo depende do corte --inicio); 'never' pula (debug)."),
    )
    parser.add_argument(
        "--probe-janela",
        action="store_true",
        help=("Imprime a decisão da janela incremental (watermark "
              "× fim declarado da origem) em JSON e sai, sem rodar nada."),
    )
    parser.add_argument(
        "--aceitar-encolhimento", action="append", default=[], metavar="TABELA[:CODI_EMP[:FATIA]]",
        help="libera a guarda de encolhimento nesse escopo; exige --motivo, que vai pro "
             "manifesto. A guarda de TABELA (perda > 5%% do pedido) so libera com o nome da "
             "tabela sozinho")
    parser.add_argument("--motivo", default=None,
                        help="obrigatorio com --aceitar-encolhimento")
    return parser.parse_args()


def valida_janela(inicio: str, fim: str) -> None:
    """Guard de janela invertida: inicio > fim não processa NADA das
    tabelas com data e sairia "sem alertas" — run inócuo indistinguível de um
    saudável. Falha cedo (vira ERRO FATAL no main). Causa comum: competência
    FUTURA nos marts puxando o modo incremental do app (lançamento com data
    futura na origem). Comparação lexicográfica — YYYY-MM ordena certo."""
    if inicio > fim:
        raise ValueError(
            f"janela invertida: --inicio {inicio} > --fim {fim} — nada seria "
            f"processado (competência futura nos marts?). Use uma "
            f"janela com inicio <= fim."
        )


# ---------------------------------------------------------------------------
# Janela incremental = watermark × fim DECLARADO da origem.
# A origem é SNAPSHOT deliberado: ela declara até onde vai, e a declaração
# é legível no batimento diário da efsaidas (documento fiscal não tem datas
# futuras; ctlancto tem typos de milênio e recorrências futuras, FOBASES
# tem adiantamento sistemático).
# ---------------------------------------------------------------------------

JANELA_FULL_INICIO = "2023-01"  # corte canônico


def mes_declarado(max_data, hoje_mes: str) -> str | None:
    """Mês YYYY-MM do último documento fiscal da origem, clampado ao mês de
    hoje (cinto contra typo de data futura, como os do ctlancto).
    None = origem sem documentos."""
    if max_data is None:
        return None
    mes = str(max_data)[:7]
    return min(mes, hoje_mes)


def alem_do_declarado(maxes, fim_declarado: str | None,
                      hoje_mes: str) -> str | None:
    """Mês mais avançado com dado PARCIAL além do fim declarado — fluxos
    contínuos (efservicos/efentradas) andam à frente da escrituração fiscal
    importada. None = nada além. Cinto: mês além de hoje é typo, não
    'parcial'. Alimenta o texto explicativo do painel do run: o usuário lê
    POR QUE a janela para no declarado."""
    if not fim_declarado:
        return None
    meses = [str(m)[:7] for m in maxes if m is not None]
    alem = [m for m in meses if fim_declarado < m <= hoje_mes]
    return max(alem) if alem else None


def data_alem_do_declarado(maxes, fim_declarado: str | None,
                           hoje_mes: str) -> str | None:
    """Par do alem_do_declarado: a DATA exata (YYYY-MM-DD) do documento mais
    avançado além do declarado — alimenta o aviso acionável do painel
    ("se quiser incluir os dados que já temos até DD/MM/AAAA, rode com
    Período customizado")."""
    if not fim_declarado:
        return None
    datas = [str(m)[:10] for m in maxes if m is not None]
    alem = [d for d in datas if fim_declarado < d[:7] <= hoje_mes]
    return max(alem) if alem else None


def decide_janela_v4(watermark: str | None, fim_declarado: str | None) -> dict:
    """Decisão da janela do modo incremental:

    inicio = watermark INCLUSIVO (re-extrai o último mês processado —
    fechamento do mês N revisa N-1; idempotente); fim = o que a origem
    DECLARA. fim == watermark RODA (growth intra-mês de snapshot
    re-atualizado é caso real). fim < watermark = noop com
    mensagem EXPLÍCITA — nunca verde silencioso.
    Watermark ausente = 1º run / banco novo -> janela completa canônica."""
    if not fim_declarado:
        return {"acao": "noop", "inicio": None, "fim": None,
                "mensagem": "origem sem documentos fiscais — nada a processar"}
    if watermark is None:
        return {"acao": "run", "inicio": JANELA_FULL_INICIO,
                "fim": fim_declarado, "mensagem": None}
    if fim_declarado < watermark:
        return {"acao": "noop", "inicio": None, "fim": None,
                "mensagem": (f"origem não tem períodos novos além de "
                             f"{watermark} (origem declara até {fim_declarado})")}
    return {"acao": "run", "inicio": watermark, "fim": fim_declarado,
            "mensagem": None}


def watermark_do_run(fim: str, fim_declarado: str | None) -> str | None:
    """Watermark gravada por um run completo: min(fim da janela, fim
    declarado pela origem NO MOMENTO do run) — um full run com --fim à
    frente (ex. 2026-12) nunca envenena a watermark. None (skip-extract
    não consulta origem) = não avança."""
    if not fim_declarado:
        return None
    return min(fim, fim_declarado)


def fim_declarado_origem(fonte: Fonte) -> str | None:
    """Mês do último documento fiscal emitido na origem — a data que o
    snapshot declara."""
    hoje_mes = date.today().strftime("%Y-%m")
    return mes_declarado(fonte.max_datas()["efsaidas"], hoje_mes)


def _le_watermark() -> str | None:
    """Última competência efetivamente processada: MAX(watermark) dos runs
    ok de carteira completa em ops.controle_runs (tabela ausente = None)."""
    conn = connect_postgres()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('ops.controle_runs')")
            if cur.fetchone()[0] is None:
                return None
            cur.execute(
                "SELECT MAX(watermark) FROM ops.controle_runs "
                "WHERE status = 'ok' AND empresa = 'all' AND modo = 'completo'"
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


_DDL_CONTROLE_RUNS = """
    CREATE TABLE IF NOT EXISTS ops.controle_runs (
        run_id        bigserial PRIMARY KEY,
        iniciado_em   timestamptz,
        terminado_em  timestamptz DEFAULT now(),
        empresa       text,
        modo          text,
        inicio        text,
        fim           text,
        fim_declarado text,
        watermark     text,
        status        text,
        linhas_raw    bigint,
        linhas_mart   bigint
    )
"""


def _abre_run(report: dict, iniciado_em: datetime) -> int:
    """Abre o run ANTES de tudo: a geracao do raw precisa do run_id, e run
    morto passa a deixar rastro ('executando'). O watermark so le status='ok'."""
    a = report.get("args", {})
    conn = connect_postgres()
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS ops")
            cur.execute(_DDL_CONTROLE_RUNS)
            # Commit do checkout que rodou (lido de .git sem subprocess; NULL se nao der).
            cur.execute("ALTER TABLE ops.controle_runs ADD COLUMN IF NOT EXISTS git_commit text")
            commit = RET.git_commit_curto(RET.RAIZ)
            report["git_commit"] = commit
            cur.execute("INSERT INTO ops.controle_runs (iniciado_em, terminado_em, empresa, modo, "
                        "inicio, fim, status, git_commit) VALUES (%s, NULL, %s, %s, %s, %s, 'executando', %s) "
                        "RETURNING run_id",
                        (iniciado_em, a.get("empresa"), a.get("modo"), a.get("inicio"), a.get("fim"), commit))
            run_id = int(cur.fetchone()[0])
        conn.commit()
        return run_id
    finally:
        conn.close()


def _status_final(report: dict) -> str:
    """'ok' | 'rejeitado' | 'parcial' -- status de fecho do run.

    Run que termina com ALERTA mart FALHOU (report['mart_errors']) nao pode
    fechar 'ok': _le_watermark (acima) e o app filtram status='ok' pra
    decidir o watermark do incremental e o texto do sidebar, e um run com
    mart quebrado avancaria os dois calado.
    'rejeitado' mantem precedencia -- e o caso que ja sai com exit code 3.
    """
    if report.get("geracoes_rejeitadas") or report.get("fatias_rejeitadas"):
        return "rejeitado"
    if report.get("mart_errors"):
        return "parcial"
    return "ok"


def _fecha_run(run_id: int | None, report: dict, status: str) -> None:
    """Fecha o run: 'ok' | 'rejeitado' | 'parcial' | 'erro'. Best-effort: nunca derruba o run."""
    if run_id is None:
        return
    try:
        a = report.get("args", {})
        fim_decl = report.get("fim_declarado")
        # A watermark e gravada em todo run completo; quem a LE
        # (_le_watermark e o app) filtra status='ok'.
        wm = (watermark_do_run(a.get("fim"), fim_decl)
              if a.get("modo") == "completo" else None)
        conn = connect_postgres()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE ops.controle_runs SET terminado_em = now(), fim_declarado = %s, "
                            "watermark = %s, status = %s, linhas_raw = %s, linhas_mart = %s "
                            "WHERE run_id = %s",
                            (fim_decl, wm, status,
                             sum(report.get("raw_upsert", {}).values()) or None,
                             sum(report.get("marts", {}).values()) or None, run_id))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — auditoria nunca derruba run
        print(f"  AVISO: fechar o run {run_id} em ops.controle_runs falhou: {exc}", flush=True)


# Chave fixa e arbitraria do advisory lock de execucao unica do ETL. So
# precisa ser ESTAVEL entre processos (a mesma chave em todo `main()`) e nao
# colidir com nenhum outro uso de advisory lock no banco.
_LOCK_ID_RUN = 727272001


class RunConcorrenteError(RuntimeError):
    """Ja existe um ETL em execucao (advisory lock em ops nao adquirido)."""


def _adquire_lock_run():
    """Trava de execucao unica do ETL.

    pg_try_advisory_lock (SESSION-scoped, NAO pg_advisory_XACT_lock) numa
    conexao DEDICADA que o chamador mantem aberta pelo run INTEIRO --
    diferente de _abre_run/_fecha_run/run_load_raw, que abrem e fecham
    conexoes proprias a cada chamada, entao um lock transacional liberaria
    no primeiro commit. Recusa com RunConcorrenteError (fecha a conexao
    antes) se outro processo ja detem a chave -- guarda contra dois runs
    concorrentes abrindo buraco no mart via DELETE-escopado.

    Processo morto SEM liberar: por ser trava
    de SESSAO, ela morre com a conexao -- um SIGKILL/crash fecha o socket
    TCP na saida do processo (o SO fecha os descritores do processo morto) e
    o Postgres libera a trava ao notar o EOF, tipicamente em segundos, sem
    depender de timeout de statement nem de keepalive. So fica exposto o
    caso patologico de queda de rede/energia sem FIN (o Postgres so
    reaproveita por keepalive TCP, que pode demorar horas no default) -- e
    residual e conhecido.
    """
    conn = connect_postgres()
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_ID_RUN,))
        obtido = cur.fetchone()[0]
    conn.commit()
    if not obtido:
        conn.close()
        raise RunConcorrenteError(
            "ja existe um ETL em execucao (advisory lock nao adquirido) -- "
            "espere o outro run terminar antes de rodar de novo."
        )
    return conn


def _libera_lock_run(conn) -> None:
    """Libera a trava de execucao unica e fecha a conexao dedicada. Best-effort: nunca
    derruba o run (mesma politica do _fecha_run)."""
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID_RUN,))
        conn.commit()
    except Exception as exc:  # noqa: BLE001 — liberar o lock nunca derruba o run
        print(f"  AVISO: liberar o lock do run falhou: {exc}", flush=True)
    finally:
        conn.close()


def _checa_detector() -> None:
    """Inicio de todo --skip-extract: recusa se a leitura vigente estiver incoerente."""
    conn = connect_postgres()
    try:
        probs, avisos = G.detecta_rapido(conn)
    finally:
        conn.close()
    for x in avisos:
        _log(f"AVISO {x}")
    if probs:
        raise RuntimeError("detector de geracao recusa o --skip-extract: " + "; ".join(probs[:5]))


def probe_janela(fonte: Fonte) -> dict:
    """Decisão da janela incremental SEM rodar nada (consumido pela UI via
    subprocess: o usuário vê 'processado até X · origem tem até Y' ANTES do
    run). alem_declarado = mês com dado parcial além do declarado (alimenta
    o texto explicativo do painel). Read-only nas duas pontas."""
    watermark = _le_watermark()
    hoje_mes = date.today().strftime("%Y-%m")
    maxes = fonte.max_datas()
    fim_decl = mes_declarado(maxes["efsaidas"], hoje_mes)
    spill = [maxes["efservicos"], maxes["efentradas"]]
    alem = alem_do_declarado(spill, fim_decl, hoje_mes)
    alem_data = data_alem_do_declarado(spill, fim_decl, hoje_mes)
    decisao = decide_janela_v4(watermark, fim_decl)
    return {"watermark": watermark, "fim_declarado": fim_decl,
            "alem_declarado": alem, "alem_declarado_data": alem_data,
            **decisao}


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_read_raw(empresa, report: dict | None = None) -> dict:
    """Le o ESTADO VIGENTE do raw (VIEWs raw."<t>") de UM instante so.

    REPEATABLE READ somente-leitura: todas as tabelas do mesmo snapshot; com a
    publicacao atomica (geracao.publica) a leitura ve um run inteiro ou nenhum.
    Dicionario GLOBAL nao leva filtro de empresa -- com filtro, o skip-extract
    por empresa gravaria a folha sem dicionario.
    """
    _step("READ RAW -PostgreSQL raw vigente -> memória")
    raw: dict = {}
    conn = connect_postgres()
    try:
        conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
        with conn.cursor() as cur:
            cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
                        (PG_SCHEMA_RAW,))
            existentes = {r[0] for r in cur.fetchall()}
            for name in RAW_KEYS:
                if name not in existentes:
                    _log(f"AUSENTE {name:<20} (nunca carregada)")
                    if report is not None:
                        report.setdefault("extract_errors", []).append(name)
                    continue
                where = (f'WHERE "codi_emp" = {int(empresa)}'
                         if empresa is not None and name not in GLOBAIS else "")
                cur.execute(f'SELECT * FROM {PG_SCHEMA_RAW}."{name}" {where}')
                cols = [d[0] for d in cur.description]
                df = pd.DataFrame(cur.fetchall(), columns=cols)
                raw[name] = df
                _log(f"OK {name:<20} {len(df):>7} linhas")
                if report is not None:
                    report.setdefault("extract", {})[name] = len(df)
    finally:
        conn.close()
    return raw


def run_extract(empresa, inicio, fim, saldo_inicial_mode="auto",
                report: dict | None = None, *, fonte: Fonte) -> dict:
    """Lê todas as tabelas da origem pela `fonte`. Retorna dict nome -> DataFrame."""
    _step("EXTRACT -origem -> memória")

    jobs = [
        ("geempre",         lambda: fonte.ler("geempre", empresa)),
        ("ctcontas",        lambda: fonte.ler("ctcontas", empresa)),
        ("ctlancto",        lambda: fonte.ler("ctlancto", empresa, inicio, fim)),
        ("efsaidas",        lambda: fonte.ler("efsaidas", empresa, inicio, fim)),
        ("efentradas",      lambda: fonte.ler("efentradas", empresa, inicio, fim)),
        ("efservicos",      lambda: fonte.ler("efservicos", empresa, inicio, fim)),
        # ISS retido sofrido: a tabela nao tem data — a janela vem da nota.
        ("efservicospar",   lambda: fonte.ler("efservicospar", empresa, inicio, fim)),
        ("efsdoimp",        lambda: fonte.ler("efsdoimp", empresa, inicio, fim)),
        ("foempregados",    lambda: fonte.ler("foempregados", empresa, inicio, fim)),
        # fobases extrai com 1 mês de overlap pra trás — âncora do
        # shift de qtd_inicial do 1º mês. Aparado antes do upsert (main()).
        ("fobases",         lambda: fonte.ler("fobases", empresa, _mes_anterior(inicio), fim)),
        ("hrfaturamento",   lambda: fonte.ler("hrfaturamento", empresa, inicio, fim)),
        # Guias de folha (encargos fiel a guia)
        ("foguiainss",      lambda: fonte.ler("foguiainss", empresa, inicio, fim)),
        ("fofgtsfilial",    lambda: fonte.ler("fofgtsfilial", empresa, inicio, fim)),
        ("foguiagrfc",      lambda: fonte.ler("foguiagrfc", empresa, inicio, fim)),
        # Como fobases, forescisoes tambem extrai com 1 mes de
        # overlap pra tras — a demissao de N-1 liquida os ativos do mes-ancora.
        # Aparado antes do upsert (main()); so o build de funcionarios ve N-1.
        ("forescisoes",     lambda: fonte.ler("forescisoes", empresa, _mes_anterior(inicio), fim)),
        # Folha por RUBRICA — base do dash de Detalhamento. Janela igual a das
        # demais, SEM overlap N-1 (nao ha logica de shift aqui, ao contrario
        # do fobases).
        ("fomovto",         lambda: fonte.ler("fomovto", empresa, inicio, fim)),
        ("foeventos",       lambda: fonte.ler("foeventos", empresa, inicio, fim)),
        ("foeventosdef",    lambda: fonte.ler("foeventosdef")),
        ("foclassificacoes_eventos", lambda: fonte.ler("foclassificacoes_eventos")),
        # Recebimento de parcela de venda de imovel.
        # Janela normal, SEM overlap N-1: nao ha shift nem ancora aqui.
        ("efimob_pagamentos", lambda: fonte.ler("efimob_pagamentos", empresa, inicio, fim)),
    ]

    # ctsaldo_inicial é extraído SEMPRE junto com a janela. O saldo depende
    # do corte --inicio, então reusar extração antiga é incorreto (deixa
    # empresas sem saldo de abertura). 'never' mantém pulo explícito (debug).
    if saldo_inicial_mode != "never":
        jobs.append(("ctsaldo_inicial",
                     lambda: fonte.saldo_inicial(empresa, inicio)))
    else:
        _log("PULA ctsaldo_inicial (modo=never)")

    raw: dict = {}
    errors: list[str] = []
    for name, fn in jobs:
        try:
            df = fn()
            raw[name] = df
            _log(f"OK {name:<20} {len(df):>7} linhas")
            if report is not None:
                report.setdefault("extract", {})[name] = len(df)
        except Exception as exc:
            _log(f"ERRO {name:<20} ERRO: {exc}")
            errors.append(name)

    if errors:
        print(f"\n  AVISO: {len(errors)} tabela(s) falharam na extração: {errors}")
    if report is not None:
        report.setdefault("extract_errors", []).extend(errors)

    return raw


def run_load_raw(raw: dict, inicio: str, fim: str, empresa, run_id: int,
                 report: dict | None = None, aceitas: frozenset = frozenset(),
                 motivo: str | None = None) -> None:
    """Grava cada tabela como uma GERACAO em raw_hist e publica tudo numa transacao.

    Nada fica vigente antes de todas as tabelas validarem. Geracao
    REJEITADA fica em raw_hist pra auditoria e nunca aparece na VIEW.
    """
    _step("LOAD RAW -memória -> raw_hist (geração por linha)")
    rep = report if report is not None else {}
    fomovto = raw.get("fomovto", pd.DataFrame())
    emp_mov = (set(pd.to_numeric(fomovto["codi_emp"]).astype("int64"))
               if "codi_emp" in fomovto.columns else set())
    resultados = []
    conn = connect_postgres()
    try:
        for name, df in raw.items():
            if name not in ESCOPOS:
                continue
            ped = pedido(name, empresa, inicio, fim, emp_mov)
            try:
                r = G.processa_tabela(conn, run_id, name, df, RAW_KEYS.get(name, []), ped,
                                      notas=raw.get("efservicos"), aceitas=aceitas,
                                      motivo_override=motivo)
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                _log(f"ERRO raw.{name:<20} ERRO: {exc}")
                rep.setdefault("extract_errors", []).append(f"load raw.{name}")
                continue
            resultados.append(r)
            dest = r.fatias["destino"].value_counts().to_dict()
            # O encolhimento do pedido (vigente -> geracao nova) sai na linha da
            # tabela, pra conferir sem consulta a parte.
            ant, atu = int(r.fatias["anterior"].sum()), int(r.fatias["linhas"].sum())
            _log(f"{'OK' if r.status == 'SUCCESS' else 'REJEITADA'} raw.{name:<20} "
                 f"geracao {r.generation_id}: {r.linhas_gravadas} linha(s), "
                 f"{dest.get('PUBLISHED', 0)} fatia(s) nova(s), {dest.get('UNCHANGED', 0)} "
                 f"inalterada(s), {dest.get('REJECTED', 0)} rejeitada(s) | "
                 f"pedido {ant} -> {atu} ({atu - ant:+d})")
            rep.setdefault("raw_upsert", {})[name] = r.linhas_gravadas
            if r.dedup > 0:
                rep.setdefault("dedup_delta", {})[name] = r.dedup
            if r.status != "SUCCESS":
                rep.setdefault("geracoes_rejeitadas", []).append(f"raw.{name}: {r.motivo}")
            else:
                rej = r.fatias[r.fatias["destino"] == "REJECTED"]
                if not rej.empty:
                    rep.setdefault("fatias_rejeitadas", []).append(
                        f"raw.{name}: {len(rej)} fatia(s), ex. "
                        f"{rej[['codi_emp', 'fatia']].head(3).values.tolist()}")
        G.publica(conn, resultados)
        problemas = G.verifica_publicacao(conn, resultados)
    finally:
        conn.close()
    if problemas:
        raise RuntimeError("publicacao da geracao INCONSISTENTE -- o transform nao roda: "
                           + "; ".join(problemas[:5]))

    # Expurgo das geracoes de saldo inicial NAO-canonicas e NAO-deste-run.
    if not raw.get("ctsaldo_inicial", pd.DataFrame()).empty:
        manter = sorted({CORTE_CANONICO, inicio_do_mes(inicio)})
        try:
            purga = expurga_geracoes_si(manter)
        except Exception as exc:  # noqa: BLE001
            _log(f"AVISO expurgo de geracoes do saldo inicial FALHOU: {exc}")
        else:
            if purga["linhas"]:
                _log(f"ctsaldo_inicial: {purga['linhas']} linha(s) de geracao antiga apagada(s) "
                     f"(cortes {', '.join(purga['cortes'])}; mantidos {', '.join(manter)})")
            rep["si_purga"] = purga


def _mes_anterior(periodo: str | None) -> str | None:
    """Mês anterior de um período 'YYYY-MM' (overlap da âncora do
    shift de qtd_inicial). Permissivo como inicio_do_mes: formato inesperado
    ou falsy passa como veio, sem levantar."""
    if not periodo or len(periodo) != 7:
        return periodo
    return str(pd.Period(periodo, freq="M") - 1)


def trim_fobases_overlap(fb: pd.DataFrame, inicio: str):
    """Apara o mês de overlap (competencia < inicio) do frame de fobases.

    A extração de fobases roda com 1 mês a mais pra trás só pra
    ancorar o shift de qtd_inicial do 1º mês da janela. O overlap é INTERNO:
    este trim roda antes do upsert de raw e do build da folha — só o build
    de funcionarios vê o mês N−1. Retorna (df_aparado, n_removidas); no-op
    (mesmo objeto) se nada está pré-janela.
    """
    if fb.empty or "competencia" not in fb.columns:
        return fb, 0
    comp = _to_competencia(fb["competencia"])
    pre = comp < inicio
    if not pre.any():
        return fb, 0
    return fb.loc[~pre], int(pre.sum())


def trim_forescisoes_overlap(fr: pd.DataFrame, inicio: str):
    """Apara o mes de overlap (demissao < inicio) do frame de forescisoes.

    Gemea da trim_fobases_overlap: a extracao roda com 1 mes a mais
    pra tras so pra LIQUIDAR os ativos do mes-ancora (demitido de N-1 fora
    de ativos(N-1)). O trim roda antes do upsert — raw fiel a janela; so o
    build de funcionarios ve N-1 (kwarg forescisoes_funcionarios). Retorna
    (df_aparado, n_removidas); no-op (mesmo objeto) se nada pre-janela.
    """
    if fr.empty or "demissao" not in fr.columns:
        return fr, 0
    comp = pd.to_datetime(fr["demissao"], errors="coerce").dt.to_period("M").astype(str)
    pre = comp < inicio
    if not pre.any():
        return fr, 0
    return fr.loc[~pre], int(pre.sum())


def clamp_janela_ctlancto(lan: pd.DataFrame, inicio: str, fim: str):
    """Remove lançamentos com competência fora de [inicio, fim].

    O upsert do raw nunca deleta: runs anteriores com janela mais larga
    deixam lançamentos que dupla-contam com o saldo inicial do corte deste
    run e retroagem a phantom row do balancete. Usa a MESMA
    derivação de competência do transform (_to_competencia) — data
    imparseável vira "NaT", que ordena após 'YYYY-MM' e cai no descarte
    (mesmo destino que teria no mart). Retorna (df_clampado,
    n_descartadas); no-op (mesmo objeto) se nada está fora.
    """
    if lan.empty or "data_lan" not in lan.columns:
        return lan, 0
    comp = _to_competencia(lan["data_lan"])
    fora = (comp < inicio) | (comp > fim)
    if not fora.any():
        return lan, 0
    return lan.loc[~fora], int(fora.sum())


ANO_SUSPEITO_MIN = 2000
ANO_SUSPEITO_MAX = 2100


def contar_anos_suspeitos_ctlancto(lan: pd.DataFrame):
    """Alerta de sanidade, SEM mudar nenhum numero.

    A origem pode ter typos de milenio em `data_lan` (fora de [2000, 2100],
    padrao de digito trocado, ex. "8021" pra 2021).
    Zero efeito no mart: o clamp ja confina o ctlancto a janela do run
    ANTES de qualquer build consumir a tabela, e este contador so LE — nao
    filtra, nao clampa, nao muda dado nenhum. So expoe o que era mudo.

    Retorna (n, (ano_min, ano_max)) — n=0 e anos=None quando nao ha
    suspeito (ou o df/coluna nao existem).
    """
    if lan.empty or "data_lan" not in lan.columns:
        return 0, None
    ano = pd.to_datetime(lan["data_lan"], errors="coerce").dt.year
    suspeito = ano.notna() & ((ano < ANO_SUSPEITO_MIN) | (ano > ANO_SUSPEITO_MAX))
    if not suspeito.any():
        return 0, None
    anos_suspeitos = ano.loc[suspeito]
    return int(suspeito.sum()), (int(anos_suspeitos.min()), int(anos_suspeitos.max()))


def clamp_janela_competencia(df: pd.DataFrame, inicio: str, fim: str):
    """Remove linhas com competência fora de [inicio, fim] (inclusivo).

    No --skip-extract janelado o raw entra FULL enquanto o balancete
    sai clampado à janela — o grid da cascata
    (regime/destacados) cobriria meses fora da janela com numerador truncado
    ('sem_deducao'/valo NULL) e o DELETE-escopado gravaria o lixo por cima
    do par bom no mart. Clampar o df confina o DELETE-escopado à janela do run; nos
    caminhos full e extract janelado é no-op por construção (frames já
    cobrem só a janela). `competencia` aqui é TEXT 'YYYY-MM' normalizado
    pelos builds (_to_competencia) — comparação lexicográfica mês a mês;
    NUNCA comparar com bounds date-style 'YYYY-MM-DD' (exclui meses por
    lexicografia). Retorna (df_clampado, n_fora); no-op (mesmo objeto) se
    nada está fora.
    """
    if df.empty or "competencia" not in df.columns:
        return df, 0
    comp = df["competencia"].astype(str)
    fora = (comp < inicio) | (comp > fim)
    if not fora.any():
        return df, 0
    return df.loc[~fora], int(fora.sum())


def _clampa_marts(marts: dict, inicio: str, fim: str) -> dict:
    """O run COM extract grava SO a janela dele no mart.

    O transform le o raw VIGENTE inteiro, entao produz a historia
    toda. Sem este corte, todo run incremental regravaria o mart inteiro --
    e, entre a migracao pro layout de geracao e o 1o run completo, a historia
    fora da janela ainda e LEGACY. Mart sem `competencia` (empresas) passa inteiro.
    """
    return {nome: clamp_janela_competencia(df, inicio, fim)[0] for nome, df in marts.items()}


class SaldoInicialTotalmenteDescartado(RuntimeError):
    """Todo o si do raw esta em outro corte E ha lancamento na janela.

    Nao e borda: acontece quando uma janela incremental reescreve
    `raw.ctsaldo_inicial` inteiro no corte dela. Seguir daqui grava
    `mart.balancete_mensal` com cumsum de ZERO na carteira inteira.
    Descarte PARCIAL continua sendo aviso: o run canonico precisa rodar
    pra corrigir.
    """


def run_transform(raw: dict, inicio: str, fim: str,
                  fobases_funcionarios: pd.DataFrame | None = None,
                  forescisoes_funcionarios: pd.DataFrame | None = None,
                  report: dict | None = None) -> dict:
    """Aplica transformações e gera os DataFrames de mart.

    fobases_funcionarios: frame de fobases COM o mês de overlap
    (N−1) que ancora o shift de qtd_inicial — só o build de funcionarios o
    consome. None (caminho skip-extract) = usa raw["fobases"] como sempre.
    forescisoes_funcionarios: frame de forescisoes COM a
    demissão do mês de overlap — liquida os ativos do mês-âncora. None =
    usa raw["forescisoes"].
    """
    _step("TRANSFORM -raw -> marts")

    def _get(name):
        return raw.get(name, pd.DataFrame())

    fb_funcionarios = (fobases_funcionarios if fobases_funcionarios is not None
                       else _get("fobases"))
    fr_funcionarios = (forescisoes_funcionarios if forescisoes_funcionarios is not None
                       else _get("forescisoes"))

    # Saldo inicial só vale pro corte == inicio deste run. Linhas de
    # outro corte (--skip-extract com janela diferente, ou linha antiga com
    # data_corte NULL) seriam saldo de abertura ERRADO — descarta com aviso.
    si = raw.get("ctsaldo_inicial", pd.DataFrame())
    if not si.empty and "data_corte" in si.columns:
        corte = inicio_do_mes(inicio)
        stale = si["data_corte"].astype(str) != corte
        if stale.any():
            _log(f"AVISO ctsaldo_inicial: {int(stale.sum())} linha(s) com corte != {corte} descartada(s)")
            if report is not None:
                # empresas com si 100% descartado E lancamentos na janela =
                # candidatas a balancete zero-base —
                # viram ALERTA nominal no resumo de fim de run.
                emps_stale = set(si.loc[stale, "codi_emp"])
                emps_ok = set(si.loc[~stale, "codi_emp"])
                lan = raw.get("ctlancto", pd.DataFrame())
                lan_emps = (set(lan["codi_emp"].unique())
                            if not lan.empty and "codi_emp" in lan.columns else set())
                report["si_descartado"] = {"linhas": int(stale.sum()),
                                           "empresas": len(emps_stale),
                                           "corte": corte}
                candidatas = (emps_stale - emps_ok) & lan_emps
                # Zero-base e CORRETO pra empresa que abriu DENTRO da janela —
                # nao ha lancamento antes do corte pra carregar, cumsum de zero
                # e a resposta certa. So alerta quem tem lancamento ANTES do corte
                # (min(data_lan) < inicio) e mesmo assim ficou sem si.
                if candidatas and not lan.empty and "data_lan" in lan.columns:
                    lan_cand = lan.loc[lan["codi_emp"].isin(candidatas)]
                    comp_lan = _to_competencia(lan_cand["data_lan"])
                    tem_lancto_pre_corte = set(
                        lan_cand.loc[comp_lan < inicio, "codi_emp"])
                else:
                    tem_lancto_pre_corte = set()
                report["si_zumbi_emps"] = sorted(
                    int(x) for x in candidatas & tem_lancto_pre_corte)
            raw["ctsaldo_inicial"] = si.loc[~stale]
            # Descarte TOTAL com movimento na janela nao e aviso, e parada.
            # O que vem depois seria balancete zero-base gravado por cima do bom.
            if not (~stale).any() and not _get("ctlancto").empty:
                raise SaldoInicialTotalmenteDescartado(
                    f"saldo inicial 100% descartado: {int(stale.sum())} "
                    f"linha(s) no raw, NENHUMA no corte {corte} deste run, "
                    f"e ha lancamento na janela. O balancete sairia com "
                    f"cumsum de ZERO. Rode o run completo (SEM "
                    f"--skip-extract) com --inicio {inicio} pra re-extrair "
                    f"o saldo inicial no corte certo.")
    if _get("ctsaldo_inicial").empty and not _get("ctlancto").empty:
        _log("AVISO balancete sera construido SEM saldo inicial (cumsum parte de zero)")
        if report is not None:
            report["sem_si_com_lancto"] = True

    # Alerta de sanidade sobre a ORIGEM, antes de qualquer clamp/filtro —
    # so CONTA e EXPOE, zero mudanca de numero.
    if report is not None:
        n_susp, anos_susp = contar_anos_suspeitos_ctlancto(_get("ctlancto"))
        if n_susp:
            report["ctlancto_anos_suspeitos"] = {
                "linhas": n_susp, "anos": anos_susp}

    # Guard de janela: ctlancto fora de [inicio, fim] dupla-conta
    # com o si do corte e retroage phantom — clampa antes do build.
    lan_clamp, n_fora = clamp_janela_ctlancto(_get("ctlancto"), inicio, fim)
    if n_fora:
        _log(f"AVISO ctlancto: {n_fora} lançamento(s) fora da janela [{inicio}, {fim}] ignorado(s) no transform")
        raw["ctlancto"] = lan_clamp
    if report is not None:
        report["clamp_fora"] = n_fora
    # Simétrico ao aviso acima: saldo inicial presente mas ZERO lançamento na
    # janela (empresa dormente). O balancete fica vazio por design — sinal
    # intencional em vez de falha calada.
    if not _get("ctsaldo_inicial").empty and _get("ctlancto").empty:
        _log("AVISO balancete: saldo inicial presente mas ZERO lançamento na "
             "janela — balancete não produzido (empresa fora do mart)")

    # GRFC: o desembolso da guia soma os tres `*_valor`. Coluna AUSENTE ja
    # degrada pra NULL dentro do build, o que e ALTO e
    # visivel; o caso mudo e o raw PARCIAL — coluna presente porque um
    # re-extract janelado a criou, mas linha antiga com os tres NULL, que
    # somaria zero e subestimaria a guia sem nada na tela. Aqui ele vira
    # ALERTA. Cura: re-extract da foguiagrfc na janela cheia.
    _grfc = _get("foguiagrfc")
    _cols_valor_grfc = ["mes_ant_valor", "resc_valor", "aviso_previo_valor"]
    if not _grfc.empty and all(c in _grfc.columns for c in _cols_valor_grfc):
        n_parcial = int(_grfc[_cols_valor_grfc].isna().all(axis=1).sum())
        if n_parcial and report is not None:
            report["grfc_parcial"] = n_parcial

    def _clamp_cascata(nome: str, df: pd.DataFrame) -> pd.DataFrame:
        # O grid da cascata nasce do efsdoimp/faturamento (FULL no
        # skip-extract) enquanto o balancete é clampado — sem este guard o
        # DELETE-escopado reescreveria pares fora da janela com numerador truncado.
        out, n_fora = clamp_janela_competencia(df, inicio, fim)
        if n_fora:
            _log(f"AVISO {nome}: {n_fora} par(es) fora da janela "
                 f"[{inicio}, {fim}] clampado(s) da cascata")
            if report is not None:
                report.setdefault("cascata_clamp", {})[nome] = n_fora
        return out

    marts: dict = {}
    jobs = [
        # As parcelas de venda de imovel sao a 3a origem de receita (coluna
        # `valo_imobiliario`). Nao e documento fiscal — entra por regime de
        # CAIXA, pelo mes de data_pagamento.
        ("faturamento_mensal",     lambda: build_faturamento_mensal(_get("efsaidas"), _get("efentradas"), _get("efservicos"), _get("efimob_pagamentos"))),
        ("impostos_mensais",       lambda: build_impostos_mensais(_get("efsdoimp"))),
        ("folha_mensal",           lambda: build_folha_mensal(_get("fobases"))),
        # Folha por RUBRICA: mart primario-do-raw, SEM cascata — nao depende
        # de outro mart e nao entra no _clamp_cascata. Base do dash de
        # Detalhamento.
        ("folha_rubricas_mensais", lambda: build_folha_rubricas_mensais(
            _get("fomovto"), _get("foeventos"),
            _get("foclassificacoes_eventos"), _get("foeventosdef"))),
        # GRFC entra por mes da DEMISSAO (join com
        # forescisoes), nao do vencimento -- ver docstring de
        # build_encargos_mensais.
        ("encargos_mensais",       lambda: build_encargos_mensais(
            _get("foguiainss"), _get("fofgtsfilial"), _get("foguiagrfc"),
            _get("forescisoes"))),
        # Fluxo folha-ancorado hibrido — presenca de fobases
        # (fb_funcionarios, com overlap N-1) MENOS demitidos tipo=1 do
        # mes (fr_funcionarios, com o mesmo overlap).
        ("funcionarios_mensal",    lambda: build_funcionarios_mensal(fb_funcionarios, fr_funcionarios, inicio, fim)),
        ("balancete_mensal",       lambda: build_balancete_mensal(_get("ctlancto"), _get("ctcontas"), _get("ctsaldo_inicial"))),
        # Mart primario-do-raw (ctlancto ja clampado no run_transform),
        # SEM cascata — nao depende de outros marts nem de _clamp_cascata.
        ("dre_mensal",             lambda: build_dre_mensal(_get("ctlancto"), _get("ctcontas"))),
        # empresa_regime_periodo DEPENDE de balancete_mensal já estar em `marts`
        # (cascata de detecção nível 3 olha clas_cta '3010201%' do balancete).
        # Os DOIS builds da cascata saem clampados à janela do run:
        # o clamp no regime já confina o grid dos destacados (que herda);
        # o clamp nos destacados é contrato explícito do mart.
        ("empresa_regime_periodo", lambda: _clamp_cascata("empresa_regime_periodo",
            build_empresa_regime_periodo(_get("efsdoimp"), marts.get("balancete_mensal", pd.DataFrame())))),
        # impostos_destacados_mensais DEPENDE de balancete, impostos, regime e
        # faturamento já estarem em `marts` (lógica híbrida).
        ("impostos_destacados_mensais", lambda: _clamp_cascata("impostos_destacados_mensais",
            build_impostos_destacados_mensais(
                marts.get("balancete_mensal", pd.DataFrame()),
                marts.get("impostos_mensais", pd.DataFrame()),
                marts.get("empresa_regime_periodo", pd.DataFrame()),
                marts.get("faturamento_mensal", pd.DataFrame()),
                # 2o componente do complemento: ISS retido SOFRIDO, direto
                # do raw (nao e mart). Entra sob o MESMO portao
                # anti-dupla-contagem da guia.
                build_iss_retido_sofrido(_get("efservicos"),
                                         _get("efservicospar"), report=report),
                report=report,
            ))),
        ("empresas",               lambda: build_empresas(_get("geempre"))),
    ]

    for name, fn in jobs:
        try:
            df = fn()
            marts[name] = df
            _log(f"OK {name:<25} {len(df):>7} linhas")
        except Exception as exc:
            _log(f"ERRO {name:<25} ERRO: {exc}")
            if report is not None:
                report.setdefault("mart_errors", []).append(f"build {name}")

    return marts


def run_load_mart(marts: dict, report: dict | None = None) -> None:
    """Carrega DataFrames transformados no schema mart do PostgreSQL."""
    _step("LOAD MART -memória -> PostgreSQL mart")

    for name, df in marts.items():
        if df.empty:
            _log(f"  {name:<25} vazio -ignorado")
            continue
        key_cols = MART_KEYS.get(name, [])
        # DELETE-escopado por (codi_emp, competencia) nos marts com competencia;
        # mart.empresas (so codi_emp) fica em upsert puro.
        scope_cols = ["codi_emp", "competencia"] if "competencia" in key_cols else None
        try:
            n = upsert_mart(name, df, key_cols, scope_cols=scope_cols)
            _log(f"OK mart.{name:<25} {n:>7} linhas")
            if report is not None:
                report.setdefault("marts", {})[name] = n
        except Exception as exc:
            _log(f"ERRO mart.{name:<25} ERRO: {exc}")
            if report is not None:
                report.setdefault("mart_errors", []).append(f"load {name}")


# ---------------------------------------------------------------------------
# Entrada principal
# ---------------------------------------------------------------------------

def valida_flags(args) -> None:
    """`--saldo-inicial` so tem efeito no caminho COM extract.

    A precedencia nunca foi escrita: ela EMERGE de onde a flag e lida.
    `args.saldo_inicial` chega a UM unico ponto — `run_extract`, no ramo
    `else` do `--skip-extract` — e `run_read_raw` nem tem o parametro.
    Sem esta checagem, `--skip-extract --saldo-inicial always` aceitaria a
    flag, imprimiria "modo: skip-extract" e ignoraria o `always` sem aviso.

    ERRO DURO e nao warning, por dois motivos. A combinacao nao tem uso
    legitimo nenhum: `always` e `never` sao os DOIS inertes no skip, entao
    flag inerte digitada de proposito e sempre mal-entendido, nunca
    escolha. E warning em run longo rola pra fora da tela — pelo botao do
    painel, vai pro log que ninguem abre — enquanto o custo de errar aqui e
    a carteira inteira com balancete zero-base.

    O `auto` default fica ISENTO pra nao quebrar quem nunca passou a flag:
    os scripts e o proprio painel.
    """
    if args.skip_extract and args.saldo_inicial != "auto":
        sys.stderr.write(
            f"ERRO: --saldo-inicial {args.saldo_inicial} nao tem efeito com "
            "--skip-extract (o saldo inicial so e extraido no caminho "
            "completo). Pra renovar o saldo inicial, rode SEM "
            "--skip-extract.\n")
        raise SystemExit(2)
    aceitar = getattr(args, "aceitar_encolhimento", None) or []
    if aceitar and not (getattr(args, "motivo", None) or "").strip():
        sys.stderr.write("ERRO: --aceitar-encolhimento exige --motivo \"texto\": a liberacao "
                         "fica registrada no manifesto da geracao.\n")
        raise SystemExit(2)
    if aceitar and args.skip_extract:
        sys.stderr.write("ERRO: --aceitar-encolhimento nao tem efeito com --skip-extract "
                         "(o skip nao grava geracao nova).\n")
        raise SystemExit(2)


def main() -> None:
    global _t0
    _t0 = time.monotonic()

    args = parse_args()
    valida_flags(args)

    if args.probe_janela:
        # saída = 1 linha JSON no stdout (a UI parseia); nada de banner.
        print(json.dumps(probe_janela(FonteSintetica()), ensure_ascii=False), flush=True)
        return

    empresa = None if args.empresa == "all" else int(args.empresa)
    inicio  = args.inicio
    fim     = args.fim
    iniciado_em = datetime.now()

    print("=" * 60)
    print("  ETL Painel Contábil -> PostgreSQL")
    print(f"  empresa : {args.empresa}")
    print(f"  período : {inicio} -> {fim}")
    print(f"  modo    : {'skip-extract (raw do PostgreSQL)' if args.skip_extract else 'completo'}")
    print("=" * 60)

    report: dict = {"args": {
        "empresa": args.empresa, "inicio": inicio, "fim": fim,
        "modo": "skip-extract" if args.skip_extract else "completo",
    }}

    run_id = None
    lock_conn = None
    try:
        valida_janela(inicio, fim)
        _log("Verificando schemas PostgreSQL...")
        ensure_schemas()
        lock_conn = _adquire_lock_run()
        run_id = _abre_run(report, iniciado_em)
        report["run_id"] = run_id

        if args.skip_extract:
            _checa_detector()
            raw = run_read_raw(empresa, report=report)
        else:
            # Registra o que a origem DECLARAVA no momento do run.
            fonte = FonteSintetica()
            report["fim_declarado"] = fim_declarado_origem(fonte)
            _log(f"origem declara até {report['fim_declarado']}")
            raw = run_extract(empresa, inicio, fim,
                              saldo_inicial_mode=args.saldo_inicial, report=report, fonte=fonte)
            # O overlap N-1 segue aparado antes da carga (raw fiel a janela).
            # A ancora do shift vem do VIGENTE, como no skip-extract.
            raw["fobases"], n_overlap = trim_fobases_overlap(raw.get("fobases", pd.DataFrame()), inicio)
            if n_overlap:
                _log(f"fobases: {n_overlap} linha(s) do mês de overlap aparada(s) antes do upsert")
            report["overlap_trim"] = n_overlap
            raw["forescisoes"], n_ov_fr = trim_forescisoes_overlap(raw.get("forescisoes", pd.DataFrame()), inicio)
            if n_ov_fr:
                _log(f"forescisoes: {n_ov_fr} linha(s) do mês de overlap aparada(s) antes do upsert")
            report["overlap_trim_foresc"] = n_ov_fr
            run_load_raw(raw, inicio, fim, empresa, run_id, report=report,
                         aceitas=frozenset(args.aceitar_encolhimento), motivo=args.motivo)
            # O transform le o VIGENTE publicado -- o MESMO insumo do
            # --skip-extract. Extract e skip ficam iguais por construcao.
            # report=None: o resumo segue contando o que o EXTRACT trouxe, e
            # tabela vazia que nunca virou VIEW nao vira ALERTA falso.
            raw = run_read_raw(empresa)
        marts = run_transform(raw, inicio, fim, report=report)
        if not args.skip_extract:
            marts = _clampa_marts(marts, inicio, fim)
        # Alteracoes retroativas. ANTES = o mart que a tela mostrava; DEPOIS = o que
        # ela passa a mostrar. Os dois LIDOS DO BANCO (run_load_mart grava mart a mart e engole
        # erro, entao o dict em memoria nao diz o que a tela viu). Best-effort: nunca levanta.
        retro = RET.captura_antes(marts, inicio, fim, run_id, iniciado_em, report, log=_log)
        run_load_mart(marts, report=report)
        RET.fecha_comparacao(retro, inicio, fim, run_id, report, report.get("git_commit"), log=_log)

    except KeyboardInterrupt:
        print("\n\n  Interrompido pelo usuário.", file=sys.stderr)
        _fecha_run(run_id, report, "erro")
        sys.exit(1)
    except Exception as exc:
        print(f"\n  ERRO FATAL: {exc}", file=sys.stderr)
        _fecha_run(run_id, report, "erro")
        sys.exit(1)
    finally:
        # Libera o advisory lock e fecha a conexao dedicada em TODO
        # caminho de saida -- sucesso, KeyboardInterrupt, Exception ou o
        # proprio RunConcorrenteError (aqui lock_conn segue None, _libera
        # e no-op). sys.exit() dos except acima levanta SystemExit, que
        # roda o finally antes de propagar.
        _libera_lock_run(lock_conn)

    resumo = format_run_report(report)
    print(resumo, flush=True)
    _espelha_log(resumo)
    rejeitado = bool(report.get("geracoes_rejeitadas") or report.get("fatias_rejeitadas"))
    _fecha_run(run_id, report, _status_final(report))

    total = time.monotonic() - _t0
    print(f"\n{'=' * 60}")
    print(f"  ETL concluído em {total:.1f}s")
    print(f"{'=' * 60}\n")
    if rejeitado:
        sys.exit(3)


if __name__ == "__main__":
    main()
