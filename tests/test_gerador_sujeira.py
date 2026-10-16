"""A sujeira injetada pelo gerador é exatamente o que a limpeza normaliza ou rejeita."""
import json

import pandas as pd
import pytest

import gerador
import qualidade as Q
from fonte.arquivos import FonteArquivos
from gerador.escrita import escreve
from gerador.sujeira import suja

MOTIVO = {"ano_absurdo": Q.M_DATA_IMPLAUSIVEL, "obrigatoria_vazia": Q.M_OBRIGATORIA,
          "empresa_inexistente": Q.M_EMPRESA, "parcela_orfa": Q.M_PARCELA_ORFA,
          "conta_fora_do_plano": Q.M_CONTA}
NORMALIZA = {"data_br": Q.N_DATA_BR, "valor_br": Q.N_VALOR_BR, "cnpj_mascara": Q.N_CNPJ,
             "texto_sujo": Q.N_TEXTO}


@pytest.fixture(scope="module")
def pastas(tmp_path_factory):
    limpo = gerador.gera(gerador.SEED)
    sujo, manifesto = suja(limpo, gerador.SEED)
    d_limpo, d_sujo = tmp_path_factory.mktemp("limpo"), tmp_path_factory.mktemp("sujo")
    escreve(limpo, d_limpo)
    escreve(sujo, d_sujo, manifesto=manifesto)
    return d_limpo, d_sujo, json.loads((d_sujo / "_sujeira.json").read_text())


def _rel(fonte, tabela):
    fonte.ler(tabela)
    return fonte.relatorio[tabela]


def test_manifesto_cobre_todos_os_tipos_de_sujeira(pastas):
    _, _, manifesto = pastas
    tipos = {tipo for t in manifesto.values() for tipo in t}
    assert tipos == {"data_br", "valor_br", "cnpj_mascara", "texto_sujo", "duplicada", "ano_absurdo",
                     "obrigatoria_vazia", "empresa_inexistente", "parcela_orfa", "conta_fora_do_plano"}


@pytest.mark.parametrize("tipo", sorted(NORMALIZA))
def test_normalizacao_contada_igual_ao_que_o_gerador_sujou(pastas, tipo):
    _, d_sujo, manifesto = pastas
    fonte = FonteArquivos(d_sujo)
    for tabela, tipos in manifesto.items():
        if tipo in tipos:
            assert _rel(fonte, tabela).normalizadas.get(NORMALIZA[tipo], 0) == tipos[tipo], tabela


def test_duplicatas_removidas_igual_ao_que_o_gerador_duplicou(pastas):
    _, d_sujo, manifesto = pastas
    fonte = FonteArquivos(d_sujo)
    for tabela, tipos in manifesto.items():
        assert _rel(fonte, tabela).duplicatas == tipos.get("duplicada", 0), tabela


@pytest.mark.parametrize("tipo,motivo", [
    ("ano_absurdo", Q.M_DATA_IMPLAUSIVEL), ("obrigatoria_vazia", Q.M_OBRIGATORIA),
    ("empresa_inexistente", Q.M_EMPRESA), ("parcela_orfa", Q.M_PARCELA_ORFA),
    ("conta_fora_do_plano", Q.M_CONTA),
])
def test_rejeicao_por_motivo_igual_ao_que_o_gerador_injetou(pastas, tipo, motivo):
    _, d_sujo, manifesto = pastas
    fonte = FonteArquivos(d_sujo)
    for tabela, tipos in manifesto.items():
        rej = _rel(fonte, tabela).rejeitadas
        achadas = int(rej["motivo"].str.startswith(motivo).sum()) if len(rej) else 0
        assert achadas == tipos.get(tipo, 0), (tabela, tipo)


def test_depois_da_limpeza_o_dado_aceito_e_o_dado_limpo(pastas):
    d_limpo, d_sujo, manifesto = pastas
    limpo, sujo = FonteArquivos(d_limpo), FonteArquivos(d_sujo)
    for tabela in manifesto:
        a = limpo.ler(tabela).sort_values(list(limpo.ler(tabela).columns)).reset_index(drop=True)
        b = sujo.ler(tabela).sort_values(list(a.columns)).reset_index(drop=True)
        pd.testing.assert_frame_equal(a, b, obj=tabela)


def test_dado_limpo_nao_gera_rejeicao_nem_normalizacao(pastas):
    d_limpo, _, manifesto = pastas
    fonte = FonteArquivos(d_limpo)
    for tabela in manifesto:
        r = _rel(fonte, tabela)
        assert r.rejeitadas.empty and not r.normalizadas and r.duplicatas == 0, tabela


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6, 7, 8])
def test_manifesto_bate_com_a_limpeza_em_qualquer_semente(tmp_path, seed):
    sujo, manifesto = suja(gerador.gera(seed), seed)
    escreve(sujo, tmp_path, manifesto=manifesto)
    fonte = FonteArquivos(tmp_path)
    for tabela, tipos in manifesto.items():
        r = _rel(fonte, tabela)
        for tipo, motivo in MOTIVO.items():
            achadas = int(r.rejeitadas["motivo"].str.startswith(motivo).sum()) if len(r.rejeitadas) else 0
            assert achadas == tipos.get(tipo, 0), (seed, tabela, tipo, r.rejeitadas_por_motivo())
        assert sum(r.normalizadas.values()) == sum(n for t, n in tipos.items() if t in NORMALIZA), (seed, tabela)
        assert r.duplicatas == tipos.get("duplicada", 0), (seed, tabela)


def test_linha_extra_rejeitada_nasce_do_dado_limpo_e_nao_de_linha_ja_suja(tmp_path):
    tabelas = gerador.gera(gerador.SEED)
    tabelas["efsaidas"] = tabelas["efsaidas"].head(14)
    sujo, manifesto = suja(tabelas, gerador.SEED)
    escreve(sujo, tmp_path, manifesto=manifesto)
    r = _rel(FonteArquivos(tmp_path), "efsaidas")
    assert r.rejeitadas_por_motivo().get(Q.M_DATA_IMPLAUSIVEL + ": dsai_sai") == 2
