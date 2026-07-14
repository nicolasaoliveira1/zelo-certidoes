"""E2E de autorização por papel (spec 01, story P1 Papéis).

ACs: AUTH-03.1 (leitura barrada em ação de operador/admin → 403, sem mutar),
AUTH-03.2 (operador barrado em ação de admin → 403), AUTH-03.3 (admin faz tudo),
AUTH-04 (checagem via decorator único).
"""
from app import db
from app.models import Certidao, Empresa, StatusEspecial


# --- leitura barrada (AUTH-03.1) ---

def test_leitura_nao_marca_pendente(login_as, ids, app):
    c = login_as('leitura')
    resp = c.post(f'/certidao/marcar_pendente_json/{ids["fgts"]}')
    assert resp.status_code == 403
    # estado não muda no 403
    with app.app_context():
        cert = db.session.get(Certidao, ids['fgts'])
        assert cert.status_especial is None


def test_leitura_nao_adiciona_empresa(login_as):
    c = login_as('leitura')
    resp = c.post('/empresa/adicionar', data={'nome': 'X', 'cnpj': '00.000.000/0000-00',
                                              'cidade': 'Tramandai', 'estado': 'RS'})
    assert resp.status_code == 403


def test_leitura_ve_dashboard(login_as):
    # leitura tem acesso de leitura ao painel
    assert login_as('leitura').get('/').status_code == 200


# --- operador barrado em ação de admin (AUTH-03.2) ---

def test_operador_nao_remove_empresa(login_as, ids, app):
    c = login_as('operador')
    resp = c.post(f'/empresa/{ids["empresa"]}/remover')
    assert resp.status_code == 403
    with app.app_context():
        assert db.session.get(Empresa, ids['empresa']) is not None  # não removida


def test_operador_nao_salva_config(login_as):
    c = login_as('operador')
    resp = c.post('/configuracoes', data={'a_vencer_dias': '10'})
    assert resp.status_code == 403


# --- operador autorizado na sua faixa (AUTH-03) ---

def test_operador_marca_pendente_ok(login_as, ids, app):
    c = login_as('operador')
    resp = c.post(f'/certidao/marcar_pendente_json/{ids["fgts"]}')
    assert resp.status_code == 200
    with app.app_context():
        cert = db.session.get(Certidao, ids['fgts'])
        assert cert.status_especial == StatusEspecial.PENDENTE


# --- admin faz tudo (AUTH-03.3) ---

def test_admin_salva_config_ok(login_as):
    assert login_as('admin').post('/configuracoes', data={'a_vencer_dias': '10'}).status_code in (200, 302)


def test_admin_remove_empresa_ok(login_as, ids, app):
    c = login_as('admin')
    resp = c.post(f'/empresa/{ids["empresa"]}/remover', data={'confirm': '1'})
    assert resp.status_code != 403
    with app.app_context():
        assert db.session.get(Empresa, ids['empresa']) is None  # removida
