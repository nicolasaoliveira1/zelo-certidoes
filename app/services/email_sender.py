"""Transporte SMTP best-effort para notificacoes (spec 03, AD-011, NOTIF-02).

Wrapper fino sobre `smtplib` (stdlib, sem deps novas): monta a mensagem, conecta
(TLS opcional), envia com retry leve e destinatarios em BCC (nao vaza a lista).
Best-effort como `captcha_solver.consultar_saldo`: NUNCA levanta — uma falha de
SMTP loga e retorna False, sem derrubar o job do agendador.
"""
import smtplib
from email.message import EmailMessage

from app.services.correlation import CorrelationContext
from app.services.execution_logger import log_event
from app.services.retry import retry_call


def smtp_configurado(config):
    """True quando ha host e remetente. O destinatario e validado no chamador
    (vem de ConfiguracaoSistema, nao do env)."""
    host = (config.get('SMTP_HOST') or '').strip()
    remetente = (config.get('SMTP_FROM') or '').strip()
    return bool(host and remetente)


def _enviar_mensagem(config, msg, destinatarios):
    host = (config.get('SMTP_HOST') or '').strip()
    port = int(config.get('SMTP_PORT') or 587)
    user = (config.get('SMTP_USER') or '').strip()
    senha = config.get('SMTP_PASSWORD') or ''
    usar_tls = bool(config.get('SMTP_USE_TLS', True))
    timeout = int(config.get('SMTP_TIMEOUT') or 20)

    with smtplib.SMTP(host, port, timeout=timeout) as servidor:
        if usar_tls:
            servidor.starttls()
        if user:
            servidor.login(user, senha)
        # send_message respeita From/To/Bcc; passamos to_addrs explicito para
        # garantir a entrega aos destinatarios ocultos.
        servidor.send_message(msg, to_addrs=destinatarios)


def enviar(config, destinatarios, assunto, corpo_texto, *, execution_id=None):
    """Envia um e-mail de texto simples via SMTP. Retorna True/False, nunca levanta.

    Os destinatarios vao em BCC (o cabecalho To recebe o proprio remetente) para
    nao expor a lista entre eles. Faz retry leve; se ainda falhar, loga e retorna
    False — o agendador nao pode cair por causa de e-mail (NOTIF-02)."""
    if execution_id:
        CorrelationContext.set_execution_id(execution_id)

    destinatarios = [d for d in (destinatarios or []) if d]
    if not smtp_configurado(config) or not destinatarios:
        log_event('notif_email_nao_enviado', level='WARNING',
                  motivo='sem_smtp_ou_destinatario',
                  tem_smtp=smtp_configurado(config), destinatarios=len(destinatarios))
        return False

    remetente = (config.get('SMTP_FROM') or '').strip()
    msg = EmailMessage()
    msg['From'] = remetente
    msg['To'] = remetente
    msg['Bcc'] = ', '.join(destinatarios)
    msg['Subject'] = assunto
    msg.set_content(corpo_texto)

    try:
        retry_call(
            lambda: _enviar_mensagem(config, msg, destinatarios),
            max_attempts=3, base_delay=0.5, jitter=0.2,
            on_retry=lambda attempt, delay, exc: log_event(
                'email_retry', level='WARNING', attempt=attempt,
                delay_ms=int(delay * 1000), error=str(exc)),
        )
    except Exception as exc:
        log_event('email_falhou', level='ERROR', error=str(exc),
                  destinatarios=len(destinatarios))
        return False

    log_event('email_enviado', status='ok', destinatarios=len(destinatarios),
              assunto=assunto)
    return True
