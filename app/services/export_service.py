"""Geracao das planilhas XLSX de exportacao (spec 04, EXPORT-01 e EXPORT-05).

Monta os arquivos em memoria (BytesIO) para as rotas devolverem via send_file.
Read-only sobre Empresa/Certidao/ExecucaoLote — sem efeitos colaterais.
"""
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font

from app.services import carteira_filtros
from app.services.execution_logger import log_event

_STATUS_ROTULO = {
    'validas': 'Válida',
    'a_vencer': 'A vencer',
    'vencidas': 'Vencida',
    'pendentes': 'Pendente',
    'nao_definida': 'Sem data',
}

_COLUNAS_CARTEIRA = ['Empresa', 'CNPJ', 'UF', 'Cidade', 'Tipo', 'Subtipo',
                     'Status', 'Validade', 'Última atualização']

_LARGURAS_CARTEIRA = [30, 20, 6, 20, 14, 12, 12, 12, 18]


def _fmt_data(d):
    return d.strftime('%d/%m/%Y') if d else '—'


def _fmt_datahora(dt):
    return dt.strftime('%d/%m/%Y %H:%M') if dt else '—'


def gerar_planilha_carteira(status=None, tipo=None, estado=None, cidade=None):
    """XLSX com 1 linha por certidao do recorte (mesmos filtros do painel).

    Recorte vazio -> planilha so com o cabecalho (arquivo valido, sem erro).
    """
    linhas = carteira_filtros.filtrar(status=status, tipo=tipo, estado=estado, cidade=cidade)
    linhas.sort(key=lambda ln: ((ln.empresa.nome or '').upper(), ln.certidao.ordem_exibicao))

    wb = Workbook()
    ws = wb.active
    ws.title = 'Carteira'
    ws.append(_COLUNAS_CARTEIRA)
    for celula in ws[1]:
        celula.font = Font(bold=True)
    for indice, largura in enumerate(_LARGURAS_CARTEIRA, start=1):
        ws.column_dimensions[ws.cell(row=1, column=indice).column_letter].width = largura

    for linha in linhas:
        emp = linha.empresa
        cert = linha.certidao
        ws.append([
            emp.nome,
            emp.cnpj,
            emp.estado,
            emp.cidade,
            cert.tipo.value,
            cert.subtipo.value if cert.subtipo else '',
            _STATUS_ROTULO.get(linha.status_cat, linha.status_cat),
            _fmt_data(cert.data_validade),
            _fmt_datahora(cert.atualizado_em),
        ])

    if not linhas:
        log_event('export_carteira_vazio', level='INFO')

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer
