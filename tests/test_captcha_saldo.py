"""Testes de captcha_solver.consultar_saldo (spec 02, SCHED-08).

Best-effort: retorna float em sucesso, None sem chave ou em erro; nunca levanta.
"""
from unittest.mock import MagicMock, patch

from app import captcha_solver


def test_consultar_saldo_sem_chave_retorna_none():
    assert captcha_solver.consultar_saldo({'CAPTCHA_2_API_KEY': ''}) is None
    assert captcha_solver.consultar_saldo({}) is None


def test_consultar_saldo_sucesso_retorna_float():
    fake = MagicMock()
    fake.balance.return_value = 12.5
    with patch.object(captcha_solver, 'TwoCaptcha', return_value=fake):
        saldo = captcha_solver.consultar_saldo({'CAPTCHA_2_API_KEY': 'k'})
    assert saldo == 12.5
    assert isinstance(saldo, float)


def test_consultar_saldo_baixo_retorna_valor():
    fake = MagicMock()
    fake.balance.return_value = 0.3
    with patch.object(captcha_solver, 'TwoCaptcha', return_value=fake):
        saldo = captcha_solver.consultar_saldo({'CAPTCHA_2_API_KEY': 'k'})
    assert saldo == 0.3


def test_consultar_saldo_erro_retorna_none_sem_levantar():
    fake = MagicMock()
    fake.balance.side_effect = RuntimeError('rede fora')
    with patch.object(captcha_solver, 'TwoCaptcha', return_value=fake):
        assert captcha_solver.consultar_saldo({'CAPTCHA_2_API_KEY': 'k'}) is None
