import json
import os
import sys
from playwright.sync_api import sync_playwright

PDF_OPTS = dict(
    landscape=True,
    format="A4",
    print_background=True,
    margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
)


def _render_one(browser, html, pdf_path):
    page = browser.new_page()
    try:
        page.set_content(html, wait_until="networkidle", timeout=30000)
        page.pdf(path=pdf_path, **PDF_OPTS)
    finally:
        page.close()


def main():
    raw = sys.stdin.buffer.read().decode("utf-8")

    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError:
        manifest = None

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            if manifest and isinstance(manifest, list):
                # Modo lote: ISOLA cada item. Um item ruim (render/timeout) NAO
                # derruba os vizinhos nem aborta o processo -> sai 0 SEMPRE e
                # reporta status por item no stdout (JSON). O parent le os PDFs
                # por presenca em disco; este status da a mensagem de erro dos
                # ausentes e suprime stub truncado (ok=False).
                results = []
                for item in manifest:
                    pdf_path = item["pdf_path"]
                    rec = {"index": item.get("index"), "pdf_path": pdf_path,
                           "ok": True, "error": None}
                    try:
                        with open(item["html_path"], "r", encoding="utf-8") as f:
                            html = f.read()
                        _render_one(browser, html, pdf_path)
                    except Exception as e:
                        rec["ok"] = False
                        rec["error"] = f"{type(e).__name__}: {e}"
                        # presenca de arquivo = render completo: nao deixar stub
                        try:
                            if os.path.exists(pdf_path):
                                os.unlink(pdf_path)
                        except OSError:
                            pass
                    results.append(rec)
                sys.stdout.write(json.dumps(results))
                sys.stdout.flush()
            else:
                # Modo single (gerar_pdf_empresa): semantica de exit code
                # inalterada — uma falha propaga (1 PDF, sem vizinhos a proteger).
                output_path = sys.argv[1]
                _render_one(browser, raw, output_path)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
