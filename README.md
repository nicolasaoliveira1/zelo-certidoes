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
- Flask-Login / Flask-WTF (autenticação de sessão e CSRF)
- APScheduler (agendador in-process da emissão proativa)
- smtplib (envio de e-mail de notificações; biblioteca padrão)
- openpyxl / pypdf / fpdf2 (exportação: planilha XLSX e dossiê PDF)

### Automação

- Selenium WebDriver
- webdriver-manager
- undetected-chromedriver (anti-bloqueio dos portais municipais IPM Atende.Net)
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
- Segurança aplicada ao uso diário: login obrigatório com papéis (leitura/operador/admin) e negação por padrão, proteção CSRF, trilha de auditoria, visualização de PDF por token assinado e credenciais sensíveis só via ambiente.
- Proatividade: agendador diário que emite o que está vencendo e avisa por e-mail (digest de vencimentos e alertas de falha/saldo), sem depender de serviço externo.
- Fluxos críticos robustos no RS/FGTS: lote com pausa/retomada/parada, polling de progresso, resumo final e fail-fast para erro de chave do solver.

## Principais funcionalidades

### Acesso e segurança (login e papéis)

- Login por sessão: **nenhuma tela funciona sem estar autenticado** (negação por padrão; só login, health e estáticos são públicos).
- Três papéis com hierarquia — **leitura** < **operador** < **admin**: leitura só consulta e exporta; operador emite/edita; admin gerencia usuários e vê a auditoria.
- Proteção CSRF em todos os formulários; senhas com hash forte.
- **Trilha de auditoria**: ações sensíveis (login, criação/alteração de usuário, etc.) ficam registradas e são consultáveis em `/admin/auditoria` (admin).
- Gestão de usuários em `/admin/usuarios` (admin). O primeiro administrador é criado por linha de comando (não há auto-cadastro público).

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
  - Portais IPM Atende.Net (Gravataí, Osório, Novo Hamburgo): a emissão individual usa **undetected-chromedriver** com um perfil persistente dedicado para não ser bloqueada pelo score anti-bot do portal (tela "validação automática de segurança / baixa pontuação"). A detecção é automática pela URL (`*.atende.net`) — qualquer novo município com esse domínio entra no fluxo sem mudança de código. O captcha em si continua resolvido manualmente pelo operador. Falhas de pré-condição (driver indisponível ou perfil em uso) retornam mensagem acionável (HTTP 409) sem cair para o navegador comum.

### Gestão de arquivos

- Detecta PDF novo/alterado na pasta Downloads.
- Move e renomeia para a pasta final da empresa.
- Salva caminho do arquivo no banco.
- Visualização de PDF com token assinado e expirável.
- Download automático no Chrome (incluindo fluxos em modo anônimo), reduzindo necessidade de interação manual no diálogo de salvar.

### Emissão proativa (agendador)

- Agendador embutido (sem serviço externo) que roda **uma vez por dia**, na hora configurada no painel.
- Todo dia tira uma "foto" das contagens (para o gráfico de evolução) e, quando ligado, **enfileira e emite automaticamente** as certidões vencidas/a vencer, reaproveitando os mesmos lotes da operação manual.
- Fila durável: o que ficou pendente sobrevive a reinício do sistema e pode ser retentado por item.
- Liga/desliga e hora ficam na tela de **Configurações**.

### Notificações por e-mail

- **Digest periódico** (semanal por padrão, ou diário) com o resumo da carteira: quantas a vencer, vencidas e pendentes.
- **Alertas** de falha recorrente de automação e de **saldo baixo do 2captcha** antes de um lote parar no meio.
- Anti-spam durável (não repete o mesmo alerta dentro da janela, mesmo após reiniciar) e envio que nunca derruba a automação se o e-mail falhar.
- Destinatários e cadência configuráveis no painel; credenciais SMTP só por variável de ambiente.

### Exportação e relatórios

- **Exportar carteira (Excel):** botão no dashboard baixa uma planilha `.xlsx` **respeitando os filtros ativos** (status, tipo, estado, cidade) — sai exatamente o que está na tela.
- **Dossiê (PDF) por empresa:** um único PDF com capa + as certidões **válidas** concatenadas, pronto para licitação/cliente (papel operador). PDF ausente/corrompido é pulado com aviso.
- **Produtividade:** página `/produtividade` com emissões/dia, taxa de sucesso por tipo e tempo médio de lote (30/90 dias), com exportação em Excel.

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
git clone https://github.com/nicolasaoliveira1/zelo-certidoes.git
cd zelo-certidoes
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

# Perfil dedicado dos municípios IPM Atende.Net (undetected-chromedriver, opcional)
# CHROME_PROFILE_MUNICIPAL_DIR=C:\CertidoesPython\chrome-profile-municipal
# Força o major do Chrome para o undetected-chromedriver (opcional; por padrão é
# detectado automaticamente do Chrome instalado). Use se a auto-detecção falhar.
# CHROME_UC_VERSION_MAIN=149

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
# CAPTCHA_2_SALDO_MINIMO=2.0

# Agendador da emissão proativa (opcional; liga/desliga e hora também no painel)
# AGENDADOR_ENABLED=true

# Notificações por e-mail (opcional; sem SMTP_HOST/SMTP_FROM o envio é ignorado com aviso)
# SMTP_HOST=smtp.seuprovedor.com
# SMTP_PORT=587
# SMTP_USER=usuario
# SMTP_PASSWORD=senha
# SMTP_FROM=certidoes@seuescritorio.com
# SMTP_USE_TLS=true
# SMTP_TIMEOUT=20
# NOTIF_DIGEST_ENVIAR_VAZIO=true
# NOTIF_ALERTA_JANELA_HORAS=24

# Captura de contexto na falha Selenium (screenshot + HTML em logs/selenium)
# SELENIUM_CAPTURE_ENABLED=true
# SELENIUM_CAPTURE_DIR=logs/selenium
# SELENIUM_CAPTURE_RETENCAO_DIAS=14
```

5. Rode as migrations:

```powershell
flask db upgrade
```

6. Crie o primeiro administrador (só é possível criar um admin por CLI; a senha é solicitada de forma interativa):

```powershell
flask criar-admin --username chefe
```

> Depois, novos usuários podem ser criados pela CLI (`flask criar-usuario --username ana --papel operador`) ou pela tela `/admin/usuarios`.

7. Inicie a aplicação:

```powershell
python run.py
```

Acesso local: http://localhost:5000 (faça login com o admin criado acima)

> **Atalho no Windows:** dê um duplo clique em `iniciar.bat` na pasta do projeto. Ele ativa o `venv`, garante as dependências (`pip install -r requirements.txt`, idempotente) e sobe o app. Se faltar alguma dependência crítica (ex.: `undetected-chromedriver`), o `run.py` aborta o boot com uma mensagem clara em vez de subir quebrado.

### Rodar com Docker (dev/reprodutibilidade)

Ambiente de desenvolvimento reprodutível com **app + MySQL** em um comando, independente do Windows/`iniciar.bat`. A automação Selenium/Chrome **não** roda no container (fica no host, com certificado e unidade de rede `Z:`); o compose serve a UI e os dados sobre um MySQL igual ao de produção (8.0, `utf8mb4`/`utf8mb4_0900_ai_ci`).

```bash
cp .env.docker.example .env.docker   # ajuste SECRET_KEY / senha de dev (sem segredo real versionado)
docker compose --env-file .env.docker up
```

- `db`: MySQL 8.0 com volume nomeado `mysql_data` (dados persistem entre `up`/`down`).
- `web`: build do `Dockerfile` (`python:3.12-slim`); o schema é criado pelas **migrations** no boot (`AUTO_DB_UPGRADE=1`), não por `create_all`. App em http://localhost:5000.
- O `.env.docker` real é ignorado pelo git; só o `.env.docker.example` é versionado.

## Como usar

1. Faça login com um usuário existente (o primeiro admin é criado por `flask criar-admin`). Sem sessão, todas as páginas redirecionam para o login.
2. Acesse a tela de nova empresa em `/empresa/nova`.
3. Cadastre empresa com CNPJ, cidade e estado.
4. No dashboard:
   - use Emitir para automações suportadas,
   - use Abrir Site quando o fluxo for assistido,
   - use Visualizar para abrir PDF salvo.
5. Acesse `/empresas` para gerenciar cadastro, edição e remoção com confirmação.
6. Para lotes:
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

Portais **IPM Atende.Net** (URL `*.atende.net`, como Gravataí/Osório/Novo Hamburgo) são roteados automaticamente para o `undetected-chromedriver` com perfil persistente próprio (`CHROME_PROFILE_MUNICIPAL_DIR`, padrão `chrome-profile-municipal/`, isolado do perfil do RS/Federal). No primeiro acesso com o perfil "frio", o bloqueio do portal pode aparecer uma vez até o operador desbloquear manualmente; depois o cookie de confiança persiste no perfil e os próximos acessos fluem.

## Estrutura do projeto

```text
.
  .env
  config.py
  run.py                     # Entrypoint; aborta o boot se faltar dependência crítica
  iniciar.bat                # Atalho Windows: venv + deps + run.py
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
  routes/                  # Pacote de rotas por domínio, todas no blueprint 'main' (spec 05)
    __init__.py            #   core: bp, hooks, dashboard, /api/pendencias, /health, /diagnostico*
    empresas.py            #   rotas de empresa (listagem/detalhe/editar/remover/nova/adicionar)
    certidoes.py           #   rotas /certidao/* (baixar fina delega a emissao_service)
    lotes.py               #   factory _register_batch_routes + fluxos do agendador
    relatorios.py          #   /relatorios, /configuracoes, exportação (carteira/dossiê/produtividade)
  auth.py                  # Login/papéis (deny-by-default, requer_papel) + painéis admin
  cli.py                   # Comandos CLI (criar-admin / criar-usuario)
  models.py                # Modelos do banco
  captcha_solver.py        # Integração 2captcha (ALTCHA e captcha de imagem)
  file_manager.py          # Detecção/movimentação de PDFs
  errors.py                # Taxonomia de erros + descrever_erro (mensagens acionáveis)
  utils.py                 # Utilitários compartilhados (to_bool, get_config_value, normalizar_cidade, json_error)
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
    deps_check.py          # Verificação de dependências críticas no boot (fail-fast)
    execution_logger.py    # Logger estruturado: console legível + app.jsonl
    diagnostics.py         # Buffer/recorrência em memória + histórico persistido
    preflight.py           # Pré-checagens (rede/Chrome/solver) antes de emitir
    health.py              # Health checks de dependências
    retry.py               # Retry com backoff/jitter
    rs_altcha.py           # Resolver/injetar ALTCHA no RS
    usuario_service.py     # Domínio de usuários (spec 01: criar/autenticar/papel/ativo)
    auditoria.py           # Registro/consulta de auditoria (spec 01)
    agendador.py           # Agendador APScheduler + jobs diários (spec 02)
    fila_emissao.py        # Fila durável de emissão TarefaEmissao (spec 02)
    snapshot_service.py    # Foto diária das contagens + classificação de status
    notificacoes.py        # Digest de vencimentos + alertas (spec 03)
    email_sender.py        # Transporte SMTP best-effort (spec 03)
    carteira_filtros.py    # Recorte da carteira replicado server-side (spec 04)
    export_service.py      # Planilhas XLSX (carteira e produtividade) (spec 04)
    dossie_service.py      # Dossiê PDF por empresa (capa fpdf2 + merge pypdf) (spec 04)
    emissao_service.py     # Orquestração da emissão individual "baixar" (spec 05)
    visualizar_token.py    # Tokens assinados de visualização de certidão (spec 05)
  static/
    css/
    images/
    js/                    # JS do dashboard extraído do HTML (spec 05)
      dashboard.js         #   entry ES module (<script type=module>, versionado)
      toasts.js            #   sistema de toasts (importado por dashboard.js)
  templates/
    base.html
    dashboard.html
    nova_empresa.html
    empresas.html
    empresa_detalhe.html
    empresa_remover_confirm.html
    relatorios.html
    produtividade.html       # Produtividade + exportação (spec 04)
    configuracoes.html
    diagnostico.html
    login.html               # Login (spec 01)
    usuarios.html            # Gestão de usuários, admin (spec 01)
    auditoria.html           # Painel de auditoria, admin (spec 01)
    403.html                 # Acesso negado (spec 01)
```

## Testes e CI

- Suíte `pytest` (`pip install -r requirements-dev.txt` + `pytest -q`).
- **CI com paridade de banco** (GitHub Actions, dois jobs em paralelo):
  - `testes-sqlite` — lint (`ruff`) + suíte em SQLite (gate rápido).
  - `testes-mysql` — suíte inteira contra **MySQL 8.0** (service container, `utf8mb4`/`utf8mb4_0900_ai_ci`) para pegar divergência de enum nativo/colação/tipo antes de produção, mais um teste de **migração idempotente** (`upgrade → downgrade → upgrade`).
- Localmente, aponte a suíte para outro banco com `TEST_DATABASE_URL` (sem a variável, usa SQLite).

## Limitações atuais

- Automações dependem da estabilidade dos portais públicos.
- Mudanças de HTML nos sites podem exigir ajuste de seletores.
- Captchas fora do lote RS e Municipal (Imbé) continuam majoritariamente manuais.
- Ainda não existe cobertura completa de testes automatizados para fluxos Selenium.

## Licença

Software proprietário — **todos os direitos reservados** (veja [LICENSE](LICENSE)).

O repositório é público apenas para fins de **estudo, demonstração e portfólio**: o código pode ser visualizado e lido, mas **não** há permissão para usar, executar, copiar, modificar ou redistribuir. Não é um projeto open-source. Para qualquer uso além da visualização, é necessária autorização prévia e por escrito do autor.
