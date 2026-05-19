from datetime import date, datetime, timedelta
from threading import Thread

from app.models import Certidao, StatusEspecial, get_a_vencer_dias
from app.services.correlation import CorrelationContext


def batch_state_defaults():
    return {
        'status': 'idle',
        'ids': [],
        'index': 0,
        'total': 0,
        'scope': 'default',
        'vencidas': 0,
        'a_vencer': 0,
        'pendentes': 0,
        'falhas': 0,
        'current_id': None,
        'message': None,
        'stop_requested': False,
        'stop_action': None,
        'driver': None,
        'last_completed': None,
        'started_at': None,
        'finished_at': None,
        'success': 0,
        'fgts_marcadas_pendente': 0,
        'positivas': 0,
        'negativas': 0,
        'efeito_negativas': 0,
        'execution_id': None,
        'last_messages': [],
    }


def reset_batch_state(batch_state):
    batch_state.update(batch_state_defaults())


def build_batch_status_payload(batch_state):
    total = batch_state['total']
    index = batch_state['index']
    remaining = max(total - index, 0)
    return {
        'status': batch_state['status'],
        'total': total,
        'index': index,
        'processed': index,
        'remaining': remaining,
        'scope': batch_state.get('scope', 'default'),
        'falhas': batch_state['falhas'],
        'current_id': batch_state['current_id'],
        'vencidas': batch_state['vencidas'],
        'a_vencer': batch_state['a_vencer'],
        'pendentes': batch_state.get('pendentes', 0),
        'message': batch_state['message'],
        'last_messages': list(batch_state.get('last_messages', [])),
        'last_completed': batch_state.get('last_completed'),
        'success': batch_state.get('success', 0),
        'execution_id': batch_state.get('execution_id'),
        'fgts_marcadas_pendente': batch_state.get('fgts_marcadas_pendente', 0),
        'positivas': batch_state.get('positivas', 0),
        'negativas': batch_state.get('negativas', 0),
        'efeito_negativas': batch_state.get('efeito_negativas', 0),
        'started_at': batch_state['started_at'].isoformat() if batch_state.get('started_at') else None,
        'finished_at': batch_state['finished_at'].isoformat() if batch_state.get('finished_at') else None,
    }


def append_batch_message(batch_state, message, level='info', certidao_id=None, max_items=6):
    if not message:
        return

    messages = batch_state.setdefault('last_messages', [])
    messages.append({
        'message': message,
        'level': level,
        'certidao_id': certidao_id,
        'timestamp': datetime.utcnow().isoformat()
    })
    if len(messages) > max_items:
        del messages[:-max_items]

    batch_state['message'] = message


def run_worker(worker_fn, app_factory):
    app = app_factory()
    thread = Thread(target=worker_fn, args=(app,), daemon=True)
    thread.start()


def request_pause(batch_lock, batch_state):
    with batch_lock:
        batch_state['stop_requested'] = True
        batch_state['stop_action'] = 'pause'
        if batch_state['status'] == 'running':
            batch_state['status'] = 'paused'
        return batch_state.get('driver')


def request_stop(batch_lock, batch_state):
    with batch_lock:
        batch_state['stop_requested'] = True
        batch_state['stop_action'] = 'stop'
        batch_state['status'] = 'stopped'
        batch_state['finished_at'] = datetime.utcnow()
        return batch_state.get('driver')


def resume_batch(batch_lock, batch_state, worker_fn, app_factory):
    with batch_lock:
        if batch_state['status'] != 'paused':
            return False

        batch_state['stop_requested'] = False
        batch_state['status'] = 'running'

    run_worker(worker_fn, app_factory)
    return True


def init_batch_run(batch_lock, batch_state, start_id, calc_targets_fn, worker_fn, app_factory):
    with batch_lock:
        if batch_state['status'] in ['running', 'paused']:
            return None

        dados_lote = calc_targets_fn(start_id)
        if not dados_lote['ids']:
            return {}

        reset_batch_state(batch_state)
        batch_state.update({
            'status': 'running',
            'ids': dados_lote['ids'],
            'total': dados_lote['total'],
            'scope': dados_lote.get('scope', 'default'),
            'vencidas': dados_lote['vencidas'],
            'a_vencer': dados_lote['a_vencer'],
            'pendentes': dados_lote.get('pendentes', 0),
            'started_at': datetime.utcnow(),
            'finished_at': None,
            'success': 0,
            'execution_id': CorrelationContext.new_execution_id(),
        })

    run_worker(worker_fn, app_factory)
    return dados_lote


def status_payload_locked(batch_lock, batch_state):
    with batch_lock:
        return build_batch_status_payload(batch_state)


def calc_targets(start_certidao_id, extra_filter=None, scope='default'):
    hoje = date.today()
    limite = hoje + timedelta(days=get_a_vencer_dias())

    query = Certidao.query.order_by(Certidao.id)

    if extra_filter is not None:
        query = extra_filter(query)

    scope_norm = (scope or 'default').strip().lower()
    if scope_norm == 'pendentes':
        query = query.filter(Certidao.status_especial == StatusEspecial.PENDENTE)
    else:
        scope_norm = 'default'
        query = (query
                 .filter(Certidao.data_validade.isnot(None))
                 .filter(Certidao.data_validade <= limite))

    certidoes = query.all()

    ids = [c.id for c in certidoes if c.data_validade]
    if scope_norm == 'pendentes':
        ids = [c.id for c in certidoes]

    vencidas = sum(1 for c in certidoes if c.data_validade and c.data_validade < hoje)
    a_vencer = sum(1 for c in certidoes if c.data_validade and hoje <= c.data_validade <= limite)
    pendentes = sum(1 for c in certidoes if c.status_especial == StatusEspecial.PENDENTE)

    if start_certidao_id in ids:
        ids.remove(start_certidao_id)
        ids.insert(0, start_certidao_id)

    return {
        'ids': ids,
        'total': len(ids),
        'scope': scope_norm,
        'vencidas': vencidas,
        'a_vencer': a_vencer,
        'pendentes': pendentes,
    }
