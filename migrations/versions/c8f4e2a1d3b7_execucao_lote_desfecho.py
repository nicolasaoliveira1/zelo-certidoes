"""Adiciona colunas de desfecho (rendimento) a execucao_lote

Revision ID: c8f4e2a1d3b7
Revises: b7e3d1f9c2a5
Create Date: 2026-07-05 13:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c8f4e2a1d3b7'
down_revision = 'b7e3d1f9c2a5'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('execucao_lote',
                  sa.Column('finalizado_em', sa.DateTime(), nullable=True))
    op.add_column('execucao_lote',
                  sa.Column('status', sa.String(length=20), nullable=True))
    op.add_column('execucao_lote',
                  sa.Column('sucesso', sa.Integer(), nullable=False,
                            server_default='0'))
    op.add_column('execucao_lote',
                  sa.Column('pendentes_resultado', sa.Integer(), nullable=False,
                            server_default='0'))
    op.add_column('execucao_lote',
                  sa.Column('falhas', sa.Integer(), nullable=False,
                            server_default='0'))


def downgrade():
    op.drop_column('execucao_lote', 'falhas')
    op.drop_column('execucao_lote', 'pendentes_resultado')
    op.drop_column('execucao_lote', 'sucesso')
    op.drop_column('execucao_lote', 'status')
    op.drop_column('execucao_lote', 'finalizado_em')
