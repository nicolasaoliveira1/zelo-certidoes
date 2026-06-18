"""Testes das pre-checagens (preflight) de emissao."""
from app.services import preflight


def test_checar_emissao_ok_sem_problemas(monkeypatch):
    monkeypatch.setattr(preflight.health, '_check_network_path', lambda: (True, 'ok'))
    monkeypatch.setattr(preflight.health, '_check_chrome_profile', lambda cfg: (True, 'ok'))
    problemas = preflight.checar_emissao({}, precisa_solver=False)
    assert problemas == []


def test_checar_emissao_detecta_rede_indisponivel(monkeypatch):
    monkeypatch.setattr(preflight.health, '_check_network_path', lambda: (False, 'sem Z:'))
    monkeypatch.setattr(preflight.health, '_check_chrome_profile', lambda cfg: (True, 'ok'))
    problemas = preflight.checar_emissao({}, precisa_solver=False)
    assert len(problemas) == 1
    assert problemas[0]['error_type'] == 'NETWORK_PATH'
    assert problemas[0]['acao']


def test_checar_emissao_solver_so_quando_exigido(monkeypatch):
    monkeypatch.setattr(preflight.health, '_check_network_path', lambda: (True, 'ok'))
    monkeypatch.setattr(preflight.health, '_check_chrome_profile', lambda cfg: (True, 'ok'))
    monkeypatch.setattr(preflight.health, '_check_solver_config', lambda cfg: (False, 'sem chave'))
    assert preflight.checar_emissao({}, precisa_solver=False) == []
    problemas = preflight.checar_emissao({}, precisa_solver=True)
    assert problemas and problemas[0]['error_type'] == 'CAPTCHA'


def test_lote_iniciar_bloqueado_por_preflight(client, ids, monkeypatch):
    from app.services import health
    monkeypatch.setattr(health, '_check_network_path', lambda: (False, 'sem Z:'))
    r = client.post('/fgts/lote/iniciar', json={'certidao_id': ids['fgts']})
    assert r.status_code == 409
    j = r.get_json()
    assert j['status'] == 'error'
    assert j['error_type'] == 'NETWORK_PATH'
    assert j['acao']
