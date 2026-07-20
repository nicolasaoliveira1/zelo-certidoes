"""Testes de `utils.normalizar_cidade` (spec 04, EXPORT-02).

A chave de cidade e a fonte unica compartilhada pelo filtro do dashboard e pela
exportacao da carteira. Variacoes de acento/caixa DEVEM colapsar na mesma chave
para o recorte do export bater 1:1 com o painel.
"""
from app.utils import normalizar_cidade


def test_acento_e_caixa_colapsam_na_mesma_chave():
    # 'Imbe'/'IMBE'/'imbé' representam a mesma cidade no painel -> mesma chave.
    chave = normalizar_cidade('Imbé')
    assert chave == 'IMBE'
    assert normalizar_cidade('IMBE') == chave
    assert normalizar_cidade('imbé') == chave
    assert normalizar_cidade('ImBe') == chave


def test_vazio_e_none_viram_string_vazia():
    assert normalizar_cidade('') == ''
    assert normalizar_cidade(None) == ''
    assert normalizar_cidade('   ') == ''


def test_espacos_das_pontas_sao_removidos():
    assert normalizar_cidade('  Porto Alegre  ') == 'PORTO ALEGRE'


def test_paridade_com_o_alias_do_dashboard():
    # O alias do dashboard deve delegar exatamente a esta funcao (mesma chave).
    from app.routes import _normalizar_cidade_dashboard
    for valor in ('Tramandaí', 'SANTO ANTÔNIO', '', None, '  Osório '):
        assert _normalizar_cidade_dashboard(valor) == normalizar_cidade(valor)
