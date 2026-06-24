"""Testes do HumanFormatter (saida legivel do console)."""
import logging
from datetime import datetime

from app.services.execution_logger import HumanFormatter


def _registro(payload):
    rec = logging.LogRecord('certidoes', logging.INFO, __file__, 0, 'msg', None, None)
    rec.payload = payload
    return rec


def _hora_local_esperada(ts_utc):
    # o payload guarda UTC; o console deve exibir hora local (tz-agnostico no teste)
    return datetime.fromisoformat(ts_utc).astimezone().strftime('%H:%M:%S')


def test_human_formatter_renderiza_campos_chave():
    ts = '2026-06-18T14:23:09+00:00'
    out = HumanFormatter(usar_cor=False).format(_registro({
        'timestamp': ts,
        'event': 'municipal_batch_emit_error',
        'level': 'ERROR',
        'request_id': 'a1b2c3',
        'error_type': 'PORTAL',
        'certidao_id': 7,
    }))
    assert out.startswith(_hora_local_esperada(ts))  # UTC convertido para hora local
    assert 'MUNI' in out           # dominio derivado do prefixo do evento
    assert 'PORTAL' in out         # error_type aparece
    assert 'certidao=7' in out
    assert '(req:a1b2c3)' in out
    assert '\033[' not in out      # sem cor quando usar_cor=False


def test_human_formatter_colore_por_nivel():
    out = HumanFormatter(usar_cor=True).format(_registro({
        'timestamp': '2026-06-18T14:23:09+00:00',
        'event': 'fgts_emit_success',
        'level': 'INFO',
    }))
    assert out.startswith('\033[90m') and out.endswith('\033[0m')


def test_human_formatter_sem_payload_cai_para_mensagem():
    rec = logging.LogRecord('certidoes', logging.INFO, __file__, 0, 'texto cru', None, None)
    assert HumanFormatter(usar_cor=False).format(rec) == 'texto cru'
