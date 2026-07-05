"""Cria tabela snapshot_certidao (foto diaria para grafico de evolucao)

Revision ID: d1a5c3e7b9f2
Revises: c8f4e2a1d3b7
Create Date: 2026-07-05 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd1a5c3e7b9f2'
down_revision = 'c8f4e2a1d3b7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'snapshot_certidao',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('data', sa.Date(), nullable=False),
        sa.Column('tipo', sa.String(length=20), nullable=False),
        sa.Column('status', sa.String(length=12), nullable=False),
        sa.Column('quantidade', sa.Integer(), nullable=False,
                  server_default='0'),
        sa.UniqueConstraint('data', 'tipo', 'status',
                            name='uq_snapshot_dia_tipo_status'),
    )
    op.create_index('ix_snapshot_certidao_data', 'snapshot_certidao', ['data'])


def downgrade():
    op.drop_index('ix_snapshot_certidao_data', table_name='snapshot_certidao')
    op.drop_table('snapshot_certidao')
