@echo off
REM Inicia o sistema de certidoes: garante o venv e as dependencias e sobe o app.
REM Idempotente: quando tudo ja esta instalado, o pip install e rapido.
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo ERRO: venv nao encontrado. Crie com: python -m venv venv
    pause
    exit /b 1
)

call "venv\Scripts\activate.bat"

echo Verificando dependencias...
python -m pip install -r requirements.txt

echo Iniciando o app...
python run.py

pause
endlocal
