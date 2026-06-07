"""Testes do cálculo de alvos de lote (batch_engine.calc_targets) com banco."""
from datetime import date, timedelta

from app import db
from app.models import Certidao, StatusEspecial, TipoCertidao
from app.services import batch_engine


def test_calc_targets_default(app, ids):
    with app.app_context():
        fgts = Certidao.query.filter_by(tipo=TipoCertidao.FGTS).first()
        est = Certidao.query.filter_by(tipo=TipoCertidao.ESTADUAL).first()
        muni = Certidao.query.filter_by(tipo=TipoCertidao.MUNICIPAL).first()
        fgts.data_validade = date.today() - timedelta(days=5)    # vencida
        est.data_validade = date.today() + timedelta(days=3)     # a vencer (<= 7)
        muni.data_validade = date.today() + timedelta(days=100)  # fora do limite
        db.session.commit()

        dados = batch_engine.calc_targets(fgts.id, scope='default')
        assert fgts.id in dados['ids']
        assert est.id in dados['ids']
        assert muni.id not in dados['ids']      # alem do limite 'a vencer'
        assert dados['ids'][0] == fgts.id        # certidao de inicio vem primeiro
        assert dados['vencidas'] >= 1
        assert dados['a_vencer'] >= 1
        assert dados['scope'] == 'default'


def test_calc_targets_pendentes(app, ids):
    with app.app_context():
        fgts = Certidao.query.filter_by(tipo=TipoCertidao.FGTS).first()
        fgts.status_especial = StatusEspecial.PENDENTE
        fgts.data_validade = None
        db.session.commit()

        dados = batch_engine.calc_targets(fgts.id, scope='pendentes')
        assert dados['ids'] == [fgts.id]
        assert dados['scope'] == 'pendentes'
        assert dados['pendentes'] >= 1


def test_calc_targets_vazio_quando_sem_data(app, ids):
    # certidoes semeadas sem data nao entram no escopo default
    with app.app_context():
        fgts = Certidao.query.filter_by(tipo=TipoCertidao.FGTS).first()
        dados = batch_engine.calc_targets(fgts.id, scope='default')
        assert dados['ids'] == []
        assert dados['total'] == 0
