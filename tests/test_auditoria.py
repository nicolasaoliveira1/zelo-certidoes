"""Testes do service de auditoria (spec 01 — AUDIT-01/02)."""
import pytest

from app import db
from app.models import EventoAuditoria, PapelUsuario
from app.services import auditoria
from app.services import usuario_service as svc


@pytest.fixture()
def ctx(app):
    with app.app_context():
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()


def test_registrar_grava_evento(ctx, app):
    with app.test_request_context('/', environ_base={'REMOTE_ADDR': '10.0.0.5'}):
        auditoria.registrar('empresa.criar', alvo_tipo='empresa', alvo_id=7)
    ev = EventoAuditoria.query.filter_by(acao='empresa.criar').first()
    assert ev is not None
    assert ev.alvo_tipo == 'empresa' and ev.alvo_id == 7
    assert ev.resultado == 'ok'
    assert ev.ip == '10.0.0.5'


def test_registrar_captura_usuario_logado(ctx, app):
    from flask_login import login_user
    u = svc.criar_usuario('root', 'senha-1', PapelUsuario.ADMIN)
    with app.test_request_context('/'):
        login_user(u)
        auditoria.registrar('login', resultado='ok')
    ev = EventoAuditoria.query.filter_by(acao='login').first()
    assert ev.usuario_id == u.id
    assert ev.usuario_nome == 'root'
    assert ev.papel == PapelUsuario.ADMIN


def test_registrar_resultado_erro(ctx, app):
    with app.test_request_context('/'):
        auditoria.registrar('certidao.emitir', resultado='erro', detalhe='timeout')
    ev = EventoAuditoria.query.filter_by(acao='certidao.emitir').first()
    assert ev.resultado == 'erro'
    assert ev.detalhe == 'timeout'


def test_registrar_best_effort_nao_propaga(ctx, app, monkeypatch):
    def _boom():
        raise RuntimeError('falha de commit')
    monkeypatch.setattr(db.session, 'commit', _boom)
    with app.test_request_context('/'):
        # não deve levantar mesmo com o commit quebrado
        auditoria.registrar('config.editar', resultado='ok')
    # nada foi persistido, mas o app seguiu vivo
    monkeypatch.undo()
    assert EventoAuditoria.query.filter_by(acao='config.editar').first() is None


def test_consultar_filtra_por_acao(ctx, app):
    with app.test_request_context('/'):
        auditoria.registrar('login')
        auditoria.registrar('logout')
        auditoria.registrar('login')
    logins = auditoria.consultar(acao='login')
    assert len(logins) == 2
    assert all(e.acao == 'login' for e in logins)


def test_consultar_filtra_por_usuario(ctx, app):
    from flask_login import login_user
    ana = svc.criar_usuario('ana', 'senha-1', PapelUsuario.OPERADOR)
    bob = svc.criar_usuario('bob', 'senha-1', PapelUsuario.LEITURA)
    with app.test_request_context('/'):
        login_user(ana)
        auditoria.registrar('certidao.marcar_pendente', alvo_tipo='certidao', alvo_id=3)
    with app.test_request_context('/'):
        login_user(bob)
        auditoria.registrar('login')
    doAna = auditoria.consultar(usuario_id=ana.id)
    assert len(doAna) == 1
    assert doAna[0].acao == 'certidao.marcar_pendente'
