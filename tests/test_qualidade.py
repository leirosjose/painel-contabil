"""Limpeza antes do raw: normaliza o que é normalizável, rejeita o resto com motivo."""
import pandas as pd

import qualidade as Q


def _bruto(linhas):
    return pd.DataFrame(linhas, dtype=object)


def _saida(**kw):
    base = {"codi_emp": "101", "codi_sai": "1", "dsai_sai": "2024-01-31", "vcon_sai": "100.50",
            "codi_nat": "5102", "codi_acu": "1"}
    base.update(kw)
    return base


def test_linha_limpa_passa_intacta():
    r = Q.limpa("efsaidas", _bruto([_saida()]), empresas={101})
    assert r.aceitas.to_dict("records") == [_saida()]
    assert r.rejeitadas.empty and r.normalizadas == {} and r.duplicatas == 0


def test_data_em_formato_brasileiro_e_normalizada():
    r = Q.limpa("efsaidas", _bruto([_saida(dsai_sai="31/01/2024")]), empresas={101})
    assert r.aceitas["dsai_sai"].tolist() == ["2024-01-31"]
    assert r.normalizadas == {Q.N_DATA_BR: 1}


def test_ano_absurdo_e_rejeitado_com_motivo():
    r = Q.limpa("efsaidas", _bruto([_saida(dsai_sai="2204-01-31")]), empresas={101})
    assert r.aceitas.empty
    assert r.rejeitadas["motivo"].tolist() == [Q.M_DATA_IMPLAUSIVEL + ": dsai_sai"]
    assert r.rejeitadas["linha"].tolist() == [2]


def test_valor_com_virgula_decimal_e_milhar_e_normalizado():
    r = Q.limpa("efsaidas", _bruto([_saida(vcon_sai="1.234,56"), _saida(codi_sai="2", vcon_sai="99,9")]),
                empresas={101})
    assert r.aceitas["vcon_sai"].tolist() == ["1234.56", "99.9"]
    assert r.normalizadas == {Q.N_VALOR_BR: 2}


def test_valor_ilegivel_e_rejeitado():
    r = Q.limpa("efsaidas", _bruto([_saida(vcon_sai="cem reais")]), empresas={101})
    assert r.rejeitadas["motivo"].tolist() == [Q.M_VALOR_INVALIDO + ": vcon_sai"]


def test_cnpj_com_mascara_e_espacos_vira_so_digitos():
    bruto = _bruto([{"codi_emp": "101", "nome_emp": "ALFA", "cgce_emp": " 89.039.653/0001-98 ", "stat_emp": "A"}])
    r = Q.limpa("geempre", bruto, empresas=None)
    assert r.aceitas["cgce_emp"].tolist() == ["89039653000198"]
    assert r.normalizadas == {Q.N_CNPJ: 1}


def test_nome_com_espacos_e_caixa_misturados_e_padronizado():
    bruto = _bruto([{"codi_emp": "101", "nome_emp": "  Alfa   comércio  ", "cgce_emp": "89039653000198",
                     "stat_emp": "A"}])
    r = Q.limpa("geempre", bruto, empresas=None)
    assert r.aceitas["nome_emp"].tolist() == ["ALFA COMÉRCIO"]
    assert r.normalizadas == {Q.N_TEXTO: 1}


def test_linha_duplicada_e_removida_e_contada():
    r = Q.limpa("efsaidas", _bruto([_saida(), _saida()]), empresas={101})
    assert len(r.aceitas) == 1 and r.duplicatas == 1 and r.rejeitadas.empty


def test_coluna_obrigatoria_vazia_e_rejeitada():
    r = Q.limpa("efsaidas", _bruto([_saida(dsai_sai=None)]), empresas={101})
    assert r.rejeitadas["motivo"].tolist() == [Q.M_OBRIGATORIA + ": dsai_sai"]


def test_empresa_fora_do_cadastro_e_rejeitada():
    r = Q.limpa("efsaidas", _bruto([_saida(codi_emp="999")]), empresas={101})
    assert r.rejeitadas["motivo"].tolist() == [Q.M_EMPRESA]


def test_parcela_sem_nota_mae_e_rejeitada():
    bruto = _bruto([{"codi_emp": "102", "codi_ser": "7", "parc_serp": "1", "vissr_serp": "10"}])
    r = Q.limpa("efservicospar", bruto, empresas={102}, notas_servico={(102, 1)})
    assert r.rejeitadas["motivo"].tolist() == [Q.M_PARCELA_ORFA]


def test_lancamento_em_conta_fora_do_plano_e_rejeitado():
    lan = {"codi_emp": "101", "nume_lan": "1", "data_lan": "2024-01-02", "cdeb_lan": "10",
           "ccre_lan": "99", "vlor_lan": "5", "orig_lan": "1"}
    r = Q.limpa("ctlancto", _bruto([lan]), empresas={101}, contas={(101, 10), (101, 20)})
    assert r.rejeitadas["motivo"].tolist() == [Q.M_CONTA + ": ccre_lan"]


def test_completude_por_coluna_conta_so_as_aceitas():
    bruto = _bruto([_saida(), _saida(codi_sai="2", codi_acu=None), _saida(codi_sai="3", codi_emp="999")])
    r = Q.limpa("efsaidas", bruto, empresas={101})
    assert r.completude["codi_acu"] == 0.5
    assert r.completude["vcon_sai"] == 1.0


def test_resumo_por_motivo():
    bruto = _bruto([_saida(codi_emp="999"), _saida(codi_sai="2", codi_emp="998"),
                    _saida(codi_sai="3", dsai_sai="1899-12-31")])
    r = Q.limpa("efsaidas", bruto, empresas={101})
    assert r.rejeitadas_por_motivo() == {Q.M_EMPRESA: 2, Q.M_DATA_IMPLAUSIVEL + ": dsai_sai": 1}


def test_chave_vazia_e_rejeitada_em_vez_de_derrubar_a_carga():
    par = _bruto([{"codi_emp": "102", "codi_ser": None, "parc_serp": "1", "vissr_serp": "10"}])
    r = Q.limpa("efservicospar", par, empresas={102}, notas_servico={(102, 1)})
    assert r.rejeitadas["motivo"].tolist() == [Q.M_OBRIGATORIA + ": codi_ser"]
    conta = _bruto([{"codi_emp": "101", "codi_cta": None, "clas_cta": "1", "nome_cta": "X", "grdre_cta": None}])
    assert Q.limpa("ctcontas", conta, empresas={101}).rejeitadas["motivo"].tolist() == [
        Q.M_OBRIGATORIA + ": codi_cta"]
    assert Q.limpa("efsaidas", _bruto([_saida(codi_sai=None)]), empresas={101}).rejeitadas["motivo"].tolist() == [
        Q.M_OBRIGATORIA + ": codi_sai"]


def test_milhar_sem_decimais_e_ambiguo_e_rejeitado():
    r = Q.limpa("efsaidas", _bruto([_saida(vcon_sai="1.234")]), empresas={101})
    assert r.rejeitadas["motivo"].tolist() == [Q.M_VALOR_AMBIGUO + ": vcon_sai"]


def test_data_vazia_vinda_do_excel_e_tratada_como_vazia():
    bruto = pd.DataFrame([{"codi_emp": 101, "i_empregados": 1, "admissao": pd.NaT}])
    r = Q.limpa("foempregados", bruto, empresas={101})
    assert r.rejeitadas.empty and r.aceitas["admissao"].tolist() == [None]
