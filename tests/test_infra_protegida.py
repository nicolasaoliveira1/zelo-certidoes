"""E2E da proteção de infra P3 (spec 01 — AUTH-07)."""


def test_diagnostico_nao_admin_403(login_as, ids):
    assert login_as('operador').get('/diagnostico').status_code == 403


def test_diagnostico_admin_ok(login_as, ids):
    assert login_as('admin').get('/diagnostico').status_code == 200


def test_health_publico_minimo(client_anon):
    resp = client_anon.get('/health')  # liveness público, sem login
    assert resp.status_code == 200
    assert resp.get_json() == {'status': 'ok'}
    assert b'checks' not in resp.data  # não vaza detalhe de infra


def test_health_detalhado_exige_admin(client_anon, login_as, ids):
    # anônimo pedindo detalhe -> 403
    assert client_anon.get('/health?detalhado=1').status_code == 403
    # admin -> detalhe com checks
    resp = login_as('admin').get('/health?detalhado=1')
    assert resp.status_code in (200, 503)
    assert 'checks' in resp.get_json()
