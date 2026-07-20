"""Recorte da carteira replicado server-side (spec 04, EXPORT-02).

Fonte unica do "o que o painel mostraria" para um dado conjunto de filtros. O
dashboard NAO filtra no servidor: carrega todas as empresas e o JS (`aplicarFiltros`)
esconde/mostra. Aqui reproduzimos a MESMA semantica em Python para a exportacao
sair exatamente igual a tela:

- estado/cidade filtram a EMPRESA (o card inteiro);
- tipo/status filtram a CERTIDAO (a linha);
- lista vazia ou 'todas' = sem filtro naquele eixo.

Reusa o classificador de status compartilhado (`snapshot_service`) e a chave de
cidade compartilhada (`utils.normalizar_cidade`) para nao divergir do painel.
"""
from dataclasses import dataclass
from datetime import date

from app.models import Certidao, Empresa
from app.services.snapshot_service import classificar_status_certidao
from app.utils import normalizar_cidade


@dataclass
class LinhaCarteira:
    """Uma certidao que passou no recorte, com a empresa e a categoria de status."""
    empresa: Empresa
    certidao: Certidao
    status_cat: str


def status_categoria(certidao, hoje):
    """Categoria de status no vocabulario do painel: validas | a_vencer | vencidas
    | pendentes | nao_definida.

    Envelopa `classificar_status_certidao` (que devolve 'sem_data') e mapeia para
    'nao_definida', o rotulo usado pelos chips do dashboard (`data-status`).
    """
    cat = classificar_status_certidao(certidao, hoje)
    return 'nao_definida' if cat == 'sem_data' else cat


def _normalizar_lista(valores):
    """None/vazio/'todas' -> None (sem filtro); caso contrario um set limpo."""
    if not valores:
        return None
    limpos = {str(v).strip() for v in valores if v is not None and str(v).strip()}
    if not limpos or 'todas' in limpos:
        return None
    return limpos


def filtrar(status=None, tipo=None, estado=None, cidade=None, hoje=None):
    """Devolve as `LinhaCarteira` que o painel mostraria com esses filtros.

    Cada argumento e uma lista (multi-selecao); vazio/'todas' = sem filtro.
    `tipo` usa os valores dos chips (federal/fgts/estadual/municipal/trabalhista).
    """
    hoje = hoje or date.today()
    status_set = _normalizar_lista(status)
    tipo_set = _normalizar_lista(tipo)
    estados = _normalizar_lista(estado)
    estado_set = {e.upper() for e in estados} if estados else None
    cidades = _normalizar_lista(cidade)
    cidade_set = {normalizar_cidade(c) for c in cidades} if cidades else None

    linhas = []
    for empresa in Empresa.query.order_by(Empresa.id).all():
        # estado/cidade filtram a empresa inteira (o card no painel)
        if estado_set is not None and (empresa.estado or '').upper() not in estado_set:
            continue
        if cidade_set is not None and normalizar_cidade(empresa.cidade) not in cidade_set:
            continue
        for certidao in empresa.certidoes:
            # tipo/status filtram a certidao (a linha no painel)
            if tipo_set is not None and certidao.tipo.name.lower() not in tipo_set:
                continue
            cat = status_categoria(certidao, hoje)
            if status_set is not None and cat not in status_set:
                continue
            linhas.append(LinhaCarteira(empresa=empresa, certidao=certidao, status_cat=cat))
    return linhas
