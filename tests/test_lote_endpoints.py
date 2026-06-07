"""Caracterização das rotas de lote (FGTS / Estadual RS / Municipal).

Trava o contrato HTTP (paths, status code, campo `status` e tokens das
mensagens). Exercita apenas caminhos seguros: status, info e erros 400 de
`iniciar` — nunca dispara um worker/Selenium. Usa as fixtures de conftest.py.
"""
PREFIXOS = {'fgts': '/fgts', 'rs': '/estadual-rs', 'municipal': '/municipal'}


def test_status_idle(client):
    for pref in PREFIXOS.values():
        r = client.get(f'{pref}/lote/status')
        assert r.status_code == 200, (pref, r.status_code)
        j = r.get_json()
        assert j['status'] == 'idle', (pref, j['status'])
        assert j['total'] == 0


def test_info(client, ids):
    chaves = {'ids', 'total', 'scope', 'vencidas', 'a_vencer', 'pendentes'}
    for k, pref in PREFIXOS.items():
        r = client.get(f'{pref}/lote/info/{ids[k]}')
        assert r.status_code == 200, (pref, r.status_code)
        j = r.get_json()
        assert j['status'] == 'ok', (pref, j)
        assert chaves <= set(j.keys()), (pref, set(j.keys()))


def test_iniciar_sem_certidao(client):
    for pref in PREFIXOS.values():
        r = client.post(f'{pref}/lote/iniciar', json={})
        assert r.status_code == 400, (pref, r.status_code)
        assert r.get_json()['status'] == 'error', pref


def test_iniciar_vazio_ou_precondicao(client, ids):
    # FGTS/Municipal: certidões sem data -> lote vazio (400, sem worker)
    r = client.post('/fgts/lote/iniciar', json={'certidao_id': ids['fgts']})
    assert r.status_code == 400, r.status_code
    m = r.get_json()['message']
    assert 'FGTS' in m and 'vencer' in m, m

    r = client.post('/municipal/lote/iniciar', json={'certidao_id': ids['municipal']})
    assert r.status_code == 400, r.status_code
    assert 'Municipal' in r.get_json()['message'], r.get_json()['message']

    # Estadual RS: flag desligada -> precondição barra antes do worker
    r = client.post('/estadual-rs/lote/iniciar', json={'certidao_id': ids['rs']})
    assert r.status_code == 400, r.status_code
    assert 'RS_ALTCHA_AUTOSOLVE_ENABLED' in r.get_json()['message'], r.get_json()['message']
