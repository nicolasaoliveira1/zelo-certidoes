"""Testes da verificacao de dependencias criticas no boot."""
from app.services import deps_check


def test_sem_faltantes_quando_tudo_importa():
    # 'os' sempre importa -> nenhuma faltante
    assert deps_check.dependencias_faltantes([('os', 'os')]) == []


def test_lista_pacote_quando_modulo_ausente():
    faltando = deps_check.dependencias_faltantes(
        [('modulo_inexistente_xyz', 'pacote-x')]
    )
    assert faltando == ['pacote-x']


def test_preserva_ordem_e_so_reporta_ausentes():
    faltando = deps_check.dependencias_faltantes([
        ('os', 'os'),
        ('modulo_inexistente_a', 'pacote-a'),
        ('sys', 'sys'),
        ('modulo_inexistente_b', 'pacote-b'),
    ])
    assert faltando == ['pacote-a', 'pacote-b']


def test_default_inclui_undetected_chromedriver():
    # a lista default cobre as deps criticas da automacao IPM
    pacotes = [pacote for _mod, pacote in deps_check.CRITICAS]
    assert 'undetected-chromedriver' in pacotes
