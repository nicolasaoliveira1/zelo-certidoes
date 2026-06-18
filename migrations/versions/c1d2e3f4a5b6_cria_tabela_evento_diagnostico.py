"""Cria tabela evento_diagnostico (historico de diagnostico)

Revision ID: c1d2e3f4a5b6
Revises: a1b2c3d4e5f6
Create Date: 2026-06-18 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c1d2e3f4a5b6'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'evento_diagnostico',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('criado_em', sa.DateTime(), nullable=False),
        sa.Column('evento', sa.String(length=80), nullable=False),
        sa.Column('nivel', sa.String(length=10), nullable=False),
        sa.Column('error_type', sa.String(length=30), nullable=True),
        sa.Column('alvo', sa.String(length=80), nullable=True),
        sa.Column('mensagem', sa.String(length=500), nullable=True),
        sa.Column('request_id', sa.String(length=40), nullable=True),
        sa.Column('execution_id', sa.String(length=40), nullable=True),
        sa.Column('certidao_id', sa.Integer(), nullable=True),
        sa.Column('empresa_id', sa.Integer(), nullable=True),
    )
    op.create_index(
        'ix_evento_diagnostico_criado_em', 'evento_diagnostico', ['criado_em'])


def downgrade():
    op.drop_index('ix_evento_diagnostico_criado_em', table_name='evento_diagnostico')
    op.drop_table('evento_diagnostico')
