import base64
import json
import os
import random
import re
import string
import tempfile
import time
from datetime import date, datetime, timedelta
from threading import Lock, Thread

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
from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchWindowException,
    TimeoutException,
    UnexpectedAlertPresentException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

from app import db, file_manager
from app.automation import SITES_CERTIDOES, VALIDADES_CERTIDOES, pdf, steps
from app.automation.driver import (
    _ativar_politica_autoselect_rs_temporaria,
    _configurar_download_automatico_chrome,
    _criar_driver_chrome,
    _desativar_politica_autoselect_rs_temporaria,
)
from app.captcha_solver import solve_normal_captcha
from app.errors import map_exception_to_error_type
from app.models import (
    Certidao,
    ConfiguracaoSistema,
    Empresa,
    Municipio,
    StatusEspecial,
    SubtipoCertidao,
    TipoCertidao,
    get_a_vencer_dias,
)
from app.utils import get_config_value as _get_config_value, to_bool as _to_bool
from app.services import batch_engine, certidao_service
from app.services.correlation import CorrelationContext
from app.services.execution_logger import log_event
from app.services.health import run_health_checks
from app.services.retry import retry_call
from app.services.rs_altcha import (
    clicar_enviar_estadual_rs as _clicar_enviar_estadual_rs,
    resolver_altcha_rs_com_2captcha as _resolver_altcha_rs_com_2captcha,
)

bp = Blueprint('main', __name__)

FGTS_BATCH_LOCK = Lock()
RS_BATCH_LOCK = Lock()
MUNICIPAL_BATCH_LOCK = Lock()

FGTS_BATCH_STATE = batch_engine.batch_state_defaults()

RS_BATCH_STATE = batch_engine.batch_state_defaults()

MUNICIPAL_BATCH_STATE = batch_engine.batch_state_defaults()


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


def _fgts_stop_requested():
    return FGTS_BATCH_STATE.get('stop_requested')


def _rs_batch_stop_requested():
    return RS_BATCH_STATE.get('stop_requested')


def _municipal_batch_stop_requested():
    return MUNICIPAL_BATCH_STATE.get('stop_requested')


def _classe_status_por_data(data, tipo=None):
    """Classe CSS de status (status-cinza/vermelho/amarelo/verde) a partir de
    uma data de validade e do limite 'a vencer' do tipo informado."""
    if not data:
        return 'status-cinza'

    diferenca = (data - date.today()).days
    limite_dias = get_a_vencer_dias(tipo=tipo)
    if diferenca < 0:
        return 'status-vermelho'
    if diferenca <= limite_dias:
        return 'status-amarelo'
    return 'status-verde'


def _fgts_status_por_data(nova_data):
    return _classe_status_por_data(nova_data, tipo=TipoCertidao.FGTS)


def _fgts_normalizar_texto(texto):
    texto_limpo = file_manager.remover_acentos((texto or '').strip().lower())
    texto_limpo = re.sub(r'\s+', ' ', texto_limpo)
    return texto_limpo


def _fgts_detectar_mensagem_impedimento(driver):
    texto_base = ''
    try:
        texto_base = driver.find_element(By.TAG_NAME, 'body').text or ''
    except Exception:
        try:
            texto_base = driver.page_source or ''
        except Exception:
            texto_base = ''

    texto_norm = _fgts_normalizar_texto(texto_base)
    if not texto_norm:
        return None

    msg_insuficiente = (
        'as informacoes disponiveis nao sao suficientes para a comprovacao automatica '
        'da regularidade do empregador perante o fgts'
    )
    msg_nao_cadastrado = 'empregador nao cadastrado'
    msg_impedimentos_caixa = 'constam impedimentos na caixa para a comprovacao da regularidade do empregador no fgts'
    msg_operacao_nao_efetuada = 'fger0419'

    if msg_insuficiente in texto_norm:
        return (
            'FGTS com informações insuficientes para comprovação automática. '
            'Mantida como PENDENTE e seguindo para a próxima empresa.'
        )

    if msg_nao_cadastrado in texto_norm:
        return 'Empregador não cadastrado no FGTS. Mantida como PENDENTE e seguindo para a próxima empresa.'

    if msg_impedimentos_caixa in texto_norm:
        return 'Constam impedimentos na CAIXA. Certidão FGTS mantida como PENDENTE e seguindo para a próxima empresa.'

    if msg_operacao_nao_efetuada in texto_norm:
        return 'FGER0419: operação não efetuada. Certidão FGTS mantida como PENDENTE e seguindo para a próxima empresa.'

    return None


def _fgts_fechar_abas_extras(driver):
    if not driver:
        return

    try:
        handles = list(driver.window_handles)
    except Exception:
        return

    if len(handles) <= 1:
        return

    principal = handles[0]
    for handle in handles[1:]:
        try:
            driver.switch_to.window(handle)
            driver.close()
        except Exception:
            continue

    try:
        driver.switch_to.window(principal)
    except Exception:
        pass


def _fgts_marcar_pendente_por_impedimento(certidao, mensagem_base=None):
    if not certidao:
        return False, 'Certidão FGTS inválida para marcação pendente por impedimento.'

    try:
        certidao.status_especial = StatusEspecial.PENDENTE
        certidao.data_validade = None
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        return False, f'Erro ao marcar FGTS como pendente após impedimento: {exc}'

    with FGTS_BATCH_LOCK:
        FGTS_BATCH_STATE['fgts_marcadas_pendente'] = FGTS_BATCH_STATE.get('fgts_marcadas_pendente', 0) + 1
        FGTS_BATCH_STATE['last_completed'] = {
            'certidao_id': certidao.id,
            'data_formatada': 'PENDENTE',
            'nova_classe': 'status-vermelho'
        }
        batch_engine.append_batch_message(
            FGTS_BATCH_STATE,
            f"FGTS ID={certidao.id} marcado como pendente por impedimento.",
            level='warning',
            certidao_id=certidao.id,
        )

    msg = mensagem_base or 'FGTS com impedimento de emissão automática. Certidão marcada como pendente.'
    return True, msg


def _fgts_quit_driver_async(driver):
    if not driver:
        return

    def _close():
        try:
            driver.quit()
        except Exception:
            pass

    Thread(target=_close, daemon=True).start()


def _normalizar_cnpj(cnpj):
    return ''.join(filter(str.isdigit, cnpj or ''))


def _formatar_cnpj(cnpj_limpo):
    if len(cnpj_limpo) != 14:
        return None
    return (
        f"{cnpj_limpo[0:2]}.{cnpj_limpo[2:5]}.{cnpj_limpo[5:8]}/"
        f"{cnpj_limpo[8:12]}-{cnpj_limpo[12:14]}"
    )


def _json_error(message, code=400, **extra):
    texto = message or 'Erro inesperado.'
    payload = {
        'status': 'error',
        'message': texto,
        'mensagem': texto,
        'codigo': code,
        'request_id': CorrelationContext.get_request_id(),
    }
    payload.update(extra)
    return jsonify(payload), code


def _preparar_pagina_fgts(driver, url, cnpj_field_id):
    if not driver or not url or not cnpj_field_id:
        return False

    try:
        driver.set_page_load_timeout(30)
    except Exception:
        pass

    try:
        driver.delete_all_cookies()
    except Exception:
        pass

    try:
        driver.get("about:blank")
    except Exception:
        pass

    def _carregar_url_fgts():
        try:
            driver.get(url)
        except TimeoutException:
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
            raise

    try:
        retry_call(
            _carregar_url_fgts,
            max_attempts=3,
            base_delay=0.5,
            jitter=0.2,
            retry_if=lambda exc: isinstance(exc, TimeoutException),
            on_retry=lambda attempt, delay, exc: log_event(
                'fgts_page_retry',
                level='WARNING',
                attempt=attempt,
                delay_ms=int(delay * 1000),
                error=str(exc),
            ),
        )
    except TimeoutException:
        pass

    deadline = time.time() + 20
    while time.time() < deadline:
        if _fgts_stop_requested():
            return False
        try:
            WebDriverWait(driver, 1).until(
                EC.element_to_be_clickable((By.ID, cnpj_field_id))
            )
            return True
        except TimeoutException:
            continue

    return True


def _calc_fgts_targets_by_scope(start_certidao_id, scope='default'):
    return batch_engine.calc_targets(
        start_certidao_id,
        extra_filter=lambda query: query.filter(Certidao.tipo == TipoCertidao.FGTS),
        scope=scope,
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
    )


def _batch_targets_vazios(scope='default'):
    scope_norm = _parse_batch_scope(scope)
    return {
        'ids': [],
        'total': 0,
        'scope': scope_norm,
        'vencidas': 0,
        'a_vencer': 0,
        'pendentes': 0,
    }


def _municipal_batch_suportado(cidade):
    cidade_norm = file_manager.remover_acentos((cidade or '').strip()).upper()
    return cidade_norm in {'IMBE', 'TRAMANDAI'}


def _calc_municipal_targets_by_scope(start_certidao_id, scope='default'):
    certidao = Certidao.query.get(start_certidao_id)
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
    )


def _parse_batch_scope(raw_scope):
    scope = (raw_scope or 'default').strip().lower()
    if scope in {'pendente', 'pendentes'}:
        return 'pendentes'
    return 'default'


def _snapshot_downloads_pdf():
    pasta_downloads = os.path.join(os.path.expanduser("~"), "Downloads")
    snapshot = {}

    try:
        nomes = os.listdir(pasta_downloads)
    except Exception:
        return snapshot

    for nome in nomes:
        caminho = os.path.join(pasta_downloads, nome)
        if not os.path.isfile(caminho):
            continue

        nome_l = nome.lower()
        if not nome_l.endswith('.pdf'):
            continue
        if nome_l.endswith('.crdownload') or nome_l.endswith('.tmp'):
            continue

        try:
            stat = os.stat(caminho)
            snapshot[caminho] = {
                'mtime': stat.st_mtime,
                'size': stat.st_size,
            }
        except OSError:
            continue

    return snapshot


def _pick_changed_download_pdf(snapshot_before):
    current = _snapshot_downloads_pdf()
    candidatos = []

    for caminho, info in current.items():
        base = snapshot_before.get(caminho)
        if base is None:
            candidatos.append((info['mtime'], caminho))
            continue

        if info['mtime'] > base.get('mtime', 0) or info['size'] != base.get('size', -1):
            candidatos.append((info['mtime'], caminho))

    if not candidatos:
        return None

    candidatos.sort(key=lambda item: item[0], reverse=True)
    return candidatos[0][1]


def _wait_file_stable(caminho_arquivo, checks=3, interval=0.6):
    ultimo_tamanho = None
    estavel = 0

    for _ in range(max(2, int(checks))):
        try:
            tamanho = os.path.getsize(caminho_arquivo)
        except OSError:
            return False

        if tamanho > 0 and tamanho == ultimo_tamanho:
            estavel += 1
            if estavel >= 2:
                return True
        else:
            estavel = 0

        ultimo_tamanho = tamanho
        time.sleep(interval)

    return False


def _rs_pagina_solicitacao_pronta(driver, cnpj_field_name='campoCnpj', timeout=3):
    try:
        WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.NAME, cnpj_field_name))
        )
        return True
    except Exception:
        return False


def _rs_garantir_pagina_solicitacao(driver, info_site):
    cnpj_field_name = info_site.get('cnpj_field_id', 'campoCnpj')
    url_atual = (driver.current_url or '').lower()
    if 'certidaositfiscalsolic.aspx' in url_atual and _rs_pagina_solicitacao_pronta(driver, cnpj_field_name):
        return True

    _login_certificado_rs(driver, info_site.get('login_cert_url'), info_site.get('url'))
    return _rs_pagina_solicitacao_pronta(driver, cnpj_field_name, timeout=8)


def _rs_preencher_cnpj_com_confirmacao(driver, cnpj_field_name, cnpj_limpo, tentativas=3):
    for _ in range(max(1, int(tentativas))):
        try:
            campo_cnpj = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.NAME, cnpj_field_name))
            )
            campo_cnpj.click()
            campo_cnpj.clear()
            campo_cnpj.send_keys(cnpj_limpo)

            valor = _normalizar_cnpj(campo_cnpj.get_attribute('value') or '')
            if valor == cnpj_limpo:
                return True
        except Exception:
            pass

        time.sleep(0.4)

    return False


def _emitir_estadual_rs_certidao(certidao_id, driver=None, usar_2captcha=False, execution_id=None):
    if execution_id:
        CorrelationContext.set_execution_id(execution_id)
    if _rs_batch_stop_requested():
        return False, False, 'Lote interrompido.'

    certidao = Certidao.query.get(certidao_id)
    if not certidao:
        return False, False, 'Certidão não encontrada.'

    if certidao.tipo != TipoCertidao.ESTADUAL or (certidao.empresa.estado or '').strip().upper() != 'RS':
        return False, True, 'Certidão não pertence ao fluxo Estadual RS.'

    info_site = (SITES_CERTIDOES.get('ESTADUAL', {}).get('RS') or {}).copy()
    if not info_site.get('url') or not info_site.get('login_cert_url'):
        return False, True, 'Configuração Estadual RS ausente.'

    cnpj_limpo = _normalizar_cnpj(certidao.empresa.cnpj)

    local_driver = driver
    criado_localmente = False
    inicio_fluxo = time.time()

    def _log_etapa(etapa, extra=''):
        state = _rs_get_page_state(local_driver)
        elapsed = time.time() - inicio_fluxo
        sufixo = f" | {extra}" if extra else ''
        log_event(
            'rs_batch_stage',
            certidao_id=certidao_id,
            empresa_id=certidao.empresa_id if certidao else None,
            stage=etapa,
            duration_ms=int(elapsed * 1000),
            status='running',
            extra=extra,
        )
        print(
            f"[ESTADUAL-RS-LOTE][ID={certidao_id}] {etapa} "
            f"| +{elapsed:.1f}s | url={state['url']} | title={state['title']}{sufixo}"
        )

    try:
        if local_driver is None:
            local_driver = _criar_driver_chrome(anonimo=False, usar_perfil=True)
            criado_localmente = True

        RS_BATCH_STATE['driver'] = local_driver

        _log_etapa('Garantindo página de solicitação RS')
        if not _rs_garantir_pagina_solicitacao(local_driver, info_site):
            _log_etapa('Falha ao abrir página de solicitação RS')
            return False, True, 'Não foi possível abrir a página de solicitação da certidão RS.'
        _log_etapa('Página de solicitação pronta')

        if _rs_sessao_expirada(local_driver):
            _log_etapa('Sessão expirada detectada logo após login')
            return False, False, 'Sessão RS expirada logo após login com certificado.'

        _log_etapa('Preenchendo CNPJ')
        if not _rs_preencher_cnpj_com_confirmacao(
            local_driver,
            info_site.get('cnpj_field_id', 'campoCnpj'),
            cnpj_limpo,
            tentativas=3,
        ):
            _log_etapa('Falha ao preencher CNPJ')
            return False, False, 'Não foi possível preencher o CNPJ antes de resolver o ALTCHA.'
        _log_etapa('CNPJ preenchido')

        if _rs_sessao_expirada(local_driver):
            _log_etapa('Sessão expirada detectada após preencher CNPJ')
            return False, False, 'Sessão RS expirada após preencher CNPJ.'

        if usar_2captcha:
            altcha_resultado = None
            for tentativa_altcha in range(1, 3):
                _log_etapa('Iniciando tentativa ALTCHA', extra=f'tentativa={tentativa_altcha}')
                altcha_resultado = _resolver_altcha_rs_com_2captcha(local_driver, current_app.config, allow_solver=True)
                _log_etapa(
                    'Retorno ALTCHA',
                    extra=(
                        f"status={altcha_resultado.get('status')} "
                        f"msg={(altcha_resultado.get('message') or '')[:180]}"
                    )
                )
                if altcha_resultado.get('status') == 'solved':
                    break
                if tentativa_altcha < 2:
                    time.sleep(1.0)

            if not altcha_resultado or altcha_resultado.get('status') != 'solved':
                detalhe = (altcha_resultado or {}).get('message') if altcha_resultado else None
                detalhe_upper = (detalhe or '').upper()
                if detalhe:
                    err_type = map_exception_to_error_type(detalhe)
                    log_event(
                        'rs_altcha_attempt_failed',
                        level='WARNING',
                        certidao_id=certidao_id,
                        empresa_id=certidao.empresa_id if certidao else None,
                        error_type=err_type.value,
                        error=detalhe,
                    )
                if 'ERROR_KEY_DOES_NOT_EXIST' in detalhe_upper:
                    return (
                        False,
                        True,
                        'Chave da API 2captcha inválida (ERROR_KEY_DOES_NOT_EXIST). '
                        'Revise CAPTCHA_2_API_KEY e reinicie a aplicação.'
                    )
                if detalhe:
                    return False, False, f"ALTCHA não resolvido: {altcha_resultado.get('status')} ({detalhe})"
                return False, False, f"ALTCHA não resolvido: {altcha_resultado.get('status') if altcha_resultado else 'sem_resposta'}"

            if _rs_sessao_expirada(local_driver):
                _log_etapa('Sessão expirada detectada após resolver ALTCHA')
                return False, False, 'Sessão RS expirada após resolver ALTCHA.'

            time.sleep(0.5)
            _log_etapa('Tentando clicar Enviar')
            snapshot_downloads_antes_envio = _snapshot_downloads_pdf()
            try:
                handle_principal_rs = local_driver.current_window_handle
            except Exception:
                handle_principal_rs = None
            envio_rs = _clicar_enviar_estadual_rs(local_driver, timeout=8, retries=4, post_wait=0.5)
            _log_etapa('Resultado clique Enviar', extra=f"clicked={envio_rs.get('clicked')} method={envio_rs.get('method')}")
            if not envio_rs.get('clicked'):
                return False, False, 'Não foi possível acionar o botão Enviar no lote RS.'

            time.sleep(0.7)
            if _rs_fechar_abas_processamento(local_driver, handle_principal=handle_principal_rs):
                _log_etapa('Certidão em processamento detectada; mantendo pendente e seguindo lote')
                certidao.caminho_arquivo = None
                certidao.status_especial = StatusEspecial.PENDENTE
                certidao.data_validade = None
                db.session.commit()

                with RS_BATCH_LOCK:
                    RS_BATCH_STATE['last_completed'] = {
                        'certidao_id': certidao.id,
                        'data_formatada': 'PENDENTE',
                        'nova_classe': 'status-vermelho'
                    }
                    batch_engine.append_batch_message(
                        RS_BATCH_STATE,
                        f"RS ID={certidao.id} manteve pendente por processamento.",
                        level='warning',
                        certidao_id=certidao.id,
                    )

                return True, False, None

            if _rs_sessao_expirada(local_driver):
                _log_etapa('Sessão expirada detectada após clicar Enviar')
                return False, False, 'Sessão RS expirada após clicar Enviar.'

        inicio_monitoramento = time.time()
        prazo_download = 180
        novo_arquivo = None
        _log_etapa('Aguardando download')

        while (time.time() - inicio_monitoramento) < prazo_download:
            if _rs_batch_stop_requested():
                return False, False, 'Lote interrompido.'

            if _rs_sessao_expirada(local_driver):
                _log_etapa('Sessão expirada detectada durante espera de download')
                return False, False, 'Sessão RS expirada durante espera do download.'

            if usar_2captcha:
                candidato = _pick_changed_download_pdf(snapshot_downloads_antes_envio)
            else:
                candidato = file_manager.verificar_novo_arquivo(inicio_monitoramento)

            if candidato:
                if not _wait_file_stable(candidato, checks=4, interval=0.6):
                    _log_etapa('Arquivo detectado ainda instável', extra=f'arquivo={os.path.basename(candidato)}')
                    time.sleep(0.8)
                    continue
                novo_arquivo = candidato
                _log_etapa('Download detectado', extra=f'arquivo={os.path.basename(candidato)}')
                break

            time.sleep(1)

        if not novo_arquivo:
            return False, True, 'Timeout aguardando download da certidão Estadual RS.'

        sucesso, caminho_final = file_manager.mover_e_renomear(
            novo_arquivo,
            certidao.empresa.nome,
            certidao.tipo.value
        )
        if not sucesso:
            return False, True, f'Falha ao mover arquivo Estadual RS: {caminho_final}'

        certidao.caminho_arquivo = caminho_final
        classificacao = pdf.classificar_estadual_rs(caminho_final)

        if classificacao == 'positiva':
            try:
                if caminho_final and os.path.exists(caminho_final):
                    os.remove(caminho_final)
            except Exception as exc_remove:
                print(f"[ESTADUAL-RS-LOTE] Aviso ao remover PDF positivo: {exc_remove}")

            certidao.caminho_arquivo = None
            certidao.status_especial = StatusEspecial.PENDENTE
            certidao.data_validade = None
            db.session.commit()

            with RS_BATCH_LOCK:
                RS_BATCH_STATE['positivas'] = RS_BATCH_STATE.get('positivas', 0) + 1
                RS_BATCH_STATE['last_completed'] = {
                    'certidao_id': certidao.id,
                    'data_formatada': 'PENDENTE',
                    'nova_classe': 'status-vermelho'
                }
                batch_engine.append_batch_message(
                    RS_BATCH_STATE,
                    f"RS ID={certidao.id} positiva: marcada como pendente.",
                    level='warning',
                    certidao_id=certidao.id,
                )
            return True, False, None

        nova_data = calcular_validade_padrao(certidao, None)
        certidao.data_validade = nova_data
        certidao.status_especial = None
        db.session.commit()

        with RS_BATCH_LOCK:
            if classificacao == 'negativa':
                RS_BATCH_STATE['negativas'] = RS_BATCH_STATE.get('negativas', 0) + 1
            elif classificacao == 'efeito_negativa':
                RS_BATCH_STATE['efeito_negativas'] = RS_BATCH_STATE.get('efeito_negativas', 0) + 1

            RS_BATCH_STATE['last_completed'] = {
                'certidao_id': certidao.id,
                'data_formatada': nova_data.strftime('%d/%m/%Y') if nova_data else None,
                'nova_classe': _fgts_status_por_data(nova_data)
            }
            batch_engine.append_batch_message(
                RS_BATCH_STATE,
                f"RS ID={certidao.id} emitida com sucesso.",
                level='info',
                certidao_id=certidao.id,
            )

        return True, False, None
    except Exception as exc:
        db.session.rollback()
        log_event(
            'rs_batch_error',
            level='ERROR',
            certidao_id=certidao_id,
            empresa_id=certidao.empresa_id if certidao else None,
            error_type=map_exception_to_error_type(exc).value,
            error=str(exc),
        )
        return False, True, f'Erro grave no lote Estadual RS: {exc}'
    finally:
        if criado_localmente:
            RS_BATCH_STATE['driver'] = None
            if local_driver:
                try:
                    local_driver.quit()
                except Exception:
                    pass


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
    )


def _imbe_encontrar_captcha_imagem(driver, timeout=10):
    candidatos = [
        "//img[contains(translate(@alt, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'verificacao')]",
        "//img[contains(translate(@alt, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'palavra')]",
        "//img[contains(translate(@src, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'captcha')]",
        "//img[contains(translate(@src, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'verificacao')]",
    ]
    for xpath in candidatos:
        try:
            return WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.XPATH, xpath))
            )
        except TimeoutException:
            continue
    return None


def _imbe_encontrar_campo_captcha(driver, timeout=10):
    xpath = (
        "//input[(contains(translate(@id, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'captcha')"
        " or contains(translate(@name, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'captcha')"
        " or contains(translate(@id, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'verificacao')"
        " or contains(translate(@name, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'verificacao')"
        " or contains(translate(@id, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'palavra')"
        " or contains(translate(@name, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'palavra'))"
        " and (not(@type) or translate(@type, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='text')]"
    )
    try:
        return WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.XPATH, xpath))
        )
    except TimeoutException:
        return None


def _imbe_obter_mensagem_sistema(driver, timeout=4):
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, '.mensagemSistema'))
        )
    except TimeoutException:
        return None

    elementos = driver.find_elements(By.CSS_SELECTOR, '.mensagemSistema')
    if not elementos:
        return None

    elemento = elementos[-1]
    texto = file_manager.remover_acentos((elemento.text or '')).upper()
    if 'RELATORIO SEM CONTEUDO' in texto:
        return 'sem_conteudo'
    if 'PALAVRA DE VERIFICACAO NAO CONFERE' in texto:
        return 'captcha_incorreto'
    return None


def _imbe_fechar_modal_erro_captcha(driver, timeout=3):
    seletores = [
        "//a[contains(@class,'ui-messages-close')]",
        "//span[contains(@class,'ui-icon-close')]",
        "//button[contains(@class,'close') or @aria-label='Close' or @aria-label='Fechar']",
        "//a[contains(@class,'ui-growl-item-close')]",
        "//*[contains(@class,'mensagemSistema')]/following-sibling::*//button",
    ]
    for seletor in seletores:
        try:
            el = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((By.XPATH, seletor))
            )
            el.click()
            time.sleep(0.3)
            return True
        except TimeoutException:
            continue
        except Exception:
            continue
    return False


def _imbe_resolver_captcha_2captcha(driver, execution_id=None):
    imagem = _imbe_encontrar_captcha_imagem(driver)
    if not imagem:
        return False, 'Imagem do captcha não encontrada.'

    campo = _imbe_encontrar_campo_captcha(driver)
    if not campo:
        return False, 'Campo do captcha não encontrado.'

    arquivo_tmp = None
    try:
        captcha_bytes = imagem.screenshot_as_png
        if not captcha_bytes:
            return False, 'Captcha sem imagem capturada.'

        with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as tmp:
            tmp.write(captcha_bytes)
            arquivo_tmp = tmp.name

        resultado = solve_normal_captcha(
            current_app.config,
            image_path=arquivo_tmp,
            execution_id=execution_id,
        )
        codigo = (resultado.get('code') or '').strip()
        if not codigo:
            return False, 'Resposta do 2captcha vazia.'

        campo.clear()
        campo.click()
        campo.send_keys(codigo)
        return True, None
    except Exception as exc:
        return False, f'Falha ao resolver captcha: {exc}'
    finally:
        if arquivo_tmp and os.path.exists(arquivo_tmp):
            try:
                os.remove(arquivo_tmp)
            except Exception:
                pass


def _emitir_municipal_certidao_lote(certidao_id, driver=None, execution_id=None):
    if execution_id:
        CorrelationContext.set_execution_id(execution_id)
    if _municipal_batch_stop_requested():
        return False, False, 'Lote interrompido.'

    certidao = Certidao.query.get(certidao_id)
    if not certidao:
        return False, False, 'Certidão não encontrada.'

    if certidao.tipo != TipoCertidao.MUNICIPAL:
        return False, False, 'Certidão não pertence ao fluxo Municipal.'

    cidade = (certidao.empresa.cidade or '').strip()
    if not _municipal_batch_suportado(cidade):
        return False, False, 'Município não habilitado para lote municipal.'

    regra_municipio = _buscar_municipio_por_cidade(cidade)
    if not regra_municipio:
        return False, False, 'Regra municipal não encontrada.'

    if regra_municipio.automacao_ativa is False:
        return False, False, 'Automação desativada para este município.'

    config_municipal = _carregar_config_municipio(regra_municipio)
    if config_municipal is None:
        return False, False, 'Município sem configuração de automação.'

    info_site = {
        'url': regra_municipio.url_certidao,
        'cnpj_field_id': regra_municipio.cnpj_field_id,
        'by': regra_municipio.by,
        'pre_fill_click_id': regra_municipio.pre_fill_click_id,
        'pre_fill_click_by': regra_municipio.pre_fill_click_by,
        'inscricao_field_id': regra_municipio.inscricao_field_id,
        'inscricao_field_by': regra_municipio.inscricao_field_by,
        'slow_typing': bool(regra_municipio.usar_slow_typing),
    }

    imbe_tipo = ''
    cidade_regra_norm = file_manager.remover_acentos(regra_municipio.nome or '').upper()
    if cidade_regra_norm == 'IMBE':
        imbe_tipo = _resolve_imbe_tipo_from_subtipo(certidao.subtipo)
        if imbe_tipo not in {'geral', 'mobiliario'}:
            return False, False, 'Certidão IMBE sem subtipo válido.'
        _aplicar_variantes_imbe(info_site, config_municipal, imbe_tipo)

    if config_municipal.get('skip_cnpj_fill'):
        info_site['cnpj_field_id'] = None

    cnpj_limpo = _normalizar_cnpj(certidao.empresa.cnpj)
    inscricao_limpa = certidao.empresa.inscricao_mobiliaria or ''

    nome_certidao_arquivo = certidao.tipo.value
    if cidade_regra_norm == 'IMBE':
        nome_certidao_arquivo = _nome_certidao_imbe(nome_certidao_arquivo, imbe_tipo)

    local_driver = driver
    criado_localmente = False

    try:
        if local_driver is None:
            local_driver = _criar_driver_chrome()
            criado_localmente = True

        MUNICIPAL_BATCH_STATE['driver'] = local_driver

        wait = WebDriverWait(local_driver, 20)
        local_driver.get(info_site.get('url'))
        try:
            _configurar_download_automatico_chrome(local_driver)
        except Exception as exc:
            print(f"[MUNICIPAL][LOTE] Falha ao reaplicar download automático: {exc}")

        snapshot_before = _snapshot_downloads_pdf()

        steps_before = config_municipal.get('before_cnpj', []) if config_municipal else []
        resultado_steps = steps.executar_municipio(
            local_driver,
            wait,
            steps_before,
            cnpj_limpo,
            inscricao_limpa,
            etapa_label='before_cnpj',
        )
        if resultado_steps and resultado_steps.get('encerrar_sem_arquivo'):
            certidao.status_especial = StatusEspecial.PENDENTE
            certidao.data_validade = None
            db.session.commit()
            return True, False, 'Certidão sem negativa, marcada como pendente.'

        if info_site.get('pre_fill_click_id'):
            click_by = info_site.get('pre_fill_click_by') or 'id'
            click_map = steps.BY_MAP
            by = click_map.get(click_by)
            if by:
                try:
                    elemento_inicial = wait.until(EC.element_to_be_clickable((by, info_site['pre_fill_click_id'])))
                    elemento_inicial.click()
                    time.sleep(1)
                except Exception:
                    pass

        if info_site.get('cnpj_field_id'):
            by_map = steps.BY_MAP
            field_by = by_map.get(info_site.get('by'))
            if field_by:
                try:
                    campo1 = wait.until(EC.element_to_be_clickable((field_by, info_site['cnpj_field_id'])))
                    if info_site.get('slow_typing'):
                        campo1.clear()
                        for digito in _normalizar_cnpj(cnpj_limpo):
                            campo1.send_keys(digito)
                            time.sleep(0.1)
                    else:
                        campo1.click()
                        campo1.send_keys(cnpj_limpo)
                except Exception:
                    pass

        steps_after = config_municipal.get('after_cnpj', []) if config_municipal else []
        resultado_steps = steps.executar_municipio(
            local_driver,
            wait,
            steps_after,
            cnpj_limpo,
            inscricao_limpa,
            etapa_label='after_cnpj',
        )
        if resultado_steps and resultado_steps.get('encerrar_sem_arquivo'):
            certidao.status_especial = StatusEspecial.PENDENTE
            certidao.data_validade = None
            db.session.commit()
            return True, False, 'Certidão sem negativa, marcada como pendente.'

        if info_site.get('inscricao_field_id'):
            by_map = steps.BY_MAP
            field_by = by_map.get(info_site.get('inscricao_field_by'))
            if field_by:
                try:
                    campo2 = wait.until(EC.element_to_be_clickable((field_by, info_site['inscricao_field_id'])))
                    campo2.click()
                    campo2.send_keys(inscricao_limpa)
                    campo2.send_keys(Keys.TAB)
                except Exception:
                    pass

        if cidade_regra_norm == 'IMBE':
            for tentativa in range(1, 3):
                if tentativa > 1:
                    _imbe_fechar_modal_erro_captcha(local_driver)
                    time.sleep(0.4)

                ok, erro_msg = _imbe_resolver_captcha_2captcha(local_driver, execution_id=execution_id)
                if not ok:
                    if tentativa >= 2:
                        return False, False, erro_msg or 'Falha ao resolver captcha IMBE.'
                    _imbe_fechar_modal_erro_captcha(local_driver)
                    time.sleep(0.6)
                    continue

                handles_antes = set(local_driver.window_handles)
                try:
                    link = wait.until(EC.element_to_be_clickable((By.ID, 'form:j_id_51_1_2_1')))
                    link.click()
                except Exception:
                    try:
                        link = local_driver.find_element(By.ID, 'form:j_id_51_1_2_1')
                        local_driver.execute_script('arguments[0].click();', link)
                    except Exception as exc:
                        return False, False, f'Não foi possível clicar no link da certidão: {exc}'

                time.sleep(1.5)
                novas_abas = set(local_driver.window_handles) - handles_antes
                if novas_abas:
                    local_driver.switch_to.window(novas_abas.pop())
                    try:
                        WebDriverWait(local_driver, 10).until(
                            lambda d: d.execute_script('return document.readyState') == 'complete'
                        )
                    except Exception:
                        pass
                    try:
                        _configurar_download_automatico_chrome(local_driver)
                    except Exception:
                        pass
                mensagem = _imbe_obter_mensagem_sistema(local_driver, timeout=4)
                if mensagem == 'captcha_incorreto':
                    if tentativa >= 2:
                        return False, False, 'Captcha incorreto (2 tentativas).'
                    time.sleep(0.6)
                    continue
                if mensagem == 'sem_conteudo':
                    certidao.status_especial = StatusEspecial.PENDENTE
                    certidao.data_validade = None
                    db.session.commit()
                    return True, False, 'Relatório sem conteúdo. Certidão marcada como pendente.'
                break

            try:
                pdf_data_url = local_driver.execute_script(
                    "var el = document.getElementById('form:pdfOut_AcessoExterno');"
                    " return el ? el.getAttribute('data') : null;"
                )
                if not pdf_data_url:
                    return False, False, 'URL do PDF Imbé não encontrada na página da certidão.'
                if pdf_data_url.startswith('/'):
                    from urllib.parse import urlparse
                    _parsed = urlparse(local_driver.current_url)
                    pdf_data_url = f"{_parsed.scheme}://{_parsed.netloc}{pdf_data_url}"
                local_driver.get(pdf_data_url)
                time.sleep(1.0)
            except Exception as exc:
                return False, False, f'Falha ao acionar download do PDF Imbé: {exc}'

        tempo_inicio = time.time()
        tempo_limite = 90

        while (time.time() - tempo_inicio) < tempo_limite:
            if _municipal_batch_stop_requested():
                return False, False, 'Lote interrompido.'

            novo_arquivo = _pick_changed_download_pdf(snapshot_before)
            if not novo_arquivo:
                novo_arquivo = file_manager.verificar_novo_arquivo(tempo_inicio)

            if novo_arquivo:
                sucesso, msg = file_manager.mover_e_renomear(
                    novo_arquivo,
                    certidao.empresa.nome,
                    nome_certidao_arquivo,
                )

                if not sucesso:
                    return False, False, f'Erro ao salvar: {msg}'

                certidao.caminho_arquivo = msg
                data_calc = _calcular_validade_municipal(regra_municipio)
                certidao.data_validade = data_calc
                certidao.status_especial = None
                try:
                    db.session.commit()
                except Exception as e_db:
                    db.session.rollback()
                    return False, False, f'Erro ao salvar no banco: {e_db}'

                if bool((config_municipal or {}).get('classificar_pdf_status')):
                    origem_pdf = f"MUNICIPAL-{regra_municipio.nome}"
                    municipal_pdf_classificacao = pdf.classificar_status(msg, origem_log=origem_pdf)
                    if municipal_pdf_classificacao == 'positiva':
                        try:
                            if msg and os.path.exists(msg):
                                os.remove(msg)
                        except Exception:
                            pass
                        certidao.caminho_arquivo = None
                        certidao.status_especial = StatusEspecial.PENDENTE
                        certidao.data_validade = None
                        try:
                            db.session.commit()
                        except Exception:
                            db.session.rollback()
                            return False, False, 'Erro ao marcar pendente após PDF positivo.'
                        with MUNICIPAL_BATCH_LOCK:
                            MUNICIPAL_BATCH_STATE['last_completed'] = {
                                'certidao_id': certidao.id,
                                'data_formatada': 'PENDENTE',
                                'nova_classe': 'status-vermelho',
                            }
                        return True, False, 'Certidão positiva detectada e marcada como pendente.'

                with MUNICIPAL_BATCH_LOCK:
                    MUNICIPAL_BATCH_STATE['last_completed'] = {
                        'certidao_id': certidao.id,
                        'data_formatada': data_calc.strftime('%d/%m/%Y') if data_calc else None,
                        'nova_classe': _fgts_status_por_data(data_calc),
                    }
                return True, False, 'Certidão municipal emitida com sucesso.'

            time.sleep(1)

        return False, False, 'Tempo esgotado sem download.'
    except UnexpectedAlertPresentException:
        try:
            local_driver.switch_to.alert.dismiss()
        except Exception:
            pass
        try:
            certidao = Certidao.query.get(certidao_id)
            if certidao:
                certidao.status_especial = StatusEspecial.PENDENTE
                certidao.data_validade = None
                db.session.commit()
        except Exception:
            db.session.rollback()
        with MUNICIPAL_BATCH_LOCK:
            MUNICIPAL_BATCH_STATE['last_completed'] = {
                'certidao_id': certidao_id,
                'data_formatada': 'PENDENTE',
                'nova_classe': 'status-vermelho',
            }
            batch_engine.append_batch_message(
                MUNICIPAL_BATCH_STATE,
                f"Municipal ID={certidao_id}: CNPJ não cadastrado, marcado como pendente.",
                level='warning',
                certidao_id=certidao_id,
            )
        return True, False, 'CNPJ não cadastrado no município. Certidão marcada como pendente.'
    except Exception as exc:
        err_type = map_exception_to_error_type(exc).value
        log_event(
            'municipal_batch_emit_error',
            level='ERROR',
            certidao_id=certidao_id,
            empresa_id=certidao.empresa_id if certidao else None,
            error_type=err_type,
            error=str(exc),
        )
        return False, True, f'Erro grave no lote municipal: {exc}'
    finally:
        if local_driver and not criado_localmente:
            _fgts_fechar_abas_extras(local_driver)
        if criado_localmente and local_driver:
            try:
                local_driver.quit()
            except Exception:
                pass


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
    )


def _emitir_fgts_certidao(certidao_id, driver=None, execution_id=None):
    if execution_id:
        CorrelationContext.set_execution_id(execution_id)

    inicio_fluxo = time.time()
    if _fgts_stop_requested():
        return False, False, 'Lote interrompido.'

    certidao = Certidao.query.get(certidao_id)
    if not certidao:
        return False, False, 'Certidão não encontrada.'

    info_site = SITES_CERTIDOES.get('FGTS', {})
    if not info_site.get('url'):
        return False, True, 'Configuração FGTS ausente.'

    local_driver = driver
    criado_localmente = False
    try:
        log_event('fgts_emit_start', certidao_id=certidao_id, empresa_id=certidao.empresa_id)
        if _fgts_stop_requested():
            return False, False, 'Lote interrompido.'

        if local_driver is None:
            local_driver = _criar_driver_chrome()
            criado_localmente = True

        FGTS_BATCH_STATE['driver'] = local_driver

        pagina_ok = _preparar_pagina_fgts(
            local_driver,
            info_site.get('url'),
            info_site.get('cnpj_field_id')
        )

        if not pagina_ok:
            return False, True, 'Erro ao carregar página FGTS.'

        wait = WebDriverWait(local_driver, 20)

        field_by = By.ID
        campo_cnpj = wait.until(EC.element_to_be_clickable(
            (field_by, info_site.get('cnpj_field_id'))))
        if _fgts_stop_requested():
            return False, False, 'Lote interrompido.'
        campo_cnpj.click()
        cnpj_limpo = _normalizar_cnpj(certidao.empresa.cnpj)
        campo_cnpj.send_keys(cnpj_limpo)

        contexto = {
            'arquivo_salvo_msg': None,
            'pular_monitoramento': False,
            'data_encontrada': None,
            'impedimento_fgts': False,
            'impedimento_msg': None,
        }

        scope_atual = (FGTS_BATCH_STATE.get('scope') or 'default').strip().lower()
        detectar_impedimento = (
            FGTS_BATCH_STATE.get('status') == 'running'
            and scope_atual in {'default', 'pendentes'}
        )

        _automatizar_fgts(contexto, local_driver, wait, certidao, detectar_impedimento)

        if _fgts_stop_requested():
            return False, False, 'Lote interrompido.'

        if contexto.get('impedimento_fgts'):
            msg_impedimento = contexto.get('impedimento_msg') or 'Certidão FGTS mantida como pendente.'

            if scope_atual == 'default':
                marcado, msg_marcacao = _fgts_marcar_pendente_por_impedimento(certidao, msg_impedimento)
                if not marcado:
                    return False, True, msg_marcacao
                return False, False, msg_marcacao

            return False, False, msg_impedimento

        if contexto.get('pdf_classificacao') == 'positiva':
            msg_positiva = contexto.get('pdf_msg') or 'Certidão FGTS detectada como POSITIVA e marcada como PENDENTE.'

            with FGTS_BATCH_LOCK:
                FGTS_BATCH_STATE['last_completed'] = {
                    'certidao_id': certidao.id,
                    'data_formatada': 'PENDENTE',
                    'nova_classe': 'status-vermelho'
                }
                batch_engine.append_batch_message(
                    FGTS_BATCH_STATE,
                    f"FGTS ID={certidao.id} positiva: marcada como pendente.",
                    level='warning',
                    certidao_id=certidao.id,
                )

            return True, False, msg_positiva

        if contexto.get('pdf_classificacao') == 'erro':
            msg_pdf = contexto.get('pdf_msg') or 'Erro ao tratar certidão FGTS positiva.'
            return False, True, msg_pdf

        if contexto.get('arquivo_salvo_msg'):
            nova_data = calcular_validade_padrao(certidao, contexto.get('data_encontrada'))
            if nova_data:
                try:
                    certidao.data_validade = nova_data
                    certidao.status_especial = None
                    db.session.commit()
                except Exception as e_db:
                    db.session.rollback()
                    print(f"[FGTS] Aviso: não foi possível salvar validade no banco: {e_db}")

            with FGTS_BATCH_LOCK:
                FGTS_BATCH_STATE['last_completed'] = {
                    'certidao_id': certidao.id,
                    'data_formatada': nova_data.strftime('%d/%m/%Y') if nova_data else None,
                    'nova_classe': _fgts_status_por_data(nova_data)
                }
            log_event(
                'fgts_emit_success',
                certidao_id=certidao_id,
                empresa_id=certidao.empresa_id,
                duration_ms=int((time.time() - inicio_fluxo) * 1000),
                status='ok',
            )
            return True, False, None
        return False, False, 'Falha ao gerar PDF FGTS.'
    except Exception as exc:
        log_event(
            'fgts_emit_error',
            level='ERROR',
            certidao_id=certidao_id,
            empresa_id=certidao.empresa_id if certidao else None,
            duration_ms=int((time.time() - inicio_fluxo) * 1000),
            error_type=map_exception_to_error_type(exc).value,
            error=str(exc),
        )
        return False, True, f'Erro grave no FGTS: {exc}'
    finally:
        if criado_localmente:
            FGTS_BATCH_STATE['driver'] = None
        if criado_localmente and local_driver:
            try:
                local_driver.quit()
            except Exception:
                pass


def _fgts_batch_worker(app):
    def _recover(certidao_id, execution_id, driver, sucesso, grave, mensagem):
        # FGTS: recria o driver e tenta de novo apos falha de carregamento da pagina
        if grave and mensagem == 'Erro ao carregar página FGTS.':
            try:
                driver.quit()
            except Exception:
                pass
            driver = _criar_driver_chrome()
            print("[FGTS-LOTE] Recriando driver após falha de carregamento.")
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
    )


def _rs_lote_precondicao():
    if not _to_bool(_get_config_value('RS_ALTCHA_AUTOSOLVE_ENABLED', False), False):
        return _json_error('Ative RS_ALTCHA_AUTOSOLVE_ENABLED para usar lote Estadual RS.', 400)
    return None


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
        if tag:
            print(f"[{tag}] Lote iniciado. Total={dados_lote['total']}.")
        with lock:
            batch_engine.append_batch_message(
                state, f"Lote {nome} iniciado. Total={dados_lote['total']}.", level='info')
        return jsonify({'status': 'ok'})

    def pausar():
        driver = batch_engine.request_pause(lock, state)
        if tag:
            print(f"[{tag}] Pausa solicitada.")
        with lock:
            batch_engine.append_batch_message(
                state, f"Lote {nome} pausado por solicitação.", level='warning')
        _fgts_quit_driver_async(driver)
        return jsonify({'status': 'ok', 'message': cfg['msg_pausado']})

    def parar():
        driver = batch_engine.request_stop(lock, state)
        if tag:
            print(f"[{tag}] Parada solicitada.")
        with lock:
            batch_engine.append_batch_message(
                state, f"Lote {nome} interrompido por solicitação.", level='warning')
        _fgts_quit_driver_async(driver)
        return jsonify({'status': 'ok', 'message': cfg['msg_interrompido']})

    def retomar():
        if not batch_engine.resume_batch(lock, state, worker, app_factory=_current_app_object):
            return _json_error(cfg['msg_nao_pausado'], 400)
        if tag:
            print(f"[{tag}] Retomada solicitada.")
        with lock:
            batch_engine.append_batch_message(
                state, f"Lote {nome} retomado por solicitação.", level='info')
        return jsonify({'status': 'ok'})

    def status_view():
        return jsonify(batch_engine.status_payload_locked(lock, state))

    bp.add_url_rule(f'{prefix}/lote/info/<int:certidao_id>', f'{endpoint_base}_info', info)
    bp.add_url_rule(f'{prefix}/lote/iniciar', f'{endpoint_base}_iniciar', iniciar, methods=['POST'])
    bp.add_url_rule(f'{prefix}/lote/pausar', f'{endpoint_base}_pausar', pausar, methods=['POST'])
    bp.add_url_rule(f'{prefix}/lote/parar', f'{endpoint_base}_parar', parar, methods=['POST'])
    bp.add_url_rule(f'{prefix}/lote/retomar', f'{endpoint_base}_retomar', retomar, methods=['POST'])
    bp.add_url_rule(f'{prefix}/lote/status', f'{endpoint_base}_status', status_view)


_register_batch_routes('/fgts', 'fgts_lote', {
    'lock': FGTS_BATCH_LOCK, 'state': FGTS_BATCH_STATE,
    'worker': _fgts_batch_worker, 'calc_targets': _calc_fgts_targets_by_scope,
    'started_event': 'fgts_batch_started', 'tag': 'FGTS-LOTE', 'nome_lote': 'FGTS',
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
    'precondicao': _rs_lote_precondicao,
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
    'started_event': 'municipal_batch_started', 'tag': None, 'nome_lote': 'Municipal',
    'msg_em_andamento': 'Já existe um lote Municipal em andamento.',
    'msg_vazio_pendentes': 'Nenhuma certidão Municipal pendente para emissão.',
    'msg_vazio_default': 'Nenhuma certidão Municipal vencida ou a vencer.',
    'msg_pausado': 'Lote Municipal pausado.',
    'msg_interrompido': 'Lote Municipal interrompido.',
    'msg_nao_pausado': 'Lote Municipal não está pausado.',
})


@bp.route('/health')
def health():
    checks = run_health_checks(current_app.config)
    has_failure = any(not item.get('ok') for item in checks.values())
    code = 200 if not has_failure else 503
    return jsonify({'status': 'ok' if not has_failure else 'degraded', 'checks': checks}), code


@bp.route('/fgts/emitir_unico', methods=['POST'])
def fgts_emitir_unico():
    dados = request.get_json() or {}
    certidao_id = dados.get('certidao_id')

    if not certidao_id:
        return _json_error('Certidão inválida.', 400)

    with FGTS_BATCH_LOCK:
        if FGTS_BATCH_STATE['status'] == 'running':
            return _json_error('Lote em andamento. Pare o lote para emitir individual.', 400)

    execution_id = CorrelationContext.new_execution_id()
    sucesso, grave, mensagem = _emitir_fgts_certidao(certidao_id, execution_id=execution_id)

    if grave:
        return _json_error(mensagem or 'Erro grave no FGTS.', 500)

    if not sucesso:
        return _json_error(mensagem or 'Falha ao emitir certidão FGTS.', 400)

    certidao = Certidao.query.get(certidao_id)
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


def calcular_validade_padrao(certidao, data_encontrada=None):
    if data_encontrada is not None:
        return data_encontrada

    tipo_chave = certidao.tipo.name
    hoje = date.today()

    if tipo_chave == 'MUNICIPAL':
        return None

    if tipo_chave in ['TRABALHISTA', 'FGTS', 'FEDERAL']:
        cfg = VALIDADES_CERTIDOES.get(tipo_chave) or {}
        dias = cfg.get('validade_dias_padrao')
        if dias:
            return hoje + timedelta(days=dias)
        return None

    if tipo_chave == 'ESTADUAL':
        estado = (certidao.empresa.estado or '').strip().upper()
        estadual_cfg = VALIDADES_CERTIDOES.get('ESTADUAL', {})
        uf_cfg = estadual_cfg.get(estado) or {}
        dias = uf_cfg.get('validade_dias_padrao')
        if dias:
            return hoje + timedelta(days=dias)
        return None

    return None


def _rs_get_page_state(driver):
    try:
        url = (driver.current_url or '').strip()
    except Exception:
        url = ''

    try:
        title = (driver.title or '').strip()
    except Exception:
        title = ''

    try:
        body_text = (driver.find_element(By.TAG_NAME, 'body').text or '').strip().lower()
    except Exception:
        body_text = ''

    return {
        'url': url,
        'title': title,
        'body_text': body_text,
    }


def _rs_sessao_expirada(driver):
    state = _rs_get_page_state(driver)
    url = state['url'].lower()
    body = state['body_text']

    if 'finalizarlogincert.aspx?exit=1' in url:
        return True

    marcadores = (
        'sessao expirou',
        'sessão expirou',
        'tempo de inatividade',
        'feche a janela do seu navegador e acesse-o novamente',
    )
    return any(marcador in body for marcador in marcadores)


def _rs_certidao_em_processamento(driver):
    state = _rs_get_page_state(driver)
    texto = file_manager.remover_acentos(
        f"{state.get('title', '')} {state.get('body_text', '')}"
    ).upper()

    if 'CERTIDAO EM PROCESSAMENTO' in texto:
        return True

    # Mensagem comum da pagina de resultado do RS.
    return 'CONSULTE NOVAMENTE EM ALGUNS MINUTOS' in texto


def _rs_fechar_abas_processamento(driver, handle_principal=None):
    if not driver:
        return False

    try:
        handles = list(driver.window_handles)
    except Exception:
        return False

    if not handles:
        return False

    principal = handle_principal if handle_principal in handles else handles[0]
    encontrou_processamento = False

    for handle in list(handles):
        try:
            driver.switch_to.window(handle)
        except Exception:
            continue

        if _rs_certidao_em_processamento(driver):
            encontrou_processamento = True
            if handle != principal:
                try:
                    driver.close()
                except Exception:
                    pass

    try:
        restantes = list(driver.window_handles)
        if principal in restantes:
            driver.switch_to.window(principal)
        elif restantes:
            driver.switch_to.window(restantes[0])
    except Exception:
        pass

    return encontrou_processamento

def _login_certificado_rs(driver, login_url, cert_url, timeout=120):
    driver.get(login_url)

    try:
        ok_btn = WebDriverWait(driver, 2).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "input[name='Action'][value='OK']"))
        )
        ok_btn.click()
    except Exception:
        pass

    time.sleep(2)
    driver.get(cert_url)

    try:
        wait_timeout = max(2, min(int(timeout or 20), 30))
        WebDriverWait(driver, wait_timeout).until(
            lambda d: (d.execute_script('return document.readyState') or '') == 'complete'
        )
    except Exception:
        pass


def _erro_indica_navegador_fechado(exc):
    tipos_fechamento = (
        InvalidSessionIdException,
        NoSuchWindowException,
        WebDriverException,
        ConnectionResetError,
    )
    marcadores = (
        'connection aborted',
        'connectionreseterror',
        'chrome not reachable',
        'disconnected',
        'invalid session id',
        'no such window',
        'target window already closed',
        'web view not found',
    )

    atual = exc
    for _ in range(6):
        if atual is None:
            break

        if isinstance(atual, tipos_fechamento):
            return True

        texto = f"{type(atual).__name__}: {atual}".lower()
        if any(marcador in texto for marcador in marcadores):
            return True

        atual = getattr(atual, '__cause__', None) or getattr(atual, '__context__', None)

    return False


def _normalizar_cidade_dashboard(valor):
    texto = (valor or '').strip()
    if not texto:
        return ''
    return file_manager.remover_acentos(texto).upper()


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

@bp.route('/')
def dashboard():
    status_filtros = request.args.getlist('status')
    tipo_filtros = request.args.getlist('tipo')
    estado_filtro = request.args.get('estado', '')
    cidade_filtro = (request.args.get('cidade', '') or '').strip()
    ordem = (request.args.get('ordem') or 'urgencia').strip().lower()

    query = db.session.query(Empresa).distinct()

    hoje = date.today()
    a_vencer_dias = get_a_vencer_dias()
    if ordem not in {'urgencia', 'az', 'vencimento'}:
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

    tipo_set = None
    if tipo_filtros and 'todas' not in tipo_filtros:
        tipo_set = set(tipo_filtros)

    certidoes_por_empresa = {}
    for empresa in empresas:
        certidoes = list(empresa.certidoes)
        if tipo_set:
            certidoes = [
                cert for cert in certidoes
                if cert.tipo and cert.tipo.name.lower() in tipo_set
            ]
        certidoes_por_empresa[empresa.id] = certidoes

    def _status_certidao(certidao):
        if certidao.status_especial == StatusEspecial.PENDENTE:
            return 'pendentes'
        if not certidao.data_validade:
            return 'nao_definida'
        if certidao.data_validade < hoje:
            return 'vencidas'
        if certidao.status == 'amarelo':
            return 'a_vencer'
        return 'validas'

    def _urgencia_bucket(empresa):
        certidoes = certidoes_por_empresa.get(empresa.id, [])
        tem_vencida = False
        tem_a_vencer = False
        tem_pendente = False
        tem_nao_definida = False
        for certidao in certidoes:
            status = _status_certidao(certidao)
            if status == 'vencidas':
                tem_vencida = True
            elif status == 'a_vencer':
                tem_a_vencer = True
            elif status == 'pendentes':
                tem_pendente = True
            elif status == 'nao_definida':
                tem_nao_definida = True
        if tem_vencida:
            return 0
        if tem_a_vencer:
            return 1
        if tem_pendente:
            return 2
        if tem_nao_definida:
            return 3
        return 4

    def _nome_empresa(empresa):
        return (empresa.nome or '').strip().upper()

    def _menor_validade(empresa):
        certidoes = certidoes_por_empresa.get(empresa.id, [])
        datas = [
            cert.data_validade for cert in certidoes
            if cert.data_validade and cert.status_especial != StatusEspecial.PENDENTE
        ]
        return min(datas) if datas else date.max

    if ordem == 'az':
        empresas.sort(key=_nome_empresa)
    elif ordem == 'vencimento':
        empresas.sort(key=lambda emp: (_menor_validade(emp), _nome_empresa(emp)))
    else:
        empresas.sort(key=lambda emp: (_urgencia_bucket(emp), _nome_empresa(emp)))

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
        status_filtros=status_filtros,
        tipo_filtros=tipo_filtros,
        estado_filtro=estado_filtro,
        cidade_filtro=cidade_filtro,
        estados_disponiveis=estados_disponiveis,
        cidades_disponiveis=cidades_disponiveis,
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
    except Exception as exc:
        db.session.rollback()
        flash(f'Erro ao atualizar empresa: {exc}', 'danger')

    return redirect(next_url)


@bp.route('/empresa/<int:empresa_id>/remover', methods=['GET', 'POST'])
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
    except Exception as exc:
        db.session.rollback()
        flash(f'Erro ao remover empresa: {exc}', 'danger')

    return redirect(next_url)


@bp.route('/empresa/nova', endpoint='nova_empresa')
def pagina_nova_empresa():
    return render_template('nova_empresa.html')


@bp.route('/relatorios')
def relatorios():
    hoje = date.today()
    a_vencer_dias = get_a_vencer_dias()
    empresas_total = Empresa.query.count()
    certidoes = Certidao.query.all()

    total_certidoes = len(certidoes)
    pendentes = 0
    vencidas = 0
    a_vencer = 0

    for certidao in certidoes:
        if certidao.status_especial == StatusEspecial.PENDENTE:
            pendentes += 1
            continue

        if not certidao.data_validade:
            continue

        dias_restantes = (certidao.data_validade - hoje).days
        if dias_restantes < 0:
            vencidas += 1
        elif certidao.status == 'amarelo':
            a_vencer += 1

    return render_template(
        'relatorios.html',
        empresas_total=empresas_total,
        total_certidoes=total_certidoes,
        pendentes=pendentes,
        vencidas=vencidas,
        a_vencer=a_vencer,
        a_vencer_dias=a_vencer_dias,
    )


_TIPOS_VENCER = [
    ('federal', 'Federal', 'a_vencer_dias_federal'),
    ('fgts', 'FGTS', 'a_vencer_dias_fgts'),
    ('estadual', 'Estadual', 'a_vencer_dias_estadual'),
    ('municipal', 'Municipal', 'a_vencer_dias_municipal'),
    ('trabalhista', 'Trabalhista', 'a_vencer_dias_trabalhista'),
]


@bp.route('/configuracoes', methods=['GET', 'POST'])
def configuracoes():
    try:
        config = ConfiguracaoSistema.query.get(1)
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

        try:
            db.session.commit()
            flash('Configuracoes atualizadas com sucesso.', 'success')
        except Exception as exc:
            db.session.rollback()
            flash(f'Erro ao salvar configuracoes: {exc}', 'danger')

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
    )


@bp.route('/empresa/adicionar', methods=['POST'])
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
    except Exception as e:
        db.session.rollback()
        flash(f'Erro ao cadastrar empresa: {e}', 'danger')

    return _redirect_apos_cadastro()


@bp.route('/certidao/atualizar/<int:certidao_id>', methods=['POST'])
def atualizar_validade(certidao_id):
    certidao = Certidao.query.get_or_404(certidao_id)
    nova_data_str = request.form.get('nova_validade')

    if nova_data_str:
        nova_data = datetime.strptime(nova_data_str, '%Y-%m-%d').date()
        ok, erro = certidao_service.aplicar_validade(certidao, nova_data)
        if ok:
            flash(
                f"Validade da certidão {certidao.tipo.value} da empresa {certidao.empresa.nome} atualizada com sucesso!", 'success')
        else:
            flash(f"Erro ao atualizar validade: {erro}", 'danger')
    else:
        flash("Nenhuma data foi fornecida.", 'warning')
    return redirect(url_for('main.dashboard'))


@bp.route('/certidao/marcar_pendente/<int:certidao_id>', methods=['POST'])
def marcar_pendente(certidao_id):
    certidao = Certidao.query.get_or_404(certidao_id)
    ok, erro = certidao_service.marcar_pendente(certidao)
    if ok:
        flash(
            f'Certidão {certidao.tipo.value} da empresa {certidao.empresa.nome} marcada como Pendente.', 'info')
    else:
        flash(f'Erro ao marcar como pendente: {erro}', 'danger')

    return redirect(url_for('main.dashboard'))

def _automatizar_fgts(contexto, driver, wait, certidao, detectar_impedimento=False):
    def _parar_se_solicitado():
        if _fgts_stop_requested():
            try:
                driver.quit()
            except Exception:
                pass
            return True
        return False

    def _aguardar_clickable(locator, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _parar_se_solicitado():
                return None
            try:
                return WebDriverWait(driver, 1).until(
                    EC.element_to_be_clickable(locator)
                )
            except TimeoutException:
                continue
        return None

    def _marcar_impedimento_e_sair(mensagem):
        contexto['impedimento_fgts'] = True
        contexto['impedimento_msg'] = mensagem
        _fgts_fechar_abas_extras(driver)
        print(f"[FGTS][PENDENTES] {mensagem}")

    try:
        btn_consultar = _aguardar_clickable((By.ID, "mainForm:btnConsultar"))
        if not btn_consultar:
            return
        print("clicando em Consultar")
        if _parar_se_solicitado():
            return
        btn_consultar.click()
        time.sleep(1)

        if detectar_impedimento:
            msg_impedimento = _fgts_detectar_mensagem_impedimento(driver)
            if msg_impedimento:
                _marcar_impedimento_e_sair(msg_impedimento)
                return

        btn_certificado = _aguardar_clickable((By.ID, "mainForm:j_id76"))
        if not btn_certificado:
            if detectar_impedimento:
                msg_impedimento = _fgts_detectar_mensagem_impedimento(driver)
                if msg_impedimento:
                    _marcar_impedimento_e_sair(msg_impedimento)
            return
        print("clicando em Certificado")
        if _parar_se_solicitado():
            return
        btn_certificado.click()
        time.sleep(1)

        if detectar_impedimento:
            msg_impedimento = _fgts_detectar_mensagem_impedimento(driver)
            if msg_impedimento:
                _marcar_impedimento_e_sair(msg_impedimento)
                return

        # tentar localizar data de validade na página
        if not contexto.get('data_encontrada'):
            try:
                elemento = driver.find_element(
                    By.XPATH, "//p[contains(., 'Validade:')]")
                texto = elemento.text
                if " a " in texto:
                    parte_data = texto.split(" a ")[-1].strip()[:10]
                    data_val = datetime.strptime(
                        parte_data, '%d/%m/%Y').date()
                    contexto['data_encontrada'] = data_val
            except Exception as e:
                if _fgts_stop_requested():
                    return
                print(f"erro ao encontrar data fgts: {e}")

        btn_visualizar = _aguardar_clickable((By.ID, "mainForm:btnVisualizar"))
        if not btn_visualizar:
            if detectar_impedimento:
                msg_impedimento = _fgts_detectar_mensagem_impedimento(driver)
                if msg_impedimento:
                    _marcar_impedimento_e_sair(msg_impedimento)
            return
        print("clicando em Visualizar")
        if _parar_se_solicitado():
            return
        btn_visualizar.click()
        time.sleep(1)

        # gerar pdf automaticamente com CDP
        def _gerar_nome_pdf_aleatorio(tamanho: int = 10) -> str:
            return ''.join(random.choices(string.ascii_letters + string.digits, k=tamanho))

        def _caminho_pdf_downloads_unico() -> str:
            pasta_downloads = os.path.join(os.path.expanduser("~"), "Downloads")
            for _ in range(50):
                nome = f"{_gerar_nome_pdf_aleatorio(10)}.pdf"
                caminho = os.path.join(pasta_downloads, nome)
                if not os.path.exists(caminho):
                    return caminho
            return os.path.join(pasta_downloads, f"{int(time.time())}_{_gerar_nome_pdf_aleatorio(6)}.pdf")

        def _aguardar_pagina_certidao_fgts():
            try:
                WebDriverWait(driver, 20).until(
                    lambda d: (
                        d.execute_script("return document.readyState") == "complete"
                        and (
                            len(d.find_elements(By.XPATH, "//button[contains(., 'Imprimir')] | //input[@value='Imprimir']")) > 0
                            or "CERTIFICADO" in (d.page_source or "").upper()
                        )
                    )
                )
            except Exception as _e:
                print(f"[FGTS] aviso: não confimou âncora da página: {_e}")

        def _gerar_pdf_da_pagina() -> str:
            try:
                try:
                    driver.execute_cdp_cmd('Page.enable', {})
                except Exception:
                    pass

                result = driver.execute_cdp_cmd('Page.printToPDF', {
                    'printBackground': True,
                    'preferCSSPageSize': True
                })
                data = (result or {}).get('data')
                if data:
                    return data
            except Exception as e_cdp:
                print(f"[FGTS] CDP printToPDF falhou, tentando print_page: {e_cdp}")

            return driver.print_page()

        try:
            if _parar_se_solicitado():
                return
            _aguardar_pagina_certidao_fgts()

            pdf_b64 = _gerar_pdf_da_pagina()
            if not pdf_b64:
                raise ValueError("PDF base64 vazio")

            caminho_pdf = _caminho_pdf_downloads_unico()
            with open(caminho_pdf, 'wb') as f:
                f.write(base64.b64decode(pdf_b64))

            print(f"[FGTS] PDF gerado em Downloads: {caminho_pdf}")

            sucesso, msg = file_manager.mover_e_renomear(
                caminho_pdf,
                certidao.empresa.nome,
                certidao.tipo.value
            )

            if sucesso:
                contexto['arquivo_salvo_msg'] = f"Arquivo salvo em: {msg}"
                contexto['pular_monitoramento'] = True
                print(contexto['arquivo_salvo_msg'])
                try:
                    certidao.caminho_arquivo = msg
                    db.session.commit()
                except Exception as e_db:
                    db.session.rollback()
                    print(f"[FGTS] Aviso: não foi possível salvar caminho no banco: {e_db}")
                classificacao_pdf, msg_pdf = pdf.classificar_e_tratar_positivo(
                    certidao,
                    msg,
                    origem_log='FGTS',
                    tipo_label=certidao.tipo.value,
                )
                if classificacao_pdf in {'positiva', 'erro'}:
                    contexto['pdf_classificacao'] = classificacao_pdf
                    contexto['pdf_msg'] = msg_pdf
                    contexto['arquivo_salvo_msg'] = None
                    contexto['data_encontrada'] = None
        except Exception as e_pdf:
            print(f"[FGTS] Erro ao gerar PDF automaticamente: {e_pdf}")
    except Exception as e:
        if _fgts_stop_requested():
            return
        print(f"erro automação emissao FGTS: {e}")


def _carregar_config_municipio(regra_municipio):
    if not regra_municipio:
        return None
    raw = regra_municipio.config_automacao
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        print(f"[MUNICIPAL] Config inválida para {regra_municipio.nome}: {exc}")
        return None


def _buscar_municipio_por_cidade(cidade):
    cidade_norm = file_manager.remover_acentos((cidade or '').strip()).upper()
    if not cidade_norm:
        return None

    for municipio in Municipio.query.all():
        nome_norm = file_manager.remover_acentos(municipio.nome or '').upper()
        if nome_norm == cidade_norm:
            return municipio
    return None


def _resolve_imbe_tipo_from_subtipo(cert_subtipo):
    if cert_subtipo == SubtipoCertidao.GERAL:
        return 'geral'
    if cert_subtipo == SubtipoCertidao.MOBILIARIO:
        return 'mobiliario'
    return ''


def _nome_certidao_imbe(nome_padrao, tipo_escolhido):
    if tipo_escolhido == 'geral':
        return 'CERTIDAO MUNICIPAL'
    if tipo_escolhido == 'mobiliario':
        return 'CERTIDAO MOBILIARIO'
    return nome_padrao


def _aplicar_variantes_imbe(info_site_cfg, config_cfg, tipo_escolhido):
    if tipo_escolhido != 'geral':
        return
    cfg_geral = (((config_cfg or {}).get('imbe_variantes') or {}).get('geral') or {})
    info_site_cfg['url'] = cfg_geral.get(
        'url',
        'https://grp.imbe.rs.gov.br/grp/acessoexterno/programaAcessoExterno.faces?codigo=684509'
    )
    info_site_cfg['cnpj_field_id'] = cfg_geral.get('cnpj_field_id', 'form:cnpjD')
    info_site_cfg['by'] = cfg_geral.get('by', 'name')
    info_site_cfg['pre_fill_click_id'] = cfg_geral.get(
        'pre_fill_click_id',
        info_site_cfg.get('pre_fill_click_id')
    )
    info_site_cfg['pre_fill_click_by'] = cfg_geral.get(
        'pre_fill_click_by',
        info_site_cfg.get('pre_fill_click_by')
    )
    info_site_cfg['inscricao_field_id'] = None
    info_site_cfg['inscricao_field_by'] = None
    if config_cfg is None:
        return

    after_cnpj = config_cfg.get('after_cnpj') or []
    already_has_tab = any((step or {}).get('tipo') == 'press_tab' for step in after_cnpj)
    if not already_has_tab:
        after_cnpj.append({
            'tipo': 'press_tab',
            'by': info_site_cfg.get('by', 'name'),
            'locator': info_site_cfg.get('cnpj_field_id'),
            'sleep': 0.4
        })
    config_cfg['after_cnpj'] = after_cnpj


def _calcular_validade_municipal(regra_municipio):
    if regra_municipio and regra_municipio.validade_dias:
        return date.today() + timedelta(days=regra_municipio.validade_dias)
    return None


@bp.route('/certidao/baixar/<int:certidao_id>')
def baixar_certidao(certidao_id):
    file_manager.criar_chave_interrupcao()
    certidao = Certidao.query.get_or_404(certidao_id)
    tipo_certidao_chave = certidao.tipo.name

    if tipo_certidao_chave == 'ESTADUAL' and (certidao.empresa.estado or '').strip().upper() == 'RS':
        with RS_BATCH_LOCK:
            if RS_BATCH_STATE['status'] in ['running', 'paused']:
                return _json_error(
                    'Lote Estadual RS em andamento. Aguarde finalizar ou interrompa o lote.',
                    400,
                )

    by_map = steps.BY_MAP

    def _get_by(key):
        return by_map.get(key)

    def _calcular_validade_sem_data(tipo_chave, regra):
        if tipo_chave == 'MUNICIPAL':
            if regra and regra.validade_dias:
                return date.today() + timedelta(days=regra.validade_dias)
            return None
        return calcular_validade_padrao(certidao, None)

    regra_municipio = None
    config_municipal = None
    usar_config_municipal = False
    imbe_tipo = (request.args.get('imbe_tipo') or '').strip().lower()

    if not imbe_tipo and certidao.subtipo:
        imbe_tipo = _resolve_imbe_tipo_from_subtipo(certidao.subtipo)

    if tipo_certidao_chave == 'FEDERAL':
        return redirect("https://servicos.receitafederal.gov.br/servico/certidoes/#/home/cnpj")

    info_site = {}
    if tipo_certidao_chave != 'MUNICIPAL':
        if tipo_certidao_chave == 'ESTADUAL':
            estado_emp = (certidao.empresa.estado or '').strip().upper()
            estadual_cfg = SITES_CERTIDOES.get('ESTADUAL', {})
            if isinstance(estadual_cfg, dict) and estado_emp in estadual_cfg:
                info_site = estadual_cfg[estado_emp].copy()
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
                return _json_error(
                    "Automação desativada para este município. Use o botão 'Abrir Site'.",
                    409,
                    status='manual_required',
                )

            config_municipal = _carregar_config_municipio(regra_municipio)
            usar_config_municipal = bool(config_municipal)

            cidade_regra_norm = file_manager.remover_acentos(regra_municipio.nome or '').upper()
            if cidade_regra_norm == 'IMBE':
                if imbe_tipo not in ['mobiliario', 'geral']:
                    return _json_error(
                        'Para Imbé, selecione no modal: Certidão Municipal Mobiliário ou Geral.',
                        409,
                        status='manual_required',
                    )

                _aplicar_variantes_imbe(info_site, config_municipal, imbe_tipo)

            if usar_config_municipal and config_municipal.get('skip_cnpj_fill'):
                info_site['cnpj_field_id'] = None
            
        else:
            return _json_error('Regra municipal não encontrada', 404)

    if tipo_certidao_chave == 'MUNICIPAL' and not usar_config_municipal:
        return _json_error('Municipio sem automacao. Configure para prosseguir.', 409)

    cnpj_limpo = _normalizar_cnpj(certidao.empresa.cnpj)
    inscricao_limpa = certidao.empresa.inscricao_mobiliaria or ''

    nome_certidao_arquivo = certidao.tipo.value
    if tipo_certidao_chave == 'MUNICIPAL' and regra_municipio:
        cidade_regra_norm = file_manager.remover_acentos(regra_municipio.nome or '').upper()
        if cidade_regra_norm == 'IMBE':
            nome_certidao_arquivo = _nome_certidao_imbe(nome_certidao_arquivo, imbe_tipo)

    driver = None
    data_encontrada = None
    arquivo_salvo_msg = None
    pular_monitoramento = False
    rs_autoselect_temporario_ativo = False
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
    estado_emp = (certidao.empresa.estado or '').strip().upper()
    usar_rs_autoselect = (
        tipo_certidao_chave == 'ESTADUAL'
        and estado_emp == 'RS'
        and bool(info_site.get('login_cert_url'))
    )

    try:
        print(f"--- INICIANDO AUTOMAÇÃO ({tipo_certidao_chave}) ---")

        if usar_rs_autoselect:
            rs_autoselect_temporario_ativo = _ativar_politica_autoselect_rs_temporaria()

        driver = _criar_driver_chrome(
            anonimo=not usar_rs_autoselect,
            usar_perfil=usar_rs_autoselect
        )
        
        wait = WebDriverWait(driver, 20)

        if tipo_certidao_chave == 'ESTADUAL' and estado_emp == 'RS' and info_site.get('login_cert_url'):
            print("1. Acessando login com certificado (RS)")
            _login_certificado_rs(
                driver,
                info_site.get('login_cert_url'),
                info_site.get('url')
            )
            print('pronto')
        else:
            print(f"1. Acessando a URL: {info_site.get('url')}")
            driver.get(info_site.get('url'))

        try:
            _configurar_download_automatico_chrome(driver)
        except Exception as exc:
            print(f"[DOWNLOAD] Falha ao reaplicar configuração de download automático: {exc}")
        
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
                    return jsonify({
                        'status': 'window_closed_no_file',
                        'certidao_id': certidao_id,
                        'tipo_certidao': nome_certidao_arquivo
                    })


        def executar_acao_aux(nome_acao):
            # 1 pre click inicial
            if nome_acao == 'pre_fill':
                if not info_site.get('pre_fill_click_id'):
                    return
                click_by = _get_by(info_site.get('pre_fill_click_by'))
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
                select_by = _get_by(info_site.get('tipo_select_by', 'id')) or By.ID
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
                    print("Select de tipo configurado com sucesso.")
                except Exception as e:
                    print(f"Aviso: não foi possível configurar select de tipo: {e}")

            #3 ação específica para FGTS: emitir e salvar PDF
            elif nome_acao == 'fgts_emitir_pdf':
                try:
                    _automatizar_fgts(contexto, driver, wait, certidao)
                except Exception as e:
                    print(f"[FGTS] Erro na ação fgts_emitir_pdf: {e}")

        # ordem das ações antes do cnpj
        steps_before_cnpj = info_site.get('steps_before_cnpj')
        if steps_before_cnpj is None:
            # padrão atual: pre_fill depois select_tipo
            steps_before_cnpj = ['pre_fill', 'select_tipo']

        for step in steps_before_cnpj:
            executar_acao_aux(step)

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
            print('[ESTADUAL-RS][ALTCHA] Emissão unitária em modo manual: resolva o captcha e clique em Enviar.')

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
                return jsonify({
                    'status': 'window_closed_no_file',
                    'certidao_id': certidao_id,
                    'tipo_certidao': nome_certidao_arquivo
                })

        # ordem das ações depois do cnpj
        steps_after_cnpj = info_site.get('steps_after_cnpj')
        if steps_after_cnpj is None:
            steps_after_cnpj = []
        for step in steps_after_cnpj:
            executar_acao_aux(step)

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
            print("--- AGUARDANDO DOWNLOAD OU FECHAMENTO ---")

            download_detectado = False

            while True:
                try:
                    driver.window_handles
                except Exception:
                    print("Janela fechada pelo usuário.")
                    break

                if not download_detectado:
                    novo_arquivo = file_manager.verificar_novo_arquivo(
                        tempo_inicio)
                    
                    if novo_arquivo:
                        print(f"Novo arquivo detectado: {novo_arquivo}")
                        download_detectado = True
                        sucesso, msg = file_manager.mover_e_renomear(
                            novo_arquivo,
                            certidao.empresa.nome,
                            nome_certidao_arquivo
                        )

                        if sucesso:
                            arquivo_salvo_msg = f"Arquivo salvo em: {msg}"
                            print(arquivo_salvo_msg)
                            try:
                                certidao.caminho_arquivo = msg
                                db.session.commit()
                            except Exception as e_db:
                                db.session.rollback()
                                print(f"Aviso: não foi possível salvar caminho no banco: {e_db}")

                            if tipo_certidao_chave == 'ESTADUAL' and estado_emp == 'RS':
                                rs_estadual_classificacao, rs_estadual_msg = pdf.classificar_e_tratar_positivo(
                                    certidao, msg, origem_log='ESTADUAL-RS', tipo_label='ESTADUAL RS'
                                )
                                print(f"[ESTADUAL-RS] Classificação do PDF: {rs_estadual_classificacao}")

                            if (
                                tipo_certidao_chave == 'MUNICIPAL'
                                and regra_municipio
                                and usar_config_municipal
                                and bool((config_municipal or {}).get('classificar_pdf_status'))
                            ):
                                origem_pdf = f"MUNICIPAL-{regra_municipio.nome}"
                                municipal_pdf_classificacao, municipal_pdf_msg = pdf.classificar_e_tratar_positivo(
                                    certidao, msg, origem_log=origem_pdf,
                                    tipo_label=f'MUNICIPAL ({regra_municipio.nome})'
                                )
                                print(f"[{origem_pdf}] Classificação do PDF: {municipal_pdf_classificacao}")

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
                                    certidao,
                                    msg,
                                    origem_log=origem_pdf,
                                    tipo_label=certidao.tipo.value,
                                )
                                if classificacao_pdf in {'positiva', 'erro'}:
                                    certidao_pdf_classificacao = classificacao_pdf
                                    certidao_pdf_msg = msg_pdf
                            try:
                                try:
                                    janelas_abertas = list(driver.window_handles)
                                except Exception:
                                    janelas_abertas = []

                                def _is_blank(url):
                                    url = (url or '').lower()
                                    return url == 'about:blank' or url == ''

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
                                    print(f"Aviso: erro ao fechar Chrome: {e_quit}")

                                break
                            except Exception as e:
                                print(f"Erro ao fechar Chrome: {e}")
                        else:
                            print(f"Erro ao salvar: {msg}")

                time.sleep(1)
        else:
            print("--- FGTS: monitoramento pulado (PDF gerado via CDP) ---")
            if driver:
                try:
                    time.sleep(1)
                    driver.quit()
                except Exception as e_quit:
                    print(f"Aviso: erro ao fechar Chrome no fluxo FGTS/CDP: {e_quit}")

    except Exception as e:
        print(f"!!!!!!!!!! ERRO NO SELENIUM !!!!!!!!!!\n{e}")
        if _erro_indica_navegador_fechado(e):
            print("Chrome fechado durante a automação; retornando fluxo pendente.")
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            return jsonify({
                'status': 'window_closed_no_file',
                'certidao_id': certidao_id,
                'tipo_certidao': nome_certidao_arquivo
            })
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        return _json_error("Ocorreu um erro na automação.", 500)
    finally:
        if rs_autoselect_temporario_ativo:
            _desativar_politica_autoselect_rs_temporaria()

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
            print(
                f"[DEBUG] Data de validade encontrada: {data_encontrada.strftime('%d/%m/%Y')}")
            response_data['nova_data'] = data_encontrada.strftime('%Y-%m-%d')
            response_data['data_formatada'] = data_encontrada.strftime(
                '%d/%m/%Y')
        else:
            data_calc = None

            data_calc = _calcular_validade_sem_data(tipo_certidao_chave, regra_municipio)

            if data_calc:
                print(
                    f"[DEBUG] Data de validade calculada: {data_calc.strftime('%d/%m/%Y')}")
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


@bp.route('/certidao/salvar_data_confirmada', methods=['POST'])
def salvar_data_confirmada():
    dados = request.get_json()
    certidao_id = dados.get('certidao_id')
    nova_validade_str = dados.get('nova_validade')

    try:
        certidao = Certidao.query.get(certidao_id)
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
        return _json_error(str(e), 500)


@bp.route('/certidao/monitorar_download_federal/<int:certidao_id>')
def monitorar_download_federal(certidao_id):
    certidao = Certidao.query.get_or_404(certidao_id)

    print(
        f"--- INICIANDO MONITORAMENTO DE DOWNLOAD (FEDERAL) - ID: {certidao_id} ---")

    file_manager.criar_chave_interrupcao()

    # Captura um snapshot antes de iniciar a janela de monitoramento
    # para detectar arquivos criados/alterados mesmo se o download iniciar cedo.
    snapshot_before = _snapshot_downloads_pdf()
    print(f"[FEDERAL][MONITOR] snapshot inicial: {len(snapshot_before)} pdf(s)")

    time.sleep(2)

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
            print(
                f"MONITORAMENTO FEDERAL (ID {certidao_id}) INTERROMPIDO POR NOVA REQUISIÇÃO.")
            file_manager.remover_chave_interrupcao()
            return _json_error('Monitoramento interrompido.', 409, status='interrupted')

        novo_arquivo = _pick_changed_download_pdf(snapshot_before)
        if not novo_arquivo:
            novo_arquivo = file_manager.verificar_novo_arquivo(
                tempo_inicio, termos_ignorar=termos_proibidos)

        agora = time.time()
        if (agora - ultimo_log) >= 5:
            restante = max(0, int(tempo_limite - (agora - tempo_inicio)))
            print(
                f"[FEDERAL][MONITOR] aguardando... restante={restante}s "
                f"| novo_arquivo={'sim' if novo_arquivo else 'nao'}"
            )
            ultimo_log = agora

        if novo_arquivo:
            print(f"Arquivo Federal detectado: {novo_arquivo}")

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
                    print(f"[FEDERAL] Aviso: não foi possível salvar caminho no banco: {e_db}")
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


@bp.route('/certidao/marcar_pendente_json/<int:certidao_id>', methods=['POST'])
def marcar_pendente_json(certidao_id):
    try:
        certidao = Certidao.query.get_or_404(certidao_id)
        ok, erro = certidao_service.marcar_pendente(certidao)
        if not ok:
            return _json_error(erro, 500)
        return jsonify({'status': 'success'})
    except Exception as e:
        db.session.rollback()
        return _json_error(str(e), 500)


@bp.route('/certidao/atualizar_json/<int:certidao_id>', methods=['POST'])
def atualizar_validade_json(certidao_id):
    data = request.get_json()
    nova_data_str = data.get('nova_validade')

    try:
        certidao = Certidao.query.get_or_404(certidao_id)

        if nova_data_str:
            nova_data = datetime.strptime(nova_data_str, '%Y-%m-%d').date()
            ok, erro = certidao_service.aplicar_validade(certidao, nova_data)
            if not ok:
                return _json_error(erro, 500)

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
        return _json_error(str(e), 500)
