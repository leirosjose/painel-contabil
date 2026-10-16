"""Página de qualidade: lê o relatório da última carga gravado no ops."""
import importlib
import sys

import pandas as pd
import pytest

import qualidade as Q
from connections import connect_postgres

pytestmark = pytest.mark.db


def _pagina(monkeypatch, banco):
    monkeypatch.setenv("PG_DB", banco)
    sys.modules.pop("pages.qualidade_dados", None)
    import streamlit as st
    monkeypatch.setattr(st, "set_page_config", lambda **k: None)
    return importlib.import_module("pages.qualidade_dados")


def test_sem_carga_ainda_devolve_none(monkeypatch, banco_scratch):
    assert _pagina(monkeypatch, banco_scratch).carrega_relatorio() is None


def test_resume_a_ultima_carga_por_tabela_motivo_e_completude(monkeypatch, banco_scratch):
    def r(dsai, emp, sai):
        return {"codi_emp": emp, "codi_sai": sai, "dsai_sai": dsai, "vcon_sai": "1",
                "codi_nat": "5102", "codi_acu": None if sai == "2" else "1"}
    conn = connect_postgres()
    velho = Q.limpa("efsaidas", pd.DataFrame([r("2024-01-01", "999", "1")], dtype=object), empresas={101})
    Q.grava_relatorio(conn, 1, {"efsaidas": velho})
    novo = Q.limpa("efsaidas", pd.DataFrame([r("31/01/2024", "101", "1"), r("2024-01-02", "101", "2"),
                                             r("2024-01-03", "998", "3")], dtype=object), empresas={101})
    Q.grava_relatorio(conn, 2, {"efsaidas": novo})
    conn.close()
    rel = _pagina(monkeypatch, banco_scratch).carrega_relatorio()
    assert rel["run_id"] == 2
    assert rel["tabelas"][["tabela", "lidas", "aceitas", "rejeitadas"]].values.tolist() == [["efsaidas", 3, 2, 1]]
    assert rel["motivos"][["motivo", "linhas"]].values.tolist() == [[Q.M_EMPRESA, 1]]
    assert rel["incompletas"][["coluna", "completude"]].values.tolist() == [["codi_acu", 0.5]]
