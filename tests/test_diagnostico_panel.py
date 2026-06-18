"""Testes da persistencia e das rotas do painel de diagnostico."""
from app import db
from app.services import diagnostics


def test_gravar_evento_e_historico(app, client):
    with app.app_context():
        diagnostics.gravar_evento({
            'event': 'fgts_emit_error', 'level': 'ERROR',
            'error_type': 'PORTAL', 'municipio': 'Imbe',
            'message': 'portal fora', 'request_id': 'r9',
        })
        hist = diagnostics.historico(limite=10)
        assert any(h['error_type'] == 'PORTAL' and h['mensagem'] == 'portal fora' for h in hist)
        # limpa a linha inserida para nao vazar entre testes
        from app.models import EventoDiagnostico
        EventoDiagnostico.query.delete()
        db.session.commit()


def test_rota_eventos_json(client):
    r = client.get('/diagnostico/eventos')
    assert r.status_code == 200
    j = r.get_json()
    assert j['status'] == 'ok'
    assert isinstance(j['eventos'], list)
    assert isinstance(j['alertas'], list)


def test_rota_pagina_diagnostico(client):
    r = client.get('/diagnostico')
    assert r.status_code == 200
    assert 'Diagn'.encode() in r.data
