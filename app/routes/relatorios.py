"""Rotas de relatorios, configuracoes e exportacao (carteira/dossie/produtividade).

Extraido de app/routes.py (spec 05, REFA-02). Registra no blueprint "main"
compartilhado (importado de app.routes).
"""
import re
from datetime import date, datetime

from flask import (
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from app import db, file_manager
from app.models import (
    Certidao,
    ConfiguracaoSistema,
    Empresa,
    ExecucaoLote,
    SnapshotCertidao,
    TipoCertidao,
    get_a_vencer_dias,
)
from app.utils import (
    to_bool as _to_bool,
    utcnow_naive,
)
from app.services import (
    agendador,
    auditoria,
    dossie_service,
    export_service,
)
from app.services.execution_logger import log_event
from app.services.snapshot_service import (
    classificar_status_certidao as _classificar_status_certidao,
    garantir_snapshot_diario as _garantir_snapshot_diario,
)
from app.auth import requer_papel

from app.routes import bp, _current_app_object

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
