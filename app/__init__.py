from flask import Flask, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from config import Config
import os
import logging

from app.services.execution_logger import configure_logging, log_event
from app.services.diagnostics import attach_handler, iniciar_persistencia
from app.services.health import run_health_checks

db = SQLAlchemy()
migrate = Migrate()

def create_app(config_class=Config):
    app = Flask(__name__, instance_relative_config=True)

    app.config.from_object(config_class)

    configure_logging(
        app.config.get('LOG_LEVEL', 'INFO'),
        log_dir=app.config.get('LOG_DIR'),
        console_format=app.config.get('LOG_CONSOLE_FORMAT', 'human'),
        json_file=app.config.get('LOG_JSON_FILE', True),
    )

    if app.config.get('QUIET_WERKZEUG_LOGS', True):
        logging.getLogger('werkzeug').setLevel(logging.WARNING)

    # observa o logger estruturado para o painel de diagnostico em memoria
    attach_handler()
    
    try:
        os.makedirs(app.instance_path)
    except OSError:
        pass

    # garante que nenhum resquício de execução anterior bloqueie o monitor federal
    _limpar_chave_interrupcao_federal()
    
    db.init_app(app)
    migrate.init_app(app, db)

    with app.app_context():
        checks = run_health_checks(app.config)
        log_event('startup_health_checks', checks=checks)
    
    # models importado para registrar as tabelas no SQLAlchemy/Migrate (efeito colateral)
    from app import routes, models  # noqa: F401
    app.register_blueprint(routes.bp)

    # persistencia do historico de diagnostico (thread escritora + prune inicial)
    if app.config.get('DIAGNOSTICO_PERSISTIR', True):
        iniciar_persistencia(app, app.config.get('DIAGNOSTICO_RETENCAO_DIAS', 30))

    # versiona estáticos locais com ?v=mtime para o navegador nunca servir CSS/JS desatualizado
    @app.context_processor
    def _injetar_static_versionado():
        def static_versionado(filename):
            caminho = os.path.join(app.static_folder, filename)
            try:
                versao = int(os.path.getmtime(caminho))
            except OSError:
                versao = 0
            return url_for('static', filename=filename, v=versao)
        return {'static_versionado': static_versionado}

    return app


def _limpar_chave_interrupcao_federal():
    # remove o arquivo de monitoramento federal do disco caso haja crash
    caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stop_federal_monitor.txt')
    if os.path.exists(caminho):
        try:
            os.remove(caminho)
            print("[startup] Arquivo stop_federal_monitor.txt removido (resquício de execução anterior).")
        except OSError as e:
            print(f"[startup] Aviso: não foi possível remover stop_federal_monitor.txt: {e}")
