"""Testes do detector de portais IPM Atende.Net (is_ipm_atende).

Deriva das ACs de UCM-01: detecta por host (atende.net / *.atende.net),
nunca por substring no path/query, e trata entradas invalidas como False.
"""
from app.automation.sites import is_ipm_atende


def test_urls_ipm_reais_retornam_true():
    # URLs reais semeadas em migrations (Gravatai/Osorio/Novo Hamburgo)
    assert is_ipm_atende(
        'https://gravatai.atende.net/autoatendimento/servicos/embed/data/xyz'
        '/servicos/certidao-negativa-de-debitos/detalhar/1'
    ) is True
    assert is_ipm_atende(
        'https://osorio.atende.net/autoatendimento/servicos/embed/data/abc'
    ) is True
    assert is_ipm_atende(
        'https://novohamburgo.atende.net/autoatendimento/servicos/embed/data/def'
    ) is True


def test_urls_nao_ipm_retornam_false():
    assert is_ipm_atende(
        'https://siat.procempa.com.br/siat/ArrSolicitarCertidao.do'
    ) is False
    assert is_ipm_atende(
        'https://grp.imbe.rs.gov.br/grp/acessoexterno/programaAcessoExterno.faces'
    ) is False
    assert is_ipm_atende(
        'https://e-gov.betha.com.br/cdweb/contribuinte/rel_cndcontribuinte.faces'
    ) is False
    assert is_ipm_atende(
        'https://cidreira.multi24h.com.br/multi24/sistemas/portal/'
    ) is False


def test_atende_net_apenas_no_path_ou_query_retorna_false():
    assert is_ipm_atende('https://exemplo.com/?redirect=atende.net') is False
    assert is_ipm_atende('https://exemplo.com/atende.net/certidao') is False


def test_dominio_que_apenas_contem_atende_net_nao_e_falso_positivo():
    # host atende.net.evil.com NAO deve ser tratado como IPM
    assert is_ipm_atende('https://atende.net.evil.com/x') is False


def test_entradas_invalidas_retornam_false_sem_excecao():
    assert is_ipm_atende(None) is False
    assert is_ipm_atende('') is False
    assert is_ipm_atende('nao e uma url') is False
    assert is_ipm_atende(12345) is False


def test_host_exato_atende_net_retorna_true():
    assert is_ipm_atende('https://atende.net/qualquer') is True
