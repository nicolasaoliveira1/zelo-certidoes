"""Testes da configuração do agendador na página de configurações (spec 02, SCHED-04)."""
from app import db
from app.models import ConfiguracaoSistema
from app.services import agendador


def _post(client, **overrides):
    dados = {'a_vencer_dias': '7', 'agendador_hora': '5', 'agendador_ativo': 'on'}
    dados.update(overrides)
    return client.post('/configuracoes', data=dados, follow_redirects=True)


def test_salva_hora_e_ativo(client, app):
    _post(client, agendador_hora='8', agendador_ativo='on')
    with app.app_context():
        cfg = db.session.get(ConfiguracaoSistema, 1)
        assert cfg.agendador_hora == 8
        assert cfg.agendador_ativo is True


def test_desliga_agendador_quando_switch_off(client, app):
    # checkbox desmarcado nao envia o campo
    _post(client, agendador_ativo=None)
    with app.app_context():
        cfg = db.session.get(ConfiguracaoSistema, 1)
        assert cfg.agendador_ativo is False


def test_hora_invalida_rejeitada(client, app):
    _post(client, agendador_hora='99')
    with app.app_context():
        cfg = db.session.get(ConfiguracaoSistema, 1)
        # hora invalida nao persiste (fica no default 3)
        assert cfg.agendador_hora == 3


def test_salvar_reprograma_o_scheduler(client, app, monkeypatch):
    chamado = {}
    monkeypatch.setattr(agendador, 'reprogramar', lambda a: chamado.setdefault('sim', True))
    _post(client, agendador_hora='6')
    assert chamado.get('sim') is True


def test_reprogramar_falho_nao_impede_salvar(client, app, monkeypatch):
    def _boom(a):
        raise RuntimeError('scheduler down')
    monkeypatch.setattr(agendador, 'reprogramar', _boom)
    resp = _post(client, agendador_hora='7')
    assert resp.status_code == 200
    with app.app_context():
        cfg = db.session.get(ConfiguracaoSistema, 1)
        assert cfg.agendador_hora == 7  # salvou apesar do reprogramar falhar


def test_configuracoes_get_mostra_campos_agendador(client):
    resp = client.get('/configuracoes')
    assert resp.status_code == 200
    corpo = resp.get_data(as_text=True)
    assert 'agendador_hora' in corpo
    assert 'agendador_ativo' in corpo
