"""Enforcement de CSRF (spec 01, story P1 CSRF — AUTH-05).

No ambiente de teste o CSRF fica desligado (o client não envia token). Aqui a
flag é religada para provar a imposição: POST sem token é rejeitado; com token
(header X-CSRFToken ou campo de form) prossegue.
"""
import re


def _token_da_pagina(client):
    html = client.get('/').get_data(as_text=True)
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, 'meta csrf-token ausente na página'
    return m.group(1)


def test_post_sem_token_csrf_rejeitado(app, login_as, ids):
    c = login_as('admin')  # login ocorre com CSRF ainda desligado
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        resp = c.post(f'/certidao/marcar_pendente_json/{ids["fgts"]}')
        assert resp.status_code == 400  # CSRFError -> 400 claro
    finally:
        app.config['WTF_CSRF_ENABLED'] = False


def test_post_com_header_token_ok(app, login_as, ids):
    c = login_as('admin')
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        token = _token_da_pagina(c)
        resp = c.post(f'/certidao/marcar_pendente_json/{ids["fgts"]}',
                      headers={'X-CSRFToken': token})
        assert resp.status_code == 200
    finally:
        app.config['WTF_CSRF_ENABLED'] = False


def test_post_com_campo_form_token_ok(app, login_as, ids):
    c = login_as('admin')
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        token = _token_da_pagina(c)
        resp = c.post('/empresa/adicionar', data={
            'csrf_token': token, 'nome': 'Nova LTDA',
            'cnpj': '22.222.222/2222-22', 'cidade': 'Tramandai', 'estado': 'RS',
        })
        assert resp.status_code != 400  # token aceito (segue o fluxo normal)
    finally:
        app.config['WTF_CSRF_ENABLED'] = False
