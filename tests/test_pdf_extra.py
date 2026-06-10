"""Testes de pdf.py: extracao de texto/validade e tratamento de POSITIVA.

pdfplumber e mockado com um PDF falso; o tratamento de POSITIVA usa um
arquivo real em tmp_path + a certidao semeada pelo conftest.
"""
import os
from datetime import date

from app.automation import pdf
from app.models import Certidao, StatusEspecial, TipoCertidao


class _FakePage:
    def __init__(self, texto):
        self._texto = texto

    def extract_text(self):
        return self._texto


class _FakePdf:
    def __init__(self, paginas):
        self.pages = paginas

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_extrair_texto(monkeypatch):
    fake = _FakePdf([_FakePage('linha 1'), _FakePage('linha 2')])
    monkeypatch.setattr(pdf.pdfplumber, 'open', lambda _c: fake)
    assert pdf.extrair_texto('x.pdf') == 'linha 1\nlinha 2'


def test_extrair_texto_sem_caminho():
    assert pdf.extrair_texto('') == ''
    assert pdf.extrair_texto(None) == ''


def test_extrair_texto_erro_retorna_vazio(monkeypatch):
    def _boom(_c):
        raise OSError('arquivo corrompido')
    monkeypatch.setattr(pdf.pdfplumber, 'open', _boom)
    assert pdf.extrair_texto('x.pdf') == ''


def test_extrair_validade_federal(monkeypatch):
    fake = _FakePdf([_FakePage('Certidão Válida até 31/12/2030 conforme...')])
    monkeypatch.setattr(pdf.pdfplumber, 'open', lambda _c: fake)
    assert pdf.extrair_validade_federal('x.pdf') == date(2030, 12, 31)


def test_extrair_validade_federal_sem_padrao(monkeypatch):
    fake = _FakePdf([_FakePage('documento sem data de validade')])
    monkeypatch.setattr(pdf.pdfplumber, 'open', lambda _c: fake)
    assert pdf.extrair_validade_federal('x.pdf') is None


def test_classificar_e_tratar_positivo(app, ids, tmp_path, monkeypatch):
    # POSITIVA: apaga o arquivo e marca a certidao como PENDENTE.
    arq = tmp_path / 'cert.pdf'
    arq.write_bytes(b'%PDF positiva')
    monkeypatch.setattr(pdf, 'classificar_status', lambda *a, **k: 'positiva')

    with app.app_context():
        cert = Certidao.query.filter_by(tipo=TipoCertidao.FGTS).first()
        classe, msg = pdf.classificar_e_tratar_positivo(cert, str(arq))
        assert classe == 'positiva'
        assert not os.path.exists(str(arq))
        assert cert.status_especial == StatusEspecial.PENDENTE
        assert cert.data_validade is None


def test_classificar_e_tratar_nao_positivo(app, ids, tmp_path, monkeypatch):
    arq = tmp_path / 'cert.pdf'
    arq.write_bytes(b'%PDF negativa')
    monkeypatch.setattr(pdf, 'classificar_status', lambda *a, **k: 'negativa')

    with app.app_context():
        cert = Certidao.query.filter_by(tipo=TipoCertidao.FGTS).first()
        classe, msg = pdf.classificar_e_tratar_positivo(cert, str(arq))
        assert classe == 'negativa'
        assert msg is None
        assert os.path.exists(str(arq))
