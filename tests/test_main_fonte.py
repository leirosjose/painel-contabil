"""O orquestrador lê a origem só pela interface Fonte."""
import sys

import pandas as pd

import main as M


class FonteFalsa:
    def __init__(self, max_datas=None):
        self.chamadas = {}
        self._max = max_datas or {"efsaidas": None, "efservicos": None, "efentradas": None}

    def ler(self, tabela, empresa=None, inicio=None, fim=None):
        self.chamadas[tabela] = (empresa, inicio, fim)
        return pd.DataFrame()

    def saldo_inicial(self, empresa, inicio):
        self.chamadas["ctsaldo_inicial"] = (empresa, inicio)
        return pd.DataFrame()

    def max_datas(self):
        return self._max


def test_run_extract_le_cada_tabela_do_raw_pela_fonte():
    fonte = FonteFalsa()
    raw = M.run_extract(1, "2025-01", "2025-03", fonte=fonte)
    assert set(raw) == set(M.RAW_KEYS)
    assert set(fonte.chamadas) == set(M.RAW_KEYS)


def test_janelas_por_tabela_continuam_decididas_pelo_orquestrador():
    fonte = FonteFalsa()
    M.run_extract(1, "2025-01", "2025-03", fonte=fonte)
    assert fonte.chamadas["fobases"] == (1, "2024-12", "2025-03")
    assert fonte.chamadas["forescisoes"] == (1, "2024-12", "2025-03")
    assert fonte.chamadas["ctlancto"] == (1, "2025-01", "2025-03")
    assert fonte.chamadas["ctsaldo_inicial"] == (1, "2025-01")
    assert fonte.chamadas["foeventosdef"] == (None, None, None)


def test_modo_never_pula_o_saldo_inicial():
    fonte = FonteFalsa()
    raw = M.run_extract(1, "2025-01", "2025-03", saldo_inicial_mode="never", fonte=fonte)
    assert "ctsaldo_inicial" not in raw


def test_fim_declarado_vem_das_datas_da_fonte():
    fonte = FonteFalsa({"efsaidas": "2025-03-28", "efservicos": "2025-04-02", "efentradas": None})
    assert M.fim_declarado_origem(fonte) == "2025-03"


def test_orquestrador_nao_carrega_driver_da_origem():
    assert "jaydebeapi" not in sys.modules
    assert not hasattr(M, "connect_sqlanywhere")

