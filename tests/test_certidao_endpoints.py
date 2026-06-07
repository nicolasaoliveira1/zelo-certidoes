"""Caracterização dos endpoints de validade/pendência de certidão.

Trava o contrato de /certidao/atualizar(_json), /salvar_data_confirmada e
/marcar_pendente(_json), incluindo as classes de status (M2). Sem Selenium.
Usa as fixtures de conftest.py.
"""
from datetime import date, timedelta

from app.models import Certidao, StatusEspecial


def _fmt(d):
    return d.isoformat()


def test_atualizar_json_cores(client, cid):
    casos = [
        (date.today() + timedelta(days=365), 'status-verde'),
        (date.today() - timedelta(days=10), 'status-vermelho'),
        (date.today() + timedelta(days=3), 'status-amarelo'),
    ]
    for d, esperado in casos:
        r = client.post(f'/certidao/atualizar_json/{cid}', json={'nova_validade': _fmt(d)})
        assert r.status_code == 200, (esperado, r.status_code)
        j = r.get_json()
        assert j['status'] == 'success', j
        assert j['nova_classe'] == esperado, (d, j['nova_classe'], esperado)
        assert j['nova_data_formatada'] == d.strftime('%d/%m/%Y')


def test_atualizar_json_sem_data(client, cid):
    r = client.post(f'/certidao/atualizar_json/{cid}', json={})
    assert r.status_code == 400, r.status_code
    assert r.get_json()['status'] == 'error'


def test_salvar_data_confirmada(client, cid):
    d = date.today() + timedelta(days=365)
    r = client.post('/certidao/salvar_data_confirmada',
                    json={'certidao_id': cid, 'nova_validade': _fmt(d)})
    assert r.status_code == 200, r.status_code
    j = r.get_json()
    assert j['status'] == 'success'
    assert j['nova_classe'] == 'status-verde', j['nova_classe']
    assert j['message'] == 'Data confirmada e atualizada com sucesso!'


def test_marcar_pendente_json(app, client, cid):
    r = client.post(f'/certidao/marcar_pendente_json/{cid}', json={})
    assert r.status_code == 200, r.status_code
    assert r.get_json()['status'] == 'success'
    with app.app_context():
        c = Certidao.query.get(cid)
        assert c.status_especial == StatusEspecial.PENDENTE
        assert c.data_validade is None


def test_form_endpoints_redirect(client, cid):
    r = client.post(f'/certidao/atualizar/{cid}',
                    data={'nova_validade': _fmt(date.today() + timedelta(days=30))})
    assert r.status_code == 302, ('atualizar form', r.status_code)
    r = client.post(f'/certidao/marcar_pendente/{cid}', data={})
    assert r.status_code == 302, ('pendente form', r.status_code)
