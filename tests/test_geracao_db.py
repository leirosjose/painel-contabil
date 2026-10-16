import pandas as pd
import pytest

import geracao as G
from raw_escopo import pedido

pytestmark = pytest.mark.db


def _conn():
    import connections
    return connections.connect_postgres()


class TestDdlOps:
    def test_ddl_e_idempotente_e_cria_a_legacy(self, banco_scratch):
        c = _conn()
        with c.cursor() as cur:
            G.garante_ops(cur)
            G.garante_ops(cur)
            cur.execute("SELECT generation_id, status FROM ops.raw_generation")
            assert cur.fetchall() == [(0, "PUBLISHED")]
        c.commit()
        c.close()

    def test_view_so_mostra_a_geracao_vigente_da_fatia_e_esconde_as_tecnicas(self, banco_scratch):
        c = _conn()
        with c.cursor() as cur:
            G.garante_ops(cur)
            cur.execute('CREATE TABLE raw_hist."efsaidas" (codi_emp bigint, codi_sai bigint, '
                        'vcon_sai double precision, "_generation_id" bigint, "_fatia" integer)')
            cur.execute('INSERT INTO raw_hist."efsaidas" VALUES '
                        '(1, 10, 5.0, 0, 202601), (1, 11, 7.0, 0, 202602), (1, 12, 9.0, 0, 202601)')
            cur.execute("INSERT INTO ops.raw_slice_vigente VALUES ('efsaidas', 1, 202601, 0, 2, NULL)")
            G.garante_view(cur, "efsaidas")
            cur.execute('SELECT * FROM raw."efsaidas" ORDER BY codi_sai')
            assert [d[0] for d in cur.description] == ["codi_emp", "codi_sai", "vcon_sai"]
            assert cur.fetchall() == [(1, 10, 5.0), (1, 12, 9.0)]   # 202602 sem vigente: some
        c.commit()
        c.close()

    def test_view_de_dicionario_global_usa_a_empresa_zero(self, banco_scratch):
        c = _conn()
        with c.cursor() as cur:
            G.garante_ops(cur)
            cur.execute('CREATE TABLE raw_hist."foeventosdef" (i_eventos bigint, nome text, '
                        '"_generation_id" bigint, "_fatia" integer)')
            cur.execute("INSERT INTO raw_hist.\"foeventosdef\" VALUES (1, 'SALARIO', 0, 0)")
            cur.execute("INSERT INTO ops.raw_slice_vigente VALUES ('foeventosdef', 0, 0, 0, 1, NULL)")
            G.garante_view(cur, "foeventosdef")
            cur.execute('SELECT * FROM raw."foeventosdef"')
            assert cur.fetchall() == [(1, "SALARIO")]
        c.commit()
        c.close()


CH = ["codi_emp", "codi_sai"]


def _saidas(codis, mes="2026-01", emp=1, valor=100.0):
    return pd.DataFrame({"codi_emp": [emp] * len(codis), "codi_sai": list(codis),
                         "dsai_sai": [f"{mes}-10"] * len(codis),
                         "vcon_sai": [valor] * len(codis)})


def _roda(c, run_id, df, ini="2026-01", fim="2026-01", **kw):
    r = G.processa_tabela(c, run_id, "efsaidas", df, CH, pedido("efsaidas", None, ini, fim), **kw)
    G.publica(c, [r])
    assert G.verifica_publicacao(c, [r]) == []
    return r


def _vigente(c):
    with c.cursor() as cur:
        cur.execute('SELECT codi_sai FROM raw."efsaidas" ORDER BY 1')
        return [x for (x,) in cur.fetchall()]


def _hist(c):
    with c.cursor() as cur:
        cur.execute('SELECT count(*) FROM raw_hist."efsaidas"')
        return cur.fetchone()[0]


@pytest.fixture
def conn(banco_scratch):
    c = _conn()
    with c.cursor() as cur:
        G.garante_ops(cur)
    c.commit()
    yield c
    c.close()


class TestGeracao:
    def test_reimportacao_com_chave_nova_nao_dobra(self, conn):
        # A origem apaga e recria com codi_sai novo.
        _roda(conn, 1, _saidas([1, 2, 3]))
        _roda(conn, 2, _saidas([11, 12, 13]))
        assert _vigente(conn) == [11, 12, 13]
        assert _hist(conn) == 6                                  # as duas ficam fisicamente

    def test_fatia_esvaziada_na_origem_some_do_vigente(self, conn):
        # A origem perdeu a fatia inteira (3 linhas, abaixo do piso).
        base = pd.concat([_saidas([1, 2, 3], "2026-01"), _saidas(range(100, 300), "2026-02")])
        _roda(conn, 1, base, "2026-01", "2026-02")
        _roda(conn, 2, _saidas(range(100, 300), "2026-02"), "2026-01", "2026-02")
        assert _vigente(conn) == list(range(100, 300))

    def test_extract_vazio_rejeita_a_geracao_e_preserva_o_vigente(self, conn):
        _roda(conn, 1, _saidas([1, 2, 3]))
        r = _roda(conn, 2, _saidas([]))
        assert r.status == "REJECTED" and "perdeu 100" in r.motivo
        assert _vigente(conn) == [1, 2, 3]

    def test_encolhimento_de_fatia_rejeita_so_a_fatia_e_guarda_as_linhas(self, conn):
        base = pd.concat([_saidas(range(0, 100), "2026-01"), _saidas(range(1000, 3000), "2026-02")])
        _roda(conn, 1, base, "2026-01", "2026-02")
        novo = pd.concat([_saidas(range(5000, 5030), "2026-01"), _saidas(range(1000, 3000), "2026-02")])
        r = _roda(conn, 2, novo, "2026-01", "2026-02")
        assert r.status == "SUCCESS"
        dest = dict(zip(r.fatias.fatia, r.fatias.destino))
        assert dest == {202601: "REJECTED", 202602: "UNCHANGED"}
        assert _vigente(conn)[:100] == list(range(0, 100))       # jan segue a geracao 1
        assert _hist(conn) == 2100 + 30                          # as 30 rejeitadas ficam p/ auditoria

    def test_override_com_motivo_publica_a_fatia(self, conn):
        base = pd.concat([_saidas(range(0, 100), "2026-01"), _saidas(range(1000, 3000), "2026-02")])
        _roda(conn, 1, base, "2026-01", "2026-02")
        novo = pd.concat([_saidas(range(5000, 5030), "2026-01"), _saidas(range(1000, 3000), "2026-02")])
        _roda(conn, 2, novo, "2026-01", "2026-02",
              aceitas=frozenset({"efsaidas:1:202601"}), motivo_override="guias recalculadas (teste)")
        v = _vigente(conn)
        assert 0 not in v and 5000 in v and len(v) == 2030      # jan virou a geracao 2
        with conn.cursor() as cur:
            cur.execute("SELECT override_motivo FROM ops.raw_generation_scope "
                        "WHERE scope_type = 'fatia' AND scope_value = 202601 AND generation_id = "
                        "(SELECT max(generation_id) FROM ops.raw_generation)")
            assert cur.fetchone()[0] == "guias recalculadas (teste)"

    def test_run_morto_antes_de_publicar_nao_aparece(self, conn):
        _roda(conn, 1, _saidas([1, 2, 3]))
        G.processa_tabela(conn, 2, "efsaidas", _saidas([7, 8, 9]), CH, pedido("efsaidas", None, "2026-01", "2026-01"))
        assert _vigente(conn) == [1, 2, 3]                       # sem publica(): invisivel
        probs, avisos = G.detecta_rapido(conn)
        assert probs == [] and any("nunca e lida" in a for a in avisos)

    def test_fatia_inalterada_nao_regrava(self, conn):
        _roda(conn, 1, _saidas([1, 2, 3]))
        r = _roda(conn, 2, _saidas([1, 2, 3]))
        assert r.linhas_gravadas == 0 and set(r.fatias.destino) == {"UNCHANGED"}
        assert _hist(conn) == 3

    def test_chave_nula_rejeita_a_geracao(self, conn):
        _roda(conn, 1, _saidas([1, 2, 3]))
        ruim = _saidas([1, 2, 3])
        ruim["codi_sai"] = [1, None, 3]
        r = _roda(conn, 2, ruim)
        assert r.status == "REJECTED" and "chave NULL" in r.motivo
        assert _vigente(conn) == [1, 2, 3]

    def test_mapa_completo_bate_depois_de_varias_geracoes(self, conn):
        _roda(conn, 1, _saidas([1, 2, 3]))
        _roda(conn, 2, _saidas([4, 5, 6]))
        assert G.confere_mapa_completo(conn) == []
