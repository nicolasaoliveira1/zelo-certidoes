"""Orquestracao das notificacoes por e-mail (spec 03, AD-011).

Decide O QUE enviar (digest periodico de vencimentos; alertas de falha recorrente
e de saldo baixo) e aplica anti-spam DURAVEL via `NotificacaoLog` (chave + janela),
que sobrevive a restart. O transporte fica em `email_sender` (best-effort). Nada
aqui pode derrubar o job do agendador — falhas sao logadas, nao propagadas.

Este modulo NAO importa `agendador` (evita ciclo): os jobs e que chamam este.
"""
from datetime import date, datetime, timedelta

from app import captcha_solver, db
from app.models import Certidao, ConfiguracaoSistema, NotificacaoLog
from app.services import diagnostics, email_sender
from app.services.execution_logger import log_event
from app.services.snapshot_service import classificar_status_certidao

# Cadencia do digest -> intervalo minimo entre envios (dias).
_CADENCIA_DIAS = {'semanal': 7, 'diaria': 1}


# --- config / destinatarios ------------------------------------------------

def _config():
    return db.session.get(ConfiguracaoSistema, 1)


def _destinatarios(cfg):
    """Lista de e-mails de `notif_destinatarios` (separados por virgula/;/linha),
    aparados, sem duplicatas (ordem preservada) e contendo '@'."""
    if cfg is None or not cfg.notif_destinatarios:
        return []
    bruto = cfg.notif_destinatarios.replace(';', ',').replace('\n', ',')
    vistos = []
    for parte in bruto.split(','):
        email = parte.strip()
        if email and '@' in email and email not in vistos:
            vistos.append(email)
    return vistos


# --- anti-spam durável (NotificacaoLog) ------------------------------------

def _ultimo_envio(chave):
    """Datetime do ultimo envio registrado para a chave, ou None."""
    try:
        row = (NotificacaoLog.query
               .filter_by(chave=chave)
               .order_by(NotificacaoLog.enviada_em.desc())
               .first())
    except Exception:
        return None
    return row.enviada_em if row else None


def _deduplicado(chave, janela_horas):
    """True se ja houve envio da chave dentro da janela (nao reenviar)."""
    ultimo = _ultimo_envio(chave)
    if ultimo is None:
        return False
    return (datetime.now() - ultimo) < timedelta(hours=janela_horas)


def _registrar_envio(chave, tipo, detalhe=None):
    """Grava o envio no NotificacaoLog (best-effort; nunca propaga erro)."""
    try:
        db.session.add(NotificacaoLog(
            chave=chave, tipo=tipo,
            detalhe=(detalhe[:500] if detalhe else None)))
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        log_event('notif_log_falhou', level='WARNING', chave=chave, error=str(exc))


# --- digest ----------------------------------------------------------------

def _contagem_carteira():
    """Conta a_vencer/vencidas/pendentes pela MESMA classificacao do painel
    (snapshot_service), para os numeros baterem com o que o operador ve."""
    hoje = date.today()
    contagem = {'a_vencer': 0, 'vencidas': 0, 'pendentes': 0}
    for certidao in Certidao.query.all():
        chave = classificar_status_certidao(certidao, hoje)
        if chave in contagem:
            contagem[chave] += 1
    return contagem


def montar_digest():
    """(assunto, corpo, resumo) do digest a partir da contagem da carteira."""
    resumo = _contagem_carteira()
    a_vencer, vencidas, pendentes = (
        resumo['a_vencer'], resumo['vencidas'], resumo['pendentes'])
    vazio = (a_vencer == 0 and vencidas == 0 and pendentes == 0)

    if vazio:
        assunto = '[Certidoes] Digest — tudo em dia'
    else:
        assunto = (f'[Certidoes] Digest — {a_vencer} a vencer, '
                   f'{vencidas} vencidas, {pendentes} pendentes')

    linhas = [
        f'Resumo da carteira de certidoes — {datetime.now():%d/%m/%Y %H:%M}',
        '',
        f'A vencer (na janela): {a_vencer}',
        f'Vencidas: {vencidas}',
        f'Pendentes: {pendentes}',
    ]
    if vazio:
        linhas += ['', 'Tudo em dia — nenhuma certidao a vencer, vencida ou pendente.']
    return assunto, '\n'.join(linhas), resumo


def _digest_devido(cfg):
    """True se ja passou o intervalo da cadencia desde o ultimo digest enviado."""
    ultimo = _ultimo_envio('digest')
    if ultimo is None:
        return True
    cadencia = (cfg.notif_cadencia if cfg else 'semanal') or 'semanal'
    dias = _CADENCIA_DIAS.get(cadencia, 7)
    return (date.today() - ultimo.date()).days >= dias


def enviar_digest_se_devido(app):
    """Envia o digest se a cadencia venceu. Retorna True se enviou.

    - Sem SMTP/destinatario: nao envia e loga aviso acionavel (AC P1.3).
    - 0/0/0: envia "tudo em dia", salvo NOTIF_DIGEST_ENVIAR_VAZIO=false (AC P1.5).
    - So registra no NotificacaoLog quando o envio de fato ocorre (retry no proximo
      tick se o SMTP estiver fora)."""
    cfg = _config()
    if not _digest_devido(cfg):
        return False

    destinatarios = _destinatarios(cfg)
    if not email_sender.smtp_configurado(app.config) or not destinatarios:
        log_event('notif_digest_sem_smtp', level='WARNING',
                  tem_smtp=email_sender.smtp_configurado(app.config),
                  destinatarios=len(destinatarios))
        return False

    assunto, corpo, resumo = montar_digest()
    vazio = not any(resumo.values())
    if vazio and not app.config.get('NOTIF_DIGEST_ENVIAR_VAZIO', True):
        log_event('notif_digest_omitido_vazio')
        return False

    enviado = email_sender.enviar(app.config, destinatarios, assunto, corpo)
    if enviado:
        _registrar_envio('digest', 'digest', detalhe=str(resumo))
    return enviado


# --- alertas (falha recorrente + saldo 2captcha) ---------------------------

def _enviar_alerta(app, destinatarios, chave, tipo, assunto, corpo, janela, detalhe):
    """Envia um alerta respeitando o anti-spam. Retorna True se enviou agora."""
    if _deduplicado(chave, janela):
        return False
    if email_sender.enviar(app.config, destinatarios, assunto, corpo):
        _registrar_envio(chave, tipo, detalhe)
        return True
    return False


def enviar_alertas(app):
    """Empurra alertas de falha recorrente (via diagnostics) e de saldo baixo do
    2captcha, com a janela anti-spam. Retorna quantos alertas enviou agora.

    - Falha recorrente: um alerta por (error_type, alvo) ativo; reenvio bloqueado
      dentro da janela (AC P2 falha).
    - Saldo: alerta so quando abaixo do limiar; saldo None (API fora) NAO gera
      falso-baixo; mantem tambem o WARNING no painel de diagnostico (spec 02)."""
    cfg = _config()
    destinatarios = _destinatarios(cfg)
    if not email_sender.smtp_configurado(app.config) or not destinatarios:
        log_event('notif_alertas_sem_smtp', level='WARNING',
                  tem_smtp=email_sender.smtp_configurado(app.config),
                  destinatarios=len(destinatarios))
        return 0

    janela = app.config.get('NOTIF_ALERTA_JANELA_HORAS', 24)
    enviados = 0

    for alerta in diagnostics.alertas_ativos():
        error_type = alerta.get('error_type')
        alvo = alerta.get('alvo')
        chave = f'falha:{error_type}:{alvo}'
        assunto = f'[Certidoes] Alerta: falha recorrente {error_type} em {alvo}'
        corpo = '\n'.join([
            f'Falha recorrente detectada em {alvo}.',
            f'Tipo de erro: {error_type}',
            f'Ocorrencias: {alerta.get("ocorrencias")}',
            f'Hipotese: {alerta.get("hipotese")}',
        ])
        if _enviar_alerta(app, destinatarios, chave, 'alerta_falha', assunto,
                          corpo, janela, detalhe=str(alerta)):
            enviados += 1

    saldo = captcha_solver.consultar_saldo(app.config)
    minimo = app.config.get('CAPTCHA_2_SALDO_MINIMO', 0)
    if saldo is not None and saldo < minimo:
        # o aviso no painel de diagnostico e responsabilidade do agendador
        # (_avisar_saldo_baixo, spec 02); aqui so cuidamos do push por e-mail.
        assunto = '[Certidoes] Alerta: saldo 2captcha baixo'
        corpo = '\n'.join([
            f'Saldo atual do 2captcha: {saldo:.2f} USD',
            f'Limiar minimo configurado: {minimo:.2f} USD',
            'Recarregue para nao interromper os lotes automatizados.',
        ])
        if _enviar_alerta(app, destinatarios, 'saldo_baixo', 'alerta_saldo',
                          assunto, corpo, janela, detalhe=f'saldo={saldo}'):
            enviados += 1

    return enviados
