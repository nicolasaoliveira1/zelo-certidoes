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
# Nao escreve arquivo de log em disco durante os testes.
os.environ.setdefault('LOG_JSON_FILE', 'false')
# Nao sobe a thread escritora de diagnostico nos testes (sem efeitos colaterais).
os.environ.setdefault('DIAGNOSTICO_PERSISTIR', 'false')
# Mantem a precondicao do lote RS deterministica (flag desligada) nos testes.
os.environ.setdefault('RS_ALTCHA_AUTOSOLVE_ENABLED', 'false')
# Nao sobe o agendador (BackgroundScheduler) nos testes; os testes do agendador
# ligam explicitamente via app.config quando precisam.
os.environ.setdefault('AGENDADOR_ENABLED', 'false')
# Sem chave 2captcha por padrao nos testes: consultar_saldo vira no-op (None) e
# nenhum teste bate na API real. Os testes que precisam mockam/injetam a chave.
os.environ.setdefault('CAPTCHA_2_API_KEY', '')
# CSRF desligado no ambiente de teste (o client nao envia token); a imposicao de
# CSRF e provada num teste dedicado que religa a flag (tests/test_csrf.py).
os.environ.setdefault('WTF_CSRF_ENABLED', 'false')

# Opt-in de banco de teste: se TEST_DATABASE_URL estiver setado (ex.: o job de CI
# aponta para um MySQL de servico), a suite roda contra ele — e como o schema de
# cada teste e construido por db.create_all()/drop_all(), isso exercita enum
# nativo/colacao/DateTime no banco real (paridade com producao, spec 06). Sem a
# variavel, mantem o SQLite temporario de sempre (rapido, gate local).
_TEST_DB_URL = os.environ.get('TEST_DATABASE_URL')
if _TEST_DB_URL:
    os.environ['DATABASE_URL'] = _TEST_DB_URL
else:
    _fd, _DBPATH = tempfile.mkstemp(suffix='.db')
    os.close(_fd)
    os.environ['DATABASE_URL'] = 'sqlite:///' + _DBPATH.replace(os.sep, '/')

# Diretorio existente para que o preflight (rede/Chrome) passe de forma
# deterministica nos testes, independente da maquina/CI.
_TMPDIR = tempfile.mkdtemp()
os.environ.setdefault('CAMINHO_REDE', _TMPDIR)
os.environ.setdefault('CHROME_PROFILE_DIR', _TMPDIR)

import pytest  # noqa: E402

from app import create_app, db  # noqa: E402
from app.models import Certidao, Empresa, TipoCertidao, Usuario  # noqa: E402

# Credenciais por papel usadas pelos fixtures de client autenticado.
USUARIOS_TESTE = {
    'admin': ('admin_test', 'senha-admin-1'),
    'operador': ('op_test', 'senha-op-1'),
    'leitura': ('leitura_test', 'senha-leitura-1'),
}


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
        # usuarios por papel (papel_key coincide com o valor de PapelUsuario)
        for papel_key, (uname, senha) in USUARIOS_TESTE.items():
            u = Usuario(username=uname, papel=papel_key)
            u.set_senha(senha)
            db.session.add(u)
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
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def cid(ids):
    """Id de uma certidao (trabalhista) para os endpoints de validade/pendencia."""
    return ids['trabalhista']


@pytest.fixture()
def client(app, ids):
    """Client autenticado como admin (a maioria dos testes de rota opera assim)."""
    c = app.test_client()
    uname, senha = USUARIOS_TESTE['admin']
    c.post('/login', data={'username': uname, 'senha': senha})
    return c


@pytest.fixture()
def client_anon(app, ids):
    """Client sem login, para testar enforcement de autenticacao."""
    return app.test_client()


@pytest.fixture()
def login_as(app, ids):
    """Fabrica um client autenticado com o papel pedido: login_as('operador')."""
    def _login(papel):
        c = app.test_client()
        uname, senha = USUARIOS_TESTE[papel]
        c.post('/login', data={'username': uname, 'senha': senha})
        return c
    return _login
