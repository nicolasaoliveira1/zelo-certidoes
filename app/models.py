import enum
from app import db
from app.utils import utcnow_naive
from datetime import date, datetime, timezone
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash


class TipoCertidao(enum.Enum):
    FEDERAL = "Federal"
    FGTS = "FGTS"
    ESTADUAL = "Estadual"
    MUNICIPAL = "Municipal"
    TRABALHISTA = "Trabalhista"


class SubtipoCertidao(enum.Enum):
    GERAL = "Geral"
    MOBILIARIO = "Mobiliário"


class StatusEspecial(enum.Enum):
    PENDENTE = "Pendente"


class Empresa(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), nullable=False)
    cnpj = db.Column(db.String(18), unique=True, nullable=False)
    estado = db.Column(db.String(2), nullable=False, default='RS')
    cidade = db.Column(db.String(50), nullable=False)
    inscricao_mobiliaria = db.Column(db.String(6), nullable=True)

    certidoes = db.relationship(
        'Certidao', backref='empresa', lazy='selectin', cascade="all, delete-orphan")

    def __repr__(self):
        return f'<Empresa {self.nome}>'


class Certidao(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tipo = db.Column(db.Enum(TipoCertidao), nullable=False)
    subtipo = db.Column(
        db.Enum(
            SubtipoCertidao,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
            name='subtipocertidao'
        ),
        nullable=True
    )

    data_validade = db.Column(db.Date, nullable=True)
    caminho_arquivo = db.Column(db.String(500), nullable=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey(
        'empresa.id'), nullable=False)
    status_especial = db.Column(db.Enum(StatusEspecial), nullable=True)
    # Ultima modificacao (hora local naive). default cobre a criacao/INSERT
    # (onde onupdate nao dispara); onupdate cobre qualquer UPDATE persistido.
    # Alimenta a ordenacao "Ultima atualizacao" do dashboard.
    atualizado_em = db.Column(
        db.DateTime, nullable=True,
        default=datetime.now, onupdate=datetime.now)

    def __repr__(self):
        if self.subtipo:
            return f'<Certidao {self.tipo.value} - {self.subtipo.value} - {self.empresa.nome}>'
        return f'<Certidao {self.tipo.value} - {self.empresa.nome}>'

    @property
    def status(self):
        if self.status_especial == StatusEspecial.PENDENTE:
            return 'vermelho'

        if self.data_validade is None:
            return 'cinza'
        hoje = date.today()
        diferenca_dias = (self.data_validade - hoje).days
        limite_dias = get_a_vencer_dias(tipo=self.tipo)
        if diferenca_dias < 0:
            return 'vermelho'
        elif diferenca_dias <= limite_dias:
            return 'amarelo'
        else:
            return 'verde'

    @property
    def ordem_exibicao(self):
        ordem_tipo = {
            TipoCertidao.FEDERAL: 1,
            TipoCertidao.FGTS: 2,
            TipoCertidao.ESTADUAL: 3,
            TipoCertidao.MUNICIPAL: 4,
            TipoCertidao.TRABALHISTA: 5,
        }
        subtipo_ordem = 0
        if self.tipo == TipoCertidao.MUNICIPAL and self.subtipo:
            if self.subtipo == SubtipoCertidao.GERAL:
                subtipo_ordem = 1
            elif self.subtipo == SubtipoCertidao.MOBILIARIO:
                subtipo_ordem = 2
        return (ordem_tipo.get(self.tipo, 99), subtipo_ordem, self.id or 0)


class Municipio(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), unique=True, nullable=False)
    url_certidao = db.Column(db.String(300), nullable=False)

    automacao_ativa = db.Column(db.Boolean, nullable=False, default=True)
    validade_dias = db.Column(db.Integer, nullable=True)
    usar_slow_typing = db.Column(db.Boolean, nullable=False, default=False)
    config_automacao = db.Column(db.Text, nullable=True)

    cnpj_field_id = db.Column(db.String(100), nullable=True)
    by = db.Column(db.String(20), nullable=True)

    inscricao_field_id = db.Column(db.String(100), nullable=True)
    inscricao_field_by = db.Column(db.String(20), nullable=True)

    pre_fill_click_id = db.Column(db.String(100), nullable=True)
    pre_fill_click_by = db.Column(db.String(20), nullable=True)

    shadow_host_selector = db.Column(db.String(100), nullable=True)
    inner_input_selector = db.Column(db.String(100), nullable=True)

    def __repr__(self):
        return f'<Municipio {self.nome}>'


class EventoDiagnostico(db.Model):
    """Historico persistente de erros/avisos para o painel de diagnostico.
    Sobrevive a reinicios do sistema (o buffer em memoria nao)."""
    __tablename__ = 'evento_diagnostico'

    id = db.Column(db.Integer, primary_key=True)
    criado_em = db.Column(db.DateTime, nullable=False, default=utcnow_naive, index=True)
    evento = db.Column(db.String(80), nullable=False)
    nivel = db.Column(db.String(10), nullable=False, default='ERROR')
    error_type = db.Column(db.String(30), nullable=True)
    alvo = db.Column(db.String(80), nullable=True)
    mensagem = db.Column(db.String(500), nullable=True)
    request_id = db.Column(db.String(40), nullable=True)
    execution_id = db.Column(db.String(40), nullable=True)
    certidao_id = db.Column(db.Integer, nullable=True)
    empresa_id = db.Column(db.Integer, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            # criado_em e gravado em UTC naive (default=utcnow_naive); marca o
            # fuso como UTC ao serializar para que o front (new Date) converta
            # corretamente para o horario local do PC (Brasilia)
            'criado_em': (
                self.criado_em.replace(tzinfo=timezone.utc).isoformat()
                if self.criado_em else None
            ),
            'evento': self.evento,
            'nivel': self.nivel,
            'error_type': self.error_type,
            'alvo': self.alvo,
            'mensagem': self.mensagem,
            'request_id': self.request_id,
            'execution_id': self.execution_id,
            'certidao_id': self.certidao_id,
            'empresa_id': self.empresa_id,
        }

    def __repr__(self):
        return f'<EventoDiagnostico {self.nivel} {self.evento}>'


class ExecucaoLote(db.Model):
    """Registro persistente de cada lote iniciado (FGTS, Estadual RS, Municipal).

    Sobrevive a reinicios do sistema (o batch_state em memoria nao). Alimenta o
    relatorio "quando foi o ultimo lote de X" — evita reprocessar pendentes cedo
    demais e gastar creditos de captcha à toa. Grava-se no INICIO do lote, pois é
    quando os creditos passam a ser consumidos."""
    __tablename__ = 'execucao_lote'

    id = db.Column(db.Integer, primary_key=True)
    # nome do lote conforme cfg['nome_lote']: 'FGTS' | 'Estadual RS' | 'Municipal'
    tipo = db.Column(db.String(30), nullable=False, index=True)
    # escopo do lote: 'pendentes' (reprocessa positivas) | 'default' (vencidas/a vencer)
    escopo = db.Column(db.String(20), nullable=False, default='default')
    total = db.Column(db.Integer, nullable=False, default=0)
    iniciado_em = db.Column(
        db.DateTime, nullable=False, default=utcnow_naive, index=True)
    execution_id = db.Column(db.String(40), nullable=True)

    # desfecho do lote (gravado no fim de run_batch_loop via on_finish). Null
    # enquanto roda / se o lote começou antes deste recurso. status:
    # 'completed' | 'stopped' | 'error' | 'paused'. Para escopo 'pendentes',
    # `sucesso` = pendentes que emitiram (viraram negativa) e `pendentes_resultado`
    # = as que seguiram pendentes.
    finalizado_em = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), nullable=True)
    sucesso = db.Column(db.Integer, nullable=False, default=0)
    pendentes_resultado = db.Column(db.Integer, nullable=False, default=0)
    falhas = db.Column(db.Integer, nullable=False, default=0)

    def __repr__(self):
        return f'<ExecucaoLote {self.tipo}/{self.escopo} {self.iniciado_em}>'


class SnapshotCertidao(db.Model):
    """Foto diária das contagens de certidões por tipo × status, para o gráfico
    de evolução no tempo (ex.: pendentes descendo). Não há histórico reconstruível
    a partir da Certidao (o estado é sobrescrito), então acumulamos uma foto por
    dia. Gravada de forma lazy (sem scheduler) na 1ª visita do dia."""
    __tablename__ = 'snapshot_certidao'

    id = db.Column(db.Integer, primary_key=True)
    data = db.Column(db.Date, nullable=False, index=True)
    tipo = db.Column(db.String(20), nullable=False)  # TipoCertidao.value
    # validas | a_vencer | vencidas | pendentes | sem_data
    status = db.Column(db.String(12), nullable=False)
    quantidade = db.Column(db.Integer, nullable=False, default=0)

    __table_args__ = (
        db.UniqueConstraint('data', 'tipo', 'status',
                            name='uq_snapshot_dia_tipo_status'),
    )

    def __repr__(self):
        return f'<SnapshotCertidao {self.data} {self.tipo}/{self.status}={self.quantidade}>'


class PapelUsuario:
    """Papéis fixos (String, não db.Enum — portabilidade SQLite↔MySQL; ver AD-005).

    Rank: leitura < operador < admin (admin = superusuário)."""
    ADMIN = 'admin'
    OPERADOR = 'operador'
    LEITURA = 'leitura'
    TODOS = (ADMIN, OPERADOR, LEITURA)


class Usuario(db.Model, UserMixin):
    """Credenciais e papel do usuário; integra Flask-Login (AD-007)."""

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    senha_hash = db.Column(db.String(255), nullable=False)
    papel = db.Column(db.String(20), nullable=False, default=PapelUsuario.LEITURA)
    ativo = db.Column(db.Boolean, nullable=False, default=True)
    criado_em = db.Column(db.DateTime, nullable=False, default=utcnow_naive)

    def set_senha(self, senha):
        # werkzeug scrypt por padrão; persiste só o hash (AUTH-02)
        self.senha_hash = generate_password_hash(senha)

    def checar_senha(self, senha):
        return check_password_hash(self.senha_hash, senha)

    @property
    def is_active(self):
        # sobrescreve UserMixin: sessão de usuário desativado é barrada (edge case)
        return self.ativo

    def __repr__(self):
        return f'<Usuario {self.username} ({self.papel})>'


class EventoAuditoria(db.Model):
    """Trilha de ações sensíveis (quem/quando/ação/alvo/IP/resultado).

    Espelha EventoDiagnostico; criado_em em UTC naive, serializado como UTC no
    to_dict (AD-006 — exceção explícita a AD-004: auditoria é registro técnico)."""
    __tablename__ = 'evento_auditoria'

    id = db.Column(db.Integer, primary_key=True)
    criado_em = db.Column(db.DateTime, nullable=False, default=utcnow_naive, index=True)
    usuario_id = db.Column(db.Integer, nullable=True, index=True)
    usuario_nome = db.Column(db.String(80), nullable=True)  # snapshot: sobrevive à remoção
    papel = db.Column(db.String(20), nullable=True)
    acao = db.Column(db.String(80), nullable=False, index=True)
    alvo_tipo = db.Column(db.String(40), nullable=True)
    alvo_id = db.Column(db.Integer, nullable=True)
    ip = db.Column(db.String(45), nullable=True)  # cabe IPv6
    resultado = db.Column(db.String(10), nullable=False, default='ok')  # 'ok' | 'erro'
    detalhe = db.Column(db.String(500), nullable=True)
    request_id = db.Column(db.String(40), nullable=True)

    @property
    def criado_em_iso(self):
        # criado_em em UTC naive; marca tzinfo=UTC para o front (new Date)
        # converter para o horário local do PC (mesmo padrão do EventoDiagnostico)
        return (self.criado_em.replace(tzinfo=timezone.utc).isoformat()
                if self.criado_em else None)

    def to_dict(self):
        return {
            'id': self.id,
            'criado_em': self.criado_em_iso,
            'usuario_id': self.usuario_id,
            'usuario_nome': self.usuario_nome,
            'papel': self.papel,
            'acao': self.acao,
            'alvo_tipo': self.alvo_tipo,
            'alvo_id': self.alvo_id,
            'ip': self.ip,
            'resultado': self.resultado,
            'detalhe': self.detalhe,
            'request_id': self.request_id,
        }

    def __repr__(self):
        return f'<EventoAuditoria {self.acao} {self.resultado}>'


class ConfiguracaoSistema(db.Model):
    __tablename__ = 'configuracao_sistema'

    id = db.Column(db.Integer, primary_key=True)
    a_vencer_dias = db.Column(db.Integer, nullable=False, default=7)
    a_vencer_dias_federal = db.Column(db.Integer, nullable=True)
    a_vencer_dias_fgts = db.Column(db.Integer, nullable=True)
    a_vencer_dias_estadual = db.Column(db.Integer, nullable=True)
    a_vencer_dias_municipal = db.Column(db.Integer, nullable=True)
    a_vencer_dias_trabalhista = db.Column(db.Integer, nullable=True)
    # caminho base da rede onde os PDFs sao organizados; em branco usa env/default
    caminho_rede = db.Column(db.String(500), nullable=True)

    def __repr__(self):
        return f'<ConfiguracaoSistema {self.id}>'


_COLUNA_POR_TIPO = {
    'Federal': 'a_vencer_dias_federal',
    'FGTS': 'a_vencer_dias_fgts',
    'Estadual': 'a_vencer_dias_estadual',
    'Municipal': 'a_vencer_dias_municipal',
    'Trabalhista': 'a_vencer_dias_trabalhista',
}


def _validar_dias(valor_raw):
    try:
        v = int(valor_raw)
    except (TypeError, ValueError):
        return None
    return v if 1 <= v <= 90 else None


def _get_config_cached():
    """Retorna ConfiguracaoSistema do cache flask.g (1 query por request)."""
    try:
        from flask import g
        if not hasattr(g, '_config_sistema'):
            try:
                g._config_sistema = db.session.get(ConfiguracaoSistema, 1)
            except Exception:
                g._config_sistema = None
        return g._config_sistema
    except RuntimeError:
        # Fora de contexto de request (ex: scripts, testes sem request)
        try:
            return db.session.get(ConfiguracaoSistema, 1)
        except Exception:
            return None


def get_a_vencer_dias(tipo=None, default=7):
    try:
        config = _get_config_cached()
    except Exception:
        return default

    if not config:
        return default

    if tipo is not None:
        chave = tipo.value if hasattr(tipo, 'value') else str(tipo)
        coluna = _COLUNA_POR_TIPO.get(chave)
        if coluna:
            valor_tipo = _validar_dias(getattr(config, coluna, None))
            if valor_tipo is not None:
                return valor_tipo

    valor = _validar_dias(config.a_vencer_dias)
    return valor if valor is not None else default
