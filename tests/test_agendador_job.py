"""Testes do job de renovação do agendador (spec 02, SCHED-04/05/06/08).

Usa um fluxo-stub registrado no agendador: `calc_ids` fornece os alvos e
`rodar_lote` chama o `wrap_emit` sobre um emit falso, exercitando as transições
de TarefaEmissao sem Selenium.
"""
import pytest

from app.models import EventoAuditoria, TarefaEmissao, TipoCertidao
from app.services import agendador


@pytest.fixture()
def fluxos_limpos():
    agendador._fluxos.clear()
    yield
    agendador._fluxos.clear()


def _registrar_stub(tipo_enum, ids_por_tipo, *, sucesso=True):
    def calc_ids(app):
        return list(ids_por_tipo)

    def rodar_lote(app, ids, wrap_emit, execution_id):
        emit = wrap_emit(
            lambda cid, drv, eid: (sucesso, False, 'ok' if sucesso else 'portal fora'))
        for cid in ids:
            emit(cid, None, execution_id)

    agendador.registrar_fluxo(tipo_enum, {
        'tipo': tipo_enum, 'calc_ids': calc_ids, 'rodar_lote': rodar_lote})


def test_job_enfileira_e_marca_ok(app, ids, fluxos_limpos):
    _registrar_stub(TipoCertidao.FGTS, [ids['fgts']], sucesso=True)
    agendador.job_renovacao_diaria(app)
    with app.app_context():
        tarefas = TarefaEmissao.query.filter_by(certidao_id=ids['fgts']).all()
        assert len(tarefas) == 1
        assert tarefas[0].status == 'ok'
        assert tarefas[0].iniciada_em is not None
        assert tarefas[0].concluida_em is not None


def test_job_marca_retry_em_falha(app, ids, fluxos_limpos):
    _registrar_stub(TipoCertidao.FGTS, [ids['fgts']], sucesso=False)
    agendador.job_renovacao_diaria(app)
    with app.app_context():
        t = TarefaEmissao.query.filter_by(certidao_id=ids['fgts']).first()
        assert t.status == 'retry'
        assert t.tentativas == 1
        assert t.erro == 'portal fora'


def test_job_sem_alvos_nao_cria_tarefa(app, ids, fluxos_limpos):
    def calc_ids(app):
        return []

    def rodar_lote(app, ids_, wrap_emit, execution_id):
        raise AssertionError('nao deveria rodar sem alvos')

    agendador.registrar_fluxo(TipoCertidao.FGTS, {
        'tipo': TipoCertidao.FGTS, 'calc_ids': calc_ids, 'rodar_lote': rodar_lote})
    agendador.job_renovacao_diaria(app)
    with app.app_context():
        assert TarefaEmissao.query.count() == 0


def test_job_enfileira_idempotente_dentro_do_run(app, ids, fluxos_limpos):
    # calc_ids devolve o mesmo id duplicado -> uma unica tarefa
    _registrar_stub(TipoCertidao.FGTS, [ids['fgts'], ids['fgts']], sucesso=True)
    agendador.job_renovacao_diaria(app)
    with app.app_context():
        assert TarefaEmissao.query.filter_by(certidao_id=ids['fgts']).count() == 1


def test_job_roda_multiplos_tipos(app, ids, fluxos_limpos):
    _registrar_stub(TipoCertidao.FGTS, [ids['fgts']], sucesso=True)
    _registrar_stub(TipoCertidao.ESTADUAL, [ids['rs']], sucesso=True)
    agendador.job_renovacao_diaria(app)
    with app.app_context():
        assert TarefaEmissao.query.filter_by(status='ok').count() == 2


def test_job_audita_com_ator_agendador(app, ids, fluxos_limpos):
    _registrar_stub(TipoCertidao.FGTS, [ids['fgts']], sucesso=True)
    agendador.job_renovacao_diaria(app)
    with app.app_context():
        ev = EventoAuditoria.query.filter_by(acao='agendador.lote').first()
        assert ev is not None
        assert ev.usuario_nome == 'agendador'
        assert ev.papel == 'sistema'
        assert ev.detalhe == 'FGTS'


def test_job_avisa_saldo_2captcha_baixo(app, ids, fluxos_limpos, monkeypatch):
    _registrar_stub(TipoCertidao.FGTS, [ids['fgts']], sucesso=True)
    monkeypatch.setattr(agendador, 'consultar_saldo', lambda cfg: 0.1)
    app.config['CAPTCHA_2_SALDO_MINIMO'] = 2.0
    eventos = []
    monkeypatch.setattr(agendador, 'log_event',
                        lambda evento, **kw: eventos.append(evento))
    agendador.job_renovacao_diaria(app)
    assert 'agendador_saldo_2captcha_baixo' in eventos


def test_job_saldo_ok_nao_avisa(app, ids, fluxos_limpos, monkeypatch):
    _registrar_stub(TipoCertidao.FGTS, [ids['fgts']], sucesso=True)
    monkeypatch.setattr(agendador, 'consultar_saldo', lambda cfg: 50.0)
    app.config['CAPTCHA_2_SALDO_MINIMO'] = 2.0
    eventos = []
    monkeypatch.setattr(agendador, 'log_event',
                        lambda evento, **kw: eventos.append(evento))
    agendador.job_renovacao_diaria(app)
    assert 'agendador_saldo_2captcha_baixo' not in eventos
