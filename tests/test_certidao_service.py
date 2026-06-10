"""Testes unitarios de certidao_service: sucesso + rollback em erro de DB."""
from datetime import date, timedelta
from unittest.mock import patch

from app import db
from app.models import Certidao, StatusEspecial, TipoCertidao
from app.services import certidao_service


def test_aplicar_validade_sucesso(app, ids):
    with app.app_context():
        cert = Certidao.query.filter_by(tipo=TipoCertidao.FGTS).first()
        nova = date.today() + timedelta(days=30)
        ok, erro = certidao_service.aplicar_validade(cert, nova)
        assert ok is True and erro is None
        assert cert.data_validade == nova
        assert cert.status_especial is None


def test_aplicar_validade_erro_db(app, ids):
    with app.app_context():
        cert = Certidao.query.filter_by(tipo=TipoCertidao.FGTS).first()
        with patch.object(db.session, 'commit', side_effect=Exception('boom')):
            ok, erro = certidao_service.aplicar_validade(cert, date.today())
        assert ok is False
        assert 'boom' in erro


def test_marcar_pendente_sucesso(app, ids):
    with app.app_context():
        cert = Certidao.query.filter_by(tipo=TipoCertidao.FGTS).first()
        ok, erro = certidao_service.marcar_pendente(cert)
        assert ok is True and erro is None
        assert cert.status_especial == StatusEspecial.PENDENTE
        assert cert.data_validade is None


def test_marcar_pendente_erro_db(app, ids):
    with app.app_context():
        cert = Certidao.query.filter_by(tipo=TipoCertidao.FGTS).first()
        with patch.object(db.session, 'commit', side_effect=Exception('falha')):
            ok, erro = certidao_service.marcar_pendente(cert)
        assert ok is False
        assert 'falha' in erro
