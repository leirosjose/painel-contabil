import geracao as G
from raw_escopo import pedido

P = pedido("efsdoimp", None, "2023-01", "2026-08")


def A(n, g=7):
    return G.Anterior(g, n, "x")


class TestGuardaDeFatia:
    def test_perda_acima_de_30pct_rejeita_a_fatia(self):
        # O caso que motivou os 30%: backup no meio do reprocessamento, 40% do mes
        # ainda nao reimportado. Com 50% passava calado.
        v = G.avalia_encolhimento("efsdoimp", P, {(1, 202607): A(100), (2, 202607): A(5000)},
                                  {(1, 202607): 60, (2, 202607): 5000})
        assert (1, 202607) in v.fatias_rejeitadas and not v.tabela_rejeitada

    def test_perda_de_30pct_exatos_passa(self):
        # 1 - 70/100 = 0.30000000000000004 no float: a conta e (ant - atual) / ant.
        v = G.avalia_encolhimento("efsdoimp", P, {(1, 202607): A(100), (2, 202607): A(5000)},
                                  {(1, 202607): 70, (2, 202607): 5000})
        assert v.fatias_rejeitadas == {}

    def test_perda_de_5pct_exatos_na_tabela_passa(self):
        v = G.avalia_encolhimento("efsdoimp", P, {(1, 202607): A(1000)}, {(1, 202607): 950})
        assert not v.tabela_rejeitada

    def test_piso_nao_julga_fatia_pequena(self):
        # 5 guias recalculadas numa empresa pequena nao podem travar a carga.
        v = G.avalia_encolhimento("efsdoimp", P, {(1, 202607): A(19), (2, 202607): A(5000)},
                                  {(2, 202607): 5000})
        assert v.fatias_rejeitadas == {}

    def test_baseline_legacy_nao_e_julgada_por_fatia(self):
        # A LEGACY carrega a reimportacao: a fatia reprocessada tem as duas geracoes.
        v = G.avalia_encolhimento("efsdoimp", P, {(1, 202606): A(200, g=G.LEGACY), (2, 202606): A(9000, g=G.LEGACY)},
                                  {(1, 202606): 100, (2, 202606): 9000})
        assert v.fatias_rejeitadas == {}

    def test_fatia_esvaziada_na_origem_conta_como_perda_total(self):
        v = G.avalia_encolhimento("efsdoimp", P, {(1, 202607): A(100), (2, 202607): A(5000)},
                                  {(2, 202607): 5000})
        assert (1, 202607) in v.fatias_rejeitadas

    def test_fatia_fora_do_pedido_nao_e_julgada(self):
        p = pedido("efsdoimp", None, "2026-01", "2026-08")
        v = G.avalia_encolhimento("efsdoimp", p, {(1, 202501): A(100), (2, 202605): A(10)},
                                  {(2, 202605): 10})
        assert v.fatias_rejeitadas == {} and v.total_anterior == 10


class TestGuardaDeTabela:
    def test_pedido_perdendo_mais_de_5pct_rejeita_a_geracao(self):
        ant = {(e, 202607): A(100) for e in range(10)}
        atu = {(e, 202607): 100 for e in range(9)} | {(9, 202607): 40}
        v = G.avalia_encolhimento("efsdoimp", P, ant, atu)
        assert v.tabela_rejeitada and "6.0%" in v.motivo

    def test_extract_vazio_com_vigente_rejeita(self):
        # Snapshot com a tabela vazia: perde 100%.
        v = G.avalia_encolhimento("efsdoimp", P, {(1, 202607): A(3)}, {})
        assert v.tabela_rejeitada

    def test_tabela_nunca_carregada_nao_rejeita(self):
        assert not G.avalia_encolhimento("efsdoimp", P, {}, {(1, 202607): 5}).tabela_rejeitada


class TestOverride:
    def test_override_de_fatia_libera_e_registra(self):
        v = G.avalia_encolhimento("efsdoimp", P, {(1, 202607): A(100), (2, 202607): A(5000)},
                                  {(1, 202607): 10, (2, 202607): 5000},
                                  aceitas=frozenset({"efsdoimp:1:202607"}))
        assert v.fatias_rejeitadas == {} and v.fatias_liberadas == {(1, 202607)}

    def test_override_de_tabela_libera_a_guarda_de_tabela(self):
        v = G.avalia_encolhimento("efsdoimp", P, {(1, 202607): A(3)}, {},
                                  aceitas=frozenset({"efsdoimp"}))
        assert not v.tabela_rejeitada

    def test_aceita_por_empresa(self):
        assert G.aceita(frozenset({"ctlancto:265"}), "ctlancto", 265, 202606)
        assert not G.aceita(frozenset({"ctlancto:265"}), "ctlancto", 266, 202606)
