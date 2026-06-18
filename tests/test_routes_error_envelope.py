"""Garante que o envelope de erro JSON carrega error_type e acao."""
from app import routes


def test_json_error_enriquece_com_excecao(app):
    with app.test_request_context('/'):
        resp, code = routes._json_error(code=500, exc=Exception('Access is denied'))
        data = resp.get_json()
    assert code == 500
    assert data['status'] == 'error'
    assert data['error_type'] == 'PERMISSION'
    assert data['acao']
    assert data['message'].startswith('Permissao negada')


def test_json_error_sem_excecao_mantem_compat(app):
    with app.test_request_context('/'):
        resp, code = routes._json_error('Certidão inválida.', 400)
        data = resp.get_json()
    assert code == 400
    assert data['message'] == 'Certidão inválida.'
    assert 'error_type' not in data
