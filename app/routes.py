import os
import re
import time
from datetime import date, datetime, timedelta

from flask import (
    Blueprint,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

from app import db, file_manager
from app.errors import ErrorType, descrever_erro, mensagem_usuario
from app.automation import SITES_CERTIDOES, capture, pdf, steps
from app.automation.batch_state import (
    FGTS_BATCH_LOCK,
    FGTS_BATCH_STATE,
    MUNICIPAL_BATCH_LOCK,
    MUNICIPAL_BATCH_STATE,
    RS_BATCH_LOCK,
    RS_BATCH_STATE,
    emissao_individual_ativa,
    marcar_emissao_individual,
)
from app.automation.driver import (
    UcIndisponivelError,
    _ativar_politica_autoselect_rs_temporaria,
    _configurar_download_automatico_chrome,
    _criar_driver_chrome,
    _criar_driver_uc,
    _desativar_politica_autoselect_rs_temporaria,
    _municipal_profile_acquire,
    _municipal_profile_release,
)
from app.automation.sites import is_ipm_atende
from app.automation.emissao import (
    _aplicar_variantes_imbe,
    _automatizar_fgts,
    _buscar_municipio_por_cidade,
    _carregar_config_municipio,
    _classe_status_por_data,
    _emitir_estadual_rs_certidao,
    _emitir_fgts_certidao,
    _emitir_municipal_certidao_lote,
    _erro_indica_navegador_fechado,
    _fgts_quit_driver_async,
    _fgts_status_por_data,
    _formatar_cnpj,
    _login_certificado_rs,
    _municipal_batch_suportado,
    _nome_certidao_imbe,
    _normalizar_cnpj,
    _pick_changed_download_pdf,
    _resolve_imbe_tipo_from_subtipo,
    _snapshot_downloads_pdf,
    calcular_validade_padrao,
)
from app.models import (
    Certidao,
    ConfiguracaoSistema,
    Empresa,
    ExecucaoLote,
    Municipio,
    SnapshotCertidao,
    StatusEspecial,
    SubtipoCertidao,
    TipoCertidao,
    get_a_vencer_dias,
)
from app.utils import get_config_value as _get_config_value, normalizar_cidade, to_bool as _to_bool, utcnow_naive
from app.services import (
    agendador,
    auditoria,
    batch_engine,
    certidao_service,
    diagnostics,
    dossie_service,
    export_service,
    preflight,
)
from app.services.correlation import CorrelationContext
from app.services.execution_logger import log_event
from app.services.snapshot_service import (
    classificar_status_certidao as _classificar_status_certidao,
    garantir_snapshot_diario as _garantir_snapshot_diario,
)
from app.services.health import run_health_checks
from app.auth import requer_papel
from flask_login import current_user

bp = Blueprint('main', __name__)

@bp.before_app_request
def _before_request_observability():
    g.req_start = time.time()
    CorrelationContext.new_request_id()


@bp.after_app_request
def _after_request_observability(response):
    request_id = CorrelationContext.get_request_id()
    if request_id:
        response.headers['X-Request-Id'] = request_id

    duration_ms = int((time.time() - getattr(g, 'req_start', time.time())) * 1000)

    path = request.path or ''
    is_static = path.startswith('/static/') or path == '/favicon.ico'
    is_batch_poll = path in {'/fgts/lote/status', '/estadual-rs/lote/status', '/municipal/lote/status'}
    is_health_ok = path == '/health' and response.status_code == 200

    if is_static or is_health_ok or (is_batch_poll and response.status_code == 200):
        CorrelationContext.clear()
        return response

    log_event(
        'http_request',
        method=request.method,
        path=path,
        status_code=response.status_code,
        duration_ms=duration_ms,
    )
    CorrelationContext.clear()
    return response


def _current_app_object():
    return current_app._get_current_object()


def _json_error(message=None, code=400, exc=None, **extra):
    info = descrever_erro(exc) if exc is not None else None
    texto = message or (mensagem_usuario(exc) if exc is not None else 'Erro inesperado.')
    payload = {
        'status': 'error',
        'message': texto,
        'mensagem': texto,
        'codigo': code,
        'request_id': CorrelationContext.get_request_id(),
    }
    if info is not None:
        payload.setdefault('error_type', info.tipo.value)
        payload.setdefault('acao', info.acao)
    payload.update(extra)
    return jsonify(payload), code


def _calc_fgts_targets_by_scope(start_certidao_id, scope='default'):
    return batch_engine.calc_targets(
        start_certidao_id,
        extra_filter=lambda query: query.filter(Certidao.tipo == TipoCertidao.FGTS),
        scope=scope,
        tipo=TipoCertidao.FGTS,
    )


def _calc_estadual_rs_targets_by_scope(start_certidao_id, scope='default'):
    return batch_engine.calc_targets(
        start_certidao_id,
        extra_filter=lambda query: (
            query.join(Empresa, Empresa.id == Certidao.empresa_id)
                 .filter(Certidao.tipo == TipoCertidao.ESTADUAL)
                 .filter(Empresa.estado == 'RS')
        ),
        scope=scope,
        tipo=TipoCertidao.ESTADUAL,
    )


def _batch_targets_vazios(scope='default'):
    scope_norm = _parse_batch_scope(scope)
    return {
        'ids': [],
        'total': 0,
        'scope': scope_norm,
        'start_incluida': False,
        'vencidas': 0,
        'a_vencer': 0,
        'pendentes': 0,
    }


def _calc_municipal_targets_by_scope(start_certidao_id, scope='default'):
    certidao = db.session.get(Certidao, start_certidao_id)
    if not certidao or certidao.tipo != TipoCertidao.MUNICIPAL:
        return _batch_targets_vazios(scope=scope)

    cidade = (certidao.empresa.cidade or '').strip()
    if not _municipal_batch_suportado(cidade):
        return _batch_targets_vazios(scope=scope)

    subtipo = certidao.subtipo

    def _extra_filter(query):
        query = (query
                 .join(Empresa, Empresa.id == Certidao.empresa_id)
                 .filter(Certidao.tipo == TipoCertidao.MUNICIPAL)
                 .filter(Empresa.cidade == cidade))
        if subtipo and file_manager.remover_acentos(cidade).upper() == 'IMBE':
            query = query.filter(Certidao.subtipo == subtipo)
        return query

    return batch_engine.calc_targets(
        start_certidao_id,
        extra_filter=_extra_filter,
        scope=scope,
        tipo=TipoCertidao.MUNICIPAL,
    )


def _parse_batch_scope(raw_scope):
    scope = (raw_scope or 'default').strip().lower()
    if scope in {'pendente', 'pendentes'}:
        return 'pendentes'
    return 'default'


def _registrar_desfecho_lote(state):
    """Grava o desfecho do lote na ExecucaoLote correspondente (casada pelo
    execution_id). Chamada por run_batch_loop no fim (on_finish). Best-effort.

    Roda em toda saída do loop (inclusive pausa): quando o lote é retomado e
    conclui, o mesmo registro é sobrescrito com os números finais."""
    execution_id = state.get('execution_id')
    if not execution_id:
        return
    try:
        registro = (ExecucaoLote.query
                    .filter_by(execution_id=execution_id)
                    .order_by(ExecucaoLote.id.desc())
                    .first())
        if registro is None:
            return
        status = state.get('status')
        terminal = status in ('completed', 'stopped', 'error')
        registro.status = status
        registro.sucesso = state.get('success', 0)
        registro.pendentes_resultado = state.get('pendentes_resultado', 0)
        registro.falhas = state.get('falhas', 0)
        registro.finalizado_em = state.get('finished_at') or (
            utcnow_naive() if terminal else None)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        log_event('execucao_lote_desfecho_falhou', level='WARNING',
                  execution_id=execution_id, error=str(e))


def _rs_batch_worker(app):
    def _on_setup(_app):
        return _ativar_politica_autoselect_rs_temporaria()

    def _on_teardown(rs_policy_ativa):
        if rs_policy_ativa:
            _desativar_politica_autoselect_rs_temporaria()

    batch_engine.run_batch_loop(
        app,
        lock=RS_BATCH_LOCK,
        state=RS_BATCH_STATE,
        emit_fn=lambda cid, drv, eid: _emitir_estadual_rs_certidao(
            cid, driver=drv, usar_2captcha=True, execution_id=eid
        ),
        nome_lote='Estadual RS',
        curto='RS',
        tag='ESTADUAL-RS-LOTE',
        event_prefix='rs_batch_worker',
        create_driver=lambda: _criar_driver_chrome(anonimo=False, usar_perfil=True),
        eager_driver=True,
        on_setup=_on_setup,
        on_teardown=_on_teardown,
        on_finish=_registrar_desfecho_lote,
    )


def _municipal_batch_worker(app):
    batch_engine.run_batch_loop(
        app,
        lock=MUNICIPAL_BATCH_LOCK,
        state=MUNICIPAL_BATCH_STATE,
        emit_fn=lambda cid, drv, eid: _emitir_municipal_certidao_lote(
            cid, driver=drv, execution_id=eid
        ),
        nome_lote='Municipal',
        curto='Municipal',
        tag='MUNICIPAL-LOTE',
        event_prefix='municipal_batch_worker',
        create_driver=_criar_driver_chrome,
        on_finish=_registrar_desfecho_lote,
    )


def _fgts_batch_worker(app):
    def _recover(certidao_id, execution_id, driver, sucesso, grave, mensagem):
        # FGTS: recria o driver e tenta de novo apos falha de carregamento da pagina
        if grave and mensagem == 'Erro ao carregar página FGTS.':
            try:
                driver.quit()
            except Exception:
                pass
            driver = _criar_driver_chrome()
            log_event(
                'fgts_batch_driver_recreate', level='WARNING',
                certidao_id=certidao_id, execution_id=execution_id,
                message='Recriando driver após falha de carregamento.',
            )
            sucesso, grave, mensagem = _emitir_fgts_certidao(
                certidao_id, driver=driver, execution_id=execution_id
            )
        return driver, sucesso, grave, mensagem

    batch_engine.run_batch_loop(
        app,
        lock=FGTS_BATCH_LOCK,
        state=FGTS_BATCH_STATE,
        emit_fn=lambda cid, drv, eid: _emitir_fgts_certidao(cid, driver=drv, execution_id=eid),
        nome_lote='FGTS',
        curto='FGTS',
        tag='FGTS-LOTE',
        event_prefix='fgts_batch_worker',
        create_driver=_criar_driver_chrome,
        recover_fn=_recover,
        on_finish=_registrar_desfecho_lote,
    )


def _rs_lote_precondicao():
    if not _to_bool(_get_config_value('RS_ALTCHA_AUTOSOLVE_ENABLED', False), False):
        return _json_error('Ative RS_ALTCHA_AUTOSOLVE_ENABLED para usar lote Estadual RS.', 400)
    return None


def _preflight_erro(precisa_solver=False):
    """Roda as pre-checagens e devolve uma resposta de erro acionavel se algo
    estiver faltando (rede, Chrome, solver); None quando tudo ok."""
    problemas = preflight.checar_emissao(current_app.config, precisa_solver=precisa_solver)
    if not problemas:
        return None
    p = problemas[0]
    return _json_error(p['message'], 409, error_type=p['error_type'],
                       acao=p['acao'], preflight=problemas)


def _preflight_precondicao(base=None, precisa_solver=False):
    """Compoe uma precondicao de lote: roda 'base' (se houver) e o preflight."""
    def _checar():
        if base is not None:
            erro = base()
            if erro is not None:
                return erro
        return _preflight_erro(precisa_solver=precisa_solver)
    return _checar


def _rotulo_execucao_municipal(certidao_id):
    """Rótulo do registro de execução do lote municipal, separado por cidade
    (Imbé / Tramandaí), já que cada lote municipal roda para uma cidade só."""
    try:
        certidao = db.session.get(Certidao, certidao_id)
        cidade = (certidao.empresa.cidade or '').strip() if certidao else ''
    except Exception:
        cidade = ''
    norm = file_manager.remover_acentos(cidade).upper()
    if norm == 'IMBE':
        return 'Municipal Imbé'
    if norm == 'TRAMANDAI':
        return 'Municipal Tramandaí'
    return 'Municipal'


def _registrar_execucao_lote(nome_lote, scope, total, execution_id):
    """Grava (persistente) o início de um lote para o relatório "último lote".
    Best-effort: uma falha ao gravar nunca deve impedir o lote de iniciar."""
    try:
        db.session.add(ExecucaoLote(
            tipo=nome_lote,
            escopo=scope or 'default',
            total=total or 0,
            iniciado_em=utcnow_naive(),
            execution_id=execution_id,
        ))
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        log_event('execucao_lote_registro_falhou', level='WARNING',
                  lote=nome_lote, error=str(e))


def _register_batch_routes(prefix, endpoint_base, cfg):
    """Registra as 6 rotas de lote (info/iniciar/pausar/parar/retomar/status) de
    um fluxo, eliminando a duplicacao entre FGTS, Estadual RS e Municipal."""
    lock = cfg['lock']
    state = cfg['state']
    worker = cfg['worker']
    calc_targets = cfg['calc_targets']
    tag = cfg.get('tag')
    nome = cfg['nome_lote']
    precondicao = cfg.get('precondicao')

    def info(certidao_id):
        scope = _parse_batch_scope(request.args.get('scope'))
        return jsonify({'status': 'ok', **calc_targets(certidao_id, scope=scope)})

    def iniciar():
        dados = request.get_json() or {}
        certidao_id = dados.get('certidao_id')
        scope = _parse_batch_scope(dados.get('scope'))
        if not certidao_id:
            return _json_error('Certidão inválida.', 400)
        if emissao_individual_ativa():
            return _json_error(
                'Há uma emissão individual em andamento. Aguarde concluir para iniciar o lote.',
                400,
            )
        if precondicao is not None:
            erro = precondicao()
            if erro is not None:
                return erro
        dados_lote = batch_engine.init_batch_run(
            lock, state, certidao_id,
            lambda start_id: calc_targets(start_id, scope=scope),
            worker, app_factory=_current_app_object,
        )
        if dados_lote is None:
            return _json_error(cfg['msg_em_andamento'], 400)
        if not dados_lote:
            if scope == 'pendentes':
                return _json_error(cfg['msg_vazio_pendentes'], 400)
            return _json_error(cfg['msg_vazio_default'], 400)
        log_event(
            cfg['started_event'], status='running', scope=scope,
            total=dados_lote['total'], execution_id=state.get('execution_id'),
        )
        rotulo_fn = cfg.get('rotulo_execucao')
        rotulo = rotulo_fn(certidao_id) if rotulo_fn else nome
        _registrar_execucao_lote(
            rotulo or nome, scope, dados_lote['total'], state.get('execution_id'))
        with lock:
            batch_engine.append_batch_message(
                state, f"Lote {nome} iniciado. Total={dados_lote['total']}.", level='info')
        auditoria.registrar('lote.iniciar', alvo_tipo='certidao', alvo_id=certidao_id, detalhe=nome)
        return jsonify({'status': 'ok'})

    def pausar():
        driver = batch_engine.request_pause(lock, state)
        log_event('batch_paused', level='WARNING', lote=nome, tag=tag)
        with lock:
            batch_engine.append_batch_message(
                state, f"Lote {nome} pausado por solicitação.", level='warning')
        _fgts_quit_driver_async(driver)
        return jsonify({'status': 'ok', 'message': cfg['msg_pausado']})

    def parar():
        driver = batch_engine.request_stop(lock, state)
        log_event('batch_stopped', level='WARNING', lote=nome, tag=tag)
        with lock:
            batch_engine.append_batch_message(
                state, f"Lote {nome} interrompido por solicitação.", level='warning')
        _fgts_quit_driver_async(driver)
        return jsonify({'status': 'ok', 'message': cfg['msg_interrompido']})

    def retomar():
        if not batch_engine.resume_batch(lock, state, worker, app_factory=_current_app_object):
            return _json_error(cfg['msg_nao_pausado'], 400)
        log_event('batch_resumed', lote=nome, tag=tag)
        with lock:
            batch_engine.append_batch_message(
                state, f"Lote {nome} retomado por solicitação.", level='info')
        return jsonify({'status': 'ok'})

    def status_view():
        return jsonify(batch_engine.status_payload_locked(lock, state))

    # info/status sao leitura (dashboard poll); iniciar/pausar/parar/retomar mutam -> operador
    op = requer_papel('operador')
    bp.add_url_rule(f'{prefix}/lote/info/<int:certidao_id>', f'{endpoint_base}_info', info)
    bp.add_url_rule(f'{prefix}/lote/iniciar', f'{endpoint_base}_iniciar', op(iniciar), methods=['POST'])
    bp.add_url_rule(f'{prefix}/lote/pausar', f'{endpoint_base}_pausar', op(pausar), methods=['POST'])
    bp.add_url_rule(f'{prefix}/lote/parar', f'{endpoint_base}_parar', op(parar), methods=['POST'])
    bp.add_url_rule(f'{prefix}/lote/retomar', f'{endpoint_base}_retomar', op(retomar), methods=['POST'])
    bp.add_url_rule(f'{prefix}/lote/status', f'{endpoint_base}_status', status_view)


_register_batch_routes('/fgts', 'fgts_lote', {
    'lock': FGTS_BATCH_LOCK, 'state': FGTS_BATCH_STATE,
    'worker': _fgts_batch_worker, 'calc_targets': _calc_fgts_targets_by_scope,
    'started_event': 'fgts_batch_started', 'tag': 'FGTS-LOTE', 'nome_lote': 'FGTS',
    'precondicao': _preflight_precondicao(),
    'msg_em_andamento': 'Já existe um lote em andamento.',
    'msg_vazio_pendentes': 'Nenhuma certidão FGTS pendente para emissão.',
    'msg_vazio_default': 'Nenhuma certidão FGTS vencida ou a vencer.',
    'msg_pausado': 'Lote pausado.',
    'msg_interrompido': 'Lote interrompido.',
    'msg_nao_pausado': 'Lote não está pausado.',
})

_register_batch_routes('/estadual-rs', 'estadual_rs_lote', {
    'lock': RS_BATCH_LOCK, 'state': RS_BATCH_STATE,
    'worker': _rs_batch_worker, 'calc_targets': _calc_estadual_rs_targets_by_scope,
    'started_event': 'rs_batch_started', 'tag': 'ESTADUAL-RS-LOTE', 'nome_lote': 'Estadual RS',
    'precondicao': _preflight_precondicao(_rs_lote_precondicao, precisa_solver=True),
    'msg_em_andamento': 'Já existe um lote Estadual RS em andamento.',
    'msg_vazio_pendentes': 'Nenhuma certidão Estadual RS pendente para emissão.',
    'msg_vazio_default': 'Nenhuma certidão Estadual RS vencida ou a vencer.',
    'msg_pausado': 'Lote Estadual RS pausado.',
    'msg_interrompido': 'Lote Estadual RS interrompido.',
    'msg_nao_pausado': 'Lote Estadual RS não está pausado.',
})

_register_batch_routes('/municipal', 'municipal_lote', {
    'lock': MUNICIPAL_BATCH_LOCK, 'state': MUNICIPAL_BATCH_STATE,
    'worker': _municipal_batch_worker, 'calc_targets': _calc_municipal_targets_by_scope,
    'rotulo_execucao': _rotulo_execucao_municipal,
    'started_event': 'municipal_batch_started', 'tag': None, 'nome_lote': 'Municipal',
    'precondicao': _preflight_precondicao(),
    'msg_em_andamento': 'Já existe um lote Municipal em andamento.',
    'msg_vazio_pendentes': 'Nenhuma certidão Municipal pendente para emissão.',
    'msg_vazio_default': 'Nenhuma certidão Municipal vencida ou a vencer.',
    'msg_pausado': 'Lote Municipal pausado.',
    'msg_interrompido': 'Lote Municipal interrompido.',
    'msg_nao_pausado': 'Lote Municipal não está pausado.',
})


# --- Fluxos automatizáveis do agendador (spec 02) --------------------------
# Reusam o MESMO run_batch_loop dos lotes manuais, mas rodam SÍNCRONO (no thread
# do agendador) e alimentados pela fila durável. Nenhum estado de lote paralelo:
# os locks/states por tipo são os mesmos de batch_state. `wrap_emit` (vindo do
# job) envolve o emit real para transicionar cada TarefaEmissao.

def _rodar_lote_agendado(app, ids, *, wrap_emit, execution_id, lock, state,
                         real_emit, nome_lote, **loop_kwargs):
    # Não concorre com uma emissão individual em curso (mesma guarda do lote
    # manual): dois drivers podem disputar o mesmo perfil Chrome (ex.: RS).
    if emissao_individual_ativa():
        log_event('agendador_lote_pulado_emissao_individual', lote=nome_lote,
                  execution_id=execution_id)
        return
    with lock:
        # Serialização com o lote manual: se já há um em andamento/pausado deste
        # tipo, o agendador não clobbera o estado — pula e roda no próximo ciclo
        # (edge case da spec: "respeitar o lock global do tipo").
        if state.get('status') in ('running', 'paused'):
            log_event('agendador_lote_pulado_em_andamento', lote=nome_lote,
                      execution_id=execution_id)
            return
        batch_engine.reset_batch_state(state)
        state.update(status='running', ids=list(ids), total=len(ids),
                     started_at=utcnow_naive(), execution_id=execution_id)
    _registrar_execucao_lote(nome_lote, 'default', len(ids), execution_id)
    # Caminho do agendador: tolerante a grave por-item (RESIL-01). Um grave
    # "comum" (ex.: timeout de download) NAO aborta o lote — vira falha por-item
    # (fila TarefaEmissao / retry) e o loop segue. GRAVE_FATAL (driver morto)
    # ainda para o lote. O lote manual (chamadas diretas em routes) mantem o
    # default parar_em_grave=True.
    batch_engine.run_batch_loop(
        app, lock=lock, state=state, emit_fn=wrap_emit(real_emit),
        nome_lote=nome_lote, parar_em_grave=False, **loop_kwargs)


def _fluxo_fgts_calc_ids(app):
    return _calc_fgts_targets_by_scope(None, scope='default')['ids']


def _fluxo_fgts_rodar(app, ids, *, wrap_emit, execution_id):
    _rodar_lote_agendado(
        app, ids, wrap_emit=wrap_emit, execution_id=execution_id,
        lock=FGTS_BATCH_LOCK, state=FGTS_BATCH_STATE,
        real_emit=lambda cid, drv, eid: _emitir_fgts_certidao(cid, driver=drv, execution_id=eid),
        nome_lote='FGTS', curto='FGTS', tag='FGTS-LOTE',
        event_prefix='fgts_batch_worker', create_driver=_criar_driver_chrome,
        on_finish=_registrar_desfecho_lote)


def _fluxo_rs_habilitado():
    return _to_bool(_get_config_value('RS_ALTCHA_AUTOSOLVE_ENABLED', False), False)


def _fluxo_rs_calc_ids(app):
    # Sem o solver ALTCHA a emissão RS não é automatizável — não enfileira nada
    # (mesma precondição do lote manual), evitando tarefas que só falhariam.
    if not _fluxo_rs_habilitado():
        return []
    return _calc_estadual_rs_targets_by_scope(None, scope='default')['ids']


def _fluxo_rs_rodar(app, ids, *, wrap_emit, execution_id):
    def _on_setup(_app):
        return _ativar_politica_autoselect_rs_temporaria()

    def _on_teardown(rs_policy_ativa):
        if rs_policy_ativa:
            _desativar_politica_autoselect_rs_temporaria()

    _rodar_lote_agendado(
        app, ids, wrap_emit=wrap_emit, execution_id=execution_id,
        lock=RS_BATCH_LOCK, state=RS_BATCH_STATE,
        real_emit=lambda cid, drv, eid: _emitir_estadual_rs_certidao(
            cid, driver=drv, usar_2captcha=True, execution_id=eid),
        nome_lote='Estadual RS', curto='RS', tag='ESTADUAL-RS-LOTE',
        event_prefix='rs_batch_worker',
        create_driver=lambda: _criar_driver_chrome(anonimo=False, usar_perfil=True),
        eager_driver=True, on_setup=_on_setup, on_teardown=_on_teardown,
        on_finish=_registrar_desfecho_lote)


def _fluxo_municipal_calc_ids(app):
    dados = batch_engine.calc_targets(
        None,
        extra_filter=lambda q: q.filter(Certidao.tipo == TipoCertidao.MUNICIPAL),
        scope='default', tipo=TipoCertidao.MUNICIPAL)
    if not dados['ids']:
        return []
    # Uma query só (id, cidade) para filtrar as cidades suportadas — sem N+1.
    rows = (db.session.query(Certidao.id, Empresa.cidade)
            .join(Empresa, Empresa.id == Certidao.empresa_id)
            .filter(Certidao.id.in_(dados['ids']))
            .all())
    return [cid for cid, cidade in rows if _municipal_batch_suportado(cidade or '')]


def _fluxo_municipal_rodar(app, ids, *, wrap_emit, execution_id):
    _rodar_lote_agendado(
        app, ids, wrap_emit=wrap_emit, execution_id=execution_id,
        lock=MUNICIPAL_BATCH_LOCK, state=MUNICIPAL_BATCH_STATE,
        real_emit=lambda cid, drv, eid: _emitir_municipal_certidao_lote(
            cid, driver=drv, execution_id=eid),
        nome_lote='Municipal', curto='Municipal', tag=None,
        event_prefix='municipal_batch_worker', create_driver=_criar_driver_chrome,
        on_finish=_registrar_desfecho_lote)


def _registrar_fluxos_agendador():
    """Registra os fluxos automatizáveis no agendador (idempotente). Chamado no
    import de routes; exposto para os testes re-registrarem se necessário."""
    agendador.registrar_fluxo(TipoCertidao.FGTS, {
        'tipo': TipoCertidao.FGTS,
        'calc_ids': _fluxo_fgts_calc_ids, 'rodar_lote': _fluxo_fgts_rodar})
    agendador.registrar_fluxo(TipoCertidao.ESTADUAL, {
        'tipo': TipoCertidao.ESTADUAL,
        'calc_ids': _fluxo_rs_calc_ids, 'rodar_lote': _fluxo_rs_rodar})
    agendador.registrar_fluxo(TipoCertidao.MUNICIPAL, {
        'tipo': TipoCertidao.MUNICIPAL,
        'calc_ids': _fluxo_municipal_calc_ids, 'rodar_lote': _fluxo_municipal_rodar})


_registrar_fluxos_agendador()


@bp.route('/health')
def health():
    # liveness publico e minimo: nao vaza detalhes de infra (AUTH-07.2)
    detalhado = _to_bool(request.args.get('detalhado'))
    if not detalhado:
        return jsonify({'status': 'ok'}), 200
    # health detalhado exige admin
    if not (current_user.is_authenticated and current_user.papel == 'admin'):
        return _json_error('Detalhes de saúde exigem admin.', 403, error_type='forbidden')
    checks = run_health_checks(current_app.config)
    has_failure = any(not item.get('ok') for item in checks.values())
    code = 200 if not has_failure else 503
    return jsonify({'status': 'ok' if not has_failure else 'degraded', 'checks': checks}), code


@bp.route('/diagnostico')
@requer_papel('admin')
def diagnostico():
    return render_template('diagnostico.html')


@bp.route('/diagnostico/eventos')
@requer_papel('admin')
def diagnostico_eventos():
    return jsonify({
        'status': 'ok',
        'eventos': diagnostics.eventos_para_painel(limite=100),
        'alertas': diagnostics.alertas_ativos(),
    })


@bp.route('/diagnostico/2captcha')
@requer_papel('admin')
def diagnostico_2captcha():
    """Saldo atual da conta 2captcha para o painel de diagnóstico. Best-effort:
    `saldo` vem None se a chave não está configurada ou a consulta falha."""
    from app.captcha_solver import consultar_saldo
    tem_chave = bool((current_app.config.get('CAPTCHA_2_API_KEY') or '').strip())
    saldo = consultar_saldo(current_app.config) if tem_chave else None
    minimo = current_app.config.get('CAPTCHA_2_SALDO_MINIMO', 0)
    return jsonify({
        'status': 'ok',
        'configurado': tem_chave,
        'saldo': saldo,
        'minimo': minimo,
        'baixo': (saldo is not None and saldo < minimo),
    })


@bp.route('/fgts/emitir_unico', methods=['POST'])
@requer_papel('operador')
def fgts_emitir_unico():
    dados = request.get_json() or {}
    certidao_id = dados.get('certidao_id')

    if not certidao_id:
        return _json_error('Certidão inválida.', 400)

    with FGTS_BATCH_LOCK:
        if FGTS_BATCH_STATE['status'] == 'running':
            return _json_error('Lote em andamento. Pare o lote para emitir individual.', 400)

    erro_preflight = _preflight_erro()
    if erro_preflight is not None:
        return erro_preflight

    execution_id = CorrelationContext.new_execution_id()
    sucesso, grave, mensagem = _emitir_fgts_certidao(certidao_id, execution_id=execution_id)

    if grave:
        return _json_error(mensagem or 'Erro grave no FGTS.', 500)

    if not sucesso:
        return _json_error(mensagem or 'Falha ao emitir certidão FGTS.', 400)

    certidao = db.session.get(Certidao, certidao_id)
    data_formatada = certidao.data_validade.strftime('%d/%m/%Y') if certidao and certidao.data_validade else None

    return jsonify({
        'status': 'ok',
        'certidao_id': certidao_id,
        'data_formatada': data_formatada,
        'nova_classe': _fgts_status_por_data(certidao.data_validade if certidao else None)
    })


def _get_visualizar_serializer():
    secret = current_app.config.get('SECRET_KEY') or 'certidoes-secret'
    return URLSafeTimedSerializer(secret, salt='visualizar-certidao')


def _gerar_visualizar_token(certidao_id):
    return _get_visualizar_serializer().dumps({'cid': certidao_id})


def _carregar_visualizar_token(token, max_age=60 * 60 * 24):
    try:
        data = _get_visualizar_serializer().loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    return data.get('cid') if isinstance(data, dict) else None


@bp.app_template_global()
def visualizar_token(certidao_id):
    return _gerar_visualizar_token(certidao_id)


def _normalizar_cidade_dashboard(valor):
    # Alias fino para a fonte unica em utils (reuso painel/export — spec 04).
    return normalizar_cidade(valor)


def _escolher_cidade_canonica_dashboard(variantes):
    def _ordenacao(item):
        nome, frequencia = item
        tem_acento = file_manager.remover_acentos(nome) != nome
        return (
            -frequencia,
            -int(tem_acento),
            _normalizar_cidade_dashboard(nome),
            nome.upper(),
        )

    return sorted(variantes.items(), key=_ordenacao)[0][0]

@bp.context_processor
def inject_year():
    return {'year': datetime.now().year}


def _contar_pendencias():
    """Total global de certidões que exigem ação (vencidas + a vencer).

    Consulta apenas as colunas necessárias para não materializar objetos
    Certidao a cada chamada. Reutilizado pelo context processor (title da
    aba no page-load) e pelo endpoint /api/pendencias (polling em tempo real).
    """
    hoje = date.today()
    limites_por_tipo = {t: get_a_vencer_dias(tipo=t) for t in TipoCertidao}

    total = 0
    linhas = db.session.query(
        Certidao.tipo,
        Certidao.data_validade,
        Certidao.status_especial,
    ).all()
    for tipo, data_validade, status_especial in linhas:
        if status_especial == StatusEspecial.PENDENTE or not data_validade:
            continue
        if data_validade < hoje or (data_validade - hoje).days <= limites_por_tipo[tipo]:
            total += 1

    return total


@bp.context_processor
def inject_pendencias_total():
    """Disponibiliza a contagem de pendências em todos os templates (title da aba)."""
    return {'pendencias_total': _contar_pendencias()}


@bp.route('/api/pendencias')
def api_pendencias():
    """Total de pendências para o polling do title da aba (base.html)."""
    return jsonify({'total': _contar_pendencias()})

@bp.route('/')
def dashboard():
    status_filtros = request.args.getlist('status')
    tipo_filtros = request.args.getlist('tipo')
    # Estado e cidade são multi-seleção e filtrados no cliente (chips com
    # contagem combinável). O servidor só lê os params para pré-marcar os chips.
    estado_filtros = [e.strip().upper() for e in request.args.getlist('estado') if e and e.strip()]
    cidade_filtros = []
    for c in request.args.getlist('cidade'):
        chave = _normalizar_cidade_dashboard(c or '')
        if chave and chave not in cidade_filtros:
            cidade_filtros.append(chave)
    ordem = (request.args.get('ordem') or 'urgencia').strip().lower()

    query = db.session.query(Empresa).distinct()

    hoje = date.today()
    _garantir_snapshot_diario()
    a_vencer_dias = get_a_vencer_dias()
    limites_por_tipo = {t: get_a_vencer_dias(tipo=t) for t in TipoCertidao}
    if ordem not in {'urgencia', 'az', 'vencimento', 'atualizacao'}:
        ordem = 'urgencia'

    if not status_filtros:
        status_filtros = ['todas']
    elif 'todas' in status_filtros:
        status_filtros = ['todas']

    if not tipo_filtros:
        tipo_filtros = ['todas']
    elif 'todas' in tipo_filtros:
        tipo_filtros = ['todas']
    else:
        tipos_validos = {'federal', 'fgts', 'estadual', 'municipal', 'trabalhista'}
        tipo_filtros = [t for t in tipo_filtros if t in tipos_validos]
        if not tipo_filtros:
            tipo_filtros = ['todas']

    # Sem filtro server-side de estado/cidade: a query carrega todas as empresas
    # para o cliente poder contar de forma cruzada (estado × cidade × tipo × status).

    # Query única para cidades e estados (evita round-trip extra ao banco)
    cidades_variantes = {}
    cidades_estados = {}
    estados_set = set()
    for cidade, estado in db.session.query(Empresa.cidade, Empresa.estado).all():
        if estado:
            estados_set.add(estado)
        cidade = (cidade or '').strip()
        if not cidade:
            continue
        chave_normalizada = _normalizar_cidade_dashboard(cidade)
        if not chave_normalizada:
            continue
        variantes = cidades_variantes.setdefault(chave_normalizada, {})
        variantes[cidade] = variantes.get(cidade, 0) + 1
        if estado:
            cidades_estados.setdefault(chave_normalizada, set()).add(estado)

    estados_disponiveis = sorted(estados_set)

    cidades_por_chave = {
        chave: _escolher_cidade_canonica_dashboard(variantes)
        for chave, variantes in cidades_variantes.items()
    }

    # Chips de cidade: rótulo canônico + estados a que a cidade pertence
    # (usado pelo recorte "cidade segue o estado" no cliente)
    cidades_chips = [
        {
            'key': chave,
            'label': cidades_por_chave.get(chave, chave),
            'estados': sorted(cidades_estados.get(chave, set())),
        }
        for chave in cidades_por_chave
    ]
    cidades_chips.sort(key=lambda c: _normalizar_cidade_dashboard(c['label']))

    empresas = query.order_by(Empresa.id).all()

    # Chave canônica de cidade por empresa: agrupa variações ("Imbé"/"IMBE")
    # e alimenta o data-cidade-key de cada card para a contagem client-side.
    cidade_key_por_empresa = {
        emp.id: _normalizar_cidade_dashboard(emp.cidade or '')
        for emp in empresas
    }

    # Pré-computa contadores e status de cada certidão em Python,
    # eliminando dois loops Jinja2 por empresa no template.
    contadores_por_empresa = {}
    status_por_cert = {}
    certidoes_por_empresa = {}
    for empresa in empresas:
        counts = {
            'total': 0, 'validas': 0, 'a_vencer': 0, 'vencidas': 0,
            'pendentes': 0, 'nao_definida': 0,
            'tipo_total': 0, 'tipo_federal': 0, 'tipo_fgts': 0,
            'tipo_estadual': 0, 'tipo_municipal': 0, 'tipo_trabalhista': 0,
            'menor_validade': '9999-12-31',
            'ultima_atualizacao': '',
        }
        for c in empresa.certidoes:
            if c.status_especial == StatusEspecial.PENDENTE:
                sc = 'pendentes'
            elif not c.data_validade:
                sc = 'nao_definida'
            elif c.data_validade < hoje:
                sc = 'vencidas'
            elif (c.data_validade - hoje).days <= limites_por_tipo[c.tipo]:
                sc = 'a_vencer'
            else:
                sc = 'validas'
            status_por_cert[c.id] = sc
            counts['total'] += 1
            counts[sc] += 1
            counts['tipo_total'] += 1
            counts['tipo_' + c.tipo.name.lower()] = counts.get('tipo_' + c.tipo.name.lower(), 0) + 1
            if c.data_validade and sc != 'pendentes':
                dval = c.data_validade.strftime('%Y-%m-%d')
                if dval < counts['menor_validade']:
                    counts['menor_validade'] = dval
            # ultima_atualizacao da empresa = a mais recente entre as certidoes
            # (max ISO). '' inicial < qualquer ISO, entao sem dado fica ''.
            if c.atualizado_em:
                iso = c.atualizado_em.strftime('%Y-%m-%dT%H:%M:%S')
                if iso > counts['ultima_atualizacao']:
                    counts['ultima_atualizacao'] = iso
        contadores_por_empresa[empresa.id] = counts
        certidoes_por_empresa[empresa.id] = sorted(empresa.certidoes, key=lambda c: c.ordem_exibicao)

    municipios = Municipio.query.all()

    urls_municipais = {}
    for m in municipios:
        if not m.url_certidao:
            continue
        nome = (m.nome or '').strip()
        nome_sem = file_manager.remover_acentos(nome)
        url = m.url_certidao

        urls_municipais[nome] = url
        urls_municipais[nome_sem] = url

        if nome_sem.upper() == 'IMBE':
            config_cfg = _carregar_config_municipio(m)
            cfg_geral = (((config_cfg or {}).get('imbe_variantes') or {}).get('geral') or {})
            url_geral = cfg_geral.get('url')
            if url_geral:
                urls_municipais[nome + '_GERAL'] = url_geral
                urls_municipais[nome_sem + '_GERAL'] = url_geral

    return render_template(
        'dashboard.html',
        empresas=empresas,
        contadores_por_empresa=contadores_por_empresa,
        status_por_cert=status_por_cert,
        certidoes_por_empresa=certidoes_por_empresa,
        status_filtros=status_filtros,
        tipo_filtros=tipo_filtros,
        estado_filtros=estado_filtros,
        cidade_filtros=cidade_filtros,
        estados_disponiveis=estados_disponiveis,
        cidades_chips=cidades_chips,
        cidade_key_por_empresa=cidade_key_por_empresa,
        hoje=hoje,
        a_vencer_dias=a_vencer_dias,
        ordem=ordem,
        sites_urls=SITES_CERTIDOES,
        urls_municipais=urls_municipais
    )


@bp.route('/empresas')
def empresas():
    termo = (request.args.get('q') or '').strip()
    estado_filtro = (request.args.get('estado') or '').strip().upper()
    cidade_filtro = (request.args.get('cidade') or '').strip()

    query = Empresa.query
    if termo:
        query = query.filter(Empresa.nome.ilike(f"%{termo}%"))

    if estado_filtro:
        query = query.filter(Empresa.estado == estado_filtro)

    cidades_variantes = {}
    cidades_db = db.session.query(Empresa.cidade).all()
    for row in cidades_db:
        cidade = (row[0] or '').strip()
        if not cidade:
            continue

        chave_normalizada = _normalizar_cidade_dashboard(cidade)
        if not chave_normalizada:
            continue

        variantes = cidades_variantes.setdefault(chave_normalizada, {})
        variantes[cidade] = variantes.get(cidade, 0) + 1

    cidades_por_chave = {
        chave: _escolher_cidade_canonica_dashboard(variantes)
        for chave, variantes in cidades_variantes.items()
    }
    cidades_disponiveis = sorted(
        cidades_por_chave.values(),
        key=_normalizar_cidade_dashboard,
    )

    empresas = query.order_by(Empresa.id).all()

    if cidade_filtro:
        chave_filtro = _normalizar_cidade_dashboard(cidade_filtro)
        if chave_filtro:
            empresas = [
                empresa for empresa in empresas
                if _normalizar_cidade_dashboard(empresa.cidade) == chave_filtro
            ]
            cidade_filtro = cidades_por_chave.get(chave_filtro, cidade_filtro)

    estados_disponiveis = [
        row[0] for row in
        db.session.query(Empresa.estado).distinct().order_by(Empresa.estado).all()
    ]

    return render_template(
        'empresas.html',
        empresas=empresas,
        termo=termo,
        estado_filtro=estado_filtro,
        cidade_filtro=cidade_filtro,
        estados_disponiveis=estados_disponiveis,
        cidades_disponiveis=cidades_disponiveis,
    )


@bp.route('/empresa/<int:empresa_id>')
def empresa_detalhe(empresa_id):
    empresa = Empresa.query.get_or_404(empresa_id)
    certidoes = sorted(empresa.certidoes, key=lambda item: item.ordem_exibicao)
    return render_template(
        'empresa_detalhe.html',
        empresa=empresa,
        certidoes=certidoes,
        hoje=date.today(),
        a_vencer_dias=get_a_vencer_dias(),
    )


@bp.route('/empresa/<int:empresa_id>/editar', methods=['POST'])
@requer_papel('operador')
def empresa_editar(empresa_id):
    empresa = Empresa.query.get_or_404(empresa_id)
    nome = (request.form.get('nome') or '').strip()
    estado = (request.form.get('estado') or '').strip().upper()
    cidade = (request.form.get('cidade') or '').strip()
    inscricao = (request.form.get('inscricao_mobiliaria') or '').strip()
    next_url = request.form.get('next') or url_for('main.empresa_detalhe', empresa_id=empresa_id)

    if not nome:
        flash('Nome da empresa é obrigatório.', 'warning')
        return redirect(next_url)

    if not estado or not re.match(r'^[A-Z]{2}$', estado):
        flash('Estado inválido. Use a sigla com 2 letras (ex: RS).', 'warning')
        return redirect(next_url)

    if not cidade:
        flash('Cidade é obrigatória.', 'warning')
        return redirect(next_url)

    if inscricao and len(inscricao) > 6:
        flash('Inscrição municipal deve ter até 6 caracteres.', 'warning')
        return redirect(next_url)

    empresa.nome = nome
    empresa.estado = estado
    empresa.cidade = cidade
    empresa.inscricao_mobiliaria = inscricao if inscricao else None

    try:
        db.session.commit()
        flash('Empresa atualizada com sucesso.', 'success')
        auditoria.registrar('empresa.editar', alvo_tipo='empresa', alvo_id=empresa_id)
    except Exception as exc:
        db.session.rollback()
        flash(f'Erro ao atualizar empresa: {exc}', 'danger')
        auditoria.registrar('empresa.editar', alvo_tipo='empresa', alvo_id=empresa_id,
                            resultado='erro', detalhe=str(exc))

    return redirect(next_url)


@bp.route('/empresa/<int:empresa_id>/remover', methods=['GET', 'POST'])
@requer_papel('admin')
def empresa_remover(empresa_id):
    empresa = Empresa.query.get_or_404(empresa_id)
    next_url = request.values.get('next') or url_for('main.empresas')
    detalhe_url = url_for('main.empresa_detalhe', empresa_id=empresa_id)

    if request.method == 'GET':
        return render_template(
            'empresa_remover_confirm.html',
            empresa=empresa,
            next_url=next_url,
        )

    confirmacao = (request.form.get('confirm') or '').strip().lower()

    if next_url == detalhe_url:
        next_url = url_for('main.empresas')

    if confirmacao != '1':
        flash('Confirmação de remoção não recebida.', 'warning')
        return redirect(next_url)

    try:
        db.session.delete(empresa)
        db.session.commit()
        flash(f'Empresa "{empresa.nome}" removida com sucesso.', 'success')
        auditoria.registrar('empresa.remover', alvo_tipo='empresa', alvo_id=empresa_id)
    except Exception as exc:
        db.session.rollback()
        flash(f'Erro ao remover empresa: {exc}', 'danger')
        auditoria.registrar('empresa.remover', alvo_tipo='empresa', alvo_id=empresa_id,
                            resultado='erro', detalhe=str(exc))

    return redirect(next_url)


@bp.route('/empresa/<int:empresa_id>/abrir-pasta', methods=['POST'])
def abrir_pasta_empresa(empresa_id):
    """Abre a pasta CERTIDOES da empresa no Explorer da maquina local.

    O app roda localmente na estacao do operador (mesma maquina do Selenium e do
    drive de rede), entao os.startfile abre o Explorer para quem esta operando.
    Acao de leitura — qualquer papel logado."""
    empresa = Empresa.query.get_or_404(empresa_id)
    pasta_empresa = file_manager.encontrar_pasta_empresa(empresa.nome)
    if not pasta_empresa:
        return _json_error(
            f'Pasta da empresa "{empresa.nome}" nao encontrada na rede.', 404,
            error_type='network_path')
    pasta = file_manager.encontrar_caminho_final(pasta_empresa)
    if not pasta or not os.path.isdir(pasta):
        return _json_error('Pasta de certidoes nao encontrada.', 404, error_type='network_path')
    if not hasattr(os, 'startfile'):
        return _json_error('Abrir pasta so e suportado no Windows.', 400, error_type='plataforma')
    try:
        os.startfile(pasta)
    except OSError as e:
        return _json_error(f'Nao foi possivel abrir a pasta: {e}', 500)
    log_event('empresa_pasta_aberta', empresa_id=empresa_id, pasta=pasta)
    return jsonify({'status': 'ok', 'pasta': pasta})


_NOMES_EXIBICAO_CIDADE = {
    'Capao da Canoa': 'Capão da Canoa',
    'Imbe': 'Imbé',
    'Osorio': 'Osório',
    'Ponta Pora': 'Ponta Porã',
    'Sao Paulo': 'São Paulo',
    'Tramandai': 'Tramandaí',
    'Xangrila': 'Xangri-Lá',
}


@bp.route('/empresa/nova', endpoint='nova_empresa')
@requer_papel('operador')
def pagina_nova_empresa():
    municipios_db = Municipio.query.order_by(Municipio.nome).all()
    vistos = set()
    municipios = []
    for m in municipios_db:
        exibicao = _NOMES_EXIBICAO_CIDADE.get(m.nome, m.nome)
        if exibicao not in vistos:
            vistos.add(exibicao)
            municipios.append((m.nome, exibicao))
    return render_template('nova_empresa.html', municipios=municipios)


_LOTE_STATUS_LABEL = {
    'completed': 'Concluído',
    'stopped': 'Interrompido',
    'error': 'Erro',
    'paused': 'Pausado',
}


def _humanizar_desde(dt, agora):
    """Descreve há quanto tempo `dt` ocorreu em relação a `agora` (ambos naive,
    horário local). Retorna string curta pt-BR: "agora", "há 3 h", "ontem",
    "há 5 dias". Usada nos relatórios para datar atividade recente/pendências."""
    if dt is None:
        return '—'
    delta = agora - dt
    segundos = delta.total_seconds()
    if segundos < 0:
        return 'agora'
    dias = delta.days
    if dias == 0:
        horas = int(segundos // 3600)
        if horas < 1:
            minutos = int(segundos // 60)
            return 'agora' if minutos < 1 else f'há {minutos} min'
        return f'há {horas} h'
    if dias == 1:
        return 'ontem'
    if dias < 30:
        return f'há {dias} dias'
    meses = dias // 30
    return 'há 1 mês' if meses == 1 else f'há {meses} meses'


@bp.route('/relatorios')
def relatorios():
    hoje = date.today()
    agora = datetime.now()
    _garantir_snapshot_diario()
    a_vencer_dias = get_a_vencer_dias()
    empresas_total = Empresa.query.count()
    certidoes = Certidao.query.all()

    total_certidoes = len(certidoes)
    pendentes = 0
    vencidas = 0
    a_vencer = 0
    validas = 0
    sem_data = 0

    # distribuição por tipo (ordem canônica do enum) e agrupamentos de pendências
    por_tipo = {t.value: 0 for t in TipoCertidao}
    pendentes_por_tipo = {}
    pendentes_por_cidade = {}
    lista_pendentes = []
    ultimas_emitidas = []

    for certidao in certidoes:
        por_tipo[certidao.tipo.value] += 1
        st = _classificar_status_certidao(certidao, hoje)

        if st == 'pendentes':
            pendentes += 1
            tipo_valor = certidao.tipo.value
            pendentes_por_tipo[tipo_valor] = pendentes_por_tipo.get(tipo_valor, 0) + 1
            cidade = certidao.empresa.cidade or '—'
            pendentes_por_cidade[cidade] = pendentes_por_cidade.get(cidade, 0) + 1
            lista_pendentes.append({
                'empresa': certidao.empresa.nome,
                'cidade': certidao.empresa.cidade,
                'tipo': tipo_valor,
                'subtipo': certidao.subtipo.value if certidao.subtipo else None,
                'desde': _humanizar_desde(certidao.atualizado_em, agora),
                'ordem': certidao.atualizado_em or datetime.min,
            })
            continue

        if st == 'sem_data':
            sem_data += 1
            continue

        if st == 'vencidas':
            vencidas += 1
        elif st == 'a_vencer':
            a_vencer += 1
        else:
            validas += 1

        # certidão efetivamente emitida (tem validade): candidata a "últimas emitidas"
        ultimas_emitidas.append({
            'empresa': certidao.empresa.nome,
            'cidade': certidao.empresa.cidade,
            'tipo': certidao.tipo.value,
            'subtipo': certidao.subtipo.value if certidao.subtipo else None,
            'validade': certidao.data_validade,
            'quando': _humanizar_desde(certidao.atualizado_em, agora),
            'ordem': certidao.atualizado_em or datetime.min,
        })

    # ordena por atividade mais recente e corta o topo
    lista_pendentes.sort(key=lambda x: x['ordem'], reverse=True)
    ultimas_emitidas.sort(key=lambda x: x['ordem'], reverse=True)
    ultimas_emitidas = ultimas_emitidas[:10]

    # distribuição por tipo na ordem canônica do enum
    distribuicao_tipo = [(t.value, por_tipo[t.value]) for t in TipoCertidao]
    # rankings de pendências (maior primeiro)
    pendentes_tipo_rank = sorted(
        pendentes_por_tipo.items(), key=lambda x: x[1], reverse=True)
    pendentes_cidade_rank = sorted(
        pendentes_por_cidade.items(), key=lambda x: x[1], reverse=True)

    distribuicao_status = [
        ('Válidas', validas, 'ok'),
        ('A vencer', a_vencer, 'warn'),
        ('Vencidas', vencidas, 'danger'),
        ('Pendentes', pendentes, 'pend'),
        ('Sem data', sem_data, 'muted'),
    ]

    # último lote iniciado por tipo × escopo (pendentes / geral). iniciado_em é
    # gravado em UTC; comparo com utcnow p/ o "há X" e converto p/ local só ao exibir.
    agora_utc = utcnow_naive()
    tz_offset = agora - agora_utc
    lotes_resumo = []
    for nome_tipo in ('FGTS', 'Estadual RS', 'Municipal Imbé', 'Municipal Tramandaí'):
        linha = {'tipo': nome_tipo, 'pendentes': None, 'geral': None}
        for escopo, chave in (('pendentes', 'pendentes'), ('default', 'geral')):
            reg = (ExecucaoLote.query
                   .filter_by(tipo=nome_tipo, escopo=escopo)
                   .order_by(ExecucaoLote.iniciado_em.desc())
                   .first())
            if reg:
                tem_desfecho = reg.status is not None
                linha[chave] = {
                    'tipo': nome_tipo,
                    'escopo': escopo,
                    'quando': _humanizar_desde(reg.iniciado_em, agora_utc),
                    'data_fmt': (reg.iniciado_em + tz_offset).strftime('%d/%m/%Y %H:%M'),
                    'total': reg.total,
                    'tem_desfecho': tem_desfecho,
                    'status_label': _LOTE_STATUS_LABEL.get(reg.status, '—'),
                    'sucesso': reg.sucesso,
                    'pendentes_resultado': reg.pendentes_resultado,
                    'falhas': reg.falhas,
                    'processados': reg.sucesso + reg.pendentes_resultado + reg.falhas,
                    'finalizado_fmt': (
                        (reg.finalizado_em + tz_offset).strftime('%d/%m/%Y %H:%M')
                        if reg.finalizado_em else None),
                }
        lotes_resumo.append(linha)

    # série de evolução por status (foto diária), agregando os snapshots por dia
    snaps = SnapshotCertidao.query.order_by(SnapshotCertidao.data).all()
    por_data = {}
    for s in snaps:
        dia = por_data.setdefault(s.data, {})
        dia[s.status] = dia.get(s.status, 0) + s.quantidade
    serie_status = []
    for dia in sorted(por_data):
        linha_dia = por_data[dia]
        serie_status.append({
            'label': dia.strftime('%d/%m'),
            'validas': linha_dia.get('validas', 0),
            'a_vencer': linha_dia.get('a_vencer', 0),
            'vencidas': linha_dia.get('vencidas', 0),
            'pendentes': linha_dia.get('pendentes', 0),
            'sem_data': linha_dia.get('sem_data', 0),
        })

    return render_template(
        'relatorios.html',
        empresas_total=empresas_total,
        total_certidoes=total_certidoes,
        pendentes=pendentes,
        vencidas=vencidas,
        a_vencer=a_vencer,
        a_vencer_dias=a_vencer_dias,
        ultimas_emitidas=ultimas_emitidas,
        lista_pendentes=lista_pendentes,
        pendentes_tipo_rank=pendentes_tipo_rank,
        pendentes_cidade_rank=pendentes_cidade_rank,
        distribuicao_status=distribuicao_status,
        distribuicao_tipo=distribuicao_tipo,
        lotes_resumo=lotes_resumo,
        serie_status=serie_status,
    )


_TIPOS_VENCER = [
    ('federal', 'Federal', 'a_vencer_dias_federal'),
    ('fgts', 'FGTS', 'a_vencer_dias_fgts'),
    ('estadual', 'Estadual', 'a_vencer_dias_estadual'),
    ('municipal', 'Municipal', 'a_vencer_dias_municipal'),
    ('trabalhista', 'Trabalhista', 'a_vencer_dias_trabalhista'),
]


@bp.route('/configuracoes', methods=['GET', 'POST'])
@requer_papel('admin')
def configuracoes():
    try:
        config = db.session.get(ConfiguracaoSistema, 1)
    except Exception:
        config = None

    if not config:
        config = ConfiguracaoSistema(id=1, a_vencer_dias=7)
        db.session.add(config)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

    if request.method == 'POST':
        valor_str = (request.form.get('a_vencer_dias') or '').strip()
        try:
            valor = int(valor_str)
        except (TypeError, ValueError):
            flash('Informe um numero inteiro entre 1 e 90.', 'warning')
            return redirect(url_for('main.configuracoes'))

        if not 1 <= valor <= 90:
            flash('O limite de "a vencer" deve ficar entre 1 e 90 dias.', 'warning')
            return redirect(url_for('main.configuracoes'))

        config.a_vencer_dias = valor

        for chave, _label, coluna in _TIPOS_VENCER:
            raw = (request.form.get(f'a_vencer_dias_{chave}') or '').strip()
            if raw == '':
                setattr(config, coluna, None)
            else:
                try:
                    v = int(raw)
                except (TypeError, ValueError):
                    flash(f'Valor invalido para {_label}: use um numero inteiro entre 1 e 90.', 'warning')
                    return redirect(url_for('main.configuracoes'))
                if not 1 <= v <= 90:
                    flash(f'O limite para {_label} deve ficar entre 1 e 90 dias.', 'warning')
                    return redirect(url_for('main.configuracoes'))
                setattr(config, coluna, v)

        caminho_rede_raw = (request.form.get('caminho_rede') or '').strip()
        config.caminho_rede = caminho_rede_raw or None

        # Agendador de emissao proativa (spec 02): hora local 0-23 + liga/desliga.
        # So processa quando o formulario traz a secao do agendador (evita mexer
        # no estado num POST parcial que nao inclui esses campos).
        if 'agendador_hora' in request.form:
            hora_raw = (request.form.get('agendador_hora') or '').strip()
            try:
                hora = int(hora_raw)
            except (TypeError, ValueError):
                flash('Informe uma hora inteira entre 0 e 23 para o agendador.', 'warning')
                return redirect(url_for('main.configuracoes'))
            if not 0 <= hora <= 23:
                flash('O horario do agendador deve ficar entre 0 e 23.', 'warning')
                return redirect(url_for('main.configuracoes'))
            config.agendador_hora = hora
            config.agendador_ativo = _to_bool(request.form.get('agendador_ativo'))

        # Notificacoes por e-mail (spec 03): destinatarios + cadencia do digest.
        # So processa quando o formulario traz a secao (POST parcial nao mexe).
        if 'notif_cadencia' in request.form:
            cadencia = (request.form.get('notif_cadencia') or '').strip().lower()
            if cadencia not in ('semanal', 'diaria'):
                flash('Cadencia de notificacao invalida: use semanal ou diaria.', 'warning')
                return redirect(url_for('main.configuracoes'))
            config.notif_cadencia = cadencia
            destinatarios = (request.form.get('notif_destinatarios') or '').strip()
            config.notif_destinatarios = destinatarios or None

        salvou = False
        try:
            db.session.commit()
            salvou = True
            flash('Configuracoes atualizadas com sucesso.', 'success')
            auditoria.registrar('config.editar')
        except Exception as exc:
            db.session.rollback()
            flash(f'Erro ao salvar configuracoes: {exc}', 'danger')
            auditoria.registrar('config.editar', resultado='erro', detalhe=str(exc))

        # reprograma o scheduler sem reiniciar — best-effort e FORA do try do
        # commit: uma falha aqui não deve reportar "erro ao salvar" (já salvou).
        if salvou:
            try:
                agendador.reprogramar(_current_app_object())
            except Exception as exc:
                log_event('agendador_reprogramar_falhou', level='WARNING', error=str(exc))

        return redirect(url_for('main.configuracoes'))

    a_vencer_dias = config.a_vencer_dias if config else get_a_vencer_dias()
    por_tipo = {
        chave: getattr(config, coluna) if config else None
        for chave, _, coluna in _TIPOS_VENCER
    }
    return render_template(
        'configuracoes.html',
        a_vencer_dias=a_vencer_dias,
        por_tipo=por_tipo,
        tipos_vencer=_TIPOS_VENCER,
        caminho_rede=(config.caminho_rede if config else None) or '',
        caminho_rede_efetivo=file_manager.get_caminho_rede(),
        agendador_ativo=(config.agendador_ativo if config else True),
        agendador_hora=(config.agendador_hora if config else 3),
        notif_destinatarios=(config.notif_destinatarios if config else None) or '',
        notif_cadencia=(config.notif_cadencia if config else 'semanal'),
    )


@bp.route('/empresa/adicionar', methods=['POST'])
@requer_papel('operador')
def adicionar_empresa():
    # dados formulário
    nome = (request.form.get('nome') or '').strip()
    cnpj = (request.form.get('cnpj') or '').strip()
    estado = (request.form.get('estado') or '').strip().upper()
    cidade = (request.form.get('cidade') or '').strip()
    inscricao = (request.form.get('inscricao_mobiliaria') or '').strip()
    origem = (request.form.get('origem') or '').strip()

    def _redirect_apos_cadastro():
        if origem == 'nova_empresa':
            return redirect(url_for('main.nova_empresa'))
        return redirect(url_for('main.dashboard'))

    if not nome:
        flash('Nome da empresa é obrigatório.', 'warning')
        return _redirect_apos_cadastro()

    cnpj_limpo = _normalizar_cnpj(cnpj)
    if len(cnpj_limpo) != 14:
        flash('CNPJ inválido, verifique os dígitos.', 'warning')
        return _redirect_apos_cadastro()

    if not estado or not re.match(r'^[A-Z]{2}$', estado):
        flash('Estado inválido. Use a sigla com 2 letras (ex: RS).', 'warning')
        return _redirect_apos_cadastro()

    if not cidade:
        flash('Cidade é obrigatória.', 'warning')
        return _redirect_apos_cadastro()

    if inscricao and len(inscricao) > 6:
        flash('Inscrição municipal deve ter até 6 caracteres.', 'warning')
        return _redirect_apos_cadastro()

    cnpj_formatado = _formatar_cnpj(cnpj_limpo) or cnpj
    cnpj = cnpj_formatado

    # validacao
    cnpj_variantes = {cnpj}
    if cnpj_limpo:
        cnpj_variantes.add(cnpj_limpo)
    if cnpj_formatado:
        cnpj_variantes.add(cnpj_formatado)

    empresa_existente = Empresa.query.filter(Empresa.cnpj.in_(cnpj_variantes)).first()
    if empresa_existente:
        flash(f'Empresa com CNPJ {cnpj} já está cadastrada.', 'warning')
        return _redirect_apos_cadastro()

    # Cria objeto empresa
    empresa_nova = Empresa(
        nome=nome,
        cnpj=cnpj,
        estado=estado,
        cidade=cidade,
        # Garante que seja nulo se vazio
        inscricao_mobiliaria=inscricao if inscricao else None
    )
    db.session.add(empresa_nova)

    cidade_norm = file_manager.remover_acentos(cidade or '').upper()
    is_imbe = cidade_norm == 'IMBE'

    for tipo in TipoCertidao:
        if tipo == TipoCertidao.MUNICIPAL:
            if is_imbe:
                db.session.add(Certidao(
                    tipo=tipo,
                    subtipo=SubtipoCertidao.GERAL,
                    empresa=empresa_nova,
                    data_validade=None
                ))
                db.session.add(Certidao(
                    tipo=tipo,
                    subtipo=SubtipoCertidao.MOBILIARIO,
                    empresa=empresa_nova,
                    data_validade=None
                ))
            else:
                db.session.add(Certidao(
                    tipo=tipo,
                    empresa=empresa_nova,
                    data_validade=None
                ))
            continue

        db.session.add(Certidao(
            tipo=tipo,
            empresa=empresa_nova,
            data_validade=None
        ))

    # Salva no banco
    try:
        db.session.commit()
        flash(f'Empresa "{nome}" cadastrada com sucesso!', 'success')
        auditoria.registrar('empresa.criar', alvo_tipo='empresa', alvo_id=empresa_nova.id)
    except Exception as e:
        db.session.rollback()
        flash(f'Erro ao cadastrar empresa: {e}', 'danger')
        auditoria.registrar('empresa.criar', resultado='erro', detalhe=str(e))

    return _redirect_apos_cadastro()


@bp.route('/certidao/atualizar/<int:certidao_id>', methods=['POST'])
@requer_papel('operador')
def atualizar_validade(certidao_id):
    certidao = Certidao.query.get_or_404(certidao_id)
    nova_data_str = request.form.get('nova_validade')

    if nova_data_str:
        nova_data = datetime.strptime(nova_data_str, '%Y-%m-%d').date()
        ok, erro = certidao_service.aplicar_validade(certidao, nova_data)
        if ok:
            flash(
                f"Validade da certidão {certidao.tipo.value} da empresa {certidao.empresa.nome} atualizada com sucesso!", 'success')
            auditoria.registrar('certidao.aplicar_validade', alvo_tipo='certidao', alvo_id=certidao_id)
        else:
            flash(f"Erro ao atualizar validade: {erro}", 'danger')
            auditoria.registrar('certidao.aplicar_validade', alvo_tipo='certidao',
                                alvo_id=certidao_id, resultado='erro', detalhe=erro)
    else:
        flash("Nenhuma data foi fornecida.", 'warning')
    return redirect(url_for('main.dashboard'))


@bp.route('/certidao/marcar_pendente/<int:certidao_id>', methods=['POST'])
@requer_papel('operador')
def marcar_pendente(certidao_id):
    certidao = Certidao.query.get_or_404(certidao_id)
    ok, erro = certidao_service.marcar_pendente(certidao)
    if ok:
        flash(
            f'Certidão {certidao.tipo.value} da empresa {certidao.empresa.nome} marcada como Pendente.', 'info')
        auditoria.registrar('certidao.marcar_pendente', alvo_tipo='certidao', alvo_id=certidao_id)
    else:
        flash(f'Erro ao marcar como pendente: {erro}', 'danger')
        auditoria.registrar('certidao.marcar_pendente', alvo_tipo='certidao',
                            alvo_id=certidao_id, resultado='erro', detalhe=erro)

    return redirect(url_for('main.dashboard'))

def _calcular_validade_sem_data(certidao, tipo_chave, regra):
    if tipo_chave == 'MUNICIPAL':
        if regra and regra.validade_dias:
            return date.today() + timedelta(days=regra.validade_dias)
        return None
    return calcular_validade_padrao(certidao, None)


def _lote_bloqueia_emissao(lock, state, mensagem):
    """Retorna erro JSON 400 se o lote (lock/state) estiver em andamento; senão None."""
    with lock:
        if state['status'] in ['running', 'paused']:
            return _json_error(mensagem, 400)
    return None


def _validar_baixar(certidao):
    """Validacoes que decidem cedo o fluxo de baixar_certidao.

    Retorna uma resposta Flask (erro JSON ou redirect) para encerrar, ou None
    para seguir com a automacao.
    """
    tipo_certidao_chave = certidao.tipo.name
    estado = (certidao.empresa.estado or '').strip().upper()

    if tipo_certidao_chave == 'ESTADUAL' and estado == 'RS':
        erro = _lote_bloqueia_emissao(
            RS_BATCH_LOCK, RS_BATCH_STATE,
            'Lote Estadual RS em andamento. Aguarde finalizar ou interrompa o lote.',
        )
        if erro is not None:
            return erro

    if tipo_certidao_chave == 'FGTS':
        erro = _lote_bloqueia_emissao(
            FGTS_BATCH_LOCK, FGTS_BATCH_STATE,
            'Lote FGTS em andamento. Aguarde finalizar ou interrompa o lote.',
        )
        if erro is not None:
            return erro

    if tipo_certidao_chave == 'MUNICIPAL':
        erro = _lote_bloqueia_emissao(
            MUNICIPAL_BATCH_LOCK, MUNICIPAL_BATCH_STATE,
            'Lote Municipal em andamento. Aguarde finalizar ou interrompa o lote.',
        )
        if erro is not None:
            return erro

    if tipo_certidao_chave == 'FEDERAL':
        return redirect("https://servicos.receitafederal.gov.br/servico/certidoes/#/home/cnpj")

    return None


def _montar_config_baixar(certidao):
    """Monta a configuracao de automacao por tipo (info_site, regra municipal,
    cnpj/inscricao, nome do arquivo, flags). Retorna (cfg, erro_response):
    cfg=None + resposta de erro quando a precondicao falha.
    """
    tipo_certidao_chave = certidao.tipo.name

    imbe_tipo = (request.args.get('imbe_tipo') or '').strip().lower()
    if not imbe_tipo and certidao.subtipo:
        imbe_tipo = _resolve_imbe_tipo_from_subtipo(certidao.subtipo)

    regra_municipio = None
    config_municipal = None
    usar_config_municipal = False
    info_site = {}

    if tipo_certidao_chave != 'MUNICIPAL':
        if tipo_certidao_chave == 'ESTADUAL':
            estado_emp_local = (certidao.empresa.estado or '').strip().upper()
            estadual_cfg = SITES_CERTIDOES.get('ESTADUAL', {})
            if isinstance(estadual_cfg, dict) and estado_emp_local in estadual_cfg:
                info_site = estadual_cfg[estado_emp_local].copy()
            else:
                info_site = SITES_CERTIDOES.get('ESTADUAL', {}).copy()
        else:
            info_site = SITES_CERTIDOES.get(tipo_certidao_chave, {}).copy()
    else:
        cidade_empresa = certidao.empresa.cidade or ''
        regra_municipio = _buscar_municipio_por_cidade(cidade_empresa)

        if regra_municipio:
            info_site = {
                'url': regra_municipio.url_certidao,
                'cnpj_field_id': regra_municipio.cnpj_field_id,
                'by': regra_municipio.by,
                'pre_fill_click_id': regra_municipio.pre_fill_click_id,
                'pre_fill_click_by': regra_municipio.pre_fill_click_by,
                'inscricao_field_id': regra_municipio.inscricao_field_id,
                'inscricao_field_by': regra_municipio.inscricao_field_by
            }
            if regra_municipio.usar_slow_typing:
                info_site['slow_typing'] = True

            if regra_municipio.automacao_ativa is False:
                return None, _json_error(
                    "Automação desativada para este município. Use o botão 'Abrir Site'.",
                    409,
                    status='manual_required',
                )

            config_municipal = _carregar_config_municipio(regra_municipio)
            usar_config_municipal = bool(config_municipal)

            cidade_regra_norm = file_manager.remover_acentos(regra_municipio.nome or '').upper()
            if cidade_regra_norm == 'IMBE':
                if imbe_tipo not in ['mobiliario', 'geral']:
                    return None, _json_error(
                        'Para Imbé, selecione no modal: Certidão Municipal Mobiliário ou Geral.',
                        409,
                        status='manual_required',
                    )

                _aplicar_variantes_imbe(info_site, config_municipal, imbe_tipo)

            if usar_config_municipal and config_municipal.get('skip_cnpj_fill'):
                info_site['cnpj_field_id'] = None

        else:
            return None, _json_error('Regra municipal não encontrada', 404)

    if tipo_certidao_chave == 'MUNICIPAL' and not usar_config_municipal:
        return None, _json_error('Municipio sem automacao. Configure para prosseguir.', 409)

    cnpj_limpo = _normalizar_cnpj(certidao.empresa.cnpj)
    inscricao_limpa = certidao.empresa.inscricao_mobiliaria or ''

    nome_certidao_arquivo = certidao.tipo.value
    if tipo_certidao_chave == 'MUNICIPAL' and regra_municipio:
        cidade_regra_norm = file_manager.remover_acentos(regra_municipio.nome or '').upper()
        if cidade_regra_norm == 'IMBE':
            nome_certidao_arquivo = _nome_certidao_imbe(nome_certidao_arquivo, imbe_tipo)

    estado_emp = (certidao.empresa.estado or '').strip().upper()
    usar_rs_autoselect = (
        tipo_certidao_chave == 'ESTADUAL'
        and estado_emp == 'RS'
        and bool(info_site.get('login_cert_url'))
    )

    cfg = {
        'tipo_certidao_chave': tipo_certidao_chave,
        'estado_emp': estado_emp,
        'imbe_tipo': imbe_tipo,
        'info_site': info_site,
        'regra_municipio': regra_municipio,
        'config_municipal': config_municipal,
        'usar_config_municipal': usar_config_municipal,
        'cnpj_limpo': cnpj_limpo,
        'inscricao_limpa': inscricao_limpa,
        'nome_certidao_arquivo': nome_certidao_arquivo,
        'usar_rs_autoselect': usar_rs_autoselect,
    }
    return cfg, None


def _resultado_baixar_vazio():
    return {
        'window_closed': False,
        'erro_500': None,
        'erro_acionavel': None,
        'arquivo_salvo_msg': None,
        'data_encontrada': None,
        'rs_estadual_classificacao': None,
        'rs_estadual_msg': None,
        'municipal_pdf_classificacao': None,
        'municipal_pdf_msg': None,
        'certidao_pdf_classificacao': None,
        'certidao_pdf_msg': None,
    }


def _baixar_classificacao_vazia():
    """Dict de classificacao de PDF com todos os campos None."""
    return {
        'rs_estadual_classificacao': None,
        'rs_estadual_msg': None,
        'municipal_pdf_classificacao': None,
        'municipal_pdf_msg': None,
        'certidao_pdf_classificacao': None,
        'certidao_pdf_msg': None,
    }


def _baixar_classificar_pdf(certidao, cfg, caminho_arquivo):
    """Classifica o PDF recem-salvo conforme o tipo da certidao e trata
    positivas (marca PENDENTE via pdf.classificar_e_tratar_positivo).
    Retorna um dict com as classificacoes/mensagens (campos None quando nao se aplica)."""
    tipo_certidao_chave = cfg['tipo_certidao_chave']
    estado_emp = cfg['estado_emp']
    regra_municipio = cfg['regra_municipio']
    config_municipal = cfg['config_municipal']
    usar_config_municipal = cfg['usar_config_municipal']

    classif = _baixar_classificacao_vazia()

    if tipo_certidao_chave == 'ESTADUAL' and estado_emp == 'RS':
        classif['rs_estadual_classificacao'], classif['rs_estadual_msg'] = pdf.classificar_e_tratar_positivo(
            certidao, caminho_arquivo, origem_log='ESTADUAL-RS', tipo_label='ESTADUAL RS'
        )
        log_event(
            'estadual_rs_pdf_classified', certidao_id=certidao.id,
            classificacao=classif['rs_estadual_classificacao'],
        )

    if (
        tipo_certidao_chave == 'MUNICIPAL'
        and regra_municipio
        and usar_config_municipal
        and bool((config_municipal or {}).get('classificar_pdf_status'))
    ):
        origem_pdf = f"MUNICIPAL-{regra_municipio.nome}"
        classif['municipal_pdf_classificacao'], classif['municipal_pdf_msg'] = pdf.classificar_e_tratar_positivo(
            certidao, caminho_arquivo, origem_log=origem_pdf,
            tipo_label=f'MUNICIPAL ({regra_municipio.nome})'
        )
        log_event(
            'municipal_pdf_classified', certidao_id=certidao.id,
            municipio=regra_municipio.nome,
            classificacao=classif['municipal_pdf_classificacao'],
        )

    if (
        tipo_certidao_chave not in {'MUNICIPAL', 'FEDERAL'}
        and not (tipo_certidao_chave == 'ESTADUAL' and estado_emp == 'RS')
    ):
        origem_pdf = (
            f"ESTADUAL-{estado_emp}"
            if tipo_certidao_chave == 'ESTADUAL' and estado_emp
            else tipo_certidao_chave
        )
        classificacao_pdf, msg_pdf = pdf.classificar_e_tratar_positivo(
            certidao, caminho_arquivo, origem_log=origem_pdf, tipo_label=certidao.tipo.value,
        )
        if classificacao_pdf in {'positiva', 'erro'}:
            classif['certidao_pdf_classificacao'] = classificacao_pdf
            classif['certidao_pdf_msg'] = msg_pdf

    return classif


def _baixar_fechar_navegador(driver, certidao):
    """Fecha aba extra (se houver) e encerra o Chrome apos salvar o arquivo."""
    try:
        janelas_abertas = list(driver.window_handles)
    except Exception:
        janelas_abertas = []

    if len(janelas_abertas) > 1:
        ultima = janelas_abertas[-1]
        try:
            driver.switch_to.window(ultima)
            driver.close()
        except Exception:
            pass

    time.sleep(1)
    try:
        driver.quit()
    except Exception as e_quit:
        log_event(
            'emit_chrome_close_warning', level='WARNING',
            certidao_id=certidao.id, error=str(e_quit),
        )


def _baixar_monitorar_download(driver, certidao, cfg, tempo_inicio, arquivo_salvo_msg=None):
    """Aguarda o download, move/renomeia o arquivo, classifica o PDF e fecha o
    navegador. Retorna (arquivo_salvo_msg, classif_dict)."""
    nome_certidao_arquivo = cfg['nome_certidao_arquivo']
    classif = _baixar_classificacao_vazia()

    log_event('emit_waiting_download', certidao_id=certidao.id)
    download_detectado = False

    while True:
        try:
            driver.window_handles
        except Exception:
            log_event('emit_window_closed', level='WARNING', certidao_id=certidao.id)
            break

        if not download_detectado:
            novo_arquivo = file_manager.verificar_novo_arquivo(tempo_inicio)

            if novo_arquivo:
                log_event('emit_file_detected', certidao_id=certidao.id, arquivo=str(novo_arquivo))
                download_detectado = True
                sucesso, msg = file_manager.mover_e_renomear(
                    novo_arquivo,
                    certidao.empresa.nome,
                    nome_certidao_arquivo
                )

                if sucesso:
                    arquivo_salvo_msg = f"Arquivo salvo em: {msg}"
                    log_event('emit_file_saved', certidao_id=certidao.id, caminho=str(msg))
                    try:
                        certidao.caminho_arquivo = msg
                        db.session.commit()
                    except Exception as e_db:
                        db.session.rollback()
                        log_event(
                            'emit_db_save_failed', level='WARNING',
                            certidao_id=certidao.id, error=str(e_db),
                        )

                    classif = _baixar_classificar_pdf(certidao, cfg, msg)

                    try:
                        _baixar_fechar_navegador(driver, certidao)
                        break
                    except Exception as e:
                        log_event(
                            'emit_chrome_close_error', level='WARNING',
                            certidao_id=certidao.id, error=str(e),
                        )
                else:
                    log_event(
                        'emit_file_save_error', level='ERROR',
                        certidao_id=certidao.id, error=str(msg),
                    )

        time.sleep(1)

    return arquivo_salvo_msg, classif


def _baixar_executar_acao(nome_acao, info_site, wait, driver, certidao, contexto):
    """Executa uma acao pre-cnpj do fluxo de emissao: pre-click inicial,
    select de tipo ou emissao FGTS via CDP. Dispatch por nome_acao."""
    # 1 pre click inicial
    if nome_acao == 'pre_fill':
        if not info_site.get('pre_fill_click_id'):
            return
        click_by = steps.BY_MAP.get(info_site.get('pre_fill_click_by'))
        if not click_by:
            return
        try:
            elemento_inicial = wait.until(
                EC.element_to_be_clickable(
                    (click_by, info_site['pre_fill_click_id'])
                )
            )
            elemento_inicial.click()
            time.sleep(2)
        except Exception:
            pass

    # 2 select de tipo
    elif nome_acao == 'select_tipo':
        if not info_site.get('tipo_select_id'):
            return
        select_by = steps.BY_MAP.get(info_site.get('tipo_select_by', 'id')) or By.ID
        try:
            select_el = wait.until(
                EC.element_to_be_clickable(
                    (select_by, info_site['tipo_select_id']))
            )
            select_obj = Select(select_el)

            value = info_site.get('tipo_select_value')
            if value is not None:
                select_obj.select_by_value(value)
            else:
                text = info_site.get('tipo_select_text')
                if text:
                    select_obj.select_by_visible_text(text)

            time.sleep(1)
            log_event('emit_select_tipo_ok', certidao_id=certidao.id)
        except Exception as e:
            log_event(
                'emit_select_tipo_failed', level='WARNING',
                certidao_id=certidao.id, error=str(e),
            )

    #3 ação específica para FGTS: emitir e salvar PDF
    elif nome_acao == 'fgts_emitir_pdf':
        try:
            _automatizar_fgts(contexto, driver, wait, certidao)
        except Exception as e:
            log_event(
                'fgts_emitir_pdf_error', level='ERROR',
                certidao_id=certidao.id, error=str(e),
            )


def _abrir_driver_baixar(cfg, certidao, resultado):
    """Escolhe e cria o WebDriver da emissao individual.

    Municipal IPM Atende.Net -> undetected-chromedriver com perfil dedicado
    (serializado por lock); demais tipos/municipios -> _criar_driver_chrome
    atual. Em falha de pre-condicao (perfil ocupado / uc indisponivel)
    preenche resultado['erro_acionavel'] e retorna (None, False) — fail-fast,
    sem fallback para incognito. Retorna (driver, lock_municipal_ativo)."""
    tipo_certidao_chave = cfg['tipo_certidao_chave']
    info_site = cfg['info_site']
    usar_rs_autoselect = cfg['usar_rs_autoselect']

    if tipo_certidao_chave == 'MUNICIPAL' and is_ipm_atende(info_site.get('url')):
        if not _municipal_profile_acquire(blocking=False):
            resultado['erro_acionavel'] = {
                'message': 'Perfil municipal em uso: aguarde a emissao atual terminar e tente novamente.',
                'error_type': ErrorType.PORTAL.value,
                'acao': 'Aguarde a emissao municipal em andamento concluir antes de iniciar outra.',
                'code': 409,
            }
            return None, False
        try:
            driver = _criar_driver_uc()
        except UcIndisponivelError as exc:
            log_event('uc_indisponivel', level='ERROR', certidao_id=certidao.id, error=str(exc))
            _municipal_profile_release()
            resultado['erro_acionavel'] = {
                'message': exc.message,
                'error_type': ErrorType.PORTAL.value,
                'acao': exc.acao or 'Verifique a instalacao do undetected-chromedriver e a versao do Chrome.',
                'code': 409,
            }
            return None, False
        return driver, True

    driver = _criar_driver_chrome(
        anonimo=not usar_rs_autoselect,
        usar_perfil=usar_rs_autoselect,
    )
    return driver, False


def _executar_automacao_baixar(certidao, cfg):
    """Camada Selenium da emissao unitaria. Recebe a config ja montada e
    devolve um dict 'resultado' com os flags de classificacao/arquivo (ou os
    sinais terminais window_closed / erro_500). Nao monta resposta HTTP."""
    tipo_certidao_chave = cfg['tipo_certidao_chave']
    estado_emp = cfg['estado_emp']
    info_site = cfg['info_site']
    config_municipal = cfg['config_municipal']
    usar_config_municipal = cfg['usar_config_municipal']
    cnpj_limpo = cfg['cnpj_limpo']
    inscricao_limpa = cfg['inscricao_limpa']
    usar_rs_autoselect = cfg['usar_rs_autoselect']

    by_map = steps.BY_MAP

    def _get_by(key):
        return by_map.get(key)

    resultado = _resultado_baixar_vazio()

    driver = None
    data_encontrada = None
    arquivo_salvo_msg = None
    pular_monitoramento = False
    rs_autoselect_temporario_ativo = False
    municipal_profile_lock_ativo = False
    rs_estadual_classificacao = None
    rs_estadual_msg = None
    municipal_pdf_classificacao = None
    municipal_pdf_msg = None
    certidao_pdf_classificacao = None
    certidao_pdf_msg = None

    # contexto compartilhado com helpers de steps
    contexto = {
        'arquivo_salvo_msg': None,
        'pular_monitoramento': False,
        'data_encontrada': None
    }

    tempo_inicio = time.time()

    try:
        log_event('emit_automation_start', certidao_id=certidao.id, tipo=tipo_certidao_chave)

        if usar_rs_autoselect:
            rs_autoselect_temporario_ativo = _ativar_politica_autoselect_rs_temporaria()

        driver, municipal_profile_lock_ativo = _abrir_driver_baixar(cfg, certidao, resultado)
        if driver is None:
            return resultado

        wait = WebDriverWait(driver, 20)

        if tipo_certidao_chave == 'ESTADUAL' and estado_emp == 'RS' and info_site.get('login_cert_url'):
            log_event('estadual_rs_cert_login', certidao_id=certidao.id)
            _login_certificado_rs(
                driver,
                info_site.get('login_cert_url'),
                info_site.get('url')
            )
        else:
            log_event('emit_navigate', certidao_id=certidao.id, url=info_site.get('url'))
            driver.get(info_site.get('url'))

        try:
            _configurar_download_automatico_chrome(driver)
        except Exception as exc:
            log_event(
                'download_config_retry_failed', level='WARNING',
                certidao_id=certidao.id, error=str(exc),
            )

        if tipo_certidao_chave == 'MUNICIPAL':
            if usar_config_municipal:
                steps_before = config_municipal.get('before_cnpj', []) if config_municipal else []
                resultado_steps = steps.executar_municipio(
                    driver,
                    wait,
                    steps_before,
                    cnpj_limpo,
                    inscricao_limpa,
                    etapa_label='before_cnpj'
                )
                if resultado_steps and resultado_steps.get('encerrar_sem_arquivo'):
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    resultado['window_closed'] = True
                    return resultado

        # ordem das ações antes do cnpj
        steps_before_cnpj = info_site.get('steps_before_cnpj')
        if steps_before_cnpj is None:
            # padrão atual: pre_fill depois select_tipo
            steps_before_cnpj = ['pre_fill', 'select_tipo']

        for step in steps_before_cnpj:
            _baixar_executar_acao(step, info_site, wait, driver, certidao, contexto)

        if info_site.get('cnpj_field_id'):
            field_by = _get_by(info_site.get('by'))
            if field_by:
                try:
                    campo1 = wait.until(EC.element_to_be_clickable(
                        (field_by, info_site['cnpj_field_id'])))
                    if info_site.get('slow_typing'):
                        campo1.clear()
                        apenas_numeros = _normalizar_cnpj(cnpj_limpo)
                        campo1.click()
                        for digito in apenas_numeros:
                            campo1.send_keys(digito)
                            time.sleep(0.1)
                    else:
                        campo1.click()
                        dado_a_preencher = inscricao_limpa if info_site.get(
                            'cnpj_field_id') == 'inscricao' else cnpj_limpo
                        campo1.send_keys(dado_a_preencher)

                    if tipo_certidao_chave == 'TRABALHISTA':
                        campo1.send_keys(Keys.TAB)
                except Exception:
                    pass

        if tipo_certidao_chave == 'ESTADUAL' and estado_emp == 'RS':
            log_event(
                'estadual_rs_manual_hint', certidao_id=certidao.id,
                message='Emissão unitária em modo manual: resolva o captcha e clique em Enviar.',
            )

        if tipo_certidao_chave == 'MUNICIPAL' and usar_config_municipal:
            steps_after = config_municipal.get('after_cnpj', []) if config_municipal else []
            resultado_steps = steps.executar_municipio(
                driver,
                wait,
                steps_after,
                cnpj_limpo,
                inscricao_limpa,
                etapa_label='after_cnpj'
            )
            if resultado_steps and resultado_steps.get('encerrar_sem_arquivo'):
                try:
                    driver.quit()
                except Exception:
                    pass
                resultado['window_closed'] = True
                return resultado

        # ordem das ações depois do cnpj
        steps_after_cnpj = info_site.get('steps_after_cnpj')
        if steps_after_cnpj is None:
            steps_after_cnpj = []
        for step in steps_after_cnpj:
            _baixar_executar_acao(step, info_site, wait, driver, certidao, contexto)

        # sincroniza variaves ja usadas
        if contexto.get('pular_monitoramento'):
            pular_monitoramento = True
        if contexto.get('arquivo_salvo_msg'):
            arquivo_salvo_msg = contexto['arquivo_salvo_msg']
        if contexto.get('data_encontrada'):
            data_encontrada = contexto['data_encontrada']

        if info_site.get('inscricao_field_id'):
            field_by = _get_by(info_site.get('inscricao_field_by'))
            if field_by:
                try:
                    campo2 = wait.until(EC.element_to_be_clickable(
                        (field_by, info_site['inscricao_field_id'])))
                    campo2.click()
                    campo2.send_keys(inscricao_limpa)
                    campo2.send_keys(Keys.TAB)
                except Exception:
                    pass

        if not pular_monitoramento:
            arquivo_salvo_msg, classif = _baixar_monitorar_download(
                driver, certidao, cfg, tempo_inicio, arquivo_salvo_msg
            )
            rs_estadual_classificacao = classif['rs_estadual_classificacao']
            rs_estadual_msg = classif['rs_estadual_msg']
            municipal_pdf_classificacao = classif['municipal_pdf_classificacao']
            municipal_pdf_msg = classif['municipal_pdf_msg']
            certidao_pdf_classificacao = classif['certidao_pdf_classificacao']
            certidao_pdf_msg = classif['certidao_pdf_msg']
        else:
            log_event('fgts_monitor_skipped', certidao_id=certidao.id)
            if driver:
                try:
                    time.sleep(1)
                    driver.quit()
                except Exception as e_quit:
                    log_event(
                        'emit_chrome_close_warning', level='WARNING',
                        certidao_id=certidao.id, error=str(e_quit),
                    )

    except Exception as e:
        log_event('emit_selenium_error', level='ERROR', certidao_id=certidao.id, error=str(e))
        if _erro_indica_navegador_fechado(e):
            log_event(
                'emit_browser_closed', level='WARNING', certidao_id=certidao.id,
                message='Chrome fechado durante a automação; retornando fluxo pendente.',
            )
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            resultado['window_closed'] = True
            return resultado
        capture.capturar_contexto_falha(
            driver, f'baixar_{tipo_certidao_chave}', certidao_id=certidao.id,
        )
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        resultado['erro_500'] = "Ocorreu um erro na automação."
        return resultado
    finally:
        if rs_autoselect_temporario_ativo:
            _desativar_politica_autoselect_rs_temporaria()
        if municipal_profile_lock_ativo:
            _municipal_profile_release()

    resultado['arquivo_salvo_msg'] = arquivo_salvo_msg
    resultado['data_encontrada'] = data_encontrada
    resultado['rs_estadual_classificacao'] = rs_estadual_classificacao
    resultado['rs_estadual_msg'] = rs_estadual_msg
    resultado['municipal_pdf_classificacao'] = municipal_pdf_classificacao
    resultado['municipal_pdf_msg'] = municipal_pdf_msg
    resultado['certidao_pdf_classificacao'] = certidao_pdf_classificacao
    resultado['certidao_pdf_msg'] = certidao_pdf_msg
    return resultado


def _montar_resposta_baixar(certidao, cfg, resultado):
    """Monta a resposta HTTP final a partir do resultado da automacao.
    Logica pura (sem Selenium): espelha o contrato JSON original."""
    certidao_id = certidao.id
    tipo_certidao_chave = cfg['tipo_certidao_chave']
    regra_municipio = cfg['regra_municipio']
    nome_certidao_arquivo = cfg['nome_certidao_arquivo']

    erro_acionavel = resultado.get('erro_acionavel')
    if erro_acionavel:
        return _json_error(
            erro_acionavel['message'],
            erro_acionavel.get('code', 409),
            error_type=erro_acionavel.get('error_type'),
            acao=erro_acionavel.get('acao'),
        )

    if resultado.get('erro_500'):
        return _json_error(resultado['erro_500'], 500)

    if resultado.get('window_closed'):
        return jsonify({
            'status': 'window_closed_no_file',
            'certidao_id': certidao_id,
            'tipo_certidao': nome_certidao_arquivo
        })

    rs_estadual_classificacao = resultado['rs_estadual_classificacao']
    rs_estadual_msg = resultado['rs_estadual_msg']
    municipal_pdf_classificacao = resultado['municipal_pdf_classificacao']
    municipal_pdf_msg = resultado['municipal_pdf_msg']
    certidao_pdf_classificacao = resultado['certidao_pdf_classificacao']
    certidao_pdf_msg = resultado['certidao_pdf_msg']
    arquivo_salvo_msg = resultado['arquivo_salvo_msg']
    data_encontrada = resultado['data_encontrada']

    response_data = {'status': 'unknown'}

    if rs_estadual_classificacao == 'positiva':
        response_data['status'] = 'estadual_rs_positiva'
        response_data['message'] = rs_estadual_msg or 'Certidão ESTADUAL RS detectada como POSITIVA e marcada como PENDENTE.'
        response_data['certidao_id'] = certidao_id
        response_data['tipo_certidao'] = nome_certidao_arquivo
        return jsonify(response_data)

    if rs_estadual_classificacao == 'erro':
        return _json_error(rs_estadual_msg or 'Erro ao tratar certidão positiva do RS.', 500)

    if municipal_pdf_classificacao == 'positiva':
        response_data['status'] = 'municipal_pdf_positiva'
        response_data['message'] = municipal_pdf_msg or 'Certidão MUNICIPAL detectada como POSITIVA e marcada como PENDENTE.'
        response_data['certidao_id'] = certidao_id
        response_data['tipo_certidao'] = nome_certidao_arquivo
        return jsonify(response_data)

    if municipal_pdf_classificacao == 'erro':
        return _json_error(municipal_pdf_msg or 'Erro ao tratar certidão municipal positiva.', 500)

    if certidao_pdf_classificacao == 'positiva':
        response_data['status'] = 'certidao_pdf_positiva'
        response_data['message'] = certidao_pdf_msg or 'Certidão POSITIVA detectada e marcada como PENDENTE.'
        response_data['certidao_id'] = certidao_id
        response_data['tipo_certidao'] = nome_certidao_arquivo
        return jsonify(response_data)

    if certidao_pdf_classificacao == 'erro':
        return _json_error(certidao_pdf_msg or 'Erro ao tratar certidão positiva.', 500)

    if arquivo_salvo_msg:
        response_data['status'] = 'success_file_saved'
        response_data['mensagem_arquivo'] = arquivo_salvo_msg
        response_data['certidao_id'] = certidao_id
        response_data['tipo_certidao'] = nome_certidao_arquivo
        response_data['visualizar_token'] = _gerar_visualizar_token(certidao_id)

        if data_encontrada:
            log_event(
                'emit_validade_encontrada', certidao_id=certidao_id,
                validade=data_encontrada.strftime('%d/%m/%Y'),
            )
            response_data['nova_data'] = data_encontrada.strftime('%Y-%m-%d')
            response_data['data_formatada'] = data_encontrada.strftime(
                '%d/%m/%Y')
        else:
            data_calc = _calcular_validade_sem_data(certidao, tipo_certidao_chave, regra_municipio)

            if data_calc:
                log_event(
                    'emit_validade_calculada', certidao_id=certidao_id,
                    validade=data_calc.strftime('%d/%m/%Y'),
                )
                response_data['nova_data'] = data_calc.strftime('%Y-%m-%d')
                response_data['data_formatada'] = data_calc.strftime(
                    '%d/%m/%Y')
            else:
                response_data['status'] = 'success_file_saved_no_date'

    else:
        response_data['status'] = 'window_closed_no_file'
        response_data['certidao_id'] = certidao_id
        response_data['tipo_certidao'] = nome_certidao_arquivo

    return jsonify(response_data)


@bp.route('/certidao/baixar/<int:certidao_id>')
@requer_papel('operador')
def baixar_certidao(certidao_id):
    file_manager.criar_chave_interrupcao()
    certidao = Certidao.query.get_or_404(certidao_id)

    erro_ou_redirect = _validar_baixar(certidao)
    if erro_ou_redirect is not None:
        return erro_ou_redirect

    cfg, erro = _montar_config_baixar(certidao)
    if erro is not None:
        return erro

    marcar_emissao_individual(True)
    try:
        resultado = _executar_automacao_baixar(certidao, cfg)
    finally:
        marcar_emissao_individual(False)

    return _montar_resposta_baixar(certidao, cfg, resultado)


@bp.route('/certidao/salvar_data_confirmada', methods=['POST'])
@requer_papel('operador')
def salvar_data_confirmada():
    dados = request.get_json()
    certidao_id = dados.get('certidao_id')
    nova_validade_str = dados.get('nova_validade')

    try:
        certidao = db.session.get(Certidao, certidao_id)
        nova_data = datetime.strptime(nova_validade_str, '%Y-%m-%d').date()

        ok, erro = certidao_service.aplicar_validade(certidao, nova_data)
        if not ok:
            return _json_error(erro, 500)

        return jsonify({
            'status': 'success',
            'message': 'Data confirmada e atualizada com sucesso!',
            'nova_data_formatada': nova_data.strftime('%d/%m/%Y'),
            'nova_classe': _classe_status_por_data(nova_data, tipo=certidao.tipo)
        })
    except Exception as e:
        return _json_error(code=500, exc=e)


@bp.route('/certidao/monitorar_download_federal/<int:certidao_id>')
@requer_papel('operador')
def monitorar_download_federal(certidao_id):
    certidao = Certidao.query.get_or_404(certidao_id)

    log_event('federal_monitor_start', certidao_id=certidao_id)

    minha_chave_ts = file_manager.criar_chave_interrupcao()

    # Captura um snapshot antes de iniciar a janela de monitoramento
    # para detectar arquivos criados/alterados mesmo se o download iniciar cedo.
    snapshot_before = _snapshot_downloads_pdf()
    log_event('federal_monitor_snapshot', certidao_id=certidao_id, pdfs=len(snapshot_before))

    time.sleep(2)

    # Se a chave foi recriada durante o sleep (por /stop ou nova sessão), sair.
    if file_manager.chave_interrupcao_mais_recente_que(minha_chave_ts):
        file_manager.remover_chave_interrupcao()
        return _json_error('Monitoramento interrompido antes de iniciar.', 409, status='interrupted')

    file_manager.remover_chave_interrupcao()

    tempo_limite = 180
    tempo_inicio = time.time()
    chave_interrupcao = file_manager.obter_caminho_chave_interrupcao()
    ultimo_log = tempo_inicio

    termos_proibidos = [
        'consulta regularidade',
        'crf',
        'cndt',
        'sitafe'
    ]

    while (time.time() - tempo_inicio) < tempo_limite:
        if os.path.exists(chave_interrupcao):
            log_event(
                'federal_monitor_interrupted', level='WARNING', certidao_id=certidao_id,
                message='Monitoramento interrompido por nova requisição.',
            )
            file_manager.remover_chave_interrupcao()
            return _json_error('Monitoramento interrompido.', 409, status='interrupted')

        novo_arquivo = _pick_changed_download_pdf(snapshot_before)
        if not novo_arquivo:
            novo_arquivo = file_manager.verificar_novo_arquivo(
                tempo_inicio, termos_ignorar=termos_proibidos)

        agora = time.time()
        if (agora - ultimo_log) >= 5:
            restante = max(0, int(tempo_limite - (agora - tempo_inicio)))
            log_event(
                'federal_monitor_waiting', certidao_id=certidao_id,
                restante_s=restante, novo_arquivo=bool(novo_arquivo),
            )
            ultimo_log = agora

        if novo_arquivo:
            log_event('federal_file_detected', certidao_id=certidao_id, arquivo=str(novo_arquivo))

            sucesso, msg = file_manager.mover_e_renomear(
                novo_arquivo,
                certidao.empresa.nome,
                certidao.tipo.value
            )

            if sucesso:
                try:
                    certidao.caminho_arquivo = msg
                    db.session.commit()
                except Exception as e_db:
                    db.session.rollback()
                    log_event(
                        'federal_db_save_failed', level='WARNING',
                        certidao_id=certidao_id, error=str(e_db),
                    )
                validade_pdf = pdf.extrair_validade_federal(msg)
                if validade_pdf:
                    return jsonify({
                        'status': 'success',
                        'mensagem': f"Arquivo salvo no servidor: {msg}",
                        'visualizar_token': _gerar_visualizar_token(certidao_id),
                        'data_validade': validade_pdf.strftime('%Y-%m-%d'),
                        'data_validade_formatada': validade_pdf.strftime('%d/%m/%Y')
                    })
                return jsonify({
                    'status': 'success',
                    'mensagem': f"Arquivo salvo no servidor: {msg}",
                    'visualizar_token': _gerar_visualizar_token(certidao_id)
                })
            else:
                return _json_error(f"Erro ao mover: {msg}", 500)

        time.sleep(1)

    # limpeza final por segurança
    file_manager.remover_chave_interrupcao()
    return _json_error('Tempo esgotado sem download.', 408, status='timeout')


@bp.route('/certidao/monitorar_download_federal/stop', methods=['POST'])
@requer_papel('operador')
def interromper_monitoramento_federal():
    file_manager.criar_chave_interrupcao()
    return jsonify({'status': 'ok'})


@bp.route('/certidao/visualizar/<token>')
def visualizar_certidao(token):
    certidao_id = _carregar_visualizar_token(token)
    if not certidao_id:
        return 'Token inválido ou expirado.', 404

    certidao = Certidao.query.get_or_404(certidao_id)
    caminho = certidao.caminho_arquivo

    if not caminho or not os.path.exists(caminho):
        caminho = file_manager.localizar_certidao_existente(
            certidao.empresa.nome,
            certidao.tipo.value,
            certidao.subtipo.value if certidao.subtipo else None
        )
        if caminho:
            certidao.caminho_arquivo = caminho
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()

    if not caminho or not os.path.exists(caminho):
        return 'Arquivo não encontrado para esta certidão.', 404

    return send_file(
        caminho,
        mimetype='application/pdf',
        as_attachment=False,
        download_name=os.path.basename(caminho)
    )


@bp.route('/certidao/<int:certidao_id>/token-visualizar')
def gerar_token_visualizar(certidao_id):
    """Gera token de visualização sob demanda (lazy), evitando crypto no render do dashboard."""
    certidao = Certidao.query.get_or_404(certidao_id)
    caminho = certidao.caminho_arquivo

    if not caminho or not os.path.exists(caminho):
        caminho = file_manager.localizar_certidao_existente(
            certidao.empresa.nome,
            certidao.tipo.value,
            certidao.subtipo.value if certidao.subtipo else None
        )
        if caminho:
            certidao.caminho_arquivo = caminho
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()

    if not caminho or not os.path.exists(caminho):
        return jsonify({'erro': 'sem_arquivo'}), 404

    token = _gerar_visualizar_token(certidao_id)
    return jsonify({'url': url_for('main.visualizar_certidao', token=token)})


@bp.route('/certidao/marcar_pendente_json/<int:certidao_id>', methods=['POST'])
@requer_papel('operador')
def marcar_pendente_json(certidao_id):
    try:
        certidao = Certidao.query.get_or_404(certidao_id)
        ok, erro = certidao_service.marcar_pendente(certidao)
        if not ok:
            auditoria.registrar('certidao.marcar_pendente', alvo_tipo='certidao',
                                alvo_id=certidao_id, resultado='erro', detalhe=erro)
            return _json_error(erro, 500)
        auditoria.registrar('certidao.marcar_pendente', alvo_tipo='certidao', alvo_id=certidao_id)
        return jsonify({'status': 'success'})
    except Exception as e:
        db.session.rollback()
        auditoria.registrar('certidao.marcar_pendente', alvo_tipo='certidao',
                            alvo_id=certidao_id, resultado='erro', detalhe=str(e))
        return _json_error(code=500, exc=e)


@bp.route('/certidao/atualizar_json/<int:certidao_id>', methods=['POST'])
@requer_papel('operador')
def atualizar_validade_json(certidao_id):
    data = request.get_json()
    nova_data_str = data.get('nova_validade')

    try:
        certidao = Certidao.query.get_or_404(certidao_id)

        if nova_data_str:
            nova_data = datetime.strptime(nova_data_str, '%Y-%m-%d').date()
            ok, erro = certidao_service.aplicar_validade(certidao, nova_data)
            if not ok:
                auditoria.registrar('certidao.aplicar_validade', alvo_tipo='certidao',
                                    alvo_id=certidao_id, resultado='erro', detalhe=erro)
                return _json_error(erro, 500)

            auditoria.registrar('certidao.aplicar_validade', alvo_tipo='certidao', alvo_id=certidao_id)
            return jsonify({
                'status': 'success',
                'message': f'Validade de {certidao.empresa.nome} atualizada com sucesso!',
                'nova_data_formatada': nova_data.strftime('%d/%m/%Y'),
                'nova_classe': _classe_status_por_data(nova_data, tipo=certidao.tipo)
            })
        else:
            return _json_error('Data inválida.', 400)

    except Exception as e:
        db.session.rollback()
        return _json_error(code=500, exc=e)


# --- Exportacao da carteira (spec 04) -----------------------------------------

_XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def _slug_arquivo(texto):
    """Nome de arquivo seguro a partir de um texto livre (sem acento/espaco)."""
    base = re.sub(r'[^a-z0-9]+', '-', file_manager.remover_acentos(texto or '').lower()).strip('-')
    return base or 'empresa'


@bp.route('/exportar/carteira.xlsx')
@requer_papel('leitura')
def exportar_carteira():
    """Planilha XLSX da carteira respeitando os filtros ativos do painel
    (status/tipo/estado/cidade replicados server-side)."""
    buffer = export_service.gerar_planilha_carteira(
        status=request.args.getlist('status'),
        tipo=request.args.getlist('tipo'),
        estado=request.args.getlist('estado'),
        cidade=request.args.getlist('cidade'),
    )
    nome = f'carteira-{date.today().strftime("%Y%m%d")}.xlsx'
    return send_file(buffer, mimetype=_XLSX_MIME, as_attachment=True, download_name=nome)


@bp.route('/exportar/dossie/<int:empresa_id>.pdf')
@requer_papel('operador')
def exportar_dossie(empresa_id):
    """Dossie PDF (capa + certidoes validas) de uma empresa. Sem certidoes
    validas, avisa e volta ao painel em vez de baixar um PDF vazio."""
    empresa = Empresa.query.get_or_404(empresa_id)
    buffer, avisos = dossie_service.gerar_dossie(empresa)
    if buffer is None:
        flash(f'Não foi possível gerar o dossiê de {empresa.nome}: {"; ".join(avisos)}.', 'warning')
        return redirect(url_for('main.dashboard'))
    nome = f'dossie-{_slug_arquivo(empresa.nome)}.pdf'
    return send_file(buffer, mimetype='application/pdf', as_attachment=True, download_name=nome)
