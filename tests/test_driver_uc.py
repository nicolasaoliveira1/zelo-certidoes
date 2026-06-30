"""Testes do driver undetected-chromedriver e do lock de perfil municipal.

Cobre as ACs de UCM-05 (fail-fast quando uc indisponivel) e UCM-06
(serializacao do perfil municipal: acquire/release/ocupado/idempotencia)
sem abrir navegador real.
"""
import sys

import pytest

from app.automation import driver


def test_criar_driver_uc_sem_pacote_levanta_uc_indisponivel(monkeypatch):
    # Forca ImportError em 'import undetected_chromedriver'
    monkeypatch.setitem(sys.modules, 'undetected_chromedriver', None)
    with pytest.raises(driver.UcIndisponivelError) as exc_info:
        driver._criar_driver_uc()
    # mensagem distingue 'nao instalado' (e nao mascara como falha de inicio)
    assert 'instale' in exc_info.value.message.lower()
    assert exc_info.value.acao


def test_detectar_version_main_usa_env_override(monkeypatch):
    monkeypatch.setenv('CHROME_UC_VERSION_MAIN', '149')
    assert driver._detectar_chrome_version_main() == 149


def test_detectar_version_main_ignora_env_nao_numerico(monkeypatch):
    monkeypatch.setenv('CHROME_UC_VERSION_MAIN', 'abc')
    # nao numerico -> nao usa o override (cai para registro/None)
    resultado = driver._detectar_chrome_version_main()
    assert resultado != 'abc'
    assert resultado is None or isinstance(resultado, int)


def test_lock_municipal_acquire_bloqueia_segundo():
    assert driver._municipal_profile_acquire(blocking=False) is True
    try:
        # segundo acquire (perfil em uso) falha rapido
        assert driver._municipal_profile_acquire(blocking=False) is False
    finally:
        driver._municipal_profile_release()


def test_lock_municipal_release_permite_readquirir():
    assert driver._municipal_profile_acquire(blocking=False) is True
    driver._municipal_profile_release()
    assert driver._municipal_profile_acquire(blocking=False) is True
    driver._municipal_profile_release()


def test_lock_municipal_release_idempotente():
    # release sem acquire previo nao lanca e mantem o lock utilizavel
    driver._municipal_profile_release()
    assert driver._municipal_profile_acquire(blocking=False) is True
    driver._municipal_profile_release()


def test_municipal_profile_settings_usa_env_e_nome_dedicado(monkeypatch, tmp_path):
    alvo = str(tmp_path / 'mun')
    monkeypatch.setenv('CHROME_PROFILE_MUNICIPAL_DIR', alvo)
    d, n = driver._get_municipal_profile_settings()
    assert d == alvo
    # nome dedicado, isolado do perfil 'Certidoes' usado por RS/Federal
    assert n == 'Municipal'
