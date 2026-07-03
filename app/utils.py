"""Pequenos utilitarios compartilhados entre modulos do app."""
import os
from datetime import datetime

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


def mtime_para_datetime_local(caminho):
    """mtime do arquivo em `caminho` como datetime local (naive), ou None.

    Retorna None quando o caminho e vazio/None, o arquivo nao existe, ou ha
    erro de I/O. Usa a mesma convencao de hora local do carimbo ao vivo
    (`datetime.now`) para o backfill de Certidao.atualizado_em ficar coerente.
    """
    if not caminho:
        return None
    try:
        return datetime.fromtimestamp(os.path.getmtime(caminho))
    except OSError:
        return None
