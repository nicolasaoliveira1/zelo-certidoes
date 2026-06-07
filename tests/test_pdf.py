"""Testes da classificação de conteúdo de certidão (pdf.classificar_texto).

Lógica pura sobre texto — não depende de PDF, Selenium nem banco.
"""
from app.automation import pdf


def test_positiva():
    assert pdf.classificar_texto('Trata-se de CERTIDÃO POSITIVA de débitos.') == 'positiva'


def test_negativa():
    assert pdf.classificar_texto('CERTIDÃO NEGATIVA de débitos relativos a tributos') == 'negativa'


def test_efeito_de_negativa_tem_prioridade():
    # "positiva com efeitos de negativa" deve ser efeito_negativa, nao positiva
    assert pdf.classificar_texto(
        'Certidão Positiva com Efeitos de Negativa'
    ) == 'efeito_negativa'
    assert pdf.classificar_texto('CERTIDAO POSITIVA COM EFEITO DE NEGATIVA') == 'efeito_negativa'


def test_desconhecida():
    assert pdf.classificar_texto('documento sem o termo esperado') == 'desconhecida'
    assert pdf.classificar_texto('') == 'desconhecida'
    assert pdf.classificar_texto(None) == 'desconhecida'


def test_acentos_e_espacos_normalizados():
    # acentos removidos e espacos colapsados antes da classificacao
    assert pdf.classificar_texto('certidão    negativa') == 'negativa'
