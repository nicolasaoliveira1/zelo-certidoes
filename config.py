import os
from dotenv import load_dotenv

basedir = os.path.abspath(os.path.dirname(__file__))

load_dotenv(os.path.join(basedir, '.env'))


def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on', 'sim'}


def _env_int(name, default=0):
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(str(value).strip())
    except ValueError:
        return default


def _env_float(name, default=0.0):
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return float(str(value).strip().replace(',', '.'))
    except ValueError:
        return default

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY')
    if not SECRET_KEY:
        raise RuntimeError("SECRET_KEY não configurada no .env")
    #--- BANCO DE DADOS ANTIGO (SQLite) ---#
    #(Comentado para não usar mais)#
    # --- NOVO BANCO DE DADOS (MySQL) ---
    # Estrutura: mysql+pymysql://USUARIO:SENHA@IP_DO_SERVIDOR/NOME_DO_BANCO
    
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or \
        'sqlite:///' + os.path.join(basedir, 'instance', 'database.db')

    # Aplica migrations pendentes ('flask db upgrade') no boot do app.
    # Evita codigo novo rodando sobre schema antigo. Desligue com AUTO_DB_UPGRADE=0.
    AUTO_DB_UPGRADE = _env_bool('AUTO_DB_UPGRADE', True)

    CHROME_PROFILE_DIR = os.environ.get('CHROME_PROFILE_DIR') or \
        os.path.join(basedir, 'chrome-profile')
    CHROME_PROFILE_NAME = os.environ.get('CHROME_PROFILE_NAME') or 'Certidoes'

    CAMINHO_REDE = os.environ.get('CAMINHO_REDE') or r"Z:\\PASTAS EMPRESAS"

    LOG_LEVEL = (os.environ.get('LOG_LEVEL') or 'INFO').strip().upper()
    QUIET_WERKZEUG_LOGS = _env_bool('QUIET_WERKZEUG_LOGS', True)
    # Observabilidade: console legivel para humano + arquivo JSON cru para a IA
    LOG_DIR = os.environ.get('LOG_DIR') or os.path.join(basedir, 'logs')
    LOG_CONSOLE_FORMAT = (os.environ.get('LOG_CONSOLE_FORMAT') or 'human').strip().lower()
    LOG_JSON_FILE = _env_bool('LOG_JSON_FILE', True)
    # Historico de diagnostico persistido em banco (sobrevive a restart)
    DIAGNOSTICO_PERSISTIR = _env_bool('DIAGNOSTICO_PERSISTIR', True)
    DIAGNOSTICO_RETENCAO_DIAS = _env_int('DIAGNOSTICO_RETENCAO_DIAS', 30)

    # Captura de contexto (screenshot + HTML) na falha de automacao Selenium
    SELENIUM_CAPTURE_ENABLED = _env_bool('SELENIUM_CAPTURE_ENABLED', True)
    SELENIUM_CAPTURE_DIR = os.environ.get('SELENIUM_CAPTURE_DIR') or \
        os.path.join(basedir, 'logs', 'selenium')
    SELENIUM_CAPTURE_RETENCAO_DIAS = _env_int('SELENIUM_CAPTURE_RETENCAO_DIAS', 14)

    RS_CERT_AUTOSELECT_ENABLED = _env_bool('RS_CERT_AUTOSELECT_ENABLED', False)
    RS_CERT_AUTOSELECT_PATTERN = os.environ.get('RS_CERT_AUTOSELECT_PATTERN') or \
        'https://www.sefaz.rs.gov.br'
    RS_CERT_AUTOSELECT_POLICY_INDEX = _env_int('RS_CERT_AUTOSELECT_POLICY_INDEX', 1)
    RS_CERT_AUTOSELECT_ISSUER_CN = os.environ.get('RS_CERT_AUTOSELECT_ISSUER_CN') or ''
    RS_CERT_AUTOSELECT_SUBJECT_CN = os.environ.get('RS_CERT_AUTOSELECT_SUBJECT_CN') or ''

    RS_ALTCHA_AUTOSOLVE_ENABLED = _env_bool('RS_ALTCHA_AUTOSOLVE_ENABLED', False)
    RS_ALTCHA_MANUAL_FALLBACK = _env_bool('RS_ALTCHA_MANUAL_FALLBACK', True)

    CAPTCHA_2_API_KEY = os.environ.get('CAPTCHA_2_API_KEY') or ''
    CAPTCHA_2_DEFAULT_TIMEOUT = _env_int('CAPTCHA_2_DEFAULT_TIMEOUT', 180)
    CAPTCHA_2_POLLING_INTERVAL = _env_int('CAPTCHA_2_POLLING_INTERVAL', 10)
    CAPTCHA_2_SERVER = os.environ.get('CAPTCHA_2_SERVER') or '2captcha.com'
    # Saldo minimo (USD) abaixo do qual o agendador avisa no painel de
    # diagnostico que os creditos do 2captcha estao acabando (spec 02, SCHED-08).
    CAPTCHA_2_SALDO_MINIMO = _env_float('CAPTCHA_2_SALDO_MINIMO', 1.0)
    # Liga o agendador de emissao proativa no boot (spec 02). Desligado nos testes.
    AGENDADOR_ENABLED = _env_bool('AGENDADOR_ENABLED', True)

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # recarrega templates Jinja a cada request
    TEMPLATES_AUTO_RELOAD = True

    # --- Sessao e CSRF (auth spec 01) ---
    # Cookie de sessao endurecido; SECURE fica off no HTTP interno atual e
    # liga por env quando houver HTTPS.
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_COOKIE_SECURE = _env_bool('SESSION_COOKIE_SECURE', False)
    # Protecao CSRF global (Flask-WTF). Desligavel no ambiente de teste.
    WTF_CSRF_ENABLED = _env_bool('WTF_CSRF_ENABLED', True)