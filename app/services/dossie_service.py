"""Dossie PDF por empresa (spec 04, EXPORT-03/EXPORT-04).

Gera um unico PDF = capa (fpdf2) + as certidoes VALIDAS (status verde/amarelo)
com PDF, concatenadas na ordem de exibicao (pypdf). Robusto: PDF ausente ou
corrompido e pulado com aviso, sem derrubar o dossie; se nenhuma certidao valida
resta, devolve (None, avisos) para a rota informar "sem certidoes validas".

Read-only: nunca escreve no banco nem move arquivos.
"""
import os
from datetime import datetime
from io import BytesIO

from fpdf import FPDF
from pypdf import PdfReader, PdfWriter

from app import file_manager
from app.services.execution_logger import log_event

# Status considerados "validos" para licitacao (dentro da validade):
# verde (valida) + amarelo (a vencer, ainda vale). Ver Certidao.status.
_STATUS_VALIDOS = {'verde', 'amarelo'}


def _latin1(texto):
    """Sanitiza para latin-1 (fonte core do fpdf2 nao aceita fora desse range)."""
    return (texto or '').encode('latin-1', 'replace').decode('latin-1')


def _resolver_pdf(certidao):
    """Caminho do PDF da certidao, ou None. Usa `caminho_arquivo` e, se faltar,
    o mesmo fallback de `visualizar_certidao` (localizar na rede)."""
    caminho = certidao.caminho_arquivo
    if caminho and os.path.exists(caminho):
        return caminho
    alternativo = file_manager.localizar_certidao_existente(
        certidao.empresa.nome,
        certidao.tipo.value,
        certidao.subtipo.value if certidao.subtipo else None,
    )
    return alternativo if alternativo and os.path.exists(alternativo) else None


def certidoes_do_dossie(empresa):
    """Certidoes validas (verde/amarelo) com PDF resolvivel, ordenadas por
    `ordem_exibicao`. Retorna lista de (certidao, caminho_pdf)."""
    elegiveis = []
    for certidao in sorted(empresa.certidoes, key=lambda c: c.ordem_exibicao):
        if certidao.status not in _STATUS_VALIDOS:
            continue
        caminho = _resolver_pdf(certidao)
        if caminho:
            elegiveis.append((certidao, caminho))
    return elegiveis


def _gerar_capa(empresa, certidoes):
    """Pagina de capa: identifica a empresa e lista as certidoes incluidas."""
    pdf = FPDF()
    pdf.add_page()

    # new_x/new_y devolvem o cursor a margem esquerda e descem a linha; sem isso
    # o multi_cell deixa x na margem direita e o proximo estoura por falta de
    # largura ("Not enough horizontal space").
    def _linha(texto):
        pdf.multi_cell(0, 8, text=_latin1(texto), new_x='LMARGIN', new_y='NEXT')

    pdf.set_font('Helvetica', 'B', 16)
    pdf.multi_cell(0, 10, text=_latin1('Dossiê de Regularidade Fiscal'),
                   new_x='LMARGIN', new_y='NEXT')
    pdf.ln(4)

    pdf.set_font('Helvetica', '', 12)
    for linha in (
        f'Empresa: {empresa.nome}',
        f'CNPJ: {empresa.cnpj}',
        f'Cidade/UF: {empresa.cidade}/{empresa.estado}',
        f'Gerado em: {datetime.now().strftime("%d/%m/%Y %H:%M")}',
    ):
        _linha(linha)
    pdf.ln(4)

    pdf.set_font('Helvetica', 'B', 12)
    _linha('Certidões incluídas:')
    pdf.set_font('Helvetica', '', 12)
    for certidao, _ in certidoes:
        rotulo = certidao.tipo.value
        if certidao.subtipo:
            rotulo += f' - {certidao.subtipo.value}'
        _linha(f'  - {rotulo}')

    return bytes(pdf.output())


def gerar_dossie(empresa):
    """Monta o dossie da empresa.

    Retorna (BytesIO, avisos) com o PDF (capa + certidoes validas), ou
    (None, avisos) quando nao ha nenhuma certidao valida legivel — a rota usa o
    None para responder "sem certidoes validas" em vez de baixar um PDF vazio.
    """
    avisos = []
    prontas = []
    for certidao, caminho in certidoes_do_dossie(empresa):
        try:
            prontas.append((certidao, PdfReader(caminho)))
        except Exception as exc:
            avisos.append(f'{certidao.tipo.value}: PDF ignorado')
            log_event('dossie_pdf_ignorado', level='WARNING',
                      empresa=empresa.nome, tipo=certidao.tipo.value, error=str(exc))

    if not prontas:
        return None, (avisos or ['sem certidões válidas'])

    writer = PdfWriter()
    writer.append(BytesIO(_gerar_capa(empresa, prontas)))
    for _, reader in prontas:
        writer.append(reader)

    buffer = BytesIO()
    writer.write(buffer)
    buffer.seek(0)
    return buffer, avisos
