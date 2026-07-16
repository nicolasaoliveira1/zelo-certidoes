"""Testes do snapshot_service (spec 02, SCHED-07): geração idempotente + classificação."""
from datetime import date, timedelta

import pytest

from app import db
from app.models import Certidao, SnapshotCertidao, StatusEspecial, TipoCertidao
from app.services import snapshot_service


@pytest.fixture(autouse=True)
def _reset_cache():
    # o cache de modulo persiste na sessao pytest; zera para isolar cada teste
    snapshot_service._ULTIMO_SNAPSHOT_DIA = None
    yield
    snapshot_service._ULTIMO_SNAPSHOT_DIA = None


def test_garantir_cria_snapshot_do_dia(app, ids):
    with app.app_context():
        assert SnapshotCertidao.query.count() == 0
        criado = snapshot_service.garantir_snapshot_diario()
        assert criado is True
        rows = SnapshotCertidao.query.filter_by(data=date.today()).all()
        assert len(rows) > 0
        # as 5 certidoes semeadas (sem data) somam 5 na categoria sem_data
        total = sum(r.quantidade for r in rows)
        assert total == 5


def test_garantir_idempotente_nao_duplica(app, ids):
    with app.app_context():
        snapshot_service.garantir_snapshot_diario()
        primeiro = SnapshotCertidao.query.count()
        # zera o cache para forcar a checagem no banco, e roda de novo
        snapshot_service._ULTIMO_SNAPSHOT_DIA = None
        snapshot_service.garantir_snapshot_diario()
        assert SnapshotCertidao.query.count() == primeiro


def test_classificar_status_certidao_categorias(app, ids):
    with app.app_context():
        hoje = date.today()
        cert = db.session.get(Certidao, ids['fgts'])

        cert.data_validade = None
        cert.status_especial = None
        assert snapshot_service.classificar_status_certidao(cert, hoje) == 'sem_data'

        cert.status_especial = StatusEspecial.PENDENTE
        assert snapshot_service.classificar_status_certidao(cert, hoje) == 'pendentes'

        cert.status_especial = None
        cert.data_validade = hoje - timedelta(days=1)
        assert snapshot_service.classificar_status_certidao(cert, hoje) == 'vencidas'

        cert.data_validade = hoje + timedelta(days=1)
        assert snapshot_service.classificar_status_certidao(cert, hoje) == 'a_vencer'

        cert.data_validade = hoje + timedelta(days=3650)
        assert snapshot_service.classificar_status_certidao(cert, hoje) == 'validas'


def test_garantir_snapshot_por_tipo_e_status(app, ids):
    with app.app_context():
        # da uma validade valida a FGTS -> deve cair em 'validas'
        fgts = db.session.get(Certidao, ids['fgts'])
        fgts.data_validade = date.today() + timedelta(days=3650)
        db.session.commit()
        snapshot_service.garantir_snapshot_diario()
        linha = SnapshotCertidao.query.filter_by(
            data=date.today(), tipo=TipoCertidao.FGTS.value, status='validas').first()
        assert linha is not None
        assert linha.quantidade == 1
