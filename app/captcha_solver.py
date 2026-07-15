import json
import time

from twocaptcha import TwoCaptcha

from app.errors import ErrorType, map_exception_to_error_type
from app.services.correlation import CorrelationContext
from app.services.execution_logger import log_event
from app.services.retry import retry_call


class AltchaSolverError(Exception):
    pass


class AltchaSolverConfigError(AltchaSolverError):
    pass


class AltchaSolverRuntimeError(AltchaSolverError):
    pass


def _parse_int(value, default_value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default_value


def consultar_saldo(config):
    """Consulta o saldo (USD) da conta 2captcha. Best-effort (spec 02, SCHED-08):

    retorna float em sucesso; None se não há chave configurada ou se a consulta
    falha (rede/API). Nunca levanta — o agendador usa o resultado só para avisar
    sobre créditos baixos, então uma falha aqui não pode derrubar o job."""
    api_key = (config.get('CAPTCHA_2_API_KEY') or '').strip()
    if not api_key:
        return None
    server = (config.get('CAPTCHA_2_SERVER') or '2captcha.com').strip() or '2captcha.com'
    try:
        solver = TwoCaptcha(apiKey=api_key, server=server)
        return float(solver.balance())
    except Exception as exc:
        log_event('captcha_saldo_consulta_falhou', level='WARNING', error=str(exc))
        return None


def _extract_code(result):
    if isinstance(result, str):
        return result.strip()

    if isinstance(result, dict):
        for key in ('token', 'code', 'text'):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        solution = result.get('solution')
        if isinstance(solution, dict):
            for key in ('token', 'code', 'text'):
                value = solution.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

        if isinstance(solution, str) and solution.strip():
            return solution.strip()

    return ''


def solve_altcha(config, page_url, challenge_json=None, challenge_url=None, execution_id=None):
    if execution_id:
        CorrelationContext.set_execution_id(execution_id)

    api_key = (config.get('CAPTCHA_2_API_KEY') or '').strip()
    if not api_key:
        raise AltchaSolverConfigError('CAPTCHA_2_API_KEY não configurada.')

    if not challenge_json and not challenge_url:
        raise AltchaSolverConfigError('Challenge ALTCHA não disponível para envio ao 2captcha.')

    server = (config.get('CAPTCHA_2_SERVER') or '2captcha.com').strip() or '2captcha.com'
    solver = TwoCaptcha(
        apiKey=api_key,
        server=server,
        defaultTimeout=_parse_int(config.get('CAPTCHA_2_DEFAULT_TIMEOUT'), 180),
        pollingInterval=_parse_int(config.get('CAPTCHA_2_POLLING_INTERVAL'), 10),
    )

    payload = {
        'pageurl': page_url,
    }

    if challenge_json:
        if isinstance(challenge_json, (dict, list)):
            payload['challenge_json'] = json.dumps(challenge_json, separators=(',', ':'))
        else:
            payload['challenge_json'] = str(challenge_json)
    else:
        payload['challenge_url'] = str(challenge_url)

    def _resolver():
        return solver.altcha(**payload)

    def _retry_if(exc):
        err_type = map_exception_to_error_type(exc)
        return err_type in {ErrorType.TIMEOUT, ErrorType.NETWORK_PATH, ErrorType.PORTAL}

    start_time = time.time()
    try:
        result = retry_call(
            _resolver,
            max_attempts=3,
            base_delay=0.5,
            jitter=0.2,
            retry_if=_retry_if,
            on_retry=lambda attempt, delay, exc: log_event(
                'altcha_retry',
                level='WARNING',
                attempt=attempt,
                delay_ms=int(delay * 1000),
                error_type=map_exception_to_error_type(exc).value,
                error=str(exc),
            ),
        )
    except Exception as exc:
        err_type = map_exception_to_error_type(exc)
        log_event(
            'altcha_error',
            level='ERROR',
            error_type=err_type.value,
            error=str(exc),
            server=server,
        )
        raise AltchaSolverRuntimeError(f'Falha ao resolver ALTCHA no 2captcha: {exc}') from exc

    duration_ms = int((time.time() - start_time) * 1000)
    code = _extract_code(result)
    if not code:
        raise AltchaSolverRuntimeError(f'Resposta ALTCHA sem token reutilizável: {result}')

    log_event('altcha_solved', status='ok', duration_ms=duration_ms, server=server)

    return {
        'code': code,
        'raw': result,
    }


def solve_normal_captcha(config, image_path=None, execution_id=None):
    if execution_id:
        CorrelationContext.set_execution_id(execution_id)

    api_key = (config.get('CAPTCHA_2_API_KEY') or '').strip()
    if not api_key:
        raise AltchaSolverConfigError('CAPTCHA_2_API_KEY não configurada.')

    if not image_path:
        raise AltchaSolverConfigError('Imagem do captcha não informada para o 2captcha.')

    server = (config.get('CAPTCHA_2_SERVER') or '2captcha.com').strip() or '2captcha.com'
    solver = TwoCaptcha(
        apiKey=api_key,
        server=server,
        defaultTimeout=_parse_int(config.get('CAPTCHA_2_DEFAULT_TIMEOUT'), 180),
        pollingInterval=_parse_int(config.get('CAPTCHA_2_POLLING_INTERVAL'), 10),
    )

    def _resolver():
        return solver.normal(image_path)

    def _retry_if(exc):
        err_type = map_exception_to_error_type(exc)
        return err_type in {ErrorType.TIMEOUT, ErrorType.NETWORK_PATH, ErrorType.PORTAL}

    start_time = time.time()
    try:
        result = retry_call(
            _resolver,
            max_attempts=3,
            base_delay=0.5,
            jitter=0.2,
            retry_if=_retry_if,
            on_retry=lambda attempt, delay, exc: log_event(
                'normal_captcha_retry',
                level='WARNING',
                attempt=attempt,
                delay_ms=int(delay * 1000),
                error_type=map_exception_to_error_type(exc).value,
                error=str(exc),
            ),
        )
    except Exception as exc:
        err_type = map_exception_to_error_type(exc)
        log_event(
            'normal_captcha_error',
            level='ERROR',
            error_type=err_type.value,
            error=str(exc),
            server=server,
        )
        raise AltchaSolverRuntimeError(f'Falha ao resolver captcha de imagem no 2captcha: {exc}') from exc

    duration_ms = int((time.time() - start_time) * 1000)
    code = _extract_code(result)
    if not code:
        raise AltchaSolverRuntimeError(f'Resposta de captcha sem texto utilizável: {result}')

    log_event('normal_captcha_solved', status='ok', duration_ms=duration_ms, server=server)

    return {
        'code': code,
        'raw': result,
    }
