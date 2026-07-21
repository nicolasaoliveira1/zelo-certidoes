"""Rotas de empresa: listagem, detalhe, edicao, remocao, abrir pasta,
pagina de nova empresa e cadastro. Extraido de app/routes.py (spec 05, REFA-02).
Registra no blueprint "main" compartilhado (importado de app.routes).
"""
import os
import re
from datetime import date

from flask import (
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from app import db, file_manager
from app.automation.emissao import (
    _formatar_cnpj,
    _normalizar_cnpj,
)
from app.models import (
    Certidao,
    Empresa,
    Municipio,
    SubtipoCertidao,
    TipoCertidao,
    get_a_vencer_dias,
)
from app.utils import (
    json_error as _json_error,
)
from app.services import (
    auditoria,
)
from app.services.execution_logger import log_event
from app.auth import requer_papel

from app.routes import bp, _escolher_cidade_canonica_dashboard, _normalizar_cidade_dashboard


@bp.route('/empresas')
def empresas():
    termo = (request.args.get('q') or '').strip()
    estado_filtro = (request.args.get('estado') or '').strip().upper()
    cidade_filtro = (request.args.get('cidade') or '').strip()

    query = Empresa.query
    if termo:
        query = query.filter(Empresa.nome.ilike(f"%{termo}%"))

    if estado_filtro:
        query = query.filter(Empresa.estado == estado_filtro)

    cidades_variantes = {}
    cidades_db = db.session.query(Empresa.cidade).all()
    for row in cidades_db:
        cidade = (row[0] or '').strip()
        if not cidade:
            continue

        chave_normalizada = _normalizar_cidade_dashboard(cidade)
        if not chave_normalizada:
            continue

        variantes = cidades_variantes.setdefault(chave_normalizada, {})
        variantes[cidade] = variantes.get(cidade, 0) + 1

    cidades_por_chave = {
        chave: _escolher_cidade_canonica_dashboard(variantes)
        for chave, variantes in cidades_variantes.items()
    }
    cidades_disponiveis = sorted(
        cidades_por_chave.values(),
        key=_normalizar_cidade_dashboard,
    )

    empresas = query.order_by(Empresa.id).all()

    if cidade_filtro:
        chave_filtro = _normalizar_cidade_dashboard(cidade_filtro)
        if chave_filtro:
            empresas = [
                empresa for empresa in empresas
                if _normalizar_cidade_dashboard(empresa.cidade) == chave_filtro
            ]
            cidade_filtro = cidades_por_chave.get(chave_filtro, cidade_filtro)

    estados_disponiveis = [
        row[0] for row in
        db.session.query(Empresa.estado).distinct().order_by(Empresa.estado).all()
    ]

    return render_template(
        'empresas.html',
        empresas=empresas,
        termo=termo,
        estado_filtro=estado_filtro,
        cidade_filtro=cidade_filtro,
        estados_disponiveis=estados_disponiveis,
        cidades_disponiveis=cidades_disponiveis,
    )


@bp.route('/empresa/<int:empresa_id>')
def empresa_detalhe(empresa_id):
    empresa = Empresa.query.get_or_404(empresa_id)
    certidoes = sorted(empresa.certidoes, key=lambda item: item.ordem_exibicao)
    return render_template(
        'empresa_detalhe.html',
        empresa=empresa,
        certidoes=certidoes,
        hoje=date.today(),
        a_vencer_dias=get_a_vencer_dias(),
    )


@bp.route('/empresa/<int:empresa_id>/editar', methods=['POST'])
@requer_papel('operador')
def empresa_editar(empresa_id):
    empresa = Empresa.query.get_or_404(empresa_id)
    nome = (request.form.get('nome') or '').strip()
    estado = (request.form.get('estado') or '').strip().upper()
    cidade = (request.form.get('cidade') or '').strip()
    inscricao = (request.form.get('inscricao_mobiliaria') or '').strip()
    next_url = request.form.get('next') or url_for('main.empresa_detalhe', empresa_id=empresa_id)

    if not nome:
        flash('Nome da empresa é obrigatório.', 'warning')
        return redirect(next_url)

    if not estado or not re.match(r'^[A-Z]{2}$', estado):
        flash('Estado inválido. Use a sigla com 2 letras (ex: RS).', 'warning')
        return redirect(next_url)

    if not cidade:
        flash('Cidade é obrigatória.', 'warning')
        return redirect(next_url)

    if inscricao and len(inscricao) > 6:
        flash('Inscrição municipal deve ter até 6 caracteres.', 'warning')
        return redirect(next_url)

    empresa.nome = nome
    empresa.estado = estado
    empresa.cidade = cidade
    empresa.inscricao_mobiliaria = inscricao if inscricao else None

    try:
        db.session.commit()
        flash('Empresa atualizada com sucesso.', 'success')
        auditoria.registrar('empresa.editar', alvo_tipo='empresa', alvo_id=empresa_id)
    except Exception as exc:
        db.session.rollback()
        flash(f'Erro ao atualizar empresa: {exc}', 'danger')
        auditoria.registrar('empresa.editar', alvo_tipo='empresa', alvo_id=empresa_id,
                            resultado='erro', detalhe=str(exc))

    return redirect(next_url)


@bp.route('/empresa/<int:empresa_id>/remover', methods=['GET', 'POST'])
@requer_papel('admin')
def empresa_remover(empresa_id):
    empresa = Empresa.query.get_or_404(empresa_id)
    next_url = request.values.get('next') or url_for('main.empresas')
    detalhe_url = url_for('main.empresa_detalhe', empresa_id=empresa_id)

    if request.method == 'GET':
        return render_template(
            'empresa_remover_confirm.html',
            empresa=empresa,
            next_url=next_url,
        )

    confirmacao = (request.form.get('confirm') or '').strip().lower()

    if next_url == detalhe_url:
        next_url = url_for('main.empresas')

    if confirmacao != '1':
        flash('Confirmação de remoção não recebida.', 'warning')
        return redirect(next_url)

    try:
        db.session.delete(empresa)
        db.session.commit()
        flash(f'Empresa "{empresa.nome}" removida com sucesso.', 'success')
        auditoria.registrar('empresa.remover', alvo_tipo='empresa', alvo_id=empresa_id)
    except Exception as exc:
        db.session.rollback()
        flash(f'Erro ao remover empresa: {exc}', 'danger')
        auditoria.registrar('empresa.remover', alvo_tipo='empresa', alvo_id=empresa_id,
                            resultado='erro', detalhe=str(exc))

    return redirect(next_url)


@bp.route('/empresa/<int:empresa_id>/abrir-pasta', methods=['POST'])
def abrir_pasta_empresa(empresa_id):
    """Abre a pasta CERTIDOES da empresa no Explorer da maquina local.

    O app roda localmente na estacao do operador (mesma maquina do Selenium e do
    drive de rede), entao os.startfile abre o Explorer para quem esta operando.
    Acao de leitura — qualquer papel logado."""
    empresa = Empresa.query.get_or_404(empresa_id)
    pasta_empresa = file_manager.encontrar_pasta_empresa(empresa.nome)
    if not pasta_empresa:
        return _json_error(
            f'Pasta da empresa "{empresa.nome}" nao encontrada na rede.', 404,
            error_type='network_path')
    pasta = file_manager.encontrar_caminho_final(pasta_empresa)
    if not pasta or not os.path.isdir(pasta):
        return _json_error('Pasta de certidoes nao encontrada.', 404, error_type='network_path')
    if not hasattr(os, 'startfile'):
        return _json_error('Abrir pasta so e suportado no Windows.', 400, error_type='plataforma')
    try:
        os.startfile(pasta)
    except OSError as e:
        return _json_error(f'Nao foi possivel abrir a pasta: {e}', 500)
    log_event('empresa_pasta_aberta', empresa_id=empresa_id, pasta=pasta)
    return jsonify({'status': 'ok', 'pasta': pasta})


_NOMES_EXIBICAO_CIDADE = {
    'Capao da Canoa': 'Capão da Canoa',
    'Imbe': 'Imbé',
    'Osorio': 'Osório',
    'Ponta Pora': 'Ponta Porã',
    'Sao Paulo': 'São Paulo',
    'Tramandai': 'Tramandaí',
    'Xangrila': 'Xangri-Lá',
}


@bp.route('/empresa/nova', endpoint='nova_empresa')
@requer_papel('operador')
def pagina_nova_empresa():
    municipios_db = Municipio.query.order_by(Municipio.nome).all()
    vistos = set()
    municipios = []
    for m in municipios_db:
        exibicao = _NOMES_EXIBICAO_CIDADE.get(m.nome, m.nome)
        if exibicao not in vistos:
            vistos.add(exibicao)
            municipios.append((m.nome, exibicao))
    return render_template('nova_empresa.html', municipios=municipios)


@bp.route('/empresa/adicionar', methods=['POST'])
@requer_papel('operador')
def adicionar_empresa():
    # dados formulário
    nome = (request.form.get('nome') or '').strip()
    cnpj = (request.form.get('cnpj') or '').strip()
    estado = (request.form.get('estado') or '').strip().upper()
    cidade = (request.form.get('cidade') or '').strip()
    inscricao = (request.form.get('inscricao_mobiliaria') or '').strip()
    origem = (request.form.get('origem') or '').strip()

    def _redirect_apos_cadastro():
        if origem == 'nova_empresa':
            return redirect(url_for('main.nova_empresa'))
        return redirect(url_for('main.dashboard'))

    if not nome:
        flash('Nome da empresa é obrigatório.', 'warning')
        return _redirect_apos_cadastro()

    cnpj_limpo = _normalizar_cnpj(cnpj)
    if len(cnpj_limpo) != 14:
        flash('CNPJ inválido, verifique os dígitos.', 'warning')
        return _redirect_apos_cadastro()

    if not estado or not re.match(r'^[A-Z]{2}$', estado):
        flash('Estado inválido. Use a sigla com 2 letras (ex: RS).', 'warning')
        return _redirect_apos_cadastro()

    if not cidade:
        flash('Cidade é obrigatória.', 'warning')
        return _redirect_apos_cadastro()

    if inscricao and len(inscricao) > 6:
        flash('Inscrição municipal deve ter até 6 caracteres.', 'warning')
        return _redirect_apos_cadastro()

    cnpj_formatado = _formatar_cnpj(cnpj_limpo) or cnpj
    cnpj = cnpj_formatado

    # validacao
    cnpj_variantes = {cnpj}
    if cnpj_limpo:
        cnpj_variantes.add(cnpj_limpo)
    if cnpj_formatado:
        cnpj_variantes.add(cnpj_formatado)

    empresa_existente = Empresa.query.filter(Empresa.cnpj.in_(cnpj_variantes)).first()
    if empresa_existente:
        flash(f'Empresa com CNPJ {cnpj} já está cadastrada.', 'warning')
        return _redirect_apos_cadastro()

    # Cria objeto empresa
    empresa_nova = Empresa(
        nome=nome,
        cnpj=cnpj,
        estado=estado,
        cidade=cidade,
        # Garante que seja nulo se vazio
        inscricao_mobiliaria=inscricao if inscricao else None
    )
    db.session.add(empresa_nova)

    cidade_norm = file_manager.remover_acentos(cidade or '').upper()
    is_imbe = cidade_norm == 'IMBE'

    for tipo in TipoCertidao:
        if tipo == TipoCertidao.MUNICIPAL:
            if is_imbe:
                db.session.add(Certidao(
                    tipo=tipo,
                    subtipo=SubtipoCertidao.GERAL,
                    empresa=empresa_nova,
                    data_validade=None
                ))
                db.session.add(Certidao(
                    tipo=tipo,
                    subtipo=SubtipoCertidao.MOBILIARIO,
                    empresa=empresa_nova,
                    data_validade=None
                ))
            else:
                db.session.add(Certidao(
                    tipo=tipo,
                    empresa=empresa_nova,
                    data_validade=None
                ))
            continue

        db.session.add(Certidao(
            tipo=tipo,
            empresa=empresa_nova,
            data_validade=None
        ))

    # Salva no banco
    try:
        db.session.commit()
        flash(f'Empresa "{nome}" cadastrada com sucesso!', 'success')
        auditoria.registrar('empresa.criar', alvo_tipo='empresa', alvo_id=empresa_nova.id)
    except Exception as e:
        db.session.rollback()
        flash(f'Erro ao cadastrar empresa: {e}', 'danger')
        auditoria.registrar('empresa.criar', resultado='erro', detalhe=str(e))

    return _redirect_apos_cadastro()

