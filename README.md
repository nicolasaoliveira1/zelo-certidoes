# Zelo — Controle de Certidões Fiscais

> Regularidade sob controle.

Aplicação web em Python/Flask para centralizar, gerenciar e apoiar a emissão de certidões fiscais (Federal, FGTS, Estadual, Municipal e Trabalhista). **Zelo** é o nome do sistema de uso interno do escritório; a identidade é monocromática (grafite sobre papel), reservando cor apenas para o status das certidões.

![Dashboard](docs/image.png)

O foco do projeto é reduzir trabalho manual no escritório contábil, manter controle visual de vencimentos, organizar automaticamente os PDFs emitidos e apoiar o controle de débitos das empresas, já que uma certidão pendente normalmente sinaliza pendência ou débito na respectiva esfera fiscal/trabalhista.

## Visão geral

O sistema combina:

- Dashboard único com status das certidões por empresa.
- Automação via Selenium para acelerar a emissão.
- Controle de arquivos (download, movimentação e visualização).
- Fluxos em lote para cenários de alto volume (FGTS, Estadual RS e Municipal — Imbé/Tramandaí).

## Tecnologias

### Backend

- Python 3.10+
- Flask
- Flask-SQLAlchemy / SQLAlchemy
- Flask-Migrate / Alembic
- PyMySQL

### Automação

- Selenium WebDriver
- webdriver-manager
- 2captcha-python (ALTCHA no lote Estadual RS)
- pdfplumber (leitura de PDF quando aplicável)

### Frontend

- Templates Jinja2
- Bootstrap 5.3 com identidade própria (**Zelo**: design tokens, IBM Plex, dark/light)
- JavaScript Vanilla (Fetch API)

### Banco de dados

- MySQL (produção)
- SQLite (desenvolvimento)

## Diferenciais técnicos

- Visão operacional centralizada: dashboard único para empresas e certidões, com status visual e filtros em tempo real.
- Arquitetura orientada a manutenção: motor compartilhado de lotes e serviços dedicados para reduzir duplicação e facilitar evolução.
- Automação híbrida pragmática: Selenium local para cenários reais de portais públicos, com fluxos assistidos e automáticos.
- Gestão de arquivos ponta a ponta: detecção de download, estabilização, movimentação/renomeação e vínculo do PDF ao registro no banco.
- Segurança aplicada ao uso diário: visualização de PDF por token assinado e controle explícito de configurações sensíveis via ambiente.
- Fluxos críticos robustos no RS/FGTS: lote com pausa/retomada/parada, polling de progresso, resumo final e fail-fast para erro de chave do solver.

## Principais funcionalidades

### Dashboard e operação

- Cadastro de empresa com criação automática de 5 certidões.
- Filtros por status e busca por nome em tempo real.
- Status visual de certidões (a única cor da interface):
  - Verde: válida
  - Âmbar: a vencer (limite configurável, global ou por tipo de certidão)
  - Vermelho: vencida
  - Laranja-tijolo: pendente (provável débito) — tom próprio, distinto de vencida
  - Cinza: sem data definida
- Cadastro de nova empresa com seleção de cidade via dropdown (apenas municípios cadastrados) e inscrição mobiliária condicional (Imbé).
- Tela de Empresas com listagem, filtros, edição e remoção com confirmação.
- Sidebar responsiva com estado persistente.
- Tema claro/escuro com persistência local.

### Automação de emissão

- Federal: fluxo assistido com monitoramento de download.
- FGTS:
  - Emissão individual com geração de PDF via Chrome DevTools.
  - Emissão em lote com pausa, retomada, parada e resumo final.
  - Detecção de PDF positiva no lote: arquivo removido e certidão marcada como PENDENTE automaticamente.
- Estadual RS:
  - Unitário mantido manual para evitar consumo indevido de solver.
  - Lote com ALTCHA automático via API 2captcha.
  - Processo robusto: só avança para o próximo CNPJ após baixar, estabilizar, mover e classificar o arquivo.
- Municipal: automação orientada por configuração da tabela Município.
  - Tramandaí: fluxo condicional com detecção de link NEGATIVA na página final; suporte a lote.
  - Gravataí: classificação de status via conteúdo do PDF (positiva/negativa), com tratamento automático de pendência quando positiva.
  - Imbé: resolução automática de captcha de imagem via 2captcha; emissão de geral e mobiliário separadamente; suporte a lote por subtipo.

### Gestão de arquivos

- Detecta PDF novo/alterado na pasta Downloads.
- Move e renomeia para a pasta final da empresa.
- Salva caminho do arquivo no banco.
- Visualização de PDF com token assinado e expirável.
- Download automático no Chrome (incluindo fluxos em modo anônimo), reduzindo necessidade de interação manual no diálogo de salvar.

### Observabilidade e diagnóstico

- Logs com **saída dupla**: console legível para humano (hora, nível, domínio, evento, campos-chave e `req_id`, com cor por nível) e arquivo `logs/app.jsonl` rotativo com o JSON cru — ideal para enviar à IA.
- `request_id` por requisição HTTP e `execution_id` por execução de lote.
- Taxonomia de erros (`TIMEOUT`, `CAPTCHA`, `PORTAL`, `SELECTOR`, `NETWORK_PATH`, `PERMISSION`, `DB`, `UNKNOWN`) traduzida em **mensagens acionáveis** (título + causa + ação) que chegam ao usuário no toast e carregam `error_type`/`acao` no JSON.
- **Pré-checagens (preflight)** antes de emitir/lote: valida rede, perfil do Chrome e solver, falhando cedo com mensagem clara em vez de quebrar no meio do Selenium.
- **Detector de padrões recorrentes**: o mesmo erro repetido no mesmo alvo abre um alerta com hipótese (provável seletor quebrado/portal fora).
- **Painel de diagnóstico** em `GET /diagnostico`: lista os últimos erros/avisos (histórico persistido em banco, sobrevive a restart) e os alertas de recorrência.
- Retry com limite e backoff em pontos recuperáveis (ex.: timeout de carregamento e leitura de caminho de rede).
- Endpoint de health check em `GET /health`.

## Requisitos

- Python 3.10+
- Google Chrome
- MySQL (recomendado para produção) ou SQLite (desenvolvimento)

## Instalação

1. Clone o repositório:

```powershell
git clone https://github.com/nicolasaoliveira1/CertidoesPython.git
cd CertidoesPython
```

2. Crie e ative o ambiente virtual:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

3. Instale as dependências:

```powershell
pip install -r requirements.txt
```

4. Copie `.env.example` para `.env` e ajuste os valores:

```env
# Obrigatória
SECRET_KEY=uma_chave_segura

# Banco (escolha um)
# DATABASE_URL=mysql+pymysql://usuario:senha@host/banco
# DATABASE_URL=sqlite:///instance/database.db

# Caminho de rede (opcional; também configurável na tela de Configurações,
# que tem precedência sobre esta variável)
# CAMINHO_REDE=Z:\\PASTAS EMPRESAS

# Perfil do Chrome (opcional)
# CHROME_PROFILE_DIR=C:\CertidoesPython\chrome-profile
# CHROME_PROFILE_NAME=Certidoes

# Certificado Estadual RS (opcional)
# RS_CERT_AUTOSELECT_ENABLED=true
# RS_CERT_AUTOSELECT_PATTERN=https://www.sefaz.rs.gov.br
# RS_CERT_AUTOSELECT_POLICY_INDEX=1
# RS_CERT_AUTOSELECT_ISSUER_CN=AC emissora
# RS_CERT_AUTOSELECT_SUBJECT_CN=Titular CPF

# ALTCHA RS em lote (opcional)
# RS_ALTCHA_AUTOSOLVE_ENABLED=true
# RS_ALTCHA_MANUAL_FALLBACK=true
# CAPTCHA_2_API_KEY=sua_chave
# CAPTCHA_2_DEFAULT_TIMEOUT=180
# CAPTCHA_2_POLLING_INTERVAL=10
# CAPTCHA_2_SERVER=2captcha.com

# Captura de contexto na falha Selenium (screenshot + HTML em logs/selenium)
# SELENIUM_CAPTURE_ENABLED=true
# SELENIUM_CAPTURE_DIR=logs/selenium
# SELENIUM_CAPTURE_RETENCAO_DIAS=14
```

5. Rode migrations e inicie a aplicação:

```powershell
flask db upgrade
python run.py
```

Acesso local: http://localhost:5000

## Como usar

1. Acesse a tela de nova empresa em `/empresa/nova`.
2. Cadastre empresa com CNPJ, cidade e estado.
3. No dashboard:
   - use Emitir para automações suportadas,
   - use Abrir Site quando o fluxo for assistido,
   - use Visualizar para abrir PDF salvo.
4. Acesse `/empresas` para gerenciar cadastro, edição e remoção com confirmação.
5. Para lotes:
   - FGTS: fluxo de lote quando houver mais de 1 item elegível.
   - Estadual RS: lote com controles de pausar, retomar e parar.
   - Municipal (Imbé e Tramandaí): lote com as mesmas ações; resolve captcha de imagem via 2captcha no Imbé.

## Configurações importantes

### Caminho de rede para salvar certidões

O caminho base onde os PDFs das empresas são organizados pode ser definido de duas formas (nesta ordem de precedência): pela tela de **Configurações** (campo "Caminho de rede", salvo no banco) ou pela variável de ambiente `CAMINHO_REDE`. Sem nenhum dos dois, usa o padrão `Z:\PASTAS EMPRESAS`.

### Captura de contexto na falha Selenium

Quando uma automação Selenium quebra (tipicamente porque um portal mudou de estrutura), o sistema salva automaticamente um screenshot e o HTML da página em `logs/selenium/` para acelerar o diagnóstico. Controlado por `SELENIUM_CAPTURE_ENABLED` (padrão ligado), com limpeza por retenção (`SELENIUM_CAPTURE_RETENCAO_DIAS`, padrão 14 dias).

### Estadual RS e 2captcha

- A integração usa API backend, sem extensão no Chrome.
- Se a chave estiver inválida, o lote RS encerra com erro explícito para evitar tentativas improdutivas.
- Se alterar variáveis no `.env`, reinicie a aplicação.

### Logs e health check

- Os eventos de observabilidade aparecem no mesmo terminal em que o Flask está rodando, em formato legível.
- O JSON completo de cada evento é gravado em `logs/app.jsonl` (rotativo) — copie de lá para enviar à IA.
- Para ajustar verbosidade/saída, use `LOG_LEVEL`, `QUIET_WERKZEUG_LOGS`, `LOG_CONSOLE_FORMAT` (`human`/`json`) e `LOG_JSON_FILE` no `.env`.
- O painel `GET /diagnostico` mostra erros/avisos e alertas de recorrência. O histórico é persistido em banco (`DIAGNOSTICO_PERSISTIR`, retenção via `DIAGNOSTICO_RETENCAO_DIAS`); requer `flask db upgrade` para criar a tabela.
- O endpoint `GET /health` retorna `ok` ou `degraded` com detalhes de:
  - banco de dados,
  - caminho de rede,
  - profile do Chrome,
  - configuração do solver.
- As respostas HTTP incluem o header `X-Request-Id` para correlacionar logs e requisições.
- O check de caminho de rede também informa leitura e escrita (útil para diagnosticar permissões).
- Para reduzir ruído local, logs HTTP de estáticos/polling são filtrados e o log padrão fica em nível `WARNING`.

### Limite de "a vencer"

Na tela de Configurações, é possível ajustar o limite de dias para uma certidão ficar "a vencer" (1 a 90 dias). Há um valor **padrão** (aplicado a todos os tipos) e limites **opcionais por tipo** (Federal, FGTS, Estadual, Municipal e Trabalhista) que sobrepõem o padrão quando preenchidos. O limite efetivo afeta dashboard, relatórios e lotes.

### Municípios

As automações municipais dependem da configuração de seletores e steps na tabela Município. Para novas cidades, é necessário mapear o portal e registrar a configuração correspondente.

## Estrutura do projeto

```text
.
  .env
  config.py
  run.py
  requirements.txt
  README.md
  docs/
    context.json
    MAPEAMENTO_MUNICIPIOS.md
  migrations/
    alembic.ini
    env.py
    versions/
  instance/
app/
  __init__.py              # Inicialização Flask (factory create_app)
  routes.py                # Rotas e fluxos de negócio (blueprint 'main')
  models.py                # Modelos do banco
  captcha_solver.py        # Integração 2captcha (ALTCHA e captcha de imagem)
  file_manager.py          # Detecção/movimentação de PDFs
  errors.py                # Taxonomia de erros + descrever_erro (mensagens acionáveis)
  utils.py                 # Utilitários compartilhados (to_bool, get_config_value)
  automation/              # Pacote de automação (antes automation.py)
    __init__.py            #   reexporta SITES_CERTIDOES, VALIDADES_CERTIDOES
    sites.py               #   URLs, seletores e validades padrão
    driver.py              #   WebDriver Chrome + auto-seleção de certificado RS
    steps.py               #   Steps municipais data-driven + mapa de localizadores
    pdf.py                 #   Leitura/classificação de PDF
    emissao.py             #   Emissão por tipo (FGTS/Estadual RS/Municipal)
    batch_state.py         #   Estado e locks compartilhados dos lotes
  # stop_federal_monitor.txt é criado/removido em runtime (não versionado)
  services/
    batch_engine.py        # Motor compartilhado de lotes
    certidao_service.py    # Operações de domínio sobre Certidão (validade/pendente)
    correlation.py         # Contexto de correlação (request_id/execution_id)
    execution_logger.py    # Logger estruturado: console legível + app.jsonl
    diagnostics.py         # Buffer/recorrência em memória + histórico persistido
    preflight.py           # Pré-checagens (rede/Chrome/solver) antes de emitir
    health.py              # Health checks de dependências
    retry.py               # Retry com backoff/jitter
    rs_altcha.py           # Resolver/injetar ALTCHA no RS
  static/
    css/
    images/
  templates/
    base.html
    dashboard.html
    nova_empresa.html
    empresas.html
    empresa_detalhe.html
    empresa_remover_confirm.html
    relatorios.html
    configuracoes.html
    diagnostico.html
```

## Limitações atuais

- Automações dependem da estabilidade dos portais públicos.
- Mudanças de HTML nos sites podem exigir ajuste de seletores.
- Captchas fora do lote RS e Municipal (Imbé) continuam majoritariamente manuais.
- Ainda não existe cobertura completa de testes automatizados para fluxos Selenium.

## Licença

Software proprietário — **todos os direitos reservados** (veja [LICENSE](LICENSE)).

O repositório é público apenas para fins de **estudo, demonstração e portfólio**: o código pode ser visualizado e lido, mas **não** há permissão para usar, executar, copiar, modificar ou redistribuir. Não é um projeto open-source. Para qualquer uso além da visualização, é necessária autorização prévia e por escrito do autor.
