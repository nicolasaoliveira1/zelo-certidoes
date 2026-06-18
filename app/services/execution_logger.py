import json
import logging
import os
import socket
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from app.services.correlation import CorrelationContext


LOGGER_NAME = 'certidoes'
HOSTNAME = socket.gethostname()
PROCESS_ID = os.getpid()

# prefixo do nome do evento -> dominio curto exibido no console humano
_DOMINIOS = (
    ('fgts', 'FGTS'),
    ('rs_', 'RS'),
    ('estadual', 'RS'),
    ('altcha', 'RS'),
    ('municipal', 'MUNI'),
    ('federal', 'FED'),
    ('http', 'HTTP'),
    ('startup', 'BOOT'),
    ('preflight', 'PRE'),
    ('pattern', 'DIAG'),
)

_ICONES = {'ERROR': 'x', 'WARNING': '!', 'INFO': '.'}
_CORES = {'ERROR': '\033[31m', 'WARNING': '\033[33m', 'INFO': '\033[90m'}
_RESET = '\033[0m'

# chaves que viram colunas fixas (ou sao ruido) e nao se repetem no "extra"
_CAMPOS_FIXOS = {'timestamp', 'event', 'level', 'request_id', 'execution_id', 'host', 'pid'}


class HumanFormatter(logging.Formatter):
    """Renderiza o payload estruturado como uma linha legivel para humanos.

    Cai para a mensagem crua (JSON) quando o registro nao traz payload."""

    def __init__(self, usar_cor=True):
        super().__init__()
        self.usar_cor = usar_cor

    def format(self, record):
        p = getattr(record, 'payload', None)
        if not isinstance(p, dict):
            return record.getMessage()

        nivel = str(p.get('level') or 'INFO').upper()
        hora = str(p.get('timestamp') or '')[11:19] or '--:--:--'
        icone = _ICONES.get(nivel, '.')
        dominio = self._dominio(p.get('event', ''))
        evento = str(p.get('event') or '')
        req = p.get('request_id') or ''
        sufixo = f'  (req:{req})' if req else ''
        linha = f'{hora}  {icone} {dominio:<4} {evento:<24} {self._campos(p)}{sufixo}'.rstrip()

        if self.usar_cor and nivel in _CORES:
            return f'{_CORES[nivel]}{linha}{_RESET}'
        return linha

    @staticmethod
    def _dominio(event):
        ev = str(event or '').lower()
        for prefixo, nome in _DOMINIOS:
            if ev.startswith(prefixo):
                return nome
        return '-'

    @staticmethod
    def _campos(p):
        partes = []
        if p.get('error_type'):
            partes.append(str(p['error_type']))
        for chave in ('certidao_id', 'empresa_id'):
            if p.get(chave) not in (None, ''):
                partes.append(f'{chave.split("_")[0]}={p[chave]}')
        if p.get('municipio'):
            partes.append(str(p['municipio']))
        for chave in ('message', 'msg', 'error'):
            val = p.get(chave)
            if val not in (None, ''):
                texto = str(val)
                partes.append(texto if len(texto) <= 80 else texto[:77] + '...')
                break
        return '  '.join(partes)


def _suporta_cor():
    if os.environ.get('NO_COLOR'):
        return False
    return bool(getattr(sys.stderr, 'isatty', lambda: False)())


def configure_logging(level='INFO', log_dir=None, console_format='human', json_file=True):
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger

    logger.setLevel(level)
    logger.propagate = False

    console = logging.StreamHandler()
    if str(console_format).lower() == 'human':
        console.setFormatter(HumanFormatter(usar_cor=_suporta_cor()))
    else:
        console.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(console)

    if json_file:
        destino = log_dir or os.path.join(os.getcwd(), 'logs')
        try:
            os.makedirs(destino, exist_ok=True)
            arquivo = RotatingFileHandler(
                os.path.join(destino, 'app.jsonl'),
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding='utf-8',
            )
            arquivo.setFormatter(logging.Formatter('%(message)s'))
            logger.addHandler(arquivo)
        except OSError:
            pass  # sem arquivo de log nao deve derrubar a aplicacao

    return logger


def log_event(event, level='INFO', **fields):
    logger = logging.getLogger(LOGGER_NAME)
    payload = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'event': event,
        'level': level,
        'request_id': CorrelationContext.get_request_id(),
        'execution_id': CorrelationContext.get_execution_id(),
        'host': HOSTNAME,
        'pid': PROCESS_ID,
    }
    payload.update(fields)

    text = json.dumps(payload, ensure_ascii=False, default=str)
    extra = {'payload': payload}
    lvl = str(level or 'INFO').upper()
    if lvl == 'ERROR':
        logger.error(text, extra=extra)
    elif lvl == 'WARNING':
        logger.warning(text, extra=extra)
    else:
        logger.info(text, extra=extra)
