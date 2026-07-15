"""Autenticação, autorização e enforcement (spec 01).

Camadas (ver AD-005):
- **Deny-by-default**: `_exigir_login` (before_request global) exige sessão para
  todo endpoint fora de `ENDPOINTS_PUBLICOS`.
- **Autorização**: `requer_papel(minimo)` por rank (leitura<operador<admin).
- Rotas de sessão: `GET/POST /login`, `POST /logout`.
- Error handlers: CSRFError (400 claro), 403.
"""
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import (
    LoginManager,
    current_user,
    login_user,
    logout_user,
)
from flask_wtf.csrf import CSRFError

from app import db
from app.models import Usuario, PapelUsuario, EventoAuditoria
from app.services import auditoria, usuario_service
from app.services.correlation import CorrelationContext

bp_auth = Blueprint('auth', __name__)

login_manager = LoginManager()

# Endpoints acessíveis sem autenticação (allowlist curta — o resto é negado).
ENDPOINTS_PUBLICOS = {'auth.login', 'static', 'main.health'}

# Rank dos papéis: admin é superusuário.
_RANK = {PapelUsuario.LEITURA: 1, PapelUsuario.OPERADOR: 2, PapelUsuario.ADMIN: 3}


def init_auth(app):
    """Liga LoginManager, enforcement global e error handlers ao app."""
    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'

    @login_manager.user_loader
    def _carregar_usuario(user_id):
        usuario = db.session.get(Usuario, int(user_id))
        # barra sessão de usuário inexistente ou desativado (edge case)
        if usuario is None or not usuario.ativo:
            return None
        return usuario

    app.before_request(_exigir_login)

    @app.errorhandler(CSRFError)
    def _tratar_csrf(erro):
        msg = 'Sessão do formulário expirou. Recarregue a página e tente de novo.'
        if _prefere_json():
            return _envelope_erro(msg, 400, error_type='csrf'), 400
        flash(msg, 'warning')
        return redirect(request.referrer or url_for('main.dashboard'))


# --- Enforcement / respostas ---

def _exigir_login():
    endpoint = request.endpoint
    if endpoint is None:
        return None  # deixa o 404 seguir
    if endpoint in ENDPOINTS_PUBLICOS or request.path.startswith('/static/'):
        return None
    if current_user.is_authenticated:
        return None
    return _resposta_nao_autenticado()


# POSTs de API que não carregam corpo JSON (fetch sem body) — precisam de resposta JSON
_ROTAS_JSON_POST = {
    '/fgts/emitir_unico',
    '/certidao/salvar_data_confirmada',
    '/certidao/monitorar_download_federal/stop',
}


def _prefere_json():
    """Decide resposta JSON (API/fetch) vs HTML (redirect/flash em página).

    Sem isso, todo POST cairia em JSON e um `<form>` de página com sessão/CSRF
    expirados mostraria um blob JSON em vez de redirecionar ao login/flash."""
    if request.path.startswith('/api/'):
        return True
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json:
        return True
    if request.method in ('GET', 'HEAD'):
        accept = request.accept_mimetypes
        return accept.accept_json and not accept.accept_html
    # POST/PUT/…: JSON só para endpoints de API (fetch); senão é form de página.
    # '_json' é substring pois esses endpoints têm o id depois (.../_json/<id>).
    p = request.path
    return '_json' in p or '/lote/' in p or p in _ROTAS_JSON_POST


def _envelope_erro(mensagem, code, **extra):
    payload = {
        'status': 'error',
        'message': mensagem,
        'mensagem': mensagem,
        'codigo': code,
        'request_id': CorrelationContext.get_request_id(),
    }
    payload.update(extra)
    return jsonify(payload)


def _resposta_nao_autenticado():
    if _prefere_json():
        return _envelope_erro('Sessão expirada. Faça login novamente.', 401,
                              error_type='unauthenticated'), 401
    return redirect(url_for('auth.login', next=request.full_path))


def _resposta_forbidden():
    if _prefere_json():
        return _envelope_erro('Permissão insuficiente para esta ação.', 403,
                              error_type='forbidden'), 403
    return render_template('403.html'), 403


# --- Autorização ---

def requer_papel(minimo):
    """Exige papel >= minimo (rank). Admin passa em tudo. Uso: @requer_papel('operador')."""
    minimo_rank = _RANK[minimo]

    def deco(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                return _resposta_nao_autenticado()
            if _RANK.get(current_user.papel, 0) < minimo_rank:
                return _resposta_forbidden()
            return f(*args, **kwargs)
        return wrapper
    return deco


# --- Rotas de sessão ---

@bp_auth.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        senha = request.form.get('senha') or ''
        usuario = usuario_service.autenticar(username, senha)
        if usuario is None:
            # mensagem genérica — não revela se o usuário existe (AUTH-01.3)
            auditoria.registrar('login', resultado='erro', detalhe=f'usuario={username[:60]}')
            flash('Usuário ou senha inválidos.', 'danger')
            return render_template('login.html'), 401
        login_user(usuario)
        auditoria.registrar('login', resultado='ok')
        return redirect(_destino_seguro(request.args.get('next') or request.form.get('next')))
    return render_template('login.html')


@bp_auth.route('/logout', methods=['POST'])
def logout():
    auditoria.registrar('logout')  # antes de limpar current_user
    logout_user()
    flash('Sessão encerrada.', 'info')
    return redirect(url_for('auth.login'))


def _destino_seguro(prox):
    """Só redireciona para caminho local (evita open-redirect).

    Rejeita `//host` e também `/\\host`: o navegador normaliza `\\`→`/`, então
    `/\\evil.com` viraria o protocolo-relativo `//evil.com`."""
    if prox and prox.startswith('/') and not prox.startswith('//') and '\\' not in prox:
        return prox
    return url_for('main.dashboard')


# --- Painel de auditoria (admin) — AUDIT-02 ---

def _parse_data(texto, *, fim_do_dia=False):
    """Converte a data local do filtro para UTC naive (criado_em é UTC — AD-006),
    senão a janela ficaria deslocada ~3h e eventos cairiam no dia errado."""
    if not texto:
        return None
    try:
        d = datetime.strptime(texto, '%Y-%m-%d')
    except ValueError:
        return None
    if fim_do_dia:
        d = d.replace(hour=23, minute=59, second=59)
    offset = datetime.now().astimezone().utcoffset() or timedelta(0)
    return d - offset


@bp_auth.route('/admin/auditoria')
@requer_papel('admin')
def auditoria_painel():
    usuario_id = request.args.get('usuario_id', type=int)
    acao = request.args.get('acao') or None
    inicio_str = request.args.get('inicio') or ''
    fim_str = request.args.get('fim') or ''
    eventos = auditoria.consultar(
        usuario_id=usuario_id,
        acao=acao,
        inicio=_parse_data(inicio_str),
        fim=_parse_data(fim_str, fim_do_dia=True),
        limite=300,
    )
    usuarios = Usuario.query.order_by(Usuario.username).all()
    acoes = [r[0] for r in db.session.query(EventoAuditoria.acao)
             .distinct().order_by(EventoAuditoria.acao).all()]
    return render_template(
        'auditoria.html',
        eventos=eventos,
        usuarios=usuarios,
        acoes=acoes,
        filtro={'usuario_id': usuario_id, 'acao': acao or '',
                'inicio': inicio_str, 'fim': fim_str},
    )


# --- Gestão de usuários (admin) — AUTH-03 / AUDIT-01 ---

@bp_auth.route('/admin/usuarios')
@requer_papel('admin')
def usuarios_painel():
    usuarios = Usuario.query.order_by(Usuario.username).all()
    return render_template('usuarios.html', usuarios=usuarios, papeis=PapelUsuario.TODOS)


@bp_auth.route('/admin/usuarios/criar', methods=['POST'])
@requer_papel('admin')
def usuario_criar():
    username = (request.form.get('username') or '').strip()
    senha = request.form.get('senha') or ''
    papel = request.form.get('papel') or PapelUsuario.LEITURA
    if not username or not senha:
        flash('Username e senha são obrigatórios.', 'warning')
        return redirect(url_for('auth.usuarios_painel'))
    try:
        novo = usuario_service.criar_usuario(username, senha, papel)
        auditoria.registrar('usuario.criar', alvo_tipo='usuario', alvo_id=novo.id,
                            detalhe=f'{username}/{papel}')
        flash(f'Usuário "{username}" criado.', 'success')
    except ValueError as e:
        auditoria.registrar('usuario.criar', resultado='erro', detalhe=str(e))
        flash(str(e), 'danger')
    return redirect(url_for('auth.usuarios_painel'))


def _usuario_ou_404(usuario_id):
    usuario = db.session.get(Usuario, usuario_id)
    if usuario is None:
        abort(404)
    return usuario


@bp_auth.route('/admin/usuarios/<int:usuario_id>/ativar', methods=['POST'])
@requer_papel('admin')
def usuario_ativar(usuario_id):
    usuario_service.definir_ativo(_usuario_ou_404(usuario_id), True)
    auditoria.registrar('usuario.ativar', alvo_tipo='usuario', alvo_id=usuario_id)
    flash('Usuário ativado.', 'success')
    return redirect(url_for('auth.usuarios_painel'))


@bp_auth.route('/admin/usuarios/<int:usuario_id>/desativar', methods=['POST'])
@requer_papel('admin')
def usuario_desativar(usuario_id):
    usuario = _usuario_ou_404(usuario_id)
    try:
        usuario_service.definir_ativo(usuario, False)
        auditoria.registrar('usuario.desativar', alvo_tipo='usuario', alvo_id=usuario_id)
        flash('Usuário desativado.', 'success')
    except usuario_service.UltimoAdminError as e:
        auditoria.registrar('usuario.desativar', alvo_tipo='usuario', alvo_id=usuario_id,
                            resultado='erro', detalhe=str(e))
        flash(str(e), 'danger')
    return redirect(url_for('auth.usuarios_painel'))


@bp_auth.route('/admin/usuarios/<int:usuario_id>/papel', methods=['POST'])
@requer_papel('admin')
def usuario_papel(usuario_id):
    usuario = _usuario_ou_404(usuario_id)
    papel = request.form.get('papel') or ''
    try:
        usuario_service.definir_papel(usuario, papel)
        auditoria.registrar('usuario.papel', alvo_tipo='usuario', alvo_id=usuario_id, detalhe=papel)
        flash('Papel atualizado.', 'success')
    except (ValueError, usuario_service.UltimoAdminError) as e:
        auditoria.registrar('usuario.papel', alvo_tipo='usuario', alvo_id=usuario_id,
                            resultado='erro', detalhe=str(e))
        flash(str(e), 'danger')
    return redirect(url_for('auth.usuarios_painel'))


@bp_auth.route('/admin/usuarios/<int:usuario_id>/resetar-senha', methods=['POST'])
@requer_papel('admin')
def usuario_resetar_senha(usuario_id):
    usuario = _usuario_ou_404(usuario_id)
    nova = request.form.get('senha') or ''
    if not nova:
        flash('Informe a nova senha.', 'warning')
        return redirect(url_for('auth.usuarios_painel'))
    usuario_service.resetar_senha(usuario, nova)
    auditoria.registrar('usuario.resetar_senha', alvo_tipo='usuario', alvo_id=usuario_id)
    flash('Senha redefinida.', 'success')
    return redirect(url_for('auth.usuarios_painel'))
