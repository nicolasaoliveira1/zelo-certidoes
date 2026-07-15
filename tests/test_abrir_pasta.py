"""Testes da rota que abre a pasta CERTIDOES da empresa no Explorer local."""
import os

from app import file_manager


def test_abrir_pasta_sem_login_401(client_anon, ids):
    # fetch (XHR) sem login -> 401 JSON
    r = client_anon.post(f'/empresa/{ids["empresa"]}/abrir-pasta',
                         headers={'X-Requested-With': 'XMLHttpRequest'})
    assert r.status_code == 401


def test_abrir_pasta_nao_encontrada_404(client, ids, monkeypatch):
    monkeypatch.setattr(file_manager, 'encontrar_pasta_empresa', lambda nome: None)
    r = client.post(f'/empresa/{ids["empresa"]}/abrir-pasta')
    assert r.status_code == 404
    assert r.get_json().get('error_type') == 'network_path'


def test_abrir_pasta_sucesso_chama_startfile(client, ids, tmp_path, monkeypatch):
    pasta = str(tmp_path)
    monkeypatch.setattr(file_manager, 'encontrar_pasta_empresa', lambda nome: pasta)
    monkeypatch.setattr(file_manager, 'encontrar_caminho_final', lambda p: pasta)
    chamado = {}
    monkeypatch.setattr(os, 'startfile', lambda p: chamado.setdefault('p', p), raising=False)
    r = client.post(f'/empresa/{ids["empresa"]}/abrir-pasta')
    assert r.status_code == 200
    assert chamado.get('p') == pasta
