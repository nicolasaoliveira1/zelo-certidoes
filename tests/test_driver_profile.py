"""Testes da parametrizacao de perfil do driver (UCM-04).

Cobre override de pasta/nome de perfil e o caminho municipal (perfil
persistente, sem incognito) sem precisar de app context (args explicitos).
"""
import os

from app.automation import driver


def test_get_profile_settings_aceita_override(tmp_path):
    alvo = str(tmp_path / 'perfil-municipal')
    d, n = driver._get_chrome_profile_settings(profile_dir=alvo, profile_name='Municipal')
    assert d == alvo
    assert n == 'Municipal'
    assert os.path.isdir(d)  # makedirs criou a pasta


def test_build_options_perfil_municipal_sem_incognito(tmp_path):
    alvo = str(tmp_path / 'perfil-municipal')
    opts = driver._build_chrome_options(
        anonimo=False, usar_perfil=True, profile_dir=alvo, profile_name='Municipal'
    )
    args = opts.arguments
    assert f'--user-data-dir={alvo}' in args
    assert '--profile-directory=Municipal' in args
    assert '--incognito' not in args


def test_build_options_default_preserva_incognito_e_sem_perfil():
    opts = driver._build_chrome_options(anonimo=True, usar_perfil=False)
    args = opts.arguments
    assert '--incognito' in args
    assert not any(a.startswith('--user-data-dir=') for a in args)
