"""Testes unitarios da fila duravel de emissao (spec 02, SCHED-01/02/03).

Cada teste ancora num AC das stories P1 (fila duravel) da spec 02.
"""
from datetime import datetime, timedelta
from unittest.mock import patch

from app import db
from app.models import Certidao, TarefaEmissao, TipoCertidao
from app.services import fila_emissao


def _nova_tarefa(app, ids, **kwargs):
    """Cria e persiste uma TarefaEmissao para a certidao FGTS semeada."""
    cert = db.session.get(Certidao, ids['fgts'])
    dados = dict(tipo='FGTS', empresa_id=cert.empresa_id, certidao_id=cert.id,
                 status='pendente')
    dados.update(kwargs)
    t = TarefaEmissao(**dados)
    db.session.add(t)
    db.session.commit()
    return t


# --- AC P1.1: enfileirar cria pendente ------------------------------------

def test_enfileirar_cria_pendente(app, ids):
    with app.app_context():
        t = fila_emissao.enfileirar(ids['fgts'], TipoCertidao.FGTS)
        assert t is not None
        assert t.status == 'pendente'
        assert t.tipo == 'FGTS'
        assert t.certidao_id == ids['fgts']
        assert t.empresa_id == ids['empresa']
        assert t.tentativas == 0
        assert TarefaEmissao.query.count() == 1


def test_enfileirar_certidao_inexistente_retorna_none(app, ids):
    with app.app_context():
        assert fila_emissao.enfileirar(999999, TipoCertidao.FGTS) is None
        assert TarefaEmissao.query.count() == 0


def test_enfileirar_falha_commit_faz_rollback(app, ids):
    with app.app_context():
        with patch.object(db.session, 'commit', side_effect=Exception('boom')):
            assert fila_emissao.enfileirar(ids['fgts'], TipoCertidao.FGTS) is None


# --- AC P1.5: idempotencia (nao duplica) ----------------------------------

def test_enfileirar_idempotente_nao_duplica(app, ids):
    with app.app_context():
        primeira = fila_emissao.enfileirar(ids['fgts'], TipoCertidao.FGTS)
        segunda = fila_emissao.enfileirar(ids['fgts'], TipoCertidao.FGTS)
        assert primeira is not None
        assert segunda is None
        assert TarefaEmissao.query.filter_by(certidao_id=ids['fgts']).count() == 1


def test_enfileirar_apos_terminal_permite_nova(app, ids):
    """Tarefa terminal (ok) nao bloqueia uma nova — historico em dias diferentes."""
    with app.app_context():
        _nova_tarefa(app, ids, status='ok')
        nova = fila_emissao.enfileirar(ids['fgts'], TipoCertidao.FGTS)
        assert nova is not None
        assert TarefaEmissao.query.filter_by(certidao_id=ids['fgts']).count() == 2


def test_enfileirar_a_vencer_multi_tipo(app, ids):
    with app.app_context():
        alvos = {'FGTS': [ids['fgts']], 'Estadual': [ids['rs']]}
        resultado = fila_emissao.enfileirar_a_vencer(alvos)
        assert resultado == {'FGTS': [ids['fgts']], 'Estadual': [ids['rs']]}
        # roda de novo: idempotente, nada novo enfileirado
        resultado2 = fila_emissao.enfileirar_a_vencer(alvos)
        assert resultado2 == {'FGTS': [], 'Estadual': []}
        assert TarefaEmissao.query.count() == 2


# --- AC P1.2: transicoes de status ----------------------------------------

def test_marcar_rodando(app, ids):
    with app.app_context():
        t = _nova_tarefa(app, ids)
        assert fila_emissao.marcar_rodando(t, execution_id='exec-1') is True
        assert t.status == 'rodando'
        assert t.iniciada_em is not None
        assert t.execution_id == 'exec-1'


def test_marcar_ok(app, ids):
    with app.app_context():
        t = _nova_tarefa(app, ids, status='rodando', erro='antigo')
        assert fila_emissao.marcar_ok(t) is True
        assert t.status == 'ok'
        assert t.concluida_em is not None
        assert t.erro is None


def test_marcar_falha_persiste_erro(app, ids):
    with app.app_context():
        t = _nova_tarefa(app, ids, status='rodando')
        assert fila_emissao.marcar_falha(t, 'portal fora do ar') is True
        assert t.status == 'falha'
        assert t.concluida_em is not None
        assert t.erro == 'portal fora do ar'


def test_marcar_falha_trunca_erro_em_500(app, ids):
    with app.app_context():
        t = _nova_tarefa(app, ids, status='rodando')
        fila_emissao.marcar_falha(t, 'x' * 800)
        assert len(t.erro) == 500


# --- AC P1.4: retry ate limite --------------------------------------------

def test_resolver_falha_retry_enquanto_ha_tentativas(app, ids):
    with app.app_context():
        t = _nova_tarefa(app, ids, status='rodando')
        status = fila_emissao.resolver_falha(t, 'timeout', max_tentativas=3)
        assert status == 'retry'
        assert t.status == 'retry'
        assert t.tentativas == 1
        assert t.concluida_em is None


def test_resolver_falha_esgota_vira_falha_definitiva(app, ids):
    with app.app_context():
        t = _nova_tarefa(app, ids, status='rodando', tentativas=1)
        status = fila_emissao.resolver_falha(t, 'timeout', max_tentativas=2)
        assert status == 'falha'
        assert t.status == 'falha'
        assert t.tentativas == 2
        assert t.concluida_em is not None


# --- AC P1.3: reconciliacao de orfas --------------------------------------

def test_reconciliar_orfas_rodando_volta_pendente(app, ids):
    with app.app_context():
        t = _nova_tarefa(app, ids, status='rodando', iniciada_em=datetime.now())
        n = fila_emissao.reconciliar_orfas()
        assert n == 1
        db.session.refresh(t)
        assert t.status == 'pendente'
        assert t.iniciada_em is None


def test_reconciliar_orfas_sem_rodando_retorna_zero(app, ids):
    with app.app_context():
        _nova_tarefa(app, ids, status='pendente')
        assert fila_emissao.reconciliar_orfas() == 0


def test_reconciliar_apenas_timeout_ignora_recente(app, ids):
    with app.app_context():
        recente = _nova_tarefa(app, ids, status='rodando',
                               iniciada_em=datetime.now())
        cert_rs = db.session.get(Certidao, ids['rs'])
        antiga = TarefaEmissao(
            tipo='Estadual', empresa_id=cert_rs.empresa_id, certidao_id=cert_rs.id,
            status='rodando',
            iniciada_em=datetime.now() - timedelta(hours=fila_emissao.TIMEOUT_ORFA_HORAS + 1))
        db.session.add(antiga)
        db.session.commit()

        n = fila_emissao.reconciliar_orfas(apenas_timeout=True)
        assert n == 1
        db.session.refresh(recente)
        db.session.refresh(antiga)
        assert recente.status == 'rodando'   # recente preservada
        assert antiga.status == 'pendente'   # travada recuperada


# --- helpers de consulta ---------------------------------------------------

def test_tarefas_elegiveis_inclui_pendente_e_retry_exclui_terminais(app, ids):
    with app.app_context():
        _nova_tarefa(app, ids, status='pendente')
        cert_rs = db.session.get(Certidao, ids['rs'])
        for st in ('retry', 'ok', 'falha'):
            db.session.add(TarefaEmissao(
                tipo='FGTS', empresa_id=cert_rs.empresa_id,
                certidao_id=cert_rs.id, status=st))
        db.session.commit()
        elegiveis = fila_emissao.tarefas_elegiveis(TipoCertidao.FGTS)
        estados = sorted(t.status for t in elegiveis)
        assert estados == ['pendente', 'retry']
