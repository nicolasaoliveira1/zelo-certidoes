"""Orquestracao da emissao individual de certidao (fluxo "baixar").

Extraido de app/routes.py (spec 05, REFA-01): concentra a camada Selenium + a
regra de negocio da emissao unitaria, deixando a rota GET /certidao/baixar/<id>
fina (a rota so delega a `baixar_certidao`). A decisao de driver — IPM
Atende.Net -> undetected-chromedriver com perfil dedicado; demais tipos/
municipios -> Chrome padrao — vive aqui em `_abrir_driver_baixar` e continua
UNICA (CLAUDE.md: estender por URL, nao por lista fixa).

Dependencia aponta rota -> servico: este modulo NAO importa de app.routes.
Helpers compartilhados vem de modulos neutros: `json_error` (app.utils) e os
tokens de visualizacao (app.services.visualizar_token).
"""
import time
from datetime import date, timedelta

from flask import jsonify, redirect, request
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

from app import db, file_manager
from app.errors import ErrorType
from app.automation import SITES_CERTIDOES, capture, pdf, steps
from app.automation.batch_state import (
    FGTS_BATCH_LOCK,
    FGTS_BATCH_STATE,
    MUNICIPAL_BATCH_LOCK,
    MUNICIPAL_BATCH_STATE,
    RS_BATCH_LOCK,
    RS_BATCH_STATE,
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
from app.automation.emissao import (
    _aplicar_variantes_imbe,
    _automatizar_fgts,
    _buscar_municipio_por_cidade,
    _carregar_config_municipio,
    _erro_indica_navegador_fechado,
    _login_certificado_rs,
    _nome_certidao_imbe,
    _normalizar_cnpj,
    _resolve_imbe_tipo_from_subtipo,
    calcular_validade_padrao,
)
from app.automation.sites import is_ipm_atende
from app.models import Certidao
from app.services.execution_logger import log_event
from app.services.visualizar_token import _gerar_visualizar_token
from app.utils import json_error as _json_error


# === bloco extraido de app/routes.py (mover != reescrever) ===
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


def baixar_certidao(certidao_id):
    """Orquestra a emissao individual e devolve a resposta HTTP (JSON/redirect).

    Chamada pela rota fina GET /certidao/baixar/<id> (blueprint main). Mantem o
    contrato original: valida, monta config, marca emissao individual ativa,
    executa a automacao Selenium e monta a resposta.
    """
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
