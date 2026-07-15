"""Agendador de emissão proativa (spec 02, AD-009).

BackgroundScheduler in-process (sem broker) iniciado uma única vez no boot,
guardado contra o reloader do Flask. Dispara dois jobs diários em hora local
(AD-004): renovação proativa e snapshot. A durabilidade do "o que fazer" vive na
`TarefaEmissao` (fila_emissao), não no jobstore — por isso o agendamento é
recriável do config a cada boot.

Este módulo NÃO importa `routes`: os fluxos automatizáveis (FGTS/RS/Municipal)
são injetados por `routes` via `registrar_fluxo` no import, evitando ciclo.
"""
import os
from threading import Lock

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.services import fila_emissao, snapshot_service
from app.services.execution_logger import log_event

_JOB_RENOVACAO = 'agendador_renovacao_diaria'
_JOB_SNAPSHOT = 'agendador_snapshot_diario'
# 6h: se o PC ligou depois do horário, o job ainda roda atrasado (catch-up).
_MISFIRE_GRACE = 6 * 3600

_scheduler = None
_scheduler_lock = Lock()
_fluxos = {}  # tipo_value -> cfg do fluxo automatizável (registrado por routes)


# --- registry de fluxos (injetado por routes, sem import circular) ---------

def registrar_fluxo(tipo, cfg):
    """Registra um fluxo automatizável para o job de renovação. Chamado por
    `routes` no import."""
    _fluxos[tipo.value if hasattr(tipo, 'value') else str(tipo)] = cfg


def fluxos_registrados():
    return dict(_fluxos)


# --- leitura de config -----------------------------------------------------

def _ler_config():
    """(hora, ativo) do agendamento, com defaults seguros quando não há linha de
    config ou o valor está fora da faixa."""
    from app import db
    from app.models import ConfiguracaoSistema
    cfg = db.session.get(ConfiguracaoSistema, 1)
    if cfg is None:
        return 3, True
    hora = cfg.agendador_hora
    if hora is None or not (0 <= hora <= 23):
        hora = 3
    return hora, bool(cfg.agendador_ativo)


# --- ciclo de vida ---------------------------------------------------------

def init(app):
    """Inicia o scheduler uma única vez e reconcilia tarefas órfãs no boot.

    Idempotente (no-op se já iniciado) e guardado contra o reloader do Flask: no
    modo debug o Werkzeug roda 2 processos e só o que serve (WERKZEUG_RUN_MAIN)
    deve agendar. Respeita `AGENDADOR_ENABLED` (desligado nos testes)."""
    global _scheduler
    if not app.config.get('AGENDADOR_ENABLED', True):
        return None
    if app.debug and not os.environ.get('WERKZEUG_RUN_MAIN'):
        # processo pai do reloader: não agenda (o filho o fará)
        return None
    with _scheduler_lock:
        if _scheduler is not None:
            return _scheduler
        with app.app_context():
            fila_emissao.reconciliar_orfas()
        _scheduler = BackgroundScheduler(daemon=True)
        _agendar_jobs(app)
        _scheduler.start()
        log_event('agendador_iniciado')
        return _scheduler


def reprogramar(app):
    """Relê hora/ativo do config e reprograma os jobs sem recriar o scheduler
    (sem restart). No-op se o scheduler não está rodando."""
    with _scheduler_lock:
        if _scheduler is None:
            return
        _agendar_jobs(app)
        log_event('agendador_reprogramado')


def shutdown():
    """Desliga o scheduler e limpa o singleton (usado no encerramento/testes)."""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None:
            try:
                _scheduler.shutdown(wait=False)
            except Exception:
                pass
            _scheduler = None


def _agendar_jobs(app):
    """(Re)agenda os jobs a partir do config. O snapshot roda sempre (sem custo);
    a renovação só quando `agendador_ativo` (gasta créditos de 2captcha)."""
    with app.app_context():
        hora, ativo = _ler_config()

    _scheduler.add_job(
        job_snapshot_diario, CronTrigger(hour=hora, minute=5),
        args=[app], id=_JOB_SNAPSHOT, replace_existing=True,
        misfire_grace_time=_MISFIRE_GRACE, coalesce=True, max_instances=1)

    if ativo:
        _scheduler.add_job(
            job_renovacao_diaria, CronTrigger(hour=hora, minute=0),
            args=[app], id=_JOB_RENOVACAO, replace_existing=True,
            misfire_grace_time=_MISFIRE_GRACE, coalesce=True, max_instances=1)
    elif _scheduler.get_job(_JOB_RENOVACAO):
        _scheduler.remove_job(_JOB_RENOVACAO)


# --- jobs ------------------------------------------------------------------

def job_snapshot_diario(app):
    """Gera o snapshot diário por job real (SCHED-07). Idempotente."""
    with app.app_context():
        snapshot_service.garantir_snapshot_diario()


def job_renovacao_diaria(app):
    """Job diário de renovação proativa (SCHED-04).

    A estrutura schedulável vive aqui; o comportamento de enfileirar os alvos a
    vencer e rodar os lotes por tipo é implementado em T8."""
    with app.app_context():
        log_event('agendador_renovacao_inicio')
        if not _fluxos:
            log_event('agendador_nada_a_fazer')
            return
