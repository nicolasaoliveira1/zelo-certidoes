"""Testes do executor de steps municipais (steps.executar_municipio).

Demonstra que dá para exercitar a lógica de fluxo Selenium com um driver/wait
FALSOS (unittest.mock), sem navegador real — exatamente o tipo de teste viável
para os fluxos de automação.
"""
from unittest.mock import MagicMock

from app.automation import steps


def test_sem_steps_retorna_none():
    assert steps.executar_municipio(MagicMock(), MagicMock(), [], '', '') is None


def test_fill_usa_cnpj_e_click():
    driver = MagicMock()
    wait = MagicMock()
    elemento = MagicMock()
    wait.until.return_value = elemento

    steps_def = [
        {'tipo': 'fill', 'by': 'id', 'locator': 'campoCnpj', 'value': 'cnpj', 'sleep': 0},
        {'tipo': 'click', 'by': 'id', 'locator': 'btnEmitir', 'sleep': 0},
    ]
    resultado = steps.executar_municipio(
        driver, wait, steps_def, '12345678000199', '', 'after_cnpj'
    )
    assert resultado is None                       # nao encerrou sem arquivo
    elemento.send_keys.assert_any_call('12345678000199')  # preencheu o CNPJ
    assert elemento.click.called                   # clicou no botao


def test_fill_usa_inscricao():
    driver = MagicMock()
    wait = MagicMock()
    elemento = MagicMock()
    wait.until.return_value = elemento

    steps_def = [{'tipo': 'fill', 'by': 'id', 'locator': 'insc', 'value': 'inscricao', 'sleep': 0}]
    steps.executar_municipio(driver, wait, steps_def, '12345678000199', '000123', 'after_cnpj')
    elemento.send_keys.assert_any_call('000123')


def test_by_invalido_ignora_step():
    driver = MagicMock()
    wait = MagicMock()
    steps_def = [{'tipo': 'click', 'by': 'xpto_invalido', 'locator': 'x', 'sleep': 0}]
    # by desconhecido -> step ignorado, sem chamar wait.until
    steps.executar_municipio(driver, wait, steps_def, '', '')
    assert not wait.until.called
