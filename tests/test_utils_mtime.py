"""Testes do helper mtime_para_datetime_local (ORD-08, parte de I/O).

Cobre os quatro ramos: arquivo existente -> datetime local; caminho None -> None;
caminho vazio -> None; caminho inexistente (erro de I/O) -> None.
"""
import os
import tempfile
from datetime import datetime

from app.utils import mtime_para_datetime_local


def test_arquivo_existente_retorna_mtime_local():
    fd, caminho = tempfile.mkstemp(suffix='.pdf')
    os.close(fd)
    try:
        esperado = datetime.fromtimestamp(os.path.getmtime(caminho))
        resultado = mtime_para_datetime_local(caminho)
        assert resultado == esperado
        assert resultado.tzinfo is None  # hora local naive
    finally:
        os.unlink(caminho)


def test_caminho_none_retorna_none():
    assert mtime_para_datetime_local(None) is None


def test_caminho_vazio_retorna_none():
    assert mtime_para_datetime_local('') is None


def test_arquivo_inexistente_retorna_none():
    assert mtime_para_datetime_local('/caminho/que/nao/existe/xyz.pdf') is None
