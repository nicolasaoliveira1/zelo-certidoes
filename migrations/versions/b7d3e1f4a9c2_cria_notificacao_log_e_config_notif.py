"""Cria tabela notificacao_log e colunas de notificacao em configuracao_sistema

Revision ID: b7d3e1f4a9c2
Revises: a2f5c9d13b7e
Create Date: 2026-07-17 19:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7d3e1f4a9c2'
down_revision = 'a2f5c9d13b7e'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'notificacao_log',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('chave', sa.String(length=120), nullable=False),
        sa.Column('tipo', sa.String(length=20), nullable=False),
        sa.Column('detalhe', sa.String(length=500), nullable=True),
        sa.Column('enviada_em', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_notificacao_log_chave', 'notificacao_log', ['chave'])

    with op.batch_alter_table('configuracao_sistema') as batch_op:
        batch_op.add_column(sa.Column(
            'notif_destinatarios', sa.String(length=1000), nullable=True))
        batch_op.add_column(sa.Column(
            'notif_cadencia', sa.String(length=10), nullable=False,
            server_default='semanal'))


def downgrade():
    with op.batch_alter_table('configuracao_sistema') as batch_op:
        batch_op.drop_column('notif_cadencia')
        batch_op.drop_column('notif_destinatarios')

    # drop_table remove o indice da tabela automaticamente.
    op.drop_table('notificacao_log')
