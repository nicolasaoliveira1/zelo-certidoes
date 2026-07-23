"""Testes do dossie PDF por empresa (spec 04, EXPORT-03/EXPORT-04).

Prova: so entram certidoes validas (verde/amarelo) com PDF; ordem de exibicao;
PDF ausente/corrompido e pulado com aviso sem quebrar; nenhuma valida -> None;
a capa carrega os dados da empresa; o PDF final abre e tem o numero de paginas
esperado.
"""
from datetime import date, timedelta

import pytest
from fpdf import FPDF
from pypdf import PdfReader

from app import db
from app.models import Certidao, Empresa, StatusEspecial, TipoCertidao
from app.services import dossie_service

HOJE = date.today()


def _pdf_valido(caminho, texto):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font('Helvetica', '', 12)
    pdf.multi_cell(0, 10, text=texto)
    caminho.write_bytes(bytes(pdf.output()))
    return str(caminho)


def _cert(tipo, *, validade=None, pendente=False, caminho=None):
    c = Certidao(tipo=tipo)
    if pendente:
        c.status_especial = StatusEspecial.PENDENTE
    if validade is not None:
        c.data_validade = validade
    if caminho is not None:
        c.caminho_arquivo = caminho
    return c


@pytest.fixture()
def empresa_mista(app, tmp_path):
    """Empresa com: FEDERAL valida (PDF ok), FGTS vencida (PDF ok, deve sair),
    ESTADUAL valida com PDF corrompido (deve pular c/ aviso), MUNICIPAL valida
    sem PDF (deve sair)."""
    fed = _pdf_valido(tmp_path / 'fed.pdf', 'CERT-FEDERAL')
    fgts = _pdf_valido(tmp_path / 'fgts.pdf', 'CERT-FGTS')
    corrompido = tmp_path / 'estadual.pdf'
    corrompido.write_bytes(b'%PDF-1.4 isto nao e um pdf valido')
    with app.app_context():
        db.create_all()
        emp = Empresa(nome='Alfa Ltda', cnpj='12.345.678/0001-90', estado='RS', cidade='Tramandaí')
        emp.certidoes = [
            _cert(TipoCertidao.FEDERAL, validade=HOJE + timedelta(days=3650), caminho=fed),
            _cert(TipoCertidao.FGTS, validade=HOJE - timedelta(days=10), caminho=fgts),
            _cert(TipoCertidao.ESTADUAL, validade=HOJE + timedelta(days=3650), caminho=str(corrompido)),
            _cert(TipoCertidao.MUNICIPAL, validade=HOJE + timedelta(days=3650), caminho=None),
        ]
        db.session.add(emp)
        db.session.commit()
        yield emp.id
        db.session.remove()
        db.drop_all()


def test_certidoes_do_dossie_so_validas_com_pdf(app, empresa_mista):
    with app.app_context():
        emp = db.session.get(Empresa, empresa_mista)
        tipos = [c.tipo for c, _ in dossie_service.certidoes_do_dossie(emp)]
        # FEDERAL (valida, PDF ok) e ESTADUAL (valida, arquivo existe) entram na
        # selecao; FGTS (vencida) e MUNICIPAL (sem caminho) ficam de fora.
        assert TipoCertidao.FEDERAL in tipos
        assert TipoCertidao.ESTADUAL in tipos
        assert TipoCertidao.FGTS not in tipos
        assert TipoCertidao.MUNICIPAL not in tipos


def test_ordem_de_exibicao_preservada(app, empresa_mista):
    with app.app_context():
        emp = db.session.get(Empresa, empresa_mista)
        tipos = [c.tipo for c, _ in dossie_service.certidoes_do_dossie(emp)]
        # Federal (ordem 1) antes de Estadual (ordem 3)
        assert tipos.index(TipoCertidao.FEDERAL) < tipos.index(TipoCertidao.ESTADUAL)


def test_pula_pdf_corrompido_com_aviso(app, empresa_mista):
    with app.app_context():
        emp = db.session.get(Empresa, empresa_mista)
        buffer, avisos = dossie_service.gerar_dossie(emp)
        assert buffer is not None
        # o PDF corrompido (Estadual) gera aviso; o dossie segue com a Federal
        assert any('Estadual' in a for a in avisos)


def test_pdf_final_abre_e_conta_paginas(app, empresa_mista):
    with app.app_context():
        emp = db.session.get(Empresa, empresa_mista)
        buffer, _ = dossie_service.gerar_dossie(emp)
        leitor = PdfReader(buffer)
        # capa (1) + Federal (1). Estadual corrompida foi pulada.
        assert len(leitor.pages) == 2


def test_capa_contem_dados_da_empresa(app, empresa_mista):
    with app.app_context():
        emp = db.session.get(Empresa, empresa_mista)
        buffer, _ = dossie_service.gerar_dossie(emp)
        texto_capa = PdfReader(buffer).pages[0].extract_text() or ''
        assert 'Alfa Ltda' in texto_capa
        assert '12.345.678/0001-90' in texto_capa


def test_empresa_sem_validas_retorna_none(app):
    with app.app_context():
        db.create_all()
        try:
            emp = Empresa(nome='Beta', cnpj='00.000.000/0002-00', estado='SC', cidade='Imbé')
            emp.certidoes = [
                _cert(TipoCertidao.FGTS, validade=HOJE - timedelta(days=10)),  # vencida
                _cert(TipoCertidao.MUNICIPAL, pendente=True),                   # pendente
            ]
            db.session.add(emp)
            db.session.commit()
            buffer, avisos = dossie_service.gerar_dossie(emp)
            assert buffer is None
            assert any('sem certid' in a.lower() for a in avisos)
        finally:
            db.session.remove()
            db.drop_all()


def test_todas_corrompidas_retorna_none(app, tmp_path):
    with app.app_context():
        db.create_all()
        try:
            ruim = tmp_path / 'ruim.pdf'
            ruim.write_bytes(b'nao e pdf')
            emp = Empresa(nome='Gama', cnpj='00.000.000/0003-00', estado='RS', cidade='Osório')
            emp.certidoes = [
                _cert(TipoCertidao.FEDERAL, validade=HOJE + timedelta(days=3650), caminho=str(ruim)),
            ]
            db.session.add(emp)
            db.session.commit()
            buffer, avisos = dossie_service.gerar_dossie(emp)
            assert buffer is None
            assert avisos  # ao menos o aviso do PDF ignorado
        finally:
            db.session.remove()
            db.drop_all()
