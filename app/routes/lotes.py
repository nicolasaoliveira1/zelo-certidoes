"""Rotas e orquestracao de lotes (FGTS, Estadual RS, Municipal) + fluxos do agendador.

Extraido de app/routes.py (spec 05, REFA-02). Registra as rotas de lote via a
factory _register_batch_routes e os fluxos automatizaveis no agendador, tudo no
blueprint 'main' compartilhado (importado de app.routes). Os efeitos colaterais
de registro rodam no import deste modulo (feito por app/routes/__init__.py).
"""

from flask import (
    current_app,
    jsonify,
    request,
)

from app import db, file_manager
from app.automation.batch_state import (
    FGTS_BATCH_LOCK,
    FGTS_BATCH_STATE,
    MUNICIPAL_BATCH_LOCK,
    MUNICIPAL_BATCH_STATE,
    RS_BATCH_LOCK,
    RS_BATCH_STATE,
    emissao_individual_ativa,
)
from app.automation.driver import (
    _ativar_politica_autoselect_rs_temporaria,
    _criar_driver_chrome,
    _desativar_politica_autoselect_rs_temporaria,
)
from app.automation.emissao import (
    _emitir_estadual_rs_certidao,
    _emitir_fgts_certidao,
    _emitir_municipal_certidao_lote,
    _fgts_quit_driver_async,
    _fgts_status_por_data,
    _municipal_batch_suportado,
)
from app.models import (
    Certidao,
    Empresa,
    ExecucaoLote,
    TipoCertidao,
)
from app.utils import (
    get_config_value as _get_config_value,
    json_error as _json_error,
    to_bool as _to_bool,
    utcnow_naive,
)
from app.services import (
    agendador,
    auditoria,
    batch_engine,
    preflight,
)
from app.services.correlation import CorrelationContext
from app.services.execution_logger import log_event
from app.auth import requer_papel

from app.routes import bp, _current_app_object


# === bloco de lotes extraido de app/routes (mover != reescrever) ===
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


def _registrar_execucao_lote(nome_lote, scope, total, execution_id, origem='manual'):
    """Grava (persistente) o início de um lote para o relatório "último lote".
    `origem` distingue lote manual (rota HTTP) de agendado (emissão proativa) sem
    esconder nenhum dos relatórios (spec 07, COV-04).
    Best-effort: uma falha ao gravar nunca deve impedir o lote de iniciar."""
    try:
        db.session.add(ExecucaoLote(
            tipo=nome_lote,
            escopo=scope or 'default',
            total=total or 0,
            iniciado_em=utcnow_naive(),
            execution_id=execution_id,
            origem=origem,
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
    _registrar_execucao_lote(nome_lote, 'default', len(ids), execution_id,
                             origem='agendador')
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


