"""Testes do usuario_service (spec 01).

Derivados de: AUTH-01.3 (auth genérica), AUTH-03/06 (papéis, bootstrap),
edge "último admin" (spec Edge Cases)."""
import pytest

from app import db
from app.models import PapelUsuario
from app.services import usuario_service as svc
from app.services.usuario_service import UltimoAdminError


@pytest.fixture()
def ctx(app):
    with app.app_context():
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()


def _admin(username='root'):
    return svc.criar_usuario(username, 'senha-forte-1', PapelUsuario.ADMIN)


# --- criar_usuario ---

def test_criar_usuario_default_leitura_e_hash(ctx):
    u = svc.criar_usuario('ana', 'segredo123')
    assert u.papel == PapelUsuario.LEITURA
    assert u.checar_senha('segredo123') is True


def test_criar_usuario_papel_invalido_levanta(ctx):
    with pytest.raises(ValueError):
        svc.criar_usuario('ana', 'x', 'chefe')


def test_criar_usuario_duplicado_levanta(ctx):
    svc.criar_usuario('ana', 'x')
    with pytest.raises(ValueError):
        svc.criar_usuario('ana', 'y')


# --- autenticar (AUTH-01.3: genérico) ---

def test_autenticar_valido_retorna_usuario(ctx):
    svc.criar_usuario('ana', 'segredo123', PapelUsuario.OPERADOR)
    u = svc.autenticar('ana', 'segredo123')
    assert u is not None and u.username == 'ana'


def test_autenticar_senha_errada_retorna_none(ctx):
    svc.criar_usuario('ana', 'segredo123')
    assert svc.autenticar('ana', 'errada') is None


def test_autenticar_inexistente_retorna_none(ctx):
    # mesmo retorno (None) que senha errada — não revela existência
    assert svc.autenticar('fantasma', 'x') is None


def test_autenticar_inativo_retorna_none(ctx):
    u = svc.criar_usuario('ana', 'segredo123')
    u.ativo = False
    db.session.commit()
    assert svc.autenticar('ana', 'segredo123') is None


# --- existe_admin / bootstrap ---

def test_existe_admin(ctx):
    assert svc.existe_admin() is False
    _admin()
    assert svc.existe_admin() is True


# --- guarda de último admin (edge case) ---

def test_desativar_ultimo_admin_bloqueia(ctx):
    a = _admin()
    with pytest.raises(UltimoAdminError):
        svc.definir_ativo(a, False)
    assert a.ativo is True  # estado inalterado


def test_rebaixar_ultimo_admin_bloqueia(ctx):
    a = _admin()
    with pytest.raises(UltimoAdminError):
        svc.definir_papel(a, PapelUsuario.LEITURA)
    assert a.papel == PapelUsuario.ADMIN


def test_desativar_admin_com_outro_admin_ok(ctx):
    _admin('root')
    a2 = _admin('root2')
    svc.definir_ativo(a2, False)  # o outro admin continua ativo -> permitido
    assert a2.ativo is False


def test_resetar_senha(ctx):
    u = svc.criar_usuario('ana', 'antiga123')
    svc.resetar_senha(u, 'nova-senha-9')
    assert svc.autenticar('ana', 'nova-senha-9') is not None
    assert svc.autenticar('ana', 'antiga123') is None
