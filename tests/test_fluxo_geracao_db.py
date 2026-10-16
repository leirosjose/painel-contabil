import pandas as pd
import pytest

import main as M

pytestmark = pytest.mark.db


def _raw(codis_saida):
    return {
        "efsaidas": pd.DataFrame({"codi_emp": [1] * len(codis_saida), "codi_sai": list(codis_saida),
                                  "dsai_sai": ["2026-01-10", "2026-01-20", "2026-02-05"][: len(codis_saida)],
                                  "vcon_sai": [100.0, 50.0, 25.0][: len(codis_saida)],
                                  "codi_nat": [5102] * len(codis_saida)}),
        "efentradas": pd.DataFrame({"codi_emp": [1], "codi_ent": [7], "dent_ent": ["2026-01-15"],
                                    "vcon_ent": [30.0], "codi_nat": [1102]}),
        "efservicos": pd.DataFrame({"codi_emp": [1], "codi_ser": [9], "dser_ser": ["2026-02-01"],
                                    "vcon_ser": [10.0]}),
    }


def _fat(marts):
    f = marts["faturamento_mensal"].sort_values(["codi_emp", "competencia"]).reset_index(drop=True)
    return f[["codi_emp", "competencia", "valo_saidas", "valo_servicos",
              "valo_imobiliario"]]


class TestExtractIgualSkip:
    def test_mesma_janela_mesmo_mart_mesmo_depois_de_reimportar(self, banco_scratch):
        M.ensure_schemas()
        pd.testing.assert_frame_equal(
            _fat(M.run_transform({k: v.copy() for k, v in _raw([1, 2, 3]).items()}, "2026-01", "2026-02", report={})),
            _fat(self._carrega_e_le(_raw([1, 2, 3]), run_id=1)), check_dtype=False)
        # A origem reimporta as mesmas notas com codi_sai NOVO:
        memoria = _fat(M.run_transform({k: v.copy() for k, v in _raw([11, 12, 13]).items()},
                                       "2026-01", "2026-02", report={}))
        vigente = _fat(self._carrega_e_le(_raw([11, 12, 13]), run_id=2))
        pd.testing.assert_frame_equal(memoria, vigente, check_dtype=False)
        assert vigente["valo_saidas"].sum() == 175.0            # nao dobrou (seriam 350)

    @staticmethod
    def _carrega_e_le(raw, run_id):
        M.run_load_raw(raw, "2026-01", "2026-02", None, run_id, report={})
        return M.run_transform(M.run_read_raw(None, report={}), "2026-01", "2026-02", report={})

    def test_skip_extract_recusa_se_o_mapa_aponta_pra_geracao_nao_publicada(self, banco_scratch):
        M.ensure_schemas()
        M.run_load_raw(_raw([1, 2, 3]), "2026-01", "2026-02", None, 1, report={})
        import connections
        c = connections.connect_postgres()
        with c.cursor() as cur:
            cur.execute("INSERT INTO ops.raw_generation (generation_id, run_id, table_name, status) "
                        "VALUES (999, 9, 'efsaidas', 'LOADING')")
            cur.execute("UPDATE ops.raw_slice_vigente SET generation_id = 999 WHERE table_name = 'efsaidas'")
        c.commit()
        c.close()
        with pytest.raises(RuntimeError, match="detector de geracao recusa"):
            M._checa_detector()

    def test_resumo_da_tabela_mostra_o_pedido_anterior_e_o_atual(self, banco_scratch, capsys):
        # O encolhimento da geracao por tabela tem que aparecer na linha dela
        # no resumo do run, sem consulta a parte.
        M.ensure_schemas()
        M.run_load_raw(_raw([1, 2, 3]), "2026-01", "2026-02", None, 1, report={})
        M.run_load_raw(_raw([11, 12]), "2026-01", "2026-02", None, 2, report={})
        linhas = [x for x in capsys.readouterr().out.splitlines() if "raw.efsaidas" in x]
        assert "pedido 0 -> 3 (+3)" in linhas[0]
        assert "pedido 3 -> 2 (-1)" in linhas[1]
