import os

from app import create_app, db
from app.models import Empresa, Certidao

app = create_app()

@app.shell_context_processor
def make_shell_context():
    return {'db': db, 'Empresa': Empresa, 'Certidao': Certidao}

if __name__ == '__main__':
    # debug desligado por padrao (esta ferramenta escreve em disco de rede e no
    # registro do Windows); habilite localmente com FLASK_DEBUG=1 quando precisar.
    debug = os.environ.get('FLASK_DEBUG', '').strip().lower() in {'1', 'true', 'yes', 'on'}
    app.run(debug=debug)
