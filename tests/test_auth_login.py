"""E2E de login/logout e enforcement de autenticação (spec 01, story P1 Login).

ACs cobertas: AUTH-01.1 (barra sem login: página→redirect, API→401),
AUTH-01.2 (login cria sessão), AUTH-01.3 (mensagem genérica),
AUTH-01.5 (logout), edge "usuário desativado barrado no próximo request".
"""
from app import db
from app.models import Usuario


# --- AUTH-01.1: barra acesso sem login ---

def test_get_pagina_sem_login_redireciona(client_anon):
    resp = client_anon.get('/')
    assert resp.status_code == 302
    assert '/login' in resp.headers['Location']


def test_get_api_sem_login_retorna_401(client_anon):
    resp = client_anon.get('/api/pendencias')
    assert resp.status_code == 401


def test_post_sem_login_retorna_401(client_anon, ids):
    resp = client_anon.post(f'/certidao/marcar_pendente_json/{ids["trabalhista"]}')
    assert resp.status_code == 401


def test_get_login_e_publico(client_anon):
    resp = client_anon.get('/login')
    assert resp.status_code == 200


# --- AUTH-01.2 / 01.3: login válido e inválido ---

def test_login_valido_cria_sessao(client_anon):
    resp = client_anon.post('/login', data={'username': 'admin_test', 'senha': 'senha-admin-1'})
    assert resp.status_code == 302  # redireciona após entrar
    # sessão ativa: agora a home responde sem redirect ao login
    home = client_anon.get('/')
    assert home.status_code == 200


def test_login_invalido_mensagem_generica(client_anon):
    resp = client_anon.post('/login', data={'username': 'admin_test', 'senha': 'errada'})
    assert resp.status_code == 401
    corpo = resp.get_data(as_text=True)
    assert 'inválidos' in corpo  # mensagem genérica; não revela existência
    # segue barrado
    assert client_anon.get('/').status_code == 302


def test_login_usuario_inexistente_tambem_generico(client_anon):
    resp = client_anon.post('/login', data={'username': 'fantasma', 'senha': 'x'})
    assert resp.status_code == 401
    assert 'inválidos' in resp.get_data(as_text=True)


# --- AUTH-01.5: logout ---

def test_logout_encerra_sessao(login_as):
    c = login_as('admin')
    assert c.get('/').status_code == 200
    resp = c.post('/logout')
    assert resp.status_code == 302
    # sessão encerrada: home volta a redirecionar para login
    assert c.get('/').status_code == 302


# --- open redirect no next do login ---

def test_login_next_backslash_bloqueia_redirect_externo(client_anon):
    resp = client_anon.post('/login', data={
        'username': 'admin_test', 'senha': 'senha-admin-1', 'next': '/\\evil.com'})
    assert resp.status_code == 302
    assert 'evil.com' not in resp.headers.get('Location', '')


def test_login_next_local_preservado(client_anon):
    resp = client_anon.post('/login', data={
        'username': 'admin_test', 'senha': 'senha-admin-1', 'next': '/empresas'})
    assert resp.headers.get('Location', '').endswith('/empresas')


# --- form POST de página (sem login) redireciona; endpoint de API responde 401 ---

def test_form_post_sem_login_redireciona_para_login(client_anon):
    resp = client_anon.post('/empresa/adicionar', data={
        'nome': 'X', 'cnpj': '00.000.000/0000-00', 'cidade': 'Tramandai', 'estado': 'RS'})
    assert resp.status_code == 302
    assert '/login' in resp.headers.get('Location', '')


# --- edge: usuário desativado barrado no próximo request ---

def test_usuario_desativado_barrado_no_proximo_request(login_as, app):
    c = login_as('operador')
    assert c.get('/api/pendencias').status_code == 200
    with app.app_context():
        u = Usuario.query.filter_by(username='op_test').first()
        u.ativo = False
        db.session.commit()
    # user_loader retorna None p/ inativo -> anônimo -> API 401
    assert c.get('/api/pendencias').status_code == 401
