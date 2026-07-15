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


def test_registrar_ator_sintetico_fora_de_request(ctx, app):
    """Spec 02: o job do agendador audita com ator sintético, sem current_user."""
    auditoria.registrar('agendador.lote', alvo_tipo='certidao', alvo_id=5,
                        ator='agendador')
    ev = EventoAuditoria.query.filter_by(acao='agendador.lote').first()
    assert ev is not None
    assert ev.usuario_nome == 'agendador'
    assert ev.papel == 'sistema'
    assert ev.usuario_id is None
    assert ev.alvo_id == 5


def test_registrar_ator_nao_sobrepoe_usuario_logado(ctx, app):
    from flask_login import login_user
    u = svc.criar_usuario('carla', 'senha-1', PapelUsuario.OPERADOR)
    with app.test_request_context('/'):
        login_user(u)
        auditoria.registrar('agendador.lote', ator='agendador')
    ev = EventoAuditoria.query.filter_by(acao='agendador.lote').first()
    assert ev.usuario_nome == 'carla'  # ator ignorado quando há usuário logado
    assert ev.papel == PapelUsuario.OPERADOR


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


# --- e2e: instrumentação nos pontos sensíveis (AUDIT-01) ---

def test_marcar_pendente_gera_evento(login_as, ids, app):
    """Independent Test da spec: operador marca pendente -> evento com usuário/ação/alvo."""
    c = login_as('operador')
    resp = c.post(f'/certidao/marcar_pendente_json/{ids["fgts"]}')
    assert resp.status_code == 200
    with app.app_context():
        evs = EventoAuditoria.query.filter_by(acao='certidao.marcar_pendente').all()
        assert len(evs) == 1  # exatamente um evento
        ev = evs[0]
        assert ev.usuario_nome == 'op_test'
        assert ev.papel == PapelUsuario.OPERADOR
        assert ev.alvo_tipo == 'certidao' and ev.alvo_id == ids['fgts']
        assert ev.resultado == 'ok'


def test_login_ok_gera_evento(login_as, app):
    login_as('operador')
    with app.app_context():
        ev = EventoAuditoria.query.filter_by(acao='login', resultado='ok').first()
        assert ev is not None
        assert ev.usuario_nome == 'op_test'


def test_login_invalido_gera_evento_erro(client_anon, ids, app):
    client_anon.post('/login', data={'username': 'op_test', 'senha': 'errada'})
    with app.app_context():
        ev = EventoAuditoria.query.filter_by(acao='login', resultado='erro').first()
        assert ev is not None  # tentativa falha também é auditada (AUDIT-01.2)


def test_acao_que_falha_registra_erro(login_as, ids, app, monkeypatch):
    from app.services import certidao_service
    monkeypatch.setattr(certidao_service, 'marcar_pendente', lambda cert: (False, 'boom'))
    c = login_as('operador')
    resp = c.post(f'/certidao/marcar_pendente_json/{ids["fgts"]}')
    assert resp.status_code == 500
    with app.app_context():
        ev = EventoAuditoria.query.filter_by(
            acao='certidao.marcar_pendente', resultado='erro').first()
        assert ev is not None
        assert ev.alvo_id == ids['fgts']


def test_empresa_criar_gera_evento(login_as, ids, app):
    c = login_as('operador')
    c.post('/empresa/adicionar', data={
        'nome': 'Beta LTDA', 'cnpj': '33.333.333/3333-33',
        'cidade': 'Tramandai', 'estado': 'RS',
    })
    with app.app_context():
        ev = EventoAuditoria.query.filter_by(acao='empresa.criar', resultado='ok').first()
        assert ev is not None
        assert ev.usuario_nome == 'op_test' and ev.alvo_tipo == 'empresa'
