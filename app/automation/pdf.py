"""Leitura e classificação de PDFs de certidões.

Extraído de routes.py (C1). São funções puras de I/O + parsing, sem
dependência de Selenium nem do estado de lote.
"""
import os
import re
from datetime import datetime

import pdfplumber

from app import db, file_manager
from app.models import StatusEspecial


def extrair_validade_federal(caminho_pdf):
    if not caminho_pdf:
        return None

    try:
        with pdfplumber.open(caminho_pdf) as pdf:
            texto = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as exc:
        print(f"[FEDERAL] Erro ao ler PDF: {exc}")
        return None

    match = re.search(r"Válida\s+até\s+(\d{2}/\d{2}/\d{4})", texto, re.IGNORECASE)
    if not match:
        return None

    try:
        return datetime.strptime(match.group(1), "%d/%m/%Y").date()
    except ValueError:
        return None


def extrair_texto(caminho_pdf, origem_log='PDF'):
    if not caminho_pdf:
        return ''

    try:
        with pdfplumber.open(caminho_pdf) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as exc:
        print(f"[{origem_log}] Erro ao ler PDF: {exc}")
        return ''


def _normalizar(texto):
    texto = file_manager.remover_acentos(texto or '')
    texto = re.sub(r'\s+', ' ', texto)
    return texto.upper().strip()


def classificar_texto(texto):
    """Classifica o conteúdo textual de uma certidão (lógica pura, testável).

    Retorna 'efeito_negativa' | 'positiva' | 'negativa' | 'desconhecida'.
    """
    texto = _normalizar(texto)
    if not texto:
        return 'desconhecida'

    if re.search(r'CERTIDAO\s+POSITIVA\s+COM\s+EFEITOS?\s+DE\s+NEGATIVA', texto):
        return 'efeito_negativa'

    if re.search(r'CERTIDAO\s+POSITIVA\b', texto):
        return 'positiva'

    if re.search(r'CERTIDAO\s+NEGATIVA\b', texto):
        return 'negativa'

    return 'desconhecida'


def classificar_status(caminho_pdf, origem_log='PDF'):
    return classificar_texto(extrair_texto(caminho_pdf, origem_log=origem_log))


def classificar_e_tratar_positivo(certidao, caminho_pdf, origem_log='PDF', tipo_label=None):
    classificacao = classificar_status(caminho_pdf, origem_log=origem_log)
    if classificacao != 'positiva':
        return classificacao, None

    erro_remocao = None
    try:
        if caminho_pdf and os.path.exists(caminho_pdf):
            os.remove(caminho_pdf)
    except Exception as exc_remove:
        erro_remocao = str(exc_remove)

    tipo_label_final = (tipo_label or (certidao.tipo.value if certidao else '') or 'CERTIDAO').strip()
    try:
        if certidao:
            certidao.caminho_arquivo = None
            certidao.status_especial = StatusEspecial.PENDENTE
            certidao.data_validade = None
            db.session.commit()
    except Exception:
        db.session.rollback()
        msg = (
            f'Certidão {tipo_label_final} POSITIVA detectada, '
            'mas houve erro ao marcar como PENDENTE no banco.'
        )
        return 'erro', msg

    msg = f'Certidão {tipo_label_final} detectada como POSITIVA. Arquivo removido e certidão marcada como PENDENTE.'
    if erro_remocao:
        msg += f' Não foi possível remover o arquivo automaticamente: {erro_remocao}'
    return 'positiva', msg


def classificar_estadual_rs(caminho_pdf):
    return classificar_status(caminho_pdf, origem_log='ESTADUAL-RS')
