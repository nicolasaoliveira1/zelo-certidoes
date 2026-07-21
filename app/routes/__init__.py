import time
from datetime import date, datetime

from flask import (
    Blueprint,
    current_app,
    g,
    jsonify,
    render_template,
    request,
)

from app import db, file_manager
from app.automation import SITES_CERTIDOES
from app.automation.emissao import _carregar_config_municipio
from app.models import (
    Certidao,
    Empresa,
    Municipio,
    StatusEspecial,
    TipoCertidao,
    get_a_vencer_dias,
)
from app.utils import (
    json_error as _json_error,
    normalizar_cidade,
    to_bool as _to_bool,
)
from app.services import diagnostics
from app.services.correlation import CorrelationContext
from app.services.execution_logger import log_event
from app.services.visualizar_token import _gerar_visualizar_token
from app.services.snapshot_service import (
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


# (ex.: a variavel local 'certidoes', as rotas empresas()/relatorios()).
from app.routes import certidoes as _mod_certidoes  # noqa: E402,F401
from app.routes import empresas as _mod_empresas  # noqa: E402,F401
from app.routes import lotes as _mod_lotes  # noqa: E402,F401
from app.routes import relatorios as _mod_relatorios  # noqa: E402,F401

# re-export p/ compat de testes que acessam app.routes._registrar_fluxos_agendador
_registrar_fluxos_agendador = _mod_lotes._registrar_fluxos_agendador  # noqa: E402
