# Smoke Test — Emissão de Certidões (verificação manual)

Os testes automatizados (`pytest`) **não** exercitam o Selenium real — eles
substituem o navegador por mocks. Por isso, **depois de cada refatoração de
risco** (A1, A2, A3) a automação real precisa ser verificada à mão com este
roteiro. Se qualquer passo falhar, **não mescle** — avise e revertemos a etapa.

## Pré-requisitos

- App rodando local: `python run.py` (sem `FLASK_DEBUG` — o reloader pode
  reiniciar no meio de um lote).
- Certificado A1 do RS instalado/disponível para o passo Estadual RS.
- Chave do 2captcha configurada (`.env`) para o passo Imbé.
- Pelo menos uma empresa de cada cenário cadastrada (RS, Tramandaí, Imbé).

## Roteiro (emissão unitária — botão "Baixar" no dashboard)

| # | Ação | Resultado esperado |
|---|------|--------------------|
| 1 | Emitir **FGTS** de uma empresa | PDF baixa e é salvo na pasta da empresa; status fica verde; validade preenchida |
| 2 | Emitir **Trabalhista** de uma empresa | PDF baixa e é salvo; validade +180 dias |
| 3 | Emitir **Estadual RS** (empresa RS, com certificado) | Login por certificado abre; após resolver captcha e Enviar, o PDF baixa; validade ~+59 dias. PDF POSITIVO → certidão vira PENDENTE |
| 4 | Emitir **Municipal Tramandaí** | PDF baixa e é salvo; sem erro de aba aberta |
| 5 | Emitir **Municipal Imbé — Mobiliário** (modal) | Captcha 2captcha resolve; PDF baixa; nome do arquivo indica "Mobiliário" |
| 6 | Emitir **Municipal Imbé — Geral** (modal) | Idem, arquivo indica "Geral" |
| 7 | Emitir **Federal** | Redireciona para o site da Receita (sem automação) |

## Cenários de borda

| # | Ação | Resultado esperado |
|---|------|--------------------|
| 8 | Fechar a janela do Chrome no meio de uma emissão | App retorna status "janela fechada"/pendente, sem erro 500 |
| 9 | Tentar emitir **Estadual RS** com um **lote RS rodando** | Bloqueia com aviso "Lote Estadual RS em andamento" |
| 10 | Município sem automação configurada | Mensagem orientando usar "Abrir Site" |

## Lote (após verificar a emissão unitária)

| # | Ação | Resultado esperado |
|---|------|--------------------|
| 11 | Rodar um **lote pequeno de FGTS** | Progresso atualiza; certidões processadas; pausar/parar funcionam |
| 12 | Rodar um **lote pequeno de Estadual RS** | Idem, com o setup de certificado |
| 13 | Rodar um **lote pequeno Municipal** | Idem |

## Relatórios (após rodar os lotes acima)

Estas features são de banco/UI (sem Selenium), mas dependem dos lotes reais
para popular. Verifique **depois** dos passos 11–13.

| # | Ação | Resultado esperado |
|---|------|--------------------|
| 14 | Abrir **/relatorios** | Página carrega; indicadores e barras de distribuição por status/tipo batem com o dashboard |
| 15 | Ver **"Últimos lotes emitidos"** | O lote que você acabou de rodar aparece na linha do tipo/escopo certos (FGTS/Estadual RS/Municipal Imbé/Municipal Tramandaí), com "há X" e data |
| 16 | Clicar em **"Rendimento"** do lote | Modal abre com status, barra e contagens (emitidas / seguem pendentes / falhas) coerentes com o lote |
| 17 | Ver **"Evolução por status"** | No 1º dia mostra o aviso de que acumula; a partir do 2º dia de uso a linha aparece (uma foto/dia) |

## Registro

Anote a data e o que passou/falhou. Exemplo:

```
2026-06-10 — A1 (refatoração de baixar_certidao)
[x] 1 FGTS  [x] 2 Trabalhista  [x] 3 RS  [x] 4 Tramandaí
[x] 5 Imbé Mob  [x] 6 Imbé Geral  [x] 7 Federal
[x] 8 janela fechada  [x] 9 lote RS ativo  [x] 10 sem automação
OK para mesclar.
```
