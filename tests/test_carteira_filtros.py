"""Testes de `carteira_filtros` (spec 04, EXPORT-02).

Prova que o recorte server-side reproduz a semantica do filtro do painel
(`aplicarFiltros` em dashboard.html): estado/cidade filtram a empresa inteira;
tipo/status filtram a certidao; vazio/'todas' = sem filtro. Se este servico
divergir do painel, a planilha exportada nao "sai igual a tela".
"""
from datetime import date, timedelta

import pytest

from app import db
from app.models import (
    Certidao,
    Empresa,
    StatusEspecial,
    TipoCertidao,
)
from app.services import carteira_filtros as cf

HOJE = date.today()


def _cert(tipo, *, validade=None, pendente=False):
    c = Certidao(tipo=tipo)
    if pendente:
        c.status_especial = StatusEspecial.PENDENTE
    if validade is not None:
        c.data_validade = validade
    return c


@pytest.fixture()
def carteira(app):
    """Semeia duas empresas em estados/cidades distintos, cobrindo as 5 categorias
    de status. Recria e limpa o schema por teste."""
    with app.app_context():
        db.create_all()
        # Empresa A — RS / Tramandai
        a = Empresa(nome='Alfa', cnpj='00.000.000/0001-00', estado='RS', cidade='Tramandaí')
        a.certidoes = [
            _cert(TipoCertidao.FEDERAL, validade=HOJE + timedelta(days=3650)),   # validas
            _cert(TipoCertidao.FGTS, validade=HOJE - timedelta(days=10)),        # vencidas
            _cert(TipoCertidao.MUNICIPAL, pendente=True),                        # pendentes
        ]
        # Empresa B — SC / Imbe (com acento para exercitar a chave de cidade)
        b = Empresa(nome='Beta', cnpj='00.000.000/0002-00', estado='SC', cidade='Imbé')
        b.certidoes = [
            _cert(TipoCertidao.ESTADUAL, validade=HOJE + timedelta(days=3)),     # a_vencer (limite 7)
            _cert(TipoCertidao.TRABALHISTA, validade=None),                      # nao_definida
        ]
        db.session.add_all([a, b])
        db.session.commit()
        yield {'a': a.id, 'b': b.id}
        db.session.remove()
        db.drop_all()


def _cats(linhas):
    return sorted(l.status_cat for l in linhas)


def test_sem_filtro_retorna_todas_as_certidoes(app, carteira):
    with app.app_context():
        linhas = cf.filtrar()
        assert len(linhas) == 5  # 3 da Alfa + 2 da Beta


def test_cinco_categorias_de_status(app, carteira):
    with app.app_context():
        cats = _cats(cf.filtrar())
        assert cats == ['a_vencer', 'nao_definida', 'pendentes', 'validas', 'vencidas']


def test_status_nao_definida_mapeado_do_sem_data(app, carteira):
    # A trabalhista sem validade deve casar o chip 'nao_definida' (nao 'sem_data').
    with app.app_context():
        linhas = cf.filtrar(status=['nao_definida'])
        assert len(linhas) == 1
        assert linhas[0].certidao.tipo == TipoCertidao.TRABALHISTA


def test_filtro_status_vencidas(app, carteira):
    with app.app_context():
        linhas = cf.filtrar(status=['vencidas'])
        assert [l.certidao.tipo for l in linhas] == [TipoCertidao.FGTS]


def test_filtro_tipo_casa_a_certidao(app, carteira):
    with app.app_context():
        linhas = cf.filtrar(tipo=['federal'])
        assert len(linhas) == 1
        assert linhas[0].certidao.tipo == TipoCertidao.FEDERAL


def test_estado_filtra_a_empresa_inteira(app, carteira):
    # SC seleciona so a Beta -> as 2 certidoes dela, independente do tipo/status.
    with app.app_context():
        linhas = cf.filtrar(estado=['SC'])
        assert len(linhas) == 2
        assert {l.empresa.nome for l in linhas} == {'Beta'}


def test_cidade_usa_chave_normalizada(app, carteira):
    # 'IMBE' (sem acento/caixa alta) deve casar a empresa cadastrada como 'Imbé'.
    with app.app_context():
        linhas = cf.filtrar(cidade=['IMBE'])
        assert len(linhas) == 2
        assert {l.empresa.nome for l in linhas} == {'Beta'}


def test_combinacao_status_e_cidade(app, carteira):
    # status=vencidas + cidade=Tramandai -> so a FGTS da Alfa.
    with app.app_context():
        linhas = cf.filtrar(status=['vencidas'], cidade=['tramandai'])
        assert len(linhas) == 1
        assert linhas[0].certidao.tipo == TipoCertidao.FGTS
        # a mesma status=vencidas noutra cidade nao retorna nada
        assert cf.filtrar(status=['vencidas'], cidade=['imbe']) == []


def test_todas_equivale_a_sem_filtro(app, carteira):
    with app.app_context():
        assert len(cf.filtrar(status=['todas'], tipo=['todas'])) == 5


def test_multi_selecao_de_status(app, carteira):
    with app.app_context():
        linhas = cf.filtrar(status=['vencidas', 'pendentes'])
        assert _cats(linhas) == ['pendentes', 'vencidas']
