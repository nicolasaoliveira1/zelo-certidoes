"""Testes da config de notificacoes na pagina de configuracoes (spec 03, NOTIF-01).

Destinatarios e cadencia sao editaveis sem mexer em codigo; cadencia invalida e
rejeitada; POST parcial (sem a secao) nao apaga o que ja estava salvo.
"""
from app import db
from app.models import ConfiguracaoSistema


def _post(client, **overrides):
    dados = {'a_vencer_dias': '7', 'notif_cadencia': 'semanal',
             'notif_destinatarios': 'op@x.com'}
    dados.update(overrides)
    return client.post('/configuracoes', data=dados, follow_redirects=True)


def test_salva_destinatarios_e_cadencia(client, app):
    _post(client, notif_cadencia='diaria',
          notif_destinatarios='a@x.com, b@y.com')
    with app.app_context():
        cfg = db.session.get(ConfiguracaoSistema, 1)
        assert cfg.notif_cadencia == 'diaria'
        assert cfg.notif_destinatarios == 'a@x.com, b@y.com'


def test_cadencia_invalida_rejeitada(client, app):
    _post(client, notif_cadencia='mensal')
    with app.app_context():
        cfg = db.session.get(ConfiguracaoSistema, 1)
        # invalida nao persiste; permanece no default 'semanal'
        assert cfg.notif_cadencia == 'semanal'


def test_destinatarios_vazio_vira_none(client, app):
    _post(client, notif_destinatarios='   ')
    with app.app_context():
        cfg = db.session.get(ConfiguracaoSistema, 1)
        assert cfg.notif_destinatarios is None


def test_post_parcial_nao_apaga_notificacoes(client, app):
    _post(client, notif_cadencia='diaria', notif_destinatarios='keep@x.com')
    # POST sem a secao de notificacoes (sem notif_cadencia) nao deve mexer
    client.post('/configuracoes', data={'a_vencer_dias': '7'},
                follow_redirects=True)
    with app.app_context():
        cfg = db.session.get(ConfiguracaoSistema, 1)
        assert cfg.notif_cadencia == 'diaria'
        assert cfg.notif_destinatarios == 'keep@x.com'


def test_get_mostra_campos_notificacoes(client):
    corpo = client.get('/configuracoes').get_data(as_text=True)
    assert 'notif_destinatarios' in corpo
    assert 'notif_cadencia' in corpo
