"""Testes dos models de auth/auditoria (spec 01).

Derivados das ACs: AUTH-02 (só hash, verificação por hash), AUTH-03 (papel),
edge "usuário desativado" (is_active) e AUDIT-01 (EventoAuditoria em UTC).
"""
import pytest

from app import db
from app.models import Usuario, PapelUsuario


@pytest.fixture()
def ctx(app):
    """Schema limpo dentro de um app_context ativo durante o teste."""
    with app.app_context():
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()


# --- Usuario (AUTH-02, AUTH-03, edge desativado) ---

def test_set_senha_nao_armazena_texto_claro(ctx):
    u = Usuario(username='ana')
    u.set_senha('segredo-forte-123')
    # AC AUTH-02: persiste apenas o hash, nunca o texto claro
    assert u.senha_hash != 'segredo-forte-123'
    assert 'segredo-forte-123' not in u.senha_hash


def test_checar_senha_verifica_por_hash(ctx):
    u = Usuario(username='ana')
    u.set_senha('segredo-forte-123')
    assert u.checar_senha('segredo-forte-123') is True
    assert u.checar_senha('senha-errada') is False


def test_is_active_reflete_ativo(ctx):
    u = Usuario(username='ana')
    u.set_senha('x')
    u.ativo = True
    assert u.is_active is True
    u.ativo = False
    # edge: usuário desativado não é "active" para o Flask-Login
    assert u.is_active is False


def test_papel_default_leitura(ctx):
    u = Usuario(username='ana')
    u.set_senha('x')
    db.session.add(u)
    db.session.commit()
    salvo = Usuario.query.filter_by(username='ana').first()
    assert salvo.papel == PapelUsuario.LEITURA == 'leitura'


def test_username_unico(ctx):
    from sqlalchemy.exc import IntegrityError
    u1 = Usuario(username='ana'); u1.set_senha('x')
    u2 = Usuario(username='ana'); u2.set_senha('y')
    db.session.add(u1); db.session.commit()
    db.session.add(u2)
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()
