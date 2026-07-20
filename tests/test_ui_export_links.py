"""Testes do wiring de UI da exportacao (spec 04, EXPORT-01/EXPORT-03/EXPORT-05).

Botao 'Exportar carteira' no dashboard, botao 'Dossie' por empresa (so operador+)
e link 'Produtividade' na navegacao.
"""


def test_dashboard_tem_botao_exportar_carteira(login_as):
    html = login_as('leitura').get('/').get_data(as_text=True)
    assert 'id="btn-exportar-carteira"' in html
    assert '/exportar/carteira.xlsx' in html


def test_dossie_visivel_para_operador(login_as, ids):
    html = login_as('operador').get('/').get_data(as_text=True)
    assert f'/exportar/dossie/{ids["empresa"]}.pdf' in html


def test_dossie_oculto_para_leitura(login_as, ids):
    html = login_as('leitura').get('/').get_data(as_text=True)
    assert f'/exportar/dossie/{ids["empresa"]}.pdf' not in html
    assert 'btn-dossie' not in html


def test_link_produtividade_no_menu(login_as):
    # visivel a qualquer papel autenticado (leitura+)
    html = login_as('leitura').get('/').get_data(as_text=True)
    assert '/produtividade' in html
    assert 'Produtividade' in html
