"""Renderiza status.html (banda de status, espinha, texto e chips) nos dois
temas, pra inspeção da Fase 2 do plano Zelo."""
from __future__ import annotations
from pathlib import Path
from playwright.sync_api import sync_playwright

AQUI = Path(__file__).resolve().parent
URL = (AQUI / "status.html").as_uri()
SAIDA = AQUI / "shots"


def main() -> None:
    SAIDA.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        nav = p.chromium.launch()
        for tema in ("light", "dark"):
            pg = nav.new_page(viewport={"width": 980, "height": 520}, device_scale_factor=2)
            pg.goto(URL, wait_until="networkidle")
            pg.evaluate("() => document.fonts.ready")
            pg.evaluate(f"() => document.documentElement.setAttribute('data-bs-theme','{tema}')")
            pg.wait_for_timeout(150)
            pg.screenshot(path=str(SAIDA / f"status_{tema}.png"), full_page=True)
            pg.close()
        nav.close()
    print(f"OK -> {SAIDA}")


if __name__ == "__main__":
    main()
