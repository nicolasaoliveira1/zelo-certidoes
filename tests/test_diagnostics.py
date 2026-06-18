"""Testes do diagnostico em memoria (buffer + recorrencia)."""
from app.services import diagnostics
from app.services.execution_logger import log_event


def _erro(municipio='Imbe'):
    return {
        'level': 'ERROR', 'event': 'municipal_batch_emit_error',
        'error_type': 'PORTAL', 'municipio': municipio,
        'timestamp': '2026-06-18T14:00:00+00:00', 'request_id': 'r1',
    }


def test_recorrencia_abre_alerta_no_limiar():
    diagnostics.limpar()
    for _ in range(diagnostics._LIMIAR_RECORRENCIA):
        diagnostics.registrar(_erro())
    alertas = diagnostics.alertas_ativos()
    assert len(alertas) == 1
    assert alertas[0]['error_type'] == 'PORTAL'
    assert alertas[0]['alvo'] == 'Imbe'
    assert alertas[0]['ocorrencias'] >= diagnostics._LIMIAR_RECORRENCIA
    assert alertas[0]['hipotese']


def test_sucesso_zera_recorrencia_do_alvo():
    diagnostics.limpar()
    for _ in range(diagnostics._LIMIAR_RECORRENCIA):
        diagnostics.registrar(_erro())
    diagnostics.registrar({'level': 'INFO', 'event': 'municipal_ok', 'municipio': 'Imbe'})
    assert diagnostics.alertas_ativos() == []


def test_handler_alimenta_buffer_via_log_event(app):
    diagnostics.limpar()
    with app.app_context():
        log_event('fgts_emit_success', empresa_id=1, certidao_id=2)
    eventos = diagnostics.eventos_recentes(limite=5)
    assert any(e.get('event') == 'fgts_emit_success' for e in eventos)
