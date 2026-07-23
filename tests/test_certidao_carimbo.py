"""Testes do carimbo automatico Certidao.atualizado_em (ORD-01, ORD-02).

Deriva da AC P1-1 do spec: "WHEN uma Certidao e criada ou tem qualquer campo
alterado e persistido THEN o sistema SHALL gravar/atualizar seu timestamp de
ultima atualizacao automaticamente".
"""
from datetime import date, datetime, timedelta

import sqlalchemy as sa

from app import db
from app.models import Certidao, Empresa, TipoCertidao


def _empresa():
    emp = Empresa(nome='Carimbo SA', cnpj='55.555.555/5555-55',
                  estado='RS', cidade='Tramandai')
    db.session.add(emp)
    db.session.commit()
    return emp


def test_atualizado_em_preenchido_na_criacao(app, ids):
    """Criar uma Certidao carimba atualizado_em (default na INSERT)."""
    with app.app_context():
        emp = _empresa()
        antes = datetime.now()
        cert = Certidao(tipo=TipoCertidao.FEDERAL, empresa=emp)
        db.session.add(cert)
        db.session.commit()
        depois = datetime.now()
        assert cert.atualizado_em is not None
        # carimbo em hora local, dentro da janela do teste com folga de 1s. O
        # DATETIME do MySQL (sem casas fracionarias) ARREDONDA para o segundo mais
        # proximo — pode cair ate ~0,5s antes de `antes` ou depois de `depois`; o
        # SQLite guarda o datetime cheio. A folga de 1s vale nos dois bancos e
        # ainda prova que o carimbo foi gravado na criacao (precisao de segundo).
        assert antes - timedelta(seconds=1) <= cert.atualizado_em <= depois + timedelta(seconds=1)


def test_atualizado_em_avanca_no_update(app, ids):
    """Alterar um campo e commitar AVANCA atualizado_em (onupdate na UPDATE).

    Usa um baseline conhecido no passado (via Core update, que nao dispara o
    onupdate) e exige que um UPDATE via ORM leve o carimbo para depois dele —
    assim o teste falha se o onupdate for removido (nao apenas 'nao regride')."""
    passado = datetime(2000, 1, 1, 0, 0, 0)
    with app.app_context():
        emp = _empresa()
        cert = Certidao(tipo=TipoCertidao.FEDERAL, empresa=emp)
        db.session.add(cert)
        db.session.commit()
        # ancora o carimbo no passado sem disparar onupdate (valor explicito)
        db.session.execute(sa.update(Certidao)
                           .where(Certidao.id == cert.id)
                           .values(atualizado_em=passado))
        db.session.commit()
        db.session.refresh(cert)
        assert cert.atualizado_em == passado
        # um UPDATE via ORM deve disparar onupdate e avancar para o presente
        cert.data_validade = date(2030, 1, 1)
        db.session.commit()
        assert cert.atualizado_em > passado
