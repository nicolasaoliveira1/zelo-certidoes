"""Pequenos utilitarios compartilhados entre modulos do app."""
import os
from datetime import datetime, timezone

from flask import current_app, jsonify


def utcnow_naive():
    """Datetime atual em UTC sem tzinfo (naive).

    Substitui datetime.utcnow() (deprecado a partir do Python 3.12)
    preservando a convencao de UTC-naive usada nas colunas e comparacoes
    de tempo do projeto.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


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


def normalizar_cidade(valor):
    """Chave canonica de cidade: trim -> remover acentos -> maiuscula; '' se vazio.

    Fonte unica compartilhada pelo filtro do dashboard (contagem/recorte por
    cidade no cliente) e pela exportacao da carteira (recorte replicado no
    servidor), garantindo que variacoes como 'Imbe'/'IMBE' caiam na mesma chave.
    Import de `remover_acentos` e lazy para manter `utils` como modulo base sem
    acoplar seu tempo de import ao de `file_manager`.
    """
    texto = (valor or '').strip()
    if not texto:
        return ''
    from app.file_manager import remover_acentos
    return remover_acentos(texto).upper()


def json_error(message=None, code=400, exc=None, **extra):
    """Envelope JSON de erro padrao das rotas assincronas.

    Fonte unica compartilhada pelas rotas (blueprint `main`) e pela camada de
    servico (ex.: `emissao_service`). Imports de `app.errors`/`correlation` sao
    lazy para manter `utils` como modulo base sem acoplar seu tempo de import.
    """
    from app.errors import descrever_erro, mensagem_usuario
    from app.services.correlation import CorrelationContext

    info = descrever_erro(exc) if exc is not None else None
    texto = message or (mensagem_usuario(exc) if exc is not None else 'Erro inesperado.')
    payload = {
        'status': 'error',
        'message': texto,
        'mensagem': texto,
        'codigo': code,
        'request_id': CorrelationContext.get_request_id(),
    }
    if info is not None:
        payload.setdefault('error_type', info.tipo.value)
        payload.setdefault('acao', info.acao)
    payload.update(extra)
    return jsonify(payload), code


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
