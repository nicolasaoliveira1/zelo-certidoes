"""Criação do WebDriver Chrome e política de auto-seleção de certificado (RS).

Extraído de routes.py (C1). Sem dependência do estado de lote.
"""
import json
import os

try:
    import winreg
except ImportError:
    winreg = None

from flask import current_app
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service as ChromeService
from threading import Lock
from webdriver_manager.chrome import ChromeDriverManager

from app.utils import get_config_value as _get_config_value, to_bool as _to_bool

RS_CERT_POLICY_LOCK = Lock()
RS_CERT_POLICY_ACTIVE_COUNT = 0


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


def _montar_politica_autoselect_rs():
    if not _to_bool(_get_config_value('RS_CERT_AUTOSELECT_ENABLED', False), False):
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
