"""Testes dos models de auth/auditoria (spec 01).

Derivados das ACs: AUTH-02 (só hash, verificação por hash), AUTH-03 (papel),
edge "usuário desativado" (is_active) e AUDIT-01 (EventoAuditoria em UTC).
"""
import pytest

from app import db
from app.models import Usuario, PapelUsuario, EventoAuditoria


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


# --- EventoAuditoria (AUDIT-01, AD-006) ---

def test_evento_auditoria_serializa_criado_em_em_utc(ctx):
    ev = EventoAuditoria(acao='login', resultado='ok', usuario_nome='ana', papel='admin')
    db.session.add(ev)
    db.session.commit()
    d = ev.to_dict()
    # AD-006: timestamp técnico marcado como UTC ao serializar
    assert d['criado_em'].endswith('+00:00')
    assert d['acao'] == 'login'
    assert d['resultado'] == 'ok'
    assert d['usuario_nome'] == 'ana'
    assert d['papel'] == 'admin'


def test_evento_auditoria_resultado_default_ok(ctx):
    ev = EventoAuditoria(acao='empresa.criar')
    db.session.add(ev)
    db.session.commit()
    salvo = EventoAuditoria.query.filter_by(acao='empresa.criar').first()
    assert salvo.resultado == 'ok'
