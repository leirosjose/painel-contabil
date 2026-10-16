#!/usr/bin/env python3
"""Gera 1 PDF via pdf_generator (wrapper CLI pro Makefile — infra, zero logica).

Uso: python scripts/gen_pdf.py <codi_emp> <YYYY-MM de> <YYYY-MM ate> [modelos]

`modelos` e opcional: lista separada por virgula com as chaves do registro
pdf_generator.MODELOS (ex.: "fiscal" ou "fiscal,folha"). Omitido = TODOS os
modelos do registro, que e o mesmo default dos botoes do Streamlit.

Saida: saida/<emp>_<de>_<ate>[_<modelos>].pdf (saida/ e gitignored). O sufixo
de modelos no nome evita que duas geracoes da MESMA
empresa/janela com selecoes diferentes se sobrescrevam.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "etl"))
os.chdir(ROOT)


def main():
    if len(sys.argv) not in (4, 5):
        print(__doc__)
        sys.exit(2)
    emp, de, ate = int(sys.argv[1]), sys.argv[2], sys.argv[3]
    modelos = ([m.strip() for m in sys.argv[4].split(",") if m.strip()]
               if len(sys.argv) == 5 else None)
    from connections import connect_postgres
    import pdf_generator as PG
    conn = connect_postgres()
    try:
        # Cabecalho do PDF: nome/cnpj vem de mart.empresas (sem isso o
        # header sai vazio — os defaults de gerar_pdf_empresa sao "").
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(apel_emp, nome_emp), cgce_emp "
                        "FROM mart.empresas WHERE codi_emp = %s", (emp,))
            row = cur.fetchone()
        nome, cnpj = (row if row else ("", ""))
        pdf = PG.gerar_pdf_empresa([emp], de, ate, conn,
                                   empresa_nome=nome or "", cnpj=cnpj or "",
                                   modelos=modelos)
    finally:
        conn.close()
    os.makedirs("saida", exist_ok=True)
    sufixo = f"_{'-'.join(modelos)}" if modelos else ""
    out = f"saida/{emp}_{de}_{ate}{sufixo}.pdf"
    with open(out, "wb") as f:
        f.write(pdf)
    print(f"OK {out} ({len(pdf)} bytes)")
    estado = PG.kaleido_estado()
    if estado["modo"] != "server":
        # A instrucao de reinicio mora AQUI e no stderr, nunca na tela do app:
        # quem le a tela e contador, sem acesso a maquina.
        print(f"AVISO: graficos gerados em modo {estado['modo']} "
              f"({estado['motivo']}). oneshot = certo mas lento; off = PDF SEM "
              f"graficos. Reinicie o processo do app (servico) pra voltar ao "
              f"modo server.", file=sys.stderr)


def _saida_segura(rc):
    """Sai sem passar pelo atexit quando o kaleido saiu do modo server.

    Com o singleton do kaleido envenenado, o atexit da lib faz close -> join
    na thread presa e o processo NUNCA termina. No modo degradado o atexit
    deste processo nao tem nada com efeito persistente: a conexao fecha no
    `finally` do main e o PDF fecha no `with` antes do OK. Os buffers de
    stdio sao a unica coisa a salvar: flush aqui.
    """
    PG = sys.modules.get("pdf_generator")
    if PG is not None and PG.kaleido_estado()["modo"] != "server":
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(rc)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        _saida_segura(e.code if isinstance(e.code, int) else (0 if e.code is None else 1))
        raise
    except BaseException:
        import traceback
        traceback.print_exc()
        _saida_segura(1)
        raise
    _saida_segura(0)
