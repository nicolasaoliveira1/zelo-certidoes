"""Adiciona atualizado_em em certidao + backfill por mtime do PDF

Revision ID: a7d3f19b2c60
Revises: f5a1b2c3d4e5
Create Date: 2026-07-03 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a7d3f19b2c60'
down_revision = 'f5a1b2c3d4e5'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('certidao', schema=None) as batch_op:
        batch_op.add_column(sa.Column('atualizado_em', sa.DateTime(), nullable=True))

    # Backfill best-effort: usa o mtime do PDF quando o arquivo existe em disco;
    # registros sem arquivo ficam NULL (tendem ao topo na ordenacao). Mesma
    # convencao de hora local do carimbo ao vivo (datetime.now).
    from app.utils import mtime_para_datetime_local

    bind = op.get_bind()
    certidao = sa.table(
        'certidao',
        sa.column('id', sa.Integer),
        sa.column('caminho_arquivo', sa.String),
        sa.column('atualizado_em', sa.DateTime),
    )
    linhas = bind.execute(
        sa.select(certidao.c.id, certidao.c.caminho_arquivo)
    ).fetchall()
    for cid, caminho in linhas:
        dt = mtime_para_datetime_local(caminho)
        if dt is not None:
            bind.execute(
                sa.update(certidao)
                .where(certidao.c.id == cid)
                .values(atualizado_em=dt)
            )


def downgrade():
    with op.batch_alter_table('certidao', schema=None) as batch_op:
        batch_op.drop_column('atualizado_em')
