"""Fixtures compartilhadas dos testes (pytest).

Configura o ambiente (SECRET_KEY de teste + SQLite temporario) antes de
importar o app e fornece app/client/dados semeados a cada teste, com banco
recriado e limpo por teste.
"""
import os
import tempfile

# Ambiente de teste deve estar definido ANTES de importar o app (config.py le
# SECRET_KEY/DATABASE_URL no momento do import).
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('QUIET_WERKZEUG_LOGS', 'true')
# Mantem a precondicao do lote RS deterministica (flag desligada) nos testes.
os.environ.setdefault('RS_ALTCHA_AUTOSOLVE_ENABLED', 'false')

_fd, _DBPATH = tempfile.mkstemp(suffix='.db')
os.close(_fd)
os.environ['DATABASE_URL'] = 'sqlite:///' + _DBPATH.replace(os.sep, '/')

import pytest  # noqa: E402

from app import create_app, db  # noqa: E402
from app.models import Certidao, Empresa, TipoCertidao  # noqa: E402


@pytest.fixture(scope='session')
def app():
    return create_app()


@pytest.fixture()
def ids(app):
    """Recria o schema, semeia uma empresa RS/Tramandai com as 5 certidoes
    (sem data) e devolve os ids por tipo. Limpa o schema ao final do teste."""
    with app.app_context():
        db.create_all()
        empresa = Empresa(nome='Empresa Teste', cnpj='11.111.111/1111-11',
                          estado='RS', cidade='Tramandai')
        db.session.add(empresa)
        db.session.commit()
        for tipo in TipoCertidao:
            db.session.add(Certidao(tipo=tipo, empresa=empresa))
        db.session.commit()
        mapa = {
            'fgts': Certidao.query.filter_by(tipo=TipoCertidao.FGTS).first().id,
            'rs': Certidao.query.filter_by(tipo=TipoCertidao.ESTADUAL).first().id,
            'municipal': Certidao.query.filter_by(tipo=TipoCertidao.MUNICIPAL).first().id,
            'trabalhista': Certidao.query.filter_by(tipo=TipoCertidao.TRABALHISTA).first().id,
            'empresa': empresa.id,
        }
    yield mapa
    with app.app_context():
        db.drop_all()


@pytest.fixture()
def cid(ids):
    """Id de uma certidao (trabalhista) para os endpoints de validade/pendencia."""
    return ids['trabalhista']


@pytest.fixture()
def client(app, ids):
    return app.test_client()
