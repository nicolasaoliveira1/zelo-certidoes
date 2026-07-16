"""Testes do wiring do agendador no boot (spec 02, SCHED-02/04)."""
import pytest

from app.services import agendador


@pytest.fixture(autouse=True)
def _limpa_scheduler():
    yield
    agendador.shutdown()


def test_create_app_chama_agendador_init(monkeypatch):
    chamado = {}
    monkeypatch.setattr(agendador, 'init', lambda app: chamado.setdefault('sim', True))
    from app import create_app
    create_app()
    assert chamado.get('sim') is True


def test_init_nao_inicia_quando_flag_desligada(app, monkeypatch):
    monkeypatch.setitem(app.config, 'AGENDADOR_ENABLED', False)
    monkeypatch.setenv('WERKZEUG_RUN_MAIN', 'true')
    agendador.shutdown()
    assert agendador.init(app) is None
    assert agendador._scheduler is None


def test_init_reloader_pai_nao_inicia(app, monkeypatch):
    # reloader ATIVO (debug on) + WERKZEUG_RUN_MAIN ausente = processo pai do
    # reloader: nao deve agendar (o filho o fara). Forca DEBUG no config para nao
    # depender do ambiente (o .env local tem FLASK_DEBUG=1; o CI nao tem .env).
    monkeypatch.delenv('WERKZEUG_RUN_MAIN', raising=False)
    monkeypatch.setitem(app.config, 'DEBUG', True)
    monkeypatch.setitem(app.config, 'AGENDADOR_ENABLED', True)
    agendador.shutdown()
    assert app.debug is True
    assert agendador.init(app) is None
    assert agendador._scheduler is None


def test_init_liga_no_processo_que_serve(app, ids, monkeypatch):
    monkeypatch.setitem(app.config, 'AGENDADOR_ENABLED', True)
    monkeypatch.setenv('WERKZEUG_RUN_MAIN', 'true')
    agendador.shutdown()
    sched = agendador.init(app)
    assert sched is not None
    assert sched.running
