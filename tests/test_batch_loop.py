"""Testes do loop generico de lote (batch_engine.run_batch_loop).

Exercita a logica de controle (pausa/parada/conclusao/erro/recuperacao)
com um emit_fn falso, sem depender de Selenium nem de banco.
Rodar: SECRET_KEY=x python tests/test_batch_loop.py
"""
import os
import sys

os.environ.setdefault('SECRET_KEY', 'test')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.batch_engine import run_batch_loop, batch_state_defaults


class FakeCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeApp:
    def app_context(self):
        return FakeCtx()


class FakeLock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeDriver:
    def __init__(self, name='drv'):
        self.name = name
        self.quit_called = False

    def quit(self):
        self.quit_called = True


def make_state(ids):
    state = batch_state_defaults()
    state['status'] = 'running'
    state['ids'] = list(ids)
    state['total'] = len(ids)
    return state


def make_emit(results, on_call=None):
    seq = list(results)
    calls = {'n': 0}

    def emit(cid, driver, eid):
        i = calls['n']
        calls['n'] += 1
        if on_call:
            on_call(cid, driver, eid, i)
        return seq[i]

    emit.calls = calls
    return emit


COMMON = dict(nome_lote='Teste', curto='T', tag='TESTE-LOTE', event_prefix='teste_batch_worker')


def run(state, emit, **kw):
    drivers = []

    def create_driver():
        d = FakeDriver(f'd{len(drivers)}')
        drivers.append(d)
        return d

    params = dict(COMMON)
    params.update(kw)
    params.setdefault('create_driver', create_driver)
    run_batch_loop(FakeApp(), lock=FakeLock(), state=state, emit_fn=emit, **params)
    return drivers


def test_all_success():
    state = make_state([1, 2, 3])
    emit = make_emit([(True, False, None)] * 3)
    drivers = run(state, emit)
    assert state['status'] == 'completed', state['status']
    assert state['success'] == 3, state['success']
    assert state['falhas'] == 0
    assert state['index'] == 3
    assert emit.calls['n'] == 3
    assert drivers and drivers[0].quit_called
    print('ok test_all_success')


def test_one_failure():
    state = make_state([1, 2, 3])
    emit = make_emit([(True, False, None), (False, False, 'x'), (True, False, None)])
    run(state, emit)
    assert state['status'] == 'completed'
    assert state['success'] == 2
    assert state['falhas'] == 1
    assert state['index'] == 3
    print('ok test_one_failure')


def test_pendente_result_nao_e_sucesso_nem_falha():
    # Um item que termina PENDENTE sinaliza incrementando pendentes_resultado.
    # O loop deve contar como pendente, sem tocar success nem falhas.
    state = make_state([1, 2, 3])

    def on_call(cid, driver, eid, i):
        if i == 1:  # segundo item termina pendente
            state['pendentes_resultado'] = state.get('pendentes_resultado', 0) + 1

    # item 1 sucesso, item 2 pendente (retorno True mas com incremento), item 3 sucesso
    emit = make_emit([(True, False, None), (True, False, 'positiva'), (True, False, None)], on_call=on_call)
    run(state, emit)
    assert state['status'] == 'completed', state['status']
    assert state['success'] == 2, state['success']
    assert state['pendentes_resultado'] == 1, state['pendentes_resultado']
    assert state['falhas'] == 0, state['falhas']
    assert state['index'] == 3
    print('ok test_pendente_result_nao_e_sucesso_nem_falha')


def test_pendente_com_retorno_false_nao_conta_falha():
    # FGTS impedimento retorna sucesso=False mas incrementa pendentes_resultado:
    # deve contar como pendente, nao como falha.
    state = make_state([1])

    def on_call(cid, driver, eid, i):
        state['pendentes_resultado'] = state.get('pendentes_resultado', 0) + 1

    emit = make_emit([(False, False, 'impedimento')], on_call=on_call)
    run(state, emit)
    assert state['status'] == 'completed', state['status']
    assert state['success'] == 0
    assert state['falhas'] == 0
    assert state['pendentes_resultado'] == 1
    assert state['index'] == 1
    print('ok test_pendente_com_retorno_false_nao_conta_falha')


def test_grave_error_stops():
    state = make_state([1, 2, 3])
    emit = make_emit([(True, False, None), (False, True, 'boom')])
    run(state, emit)
    assert state['status'] == 'error', state['status']
    assert state['message'] == 'boom', state['message']
    assert state['success'] == 1
    assert state['index'] == 1  # nao avancou no item grave
    assert emit.calls['n'] == 2
    print('ok test_grave_error_stops')


def test_stop_before_processing():
    state = make_state([1, 2, 3])
    state['stop_requested'] = True
    state['stop_action'] = 'stop'
    emit = make_emit([(True, False, None)] * 3)
    run(state, emit)
    assert state['status'] == 'stopped', state['status']
    assert emit.calls['n'] == 0  # nunca emitiu
    assert state['index'] == 0
    print('ok test_stop_before_processing')


def test_pause_before_processing():
    state = make_state([1, 2, 3])
    state['stop_requested'] = True
    state['stop_action'] = 'pause'
    emit = make_emit([(True, False, None)] * 3)
    run(state, emit)
    assert state['status'] == 'paused', state['status']
    assert emit.calls['n'] == 0
    print('ok test_pause_before_processing')


def test_stop_during_emit():
    state = make_state([1, 2])

    def on_call(cid, driver, eid, i):
        if i == 0:
            state['stop_requested'] = True
            state['stop_action'] = 'stop'

    emit = make_emit([(True, False, None), (True, False, None)], on_call=on_call)
    run(state, emit)
    assert state['status'] == 'stopped', state['status']
    assert state['success'] == 0   # nao contabiliza apos parada
    assert state['index'] == 0     # nao avanca
    assert emit.calls['n'] == 1
    print('ok test_stop_during_emit')


def test_recover_fn():
    state = make_state([1])
    emit = make_emit([(False, True, 'RELOAD')])
    invoked = {'n': 0}

    def recover(cid, eid, driver, sucesso, grave, mensagem):
        invoked['n'] += 1
        if grave and mensagem == 'RELOAD':
            return FakeDriver('recriado'), True, False, None
        return driver, sucesso, grave, mensagem

    run(state, emit, recover_fn=recover)
    assert invoked['n'] == 1
    assert state['status'] == 'completed', state['status']
    assert state['success'] == 1
    print('ok test_recover_fn')


def test_eager_driver_created_once():
    state = make_state([1, 2])
    emit = make_emit([(True, False, None)] * 2)
    drivers = run(state, emit, eager_driver=True)
    assert len(drivers) == 1  # criado uma vez antes do loop
    assert state['status'] == 'completed'
    print('ok test_eager_driver_created_once')


def test_setup_teardown_called():
    state = make_state([1])
    emit = make_emit([(True, False, None)])
    events = []
    run(
        state, emit,
        on_setup=lambda app: events.append('setup') or 'CTX',
        on_teardown=lambda ctx: events.append(f'teardown:{ctx}'),
    )
    assert events == ['setup', 'teardown:CTX'], events
    print('ok test_setup_teardown_called')


def main():
    tests = [
        test_all_success,
        test_one_failure,
        test_pendente_result_nao_e_sucesso_nem_falha,
        test_pendente_com_retorno_false_nao_conta_falha,
        test_grave_error_stops,
        test_stop_before_processing,
        test_pause_before_processing,
        test_stop_during_emit,
        test_recover_fn,
        test_eager_driver_created_once,
        test_setup_teardown_called,
    ]
    for t in tests:
        t()
    print(f'\n{len(tests)} testes passaram.')


if __name__ == '__main__':
    main()
