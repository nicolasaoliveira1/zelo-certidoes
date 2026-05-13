import os
import time


def _timed_check(check_fn):
    start = time.time()
    try:
        ok, detail = check_fn()
    except Exception as exc:
        ok, detail = False, str(exc)
    latency_ms = int((time.time() - start) * 1000)
    return {'ok': ok, 'detail': detail, 'latency_ms': latency_ms}


def _check_db():
    from app import db

    db.session.execute(db.text('SELECT 1'))
    return True, 'ok'


def _check_network_path():
    from app import file_manager

    path = file_manager.CAMINHO_REDE
    exists = os.path.exists(path)
    readable = os.access(path, os.R_OK) if exists else False
    writable = os.access(path, os.W_OK) if exists else False
    return exists, {
        'path': path,
        'exists': exists,
        'readable': readable,
        'writable': writable,
    }


def _check_chrome_profile(config):
    profile_dir = config.get('CHROME_PROFILE_DIR')
    if not profile_dir:
        return False, 'CHROME_PROFILE_DIR nao configurado'
    exists = os.path.isdir(profile_dir)
    readable = os.access(profile_dir, os.R_OK) if exists else False
    writable = os.access(profile_dir, os.W_OK) if exists else False
    return exists, {
        'path': profile_dir,
        'exists': exists,
        'readable': readable,
        'writable': writable,
    }


def _check_solver_config(config):
    key = (config.get('CAPTCHA_2_API_KEY') or '').strip()
    server = (config.get('CAPTCHA_2_SERVER') or '2captcha.com').strip() or '2captcha.com'
    return bool(key), {
        'status': 'ok' if key else 'missing',
        'server': server,
        'message': 'CAPTCHA_2_API_KEY ausente' if not key else 'ok',
    }


def run_health_checks(config):
    return {
        'db': _timed_check(_check_db),
        'network_path': _timed_check(_check_network_path),
        'chrome_profile': _timed_check(lambda: _check_chrome_profile(config)),
        'solver': _timed_check(lambda: _check_solver_config(config)),
    }
