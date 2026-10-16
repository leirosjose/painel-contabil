"""Guards de etl/queries.py — os helpers puros, funcao pura.

O SQL em si nao tem teste aqui (precisaria de banco, marker `db`); o que se
testa e a parte que da errado em silencio: a contagem de placeholders, o
alargamento da janela do passivo de IRPJ/CSLL e o criterio de visibilidade
da carteira.
"""
import pandas as pd

from etl.mappings import CLAS_PASSIVO_IRPJ_CSLL
from etl.queries import (
    SQL_COBERTURA_EMPRESAS,
    SQL_PASSIVO_IRPJ_CSLL,
    carteira_visivel,
    params_cobertura,
    params_passivo_ir,
)


class TestParamsCobertura:
    def test_um_par_por_placeholder(self):
        p = params_cobertura("2025-01", "2025-12")
        assert len(p) == SQL_COBERTURA_EMPRESAS.count("%s")
        assert set(p) == {"2025-01", "2025-12"}


class TestParamsPassivoIr:
    def test_janela_alarga_tres_meses(self):
        # A razao de ser do helper: o eixo do fallback e a COMPETENCIA DO
        # TRIMESTRE, e a quota de jan-mar liquida o T4 do ano ANTERIOR. Sem o
        # +3 o dezembro da janela sai vazio - e sai em SILENCIO, com um numero
        # menor porem plausivel.
        assert params_passivo_ir([1], "2025-01", "2025-12")[2] == "2026-03"
        assert params_passivo_ir([1], "2024-01", "2024-12")[2] == "2025-03"

    def test_rollover_de_dezembro_em_qualquer_mes(self):
        for fim, esperado in (("2025-09", "2025-12"), ("2025-10", "2026-01"),
                              ("2025-11", "2026-02"), ("2025-01", "2025-04")):
            assert params_passivo_ir([1], "2025-01", fim)[2] == esperado

    def test_inicio_nao_e_alargado(self):
        # So o FIM anda: alargar o inicio traria quota de trimestre anterior a
        # janela pedida, inflando o numerador.
        assert params_passivo_ir([1], "2025-03", "2025-12")[1] == "2025-03"

    def test_ids_viram_lista_de_int(self):
        # psycopg2 so mapeia LISTA pra ARRAY no "= ANY(%s)"; tupla nao serve.
        assert params_passivo_ir((7, 9), "2025-01", "2025-12")[0] == [7, 9]
        assert params_passivo_ir(7, "2025-01", "2025-12")[0] == [7]

    def test_classes_vem_do_mappings(self):
        # Nunca hardcodar as contas aqui: elas sao criterio, e o criterio mora
        # no mappings (clas INTERSECAO nome, com o RE_PASSIVO_FORA).
        assert params_passivo_ir([1], "2025-01", "2025-12")[3] == list(
            CLAS_PASSIVO_IRPJ_CSLL)

    def test_um_parametro_por_placeholder(self):
        assert len(params_passivo_ir([1], "2025-01", "2025-12")) == \
            SQL_PASSIVO_IRPJ_CSLL.count("%s")


def _linha(codi_emp, ativa, cgce, tem_fat=False, tem_imp=False,
           tem_func=False, tem_folha=False, tem_bal=False):
    return {
        "codi_emp": codi_emp, "nome_emp": f"EMPRESA {codi_emp}",
        "cgce_emp": cgce, "ativa": ativa, "tem_fat": tem_fat,
        "tem_imp": tem_imp, "tem_func": tem_func, "tem_folha": tem_folha,
        "tem_bal": tem_bal,
    }


def _visiveis(*linhas):
    return set(carteira_visivel(pd.DataFrame(list(linhas)))["codi_emp"])


class TestCarteiraVisivel:
    """O criterio de quem aparece no dropdown e no lote de PDF.

    Inativacao e fato cadastral de HOJE e nao apaga o faturamento de ontem —
    inativa com dado no periodo CONTINUA na carteira, so com documento valido.
    """

    def test_ativa_sem_dado_entra(self):
        # Ativa e ativa: a carteira nao depende de ela ter movimento.
        assert _visiveis(_linha(1, True, "82381183000105")) == {1}

    def test_inativa_com_dado_e_cnpj_entra(self):
        assert _visiveis(_linha(213, False, "89039653000279", tem_fat=True)) == {213}

    def test_inativa_com_dado_e_cpf_entra(self):
        # 11 digitos e CPF: autonomo e cliente real, nao lixo de cadastro.
        assert _visiveis(_linha(291, False, "52918460731", tem_folha=True)) == {291}

    def test_inativa_com_dado_sem_documento_sai(self):
        # Registro sem documento nao e pessoa juridica nem fisica, entao nao
        # e carteira.
        assert _visiveis(_linha(292, False, None, tem_folha=True)) == set()

    def test_inativa_com_dado_e_documento_de_12_digitos_sai(self):
        # 12 digitos nao e CPF nem CNPJ: estruturalmente invalido.
        assert _visiveis(_linha(7, False, "123456789012", tem_fat=True)) == set()

    def test_inativa_sem_dado_sai(self):
        # O outro lado do criterio: janela que nao alcanca a empresa a esconde.
        assert _visiveis(_linha(235, False, "76352993000203")) == set()

    def test_documento_pontuado_entra(self):
        # Quem normaliza os dois lados e o only_digits, igual a busca por CNPJ.
        assert _visiveis(
            _linha(2, False, "89.039.653/0002-79", tem_bal=True)) == {2}

    def test_qualquer_um_dos_cinco_marts_conta_como_dado(self):
        for flag in ("tem_fat", "tem_imp", "tem_func", "tem_folha", "tem_bal"):
            assert _visiveis(
                _linha(3, False, "82381183000105", **{flag: True})) == {3}, flag

    def test_preserva_as_colunas_e_a_ordem_das_linhas(self):
        # O consumer segue fazendo groupby/sort por conta — o helper so FILTRA.
        df = pd.DataFrame([_linha(9, True, "82381183000105"),
                           _linha(292, False, None, tem_fat=True),
                           _linha(5, True, "45810689000159")])
        out = carteira_visivel(df)
        assert list(out.columns) == list(df.columns)
        assert list(out["codi_emp"]) == [9, 5]

    def test_carteira_vazia_nao_estoura(self):
        vazio = pd.DataFrame(columns=list(_linha(1, True, "1").keys()))
        assert carteira_visivel(vazio).empty


class TestGuardDaFonteUnica:
    """O que pega o consumer futuro que esquecer o filtro.

    O `WHERE e.stat_emp IS DISTINCT FROM 'I'` saiu do SQL, entao a query
    devolve a carteira INTEIRA — quem nao chamar `carteira_visivel` exibe as
    inativas. Sao 2 consumers e os dois estao cobertos aqui.
    """

    def test_o_sql_nao_filtra_mais_a_carteira(self):
        # Voltar um WHERE pro corpo principal esconderia inativas com dado em
        # silencio. Os WHERE dos EXISTS ficam ANTES do FROM da tabela base.
        depois_do_from = SQL_COBERTURA_EMPRESAS.split("FROM mart.empresas e")[1]
        assert "WHERE" not in depois_do_from.upper()

    def test_stat_emp_so_aparece_na_derivacao_de_ativa(self):
        ocorrencias = SQL_COBERTURA_EMPRESAS.count("stat_emp")
        assert ocorrencias == 1, "stat_emp voltou a filtrar em vez de so derivar"
        assert "(e.stat_emp IS DISTINCT FROM 'I') AS ativa" in SQL_COBERTURA_EMPRESAS

    def test_o_sql_expoe_a_coluna_ativa(self):
        # `carteira_visivel` e a marcacao dependem dela.
        assert "AS ativa" in SQL_COBERTURA_EMPRESAS

    def test_os_dois_consumers_chamam_o_helper(self):
        from pathlib import Path
        raiz = Path(__file__).resolve().parent.parent
        for arquivo in ("app.py", "pdf_generator.py"):
            fonte = (raiz / arquivo).read_text(encoding="utf-8")
            assert "SQL_COBERTURA_EMPRESAS" in fonte, arquivo
            assert "carteira_visivel(" in fonte, (
                f"{arquivo} consome a carteira sem passar por carteira_visivel"
            )

    def test_params_cobertura_segue_pareado_apos_a_coluna_nova(self):
        # A coluna `ativa` nao tem placeholder; o helper conta os %s sozinho.
        assert len(params_cobertura("2025-01", "2025-12")) == \
            SQL_COBERTURA_EMPRESAS.count("%s")
