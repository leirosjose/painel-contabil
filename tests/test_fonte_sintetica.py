"""FonteSintetica: o gerador entrega as tabelas no layout do Domínio, com o MESMO
contrato de colunas, tipos e janela que o extract da origem entrega ao pipeline."""
import pandas as pd
import pytest

from fonte.sintetica import FonteSintetica
from gerador import gera
from raw_schema import TIPOS, aplica_contrato


@pytest.fixture(scope="module")
def fonte():
    return FonteSintetica()


@pytest.mark.parametrize("tabela", sorted(set(TIPOS) - {"ctsaldo_inicial"}))
def test_toda_tabela_sai_no_contrato(fonte, tabela):
    df = fonte.ler(tabela)
    assert list(df.columns) == list(TIPOS[tabela])
    if "codi_emp" in df.columns and len(df):
        assert df["codi_emp"].dtype == "int64"


def test_data_sai_como_texto_e_valor_como_float(fonte):
    df = fonte.ler("efsaidas")
    assert isinstance(df["dsai_sai"].iloc[0], str) and len(df["dsai_sai"].iloc[0]) == 10
    assert df["vcon_sai"].dtype == "float64"


def test_inteiro_com_vazio_vira_float_como_o_driver_da_origem(fonte):
    assert fonte.ler("ctcontas")["grdre_cta"].dtype == "float64"


def test_janela_meia_aberta_e_filtro_de_empresa(fonte):
    df = fonte.ler("efsaidas", empresa=101, inicio="2024-02", fim="2024-02")
    assert len(df) and set(df["codi_emp"]) == {101}
    assert df["dsai_sai"].str[:7].eq("2024-02").all()


def test_parcela_de_servico_herda_a_janela_da_nota_mae(fonte):
    notas = fonte.ler("efservicos", inicio="2024-02", fim="2024-02")
    par = fonte.ler("efservicospar", inicio="2024-02", fim="2024-02")
    assert len(par)
    assert set(zip(par.codi_emp, par.codi_ser)) <= set(zip(notas.codi_emp, notas.codi_ser))


def test_eventos_so_de_empresa_com_movimento_na_janela(fonte):
    assert fonte.ler("foeventos", inicio="2020-01", fim="2020-12").empty
    assert set(fonte.ler("foeventos", inicio="2024-01", fim="2024-12")["codi_emp"]) == {101, 102, 103, 104, 105}


def test_empregados_admitidos_ate_o_fim_da_janela(fonte):
    antes = fonte.ler("foempregados", empresa=103, inicio="2024-01", fim="2024-06")
    depois = fonte.ler("foempregados", empresa=103, inicio="2024-01", fim="2024-07")
    assert len(depois) == len(antes) + 1


def test_dicionario_global_ignora_empresa_e_janela(fonte):
    assert len(fonte.ler("foclassificacoes_eventos", empresa=101, inicio="2020-01", fim="2020-01")) == 8


def test_saldo_inicial_soma_debito_e_credito_antes_do_inicio(fonte):
    df = fonte.saldo_inicial(empresa=101, inicio="2023-12")
    assert list(df.columns) == list(TIPOS["ctsaldo_inicial"])
    por_conta = df.set_index("codi_cta")
    assert por_conta.loc[2, "valo_deb"] == 500_000.0     # bancos: abertura
    assert por_conta.loc[14, "valo_cre"] == 500_000.0    # capital social
    assert set(df["data_corte"]) == {"2023-12-01"}


def test_max_datas_dos_fluxos_de_documento(fonte):
    t = gera()
    assert fonte.max_datas() == {"efsaidas": t["efsaidas"]["dsai_sai"].max(),
                                 "efservicos": t["efservicos"]["dser_ser"].max(),
                                 "efentradas": t["efentradas"]["dent_ent"].max()}


def test_contrato_exige_todas_as_colunas():
    with pytest.raises(ValueError, match="vcon_sai"):
        aplica_contrato(pd.DataFrame({"codi_emp": [101], "codi_sai": [1], "dsai_sai": ["2024-01-31"]}),
                        "efsaidas")
