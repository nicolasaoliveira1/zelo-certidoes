"""Testes do carimbo automatico Certidao.atualizado_em (ORD-01, ORD-02).

Deriva da AC P1-1 do spec: "WHEN uma Certidao e criada ou tem qualquer campo
alterado e persistido THEN o sistema SHALL gravar/atualizar seu timestamp de
ultima atualizacao automaticamente".
"""
from datetime import date, datetime

from app import db
from app.models import Certidao, Empresa, TipoCertidao


def _empresa():
    emp = Empresa(nome='Carimbo SA', cnpj='55.555.555/5555-55',
                  estado='RS', cidade='Tramandai')
    db.session.add(emp)
    db.session.commit()
    return emp


def test_atualizado_em_preenchido_na_criacao(app, ids):
    """Criar uma Certidao carimba atualizado_em (default na INSERT)."""
    with app.app_context():
        emp = _empresa()
        antes = datetime.now()
        cert = Certidao(tipo=TipoCertidao.FEDERAL, empresa=emp)
        db.session.add(cert)
        db.session.commit()
        depois = datetime.now()
        assert cert.atualizado_em is not None
        # carimbo em hora local, dentro da janela do teste
        assert antes <= cert.atualizado_em <= depois


def test_atualizado_em_avanca_no_update(app, ids):
    """Alterar um campo e commitar avanca atualizado_em (onupdate na UPDATE)."""
    with app.app_context():
        emp = _empresa()
        cert = Certidao(tipo=TipoCertidao.FEDERAL, empresa=emp)
        db.session.add(cert)
        db.session.commit()
        inicial = cert.atualizado_em
        assert inicial is not None
        # forca um instante depois para o onupdate render um valor maior
        cert.data_validade = date(2030, 1, 1)
        db.session.commit()
        assert cert.atualizado_em is not None
        assert cert.atualizado_em >= inicial
