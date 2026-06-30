"""Testes da selecao de driver e do surface acionavel na emissao individual.

Cobre as ACs UCM-02 (municipal IPM -> uc), UCM-03 (nao-IPM/outros tipos ->
driver atual), UCM-05 (uc indisponivel -> fail-fast acionavel) e UCM-06
(perfil ocupado -> fail-fast + release do lock). Nenhum navegador e aberto.
"""
import types

from app import routes
from app.automation.driver import UcIndisponivelError
from app.models import Certidao


def _cfg(url, tipo='MUNICIPAL', usar_rs_autoselect=False):
    return {
        'tipo_certidao_chave': tipo,
        'info_site': {'url': url},
        'usar_rs_autoselect': usar_rs_autoselect,
    }


def _cert():
    return types.SimpleNamespace(id=1, empresa_id=1)


def _patch_factories(monkeypatch, called, uc_exc=None, acquire=True):
    def fake_uc(*a, **k):
        called.append('uc')
        if uc_exc:
            raise uc_exc
        return 'DRV_UC'

    def fake_chrome(*a, **k):
        called.append('chrome')
        return 'DRV_CHROME'

    monkeypatch.setattr(routes, '_criar_driver_uc', fake_uc)
    monkeypatch.setattr(routes, '_criar_driver_chrome', fake_chrome)
    monkeypatch.setattr(routes, '_municipal_profile_acquire', lambda blocking=False: acquire)
    liberou = []
    monkeypatch.setattr(routes, '_municipal_profile_release', lambda: liberou.append(True))
    return liberou


# ---------------------------- UCM-02 / UCM-03 ----------------------------

def test_municipal_ipm_usa_driver_uc(monkeypatch):
    called = []
    _patch_factories(monkeypatch, called)
    resultado = routes._resultado_baixar_vazio()
    drv, lock = routes._abrir_driver_baixar(_cfg('https://gravatai.atende.net/x'), _cert(), resultado)
    assert drv == 'DRV_UC'
    assert lock is True
    assert called == ['uc']  # uc chamado, chrome nao
    assert resultado['erro_acionavel'] is None


def test_municipal_nao_ipm_usa_driver_chrome(monkeypatch):
    called = []
    _patch_factories(monkeypatch, called)
    resultado = routes._resultado_baixar_vazio()
    drv, lock = routes._abrir_driver_baixar(
        _cfg('https://grp.imbe.rs.gov.br/grp/acessoexterno/x'), _cert(), resultado
    )
    assert drv == 'DRV_CHROME'
    assert lock is False
    assert called == ['chrome']  # uc nao chamado


def test_tipo_nao_municipal_usa_driver_chrome(monkeypatch):
    called = []
    _patch_factories(monkeypatch, called)
    resultado = routes._resultado_baixar_vazio()
    # Mesmo com URL atende.net, tipo FGTS nao roteia para uc
    drv, lock = routes._abrir_driver_baixar(
        _cfg('https://gravatai.atende.net/x', tipo='FGTS'), _cert(), resultado
    )
    assert drv == 'DRV_CHROME'
    assert lock is False
    assert called == ['chrome']


# ------------------------------- UCM-06 -------------------------------

def test_perfil_ocupado_falha_rapido_sem_abrir_navegador(monkeypatch):
    called = []
    _patch_factories(monkeypatch, called, acquire=False)
    resultado = routes._resultado_baixar_vazio()
    drv, lock = routes._abrir_driver_baixar(_cfg('https://osorio.atende.net/x'), _cert(), resultado)
    assert drv is None
    assert lock is False
    assert called == []  # nenhuma fabrica chamada (sem 2o Chrome no perfil)
    assert resultado['erro_acionavel']['code'] == 409
    assert 'uso' in resultado['erro_acionavel']['message'].lower()


# ------------------------------- UCM-05 -------------------------------

def test_uc_indisponivel_falha_rapido_e_libera_lock(monkeypatch):
    called = []
    exc = UcIndisponivelError('Driver anti-bloqueio nao pode iniciar o Chrome: versao X', acao='Acao Y')
    liberou = _patch_factories(monkeypatch, called, uc_exc=exc)
    resultado = routes._resultado_baixar_vazio()
    drv, lock = routes._abrir_driver_baixar(
        _cfg('https://novohamburgo.atende.net/x'), _cert(), resultado
    )
    assert drv is None
    assert lock is False
    assert called == ['uc']  # tentou uc, NAO caiu para chrome (sem fallback)
    assert liberou == [True]  # lock liberado apos a falha
    assert resultado['erro_acionavel']['code'] == 409
    # a resposta surfaceia a mensagem e acao da propria excecao (nao hardcode)
    assert resultado['erro_acionavel']['message'] == 'Driver anti-bloqueio nao pode iniciar o Chrome: versao X'
    assert resultado['erro_acionavel']['acao'] == 'Acao Y'


# ------------------ surface no _montar_resposta_baixar ------------------

def test_resposta_erro_acionavel_retorna_409(app, ids):
    with app.test_request_context('/'):
        cert = Certidao.query.get(ids['municipal'])
        cfg = {
            'tipo_certidao_chave': 'MUNICIPAL',
            'regra_municipio': None,
            'nome_certidao_arquivo': cert.tipo.value,
        }
        resultado = routes._resultado_baixar_vazio()
        resultado['erro_acionavel'] = {
            'message': 'Perfil municipal em uso.',
            'error_type': 'PORTAL',
            'acao': 'Aguarde e tente novamente.',
            'code': 409,
        }
        body, code = routes._montar_resposta_baixar(cert, cfg, resultado)
        assert code == 409
        data = body.get_json()
        assert data['status'] == 'error'
        assert data['error_type'] == 'PORTAL'
        assert data['acao'] == 'Aguarde e tente novamente.'
        assert data['message'] == 'Perfil municipal em uso.'
