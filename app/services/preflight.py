"""Pre-checagens reutilizaveis antes de iniciar emissao/lote.

Reaproveita os checks individuais de health.py para falhar cedo e com
mensagem acionavel, antes de abrir o Selenium (que e caro e demorado)."""
from app.errors import ErrorType, _CATALOGO
from app.services import health


def _problema(error_type, titulo=None, acao=None, detalhe=None):
    cat_titulo, _causa, cat_acao, _rec = _CATALOGO[error_type]
    titulo = titulo or cat_titulo
    acao = acao or cat_acao
    return {
        'check': error_type.value,
        'error_type': error_type.value,
        'titulo': titulo,
        'acao': acao,
        'message': f'{titulo}: {acao}',
        'detalhe': detalhe,
    }


def checar_emissao(config, *, precisa_rede=True, precisa_chrome=True, precisa_solver=False):
    """Retorna a lista de problemas (vazia = tudo ok) para as pre-condicoes
    relevantes da emissao. Reusa health._check_* para nao duplicar logica."""
    problemas = []

    if precisa_rede:
        ok, detalhe = health._check_network_path()
        if not ok:
            problemas.append(_problema(ErrorType.NETWORK_PATH, detalhe=detalhe))

    if precisa_chrome:
        ok, detalhe = health._check_chrome_profile(config)
        if not ok:
            problemas.append(_problema(
                ErrorType.UNKNOWN,
                titulo='Perfil do Chrome indisponivel',
                acao='Verifique CHROME_PROFILE_DIR; a pasta precisa existir e ser acessivel.',
                detalhe=detalhe,
            ))

    if precisa_solver:
        ok, detalhe = health._check_solver_config(config)
        if not ok:
            problemas.append(_problema(
                ErrorType.CAPTCHA,
                titulo='Captcha nao configurado',
                acao='Defina CAPTCHA_2_API_KEY no .env para resolver o captcha automaticamente.',
                detalhe=detalhe,
            ))

    return problemas
