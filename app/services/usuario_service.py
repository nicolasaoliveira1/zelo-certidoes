"""Operações de domínio sobre Usuario (spec 01).

Reutilizado pela CLI de bootstrap (`flask criar-admin`) e pelas rotas admin de
gestão de usuários. Centraliza commit/rollback e as regras de segurança
(autenticação genérica, guarda de último admin)."""
from sqlalchemy.exc import IntegrityError

from app import db
from app.models import Usuario, PapelUsuario


class UltimoAdminError(Exception):
    """Bloqueia operação que deixaria o sistema sem nenhum admin ativo (edge case)."""


def _contar_admins_ativos():
    return Usuario.query.filter_by(papel=PapelUsuario.ADMIN, ativo=True).count()


def existe_admin():
    """Há algum usuário admin (ativo ou não)? Usado pelo bootstrap para recusar 2º admin."""
    return Usuario.query.filter_by(papel=PapelUsuario.ADMIN).count() > 0


def criar_usuario(username, senha, papel=PapelUsuario.LEITURA):
    if papel not in PapelUsuario.TODOS:
        raise ValueError(f'Papel inválido: {papel}')
    usuario = Usuario(username=username, papel=papel)
    usuario.set_senha(senha)
    db.session.add(usuario)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ValueError(f'Username já existe: {username}')
    return usuario


def autenticar(username, senha):
    """Retorna o Usuario se credencial válida e ativo; senão None.

    Não distingue "usuário inexistente" de "senha errada" (mensagem genérica —
    AC AUTH-01.3)."""
    usuario = Usuario.query.filter_by(username=username).first()
    if usuario and usuario.ativo and usuario.checar_senha(senha):
        return usuario
    return None


def garantir_nao_ultimo_admin(usuario, *, para_operacao):
    if (usuario.papel == PapelUsuario.ADMIN and usuario.ativo
            and _contar_admins_ativos() <= 1):
        raise UltimoAdminError(
            f'Não é possível {para_operacao} o último admin ativo.')


def definir_ativo(usuario, ativo):
    if not ativo:
        garantir_nao_ultimo_admin(usuario, para_operacao='desativar')
    usuario.ativo = ativo
    db.session.commit()


def definir_papel(usuario, papel):
    if papel not in PapelUsuario.TODOS:
        raise ValueError(f'Papel inválido: {papel}')
    if papel != PapelUsuario.ADMIN:
        garantir_nao_ultimo_admin(usuario, para_operacao='rebaixar')
    usuario.papel = papel
    db.session.commit()


def resetar_senha(usuario, nova_senha):
    usuario.set_senha(nova_senha)
    db.session.commit()
