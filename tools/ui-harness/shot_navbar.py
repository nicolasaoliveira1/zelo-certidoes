"""Renderiza navbar.html com uma linha-guia no centro vertical e recorta o
cluster esquerdo (hambúrguer + Zelo) com zoom, pra inspeção óptica."""
from __future__ import annotations
from pathlib import Path
from playwright.sync_api import sync_playwright

AQUI = Path(__file__).resolve().parent
URL = (AQUI / "navbar.html").as_uri()
SAIDA = AQUI / "shots"


def main() -> None:
    SAIDA.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        nav = p.chromium.launch()
        for tema in ("light", "dark"):
            pg = nav.new_page(viewport={"width": 1280, "height": 120}, device_scale_factor=3)
            pg.goto(URL, wait_until="networkidle")
            pg.evaluate("() => document.fonts.ready")
            pg.evaluate(f"() => document.documentElement.setAttribute('data-bs-theme','{tema}')")
            pg.wait_for_timeout(150)
            d = pg.evaluate("() => window.medirNavbar()")
            cy = d["navbar"]["cy"]
            # linha-guia vermelha no centro da navbar
            pg.evaluate(
                "(cy) => { const g=document.createElement('div'); g.className='guia';"
                " g.style.top = cy + 'px'; document.body.appendChild(g); }", cy)
            print(f"{tema}: navbar cy={cy} | hamburger cy={d['hamburger']['cy']} | zelo cy={d['zelo']['cy']}")
            pg.screenshot(path=str(SAIDA / f"navbar_left_{tema}.png"),
                          clip={"x": 0, "y": 0, "width": 240, "height": d["navbar"]["bottom"]})
            pg.close()
        nav.close()
    print(f"OK -> {SAIDA}")


if __name__ == "__main__":
    main()
