"""E2E da gestão de usuários (spec 01 — AUTH-03, AUDIT-01, edge último admin)."""
from app import db
from app.models import Usuario, EventoAuditoria, PapelUsuario


def _id_por_username(app, username):
    with app.app_context():
        return Usuario.query.filter_by(username=username).first().id


def test_operador_barrado_no_painel(login_as, ids):
    assert login_as('operador').get('/admin/usuarios').status_code == 403


def test_admin_cria_usuario(login_as, ids, app):
    c = login_as('admin')
    c.post('/admin/usuarios/criar', data={
        'username': 'novo_op', 'senha': 'Senha-123', 'papel': 'operador'})
    with app.app_context():
        u = Usuario.query.filter_by(username='novo_op').first()
        assert u is not None and u.papel == PapelUsuario.OPERADOR
        # ação auditada
        ev = EventoAuditoria.query.filter_by(acao='usuario.criar', resultado='ok').first()
        assert ev is not None and ev.alvo_id == u.id


def test_admin_troca_papel(login_as, ids, app):
    leitura_id = _id_por_username(app, 'leitura_test')
    c = login_as('admin')
    c.post(f'/admin/usuarios/{leitura_id}/papel', data={'papel': 'operador'})
    with app.app_context():
        assert db.session.get(Usuario, leitura_id).papel == PapelUsuario.OPERADOR


def test_desativar_ultimo_admin_bloqueado(login_as, ids, app):
    admin_id = _id_por_username(app, 'admin_test')  # único admin
    c = login_as('admin')
    c.post(f'/admin/usuarios/{admin_id}/desativar')
    with app.app_context():
        # guarda de último admin: segue ativo
        assert db.session.get(Usuario, admin_id).ativo is True


def test_desativar_usuario_comum_ok(login_as, ids, app):
    op_id = _id_por_username(app, 'op_test')
    c = login_as('admin')
    c.post(f'/admin/usuarios/{op_id}/desativar')
    with app.app_context():
        assert db.session.get(Usuario, op_id).ativo is False
