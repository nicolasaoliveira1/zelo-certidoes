import os
import sys

from app import create_app, db
from app.models import Empresa, Certidao
from app.services.deps_check import dependencias_faltantes

app = create_app()

@app.shell_context_processor
def make_shell_context():
    return {'db': db, 'Empresa': Empresa, 'Certidao': Certidao}

if __name__ == '__main__':
    # Fail-fast acionavel: nao sobe meio quebrado se faltar dependencia critica.
    faltando = dependencias_faltantes()
    if faltando:
        print('ERRO: dependencias ausentes: ' + ', '.join(faltando))
        print('Rode "iniciar.bat" (ou "pip install -r requirements.txt" no venv ativo) e tente de novo.')
        sys.exit(1)

    # debug desligado por padrao (esta ferramenta escreve em disco de rede e no
    # registro do Windows); habilite localmente com FLASK_DEBUG=1 quando precisar.
    debug = os.environ.get('FLASK_DEBUG', '').strip().lower() in {'1', 'true', 'yes', 'on'}
    app.run(debug=debug)
