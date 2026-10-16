"""FonteArquivos: CSV/XLSX no lugar do banco do Domínio, com o MESMO contrato
de colunas, tipos e janela que o extract da origem entrega ao pipeline."""
import pandas as pd
import pytest

from fonte.arquivos import FonteArquivos
from raw_schema import TIPOS


def _csv(pasta, tabela, linhas):
    pd.DataFrame(linhas).to_csv(pasta / f"{tabela}.csv", index=False)


@pytest.fixture
def pasta(tmp_path):
    _csv(tmp_path, "efsaidas", [
        {"codi_emp": 101, "codi_sai": 1, "dsai_sai": "2024-01-31", "vcon_sai": "100.50", "codi_nat": 5102, "codi_acu": 1},
        {"codi_emp": 101, "codi_sai": 2, "dsai_sai": "2024-02-01", "vcon_sai": "200", "codi_nat": 5102, "codi_acu": 1},
        {"codi_emp": 102, "codi_sai": 3, "dsai_sai": "2024-02-15", "vcon_sai": "300", "codi_nat": 6102, "codi_acu": 1},
        {"codi_emp": 101, "codi_sai": 4, "dsai_sai": "2024-03-01", "vcon_sai": "400", "codi_nat": 5102, "codi_acu": 1},
    ])
    return tmp_path


def test_ler_entrega_tipos_do_contrato(pasta):
    df = FonteArquivos(pasta).ler("efsaidas")
    assert list(df.columns) == list(TIPOS["efsaidas"])
    assert df["codi_emp"].dtype == "int64"
    assert df["vcon_sai"].dtype == "float64"
    assert df["dsai_sai"].tolist()[0] == "2024-01-31"
    assert isinstance(df["dsai_sai"].iloc[0], str)


def test_inteiro_com_vazio_vira_float_como_o_driver_da_origem(tmp_path):
    _csv(tmp_path, "ctcontas", [
        {"codi_emp": 101, "codi_cta": 1, "clas_cta": "1", "nome_cta": "ATIVO", "grdre_cta": None},
        {"codi_emp": 101, "codi_cta": 2, "clas_cta": "3010101", "nome_cta": "VENDAS", "grdre_cta": 3},
    ])
    df = FonteArquivos(tmp_path).ler("ctcontas")
    assert df["grdre_cta"].dtype == "float64"
    assert df["clas_cta"].tolist() == ["1", "3010101"]


def test_janela_meia_aberta_e_filtro_de_empresa(pasta):
    df = FonteArquivos(pasta).ler("efsaidas", empresa=101, inicio="2024-02", fim="2024-02")
    assert df["codi_sai"].tolist() == [2]


def test_xlsx_equivale_ao_csv_inclusive_com_celula_de_data(tmp_path):
    linhas = pd.DataFrame([
        {"codi_emp": 101, "codi_sai": 1, "dsai_sai": pd.Timestamp("2024-01-31"), "vcon_sai": 100.5, "codi_nat": 5102, "codi_acu": 1},
    ])
    linhas.to_excel(tmp_path / "efsaidas.xlsx", index=False)
    df = FonteArquivos(tmp_path).ler("efsaidas")
    assert df["dsai_sai"].tolist() == ["2024-01-31"]
    assert df["vcon_sai"].tolist() == [100.5]


def test_parcela_de_servico_herda_a_janela_da_nota_mae(tmp_path):
    _csv(tmp_path, "efservicos", [
        {"codi_emp": 102, "codi_ser": 1, "dser_ser": "2024-01-10", "vcon_ser": 1000},
        {"codi_emp": 102, "codi_ser": 2, "dser_ser": "2024-02-10", "vcon_ser": 1000},
    ])
    _csv(tmp_path, "efservicospar", [
        {"codi_emp": 102, "codi_ser": 1, "parc_serp": 1, "vissr_serp": 50},
        {"codi_emp": 102, "codi_ser": 2, "parc_serp": 1, "vissr_serp": 60},
    ])
    df = FonteArquivos(tmp_path).ler("efservicospar", inicio="2024-02", fim="2024-02")
    assert df["codi_ser"].tolist() == [2]


def test_eventos_so_de_empresa_com_movimento_na_janela(tmp_path):
    _csv(tmp_path, "fomovto", [
        {"codi_emp": 101, "i_empregados": 1, "data": "2024-02-01", "tipo_proces": 11, "i_eventos": 1,
         "valor_inf": 0, "valor_cal": 1000, "prov_desc": "P", "i_calculos": "a"},
        {"codi_emp": 103, "i_empregados": 1, "data": "2023-12-01", "tipo_proces": 11, "i_eventos": 1,
         "valor_inf": 0, "valor_cal": 1000, "prov_desc": "P", "i_calculos": "b"},
    ])
    _csv(tmp_path, "foeventos", [
        {"codi_emp": 101, "i_eventos": 1, "nome": "SALARIO", "prov_desc": "P", "classificacao": 1},
        {"codi_emp": 103, "i_eventos": 1, "nome": "SALARIO", "prov_desc": "P", "classificacao": 1},
    ])
    df = FonteArquivos(tmp_path).ler("foeventos", inicio="2024-01", fim="2024-12")
    assert df["codi_emp"].tolist() == [101]


def test_empregados_admitidos_ate_o_fim_da_janela(tmp_path):
    _csv(tmp_path, "foempregados", [
        {"codi_emp": 101, "i_empregados": 1, "admissao": "2020-05-01"},
        {"codi_emp": 101, "i_empregados": 2, "admissao": "2024-03-01"},
    ])
    df = FonteArquivos(tmp_path).ler("foempregados", inicio="2024-01", fim="2024-02")
    assert df["i_empregados"].tolist() == [1]


def test_dicionario_global_ignora_empresa_e_janela(tmp_path):
    _csv(tmp_path, "foclassificacoes_eventos", [
        {"i_classificacao": 1, "descricao": "Salários"},
        {"i_classificacao": 2, "descricao": "Horas Extras"},
    ])
    df = FonteArquivos(tmp_path).ler("foclassificacoes_eventos", empresa=101, inicio="2024-01", fim="2024-01")
    assert len(df) == 2


def test_saldo_inicial_soma_debito_e_credito_antes_do_inicio(tmp_path):
    _csv(tmp_path, "ctlancto", [
        {"codi_emp": 101, "nume_lan": 1, "data_lan": "2023-12-31", "cdeb_lan": 10, "ccre_lan": 20, "vlor_lan": 500, "orig_lan": 1},
        {"codi_emp": 101, "nume_lan": 2, "data_lan": "2023-12-31", "cdeb_lan": 10, "ccre_lan": 30, "vlor_lan": 100, "orig_lan": 1},
        {"codi_emp": 101, "nume_lan": 3, "data_lan": "2024-01-05", "cdeb_lan": 10, "ccre_lan": 20, "vlor_lan": 999, "orig_lan": 1},
    ])
    bruto = FonteArquivos(tmp_path).saldo_inicial(empresa=None, inicio="2024-01")
    assert list(bruto.columns) == list(TIPOS["ctsaldo_inicial"])
    df = bruto.set_index("codi_cta")
    assert df.loc[10, "valo_deb"] == 600.0 and df.loc[10, "valo_cre"] == 0.0
    assert df.loc[20, "valo_cre"] == 500.0 and df.loc[30, "valo_cre"] == 100.0
    assert set(df["data_corte"]) == {"2024-01-01"}


def test_max_datas_dos_fluxos_de_documento(pasta):
    _csv(pasta, "efservicos", [{"codi_emp": 102, "codi_ser": 1, "dser_ser": "2024-04-02", "vcon_ser": 1}])
    _csv(pasta, "efentradas", [{"codi_emp": 101, "codi_ent": 1, "dent_ent": "2024-01-02", "vcon_ent": 1, "codi_nat": 1202}])
    assert FonteArquivos(pasta).max_datas() == {
        "efsaidas": "2024-03-01", "efservicos": "2024-04-02", "efentradas": "2024-01-02"}


def test_arquivo_ausente_levanta_erro_claro(tmp_path):
    with pytest.raises(FileNotFoundError, match="efsaidas"):
        FonteArquivos(tmp_path).ler("efsaidas")


def test_coluna_do_contrato_ausente_levanta_erro_claro(tmp_path):
    _csv(tmp_path, "efsaidas", [{"codi_emp": 101, "codi_sai": 1, "dsai_sai": "2024-01-31"}])
    with pytest.raises(ValueError, match="vcon_sai"):
        FonteArquivos(tmp_path).ler("efsaidas")
