from enum import Enum


class ErrorType(str, Enum):
    TIMEOUT = 'TIMEOUT'
    CAPTCHA = 'CAPTCHA'
    PORTAL = 'PORTAL'
    SELECTOR = 'SELECTOR'
    NETWORK_PATH = 'NETWORK_PATH'
    PERMISSION = 'PERMISSION'
    DB = 'DB'
    UNKNOWN = 'UNKNOWN'


class ExecutionError(Exception):
    def __init__(self, message, error_type=ErrorType.UNKNOWN, retry_eligible=False, cause=None):
        super().__init__(message)
        self.message = message
        self.error_type = error_type
        self.retry_eligible = retry_eligible
        self.cause = cause


def map_exception_to_error_type(exc):
    text = str(exc or '').upper()
    name = exc.__class__.__name__.upper() if exc else ''

    if 'TIMEOUT' in name or 'TIMEOUT' in text:
        return ErrorType.TIMEOUT

    if 'CAPTCHA' in text or 'ALTCHA' in text or '2CAPTCHA' in text:
        return ErrorType.CAPTCHA

    if 'PERMISSION' in name or 'ACCESS IS DENIED' in text or 'PERMISSAO' in text:
        return ErrorType.PERMISSION

    if 'NOSUCHELEMENT' in name or 'STALEELEMENT' in name or 'SELECTOR' in text:
        return ErrorType.SELECTOR

    if 'NETWORK' in text or 'Z:' in text or 'PATH' in text:
        return ErrorType.NETWORK_PATH

    if 'ECONN' in text or 'CONNECTION' in text or 'CONNREFUSED' in text or 'CONNRESET' in text:
        return ErrorType.NETWORK_PATH

    if 'DNS' in text or 'NAME RESOLUTION' in text:
        return ErrorType.NETWORK_PATH

    if 'SQL' in text or 'DATABASE' in text or 'DB' in text:
        return ErrorType.DB

    if 'WEBDRIVER' in name or 'SELENIUM' in text or 'PORTAL' in text:
        return ErrorType.PORTAL

    return ErrorType.UNKNOWN
