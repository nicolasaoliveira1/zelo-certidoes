"""Testes da CLI de bootstrap (spec 01 — AUTH-06)."""
import pytest

from app import db
from app.models import Usuario, PapelUsuario
from app.services import usuario_service as svc


@pytest.fixture()
def ctx(app):
    with app.app_context():
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()


def test_criar_admin_em_banco_vazio(app, ctx):
    runner = app.test_cli_runner()
    result = runner.invoke(args=['criar-admin', '--username', 'chefe'],
                           input='Senha-1234\nSenha-1234\n')
    assert result.exit_code == 0
    assert svc.existe_admin() is True
    u = Usuario.query.filter_by(username='chefe').first()
    assert u.papel == PapelUsuario.ADMIN
    assert u.checar_senha('Senha-1234')


def test_criar_admin_recusa_segundo(app, ctx):
    svc.criar_usuario('root', 'Senha-1234', PapelUsuario.ADMIN)
    runner = app.test_cli_runner()
    result = runner.invoke(args=['criar-admin', '--username', 'outro'],
                           input='Senha-9999\nSenha-9999\n')
    assert result.exit_code != 0
    assert Usuario.query.filter_by(papel=PapelUsuario.ADMIN).count() == 1


def test_criar_admin_forcar_cria_segundo(app, ctx):
    svc.criar_usuario('root', 'Senha-1234', PapelUsuario.ADMIN)
    runner = app.test_cli_runner()
    result = runner.invoke(args=['criar-admin', '--username', 'outro', '--forcar'],
                           input='Senha-9999\nSenha-9999\n')
    assert result.exit_code == 0
    assert Usuario.query.filter_by(papel=PapelUsuario.ADMIN).count() == 2


def test_criar_usuario_operador(app, ctx):
    runner = app.test_cli_runner()
    result = runner.invoke(args=['criar-usuario', '--username', 'ana', '--papel', 'operador'],
                           input='Senha-1234\nSenha-1234\n')
    assert result.exit_code == 0
    u = Usuario.query.filter_by(username='ana').first()
    assert u.papel == PapelUsuario.OPERADOR


def test_senha_nunca_e_argumento(app):
    # AUTH-06: senha só interativa. O comando não expõe uma opção --senha.
    from app.cli import criar_admin
    nomes_opcoes = {p.name for p in criar_admin.params}
    assert 'senha' not in nomes_opcoes
