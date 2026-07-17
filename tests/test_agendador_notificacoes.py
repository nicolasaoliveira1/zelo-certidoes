"""Enganche das notificacoes nos jobs do agendador (spec 03, NOTIF-01/03/05).

O digest sai no job de snapshot (roda sempre); os alertas saem no job de
renovacao. Falha no envio nunca derruba o tick.
"""
import pytest

from app.models import TarefaEmissao, TipoCertidao
from app.services import agendador, notificacoes


@pytest.fixture()
def fluxos_limpos():
    agendador._fluxos.clear()
    yield
    agendador._fluxos.clear()


def _registrar_stub(tipo_enum, ids_por_tipo):
    def calc_ids(app):
        return list(ids_por_tipo)

    def rodar_lote(app, ids, wrap_emit, execution_id):
        emit = wrap_emit(lambda cid, drv, eid: (True, False, 'ok'))
        for cid in ids:
            emit(cid, None, execution_id)

    agendador.registrar_fluxo(tipo_enum, {
        'tipo': tipo_enum, 'calc_ids': calc_ids, 'rodar_lote': rodar_lote})


# --- digest no job de snapshot ---------------------------------------------

def test_snapshot_job_dispara_digest(app, ids, monkeypatch):
    chamou = []
    monkeypatch.setattr(notificacoes, 'enviar_digest_se_devido',
                        lambda a: chamou.append(a) or True)
    agendador.job_snapshot_diario(app)
    assert chamou == [app]


def test_snapshot_job_nao_quebra_se_digest_falha(app, ids, monkeypatch):
    def explode(a):
        raise RuntimeError('smtp explodiu')
    monkeypatch.setattr(notificacoes, 'enviar_digest_se_devido', explode)
    # nao deve propagar — o snapshot ja rodou antes do envio
    agendador.job_snapshot_diario(app)


# --- alertas no job de renovacao -------------------------------------------

def test_renovacao_job_dispara_alertas(app, ids, fluxos_limpos, monkeypatch):
    _registrar_stub(TipoCertidao.FGTS, [ids['fgts']])
    monkeypatch.setattr(agendador, '_avisar_saldo_baixo', lambda a: None)
    chamou = []
    monkeypatch.setattr(notificacoes, 'enviar_alertas',
                        lambda a: chamou.append(a) or 0)
    agendador.job_renovacao_diaria(app)
    assert chamou == [app]


def test_renovacao_job_nao_quebra_se_alertas_falham(app, ids, fluxos_limpos, monkeypatch):
    _registrar_stub(TipoCertidao.FGTS, [ids['fgts']])
    monkeypatch.setattr(agendador, '_avisar_saldo_baixo', lambda a: None)
    monkeypatch.setattr(notificacoes, 'enviar_alertas',
                        lambda a: (_ for _ in ()).throw(RuntimeError('boom')))
    agendador.job_renovacao_diaria(app)
    # o lote seguiu apesar da falha no envio de alertas
    with app.app_context():
        t = TarefaEmissao.query.filter_by(certidao_id=ids['fgts']).first()
        assert t.status == 'ok'
