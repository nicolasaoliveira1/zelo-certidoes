"""Testes do digest periodico e do anti-spam durável (spec 03, NOTIF-01/04).

Os numeros do digest batem com a classificacao do painel; a cadencia decide se
envia; sem SMTP nao envia (loga); 0/0/0 envia "tudo em dia" salvo flag; envio so
e registrado quando de fato ocorre.
"""
from datetime import date, datetime, timedelta

import pytest

from app import db
from app.models import (Certidao, ConfiguracaoSistema, Empresa, NotificacaoLog,
                        StatusEspecial, TipoCertidao)
from app.services import notificacoes
from app.services.snapshot_service import classificar_status_certidao


def _empresa():
    emp = Empresa(nome='E', cnpj=f'00.000.000/000{Empresa.query.count()}-00',
                  estado='RS', cidade='Tramandai')
    db.session.add(emp)
    db.session.commit()
    return emp


def _cert(emp, tipo, *, validade=None, pendente=False):
    c = Certidao(tipo=tipo, empresa=emp, data_validade=validade,
                 status_especial=(StatusEspecial.PENDENTE if pendente else None))
    db.session.add(c)
    db.session.commit()
    return c


def _config(destinatarios='op@x.com', cadencia='semanal'):
    cfg = ConfiguracaoSistema(id=1, notif_destinatarios=destinatarios,
                              notif_cadencia=cadencia)
    db.session.add(cfg)
    db.session.commit()
    return cfg


@pytest.fixture()
def ctx(app):
    with app.app_context():
        db.create_all()
        yield app
        db.session.rollback()
        db.session.remove()
        db.drop_all()


# --- _destinatarios --------------------------------------------------------

def test_destinatarios_parse_separadores_trim_e_dedup(ctx):
    cfg = _config('a@x.com, b@y.com; a@x.com\nc@z.com,  , semarroba')
    assert notificacoes._destinatarios(cfg) == ['a@x.com', 'b@y.com', 'c@z.com']


def test_destinatarios_vazio_quando_sem_config(ctx):
    assert notificacoes._destinatarios(None) == []
    assert notificacoes._destinatarios(_config('')) == []


# --- contagem bate com o painel (AC P1.2) ----------------------------------

def test_contagem_bate_com_classificacao_do_painel(ctx):
    emp = _empresa()
    hoje = date.today()
    _cert(emp, TipoCertidao.FGTS, validade=hoje + timedelta(days=3))   # a_vencer
    _cert(emp, TipoCertidao.ESTADUAL, validade=hoje + timedelta(days=2))  # a_vencer
    _cert(emp, TipoCertidao.FEDERAL, validade=hoje - timedelta(days=5))  # vencida
    _cert(emp, TipoCertidao.MUNICIPAL, pendente=True)                    # pendente
    _cert(emp, TipoCertidao.TRABALHISTA, pendente=True)                  # pendente
    _cert(emp, TipoCertidao.FGTS, validade=hoje + timedelta(days=365))   # valida (nao conta)

    _, _, resumo = notificacoes.montar_digest()

    # referencia independente: reclassifica a carteira do zero
    esperado = {'a_vencer': 0, 'vencidas': 0, 'pendentes': 0}
    for c in Certidao.query.all():
        k = classificar_status_certidao(c, hoje)
        if k in esperado:
            esperado[k] += 1
    assert resumo == esperado
    assert resumo == {'a_vencer': 2, 'vencidas': 1, 'pendentes': 2}


# --- cadencia / dedup ------------------------------------------------------

def test_digest_devido_quando_nunca_enviado(ctx):
    assert notificacoes._digest_devido(_config(cadencia='semanal')) is True


def test_digest_nao_devido_dentro_da_semana(ctx):
    _config(cadencia='semanal')
    db.session.add(NotificacaoLog(
        chave='digest', tipo='digest',
        enviada_em=datetime.now() - timedelta(days=3)))
    db.session.commit()
    assert notificacoes._digest_devido(notificacoes._config()) is False


def test_digest_devido_apos_intervalo_diario(ctx):
    _config(cadencia='diaria')
    db.session.add(NotificacaoLog(
        chave='digest', tipo='digest',
        enviada_em=datetime.now() - timedelta(days=1, hours=1)))
    db.session.commit()
    assert notificacoes._digest_devido(notificacoes._config()) is True


# --- enviar_digest_se_devido -----------------------------------------------

def test_sem_smtp_nao_envia_e_loga(ctx, monkeypatch):
    _config()
    ctx.config['SMTP_HOST'] = ''  # nao configurado
    chamou = []
    monkeypatch.setattr(notificacoes.email_sender, 'enviar',
                        lambda *a, **k: chamou.append(1) or True)
    assert notificacoes.enviar_digest_se_devido(ctx) is False
    assert chamou == []  # nao tentou enviar (AC P1.3)


def test_vazio_envia_tudo_em_dia_por_padrao(ctx, monkeypatch):
    _config()
    ctx.config.update(SMTP_HOST='smtp', SMTP_FROM='f@x.com',
                      NOTIF_DIGEST_ENVIAR_VAZIO=True)
    capturado = {}
    monkeypatch.setattr(notificacoes.email_sender, 'smtp_configurado', lambda c: True)
    monkeypatch.setattr(notificacoes.email_sender, 'enviar',
                        lambda cfg, dest, assunto, corpo: capturado.update(
                            assunto=assunto, corpo=corpo) or True)
    assert notificacoes.enviar_digest_se_devido(ctx) is True
    assert 'tudo em dia' in capturado['assunto']


def test_vazio_omitido_quando_flag_desligada(ctx, monkeypatch):
    _config()
    ctx.config.update(SMTP_HOST='smtp', SMTP_FROM='f@x.com',
                      NOTIF_DIGEST_ENVIAR_VAZIO=False)
    chamou = []
    monkeypatch.setattr(notificacoes.email_sender, 'smtp_configurado', lambda c: True)
    monkeypatch.setattr(notificacoes.email_sender, 'enviar',
                        lambda *a, **k: chamou.append(1) or True)
    assert notificacoes.enviar_digest_se_devido(ctx) is False
    assert chamou == []


def test_envio_ok_registra_e_nao_reenvia_na_cadencia(ctx, monkeypatch):
    _config(cadencia='semanal')
    _cert(_empresa(), TipoCertidao.FGTS,
          validade=date.today() + timedelta(days=3))  # digest nao-vazio
    ctx.config.update(SMTP_HOST='smtp', SMTP_FROM='f@x.com')
    monkeypatch.setattr(notificacoes.email_sender, 'smtp_configurado', lambda c: True)
    monkeypatch.setattr(notificacoes.email_sender, 'enviar', lambda *a, **k: True)

    assert notificacoes.enviar_digest_se_devido(ctx) is True
    assert NotificacaoLog.query.filter_by(chave='digest').count() == 1
    # 2a chamada no mesmo dia: cadencia semanal nao venceu -> nao reenvia
    assert notificacoes.enviar_digest_se_devido(ctx) is False
    assert NotificacaoLog.query.filter_by(chave='digest').count() == 1


def test_envio_falho_nao_registra_permanece_devido(ctx, monkeypatch):
    _config(cadencia='semanal')
    _cert(_empresa(), TipoCertidao.FGTS,
          validade=date.today() + timedelta(days=3))  # digest nao-vazio
    ctx.config.update(SMTP_HOST='smtp', SMTP_FROM='f@x.com')
    monkeypatch.setattr(notificacoes.email_sender, 'smtp_configurado', lambda c: True)
    monkeypatch.setattr(notificacoes.email_sender, 'enviar', lambda *a, **k: False)

    assert notificacoes.enviar_digest_se_devido(ctx) is False
    assert NotificacaoLog.query.filter_by(chave='digest').count() == 0
    assert notificacoes._digest_devido(notificacoes._config()) is True
