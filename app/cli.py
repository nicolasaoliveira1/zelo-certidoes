"""Comandos CLI de bootstrap de usuários (spec 01 — AUTH-06).

A senha é sempre lida interativamente (prompt oculto), nunca por argumento —
para não vazar credencial em histórico de shell ou logs, nem versionar no repo.
"""
import click
from flask.cli import with_appcontext

from app.models import PapelUsuario
from app.services import usuario_service


@click.command('criar-admin')
@click.option('--username', prompt='Username do admin')
@click.option('--forcar', is_flag=True, default=False,
              help='Cria um admin adicional mesmo que já exista um.')
@with_appcontext
def criar_admin(username, forcar):
    if usuario_service.existe_admin() and not forcar:
        click.echo('Já existe um admin. Use --forcar para criar outro.')
        raise SystemExit(1)
    senha = click.prompt('Senha', hide_input=True, confirmation_prompt=True)
    try:
        usuario_service.criar_usuario(username, senha, PapelUsuario.ADMIN)
    except ValueError as e:
        click.echo(f'Erro: {e}')
        raise SystemExit(1)
    click.echo(f'Admin "{username}" criado.')


@click.command('criar-usuario')
@click.option('--username', prompt='Username')
@click.option('--papel', type=click.Choice([PapelUsuario.OPERADOR, PapelUsuario.LEITURA]),
              default=PapelUsuario.LEITURA, help='Papel do usuário (não-admin).')
@with_appcontext
def criar_usuario_cmd(username, papel):
    senha = click.prompt('Senha', hide_input=True, confirmation_prompt=True)
    try:
        usuario_service.criar_usuario(username, senha, papel)
    except ValueError as e:
        click.echo(f'Erro: {e}')
        raise SystemExit(1)
    click.echo(f'Usuário "{username}" ({papel}) criado.')


def register_cli(app):
    app.cli.add_command(criar_admin)
    app.cli.add_command(criar_usuario_cmd)
