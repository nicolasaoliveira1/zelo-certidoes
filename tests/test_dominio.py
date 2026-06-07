"""Testes de regras de domínio: CNPJ, classe de status e validade padrão."""
from datetime import date, timedelta

from app.automation.emissao import (
    _classe_status_por_data,
    _formatar_cnpj,
    _normalizar_cnpj,
    calcular_validade_padrao,
)
from app.models import Certidao, TipoCertidao


def test_normalizar_cnpj():
    assert _normalizar_cnpj('12.345.678/0001-99') == '12345678000199'
    assert _normalizar_cnpj('') == ''
    assert _normalizar_cnpj(None) == ''


def test_formatar_cnpj():
    assert _formatar_cnpj('12345678000199') == '12.345.678/0001-99'
    assert _formatar_cnpj('123') is None  # tamanho invalido


def test_classe_status_sem_data():
    assert _classe_status_por_data(None) == 'status-cinza'


def test_classe_status_cores(app):
    # sem ConfiguracaoSistema o limite 'a vencer' cai no default (7 dias)
    with app.app_context():
        assert _classe_status_por_data(date.today() + timedelta(days=365)) == 'status-verde'
        assert _classe_status_por_data(date.today() - timedelta(days=1)) == 'status-vermelho'
        assert _classe_status_por_data(date.today() + timedelta(days=3)) == 'status-amarelo'


def test_calcular_validade_data_encontrada_tem_prioridade(app, ids):
    alvo = date(2030, 1, 1)
    with app.app_context():
        cert = Certidao.query.filter_by(tipo=TipoCertidao.TRABALHISTA).first()
        assert calcular_validade_padrao(cert, alvo) == alvo


def test_calcular_validade_trabalhista_180(app, ids):
    with app.app_context():
        cert = Certidao.query.filter_by(tipo=TipoCertidao.TRABALHISTA).first()
        assert calcular_validade_padrao(cert, None) == date.today() + timedelta(days=180)


def test_calcular_validade_municipal_none(app, ids):
    with app.app_context():
        cert = Certidao.query.filter_by(tipo=TipoCertidao.MUNICIPAL).first()
        assert calcular_validade_padrao(cert, None) is None


def test_calcular_validade_estadual_rs_59(app, ids):
    with app.app_context():
        cert = Certidao.query.filter_by(tipo=TipoCertidao.ESTADUAL).first()
        # empresa semeada e do RS -> 59 dias
        assert calcular_validade_padrao(cert, None) == date.today() + timedelta(days=59)
