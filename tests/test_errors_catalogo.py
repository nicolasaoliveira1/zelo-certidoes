"""Testes do catalogo de erros acionaveis (descrever_erro/mensagem_usuario)."""
from selenium.common.exceptions import TimeoutException

from app.errors import (
    ErrorType,
    _CATALOGO,
    descrever_erro,
    mensagem_usuario,
)


def test_catalogo_cobre_todos_os_tipos():
    assert set(_CATALOGO) == set(ErrorType)


def test_descrever_erro_classifica_timeout():
    info = descrever_erro(TimeoutException('demorou'))
    assert info.tipo is ErrorType.TIMEOUT
    assert info.recuperavel is True
    assert 'demorou' in info.detalhe


def test_descrever_erro_aplica_contexto():
    info = descrever_erro(Exception('Z: nao encontrado'), contexto='FGTS lote')
    assert info.tipo is ErrorType.NETWORK_PATH
    assert '(FGTS lote)' in info.causa


def test_mensagem_usuario_junta_titulo_e_acao():
    msg = mensagem_usuario(Exception('Access is denied'))
    assert msg.startswith('Permissao negada:')
    assert 'permiss' in msg.lower()
