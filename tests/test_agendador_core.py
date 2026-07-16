"""Testes do agendador core (spec 02, SCHED-02/04/07): lifecycle, reconciliação,
reprogramação e snapshot job."""
from datetime import date, datetime

import pytest

from app import db
from app.models import Certidao, ConfiguracaoSistema, SnapshotCertidao, TarefaEmissao
from app.services import agendador, snapshot_service


@pytest.fixture()
def sched_limpo(app, monkeypatch):
    # simula o processo que serve (filho do reloader): sem isto, o guard anti
    # duplo-start do reloader barra o init quando app.debug=True (.env FLASK_DEBUG=1)
    monkeypatch.setenv('WERKZEUG_RUN_MAIN', 'true')
    # religa o agendador (conftest desliga por padrao nos testes)
    monkeypatch.setitem(app.config, 'AGENDADOR_ENABLED', True)
    agendador._fluxos.clear()
    yield
    agendador.shutdown()
    agendador._fluxos.clear()


def _config(app, **kwargs):
    with app.app_context():
        cfg = ConfiguracaoSistema(**kwargs)
        db.session.add(cfg)
        db.session.commit()


def test_init_inicia_e_agenda_snapshot(app, ids, sched_limpo):
    sched = agendador.init(app)
    assert sched is not None
    assert sched.running
    assert sched.get_job(agendador._JOB_SNAPSHOT) is not None


def test_init_idempotente_nao_duplica(app, ids, sched_limpo):
    s1 = agendador.init(app)
    s2 = agendador.init(app)
    assert s1 is s2
    # 2 jobs: snapshot + renovacao (ativo por padrao), sem duplicar
    assert len(s1.get_jobs()) == 2


def test_init_reconcilia_orfas(app, ids, sched_limpo):
    with app.app_context():
        cert = db.session.get(Certidao, ids['fgts'])
        t = TarefaEmissao(tipo='FGTS', empresa_id=cert.empresa_id,
                          certidao_id=cert.id, status='rodando',
                          iniciada_em=datetime.now())
        db.session.add(t)
        db.session.commit()
        tid = t.id

    agendador.init(app)

    with app.app_context():
        assert db.session.get(TarefaEmissao, tid).status == 'pendente'


def test_reprogramar_muda_hora_sem_recriar(app, ids, sched_limpo):
    agendador.init(app)  # sem linha de config -> hora default 3
    job = agendador._scheduler.get_job(agendador._JOB_RENOVACAO)
    assert "hour='3'" in str(job.trigger)

    _config(app, agendador_hora=9, agendador_ativo=True)
    agendador.reprogramar(app)
    job = agendador._scheduler.get_job(agendador._JOB_RENOVACAO)
    assert "hour='9'" in str(job.trigger)


def test_ativo_false_nao_agenda_renovacao(app, ids, sched_limpo):
    _config(app, agendador_hora=3, agendador_ativo=False)
    agendador.init(app)
    # renovacao desligada, snapshot (sem custo) segue agendado
    assert agendador._scheduler.get_job(agendador._JOB_RENOVACAO) is None
    assert agendador._scheduler.get_job(agendador._JOB_SNAPSHOT) is not None


def test_reprogramar_ativo_false_remove_renovacao(app, ids, sched_limpo):
    _config(app, agendador_hora=3, agendador_ativo=True)
    agendador.init(app)
    assert agendador._scheduler.get_job(agendador._JOB_RENOVACAO) is not None

    with app.app_context():
        cfg = db.session.get(ConfiguracaoSistema, 1)
        cfg.agendador_ativo = False
        db.session.commit()
    agendador.reprogramar(app)
    assert agendador._scheduler.get_job(agendador._JOB_RENOVACAO) is None


def test_job_snapshot_diario_gera_snapshot(app, ids, sched_limpo):
    snapshot_service._ULTIMO_SNAPSHOT_DIA = None
    agendador.job_snapshot_diario(app)
    with app.app_context():
        assert SnapshotCertidao.query.filter_by(data=date.today()).count() > 0
    snapshot_service._ULTIMO_SNAPSHOT_DIA = None
