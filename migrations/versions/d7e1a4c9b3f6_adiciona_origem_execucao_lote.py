"""Adiciona origem (manual|agendador) em execucao_lote

Revision ID: d7e1a4c9b3f6
Revises: b7d3e1f4a9c2
Create Date: 2026-07-22 12:00:00.000000

Rastreia quem disparou o lote (operador via rota HTTP vs agendador da emissao
proativa) para segmentar os relatorios sem esconder nenhum lote (spec 07, COV-04).
Registros existentes recebem 'manual' via server_default no backfill.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd7e1a4c9b3f6'
down_revision = 'b7d3e1f4a9c2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('execucao_lote', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'origem', sa.String(length=12), nullable=False,
            server_default='manual'))
        batch_op.create_index(
            'ix_execucao_lote_origem', ['origem'], unique=False)


def downgrade():
    with op.batch_alter_table('execucao_lote', schema=None) as batch_op:
        batch_op.drop_index('ix_execucao_lote_origem')
        batch_op.drop_column('origem')
