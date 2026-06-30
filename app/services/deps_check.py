"""Verificacao de dependencias criticas no boot (fail-fast acionavel).

Funcao pura (sem efeitos colaterais): o entrypoint run.py a usa para abortar
com mensagem clara quando uma dependencia obrigatoria nao esta instalada,
em vez de quebrar so na hora de emitir.
"""
import importlib

# (modulo_importavel, nome_no_requirements/pip)
CRITICAS = [
    ('undetected_chromedriver', 'undetected-chromedriver'),
    ('setuptools', 'setuptools'),
]


def dependencias_faltantes(modulos=None):
    """Retorna a lista de pacotes (nome pip) cujos modulos nao importam,
    preservando a ordem de `modulos` (default: CRITICAS)."""
    modulos = modulos if modulos is not None else CRITICAS
    faltando = []
    for modulo, pacote in modulos:
        try:
            importlib.import_module(modulo)
        except ImportError:
            faltando.append(pacote)
    return faltando
