"""Cria tabela execucao_lote (historico de inicio de lotes)

Revision ID: b7e3d1f9c2a5
Revises: a7d3f19b2c60
Create Date: 2026-07-05 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7e3d1f9c2a5'
down_revision = 'a7d3f19b2c60'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'execucao_lote',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tipo', sa.String(length=30), nullable=False),
        sa.Column('escopo', sa.String(length=20), nullable=False,
                  server_default='default'),
        sa.Column('total', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('iniciado_em', sa.DateTime(), nullable=False),
        sa.Column('execution_id', sa.String(length=40), nullable=True),
    )
    op.create_index('ix_execucao_lote_tipo', 'execucao_lote', ['tipo'])
    op.create_index(
        'ix_execucao_lote_iniciado_em', 'execucao_lote', ['iniciado_em'])


def downgrade():
    op.drop_index('ix_execucao_lote_iniciado_em', table_name='execucao_lote')
    op.drop_index('ix_execucao_lote_tipo', table_name='execucao_lote')
    op.drop_table('execucao_lote')
