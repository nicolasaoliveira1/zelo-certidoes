import os
import re
import time
from datetime import date, datetime

from flask import (
    Blueprint,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from app import db, file_manager
from app.automation import SITES_CERTIDOES
from app.automation.emissao import (
    _carregar_config_municipio,
    _formatar_cnpj,
    _normalizar_cnpj,
)
from app.models import (
    Certidao,
    ConfiguracaoSistema,
    Empresa,
    ExecucaoLote,
    Municipio,
    SnapshotCertidao,
    StatusEspecial,
    SubtipoCertidao,
    TipoCertidao,
    get_a_vencer_dias,
)
from app.utils import (
    json_error as _json_error,
    normalizar_cidade,
    to_bool as _to_bool,
    utcnow_naive,
)
from app.services import (
    agendador,
    auditoria,
    diagnostics,
    dossie_service,
    export_service,
)
from app.services.correlation import CorrelationContext
from app.services.execution_logger import log_event
from app.services.visualizar_token import _gerar_visualizar_token
from app.services.snapshot_service import (
    classificar_status_certidao as _classificar_status_certidao,
    garantir_snapshot_diario as _garantir_snapshot_diario,
)
from app.services.health import run_health_checks
from app.auth import requer_papel
from flask_login import current_user

bp = Blueprint('main', __name__)

@bp.before_app_request
def _before_request_observability():
    g.req_start = time.time()
    CorrelationContext.new_request_id()


@bp.after_app_request
def _after_request_observability(response):
    request_id = CorrelationContext.get_request_id()
    if request_id:
        response.headers['X-Request-Id'] = request_id

    duration_ms = int((time.time() - getattr(g, 'req_start', time.time())) * 1000)

    path = request.path or ''
    is_static = path.startswith('/static/') or path == '/favicon.ico'
    is_batch_poll = path in {'/fgts/lote/status', '/estadual-rs/lote/status', '/municipal/lote/status'}
    is_health_ok = path == '/health' and response.status_code == 200

    if is_static or is_health_ok or (is_batch_poll and response.status_code == 200):
        CorrelationContext.clear()
        return response

    log_event(
        'http_request',
        method=request.method,
        path=path,
        status_code=response.status_code,
        duration_ms=duration_ms,
    )
    CorrelationContext.clear()
    return response


def _current_app_object():
    return current_app._get_current_object()




@bp.route('/health')
def health():
    # liveness publico e minimo: nao vaza detalhes de infra (AUTH-07.2)
    detalhado = _to_bool(request.args.get('detalhado'))
    if not detalhado:
        return jsonify({'status': 'ok'}), 200
    # health detalhado exige admin
    if not (current_user.is_authenticated and current_user.papel == 'admin'):
        return _json_error('Detalhes de saúde exigem admin.', 403, error_type='forbidden')
    checks = run_health_checks(current_app.config)
    has_failure = any(not item.get('ok') for item in checks.values())
    code = 200 if not has_failure else 503
    return jsonify({'status': 'ok' if not has_failure else 'degraded', 'checks': checks}), code


@bp.route('/diagnostico')
@requer_papel('admin')
def diagnostico():
    return render_template('diagnostico.html')


@bp.route('/diagnostico/eventos')
@requer_papel('admin')
def diagnostico_eventos():
    return jsonify({
        'status': 'ok',
        'eventos': diagnostics.eventos_para_painel(limite=100),
        'alertas': diagnostics.alertas_ativos(),
    })


@bp.route('/diagnostico/2captcha')
@requer_papel('admin')
def diagnostico_2captcha():
    """Saldo atual da conta 2captcha para o painel de diagnóstico. Best-effort:
    `saldo` vem None se a chave não está configurada ou a consulta falha."""
    from app.captcha_solver import consultar_saldo
    tem_chave = bool((current_app.config.get('CAPTCHA_2_API_KEY') or '').strip())
    saldo = consultar_saldo(current_app.config) if tem_chave else None
    minimo = current_app.config.get('CAPTCHA_2_SALDO_MINIMO', 0)
    return jsonify({
        'status': 'ok',
        'configurado': tem_chave,
        'saldo': saldo,
        'minimo': minimo,
        'baixo': (saldo is not None and saldo < minimo),
    })


@bp.app_template_global()
def visualizar_token(certidao_id):
    return _gerar_visualizar_token(certidao_id)


def _normalizar_cidade_dashboard(valor):
    # Alias fino para a fonte unica em utils (reuso painel/export — spec 04).
    return normalizar_cidade(valor)


def _escolher_cidade_canonica_dashboard(variantes):
    def _ordenacao(item):
        nome, frequencia = item
        tem_acento = file_manager.remover_acentos(nome) != nome
        return (
            -frequencia,
            -int(tem_acento),
            _normalizar_cidade_dashboard(nome),
            nome.upper(),
        )

    return sorted(variantes.items(), key=_ordenacao)[0][0]

@bp.context_processor
def inject_year():
    return {'year': datetime.now().year}


def _contar_pendencias():
    """Total global de certidões que exigem ação (vencidas + a vencer).

    Consulta apenas as colunas necessárias para não materializar objetos
    Certidao a cada chamada. Reutilizado pelo context processor (title da
    aba no page-load) e pelo endpoint /api/pendencias (polling em tempo real).
    """
    hoje = date.today()
    limites_por_tipo = {t: get_a_vencer_dias(tipo=t) for t in TipoCertidao}

    total = 0
    linhas = db.session.query(
        Certidao.tipo,
        Certidao.data_validade,
        Certidao.status_especial,
    ).all()
    for tipo, data_validade, status_especial in linhas:
        if status_especial == StatusEspecial.PENDENTE or not data_validade:
            continue
        if data_validade < hoje or (data_validade - hoje).days <= limites_por_tipo[tipo]:
            total += 1

    return total


@bp.context_processor
def inject_pendencias_total():
    """Disponibiliza a contagem de pendências em todos os templates (title da aba)."""
    return {'pendencias_total': _contar_pendencias()}


@bp.route('/api/pendencias')
def api_pendencias():
    """Total de pendências para o polling do title da aba (base.html)."""
    return jsonify({'total': _contar_pendencias()})

@bp.route('/')
def dashboard():
    status_filtros = request.args.getlist('status')
    tipo_filtros = request.args.getlist('tipo')
    # Estado e cidade são multi-seleção e filtrados no cliente (chips com
    # contagem combinável). O servidor só lê os params para pré-marcar os chips.
    estado_filtros = [e.strip().upper() for e in request.args.getlist('estado') if e and e.strip()]
    cidade_filtros = []
    for c in request.args.getlist('cidade'):
        chave = _normalizar_cidade_dashboard(c or '')
        if chave and chave not in cidade_filtros:
            cidade_filtros.append(chave)
    ordem = (request.args.get('ordem') or 'urgencia').strip().lower()

    query = db.session.query(Empresa).distinct()

    hoje = date.today()
    _garantir_snapshot_diario()
    a_vencer_dias = get_a_vencer_dias()
    limites_por_tipo = {t: get_a_vencer_dias(tipo=t) for t in TipoCertidao}
    if ordem not in {'urgencia', 'az', 'vencimento', 'atualizacao'}:
        ordem = 'urgencia'

    if not status_filtros:
        status_filtros = ['todas']
    elif 'todas' in status_filtros:
        status_filtros = ['todas']

    if not tipo_filtros:
        tipo_filtros = ['todas']
    elif 'todas' in tipo_filtros:
        tipo_filtros = ['todas']
    else:
        tipos_validos = {'federal', 'fgts', 'estadual', 'municipal', 'trabalhista'}
        tipo_filtros = [t for t in tipo_filtros if t in tipos_validos]
        if not tipo_filtros:
            tipo_filtros = ['todas']

    # Sem filtro server-side de estado/cidade: a query carrega todas as empresas
    # para o cliente poder contar de forma cruzada (estado × cidade × tipo × status).

    # Query única para cidades e estados (evita round-trip extra ao banco)
    cidades_variantes = {}
    cidades_estados = {}
    estados_set = set()
    for cidade, estado in db.session.query(Empresa.cidade, Empresa.estado).all():
        if estado:
            estados_set.add(estado)
        cidade = (cidade or '').strip()
        if not cidade:
            continue
        chave_normalizada = _normalizar_cidade_dashboard(cidade)
        if not chave_normalizada:
            continue
        variantes = cidades_variantes.setdefault(chave_normalizada, {})
        variantes[cidade] = variantes.get(cidade, 0) + 1
        if estado:
            cidades_estados.setdefault(chave_normalizada, set()).add(estado)

    estados_disponiveis = sorted(estados_set)

    cidades_por_chave = {
        chave: _escolher_cidade_canonica_dashboard(variantes)
        for chave, variantes in cidades_variantes.items()
    }

    # Chips de cidade: rótulo canônico + estados a que a cidade pertence
    # (usado pelo recorte "cidade segue o estado" no cliente)
    cidades_chips = [
        {
            'key': chave,
            'label': cidades_por_chave.get(chave, chave),
            'estados': sorted(cidades_estados.get(chave, set())),
        }
        for chave in cidades_por_chave
    ]
    cidades_chips.sort(key=lambda c: _normalizar_cidade_dashboard(c['label']))

    empresas = query.order_by(Empresa.id).all()

    # Chave canônica de cidade por empresa: agrupa variações ("Imbé"/"IMBE")
    # e alimenta o data-cidade-key de cada card para a contagem client-side.
    cidade_key_por_empresa = {
        emp.id: _normalizar_cidade_dashboard(emp.cidade or '')
        for emp in empresas
    }

    # Pré-computa contadores e status de cada certidão em Python,
    # eliminando dois loops Jinja2 por empresa no template.
    contadores_por_empresa = {}
    status_por_cert = {}
    certidoes_por_empresa = {}
    for empresa in empresas:
        counts = {
            'total': 0, 'validas': 0, 'a_vencer': 0, 'vencidas': 0,
            'pendentes': 0, 'nao_definida': 0,
            'tipo_total': 0, 'tipo_federal': 0, 'tipo_fgts': 0,
            'tipo_estadual': 0, 'tipo_municipal': 0, 'tipo_trabalhista': 0,
            'menor_validade': '9999-12-31',
            'ultima_atualizacao': '',
        }
        for c in empresa.certidoes:
            if c.status_especial == StatusEspecial.PENDENTE:
                sc = 'pendentes'
            elif not c.data_validade:
                sc = 'nao_definida'
            elif c.data_validade < hoje:
                sc = 'vencidas'
            elif (c.data_validade - hoje).days <= limites_por_tipo[c.tipo]:
                sc = 'a_vencer'
            else:
                sc = 'validas'
            status_por_cert[c.id] = sc
            counts['total'] += 1
            counts[sc] += 1
            counts['tipo_total'] += 1
            counts['tipo_' + c.tipo.name.lower()] = counts.get('tipo_' + c.tipo.name.lower(), 0) + 1
            if c.data_validade and sc != 'pendentes':
                dval = c.data_validade.strftime('%Y-%m-%d')
                if dval < counts['menor_validade']:
                    counts['menor_validade'] = dval
            # ultima_atualizacao da empresa = a mais recente entre as certidoes
            # (max ISO). '' inicial < qualquer ISO, entao sem dado fica ''.
            if c.atualizado_em:
                iso = c.atualizado_em.strftime('%Y-%m-%dT%H:%M:%S')
                if iso > counts['ultima_atualizacao']:
                    counts['ultima_atualizacao'] = iso
        contadores_por_empresa[empresa.id] = counts
        certidoes_por_empresa[empresa.id] = sorted(empresa.certidoes, key=lambda c: c.ordem_exibicao)

    municipios = Municipio.query.all()

    urls_municipais = {}
    for m in municipios:
        if not m.url_certidao:
            continue
        nome = (m.nome or '').strip()
        nome_sem = file_manager.remover_acentos(nome)
        url = m.url_certidao

        urls_municipais[nome] = url
        urls_municipais[nome_sem] = url

        if nome_sem.upper() == 'IMBE':
            config_cfg = _carregar_config_municipio(m)
            cfg_geral = (((config_cfg or {}).get('imbe_variantes') or {}).get('geral') or {})
            url_geral = cfg_geral.get('url')
            if url_geral:
                urls_municipais[nome + '_GERAL'] = url_geral
                urls_municipais[nome_sem + '_GERAL'] = url_geral

    return render_template(
        'dashboard.html',
        empresas=empresas,
        contadores_por_empresa=contadores_por_empresa,
        status_por_cert=status_por_cert,
        certidoes_por_empresa=certidoes_por_empresa,
        status_filtros=status_filtros,
        tipo_filtros=tipo_filtros,
        estado_filtros=estado_filtros,
        cidade_filtros=cidade_filtros,
        estados_disponiveis=estados_disponiveis,
        cidades_chips=cidades_chips,
        cidade_key_por_empresa=cidade_key_por_empresa,
        hoje=hoje,
        a_vencer_dias=a_vencer_dias,
        ordem=ordem,
        sites_urls=SITES_CERTIDOES,
        urls_municipais=urls_municipais
    )


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


_LOTE_STATUS_LABEL = {
    'completed': 'Concluído',
    'stopped': 'Interrompido',
    'error': 'Erro',
    'paused': 'Pausado',
}


def _humanizar_desde(dt, agora):
    """Descreve há quanto tempo `dt` ocorreu em relação a `agora` (ambos naive,
    horário local). Retorna string curta pt-BR: "agora", "há 3 h", "ontem",
    "há 5 dias". Usada nos relatórios para datar atividade recente/pendências."""
    if dt is None:
        return '—'
    delta = agora - dt
    segundos = delta.total_seconds()
    if segundos < 0:
        return 'agora'
    dias = delta.days
    if dias == 0:
        horas = int(segundos // 3600)
        if horas < 1:
            minutos = int(segundos // 60)
            return 'agora' if minutos < 1 else f'há {minutos} min'
        return f'há {horas} h'
    if dias == 1:
        return 'ontem'
    if dias < 30:
        return f'há {dias} dias'
    meses = dias // 30
    return 'há 1 mês' if meses == 1 else f'há {meses} meses'


@bp.route('/relatorios')
def relatorios():
    hoje = date.today()
    agora = datetime.now()
    _garantir_snapshot_diario()
    a_vencer_dias = get_a_vencer_dias()
    empresas_total = Empresa.query.count()
    certidoes = Certidao.query.all()

    total_certidoes = len(certidoes)
    pendentes = 0
    vencidas = 0
    a_vencer = 0
    validas = 0
    sem_data = 0

    # distribuição por tipo (ordem canônica do enum) e agrupamentos de pendências
    por_tipo = {t.value: 0 for t in TipoCertidao}
    pendentes_por_tipo = {}
    pendentes_por_cidade = {}
    lista_pendentes = []
    ultimas_emitidas = []

    for certidao in certidoes:
        por_tipo[certidao.tipo.value] += 1
        st = _classificar_status_certidao(certidao, hoje)

        if st == 'pendentes':
            pendentes += 1
            tipo_valor = certidao.tipo.value
            pendentes_por_tipo[tipo_valor] = pendentes_por_tipo.get(tipo_valor, 0) + 1
            cidade = certidao.empresa.cidade or '—'
            pendentes_por_cidade[cidade] = pendentes_por_cidade.get(cidade, 0) + 1
            lista_pendentes.append({
                'empresa': certidao.empresa.nome,
                'cidade': certidao.empresa.cidade,
                'tipo': tipo_valor,
                'subtipo': certidao.subtipo.value if certidao.subtipo else None,
                'desde': _humanizar_desde(certidao.atualizado_em, agora),
                'ordem': certidao.atualizado_em or datetime.min,
            })
            continue

        if st == 'sem_data':
            sem_data += 1
            continue

        if st == 'vencidas':
            vencidas += 1
        elif st == 'a_vencer':
            a_vencer += 1
        else:
            validas += 1

        # certidão efetivamente emitida (tem validade): candidata a "últimas emitidas"
        ultimas_emitidas.append({
            'empresa': certidao.empresa.nome,
            'cidade': certidao.empresa.cidade,
            'tipo': certidao.tipo.value,
            'subtipo': certidao.subtipo.value if certidao.subtipo else None,
            'validade': certidao.data_validade,
            'quando': _humanizar_desde(certidao.atualizado_em, agora),
            'ordem': certidao.atualizado_em or datetime.min,
        })

    # ordena por atividade mais recente e corta o topo
    lista_pendentes.sort(key=lambda x: x['ordem'], reverse=True)
    ultimas_emitidas.sort(key=lambda x: x['ordem'], reverse=True)
    ultimas_emitidas = ultimas_emitidas[:10]

    # distribuição por tipo na ordem canônica do enum
    distribuicao_tipo = [(t.value, por_tipo[t.value]) for t in TipoCertidao]
    # rankings de pendências (maior primeiro)
    pendentes_tipo_rank = sorted(
        pendentes_por_tipo.items(), key=lambda x: x[1], reverse=True)
    pendentes_cidade_rank = sorted(
        pendentes_por_cidade.items(), key=lambda x: x[1], reverse=True)

    distribuicao_status = [
        ('Válidas', validas, 'ok'),
        ('A vencer', a_vencer, 'warn'),
        ('Vencidas', vencidas, 'danger'),
        ('Pendentes', pendentes, 'pend'),
        ('Sem data', sem_data, 'muted'),
    ]

    # último lote iniciado por tipo × escopo (pendentes / geral). iniciado_em é
    # gravado em UTC; comparo com utcnow p/ o "há X" e converto p/ local só ao exibir.
    agora_utc = utcnow_naive()
    tz_offset = agora - agora_utc
    lotes_resumo = []
    for nome_tipo in ('FGTS', 'Estadual RS', 'Municipal Imbé', 'Municipal Tramandaí'):
        linha = {'tipo': nome_tipo, 'pendentes': None, 'geral': None}
        for escopo, chave in (('pendentes', 'pendentes'), ('default', 'geral')):
            reg = (ExecucaoLote.query
                   .filter_by(tipo=nome_tipo, escopo=escopo)
                   .order_by(ExecucaoLote.iniciado_em.desc())
                   .first())
            if reg:
                tem_desfecho = reg.status is not None
                linha[chave] = {
                    'tipo': nome_tipo,
                    'escopo': escopo,
                    'quando': _humanizar_desde(reg.iniciado_em, agora_utc),
                    'data_fmt': (reg.iniciado_em + tz_offset).strftime('%d/%m/%Y %H:%M'),
                    'total': reg.total,
                    'tem_desfecho': tem_desfecho,
                    'status_label': _LOTE_STATUS_LABEL.get(reg.status, '—'),
                    'sucesso': reg.sucesso,
                    'pendentes_resultado': reg.pendentes_resultado,
                    'falhas': reg.falhas,
                    'processados': reg.sucesso + reg.pendentes_resultado + reg.falhas,
                    'finalizado_fmt': (
                        (reg.finalizado_em + tz_offset).strftime('%d/%m/%Y %H:%M')
                        if reg.finalizado_em else None),
                }
        lotes_resumo.append(linha)

    # série de evolução por status (foto diária), agregando os snapshots por dia
    snaps = SnapshotCertidao.query.order_by(SnapshotCertidao.data).all()
    por_data = {}
    for s in snaps:
        dia = por_data.setdefault(s.data, {})
        dia[s.status] = dia.get(s.status, 0) + s.quantidade
    serie_status = []
    for dia in sorted(por_data):
        linha_dia = por_data[dia]
        serie_status.append({
            'label': dia.strftime('%d/%m'),
            'validas': linha_dia.get('validas', 0),
            'a_vencer': linha_dia.get('a_vencer', 0),
            'vencidas': linha_dia.get('vencidas', 0),
            'pendentes': linha_dia.get('pendentes', 0),
            'sem_data': linha_dia.get('sem_data', 0),
        })

    return render_template(
        'relatorios.html',
        empresas_total=empresas_total,
        total_certidoes=total_certidoes,
        pendentes=pendentes,
        vencidas=vencidas,
        a_vencer=a_vencer,
        a_vencer_dias=a_vencer_dias,
        ultimas_emitidas=ultimas_emitidas,
        lista_pendentes=lista_pendentes,
        pendentes_tipo_rank=pendentes_tipo_rank,
        pendentes_cidade_rank=pendentes_cidade_rank,
        distribuicao_status=distribuicao_status,
        distribuicao_tipo=distribuicao_tipo,
        lotes_resumo=lotes_resumo,
        serie_status=serie_status,
    )


_TIPOS_VENCER = [
    ('federal', 'Federal', 'a_vencer_dias_federal'),
    ('fgts', 'FGTS', 'a_vencer_dias_fgts'),
    ('estadual', 'Estadual', 'a_vencer_dias_estadual'),
    ('municipal', 'Municipal', 'a_vencer_dias_municipal'),
    ('trabalhista', 'Trabalhista', 'a_vencer_dias_trabalhista'),
]


@bp.route('/configuracoes', methods=['GET', 'POST'])
@requer_papel('admin')
def configuracoes():
    try:
        config = db.session.get(ConfiguracaoSistema, 1)
    except Exception:
        config = None

    if not config:
        config = ConfiguracaoSistema(id=1, a_vencer_dias=7)
        db.session.add(config)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

    if request.method == 'POST':
        valor_str = (request.form.get('a_vencer_dias') or '').strip()
        try:
            valor = int(valor_str)
        except (TypeError, ValueError):
            flash('Informe um numero inteiro entre 1 e 90.', 'warning')
            return redirect(url_for('main.configuracoes'))

        if not 1 <= valor <= 90:
            flash('O limite de "a vencer" deve ficar entre 1 e 90 dias.', 'warning')
            return redirect(url_for('main.configuracoes'))

        config.a_vencer_dias = valor

        for chave, _label, coluna in _TIPOS_VENCER:
            raw = (request.form.get(f'a_vencer_dias_{chave}') or '').strip()
            if raw == '':
                setattr(config, coluna, None)
            else:
                try:
                    v = int(raw)
                except (TypeError, ValueError):
                    flash(f'Valor invalido para {_label}: use um numero inteiro entre 1 e 90.', 'warning')
                    return redirect(url_for('main.configuracoes'))
                if not 1 <= v <= 90:
                    flash(f'O limite para {_label} deve ficar entre 1 e 90 dias.', 'warning')
                    return redirect(url_for('main.configuracoes'))
                setattr(config, coluna, v)

        caminho_rede_raw = (request.form.get('caminho_rede') or '').strip()
        config.caminho_rede = caminho_rede_raw or None

        # Agendador de emissao proativa (spec 02): hora local 0-23 + liga/desliga.
        # So processa quando o formulario traz a secao do agendador (evita mexer
        # no estado num POST parcial que nao inclui esses campos).
        if 'agendador_hora' in request.form:
            hora_raw = (request.form.get('agendador_hora') or '').strip()
            try:
                hora = int(hora_raw)
            except (TypeError, ValueError):
                flash('Informe uma hora inteira entre 0 e 23 para o agendador.', 'warning')
                return redirect(url_for('main.configuracoes'))
            if not 0 <= hora <= 23:
                flash('O horario do agendador deve ficar entre 0 e 23.', 'warning')
                return redirect(url_for('main.configuracoes'))
            config.agendador_hora = hora
            config.agendador_ativo = _to_bool(request.form.get('agendador_ativo'))

        # Notificacoes por e-mail (spec 03): destinatarios + cadencia do digest.
        # So processa quando o formulario traz a secao (POST parcial nao mexe).
        if 'notif_cadencia' in request.form:
            cadencia = (request.form.get('notif_cadencia') or '').strip().lower()
            if cadencia not in ('semanal', 'diaria'):
                flash('Cadencia de notificacao invalida: use semanal ou diaria.', 'warning')
                return redirect(url_for('main.configuracoes'))
            config.notif_cadencia = cadencia
            destinatarios = (request.form.get('notif_destinatarios') or '').strip()
            config.notif_destinatarios = destinatarios or None

        salvou = False
        try:
            db.session.commit()
            salvou = True
            flash('Configuracoes atualizadas com sucesso.', 'success')
            auditoria.registrar('config.editar')
        except Exception as exc:
            db.session.rollback()
            flash(f'Erro ao salvar configuracoes: {exc}', 'danger')
            auditoria.registrar('config.editar', resultado='erro', detalhe=str(exc))

        # reprograma o scheduler sem reiniciar — best-effort e FORA do try do
        # commit: uma falha aqui não deve reportar "erro ao salvar" (já salvou).
        if salvou:
            try:
                agendador.reprogramar(_current_app_object())
            except Exception as exc:
                log_event('agendador_reprogramar_falhou', level='WARNING', error=str(exc))

        return redirect(url_for('main.configuracoes'))

    a_vencer_dias = config.a_vencer_dias if config else get_a_vencer_dias()
    por_tipo = {
        chave: getattr(config, coluna) if config else None
        for chave, _, coluna in _TIPOS_VENCER
    }
    return render_template(
        'configuracoes.html',
        a_vencer_dias=a_vencer_dias,
        por_tipo=por_tipo,
        tipos_vencer=_TIPOS_VENCER,
        caminho_rede=(config.caminho_rede if config else None) or '',
        caminho_rede_efetivo=file_manager.get_caminho_rede(),
        agendador_ativo=(config.agendador_ativo if config else True),
        agendador_hora=(config.agendador_hora if config else 3),
        notif_destinatarios=(config.notif_destinatarios if config else None) or '',
        notif_cadencia=(config.notif_cadencia if config else 'semanal'),
    )


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



_XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def _slug_arquivo(texto):
    """Nome de arquivo seguro a partir de um texto livre (sem acento/espaco)."""
    base = re.sub(r'[^a-z0-9]+', '-', file_manager.remover_acentos(texto or '').lower()).strip('-')
    return base or 'empresa'


@bp.route('/exportar/carteira.xlsx')
@requer_papel('leitura')
def exportar_carteira():
    """Planilha XLSX da carteira respeitando os filtros ativos do painel
    (status/tipo/estado/cidade replicados server-side)."""
    buffer = export_service.gerar_planilha_carteira(
        status=request.args.getlist('status'),
        tipo=request.args.getlist('tipo'),
        estado=request.args.getlist('estado'),
        cidade=request.args.getlist('cidade'),
    )
    nome = f'carteira-{date.today().strftime("%Y%m%d")}.xlsx'
    return send_file(buffer, mimetype=_XLSX_MIME, as_attachment=True, download_name=nome)


@bp.route('/exportar/dossie/<int:empresa_id>.pdf')
@requer_papel('operador')
def exportar_dossie(empresa_id):
    """Dossie PDF (capa + certidoes validas) de uma empresa. Sem certidoes
    validas, avisa e volta ao painel em vez de baixar um PDF vazio."""
    empresa = Empresa.query.get_or_404(empresa_id)
    buffer, avisos = dossie_service.gerar_dossie(empresa)
    if buffer is None:
        flash(f'Não foi possível gerar o dossiê de {empresa.nome}: {"; ".join(avisos)}.', 'warning')
        return redirect(url_for('main.dashboard'))
    nome = f'dossie-{_slug_arquivo(empresa.nome)}.pdf'
    return send_file(buffer, mimetype='application/pdf', as_attachment=True, download_name=nome)


_PRESETS_PRODUTIVIDADE = (30, 90)


def _dias_produtividade(valor):
    """Periodo (dias) da produtividade: um dos presets, senao 30 (default)."""
    try:
        dias = int(valor)
    except (TypeError, ValueError):
        return 30
    return dias if dias in _PRESETS_PRODUTIVIDADE else 30


@bp.route('/produtividade')
@requer_papel('leitura')
def produtividade():
    """Pagina de produtividade (emissoes/dia, taxa por tipo, tempo medio de lote)
    a partir de ExecucaoLote. Reflete os lotes registrados (FGTS/Estadual/Municipal)."""
    dias = _dias_produtividade(request.args.get('dias'))
    dados = export_service.coletar_produtividade(dias)
    return render_template('produtividade.html', dados=dados, dias=dias,
                           presets=_PRESETS_PRODUTIVIDADE)


@bp.route('/produtividade/exportar.xlsx')
@requer_papel('leitura')
def produtividade_exportar():
    dias = _dias_produtividade(request.args.get('dias'))
    dados = export_service.coletar_produtividade(dias)
    buffer = export_service.gerar_planilha_produtividade(dados)
    nome = f'produtividade-{dias}d-{date.today().strftime("%Y%m%d")}.xlsx'
    return send_file(buffer, mimetype=_XLSX_MIME, as_attachment=True, download_name=nome)


# --- submodulos por dominio (spec 05, REFA-02) ---
# Importados so pelo efeito colateral de registrar rotas no blueprint 'main' (e,
# no caso de lotes, os fluxos do agendador). bp ja esta definido acima. Alias com
# prefixo _mod_ evita colisao de nome com funcoes/variaveis de rota homonimas
# (ex.: a variavel local 'certidoes', as rotas empresas()/relatorios()).
from app.routes import certidoes as _mod_certidoes  # noqa: E402,F401
from app.routes import lotes as _mod_lotes  # noqa: E402,F401

# re-export p/ compat de testes que acessam app.routes._registrar_fluxos_agendador
_registrar_fluxos_agendador = _mod_lotes._registrar_fluxos_agendador  # noqa: E402
