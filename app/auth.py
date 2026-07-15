"""Autenticação, autorização e enforcement (spec 01).

Camadas (ver AD-005):
- **Deny-by-default**: `_exigir_login` (before_request global) exige sessão para
  todo endpoint fora de `ENDPOINTS_PUBLICOS`.
- **Autorização**: `requer_papel(minimo)` por rank (leitura<operador<admin).
- Rotas de sessão: `GET/POST /login`, `POST /logout`.
- Error handlers: CSRFError (400 claro), 403.
"""
from datetime import datetime
from functools import wraps

from flask import (
    Blueprint,
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


def _prefere_json():
    """API/mutação → JSON; página GET → HTML (redirect)."""
    if request.method not in ('GET', 'HEAD'):
        return True
    if request.path.startswith('/api/'):
        return True
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return True
    accept = request.accept_mimetypes
    return accept.accept_json and not accept.accept_html


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
    """Só redireciona para caminho local (evita open-redirect)."""
    if prox and prox.startswith('/') and not prox.startswith('//'):
        return prox
    return url_for('main.dashboard')


# --- Painel de auditoria (admin) — AUDIT-02 ---

def _parse_data(texto, *, fim_do_dia=False):
    if not texto:
        return None
    try:
        d = datetime.strptime(texto, '%Y-%m-%d')
    except ValueError:
        return None
    return d.replace(hour=23, minute=59, second=59) if fim_do_dia else d


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
