"""E2E do painel de auditoria admin (spec 01 — AUDIT-02)."""


def test_admin_ve_painel(login_as, ids):
    c = login_as('admin')  # gera um evento 'login'
    resp = c.get('/admin/auditoria')
    assert resp.status_code == 200
    assert b'login' in resp.data  # o evento de login aparece na trilha


def test_nao_admin_barrado(login_as, ids):
    # AUDIT-02.4: não-admin acessando auditoria -> 403
    resp = login_as('operador').get('/admin/auditoria')
    assert resp.status_code == 403


def test_filtro_por_acao_mostra_evento(login_as, ids):
    op = login_as('operador')
    op.post(f'/certidao/marcar_pendente_json/{ids["fgts"]}')
    resp = login_as('admin').get('/admin/auditoria?acao=certidao.marcar_pendente')
    assert resp.status_code == 200
    assert b'certidao.marcar_pendente' in resp.data


def test_filtro_sem_correspondencia_mostra_vazio(login_as, ids):
    resp = login_as('admin').get('/admin/auditoria?acao=acao.inexistente')
    assert resp.status_code == 200
    assert 'Nenhum evento'.encode() in resp.data
