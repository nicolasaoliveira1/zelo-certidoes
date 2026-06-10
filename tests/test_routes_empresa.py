"""Caracterizacao do POST /empresa/adicionar.

Todas as respostas sao redirect (302); o efeito e verificado no banco.
O conftest (fixture client/ids) ja semeia 1 empresa RS/Tramandai com
CNPJ 11.111.111/1111-11, usada no teste de duplicidade.
"""
from app.models import Certidao, Empresa, SubtipoCertidao, TipoCertidao

N_TIPOS = len(list(TipoCertidao))


def _form(**over):
    base = {
        'nome': 'Nova Empresa',
        'cnpj': '22.222.222/2222-22',
        'estado': 'RS',
        'cidade': 'Porto Alegre',
        'inscricao_mobiliaria': '',
    }
    base.update(over)
    return base


def test_adicionar_empresa_sucesso(app, client):
    r = client.post('/empresa/adicionar', data=_form())
    assert r.status_code == 302
    with app.app_context():
        emp = Empresa.query.filter_by(nome='Nova Empresa').first()
        assert emp is not None
        certs = Certidao.query.filter_by(empresa_id=emp.id).all()
        assert len(certs) == N_TIPOS  # 1 certidao por tipo (cidade != Imbe)


def test_adicionar_empresa_imbe_dois_subtipos(app, client):
    r = client.post('/empresa/adicionar', data=_form(cidade='Imbé'))
    assert r.status_code == 302
    with app.app_context():
        emp = Empresa.query.filter_by(nome='Nova Empresa').first()
        municipais = Certidao.query.filter_by(
            empresa_id=emp.id, tipo=TipoCertidao.MUNICIPAL).all()
        assert len(municipais) == 2
        subtipos = {c.subtipo for c in municipais}
        assert SubtipoCertidao.GERAL in subtipos
        assert SubtipoCertidao.MOBILIARIO in subtipos
        assert Certidao.query.filter_by(empresa_id=emp.id).count() == N_TIPOS + 1


def test_adicionar_empresa_cnpj_invalido(app, client):
    r = client.post('/empresa/adicionar', data=_form(cnpj='123'))
    assert r.status_code == 302
    with app.app_context():
        assert Empresa.query.filter_by(nome='Nova Empresa').first() is None


def test_adicionar_empresa_estado_invalido(app, client):
    r = client.post('/empresa/adicionar', data=_form(estado='Brasil'))
    assert r.status_code == 302
    with app.app_context():
        assert Empresa.query.filter_by(nome='Nova Empresa').first() is None


def test_adicionar_empresa_duplicada(app, client):
    r = client.post('/empresa/adicionar', data=_form(cnpj='11111111111111'))
    assert r.status_code == 302
    with app.app_context():
        assert Empresa.query.filter(
            Empresa.cnpj.in_({'11111111111111', '11.111.111/1111-11'})).count() == 1


def test_adicionar_empresa_inscricao_longa(app, client):
    r = client.post('/empresa/adicionar', data=_form(inscricao_mobiliaria='1234567'))
    assert r.status_code == 302
    with app.app_context():
        assert Empresa.query.filter_by(nome='Nova Empresa').first() is None
