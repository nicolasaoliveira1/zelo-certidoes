"""Testes do cálculo de alvos de lote (batch_engine.calc_targets) com banco."""
from datetime import date, timedelta

from app import db
from app.models import (
    Certidao,
    ConfiguracaoSistema,
    Empresa,
    StatusEspecial,
    TipoCertidao,
)
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


def test_calc_targets_respeita_prazo_do_tipo(app, ids):
    # FGTS com prazo menor que o global nao deve contar certidoes alem da
    # propria janela so porque outro tipo tem prazo maior.
    with app.app_context():
        config = ConfiguracaoSistema.query.get(1)
        if config is None:
            config = ConfiguracaoSistema(id=1, a_vencer_dias=7)
            db.session.add(config)
        config.a_vencer_dias = 7
        config.a_vencer_dias_fgts = 3

        fgts = Certidao.query.filter_by(tipo=TipoCertidao.FGTS).first()
        fgts.data_validade = date.today() + timedelta(days=5)  # fora da janela FGTS (3)
        db.session.commit()

        dados = batch_engine.calc_targets(
            fgts.id, scope='default', tipo=TipoCertidao.FGTS
        )
        assert fgts.id not in dados['ids']
        assert dados['a_vencer'] == 0


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


def test_pendente_com_data_vencida_excluido_do_escopo_default(app, ids):
    # Certidao PENDENTE com data_validade no passado deve ser EXCLUIDA do escopo
    # 'default' (assim como o dashboard a classifica como 'pendentes', nao 'vencidas').
    with app.app_context():
        fgts = Certidao.query.filter_by(tipo=TipoCertidao.FGTS).first()
        fgts.status_especial = StatusEspecial.PENDENTE
        fgts.data_validade = date.today() - timedelta(days=10)  # vencida, mas PENDENTE
        db.session.commit()

        dados = batch_engine.calc_targets(fgts.id, scope='default')
        assert fgts.id not in dados['ids']
        assert dados['vencidas'] == 0

        # no escopo 'pendentes' ela deve aparecer normalmente
        dados_p = batch_engine.calc_targets(fgts.id, scope='pendentes')
        assert fgts.id in dados_p['ids']


def test_calc_targets_vazio_quando_sem_data(app, ids):
    # certidoes semeadas sem data nao entram no escopo default
    with app.app_context():
        fgts = Certidao.query.filter_by(tipo=TipoCertidao.FGTS).first()
        dados = batch_engine.calc_targets(fgts.id, scope='default')
        assert dados['ids'] == []
        assert dados['total'] == 0


def test_start_incluida_false_para_nao_definida_com_outras_vencidas(app, ids):
    # Bug: clicar em emitir numa certidao "Nao definida" (sem validade) abria o
    # modal de lote so porque OUTRAS do mesmo tipo estavam vencidas. A clicada
    # nao pertence ao lote -> start_incluida deve ser False (front emite so ela).
    with app.app_context():
        clicada = Certidao.query.filter_by(tipo=TipoCertidao.FGTS).first()
        clicada.data_validade = None                              # "Nao definida"
        # segunda empresa com FGTS vencida (compoe o "lote" de outras)
        outra_empresa = Empresa(nome='Outra', cnpj='22.222.222/2222-22',
                                estado='RS', cidade='Tramandai')
        db.session.add(outra_empresa)
        db.session.commit()
        outra = Certidao(tipo=TipoCertidao.FGTS, empresa=outra_empresa,
                         data_validade=date.today() - timedelta(days=5))
        db.session.add(outra)
        db.session.commit()

        dados = batch_engine.calc_targets(clicada.id, scope='default')
        assert dados['start_incluida'] is False
        assert clicada.id not in dados['ids']
        assert outra.id in dados['ids']


def test_start_incluida_true_quando_clicada_no_lote(app, ids):
    with app.app_context():
        fgts = Certidao.query.filter_by(tipo=TipoCertidao.FGTS).first()
        fgts.data_validade = date.today() - timedelta(days=5)     # vencida
        db.session.commit()

        dados = batch_engine.calc_targets(fgts.id, scope='default')
        assert dados['start_incluida'] is True
        assert dados['ids'][0] == fgts.id
