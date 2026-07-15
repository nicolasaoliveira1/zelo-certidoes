"""Trilha de auditoria de ações sensíveis (spec 01 — AUDIT-01/02).

`registrar` grava um EventoAuditoria de forma síncrona no thread do request,
em transação própria e **best-effort**: uma falha ao auditar nunca derruba a
ação (só loga). Deve ser chamado depois que a ação resolveu (commit ou rollback).
"""
from flask import has_request_context, request
from flask_login import current_user

from app import db
from app.models import EventoAuditoria
from app.services.correlation import CorrelationContext
from app.services.execution_logger import log_event


def registrar(acao, *, alvo_tipo=None, alvo_id=None, resultado='ok', detalhe=None):
    try:
        usuario_id = usuario_nome = papel = ip = None
        if has_request_context():
            ip = request.remote_addr
            if getattr(current_user, 'is_authenticated', False):
                usuario_id = current_user.id
                usuario_nome = current_user.username
                papel = current_user.papel
        evento = EventoAuditoria(
            acao=acao,
            alvo_tipo=alvo_tipo,
            alvo_id=alvo_id,
            resultado=resultado,
            detalhe=(detalhe[:500] if detalhe else None),
            usuario_id=usuario_id,
            usuario_nome=usuario_nome,
            papel=papel,
            ip=ip,
            request_id=CorrelationContext.get_request_id(),
        )
        db.session.add(evento)
        db.session.commit()
    except Exception as e:
        try:
            db.session.rollback()
        except Exception:
            pass
        log_event('auditoria_registro_falhou', level='ERROR', acao=acao, error=str(e))


def consultar(*, usuario_id=None, acao=None, inicio=None, fim=None, limite=200):
    q = EventoAuditoria.query
    if usuario_id is not None:
        q = q.filter(EventoAuditoria.usuario_id == usuario_id)
    if acao:
        q = q.filter(EventoAuditoria.acao == acao)
    if inicio is not None:
        q = q.filter(EventoAuditoria.criado_em >= inicio)
    if fim is not None:
        q = q.filter(EventoAuditoria.criado_em <= fim)
    return q.order_by(EventoAuditoria.criado_em.desc()).limit(limite).all()
