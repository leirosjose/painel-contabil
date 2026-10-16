"""Alteracoes retroativas -- unitarios do etl/retroativas.py.

Sem banco: compara, causa, R0, git_commit sem
subprocess, guards de source e a linha do resumo de fim de run. A parte com
banco vive em tests/test_retroativas_db.py.
"""
import ast
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

import retroativas as R

RAIZ = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# centavos / mes da carga / compara
# ---------------------------------------------------------------------------

class TestCentavos:
    def test_meio_centavo_arredonda_pra_longe_do_zero_como_o_postgres(self):
        # float binario: round(1.005, 2) == 1.0. O numeric do Postgres da 1.01.
        assert R.centavos(1.005) == 101
        assert R.centavos(-1.005) == -101

    def test_arredonda_a_2_casas(self):
        assert R.centavos(10.004) == 1000
        assert R.centavos(10.996) == 1100

    def test_nulo_e_nan_viram_none(self):
        assert R.centavos(None) is None
        assert R.centavos(float("nan")) is None


class TestMesDaCarga:
    def test_mes_da_data_de_inicio_do_run(self):
        assert R.mes_da_carga(datetime(2026, 9, 24, 23, 59)) == "2026-09"
        assert R.mes_da_carga(datetime(2027, 1, 1, 0, 0)) == "2027-01"


def _k(emp, comp, num):
    return (emp, comp, num)


class TestCompara:
    def test_abaixo_de_1_real_fica_fora(self):
        d = R.compara({_k(1, "2025-01", "NFe"): 100.00}, {_k(1, "2025-01", "NFe"): 100.99}, "2026-09")
        assert d == []

    def test_exatamente_1_real_entra(self):
        d = R.compara({_k(1, "2025-01", "NFe"): 100.00}, {_k(1, "2025-01", "NFe"): 101.00}, "2026-09")
        assert [x.dif_c for x in d] == [100]

    def test_menos_1_real_entra(self):
        d = R.compara({_k(1, "2025-01", "NFe"): 101.00}, {_k(1, "2025-01", "NFe"): 100.00}, "2026-09")
        assert [x.dif_c for x in d] == [-100]

    def test_armadilha_do_float_10_01_menos_9_01_e_exatamente_1(self):
        # 10.01 - 9.01 = 0.9999999999999996 em float: a conta tem de ser em centavos.
        d = R.compara({_k(1, "2025-01", "NFe"): 9.01}, {_k(1, "2025-01", "NFe"): 10.01}, "2026-09")
        assert [x.dif_c for x in d] == [100]

    def test_arredonda_cada_lado_ANTES_da_subtracao(self):
        # 10.004 -> 10.00 e 10.996 -> 11.00: diferenca 1.00 (entra). Sem arredondar
        # antes seria 0.992 (ficaria fora).
        d = R.compara({_k(1, "2025-01", "NFe"): 10.004}, {_k(1, "2025-01", "NFe"): 10.996}, "2026-09")
        assert [(x.antes_c, x.depois_c, x.dif_c) for x in d] == [(1000, 1100, 100)]

    def test_lado_ausente_vale_zero_e_guarda_none(self):
        d = R.compara({}, {_k(1, "2025-01", "Outras receitas"): 500.0}, "2026-09")
        assert [(x.antes_c, x.depois_c, x.dif_c) for x in d] == [(None, 50000, 50000)]
        d = R.compara({_k(1, "2025-01", "NFe"): 500.0}, {}, "2026-09")
        assert [(x.antes_c, x.depois_c, x.dif_c) for x in d] == [(50000, None, -50000)]

    def test_ausente_contra_zero_nao_e_diferenca(self):
        assert R.compara({}, {_k(1, "2025-01", "NFe"): 0.0}, "2026-09") == []
        assert R.compara({_k(1, "2025-01", "NFe"): float("nan")}, {}, "2026-09") == []

    def test_mes_da_carga_fica_fora_e_o_anterior_entra(self):
        a = {_k(1, "2026-08", "NFe"): 0.0, _k(1, "2026-09", "NFe"): 0.0}
        b = {_k(1, "2026-08", "NFe"): 50.0, _k(1, "2026-09", "NFe"): 50.0}
        assert [x.competencia for x in R.compara(a, b, "2026-09")] == ["2026-08"]

    def test_virada_de_ano_carga_em_janeiro_inclui_dezembro(self):
        a = {_k(1, "2026-12", "NFe"): 0.0, _k(1, "2027-01", "NFe"): 0.0}
        b = {_k(1, "2026-12", "NFe"): 50.0, _k(1, "2027-01", "NFe"): 50.0}
        assert [x.competencia for x in R.compara(a, b, "2027-01")] == ["2026-12"]

    def test_ordem_estavel_por_empresa_competencia_numero(self):
        a = {}
        b = {_k(2, "2025-02", "NFe"): 9.0, _k(1, "2025-03", "NFe"): 9.0, _k(1, "2025-03", "Folha"): 9.0,
             _k(1, "2025-01", "NFe"): 9.0}
        d = R.compara(a, b, "2026-09")
        assert [(x.codi_emp, x.competencia, x.numero) for x in d] == [
            (1, "2025-01", "NFe"), (1, "2025-03", "Folha"), (1, "2025-03", "NFe"), (2, "2025-02", "NFe")]


# ---------------------------------------------------------------------------
# fontes / causa
# ---------------------------------------------------------------------------

def man(pub=None, pedidos=None, legacy=()):
    return R.Manifesto(pub=pub or {}, pedidos=pedidos or [], legacy=frozenset(legacy))


TODAS = (None, 202301, 202612)      # pedido de carteira inteira, janela 2023-01..2026-12


class TestFontes:
    def test_faturamento_por_mes(self):
        # O NFe e liquido de devolucao de venda
        # (efentradas, mes da entrada) -- devolucao retroativa em mes fechado
        # tem de cair em 'mudou no Dominio', nao em 'inesperado'.
        assert R.fontes("NFe", 234, "2025-10") == [
            ("efsaidas", 234, 202510), ("efentradas", 234, 202510)]
        assert R.fontes("NFS-e", 27, "2025-11") == [("efservicos", 27, 202511)]
        assert R.fontes("Outras receitas", 9, "2025-03") == [("efimob_pagamentos", 9, 202503)]

    def test_total_e_a_uniao_das_tres(self):
        assert set(R.fontes("Total", 5, "2025-03")) == {
            ("efsaidas", 5, 202503), ("efentradas", 5, 202503),
            ("efservicos", 5, 202503), ("efimob_pagamentos", 5, 202503)}

    def test_impostos_tem_ctlancto_dos_3_meses_seguintes_cruzando_o_ano(self):
        f = R.fontes("Impostos", 7, "2025-11")
        assert [x for x in f if x[0] == "ctlancto"] == [
            ("ctlancto", 7, 202511), ("ctlancto", 7, 202512), ("ctlancto", 7, 202601), ("ctlancto", 7, 202602)]
        assert ("efsdoimp", 7, 202511) in f and ("efservicos", 7, 202511) in f
        assert ("efservicospar", 7, 202511) in f and ("ctcontas", 7, 0) in f

    def test_folha_grfc_do_mes_e_do_seguinte_e_dicionarios_globais_na_empresa_0(self):
        f = R.fontes("Folha", 7, "2025-12")
        assert ("foguiagrfc", 7, 202512) in f and ("foguiagrfc", 7, 202601) in f
        assert ("foeventos", 7, 0) in f
        assert ("foeventosdef", 0, 0) in f and ("foclassificacoes_eventos", 0, 0) in f
        for t in ("fomovto", "foguiainss", "fofgtsfilial", "fobases", "forescisoes"):
            assert (t, 7, 202512) in f


class TestClassifica:
    def test_1293_fatia_com_prev_row_count_zero_mas_JA_PEDIDA_e_mudou_no_dominio(self):
        # caso medido que derruba a regra ingenua (prev_row_count = 0 => carga nova)
        m = man(pub={("efsdoimp", 226, 202509): [17]},
                pedidos=[(12, "efsdoimp", *TODAS), (13, "efsdoimp", *TODAS), (17, "efsdoimp", *TODAS)])
        c = R.classifica("Impostos", 226, "2025-09", 6969_26, m, 16, "abc", "abc")
        assert c.causa == "mudou no Dominio"
        assert "efsdoimp 226 202509" in c.fontes and "run 17" in c.fontes

    def test_efimob_sem_pedido_anterior_e_carga_nova(self):
        m = man(pub={("efimob_pagamentos", 9, 202503): [18]},
                pedidos=[(17, "efsdoimp", *TODAS), (18, "efimob_pagamentos", *TODAS)])
        c = R.classifica("Outras receitas", 9, "2025-03", 1500_00, m, 17, "abc", "abc")
        assert c.causa == "carga nova"

    def test_fatia_na_geracao_legacy_conta_como_ja_coberta(self):
        m = man(pub={("efsaidas", 234, 202510): [17]}, legacy=[("efsaidas", 234, 202510)])
        c = R.classifica("NFe", 234, "2025-10", 114537_00, m, 16, "abc", "abc")
        assert c.causa == "mudou no Dominio"

    def test_pedido_do_MESMO_run_que_publicou_nao_conta_como_cobertura_previa(self):
        m = man(pub={("efimob_pagamentos", 9, 202503): [18]}, pedidos=[(18, "efimob_pagamentos", *TODAS)])
        assert R.classifica("Outras receitas", 9, "2025-03", 100_00, m, 17, None, None).causa == "carga nova"

    def test_pedido_de_outra_empresa_ou_fora_da_faixa_nao_cobre(self):
        m = man(pub={("efsaidas", 234, 202510): [17]},
                pedidos=[(12, "efsaidas", 999, 202301, 202612),       # outra empresa
                         (13, "efsaidas", None, 202601, 202612)])     # fatia fora da faixa
        assert R.classifica("NFe", 234, "2025-10", 100_00, m, 16, None, None).causa == "carga nova"

    def test_pedido_sem_limite_inferior_cobre_como_foempregados_ate_fim(self):
        m = man(pub={("efsaidas", 234, 202510): [17]}, pedidos=[(12, "efsaidas", None, None, 202612)])
        assert R.classifica("NFe", 234, "2025-10", 100_00, m, 16, None, None).causa == "mudou no Dominio"

    def test_fatia_publicada_em_run_ate_R0_nao_conta(self):
        # publicada pelo run 15 e R0 = 15: o mart daquele run ja a absorveu.
        m = man(pub={("efsaidas", 234, 202510): [15]}, pedidos=[(12, "efsaidas", *TODAS)])
        c = R.classifica("NFe", 234, "2025-10", 100_00, m, 15, "novo", "velho")
        assert c.causa == "mudou o calculo"

    def test_R0_pulando_run_parcial_enxerga_a_fatia_do_parcial(self):
        # run 16 fechou 'parcial' (nao e R0); R0 = 15. A fatia publicada pelo 16 entra.
        m = man(pub={("efsaidas", 234, 202510): [16]}, pedidos=[(12, "efsaidas", *TODAS)])
        assert R.classifica("NFe", 234, "2025-10", 100_00, m, 15, "abc", "abc").causa == "mudou no Dominio"
        # com R0 = 16 (o 16 fechou ok) a mesma fatia nao conta mais
        assert R.classifica("NFe", 234, "2025-10", 100_00, m, 16, "abc", "abc").causa == "inesperado"

    def test_cobertura_e_avaliada_no_PRIMEIRO_run_que_publicou_apos_R0(self):
        m = man(pub={("efsaidas", 234, 202510): [16, 17]}, pedidos=[(16, "efsaidas", *TODAS)])
        assert R.classifica("NFe", 234, "2025-10", 100_00, m, 15, None, None).causa == "carga nova"

    def test_total_misto_com_as_duas_partes(self):
        m = man(pub={("efsaidas", 234, 202510): [17], ("efimob_pagamentos", 234, 202510): [18]},
                pedidos=[(12, "efsaidas", *TODAS), (17, "efsaidas", *TODAS), (18, "efimob_pagamentos", *TODAS)])
        comp = {"NFe": 100_00, "NFS-e": 0, "Outras receitas": 500_00}
        c = R.classifica("Total", 234, "2025-10", 600_00, m, 16, "abc", "abc", componentes=comp)
        assert c.causa == "mudou no Dominio + carga nova"
        assert (c.parte_dominio_c, c.parte_carga_nova_c) == (100_00, 500_00)

    def test_total_so_com_componente_de_dominio_nao_e_misto_mesmo_com_efimob_publicada(self):
        m = man(pub={("efsaidas", 234, 202510): [17], ("efimob_pagamentos", 234, 202510): [18]},
                pedidos=[(12, "efsaidas", *TODAS), (18, "efimob_pagamentos", *TODAS)])
        comp = {"NFe": 100_00, "NFS-e": 0, "Outras receitas": 0}
        c = R.classifica("Total", 234, "2025-10", 100_00, m, 16, None, None, componentes=comp)
        assert c.causa == "mudou no Dominio"
        assert c.parte_dominio_c is None and c.parte_carga_nova_c is None

    def test_outro_mes_da_janela_ramo_d(self):
        # Impostos de jul/25 muda sem fatia publicada em jul, mas efsdoimp de mar/25
        # (mesmo ano, mesma empresa) foi publicada: portao por janela.
        m = man(pub={("efsdoimp", 7, 202503): [17]}, pedidos=[(12, "efsdoimp", *TODAS)])
        c = R.classifica("Impostos", 7, "2025-07", 50_00, m, 16, "abc", "abc")
        assert c.causa == "mudou no Dominio"
        assert "outro mes da janela" in c.fontes

    def test_outro_mes_de_OUTRO_ano_nao_conta(self):
        m = man(pub={("efsdoimp", 7, 202403): [17]}, pedidos=[(12, "efsdoimp", *TODAS)])
        assert R.classifica("Impostos", 7, "2025-07", 50_00, m, 16, "abc", "abc").causa == "inesperado"

    def test_nada_publicado_commit_diferente_e_mudou_o_calculo(self):
        c = R.classifica("Folha", 7, "2025-07", 50_00, man(), 16, "bbbbbbb", "aaaaaaa")
        assert c.causa == "mudou o calculo" and "aaaaaaa" in c.fontes and "bbbbbbb" in c.fontes

    def test_nada_publicado_commit_do_R0_desconhecido_e_mudou_o_calculo(self):
        c = R.classifica("Folha", 7, "2025-07", 50_00, man(), 16, "bbbbbbb", None)
        assert c.causa == "mudou o calculo" and c.fontes == "commit anterior desconhecido"

    def test_nada_publicado_commits_iguais_ou_nulos_e_inesperado(self):
        assert R.classifica("Folha", 7, "2025-07", 50_00, man(), 16, "aaaaaaa", "aaaaaaa").causa == "inesperado"
        assert R.classifica("Folha", 7, "2025-07", 50_00, man(), 16, None, None).causa == "inesperado"
        assert R.classifica("Folha", 7, "2025-07", 50_00, man(), 16, None, "aaaaaaa").causa == "inesperado"


class TestR0DoPar:
    RUNS = [
        R.RunInfo(15, "all", "2023-01", "2026-12", "a"),
        R.RunInfo(16, "all", "2026-01", "2026-09", "b"),
        R.RunInfo(17, "201", "2025-01", "2025-12", "c"),
    ]

    def test_maior_run_que_cobre_a_competencia(self):
        assert R.r0_do_par(self.RUNS, 999, "2025-06").run_id == 15
        assert R.r0_do_par(self.RUNS, 999, "2026-03").run_id == 16

    def test_run_de_uma_empresa_so_cobre_essa_empresa(self):
        assert R.r0_do_par(self.RUNS, 201, "2025-06").run_id == 17
        assert R.r0_do_par(self.RUNS, 999, "2025-06").run_id == 15

    def test_nenhum_cobre_devolve_none(self):
        assert R.r0_do_par(self.RUNS, 999, "2022-12") is None


# ---------------------------------------------------------------------------
# git_commit sem subprocess
# ---------------------------------------------------------------------------

SHA = "0123456789abcdef0123456789abcdef01234567"


def _git(tmp_path, head, refs=None, packed=None):
    g = tmp_path / ".git"
    g.mkdir()
    (g / "HEAD").write_text(head)
    for nome, conteudo in (refs or {}).items():
        p = g / nome
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(conteudo)
    if packed is not None:
        (g / "packed-refs").write_text(packed)
    return tmp_path


class TestGitCommit:
    def test_ref_solto(self, tmp_path):
        raiz = _git(tmp_path, "ref: refs/heads/main\n", refs={"refs/heads/main": SHA + "\n"})
        assert R.git_commit_curto(raiz) == SHA[:7]

    def test_ref_em_packed_refs(self, tmp_path):
        raiz = _git(tmp_path, "ref: refs/heads/main\n",
                    packed=f"# pack-refs with: peeled\n{SHA} refs/heads/main\nffffffffffffffffffffffffffffffffffffffff refs/heads/x\n")
        assert R.git_commit_curto(raiz) == SHA[:7]

    def test_head_destacado(self, tmp_path):
        raiz = _git(tmp_path, SHA + "\n")
        assert R.git_commit_curto(raiz) == SHA[:7]

    def test_sem_git_devolve_none(self, tmp_path):
        assert R.git_commit_curto(tmp_path) is None

    def test_ref_que_nao_existe_devolve_none(self, tmp_path):
        raiz = _git(tmp_path, "ref: refs/heads/sumiu\n")
        assert R.git_commit_curto(raiz) is None

    def test_lixo_no_head_devolve_none(self, tmp_path):
        assert R.git_commit_curto(_git(tmp_path, "isto nao e sha\n")) is None

    def test_nunca_chama_subprocess(self, tmp_path, monkeypatch):
        import subprocess

        def boom(*a, **k):
            raise AssertionError("subprocess chamado")
        monkeypatch.setattr(subprocess, "run", boom)
        monkeypatch.setattr(subprocess, "Popen", boom)
        raiz = _git(tmp_path, "ref: refs/heads/main\n", refs={"refs/heads/main": SHA})
        assert R.git_commit_curto(raiz) == SHA[:7]
        importados = set()
        for n in ast.walk(_fonte_ast()):
            if isinstance(n, ast.Import):
                importados |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                importados.add(n.module or "")
        assert not importados & {"subprocess", "os"}

    def test_no_repo_real_devolve_sha_curto_de_7_hex(self):
        v = R.git_commit_curto(RAIZ)
        assert v is None or re.fullmatch(r"[0-9a-f]{7}", v)


# ---------------------------------------------------------------------------
# Guards de source
# ---------------------------------------------------------------------------

def _fonte_ast():
    return ast.parse((RAIZ / "etl" / "retroativas.py").read_text(encoding="utf-8"))


class TestGuardsDeSource:
    def test_modulo_e_append_only_sem_update_nem_delete_em_sql(self):
        # varre toda string do modulo (SQL incluida) -- nada de UPDATE/DELETE
        textos = [n.value for n in ast.walk(_fonte_ast()) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        ruins = [t[:60] for t in textos if re.search(r"\b(UPDATE|DELETE)\b", t, re.I)]
        assert ruins == []

    def test_modulo_nao_importa_etl_main(self):
        nomes = []
        for n in ast.walk(_fonte_ast()):
            if isinstance(n, ast.Import):
                nomes += [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                nomes.append(n.module or "")
        assert not [x for x in nomes if x in ("main", "etl.main")]

    def test_constantes_do_contrato(self):
        assert R.NUMEROS == ("NFe", "NFS-e", "Outras receitas", "Total", "Impostos", "Folha")
        assert R.CAUSAS == ("mudou no Dominio", "carga nova", "mudou no Dominio + carga nova",
                            "mudou o calculo", "inesperado")
        assert R.LIMIAR_CENTAVOS == 100


# ---------------------------------------------------------------------------
# Resumo de fim de run (etl/main.format_run_report)
# ---------------------------------------------------------------------------

def _report_limpo():
    return {
        "args": {"empresa": "all", "inicio": "2025-01", "fim": "2025-03", "modo": "completo"},
        "extract": {"fobases": 1947}, "extract_errors": [],
        "raw_upsert": {"fobases": 1166}, "dedup_delta": {},
        "marts": {"folha_mensal": 10}, "mart_errors": [],
    }


def _novas(r):
    import main as m
    base = m.format_run_report(_report_limpo()).splitlines()
    com = m.format_run_report(r).splitlines()
    return [lin for lin in com if lin not in base], com


class TestResumoDoRun:
    def test_linha_com_total_e_mudou_no_dominio(self):
        r = _report_limpo()
        r["retroativas"] = {"total": 12, "dominio": 3, "calculo": 9, "inesperado": 0}
        novas, com = _novas(r)
        assert len(novas) == 1
        assert "12 alteracoes retroativas (3 mudou no Dominio)" in novas[0]
        assert "ALERTA" not in "\n".join(com)

    def test_inesperado_vira_alerta(self):
        r = _report_limpo()
        r["retroativas"] = {"total": 5, "dominio": 0, "calculo": 3, "inesperado": 2}
        _, com = _novas(r)
        alerta = [lin for lin in com if "ALERTA" in lin]
        assert len(alerta) == 1 and "inesperado" in alerta[0] and "2" in alerta[0]
        # alertas ficam por ultimo
        assert com.index(alerta[0]) > max(i for i, lin in enumerate(com) if "alteracoes retroativas" in lin)

    def test_falha_na_comparacao_vira_alerta_e_nao_derruba(self):
        r = _report_limpo()
        r["retroativas_erro"] = "boom"
        _, com = _novas(r)
        assert any("ALERTA" in lin and "alteracoes retroativas" in lin and "boom" in lin for lin in com)

    def test_comparacao_pulada_sem_run_anterior_e_so_uma_linha(self):
        r = _report_limpo()
        r["retroativas_pulada"] = "sem run anterior"
        novas, com = _novas(r)
        assert len(novas) == 1 and "pulada" in novas[0] and "sem run anterior" in novas[0]
        assert "ALERTA" not in "\n".join(com)

    def test_sem_a_chave_nada_muda(self):
        novas, _ = _novas(_report_limpo())
        assert novas == []

    def test_nenhuma_linha_nova_casa_com_o_OK_que_o_progresso_do_app_conta(self):
        # app.py: _OK_LINE_RE = r"OK " conta linha contra _PIPELINE_OK_TOTAL = 38
        import main as m
        casos = [
            {"retroativas": {"total": 12, "dominio": 3, "calculo": 9, "inesperado": 2}},
            {"retroativas_erro": "algo OK falhou"},
            {"retroativas_pulada": "sem run anterior"},
        ]
        for extra in casos:
            r = _report_limpo()
            r.update(extra)
            novas, _ = _novas(r)
            assert novas and not [lin for lin in novas if re.search(r"OK ", lin)], novas
        for msg in (R.linha_resumo({"total": 1, "dominio": 1, "calculo": 0, "inesperado": 0}),
                    R.MSG_PULADA_SEM_RUN, R.MSG_INICIO_ANTES, R.MSG_FIM_DEPOIS):
            assert not re.search(r"OK ", msg), msg
        assert m  # import usado


# ---------------------------------------------------------------------------
# Gancho no main(): sem este teste, tirar captura_antes ou
# fecha_comparacao do main.py passava a suite inteira.
# ---------------------------------------------------------------------------

class _Cnx:
    def close(self):
        pass


def _main_com_tudo_dublado(monkeypatch, chamadas):
    """main() por chamada direta de funcao (nada de CLI): so o gancho e o load ficam reais
    na ORDEM; extract, transform e banco sao dublados."""
    import main as m

    marts = {"faturamento_mensal": pd.DataFrame({"codi_emp": [1], "competencia": ["2025-03"]})}
    monkeypatch.setattr("sys.argv", ["main.py", "--skip-extract", "--empresa", "all",
                                     "--inicio", "2025-01", "--fim", "2025-03"])
    monkeypatch.setattr(m, "ensure_schemas", lambda: None)
    monkeypatch.setattr(m, "_adquire_lock_run", lambda: _Cnx())
    monkeypatch.setattr(m, "_libera_lock_run", lambda c: None)

    def abre(report, iniciado_em):
        report["git_commit"] = "abc1234"
        return 7
    monkeypatch.setattr(m, "_abre_run", abre)
    monkeypatch.setattr(m, "_checa_detector", lambda: None)
    monkeypatch.setattr(m, "run_read_raw", lambda *a, **k: {})
    monkeypatch.setattr(m, "run_transform", lambda *a, **k: marts)
    monkeypatch.setattr(m, "_fecha_run", lambda *a, **k: chamadas.append(("fecha_run",)))
    monkeypatch.setattr(m, "_espelha_log", lambda r: None)
    monkeypatch.setattr(m, "run_load_mart", lambda mm, report=None: chamadas.append(("run_load_mart", mm)))

    def antes(mm, inicio, fim, run_id, iniciado_em, report, log=None):
        chamadas.append(("captura_antes", mm, inicio, fim, run_id))
        return {"estado": "do-antes"}

    def depois(estado, inicio, fim, run_id, report, commit, r0_teto=None, log=None):
        chamadas.append(("fecha_comparacao", estado, inicio, fim, run_id, commit))
    monkeypatch.setattr(m.RET, "captura_antes", antes)
    monkeypatch.setattr(m.RET, "fecha_comparacao", depois)
    return m, marts


class TestGanchoNoMain:
    def test_captura_antes_do_load_e_fecha_comparacao_depois_na_ordem(self, monkeypatch):
        chamadas = []
        m, marts = _main_com_tudo_dublado(monkeypatch, chamadas)
        m.main()
        nomes = [c[0] for c in chamadas]
        assert nomes.index("captura_antes") < nomes.index("run_load_mart") < nomes.index("fecha_comparacao")
        assert ("captura_antes", marts, "2025-01", "2025-03", 7) in chamadas
        # o DEPOIS recebe o estado do ANTES, o run_id e o commit gravado no _abre_run
        assert ("fecha_comparacao", {"estado": "do-antes"}, "2025-01", "2025-03", 7, "abc1234") in chamadas

    def test_falha_no_gancho_nao_muda_o_fecho_do_run(self, monkeypatch):
        chamadas = []
        m, _ = _main_com_tudo_dublado(monkeypatch, chamadas)
        monkeypatch.setattr(m.RET, "captura_antes", lambda *a, **k: None)   # antes indisponivel
        m.main()
        assert ("fecha_run",) in chamadas and any(c[0] == "run_load_mart" for c in chamadas)


# ---------------------------------------------------------------------------
# O run seguinte avisa quando o anterior morreu depois de publicar
# ---------------------------------------------------------------------------

AVISO_RUN_MORTO = ("run 2 anterior terminou em 'executando' depois de publicar -- "
                   "alteracoes daquele run podem nao ter sido comparadas")


class TestAvisoRunAnteriorMorto:
    def test_texto_do_aviso_e_exato_e_sem_OK(self):
        assert R.aviso_run_morto(2, "executando") == AVISO_RUN_MORTO
        assert R.aviso_run_morto(9, "erro") == AVISO_RUN_MORTO.replace("run 2", "run 9").replace(
            "'executando'", "'erro'")
        assert not re.search(r"OK ", R.aviso_run_morto(2, "executando"))

    def test_aviso_vira_alerta_no_resumo_e_fica_por_ultimo(self):
        r = _report_limpo()
        r["retroativas_aviso"] = AVISO_RUN_MORTO
        novas, com = _novas(r)
        alerta = [ln for ln in com if "ALERTA" in ln]
        assert len(alerta) == 1 and AVISO_RUN_MORTO in alerta[0]
        assert not [ln for ln in novas if re.search(r"OK ", ln)]
        assert "sem alertas" not in "\n".join(com).lower()
