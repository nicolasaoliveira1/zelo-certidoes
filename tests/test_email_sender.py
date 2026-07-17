"""Testes do transporte SMTP best-effort (spec 03, NOTIF-02).

email_sender.enviar nunca levanta: envia via SMTP com BCC (nao vaza a lista),
retorna True no sucesso e False quando SMTP nao esta configurado ou falha.
"""
from unittest.mock import MagicMock, patch

from app.services import email_sender

CONF = {
    'SMTP_HOST': 'smtp.exemplo.com', 'SMTP_PORT': 587,
    'SMTP_USER': 'usuario', 'SMTP_PASSWORD': 'senha',
    'SMTP_FROM': 'certidoes@exemplo.com', 'SMTP_USE_TLS': True, 'SMTP_TIMEOUT': 5,
}


def _fake_smtp():
    """Retorna (patcher-target, servidor_mock). O SMTP(...) e um context manager;
    __enter__ devolve o servidor onde send_message/login/starttls sao observados."""
    servidor = MagicMock()
    ctor = MagicMock()
    ctor.return_value.__enter__.return_value = servidor
    return ctor, servidor


def test_smtp_configurado_true_com_host_e_from():
    assert email_sender.smtp_configurado(CONF) is True


def test_smtp_configurado_false_sem_host_ou_from():
    assert email_sender.smtp_configurado({**CONF, 'SMTP_HOST': ''}) is False
    assert email_sender.smtp_configurado({**CONF, 'SMTP_FROM': ''}) is False


def test_enviar_sucesso_retorna_true():
    ctor, servidor = _fake_smtp()
    with patch.object(email_sender.smtplib, 'SMTP', ctor):
        ok = email_sender.enviar(CONF, ['a@x.com', 'b@y.com'], 'Assunto', 'Corpo')
    assert ok is True
    servidor.send_message.assert_called_once()


def test_enviar_usa_bcc_e_nao_vaza_lista():
    ctor, servidor = _fake_smtp()
    with patch.object(email_sender.smtplib, 'SMTP', ctor):
        email_sender.enviar(CONF, ['a@x.com', 'b@y.com'], 'Assunto', 'Corpo')
    msg = servidor.send_message.call_args.args[0]
    # destinatarios ocultos em Bcc; To recebe o proprio remetente (nao os alvos)
    assert 'a@x.com' in msg['Bcc'] and 'b@y.com' in msg['Bcc']
    assert msg['To'] == CONF['SMTP_FROM']
    assert 'a@x.com' not in (msg['To'] or '')
    # to_addrs entregue de fato aos destinatarios
    assert servidor.send_message.call_args.kwargs['to_addrs'] == ['a@x.com', 'b@y.com']


def test_enviar_com_tls_e_login_conecta_seguro():
    ctor, servidor = _fake_smtp()
    with patch.object(email_sender.smtplib, 'SMTP', ctor):
        email_sender.enviar(CONF, ['a@x.com'], 'Assunto', 'Corpo')
    servidor.starttls.assert_called_once()
    servidor.login.assert_called_once_with('usuario', 'senha')


def test_enviar_servidor_fora_retorna_false_sem_levantar():
    ctor, servidor = _fake_smtp()
    servidor.send_message.side_effect = OSError('conexao recusada')
    with patch.object(email_sender.smtplib, 'SMTP', ctor), \
            patch('app.services.retry.time.sleep'):
        ok = email_sender.enviar(CONF, ['a@x.com'], 'Assunto', 'Corpo')
    assert ok is False


def test_enviar_sem_smtp_configurado_nao_conecta_e_retorna_false():
    ctor, _ = _fake_smtp()
    with patch.object(email_sender.smtplib, 'SMTP', ctor):
        ok = email_sender.enviar({**CONF, 'SMTP_HOST': ''}, ['a@x.com'], 'S', 'C')
    assert ok is False
    ctor.assert_not_called()


def test_enviar_sem_destinatarios_retorna_false():
    ctor, _ = _fake_smtp()
    with patch.object(email_sender.smtplib, 'SMTP', ctor):
        assert email_sender.enviar(CONF, [], 'S', 'C') is False
        assert email_sender.enviar(CONF, None, 'S', 'C') is False
    ctor.assert_not_called()
