# ---------------------------------------------------------------------------
# Blindagem do kaleido COM CHROME REAL.
#
# Marker `chrome`: fora do CI (`-m "not db and not chrome"`), que nao tem Chrome.
# Os contratos sem Chrome vivem em tests/test_pdf_generator.py
# (TestBlindagemKaleido); estes sao a UNICA prova ponta a ponta -- a race
# entre threads mora dentro da lib e mock nao a reproduz.
#
# Cada caso roda num SUBPROCESSO que sai por os._exit: com o singleton do
# kaleido envenenado, o atexit da lib trava a saida do processo
# e travaria o pytest junto. Isolar tambem impede que
# um caso herde o singleton do outro.
# ---------------------------------------------------------------------------
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.chrome

RAIZ = Path(__file__).resolve().parents[1]

_CABECALHO = """
import json, os, subprocess, sys, threading, time
sys.path.insert(0, os.getcwd())
import pdf_generator as PG
LABELS = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
W, H = PG._CHART_MEIA_LARGURA["width"], PG._CHART_MEIA_LARGURA["height"]
def fig(i):
    return PG._make_bar(LABELS, [(i + 1) * 1000 + k * 37 for k in range(12)], PG.COR_FAT)
def com_limite(fn, limite):
    res = {}
    t = threading.Thread(target=lambda: res.setdefault("v", fn()), daemon=True)
    t0 = time.monotonic()
    t.start()
    t.join(limite)
    return (not t.is_alive()), res.get("v"), round(time.monotonic() - t0, 2)
def sai(d):
    print("RESULTADO " + json.dumps(d), flush=True)
    os._exit(0)
"""


def _roda(corpo, timeout, env_extra=None):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", **(env_extra or {}))
    kw = {"start_new_session": True} if os.name == "posix" else {}
    proc = subprocess.Popen([sys.executable, "-c", _CABECALHO + corpo], cwd=RAIZ,
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, **kw)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
        out, err = proc.communicate()
        pytest.fail(f"subprocesso nao terminou em {timeout}s\nstderr:\n{err[-2000:]}")
    linhas = [x for x in out.splitlines() if x.startswith("RESULTADO ")]
    assert linhas, f"sem RESULTADO (rc={proc.returncode})\nstderr:\n{err[-2000:]}"
    return json.loads(linhas[-1][len("RESULTADO "):]), err


def test_duas_threads_nao_trocam_png():
    # Antes da blindagem: 12 de 40 renders trocados, sempre o PNG exato da outra.
    res, err = _roda("""
figs = [fig(0), fig(1)]
ref = [PG._fig_to_b64(f, W, H) for f in figs]
seq = [PG._fig_to_b64(f, W, H) for f in figs]
trocas, erros = [], []
barreira = threading.Barrier(2)
def worker(i):
    try:
        for r in range(20):
            barreira.wait(timeout=120)
            if PG._fig_to_b64(figs[i], W, H) != ref[i]:
                trocas.append((i, r))
    except Exception as e:
        erros.append(repr(e))
ts = [threading.Thread(target=worker, args=(i,), daemon=True) for i in (0, 1)]
[t.start() for t in ts]
[t.join(300) for t in ts]
sai({"ref_ok": bool(ref[0] and ref[1] and ref[0] != ref[1]), "determinismo": seq == ref,
     "trocas": len(trocas), "renders": 40, "erros": erros,
     "vivas": sum(t.is_alive() for t in ts), "modo": PG.kaleido_estado()["modo"]})
""", timeout=420)
    assert res["ref_ok"] and res["determinismo"], res
    assert res["erros"] == [] and res["vivas"] == 0, res
    assert res["modo"] == "server", res
    assert res["trocas"] == 0, f"{res['trocas']}/{res['renders']} renders trocados"


def test_chrome_inexistente_devolve_vazio_dentro_do_limite():
    # Antes: _fig_to_b64 nao retornava em 60s e _kaleido_server_ok ficava True.
    res, err = _roda("""
limite = PG.KALEIDO_N_START_S + PG.KALEIDO_T_STOP_S + 2 * PG.KALEIDO_N_ONESHOT_S + 10
voltou, b64, s = com_limite(lambda: PG._fig_to_b64(fig(0), W, H), limite)
sai({"voltou": voltou, "vazio": b64 == "", "s": s, "limite": limite,
     "server_ok": PG._kaleido_server_ok, "estado": PG.kaleido_estado()})
""", timeout=300, env_extra={"CHROME_PATH": "/nao/existe/chrome"})
    assert res["voltou"], f"nao retornou em {res['limite']}s"
    assert res["vazio"] and res["server_ok"] is False, res
    assert res["estado"]["modo"] == "off", res
    assert "[kaleido] server -> oneshot" in err and "-> off" in err


@pytest.mark.skipif(os.name != "posix", reason="mata o Chrome via pgrep/SIGKILL")
def test_chrome_morto_derruba_o_server_e_o_oneshot_salva_o_grafico():
    # Antes: _kaleido_server_ok continuava True e o render seguinte TRAVAVA.
    res, err = _roda("""
f = fig(0)
b64_antes = PG._fig_to_b64(f, W, H)
def descendentes(pid):
    r = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True)
    filhos = [int(x) for x in r.stdout.split()]
    return filhos + [n for x in filhos for n in descendentes(x)]
mortos = descendentes(os.getpid())
for p in mortos:
    try:
        os.kill(p, 9)
    except ProcessLookupError:
        pass
time.sleep(1)
limite = PG.KALEIDO_M_RENDER_S + PG.KALEIDO_T_STOP_S + 2 * PG.KALEIDO_N_ONESHOT_S + 10
voltou, b64_depois, s = com_limite(lambda: PG._fig_to_b64(f, W, H), limite)
sai({"antes_ok": bool(b64_antes), "mortos": len(mortos), "voltou": voltou, "s": s,
     "mesmo_png": b64_depois == b64_antes, "server_ok": PG._kaleido_server_ok,
     "preso": PG._kaleido_singleton_preso, "estado": PG.kaleido_estado()})
""", timeout=300)
    assert res["antes_ok"] and res["mortos"] > 0, res
    assert res["voltou"], res
    assert res["server_ok"] is False, res
    assert res["estado"]["modo"] == "oneshot", res
    # a mesma figura, antes pelo server e depois pelo oneshot: pixel identico
    assert res["mesmo_png"], res
