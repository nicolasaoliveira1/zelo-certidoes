"""Cria tabela tarefa_emissao e colunas de agendador em configuracao_sistema

Revision ID: a2f5c9d13b7e
Revises: 691521add9a0
Create Date: 2026-07-15 20:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a2f5c9d13b7e'
down_revision = '691521add9a0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'tarefa_emissao',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tipo', sa.String(length=20), nullable=False),
        sa.Column('empresa_id', sa.Integer(),
                  sa.ForeignKey('empresa.id'), nullable=False),
        sa.Column('certidao_id', sa.Integer(),
                  sa.ForeignKey('certidao.id'), nullable=False),
        sa.Column('status', sa.String(length=12), nullable=False,
                  server_default='pendente'),
        sa.Column('tentativas', sa.Integer(), nullable=False,
                  server_default='0'),
        sa.Column('agendada_em', sa.DateTime(), nullable=False),
        sa.Column('iniciada_em', sa.DateTime(), nullable=True),
        sa.Column('concluida_em', sa.DateTime(), nullable=True),
        sa.Column('erro', sa.String(length=500), nullable=True),
        sa.Column('execution_id', sa.String(length=40), nullable=True),
    )
    op.create_index('ix_tarefa_emissao_tipo', 'tarefa_emissao', ['tipo'])
    op.create_index('ix_tarefa_emissao_certidao_id', 'tarefa_emissao', ['certidao_id'])
    op.create_index('ix_tarefa_emissao_status', 'tarefa_emissao', ['status'])
    op.create_index('ix_tarefa_emissao_execution_id', 'tarefa_emissao', ['execution_id'])

    with op.batch_alter_table('configuracao_sistema') as batch_op:
        batch_op.add_column(sa.Column(
            'agendador_ativo', sa.Boolean(), nullable=False,
            server_default=sa.text('1')))
        batch_op.add_column(sa.Column(
            'agendador_hora', sa.Integer(), nullable=False,
            server_default='3'))


def downgrade():
    with op.batch_alter_table('configuracao_sistema') as batch_op:
        batch_op.drop_column('agendador_hora')
        batch_op.drop_column('agendador_ativo')

    # drop_table remove os indices/FKs da tabela automaticamente; no MySQL um
    # drop_index explicito em coluna de FK (certidao_id) falharia (1553).
    op.drop_table('tarefa_emissao')
