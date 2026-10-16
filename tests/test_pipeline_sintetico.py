"""Pipeline inteiro em memória sobre os dados sintéticos: gerador -> fonte -> marts."""
import pandas as pd
import pytest

import gerador
import main as M
from fonte.sintetica import FonteSintetica

COLUNAS_TEXTO = {"competencia", "fonte", "regime", "fonte_deteccao", "clas_cta", "nome_cta", "nome_emp",
                 "cgce_emp", "stat_emp", "desc_classificacao", "fonte_dicionario", "nome"}
INICIO, FIM = "2023-12", "2025-12"


@pytest.fixture(scope="module")
def marts():
    raw = M.run_extract(None, INICIO, FIM, fonte=FonteSintetica())
    return M.run_transform(raw, INICIO, FIM)


def test_todo_mart_sai_preenchido_para_as_cinco_empresas(marts):
    assert set(marts) == set(M.MART_KEYS)
    for nome, df in marts.items():
        assert len(df), nome
        if nome != "empresas" and "competencia" in df.columns:
            assert set(df["codi_emp"]) == {e.codi for e in gerador.EMPRESAS}, nome


def test_coluna_de_valor_de_mart_nunca_sai_como_texto(marts):
    for nome, df in marts.items():
        objeto = [c for c in df.columns if df[c].dtype == object and c not in COLUNAS_TEXTO]
        assert not objeto, f"{nome}: {objeto}"


def test_faturamento_respeita_venda_a_ordem_e_entrega_futura(marts):
    fat = marts["faturamento_mensal"].set_index(["codi_emp", "competencia"])
    assert (fat["valo_saidas"] > 0).groupby(level=0).all().loc[[101, 103, 105]].all()
    assert (fat.loc[102, "valo_servicos"] > 0).all()
    assert (fat.loc[104, "valo_imobiliario"] > 0).all()


def test_regime_detectado_bate_com_o_cadastro_do_gerador(marts):
    reg = marts["empresa_regime_periodo"].groupby("codi_emp")["regime"].agg(set)
    for e in gerador.EMPRESAS:
        assert reg[e.codi] == ({"simples"} if e.regime == "simples" else {"real_presumido"}), e.codi


def test_iss_da_prestadora_vem_de_guia_mais_retido(marts):
    d = marts["impostos_destacados_mensais"]
    beta = d[d.codi_emp == 102]
    assert set(beta["fonte"]) == {"balancete+guia+retido"}
    assert (pd.to_numeric(beta["valo_iss"]) > 0).all()
