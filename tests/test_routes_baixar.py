"""Caracterizacao do contrato de /certidao/baixar (A1).

A camada Selenium (_executar_automacao_baixar) e substituida por um mock que
devolve um 'resultado' controlado, entao nenhum navegador e aberto. Cobre:
- a rota (404, sucesso, erro 500, janela fechada)
- as fronteiras puras _validar_baixar / _montar_config_baixar / _montar_resposta_baixar
"""
from datetime import date, timedelta

from app import routes
from app.models import Certidao, TipoCertidao


def _mock_automacao(monkeypatch, resultado):
    monkeypatch.setattr(routes, '_executar_automacao_baixar', lambda certidao, cfg: resultado)
    # evita criar o arquivo-chave de interrupcao no diretorio do app durante o teste
    monkeypatch.setattr(routes.file_manager, 'criar_chave_interrupcao', lambda: None)


# --------------------------- rota (contrato HTTP) ---------------------------

def test_baixar_inexistente_404(client):
    r = client.get('/certidao/baixar/999999')
    assert r.status_code == 404


def test_baixar_sucesso(client, ids, monkeypatch):
    resultado = routes._resultado_baixar_vazio()
    resultado['arquivo_salvo_msg'] = 'Arquivo salvo em: C:/x/cert.pdf'
    _mock_automacao(monkeypatch, resultado)

    r = client.get(f"/certidao/baixar/{ids['fgts']}")
    assert r.status_code == 200
    j = r.get_json()
    assert j['status'] in ('success_file_saved', 'success_file_saved_no_date')
    assert j['certidao_id'] == ids['fgts']
    assert j['mensagem_arquivo'].startswith('Arquivo salvo em:')
    assert 'visualizar_token' in j


def test_baixar_erro_500(client, ids, monkeypatch):
    resultado = routes._resultado_baixar_vazio()
    resultado['erro_500'] = 'Ocorreu um erro na automação.'
    _mock_automacao(monkeypatch, resultado)

    r = client.get(f"/certidao/baixar/{ids['fgts']}")
    assert r.status_code == 500
    assert r.get_json()['status'] == 'error'


def test_baixar_janela_fechada(client, ids, monkeypatch):
    resultado = routes._resultado_baixar_vazio()
    resultado['window_closed'] = True
    _mock_automacao(monkeypatch, resultado)

    r = client.get(f"/certidao/baixar/{ids['fgts']}")
    assert r.status_code == 200
    j = r.get_json()
    assert j['status'] == 'window_closed_no_file'
    assert j['certidao_id'] == ids['fgts']


# --------------------------- _validar_baixar ---------------------------

def test_validar_baixar_federal_redireciona(app, ids):
    with app.app_context():
        cert = Certidao.query.filter_by(tipo=TipoCertidao.FEDERAL).first()
        resp = routes._validar_baixar(cert)
        assert resp is not None
        assert resp.status_code == 302  # redirect para a Receita


def test_validar_baixar_rs_lote_ativo(app, ids):
    original = routes.RS_BATCH_STATE.get('status')
    with app.app_context():
        cert = Certidao.query.filter_by(tipo=TipoCertidao.ESTADUAL).first()  # empresa semeada e RS
        routes.RS_BATCH_STATE['status'] = 'running'
        try:
            resp = routes._validar_baixar(cert)
        finally:
            routes.RS_BATCH_STATE['status'] = original
    assert resp is not None
    _body, code = resp  # _json_error -> (response, code)
    assert code == 400


def test_validar_baixar_segue_quando_ok(app, ids):
    with app.app_context():
        cert = Certidao.query.filter_by(tipo=TipoCertidao.FGTS).first()
        assert routes._validar_baixar(cert) is None


# --------------------------- _montar_config_baixar ---------------------------

def test_montar_config_fgts(app, ids):
    with app.test_request_context(f"/certidao/baixar/{ids['fgts']}"):
        cert = Certidao.query.get(ids['fgts'])
        cfg, erro = routes._montar_config_baixar(cert)
        assert erro is None
        assert cfg['tipo_certidao_chave'] == 'FGTS'
        assert len(cfg['cnpj_limpo']) == 14
        assert cfg['nome_certidao_arquivo'] == 'FGTS'


def test_montar_config_municipal_sem_regra(app, ids):
    # empresa semeada e de Tramandai, mas nao ha Municipio cadastrado -> 404
    with app.test_request_context(f"/certidao/baixar/{ids['municipal']}"):
        cert = Certidao.query.get(ids['municipal'])
        cfg, erro = routes._montar_config_baixar(cert)
        assert cfg is None
        _body, code = erro
        assert code == 404


# --------------------------- _montar_resposta_baixar ---------------------------

def _cfg_simples(certidao):
    return {
        'tipo_certidao_chave': certidao.tipo.name,
        'estado_emp': 'RS',
        'imbe_tipo': '',
        'info_site': {},
        'regra_municipio': None,
        'config_municipal': None,
        'usar_config_municipal': False,
        'cnpj_limpo': '11111111111111',
        'inscricao_limpa': '',
        'nome_certidao_arquivo': certidao.tipo.value,
        'usar_rs_autoselect': False,
    }


def test_resposta_erro_500(app, ids):
    with app.test_request_context('/'):
        cert = Certidao.query.get(ids['fgts'])
        resultado = routes._resultado_baixar_vazio()
        resultado['erro_500'] = 'falhou'
        resp = routes._montar_resposta_baixar(cert, _cfg_simples(cert), resultado)
        _body, code = resp
        assert code == 500


def test_resposta_window_closed(app, ids):
    with app.test_request_context('/'):
        cert = Certidao.query.get(ids['fgts'])
        resultado = routes._resultado_baixar_vazio()
        resultado['window_closed'] = True
        resp = routes._montar_resposta_baixar(cert, _cfg_simples(cert), resultado)
        assert resp.get_json()['status'] == 'window_closed_no_file'


def test_resposta_sucesso_com_data_encontrada(app, ids):
    alvo = date.today() + timedelta(days=120)
    with app.test_request_context('/'):
        cert = Certidao.query.get(ids['fgts'])
        resultado = routes._resultado_baixar_vazio()
        resultado['arquivo_salvo_msg'] = 'Arquivo salvo em: X'
        resultado['data_encontrada'] = alvo
        resp = routes._montar_resposta_baixar(cert, _cfg_simples(cert), resultado)
        j = resp.get_json()
        assert j['status'] == 'success_file_saved'
        assert j['nova_data'] == alvo.strftime('%Y-%m-%d')
        assert j['data_formatada'] == alvo.strftime('%d/%m/%Y')


def test_resposta_rs_positiva(app, ids):
    with app.test_request_context('/'):
        cert = Certidao.query.get(ids['rs'])
        resultado = routes._resultado_baixar_vazio()
        resultado['rs_estadual_classificacao'] = 'positiva'
        resultado['rs_estadual_msg'] = 'POSITIVA detectada'
        resp = routes._montar_resposta_baixar(cert, _cfg_simples(cert), resultado)
        j = resp.get_json()
        assert j['status'] == 'estadual_rs_positiva'
        assert j['message'] == 'POSITIVA detectada'
