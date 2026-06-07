"""Pequenos utilitarios compartilhados entre modulos do app."""
import os

from flask import current_app


def to_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on', 'sim'}


def get_config_value(name, default=None):
    """Le um valor de config preferindo o app context; cai para os.environ
    quando chamado fora de um contexto Flask."""
    try:
        return current_app.config.get(name, default)
    except RuntimeError:
        return os.environ.get(name, default)
