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

    CHROME_PROFILE_DIR = os.environ.get('CHROME_PROFILE_DIR') or \
        os.path.join(basedir, 'chrome-profile')
    CHROME_PROFILE_NAME = os.environ.get('CHROME_PROFILE_NAME') or 'Certidoes'

    CAMINHO_REDE = os.environ.get('CAMINHO_REDE') or r"Z:\\PASTAS EMPRESAS"

    LOG_LEVEL = (os.environ.get('LOG_LEVEL') or 'INFO').strip().upper()
    QUIET_WERKZEUG_LOGS = _env_bool('QUIET_WERKZEUG_LOGS', True)

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
        
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # recarrega templates Jinja a cada request
    TEMPLATES_AUTO_RELOAD = True