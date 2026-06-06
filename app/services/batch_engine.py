from datetime import date, datetime, timedelta
from threading import Thread

from app.models import Certidao, StatusEspecial, TipoCertidao, get_a_vencer_dias
from app.services.correlation import CorrelationContext
from app.services.execution_logger import log_event


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


def run_batch_loop(
    app,
    *,
    lock,
    state,
    emit_fn,
    nome_lote,
    curto,
    tag,
    event_prefix,
    create_driver=None,
    eager_driver=False,
    on_setup=None,
    on_teardown=None,
    recover_fn=None,
):
    """Loop generico de lote compartilhado por FGTS, Estadual RS e Municipal.

    Parametros:
      emit_fn(certidao_id, driver, execution_id) -> (sucesso, grave, mensagem)
      nome_lote: rotulo usado nas mensagens de lote (ex.: 'FGTS', 'Estadual RS').
      curto: rotulo curto por item (ex.: 'FGTS', 'RS', 'Municipal').
      tag: prefixo dos prints de console (ex.: 'FGTS-LOTE').
      event_prefix: prefixo dos eventos de log (`<prefix>_start` / `<prefix>_end`).
      create_driver(): cria um WebDriver; chamado uma vez (eager) ou sob demanda.
      eager_driver: cria o driver antes do loop (RS) em vez de no 1o item.
      on_setup(app) -> ctx: hook opcional antes do loop (ex.: politica RS).
      on_teardown(ctx): hook opcional no finally (ex.: desativar politica RS).
      recover_fn(certidao_id, execution_id, driver, sucesso, grave, mensagem)
        -> (driver, sucesso, grave, mensagem): recuperacao opcional pos-emissao
        (ex.: recriar driver do FGTS apos falha de carregamento).
    """
    with app.app_context():
        driver = None
        setup_ctx = None
        print(f"[{tag}] Worker iniciado.")
        execution_id = state.get('execution_id')
        if execution_id:
            CorrelationContext.set_execution_id(execution_id)
        log_event(f'{event_prefix}_start', status='running')

        try:
            if on_setup:
                setup_ctx = on_setup(app)
            if eager_driver and create_driver:
                driver = create_driver()

            while True:
                with lock:
                    if state['stop_requested']:
                        if state.get('stop_action') == 'stop':
                            state['status'] = 'stopped'
                            append_batch_message(
                                state,
                                f'Lote {nome_lote} interrompido por solicitação.',
                                level='warning',
                            )
                        else:
                            state['status'] = 'paused'
                            append_batch_message(
                                state,
                                f'Lote {nome_lote} pausado por solicitação.',
                                level='warning',
                            )
                        break

                    if state['index'] >= state['total']:
                        state['status'] = 'completed'
                        state['current_id'] = None
                        state['finished_at'] = datetime.utcnow()
                        append_batch_message(
                            state,
                            f'Lote {nome_lote} concluído com sucesso.',
                            level='info',
                        )
                        break

                    certidao_id = state['ids'][state['index']]
                    state['current_id'] = certidao_id
                    append_batch_message(
                        state,
                        f"{curto} iniciando ID={certidao_id} "
                        f"({state['index'] + 1}/{state['total']}).",
                        level='info',
                        certidao_id=certidao_id,
                    )

                if driver is None and create_driver:
                    driver = create_driver()

                sucesso, grave, mensagem = emit_fn(certidao_id, driver, execution_id)

                if recover_fn:
                    driver, sucesso, grave, mensagem = recover_fn(
                        certidao_id, execution_id, driver, sucesso, grave, mensagem
                    )

                with lock:
                    if state['stop_requested']:
                        state['status'] = (
                            'stopped' if state.get('stop_action') == 'stop' else 'paused'
                        )
                        break

                    if grave:
                        state['status'] = 'error'
                        state['message'] = mensagem or f'Erro grave no lote {nome_lote}.'
                        append_batch_message(
                            state, state['message'], level='error', certidao_id=certidao_id
                        )
                        break

                    if not sucesso:
                        state['falhas'] += 1
                        append_batch_message(
                            state,
                            f"{curto} falhou ID={certidao_id}: {mensagem}",
                            level='warning',
                            certidao_id=certidao_id,
                        )
                    else:
                        state['success'] += 1
                        append_batch_message(
                            state,
                            f"{curto} OK ID={certidao_id}.",
                            level='info',
                            certidao_id=certidao_id,
                        )

                    state['index'] += 1
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            if on_teardown:
                try:
                    on_teardown(setup_ctx)
                except Exception:
                    pass
            log_event(f'{event_prefix}_end', status=state.get('status'))
            CorrelationContext.clear()
            print(f"[{tag}] Worker encerrado.")


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
    limite = hoje + timedelta(days=max(get_a_vencer_dias(tipo=t) for t in TipoCertidao))

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
