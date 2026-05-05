import os
import json
import time
import string
import random
import base64
import re
import unicodedata
from threading import Thread, Lock
from datetime import date, datetime, timedelta

try:
    import winreg
except ImportError:
    winreg = None

from flask import (Blueprint, flash, jsonify, redirect, render_template,
                   request, url_for, send_file, current_app, g)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from selenium import webdriver
from selenium.common.exceptions import (InvalidSessionIdException,
                                        NoAlertPresentException,
                                        NoSuchWindowException,
                                        TimeoutException,
                                        WebDriverException)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.common.keys import Keys
from sqlalchemy import or_
from webdriver_manager.chrome import ChromeDriverManager
import pdfplumber

from app import db, file_manager
from app.automation import SITES_CERTIDOES, VALIDADES_CERTIDOES
from app.errors import map_exception_to_error_type
from app.models import (Certidao, Empresa, Municipio, StatusEspecial,
                        SubtipoCertidao, TipoCertidao)
from app.services import batch_engine
from app.services.correlation import CorrelationContext
from app.services.execution_logger import log_event
from app.services.health import run_health_checks
from app.services.retry import retry_call
from app.services.rs_altcha import (clicar_enviar_estadual_rs as _clicar_enviar_estadual_rs,
                                    resolver_altcha_rs_com_2captcha as _resolver_altcha_rs_com_2captcha)

bp = Blueprint('main', __name__)

FGTS_BATCH_LOCK = Lock()
RS_BATCH_LOCK = Lock()
RS_CERT_POLICY_LOCK = Lock()
RS_CERT_POLICY_ACTIVE_COUNT = 0

FGTS_BATCH_STATE = batch_engine.batch_state_defaults()

RS_BATCH_STATE = batch_engine.batch_state_defaults()


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
    is_batch_poll = path in {'/fgts/lote/status', '/estadual-rs/lote/status'}
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


def _fgts_status_por_data(nova_data):
    if not nova_data:
        return 'status-cinza'

    hoje = date.today()
    diferenca = (nova_data - hoje).days
    if diferenca < 0:
        return 'status-vermelho'
    if diferenca <= 7:
        return 'status-amarelo'
    return 'status-verde'


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

    if msg_insuficiente in texto_norm:
        return (
            'FGTS com informações insuficientes para comprovação automática. '
            'Mantida como PENDENTE e seguindo para a próxima empresa.'
        )

    if msg_nao_cadastrado in texto_norm:
        return 'Empregador não cadastrado no FGTS. Mantida como PENDENTE e seguindo para a próxima empresa.'

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


def _get_chrome_profile_settings():
    profile_dir = None
    profile_name = None

    try:
        profile_dir = current_app.config.get('CHROME_PROFILE_DIR')
        profile_name = current_app.config.get('CHROME_PROFILE_NAME')
    except RuntimeError:
        profile_dir = os.environ.get('CHROME_PROFILE_DIR')
        profile_name = os.environ.get('CHROME_PROFILE_NAME')

    if not profile_dir:
        profile_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), '..', 'chrome-profile')
        )
    if not profile_name:
        profile_name = 'Certidoes'

    os.makedirs(profile_dir, exist_ok=True)
    return profile_dir, profile_name


def _get_config_value(name, default=None):
    try:
        return current_app.config.get(name, default)
    except RuntimeError:
        return os.environ.get(name, default)


def _to_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on', 'sim'}


def _montar_politica_autoselect_rs():
    if not _to_bool(_get_config_value('RS_CERT_AUTOSELECT_ENABLED', True), True):
        return None

    pattern = (_get_config_value('RS_CERT_AUTOSELECT_PATTERN', 'https://www.sefaz.rs.gov.br') or '').strip()
    issuer_cn = (_get_config_value('RS_CERT_AUTOSELECT_ISSUER_CN', '') or '').strip()
    subject_cn = (_get_config_value('RS_CERT_AUTOSELECT_SUBJECT_CN', '') or '').strip()

    if not pattern:
        return None

    filtro = {}
    if issuer_cn:
        filtro['ISSUER'] = {'CN': issuer_cn}
    if subject_cn:
        filtro['SUBJECT'] = {'CN': subject_cn}

    if not filtro:
        return None

    return {
        'pattern': pattern,
        'filter': filtro,
    }


def _sincronizar_politica_autoselect_rs(aplicar=True):
    if os.name != 'nt' or winreg is None:
        return

    indice = str(_get_config_value('RS_CERT_AUTOSELECT_POLICY_INDEX', '1') or '1').strip() or '1'
    politica = _montar_politica_autoselect_rs() if aplicar else None
    chave_registro = r"Software\Policies\Google\Chrome\AutoSelectCertificateForUrls"

    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, chave_registro) as chave:
            if politica is None:
                try:
                    winreg.DeleteValue(chave, indice)
                    print(f"[RS] Política AutoSelectCertificate removida (índice {indice}).")
                except FileNotFoundError:
                    pass
                return

            valor = json.dumps(politica, ensure_ascii=False, separators=(',', ':'))
            winreg.SetValueEx(chave, indice, 0, winreg.REG_SZ, valor)
            print(f"[RS] Política AutoSelectCertificate aplicada (índice {indice}).")
    except OSError as exc:
        print(f"[RS] Não foi possível sincronizar a política AutoSelectCertificate: {exc}")


def _ativar_politica_autoselect_rs_temporaria():
    global RS_CERT_POLICY_ACTIVE_COUNT

    if not _montar_politica_autoselect_rs():
        return False

    with RS_CERT_POLICY_LOCK:
        RS_CERT_POLICY_ACTIVE_COUNT += 1
        if RS_CERT_POLICY_ACTIVE_COUNT == 1:
            _sincronizar_politica_autoselect_rs(aplicar=True)
    return True


def _desativar_politica_autoselect_rs_temporaria():
    global RS_CERT_POLICY_ACTIVE_COUNT

    with RS_CERT_POLICY_LOCK:
        if RS_CERT_POLICY_ACTIVE_COUNT <= 0:
            RS_CERT_POLICY_ACTIVE_COUNT = 0
            _sincronizar_politica_autoselect_rs(aplicar=False)
            return

        RS_CERT_POLICY_ACTIVE_COUNT -= 1
        if RS_CERT_POLICY_ACTIVE_COUNT == 0:
            _sincronizar_politica_autoselect_rs(aplicar=False)


def _build_chrome_options(anonimo=True, usar_perfil=False):
    chrome_options = Options()
    chrome_options.add_argument("--start-maximized")
    chrome_options.add_argument("--disable-features=DownloadBubble,DownloadBubbleV2")
    chrome_options.add_argument("--no-first-run")
    chrome_options.add_argument("--no-default-browser-check")

    downloads_dir = os.path.join(os.path.expanduser("~"), "Downloads")
    chrome_options.add_experimental_option('prefs', {
        'download.default_directory': downloads_dir,
        'download.prompt_for_download': False,
        'download.directory_upgrade': True,
        'download.open_pdf_in_system_reader': False,
        'profile.default_content_setting_values.automatic_downloads': 1,
        'safebrowsing.enabled': True,
        'plugins.always_open_pdf_externally': True,
    })

    if anonimo:
        chrome_options.add_argument("--incognito")

    if usar_perfil:
        profile_dir, profile_name = _get_chrome_profile_settings()
        if profile_dir:
            chrome_options.add_argument(f"--user-data-dir={profile_dir}")
        if profile_name:
            chrome_options.add_argument(f"--profile-directory={profile_name}")

    return chrome_options


def _configurar_download_automatico_chrome(driver):
    downloads_dir = os.path.join(os.path.expanduser("~"), "Downloads")

    try:
        driver.execute_cdp_cmd('Page.setDownloadBehavior', {
            'behavior': 'allow',
            'downloadPath': downloads_dir,
        })
    except Exception:
        pass

    try:
        driver.execute_cdp_cmd('Browser.setDownloadBehavior', {
            'behavior': 'allow',
            'downloadPath': downloads_dir,
            'eventsEnabled': False,
        })
    except Exception:
        pass

    try:
        info = driver.execute_cdp_cmd('Target.getTargetInfo', {})
        target_info = (info or {}).get('targetInfo') or {}
        browser_context_id = target_info.get('browserContextId')
        if browser_context_id:
            driver.execute_cdp_cmd('Browser.setDownloadBehavior', {
                'behavior': 'allow',
                'downloadPath': downloads_dir,
                'eventsEnabled': False,
                'browserContextId': browser_context_id,
            })
    except Exception:
        pass


def _criar_driver_chrome(anonimo=True, usar_perfil=False):
    chrome_options = _build_chrome_options(anonimo=anonimo, usar_perfil=usar_perfil)
    driver = webdriver.Chrome(service=ChromeService(
        ChromeDriverManager().install()), options=chrome_options)

    try:
        _configurar_download_automatico_chrome(driver)
    except Exception as exc:
        print(f"[DOWNLOAD] Não foi possível configurar download automático no Chrome: {exc}")

    return driver


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


def _calc_fgts_targets(start_certidao_id):
    return _calc_fgts_targets_by_scope(start_certidao_id, 'default')


def _calc_fgts_targets_by_scope(start_certidao_id, scope='default'):
    return batch_engine.calc_targets(
        start_certidao_id,
        extra_filter=lambda query: query.filter(Certidao.tipo == TipoCertidao.FGTS),
        scope=scope,
    )


def _calc_estadual_rs_targets(start_certidao_id):
    return _calc_estadual_rs_targets_by_scope(start_certidao_id, 'default')


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

            valor = ''.join(filter(str.isdigit, campo_cnpj.get_attribute('value') or ''))
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

    cnpj_limpo = ''.join(filter(str.isdigit, certidao.empresa.cnpj or ''))

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
        classificacao = _classificar_certidao_estadual_rs(caminho_final)

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
    with app.app_context():
        driver = None
        rs_policy_ativa = False
        print("[ESTADUAL-RS-LOTE] Worker iniciado.")
        execution_id = RS_BATCH_STATE.get('execution_id')
        if execution_id:
            CorrelationContext.set_execution_id(execution_id)
        log_event('rs_batch_worker_start', status='running')

        try:
            rs_policy_ativa = _ativar_politica_autoselect_rs_temporaria()
            driver = _criar_driver_chrome(anonimo=False, usar_perfil=True)

            while True:
                with RS_BATCH_LOCK:
                    if RS_BATCH_STATE['stop_requested']:
                        if RS_BATCH_STATE.get('stop_action') == 'stop':
                            RS_BATCH_STATE['status'] = 'stopped'
                            print("[ESTADUAL-RS-LOTE] Interrompido por parada solicitada.")
                        else:
                            RS_BATCH_STATE['status'] = 'paused'
                            print("[ESTADUAL-RS-LOTE] Pausado por solicitação.")
                        break

                    if RS_BATCH_STATE['index'] >= RS_BATCH_STATE['total']:
                        RS_BATCH_STATE['status'] = 'completed'
                        RS_BATCH_STATE['current_id'] = None
                        RS_BATCH_STATE['finished_at'] = datetime.utcnow()
                        print("[ESTADUAL-RS-LOTE] Finalizado com sucesso.")
                        break

                    certidao_id = RS_BATCH_STATE['ids'][RS_BATCH_STATE['index']]
                    RS_BATCH_STATE['current_id'] = certidao_id
                    print(
                        f"[ESTADUAL-RS-LOTE] Iniciando emissão ID={certidao_id} "
                        f"({RS_BATCH_STATE['index'] + 1}/{RS_BATCH_STATE['total']})."
                    )

                sucesso, grave, mensagem = _emitir_estadual_rs_certidao(
                    certidao_id,
                    driver=driver,
                    usar_2captcha=True,
                    execution_id=execution_id,
                )

                with RS_BATCH_LOCK:
                    if RS_BATCH_STATE['stop_requested']:
                        if RS_BATCH_STATE.get('stop_action') == 'stop':
                            RS_BATCH_STATE['status'] = 'stopped'
                        else:
                            RS_BATCH_STATE['status'] = 'paused'
                        break

                    if grave:
                        RS_BATCH_STATE['status'] = 'error'
                        RS_BATCH_STATE['message'] = mensagem or 'Erro grave no lote Estadual RS.'
                        print(f"[ESTADUAL-RS-LOTE] Erro grave: {RS_BATCH_STATE['message']}")
                        break

                    if not sucesso:
                        RS_BATCH_STATE['falhas'] += 1
                        print(f"[ESTADUAL-RS-LOTE] Falha na emissão ID={certidao_id}: {mensagem}")
                    else:
                        RS_BATCH_STATE['success'] += 1
                        print(f"[ESTADUAL-RS-LOTE] Emissão OK ID={certidao_id}.")

                    RS_BATCH_STATE['index'] += 1
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            if rs_policy_ativa:
                _desativar_politica_autoselect_rs_temporaria()
            log_event('rs_batch_worker_end', status=RS_BATCH_STATE.get('status'))
            CorrelationContext.clear()
            print("[ESTADUAL-RS-LOTE] Worker encerrado.")


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
        cnpj_limpo = ''.join(filter(str.isdigit, certidao.empresa.cnpj or ''))
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
    with app.app_context():
        driver = None
        print("[FGTS-LOTE] Worker iniciado.")
        execution_id = FGTS_BATCH_STATE.get('execution_id')
        if execution_id:
            CorrelationContext.set_execution_id(execution_id)
        log_event('fgts_batch_worker_start', status='running')
        while True:
            with FGTS_BATCH_LOCK:
                if FGTS_BATCH_STATE['stop_requested']:
                    if FGTS_BATCH_STATE.get('stop_action') == 'stop':
                        FGTS_BATCH_STATE['status'] = 'stopped'
                        print("[FGTS-LOTE] Interrompido por parada solicitada.")
                    else:
                        FGTS_BATCH_STATE['status'] = 'paused'
                        print("[FGTS-LOTE] Pausado por solicitação.")
                    break

                if FGTS_BATCH_STATE['index'] >= FGTS_BATCH_STATE['total']:
                    FGTS_BATCH_STATE['status'] = 'completed'
                    FGTS_BATCH_STATE['current_id'] = None
                    FGTS_BATCH_STATE['finished_at'] = datetime.utcnow()
                    print("[FGTS-LOTE] Finalizado com sucesso.")
                    break

                certidao_id = FGTS_BATCH_STATE['ids'][FGTS_BATCH_STATE['index']]
                FGTS_BATCH_STATE['current_id'] = certidao_id
                print(f"[FGTS-LOTE] Iniciando emissão ID={certidao_id} ({FGTS_BATCH_STATE['index'] + 1}/{FGTS_BATCH_STATE['total']}).")

            if driver is None:
                driver = _criar_driver_chrome()

            sucesso, grave, mensagem = _emitir_fgts_certidao(certidao_id, driver=driver, execution_id=execution_id)

            if grave and mensagem == 'Erro ao carregar página FGTS.':
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = _criar_driver_chrome()
                print("[FGTS-LOTE] Recriando driver após falha de carregamento.")
                sucesso, grave, mensagem = _emitir_fgts_certidao(certidao_id, driver=driver, execution_id=execution_id)

            with FGTS_BATCH_LOCK:
                if FGTS_BATCH_STATE['stop_requested']:
                    if FGTS_BATCH_STATE.get('stop_action') == 'stop':
                        FGTS_BATCH_STATE['status'] = 'stopped'
                        print("[FGTS-LOTE] Interrompido durante execução.")
                    else:
                        FGTS_BATCH_STATE['status'] = 'paused'
                        print("[FGTS-LOTE] Pausado durante execução.")
                    break

                if grave:
                    FGTS_BATCH_STATE['status'] = 'error'
                    FGTS_BATCH_STATE['message'] = mensagem or 'Erro grave.'
                    print(f"[FGTS-LOTE] Erro grave: {FGTS_BATCH_STATE['message']}")
                    break

                if not sucesso:
                    FGTS_BATCH_STATE['falhas'] += 1
                    print(f"[FGTS-LOTE] Falha na emissão ID={certidao_id}. Motivo: {mensagem}")
                else:
                    FGTS_BATCH_STATE['success'] += 1
                    print(f"[FGTS-LOTE] Emissão OK ID={certidao_id}.")

                FGTS_BATCH_STATE['index'] += 1

        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        log_event('fgts_batch_worker_end', status=FGTS_BATCH_STATE.get('status'))
        CorrelationContext.clear()
        print("[FGTS-LOTE] Worker encerrado.")


@bp.route('/fgts/lote/info/<int:certidao_id>')
def fgts_lote_info(certidao_id):
    scope = _parse_batch_scope(request.args.get('scope'))
    dados = _calc_fgts_targets_by_scope(certidao_id, scope=scope)
    return jsonify({
        'status': 'ok',
        **dados
    })


@bp.route('/fgts/lote/iniciar', methods=['POST'])
def fgts_lote_iniciar():
    dados = request.get_json() or {}
    certidao_id = dados.get('certidao_id')
    scope = _parse_batch_scope(dados.get('scope'))

    if not certidao_id:
        return jsonify({'status': 'error', 'message': 'Certidão inválida.'}), 400

    dados_lote = batch_engine.init_batch_run(
        FGTS_BATCH_LOCK,
        FGTS_BATCH_STATE,
        certidao_id,
        lambda start_id: _calc_fgts_targets_by_scope(start_id, scope=scope),
        _fgts_batch_worker,
        app_factory=_current_app_object,
    )

    if dados_lote is None:
        return jsonify({'status': 'error', 'message': 'Já existe um lote em andamento.'}), 400

    if not dados_lote:
        if scope == 'pendentes':
            return jsonify({'status': 'error', 'message': 'Nenhuma certidão FGTS pendente para emissão.'}), 400
        return jsonify({'status': 'error', 'message': 'Nenhuma certidão FGTS vencida ou a vencer.'}), 400

    log_event(
        'fgts_batch_started',
        status='running',
        scope=scope,
        total=dados_lote['total'],
        execution_id=FGTS_BATCH_STATE.get('execution_id'),
    )
    print(f"[FGTS-LOTE] Lote iniciado. Total={dados_lote['total']}.")

    return jsonify({'status': 'ok'})


@bp.route('/fgts/lote/pausar', methods=['POST'])
def fgts_lote_pausar():
    driver = batch_engine.request_pause(FGTS_BATCH_LOCK, FGTS_BATCH_STATE)
    print("[FGTS-LOTE] Pausa solicitada.")

    _fgts_quit_driver_async(driver)

    return jsonify({'status': 'ok', 'message': 'Lote pausado.'})


@bp.route('/fgts/lote/parar', methods=['POST'])
def fgts_lote_parar():
    driver = batch_engine.request_stop(FGTS_BATCH_LOCK, FGTS_BATCH_STATE)
    print("[FGTS-LOTE] Parada solicitada.")

    _fgts_quit_driver_async(driver)

    return jsonify({'status': 'ok', 'message': 'Lote interrompido.'})


@bp.route('/fgts/lote/retomar', methods=['POST'])
def fgts_lote_retomar():
    if not batch_engine.resume_batch(
        FGTS_BATCH_LOCK,
        FGTS_BATCH_STATE,
        _fgts_batch_worker,
        app_factory=_current_app_object,
    ):
        return jsonify({'status': 'error', 'message': 'Lote não está pausado.'}), 400

    print("[FGTS-LOTE] Retomada solicitada.")

    return jsonify({'status': 'ok'})


@bp.route('/fgts/lote/status')
def fgts_lote_status():
    return jsonify(batch_engine.status_payload_locked(FGTS_BATCH_LOCK, FGTS_BATCH_STATE))


@bp.route('/health')
def health():
    checks = run_health_checks(current_app.config)
    has_failure = any(not item.get('ok') for item in checks.values())
    code = 200 if not has_failure else 503
    return jsonify({'status': 'ok' if not has_failure else 'degraded', 'checks': checks}), code


@bp.route('/estadual-rs/lote/info/<int:certidao_id>')
def estadual_rs_lote_info(certidao_id):
    scope = _parse_batch_scope(request.args.get('scope'))
    dados = _calc_estadual_rs_targets_by_scope(certidao_id, scope=scope)
    return jsonify({
        'status': 'ok',
        **dados
    })


@bp.route('/estadual-rs/lote/iniciar', methods=['POST'])
def estadual_rs_lote_iniciar():
    dados = request.get_json() or {}
    certidao_id = dados.get('certidao_id')
    scope = _parse_batch_scope(dados.get('scope'))

    if not certidao_id:
        return jsonify({'status': 'error', 'message': 'Certidão inválida.'}), 400

    if not _to_bool(_get_config_value('RS_ALTCHA_AUTOSOLVE_ENABLED', False), False):
        return jsonify({
            'status': 'error',
            'message': 'Ative RS_ALTCHA_AUTOSOLVE_ENABLED para usar lote Estadual RS.'
        }), 400

    dados_lote = batch_engine.init_batch_run(
        RS_BATCH_LOCK,
        RS_BATCH_STATE,
        certidao_id,
        lambda start_id: _calc_estadual_rs_targets_by_scope(start_id, scope=scope),
        _rs_batch_worker,
        app_factory=_current_app_object,
    )

    if dados_lote is None:
        return jsonify({'status': 'error', 'message': 'Já existe um lote Estadual RS em andamento.'}), 400

    if not dados_lote:
        if scope == 'pendentes':
            return jsonify({'status': 'error', 'message': 'Nenhuma certidão Estadual RS pendente para emissão.'}), 400
        return jsonify({'status': 'error', 'message': 'Nenhuma certidão Estadual RS vencida ou a vencer.'}), 400

    log_event(
        'rs_batch_started',
        status='running',
        scope=scope,
        total=dados_lote['total'],
        execution_id=RS_BATCH_STATE.get('execution_id'),
    )
    print(f"[ESTADUAL-RS-LOTE] Lote iniciado. Total={dados_lote['total']}.")

    return jsonify({'status': 'ok'})


@bp.route('/estadual-rs/lote/pausar', methods=['POST'])
def estadual_rs_lote_pausar():
    driver = batch_engine.request_pause(RS_BATCH_LOCK, RS_BATCH_STATE)
    print("[ESTADUAL-RS-LOTE] Pausa solicitada.")

    _fgts_quit_driver_async(driver)

    return jsonify({'status': 'ok', 'message': 'Lote Estadual RS pausado.'})


@bp.route('/estadual-rs/lote/parar', methods=['POST'])
def estadual_rs_lote_parar():
    driver = batch_engine.request_stop(RS_BATCH_LOCK, RS_BATCH_STATE)
    print("[ESTADUAL-RS-LOTE] Parada solicitada.")

    _fgts_quit_driver_async(driver)

    return jsonify({'status': 'ok', 'message': 'Lote Estadual RS interrompido.'})


@bp.route('/estadual-rs/lote/retomar', methods=['POST'])
def estadual_rs_lote_retomar():
    if not batch_engine.resume_batch(
        RS_BATCH_LOCK,
        RS_BATCH_STATE,
        _rs_batch_worker,
        app_factory=_current_app_object,
    ):
        return jsonify({'status': 'error', 'message': 'Lote Estadual RS não está pausado.'}), 400

    print("[ESTADUAL-RS-LOTE] Retomada solicitada.")

    return jsonify({'status': 'ok'})


@bp.route('/estadual-rs/lote/status')
def estadual_rs_lote_status():
    return jsonify(batch_engine.status_payload_locked(RS_BATCH_LOCK, RS_BATCH_STATE))


@bp.route('/fgts/emitir_unico', methods=['POST'])
def fgts_emitir_unico():
    dados = request.get_json() or {}
    certidao_id = dados.get('certidao_id')

    if not certidao_id:
        return jsonify({'status': 'error', 'message': 'Certidão inválida.'}), 400

    with FGTS_BATCH_LOCK:
        if FGTS_BATCH_STATE['status'] == 'running':
            return jsonify({'status': 'error', 'message': 'Lote em andamento. Pare o lote para emitir individual.'}), 400

    execution_id = CorrelationContext.new_execution_id()
    sucesso, grave, mensagem = _emitir_fgts_certidao(certidao_id, execution_id=execution_id)

    if grave:
        return jsonify({'status': 'error', 'message': mensagem or 'Erro grave no FGTS.'}), 500

    if not sucesso:
        return jsonify({'status': 'error', 'message': mensagem or 'Falha ao emitir certidão FGTS.'}), 400

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


def _extrair_validade_pdf_federal(caminho_pdf):
    if not caminho_pdf:
        return None

    try:
        with pdfplumber.open(caminho_pdf) as pdf:
            texto = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as exc:
        print(f"[FEDERAL] Erro ao ler PDF: {exc}")
        return None

    match = re.search(r"Válida\s+até\s+(\d{2}/\d{2}/\d{4})", texto, re.IGNORECASE)
    if not match:
        return None

    try:
        return datetime.strptime(match.group(1), "%d/%m/%Y").date()
    except ValueError:
        return None


def _extrair_texto_pdf(caminho_pdf, origem_log='PDF'):
    if not caminho_pdf:
        return ''

    try:
        with pdfplumber.open(caminho_pdf) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as exc:
        print(f"[{origem_log}] Erro ao ler PDF: {exc}")
        return ''


def _normalizar_texto_pdf(texto):
    texto = file_manager.remover_acentos(texto or '')
    texto = re.sub(r'\s+', ' ', texto)
    return texto.upper().strip()


def _classificar_status_certidao_pdf(caminho_pdf, origem_log='PDF'):
    texto = _normalizar_texto_pdf(_extrair_texto_pdf(caminho_pdf, origem_log=origem_log))
    if not texto:
        return 'desconhecida'

    if re.search(r'CERTIDAO\s+POSITIVA\s+COM\s+EFEITOS?\s+DE\s+NEGATIVA', texto):
        return 'efeito_negativa'

    if re.search(r'CERTIDAO\s+POSITIVA\b', texto):
        return 'positiva'

    if re.search(r'CERTIDAO\s+NEGATIVA\b', texto):
        return 'negativa'

    return 'desconhecida'


def _classificar_certidao_estadual_rs(caminho_pdf):
    return _classificar_status_certidao_pdf(caminho_pdf, origem_log='ESTADUAL-RS')


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


@bp.route('/')
def dashboard():
    status_filtros = request.args.getlist('status')
    tipo_filtros = request.args.getlist('tipo')
    estado_filtro = request.args.get('estado', '')
    cidade_filtro = (request.args.get('cidade', '') or '').strip()

    query = db.session.query(Empresa).distinct()

    hoje = date.today()
    join_certidao_feito = False

    if not status_filtros:
        status_filtros = ['todas']

    if 'todas' in status_filtros or not status_filtros:
        status_filtros = ['todas']
    else:
        query = query.join(Certidao)
        join_certidao_feito = True

        conditions = []

        if 'validas' in status_filtros:
            conditions.append(Certidao.data_validade >
                              (hoje + timedelta(days=7)))

        if 'a_vencer' in status_filtros:
            conditions.append(Certidao.data_validade.between(
                hoje, hoje + timedelta(days=7)))

        if 'vencidas' in status_filtros:
            conditions.append(
                (Certidao.data_validade < hoje) & (
                    Certidao.status_especial == None)
            )

        if 'pendentes' in status_filtros:
            conditions.append(Certidao.status_especial ==
                              StatusEspecial.PENDENTE)

        if 'nao_definida' in status_filtros:
            conditions.append(Certidao.data_validade == None)

        if conditions:
            query = query.filter(or_(*conditions))
        else:
            query = query.filter(Empresa.id == -1)

    if not tipo_filtros or 'todas' in tipo_filtros:
        tipo_filtros = ['todas']
    else:
        tipos_enum = []
        mapa_tipo = {
            'federal': TipoCertidao.FEDERAL,
            'fgts': TipoCertidao.FGTS,
            'estadual': TipoCertidao.ESTADUAL,
            'municipal': TipoCertidao.MUNICIPAL,
            'trabalhista': TipoCertidao.TRABALHISTA,
        }
        for t in tipo_filtros:
            enum_val = mapa_tipo.get(t)
            if enum_val:
                tipos_enum.append(enum_val)
        if tipos_enum:
            if not join_certidao_feito:
                query = query.join(Certidao)
                join_certidao_feito = True
            query = query.filter(Certidao.tipo.in_(tipos_enum))
        else:
            query = query.filter(Empresa.id == -1)

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

    municipios = Municipio.query.all()

    urls_municipais = {}
    for m in municipios:
        if not m.url_certidao:
            continue
        nome = (m.nome or '').strip()
        url = m.url_certidao
        
        urls_municipais[nome] = url
        nome_sem = file_manager.remover_acentos(nome)
        urls_municipais[nome_sem] = url
        
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
        sites_urls=SITES_CERTIDOES,
        urls_municipais=urls_municipais
    )


@bp.route('/empresa/nova', endpoint='nova_empresa')
def pagina_nova_empresa():
    return render_template('nova_empresa.html')


@bp.route('/relatorios')
def relatorios():
    hoje = date.today()
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
        elif dias_restantes <= 7:
            a_vencer += 1

    return render_template(
        'relatorios.html',
        empresas_total=empresas_total,
        total_certidoes=total_certidoes,
        pendentes=pendentes,
        vencidas=vencidas,
        a_vencer=a_vencer,
    )


@bp.route('/configuracoes')
def configuracoes():
    return render_template('configuracoes.html')


@bp.route('/empresa/adicionar', methods=['POST'])
def adicionar_empresa():
    # dados formulário
    nome = request.form.get('nome')
    cnpj = request.form.get('cnpj')
    estado = request.form.get('estado')
    cidade = request.form.get('cidade')
    inscricao = request.form.get('inscricao_mobiliaria')
    origem = request.form.get('origem')

    def _redirect_apos_cadastro():
        if origem == 'nova_empresa':
            return redirect(url_for('main.nova_empresa'))
        return redirect(url_for('main.dashboard'))

    if not cnpj or len(cnpj) < 18:
        flash('CNPJ incompleto, preencha todos os dígitos.', 'warning')
        return _redirect_apos_cadastro()

    # validacao
    empresa_existente = Empresa.query.filter_by(cnpj=cnpj).first()
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
        certidao.data_validade = nova_data
        certidao.status_especial = None

        try:
            db.session.commit()
            flash(
                f"Validade da certidão {certidao.tipo.value} da empresa {certidao.empresa.nome} atualizada com sucesso!", 'success')
        except Exception as e:
            db.session.rollback()
            flash(f"Erro ao atualizar validade: {e}", 'danger')
    else:
        flash("Nenhuma data foi fornecida.", 'warning')
    return redirect(url_for('main.dashboard'))


@bp.route('/certidao/marcar_pendente/<int:certidao_id>', methods=['POST'])
def marcar_pendente(certidao_id):
    certidao = Certidao.query.get_or_404(certidao_id)
    certidao.status_especial = StatusEspecial.PENDENTE
    certidao.data_validade = None
    try:
        db.session.commit()
        flash(
            f'Certidão {certidao.tipo.value} da empresa {certidao.empresa.nome} marcada como Pendente.', 'info')
    except Exception as e:
        db.session.rollback()
        flash(f'Erro ao marcar como pendente: {e}', 'danger')

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

        btn_certificado = _aguardar_clickable((By.ID, "mainForm:j_id51"))
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


def _executar_steps_municipio(driver, wait, steps, cnpj_limpo, inscricao_limpa, etapa_label='steps'):
    if not steps:
        return None

    def _normalizar_texto(valor):
        texto = (valor or '')
        texto = unicodedata.normalize('NFKD', texto)
        texto = ''.join(ch for ch in texto if not unicodedata.combining(ch))
        texto = re.sub(r'\s+', ' ', texto).strip().upper()
        return texto

    by_map = {
        'id': By.ID,
        'name': By.NAME,
        'css_selector': By.CSS_SELECTOR,
        'xpath': By.XPATH,
        'class_name': By.CLASS_NAME
    }

    for idx, step in enumerate(steps, start=1):
        tipo = (step or {}).get('tipo')
        if not tipo:
            continue

        if tipo == 'click_if_text_or_close':
            by = by_map.get(step.get('by'))
            locator = step.get('locator')
            expected_text = _normalizar_texto(step.get('expected_text_contains'))
            timeout = float(step.get('timeout', 10))
            sleep_after = float(step.get('sleep', 0.5))
            wait_url_contains = (step.get('wait_url_contains') or '').strip()

            if not by or not locator or not expected_text:
                continue

            if wait_url_contains:
                try:
                    WebDriverWait(driver, timeout).until(lambda d: wait_url_contains in (d.current_url or ''))
                except TimeoutException:
                    print(f"[MUNICIPAL] Timeout aguardando URL final do step condicional ({wait_url_contains}).")

            try:
                WebDriverWait(driver, timeout).until(
                    lambda d: d.execute_script('return document.readyState') == 'complete'
                )
            except TimeoutException:
                pass

            try:
                WebDriverWait(driver, timeout).until(
                    EC.presence_of_all_elements_located((by, locator))
                )
            except TimeoutException:
                pass

            try:
                elementos = driver.find_elements(by, locator)
            except Exception as exc_find:
                print(f"[MUNICIPAL] Erro ao buscar elementos do step condicional: {exc_find!r}")
                raise

            alvo = None
            for pos, elemento in enumerate(elementos, start=1):
                texto_variantes = [
                    _normalizar_texto(elemento.text),
                    _normalizar_texto(elemento.get_attribute('textContent')),
                    _normalizar_texto(elemento.get_attribute('innerText')),
                ]
                if any(expected_text in t for t in texto_variantes if t):
                    alvo = elemento
                    break

            if alvo is None:
                try:
                    js_click_result = driver.execute_script(
                        """
                        const expected = arguments[0];
                        const normalize = (txt) => (txt || '')
                          .normalize('NFD')
                          .replace(/[\u0300-\u036f]/g, '')
                                                    .replace(/\\s+/g, ' ')
                          .trim()
                          .toUpperCase();
                        const anchors = Array.from(document.querySelectorAll('a'));
                        for (const a of anchors) {
                          const text = normalize(a.innerText || a.textContent || '');
                          if (text.includes(expected)) {
                            a.click();
                            return {
                              clicked: true,
                              text,
                              href: a.getAttribute('href') || ''
                            };
                          }
                        }
                        return {clicked: false, count: anchors.length};
                        """,
                        expected_text
                    )
                    if js_click_result and js_click_result.get('clicked'):
                        print('[MUNICIPAL] Link de certidão NEGATIVA encontrado. Prosseguindo com download.')
                        time.sleep(sleep_after)
                        continue
                except Exception as exc_js_click:
                    print(f"[MUNICIPAL] Erro no fallback JS do step condicional: {exc_js_click!r}")

                print('[MUNICIPAL] Link de certidão NEGATIVA não encontrado. Fechando e retornando pendente.')
                try:
                    driver.close()
                except Exception:
                    pass
                return {'encerrar_sem_arquivo': True}

            try:
                alvo.click()
            except Exception:
                driver.execute_script('arguments[0].click();', alvo)

            print('[MUNICIPAL] Link de certidão NEGATIVA encontrado. Prosseguindo com download.')
            time.sleep(sleep_after)
            continue

        if tipo == 'sleep':
            time.sleep(float(step.get('seconds', 1)))
            continue

        if tipo == 'refresh':
            driver.refresh()
            time.sleep(float(step.get('sleep', 1)))
            continue

        if tipo == 'wait_for':
            by = by_map.get(step.get('by'))
            locator = step.get('locator')
            if not by or not locator:
                continue
            timeout = step.get('timeout', 10)
            state = step.get('state', 'clickable')
            cond = EC.element_to_be_clickable if state == 'clickable' else EC.presence_of_element_located
            WebDriverWait(driver, timeout).until(cond((by, locator)))
            continue

        if tipo in ['click', 'click_js', 'select', 'fill']:
            by = by_map.get(step.get('by'))
            locator = step.get('locator')
            if not by or not locator:
                continue

            elemento = wait.until(EC.element_to_be_clickable((by, locator)))

            if tipo == 'click':
                elemento.click()
                time.sleep(float(step.get('sleep', 0.5)))
                continue

            if tipo == 'click_js':
                driver.execute_script("arguments[0].click();", elemento)
                time.sleep(float(step.get('sleep', 0.5)))
                continue

            if tipo == 'select':
                select_obj = Select(elemento)
                value = step.get('value')
                text = step.get('text')
                contains = step.get('text_contains')
                if value is not None:
                    select_obj.select_by_value(value)
                elif text:
                    select_obj.select_by_visible_text(text)
                elif contains:
                    for opt in select_obj.options:
                        if contains.upper() in opt.text.upper():
                            select_obj.select_by_visible_text(opt.text)
                            break
                time.sleep(float(step.get('sleep', 0.5)))
                continue

            if tipo == 'fill':
                value = step.get('value')
                if value == 'cnpj':
                    value = cnpj_limpo
                elif value == 'inscricao':
                    value = inscricao_limpa
                if value is None:
                    continue
                elemento.clear()
                elemento.click()
                elemento.send_keys(value)
                time.sleep(float(step.get('sleep', 0.5)))
                continue
    return None


# baixar certidao com automacao salvamento ||||
# VVVV

@bp.route('/certidao/baixar/<int:certidao_id>')
def baixar_certidao(certidao_id):
    file_manager.criar_chave_interrupcao()
    certidao = Certidao.query.get_or_404(certidao_id)
    tipo_certidao_chave = certidao.tipo.name

    if tipo_certidao_chave == 'ESTADUAL' and (certidao.empresa.estado or '').strip().upper() == 'RS':
        with RS_BATCH_LOCK:
            if RS_BATCH_STATE['status'] in ['running', 'paused']:
                return jsonify({
                    'status': 'error',
                    'message': 'Lote Estadual RS em andamento. Aguarde finalizar ou interrompa o lote.'
                }), 400

    by_map = {
        'id': By.ID,
        'css_selector': By.CSS_SELECTOR,
        'xpath': By.XPATH,
        'name': By.NAME
    }

    def _get_by(key):
        return by_map.get(key)

    def _calcular_validade_sem_data(tipo_chave, regra):
        if tipo_chave == 'MUNICIPAL':
            if regra and regra.validade_dias:
                return date.today() + timedelta(days=regra.validade_dias)
            return None
        return calcular_validade_padrao(certidao, None)

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

    def _nome_certidao_imbe(nome_padrao, tipo_escolhido):
        if tipo_escolhido == 'geral':
            return 'CERTIDAO MUNICIPAL'
        if tipo_escolhido == 'mobiliario':
            return 'CERTIDAO MOBILIARIO'
        return nome_padrao

    def _resolve_imbe_tipo(cert_subtipo):
        if cert_subtipo == SubtipoCertidao.GERAL:
            return 'geral'
        if cert_subtipo == SubtipoCertidao.MOBILIARIO:
            return 'mobiliario'
        return ''

    regra_municipio = None
    config_municipal = None
    usar_config_municipal = False
    imbe_tipo = (request.args.get('imbe_tipo') or '').strip().lower()

    if not imbe_tipo and certidao.subtipo:
        imbe_tipo = _resolve_imbe_tipo(certidao.subtipo)

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
        cidade_norm = file_manager.remover_acentos(cidade_empresa).upper()
        
        for m in Municipio.query.all():
            nome_norm = file_manager.remover_acentos(m.nome or '').upper()
            if nome_norm == cidade_norm:
                regra_municipio = m
                break
        
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
                return jsonify({
                    "status": "manual_required",
                    "message": "Automação desativada para este município. Use o botão 'Abrir Site'."
                })

            config_municipal = _carregar_config_municipio(regra_municipio)
            usar_config_municipal = bool(config_municipal)

            cidade_regra_norm = file_manager.remover_acentos(regra_municipio.nome or '').upper()
            if cidade_regra_norm == 'IMBE':
                if imbe_tipo not in ['mobiliario', 'geral']:
                    return jsonify({
                        'status': 'manual_required',
                        'message': 'Para Imbé, selecione no modal: Certidão Municipal Mobiliário ou Geral.'
                    })

                _aplicar_variantes_imbe(info_site, config_municipal, imbe_tipo)

            if usar_config_municipal and config_municipal.get('skip_cnpj_fill'):
                info_site['cnpj_field_id'] = None
            
        else:
            return jsonify({'status': 'error', 'message': 'Regra municipal não encontrada'})

    if tipo_certidao_chave == 'MUNICIPAL' and not usar_config_municipal:
        return jsonify({
            'status': 'error',
            'message': 'Municipio sem automacao. Configure para prosseguir.'
        })

    cnpj_limpo = ''.join(filter(str.isdigit, certidao.empresa.cnpj))
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
                resultado_steps = _executar_steps_municipio(
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
                        apenas_numeros = ''.join(filter(str.isdigit, cnpj_limpo))
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
                except:
                    pass

        if tipo_certidao_chave == 'ESTADUAL' and estado_emp == 'RS':
            print('[ESTADUAL-RS][ALTCHA] Emissão unitária em modo manual: resolva o captcha e clique em Enviar.')

        if tipo_certidao_chave == 'MUNICIPAL' and usar_config_municipal:
            steps_after = config_municipal.get('after_cnpj', []) if config_municipal else []
            resultado_steps = _executar_steps_municipio(
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
                except:
                    pass

        if not pular_monitoramento:
            print("--- AGUARDANDO DOWNLOAD OU FECHAMENTO ---")

            download_detectado = False

            while True:
                try:
                    driver.window_handles
                except:
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
                                rs_estadual_classificacao = _classificar_certidao_estadual_rs(msg)
                                print(f"[ESTADUAL-RS] Classificação do PDF: {rs_estadual_classificacao}")

                                if rs_estadual_classificacao == 'positiva':
                                    erro_remocao = None
                                    try:
                                        if msg and os.path.exists(msg):
                                            os.remove(msg)
                                    except Exception as exc_remove:
                                        erro_remocao = str(exc_remove)
                                        print(f"[ESTADUAL-RS] Não foi possível remover PDF positivo: {exc_remove}")

                                    try:
                                        certidao.caminho_arquivo = None
                                        certidao.status_especial = StatusEspecial.PENDENTE
                                        certidao.data_validade = None
                                        db.session.commit()
                                    except Exception as e_db:
                                        db.session.rollback()
                                        print(f"[ESTADUAL-RS] Erro ao marcar pendente após PDF positivo: {e_db}")
                                        rs_estadual_msg = 'Certidão POSITIVA detectada, mas houve erro ao marcar como PENDENTE no banco.'
                                        rs_estadual_classificacao = 'erro'
                                    else:
                                        rs_estadual_msg = 'Certidão ESTADUAL RS detectada como POSITIVA. Arquivo removido e certidão marcada como PENDENTE.'
                                        if erro_remocao:
                                            rs_estadual_msg += f' Não foi possível remover o arquivo automaticamente: {erro_remocao}'

                            if (
                                tipo_certidao_chave == 'MUNICIPAL'
                                and regra_municipio
                                and usar_config_municipal
                                and bool((config_municipal or {}).get('classificar_pdf_status'))
                            ):
                                origem_pdf = f"MUNICIPAL-{regra_municipio.nome}"
                                municipal_pdf_classificacao = _classificar_status_certidao_pdf(msg, origem_log=origem_pdf)
                                print(f"[{origem_pdf}] Classificação do PDF: {municipal_pdf_classificacao}")

                                if municipal_pdf_classificacao == 'positiva':
                                    erro_remocao = None
                                    try:
                                        if msg and os.path.exists(msg):
                                            os.remove(msg)
                                    except Exception as exc_remove:
                                        erro_remocao = str(exc_remove)
                                        print(f"[{origem_pdf}] Não foi possível remover PDF positivo: {exc_remove}")

                                    try:
                                        certidao.caminho_arquivo = None
                                        certidao.status_especial = StatusEspecial.PENDENTE
                                        certidao.data_validade = None
                                        db.session.commit()
                                    except Exception as e_db:
                                        db.session.rollback()
                                        print(f"[{origem_pdf}] Erro ao marcar pendente após PDF positivo: {e_db}")
                                        municipal_pdf_msg = 'Certidão MUNICIPAL POSITIVA detectada, mas houve erro ao marcar como PENDENTE no banco.'
                                        municipal_pdf_classificacao = 'erro'
                                    else:
                                        municipal_pdf_msg = (
                                            f"Certidão MUNICIPAL ({regra_municipio.nome}) detectada como POSITIVA. "
                                            "Arquivo removido e certidão marcada como PENDENTE."
                                        )
                                        if erro_remocao:
                                            municipal_pdf_msg += f' Não foi possível remover o arquivo automaticamente: {erro_remocao}'
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
            except:
                pass
        return jsonify({"status": "error", "message": "Ocorreu um erro na automação."}), 500
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
        return jsonify({
            'status': 'error',
            'message': rs_estadual_msg or 'Erro ao tratar certidão positiva do RS.'
        }), 500

    if municipal_pdf_classificacao == 'positiva':
        response_data['status'] = 'municipal_pdf_positiva'
        response_data['message'] = municipal_pdf_msg or 'Certidão MUNICIPAL detectada como POSITIVA e marcada como PENDENTE.'
        response_data['certidao_id'] = certidao_id
        response_data['tipo_certidao'] = nome_certidao_arquivo
        return jsonify(response_data)

    if municipal_pdf_classificacao == 'erro':
        return jsonify({
            'status': 'error',
            'message': municipal_pdf_msg or 'Erro ao tratar certidão municipal positiva.'
        }), 500

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

        certidao.data_validade = nova_data
        certidao.status_especial = None

        hoje = date.today()
        diferenca = (nova_data - hoje).days

        nova_classe = 'status-verde'
        if diferenca < 0:
            nova_classe = 'status-vermelho'
        elif diferenca <= 7:
            nova_classe = 'status-amarelo'

        db.session.commit()

        return jsonify({
            'status': 'success',
            'message': 'Data confirmada e atualizada com sucesso!',
            'nova_data_formatada': nova_data.strftime('%d/%m/%Y'),
            'nova_classe': nova_classe
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@bp.route('/certidao/monitorar_download_federal/<int:certidao_id>')
def monitorar_download_federal(certidao_id):
    certidao = Certidao.query.get_or_404(certidao_id)

    print(
        f"--- INICIANDO MONITORAMENTO DE DOWNLOAD (FEDERAL) - ID: {certidao_id} ---")

    file_manager.criar_chave_interrupcao()

    time.sleep(2)

    file_manager.remover_chave_interrupcao()

    tempo_limite = 180
    tempo_inicio = time.time()
    chave_interrupcao = file_manager.obter_caminho_chave_interrupcao()

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
            return jsonify({'status': 'interrupted', 'mensagem': 'Monitoramento interrompido.'})

        novo_arquivo = file_manager.verificar_novo_arquivo(
            tempo_inicio, termos_ignorar=termos_proibidos)

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
                validade_pdf = _extrair_validade_pdf_federal(msg)
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
                return jsonify({
                    'status': 'error',
                    'mensagem': f"Erro ao mover: {msg}"
                })

        time.sleep(1)

    # limpeza final por segurança
    file_manager.remover_chave_interrupcao()
    return jsonify({'status': 'timeout', 'mensagem': 'Tempo esgotado sem download.'})


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
        certidao.status_especial = StatusEspecial.PENDENTE
        certidao.data_validade = None

        db.session.commit()
        return jsonify({'status': 'success'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500


@bp.route('/certidao/atualizar_json/<int:certidao_id>', methods=['POST'])
def atualizar_validade_json(certidao_id):
    data = request.get_json()
    nova_data_str = data.get('nova_validade')

    try:
        certidao = Certidao.query.get_or_404(certidao_id)

        if nova_data_str:
            nova_data = datetime.strptime(nova_data_str, '%Y-%m-%d').date()
            certidao.data_validade = nova_data
            certidao.status_especial = None

            hoje = date.today()
            diferenca = (nova_data - hoje).days

            nova_classe = 'status-verde'
            if diferenca < 0:
                nova_classe = 'status-vermelho'
            elif diferenca <= 7:
                nova_classe = 'status-amarelo'

            db.session.commit()

            return jsonify({
                'status': 'success',
                'message': f'Validade de {certidao.empresa.nome} atualizada com sucesso!',
                'nova_data_formatada': nova_data.strftime('%d/%m/%Y'),
                'nova_classe': nova_classe
            })
        else:
            return jsonify({'status': 'error', 'message': 'Data inválida.'})

    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500
