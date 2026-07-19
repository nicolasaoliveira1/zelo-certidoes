"""Testes dos alertas por e-mail (spec 03, NOTIF-03/04/05).

Falha recorrente e saldo baixo geram no maximo um e-mail por janela anti-spam;
saldo None (API fora) nao gera falso-baixo; sem SMTP nao envia.
"""
import pytest

from app import db
from app.models import ConfiguracaoSistema, NotificacaoLog
from app.services import notificacoes

_ALERTA = {'error_type': 'SELECTOR', 'alvo': 'MUNI', 'ocorrencias': 3,
           'hipotese': 'Portal pode ter mudado.'}


@pytest.fixture()
def ctx(app):
    with app.app_context():
        db.create_all()
        db.session.add(ConfiguracaoSistema(id=1, notif_destinatarios='op@x.com',
                                           notif_cadencia='semanal'))
        db.session.commit()
        app.config.update(SMTP_HOST='smtp', SMTP_FROM='f@x.com',
                          CAPTCHA_2_SALDO_MINIMO=2.0, NOTIF_ALERTA_JANELA_HORAS=24)
        yield app
        db.session.rollback()
        db.drop_all()


def _mock_envio(monkeypatch, enviado=True):
    monkeypatch.setattr(notificacoes.email_sender, 'smtp_configurado', lambda c: True)
    enviados = []
    monkeypatch.setattr(notificacoes.email_sender, 'enviar',
                        lambda cfg, dest, assunto, corpo: enviados.append(assunto)
                        or enviado)
    return enviados


def _sem_alertas(monkeypatch):
    monkeypatch.setattr(notificacoes.diagnostics, 'alertas_ativos', lambda: [])


def _saldo(monkeypatch, valor):
    monkeypatch.setattr(notificacoes.captcha_solver, 'consultar_saldo', lambda c: valor)


# --- falha recorrente ------------------------------------------------------

def test_falha_recorrente_envia_um_alerta_e_nao_reenvia_na_janela(ctx, monkeypatch):
    enviados = _mock_envio(monkeypatch)
    monkeypatch.setattr(notificacoes.diagnostics, 'alertas_ativos', lambda: [_ALERTA])
    _saldo(monkeypatch, 10.0)  # saldo alto: sem alerta de saldo

    assert notificacoes.enviar_alertas(ctx) == 1
    assert NotificacaoLog.query.filter_by(tipo='alerta_falha').count() == 1
    # 2a passada dentro da janela: um unico alerta, nao dois (AC P2 anti-spam)
    assert notificacoes.enviar_alertas(ctx) == 0
    assert NotificacaoLog.query.filter_by(tipo='alerta_falha').count() == 1
    assert len(enviados) == 1


def test_alerta_falha_contem_tipo_alvo_e_hipotese(ctx, monkeypatch):
    corpos = []
    monkeypatch.setattr(notificacoes.email_sender, 'smtp_configurado', lambda c: True)
    monkeypatch.setattr(notificacoes.email_sender, 'enviar',
                        lambda cfg, dest, assunto, corpo: corpos.append((assunto, corpo))
                        or True)
    monkeypatch.setattr(notificacoes.diagnostics, 'alertas_ativos', lambda: [_ALERTA])
    _saldo(monkeypatch, 10.0)

    notificacoes.enviar_alertas(ctx)
    assunto, corpo = corpos[0]
    assert 'SELECTOR' in assunto and 'MUNI' in assunto
    assert 'Portal pode ter mudado.' in corpo


# --- saldo 2captcha --------------------------------------------------------

def test_saldo_baixo_envia_um_alerta(ctx, monkeypatch):
    enviados = _mock_envio(monkeypatch)
    _sem_alertas(monkeypatch)
    _saldo(monkeypatch, 0.3)  # < 2.0

    assert notificacoes.enviar_alertas(ctx) == 1
    assert NotificacaoLog.query.filter_by(tipo='alerta_saldo').count() == 1
    assert '0.30' in enviados[0] or 'saldo' in enviados[0].lower()


def test_saldo_none_api_fora_nao_gera_falso_baixo(ctx, monkeypatch):
    _mock_envio(monkeypatch)
    _sem_alertas(monkeypatch)
    _saldo(monkeypatch, None)

    assert notificacoes.enviar_alertas(ctx) == 0
    assert NotificacaoLog.query.filter_by(tipo='alerta_saldo').count() == 0


def test_saldo_alto_nao_alerta(ctx, monkeypatch):
    _mock_envio(monkeypatch)
    _sem_alertas(monkeypatch)
    _saldo(monkeypatch, 50.0)

    assert notificacoes.enviar_alertas(ctx) == 0


def test_saldo_baixo_nao_reenvia_dentro_da_janela(ctx, monkeypatch):
    _mock_envio(monkeypatch)
    _sem_alertas(monkeypatch)
    _saldo(monkeypatch, 0.3)

    assert notificacoes.enviar_alertas(ctx) == 1
    assert notificacoes.enviar_alertas(ctx) == 0  # AC saldo.3
    assert NotificacaoLog.query.filter_by(tipo='alerta_saldo').count() == 1


# --- sem SMTP --------------------------------------------------------------

def test_sem_smtp_nao_envia_nenhum_alerta(ctx, monkeypatch):
    monkeypatch.setattr(notificacoes.email_sender, 'smtp_configurado', lambda c: False)
    monkeypatch.setattr(notificacoes.diagnostics, 'alertas_ativos', lambda: [_ALERTA])
    _saldo(monkeypatch, 0.3)
    chamou = []
    monkeypatch.setattr(notificacoes.email_sender, 'enviar',
                        lambda *a, **k: chamou.append(1) or True)

    assert notificacoes.enviar_alertas(ctx) == 0
    assert chamou == []
