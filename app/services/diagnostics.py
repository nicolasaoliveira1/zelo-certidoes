"""Diagnostico em memoria: buffer de eventos recentes + deteccao de padroes
recorrentes (mesmo erro N vezes no mesmo alvo => provavel quebra de seletor
ou portal fora do ar).

Alimentado por um logging.Handler que le o payload ja anexado por log_event,
evitando acoplar o execution_logger a este modulo."""
import logging
import queue
import threading
from collections import deque
from datetime import datetime, timedelta, timezone

_MAX_EVENTOS = 200
_LIMIAR_RECORRENCIA = 3  # falhas iguais seguidas para abrir um alerta
_NIVEIS_PERSISTIDOS = {'WARNING', 'ERROR'}

_lock = threading.Lock()
_eventos = deque(maxlen=_MAX_EVENTOS)
_recorrencia = {}  # (error_type, alvo) -> contagem
_alertas = {}      # (error_type, alvo) -> alerta ativo

# Persistencia desacoplada: a fila e drenada por uma unica thread escritora,
# com sessao/transacao propria, para nao colidir com a transacao do chamador
# (workers de lote fazem rollback no meio) nem travar o SQLite.
_fila = queue.Queue()
_app = None
_writer_iniciado = False

_PREFIXOS = (
    ('fgts', 'FGTS'), ('rs_', 'RS'), ('estadual', 'RS'), ('altcha', 'RS'),
    ('municipal', 'MUNI'), ('federal', 'FED'), ('http', 'HTTP'),
)


def _alvo(payload):
    if payload.get('municipio'):
        return str(payload['municipio'])
    ev = str(payload.get('event') or '').lower()
    for prefixo, nome in _PREFIXOS:
        if ev.startswith(prefixo):
            return nome
    return ev or '-'


def _hipotese(error_type):
    if error_type in ('SELECTOR', 'PORTAL', 'TIMEOUT'):
        return 'Portal pode ter mudado ou caido — revise o mapeamento/seletores.'
    if error_type == 'NETWORK_PATH':
        return 'Acesso a rede falhando em sequencia — verifique o drive de rede (Z:).'
    if error_type == 'CAPTCHA':
        return 'Captcha falhando repetidamente — verifique a chave/saldo do 2captcha.'
    return 'Falhas repetidas do mesmo tipo — investigue pelo req_id no log.'


def _mensagem(payload):
    for chave in ('message', 'msg', 'error'):
        if payload.get(chave):
            return str(payload[chave])[:500]
    return None


def registrar(payload):
    """Registra um evento estruturado (o mesmo dict montado por log_event)."""
    nivel = str(payload.get('level') or 'INFO').upper()
    alvo = _alvo(payload)
    with _lock:
        _eventos.append(payload)
        if nivel == 'ERROR' and payload.get('error_type'):
            chave = (payload['error_type'], alvo)
            n = _recorrencia.get(chave, 0) + 1
            _recorrencia[chave] = n
            if n >= _LIMIAR_RECORRENCIA:
                _alertas[chave] = {
                    'error_type': payload['error_type'],
                    'alvo': alvo,
                    'ocorrencias': n,
                    'ultimo': payload.get('timestamp'),
                    'request_id': payload.get('request_id'),
                    'hipotese': _hipotese(payload['error_type']),
                }
        else:
            # qualquer atividade nao-erro no mesmo alvo zera contadores e alerta
            for chave in [k for k in _recorrencia if k[1] == alvo]:
                _recorrencia.pop(chave, None)
                _alertas.pop(chave, None)

    if _writer_iniciado and nivel in _NIVEIS_PERSISTIDOS:
        _fila.put((payload, alvo))


def eventos_recentes(limite=50, nivel=None):
    """Eventos mais recentes primeiro, opcionalmente filtrados por nivel."""
    with _lock:
        itens = list(_eventos)
    if nivel:
        alvo_nivel = nivel.upper()
        itens = [e for e in itens if str(e.get('level') or 'INFO').upper() == alvo_nivel]
    return list(reversed(itens[-limite:]))


def alertas_ativos():
    with _lock:
        return list(_alertas.values())


def limpar():
    with _lock:
        _eventos.clear()
        _recorrencia.clear()
        _alertas.clear()


class DiagnosticsHandler(logging.Handler):
    """Observa o logger estruturado e alimenta o diagnostico em memoria."""

    def emit(self, record):
        payload = getattr(record, 'payload', None)
        if isinstance(payload, dict):
            registrar(payload)


def attach_handler(logger_name='certidoes'):
    logger = logging.getLogger(logger_name)
    if not any(isinstance(h, DiagnosticsHandler) for h in logger.handlers):
        logger.addHandler(DiagnosticsHandler())


def gravar_evento(payload, alvo=None):
    """Persiste um evento na tabela de historico. Exige app context ativo.
    Usa transacao propria e nunca propaga erro (diagnostico nao pode derrubar
    o fluxo principal)."""
    from app import db
    from app.models import EventoDiagnostico

    try:
        evento = EventoDiagnostico(
            evento=str(payload.get('event') or '')[:80],
            nivel=str(payload.get('level') or 'ERROR').upper()[:10],
            error_type=(payload.get('error_type') or None),
            alvo=(alvo or _alvo(payload))[:80],
            mensagem=_mensagem(payload),
            request_id=payload.get('request_id'),
            execution_id=payload.get('execution_id'),
            certidao_id=payload.get('certidao_id'),
            empresa_id=payload.get('empresa_id'),
        )
        db.session.add(evento)
        db.session.commit()
    except Exception:
        db.session.rollback()


def _drenar():
    while True:
        payload, alvo = _fila.get()
        try:
            with _app.app_context():
                gravar_evento(payload, alvo)
        except Exception:
            pass
        finally:
            _fila.task_done()


def prune(retencao_dias=30):
    """Remove eventos mais antigos que a janela de retencao."""
    from app import db
    from app.models import EventoDiagnostico

    corte = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=retencao_dias)
    try:
        EventoDiagnostico.query.filter(EventoDiagnostico.criado_em < corte).delete()
        db.session.commit()
    except Exception:
        db.session.rollback()


def iniciar_persistencia(app, retencao_dias=30):
    """Liga a persistencia em banco: faz prune inicial e sobe a thread escritora."""
    global _app, _writer_iniciado
    if _writer_iniciado:
        return
    _app = app
    with app.app_context():
        prune(retencao_dias)
    thread = threading.Thread(target=_drenar, name='diagnostics-writer', daemon=True)
    thread.start()
    _writer_iniciado = True


def historico(limite=100):
    """Eventos persistidos mais recentes primeiro (para o painel)."""
    from app.models import EventoDiagnostico

    rows = (EventoDiagnostico.query
            .order_by(EventoDiagnostico.criado_em.desc(), EventoDiagnostico.id.desc())
            .limit(limite).all())
    return [r.to_dict() for r in rows]


def _payload_para_painel(p):
    return {
        'criado_em': p.get('timestamp'),
        'evento': p.get('event'),
        'nivel': str(p.get('level') or 'INFO').upper(),
        'error_type': p.get('error_type'),
        'alvo': _alvo(p),
        'mensagem': _mensagem(p),
        'request_id': p.get('request_id'),
        'execution_id': p.get('execution_id'),
        'certidao_id': p.get('certidao_id'),
        'empresa_id': p.get('empresa_id'),
    }


def eventos_para_painel(limite=100):
    """Fonte unica do painel: historico persistido; se a persistencia estiver
    desligada/indisponivel, cai para os erros/avisos do buffer em memoria."""
    try:
        persistidos = historico(limite=limite)
    except Exception:
        persistidos = []
    if persistidos:
        return persistidos
    memoria = [e for e in eventos_recentes(limite=limite)
               if str(e.get('level') or 'INFO').upper() in _NIVEIS_PERSISTIDOS]
    return [_payload_para_painel(e) for e in memoria]
