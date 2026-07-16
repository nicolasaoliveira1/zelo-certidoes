"""Emissão de certidões por tipo (FGTS, Estadual RS, Municipal) e helpers.

Extraído de routes.py (C1.4). Concentra a automação Selenium de emissão e
os utilitários de validade/status/download usados por ela e por baixar_certidao.
"""
import base64
import json
import os
import random
import re
import string
import tempfile
import time
from datetime import date, datetime, timedelta
from threading import Thread

from flask import current_app
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
from selenium.webdriver.support.ui import WebDriverWait

from app import db, file_manager
from app.automation import capture, pdf, steps
from app.automation.sites import SITES_CERTIDOES, VALIDADES_CERTIDOES
from app.automation.driver import (
    _configurar_download_automatico_chrome,
    _criar_driver_chrome,
)
from app.automation.batch_state import (
    FGTS_BATCH_LOCK,
    FGTS_BATCH_STATE,
    MUNICIPAL_BATCH_LOCK,
    MUNICIPAL_BATCH_STATE,
    RS_BATCH_LOCK,
    RS_BATCH_STATE,
    fgts_stop_requested as _fgts_stop_requested,
    municipal_batch_stop_requested as _municipal_batch_stop_requested,
    rs_batch_stop_requested as _rs_batch_stop_requested,
)
from app.captcha_solver import solve_normal_captcha
from app.errors import map_exception_to_error_type, mensagem_usuario
from app.models import (
    Certidao,
    Municipio,
    StatusEspecial,
    SubtipoCertidao,
    TipoCertidao,
    get_a_vencer_dias,
)
from app.services import batch_engine
from app.services.correlation import CorrelationContext
from app.services.retry import retry_call
from app.services.execution_logger import log_event
from app.services.rs_altcha import (
    clicar_enviar_estadual_rs as _clicar_enviar_estadual_rs,
    resolver_altcha_rs_com_2captcha as _resolver_altcha_rs_com_2captcha,
)


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
        FGTS_BATCH_STATE['pendentes_resultado'] = FGTS_BATCH_STATE.get('pendentes_resultado', 0) + 1
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


def _municipal_batch_suportado(cidade):
    cidade_norm = file_manager.remover_acentos((cidade or '').strip()).upper()
    return cidade_norm in {'IMBE', 'TRAMANDAI'}


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

    certidao = db.session.get(Certidao, certidao_id)
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
        log_event(
            'rs_batch_stage',
            certidao_id=certidao_id,
            empresa_id=certidao.empresa_id if certidao else None,
            stage=etapa,
            duration_ms=int(elapsed * 1000),
            status='running',
            extra=extra,
            url=state['url'],
            title=state['title'],
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
                    RS_BATCH_STATE['pendentes_resultado'] = RS_BATCH_STATE.get('pendentes_resultado', 0) + 1
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
                log_event(
                    'rs_batch_pdf_positivo_remove_warning', level='WARNING',
                    certidao_id=certidao_id, error=str(exc_remove),
                )

            certidao.caminho_arquivo = None
            certidao.status_especial = StatusEspecial.PENDENTE
            certidao.data_validade = None
            db.session.commit()

            with RS_BATCH_LOCK:
                RS_BATCH_STATE['positivas'] = RS_BATCH_STATE.get('positivas', 0) + 1
                RS_BATCH_STATE['pendentes_resultado'] = RS_BATCH_STATE.get('pendentes_resultado', 0) + 1
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
        capture.capturar_contexto_falha(
            local_driver, 'estadual_rs_lote',
            certidao_id=certidao_id, execution_id=execution_id,
        )
        return False, _classificar_grave(exc), mensagem_usuario(exc, contexto='lote Estadual RS')
    finally:
        if criado_localmente:
            RS_BATCH_STATE['driver'] = None
            if local_driver:
                try:
                    local_driver.quit()
                except Exception:
                    pass


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

    certidao = db.session.get(Certidao, certidao_id)
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
            log_event(
                'municipal_batch_download_config_failed', level='WARNING',
                certidao_id=certidao_id, error=str(exc),
            )

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
            batch_engine.marcar_resultado_pendente(MUNICIPAL_BATCH_STATE, MUNICIPAL_BATCH_LOCK)
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
            batch_engine.marcar_resultado_pendente(MUNICIPAL_BATCH_STATE, MUNICIPAL_BATCH_LOCK)
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
                    batch_engine.marcar_resultado_pendente(MUNICIPAL_BATCH_STATE, MUNICIPAL_BATCH_LOCK)
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
                            MUNICIPAL_BATCH_STATE['pendentes_resultado'] = MUNICIPAL_BATCH_STATE.get('pendentes_resultado', 0) + 1
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
            certidao = db.session.get(Certidao, certidao_id)
            if certidao:
                certidao.status_especial = StatusEspecial.PENDENTE
                certidao.data_validade = None
                db.session.commit()
        except Exception:
            db.session.rollback()
        with MUNICIPAL_BATCH_LOCK:
            MUNICIPAL_BATCH_STATE['pendentes_resultado'] = MUNICIPAL_BATCH_STATE.get('pendentes_resultado', 0) + 1
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
        capture.capturar_contexto_falha(
            local_driver, 'municipal_lote',
            certidao_id=certidao_id, execution_id=execution_id,
        )
        return False, _classificar_grave(exc), mensagem_usuario(exc, contexto='lote municipal')
    finally:
        if local_driver and not criado_localmente:
            _fgts_fechar_abas_extras(local_driver)
        if criado_localmente and local_driver:
            try:
                local_driver.quit()
            except Exception:
                pass


def _emitir_fgts_certidao(certidao_id, driver=None, execution_id=None):
    if execution_id:
        CorrelationContext.set_execution_id(execution_id)

    inicio_fluxo = time.time()
    if _fgts_stop_requested():
        return False, False, 'Lote interrompido.'

    certidao = db.session.get(Certidao, certidao_id)
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
                # _fgts_marcar_pendente_por_impedimento já incrementa
                # pendentes_resultado ao marcar a certidão como PENDENTE.
                marcado, msg_marcacao = _fgts_marcar_pendente_por_impedimento(certidao, msg_impedimento)
                if not marcado:
                    return False, True, msg_marcacao
                return False, False, msg_marcacao

            # escopo pendentes: já estava PENDENTE e continua PENDENTE.
            batch_engine.marcar_resultado_pendente(FGTS_BATCH_STATE, FGTS_BATCH_LOCK)
            return False, False, msg_impedimento

        if contexto.get('pdf_classificacao') == 'positiva':
            msg_positiva = contexto.get('pdf_msg') or 'Certidão FGTS detectada como POSITIVA e marcada como PENDENTE.'

            with FGTS_BATCH_LOCK:
                FGTS_BATCH_STATE['pendentes_resultado'] = FGTS_BATCH_STATE.get('pendentes_resultado', 0) + 1
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
                    log_event(
                        'fgts_db_validade_save_failed', level='WARNING',
                        certidao_id=certidao.id, error=str(e_db),
                    )

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
        capture.capturar_contexto_falha(
            local_driver, 'fgts', certidao_id=certidao_id, execution_id=execution_id,
        )
        return False, _classificar_grave(exc), mensagem_usuario(exc, contexto='FGTS')
    finally:
        if criado_localmente:
            FGTS_BATCH_STATE['driver'] = None
        if criado_localmente and local_driver:
            try:
                local_driver.quit()
            except Exception:
                pass


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


def _classificar_grave(exc):
    """Classifica uma excecao "grave" do emit em dois niveis para o run_batch_loop:

    - `batch_engine.GRAVE_FATAL` quando indica navegador/sessao morta
      (`_erro_indica_navegador_fechado`): repetir os proximos itens com o mesmo
      driver morto e' inutil, entao o lote automatico para (RESIL-04).
    - `True` (grave "comum", ex.: timeout de download): no lote automatico vira
      falha por-item e o loop segue; no manual continua abortando (RESIL-01/03).
    """
    return batch_engine.GRAVE_FATAL if _erro_indica_navegador_fechado(exc) else True


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
        log_event('fgts_impedimento', level='WARNING', certidao_id=certidao.id, message=mensagem)

    try:
        btn_consultar = _aguardar_clickable((By.ID, "mainForm:btnConsultar"))
        if not btn_consultar:
            return
        log_event('fgts_click', certidao_id=certidao.id, botao='Consultar')
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
        log_event('fgts_click', certidao_id=certidao.id, botao='Certificado')
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
                log_event(
                    'fgts_data_nao_encontrada', level='WARNING',
                    certidao_id=certidao.id, error=str(e),
                )

        btn_visualizar = _aguardar_clickable((By.ID, "mainForm:btnVisualizar"))
        if not btn_visualizar:
            if detectar_impedimento:
                msg_impedimento = _fgts_detectar_mensagem_impedimento(driver)
                if msg_impedimento:
                    _marcar_impedimento_e_sair(msg_impedimento)
            return
        log_event('fgts_click', certidao_id=certidao.id, botao='Visualizar')
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
                log_event(
                    'fgts_ancora_nao_confirmada', level='WARNING',
                    certidao_id=certidao.id, error=str(_e),
                )

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
                log_event(
                    'fgts_cdp_printpdf_fallback', level='WARNING',
                    certidao_id=certidao.id, error=str(e_cdp),
                )

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

            log_event('fgts_pdf_gerado', certidao_id=certidao.id, caminho=str(caminho_pdf))

            sucesso, msg = file_manager.mover_e_renomear(
                caminho_pdf,
                certidao.empresa.nome,
                certidao.tipo.value
            )

            if sucesso:
                contexto['arquivo_salvo_msg'] = f"Arquivo salvo em: {msg}"
                contexto['pular_monitoramento'] = True
                log_event('fgts_arquivo_salvo', certidao_id=certidao.id, caminho=str(msg))
                try:
                    certidao.caminho_arquivo = msg
                    db.session.commit()
                except Exception as e_db:
                    db.session.rollback()
                    log_event(
                        'fgts_db_caminho_save_failed', level='WARNING',
                        certidao_id=certidao.id, error=str(e_db),
                    )
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
            log_event(
                'fgts_pdf_gerar_error', level='ERROR',
                certidao_id=certidao.id, error=str(e_pdf),
            )
    except Exception as e:
        if _fgts_stop_requested():
            return
        log_event('fgts_automacao_error', level='ERROR', certidao_id=certidao.id, error=str(e))


def _carregar_config_municipio(regra_municipio):
    if not regra_municipio:
        return None
    raw = regra_municipio.config_automacao
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        log_event(
            'municipal_config_invalida', level='WARNING',
            municipio=regra_municipio.nome, error=str(exc),
        )
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
