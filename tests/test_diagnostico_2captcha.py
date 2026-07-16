"""Testes do painel de saldo 2captcha e do link de Diagnóstico no menu (admin)."""
from app import captcha_solver


def test_2captcha_endpoint_sem_chave(login_as, app, monkeypatch):
    monkeypatch.setitem(app.config, 'CAPTCHA_2_API_KEY', '')
    c = login_as('admin')
    r = c.get('/diagnostico/2captcha')
    assert r.status_code == 200
    d = r.get_json()
    assert d['configurado'] is False
    assert d['saldo'] is None
    assert d['baixo'] is False


def test_2captcha_endpoint_saldo_baixo(login_as, app, monkeypatch):
    monkeypatch.setitem(app.config, 'CAPTCHA_2_API_KEY', 'k')
    monkeypatch.setitem(app.config, 'CAPTCHA_2_SALDO_MINIMO', 2.0)
    monkeypatch.setattr(captcha_solver, 'consultar_saldo', lambda cfg: 0.5)
    c = login_as('admin')
    d = c.get('/diagnostico/2captcha').get_json()
    assert d['configurado'] is True
    assert d['saldo'] == 0.5
    assert d['baixo'] is True


def test_2captcha_endpoint_saldo_ok(login_as, app, monkeypatch):
    monkeypatch.setitem(app.config, 'CAPTCHA_2_API_KEY', 'k')
    monkeypatch.setitem(app.config, 'CAPTCHA_2_SALDO_MINIMO', 2.0)
    monkeypatch.setattr(captcha_solver, 'consultar_saldo', lambda cfg: 15.0)
    c = login_as('admin')
    d = c.get('/diagnostico/2captcha').get_json()
    assert d['saldo'] == 15.0
    assert d['baixo'] is False


def test_2captcha_endpoint_requer_admin(login_as):
    c = login_as('operador')
    assert c.get('/diagnostico/2captcha').status_code == 403


def test_menu_mostra_diagnostico_para_admin(login_as):
    corpo = login_as('admin').get('/').get_data(as_text=True)
    assert 'Diagnóstico' in corpo


def test_menu_esconde_diagnostico_para_leitura(login_as):
    corpo = login_as('leitura').get('/').get_data(as_text=True)
    assert 'Diagnóstico' not in corpo
