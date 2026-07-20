"""Testes da pagina/rota de produtividade (spec 04, EXPORT-05).

Autorizacao (leitura), render das metricas, periodo sem lotes com zeros, export
XLSX e clamp do preset de dias.
"""
from datetime import timedelta

from app import db
from app.models import ExecucaoLote
from app.utils import utcnow_naive


def _semear_lote(tipo='FGTS', sucesso=5, falhas=1, dias_atras=1, duracao_min=10):
    iniciado = utcnow_naive() - timedelta(days=dias_atras)
    db.session.add(ExecucaoLote(
        tipo=tipo, iniciado_em=iniciado, sucesso=sucesso, falhas=falhas,
        finalizado_em=iniciado + timedelta(minutes=duracao_min)))
    db.session.commit()


def test_pagina_leitura_ok(login_as):
    resp = login_as('leitura').get('/produtividade')
    assert resp.status_code == 200
    assert 'Produtividade' in resp.get_data(as_text=True)


def test_pagina_anon_negado(client_anon):
    assert client_anon.get('/produtividade').status_code == 302


def test_periodo_sem_lotes_mostra_zeros(login_as):
    html = login_as('leitura').get('/produtividade').get_data(as_text=True)
    assert 'Nenhum lote no período.' in html


def test_pagina_com_lotes_mostra_numeros(login_as, app):
    with app.app_context():
        _semear_lote(tipo='FGTS', sucesso=5, falhas=1)
    html = login_as('leitura').get('/produtividade').get_data(as_text=True)
    # '83.3%' so aparece na tabela por tipo (5/(5+1)); a nota informativa nao tem %
    assert '83.3%' in html


def test_export_baixa_xlsx(login_as):
    resp = login_as('leitura').get('/produtividade/exportar.xlsx')
    assert resp.status_code == 200
    assert 'spreadsheetml' in resp.headers['Content-Type']
    assert 'produtividade-' in resp.headers['Content-Disposition']


def test_export_anon_negado(client_anon):
    assert client_anon.get('/produtividade/exportar.xlsx').status_code == 302


def test_preset_invalido_cai_para_30(login_as):
    html = login_as('leitura').get('/produtividade?dias=999').get_data(as_text=True)
    assert 'últimos 30 dias' in html
