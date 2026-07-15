"""Testes do registro dos fluxos automatizáveis no agendador (spec 02, SCHED-05)."""
from datetime import date, timedelta

import pytest

from app import db, routes
from app.automation.batch_state import FGTS_BATCH_STATE
from app.models import Certidao, Empresa, TipoCertidao
from app.services import agendador, batch_engine


@pytest.fixture()
def fluxos_registrados():
    # T8 pode ter limpado o registry; garante os fluxos reais para este teste
    agendador._fluxos.clear()
    routes._registrar_fluxos_agendador()
    yield agendador.fluxos_registrados()
    agendador._fluxos.clear()


def test_registra_os_tres_fluxos(fluxos_registrados):
    assert set(fluxos_registrados) == {'FGTS', 'Estadual', 'Municipal'}
    for cfg in fluxos_registrados.values():
        assert callable(cfg['calc_ids'])
        assert callable(cfg['rodar_lote'])


def test_rodar_lote_pula_se_lote_manual_em_andamento(app, ids, fluxos_registrados):
    """Edge case: com um lote FGTS manual 'running', o agendador não clobbera o
    estado nem chama o emit — respeita a serialização e roda no próximo ciclo."""
    try:
        FGTS_BATCH_STATE['status'] = 'running'

        def _emit_proibido(cid, drv, eid):
            raise AssertionError('nao deveria emitir com lote manual em andamento')

        with app.app_context():
            fluxos_registrados['FGTS']['rodar_lote'](
                app, [ids['fgts']],
                wrap_emit=lambda real: _emit_proibido, execution_id='x')
        # estado do lote manual preservado (nao foi resetado)
        assert FGTS_BATCH_STATE['status'] == 'running'
    finally:
        batch_engine.reset_batch_state(FGTS_BATCH_STATE)


def test_fgts_calc_ids_traz_a_vencer(app, ids, fluxos_registrados):
    with app.app_context():
        fgts = db.session.get(Certidao, ids['fgts'])
        fgts.data_validade = date.today() + timedelta(days=1)  # dentro da janela
        db.session.commit()
        alvos = fluxos_registrados['FGTS']['calc_ids'](app)
        assert ids['fgts'] in alvos


def test_rs_calc_ids_vazio_sem_solver(app, ids, fluxos_registrados):
    # RS_ALTCHA_AUTOSOLVE_ENABLED desligado no ambiente de teste (conftest)
    with app.app_context():
        rs = db.session.get(Certidao, ids['rs'])
        rs.data_validade = date.today() + timedelta(days=1)
        db.session.commit()
        assert fluxos_registrados['Estadual']['calc_ids'](app) == []


def test_municipal_calc_ids_filtra_cidade_suportada(app, ids, fluxos_registrados):
    with app.app_context():
        # a certidao municipal semeada esta em Tramandai (suportada)
        muni = db.session.get(Certidao, ids['municipal'])
        muni.data_validade = date.today() + timedelta(days=1)
        db.session.commit()
        suportada = fluxos_registrados['Municipal']['calc_ids'](app)
        assert ids['municipal'] in suportada

        # empresa em cidade nao suportada -> excluida
        outra = Empresa(nome='Fora', cnpj='22.222.222/2222-22',
                        estado='SP', cidade='São Paulo')
        db.session.add(outra)
        db.session.commit()
        cert_fora = Certidao(tipo=TipoCertidao.MUNICIPAL, empresa_id=outra.id,
                             data_validade=date.today() + timedelta(days=1))
        db.session.add(cert_fora)
        db.session.commit()
        alvos = fluxos_registrados['Municipal']['calc_ids'](app)
        assert cert_fora.id not in alvos
