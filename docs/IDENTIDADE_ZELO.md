# Identidade Visual — Zelo

> Plano de rebrand do sistema interno (antes "Controle de Certidões Fiscais") para a
> identidade **Zelo**. Guiado pela skill `frontend-design` (oficial Anthropic).
> Decisões fechadas: **mantém Bootstrap 5.3** (sem Tailwind); mudanças via *design tokens*,
> não reescrita de layout.

## 1. Brief (o mundo do produto)

- **Sujeito:** sistema interno que vigia a validade de certidões fiscais e dispara a emissão.
- **Quem usa:** time fiscal/administrativo da **Assecon** — escritório contábil familiar.
- **Trabalho único da tela:** responder, num relance, *"está tudo em dia?"* e deixar agir (emitir).
- **Marca-mãe:** Assecon é preto e branco, sóbria, séria. **Zelo** = cuidado, diligência, vigilância.
- **Coexistência:** Zelo é o nome do *sistema*; Assecon é a *casa*. A logo Assecon permanece;
  o wordmark Zelo passa a nomear a aplicação.

## 2. Conceito / assinatura

**"A única cor é o status."**

Toda a interface é grafite sobre papel — séria, monocromática, como a Assecon. A *única*
cromaticidade que sobra é o **indicador de validade** da certidão (válida / a vencer / vencida).
A vigilância — o *zelo* — ganha forma: o olho vai direto ao que precisa de atenção, porque é a
única coisa colorida na tela.

Reforço do motivo: a "espinha" vertical de status que já existe nas linhas
(`td.cert-tipo-col::before`) vira o **elemento-assinatura do sistema**, repetido com disciplina.

## 3. Sistema de tokens

### Cores — chrome monocromático (4–6 valores nomeados)

| Token            | Light     | Dark      | Uso                                   |
|------------------|-----------|-----------|---------------------------------------|
| `--zelo-ink`     | `#16181C` | `#F2F4F7` | Texto forte, wordmark, ação primária  |
| `--zelo-graphite`| `#2B2F36` | `#E2E5EA` | Botões/links primários (substitui azul)|
| `--zelo-slate`   | `#6B7280` | `#9BA3AD` | Texto secundário / muted              |
| `--zelo-line`    | `#E3E6EB` | `#2A2F35` | Bordas, divisores                     |
| `--zelo-mist`    | `#F5F6F8` | `#16191D` | Superfícies (cards, sidebar, navbar)  |
| `--zelo-paper`   | `#FFFFFF` | `#0F1115` | Fundo da página                       |

**Movimento-chave:** mapear `--bs-primary` (hoje azul `#0d6efd`) → `--zelo-graphite`/`--zelo-ink`.
Botões e links deixam de ser azuis e ficam grafite/quase-preto. Só isso já "veste" o app de Zelo.

### Cores — status (a *única* cromaticidade; dessaturada de propósito)

Refinar os tons de status para que pareçam **escolha**, não os vermelhos/verdes saturados padrão
do Bootstrap. Mantêm o significado, ficam mais sóbrios:

| Status                | Atual (BS)  | Zelo — Light | Zelo — Dark |
|-----------------------|-------------|--------------|-------------|
| Válida (verde)        | `#198754`   | `#2E7D52`    | `#4FB07E`   |
| A vencer (âmbar)      | `#ffc107`   | `#B07408`    | `#E0A642`   |
| Vencida (vermelho)    | `#dc3545`   | `#B23A33`    | `#E8736B`   |
| Pendente (tijolo)     | `#fd7e14`   | `#C2622A`    | `#D98A52`   |
| Sem data (cinza)      | `#6c757d`   | `#9AA1AB`    | `#6B7280`   |

> Nota (impl.): **Pendente** ganhou tom próprio (`--zelo-pend`), distinto de Vencida — costuma indicar débito, o alerta mais relevante. A *espinha* e o texto `.cert-status-*` de pendente seguem como `danger` (vermelho); só o contador/banda do resumo usa o tijolo.

No light, os tons são mais escuros (contraste sobre papel); no dark, mais claros/tintados
(legíveis sobre grafite). O significado é o mesmo nos dois temas.

### Tipografia — superfamília IBM Plex (3 papéis, coesa e não-default)

A skill pede fugir de Inter/Roboto/Arial. Escolha justificada pelo *mundo* do produto (documentos
oficiais + dados numéricos):

| Papel    | Fonte             | Onde                                             |
|----------|-------------------|--------------------------------------------------|
| Display  | **IBM Plex Serif**| Wordmark "Zelo", títulos de página, cabeçalhos   |
| Corpo/UI | **IBM Plex Sans** | Labels, botões, navegação, texto geral           |
| Dados    | **IBM Plex Mono** | CNPJ, datas, números, badges de contagem         |

Por quê: o serif dá **gravidade notarial** (cara de documento); o sans entrega **legibilidade**
em tabelas densas; o mono **alinha números** e dá tom de *ledger* contábil. Uma só superfamília =
coesão. É distinto sem ser tendência genérica. Disciplina de pesos: Serif 500/600; Sans 400/500/600;
Mono 400/500. Self-host ou Google Fonts com subset latin.

### Forma

- **Raio:** padronizar em `0.5rem` (cards/inputs) e `999px` (chips/badges) — já é o padrão atual,
  só consolidar em token.
- **Sombra:** sutil e fria (`0 1px 2px rgba(16,18,28,.06)`); nada de sombras pesadas.
- **Linhas:** divisores hairline em `--zelo-line`.

### Temas — claro e escuro (ambos de primeira classe)

O sistema já tem alternância de tema via `data-bs-theme` (toggle na navbar, persistido em
`localStorage`, com anti-flash no `<head>`). A identidade Zelo **preserva esse mecanismo** e trata
os dois temas como entregas iguais — não há tema "principal" e tema "secundário".

- **Tokens por tema:** cada token de cor tem valor para light **e** dark (tabelas acima). Definir os
  dois conjuntos em `[data-bs-theme="light"]` e `[data-bs-theme="dark"]` no `:root` (o app já usa
  esse padrão para sobrescrever os fundos do Bootstrap em `style.css`).
- **Claro (papel):** grafite sobre branco/`--zelo-mist`; sensação de documento limpo.
- **Escuro (grafite):** texto claro sobre `--zelo-paper` quase-preto; superfícies em `--zelo-mist`
  escuro. Wordmark e ação primária invertem (ink claro).
- **Status:** usa as colunas Light/Dark da tabela de status — tons escuros no claro, tintados no
  escuro, mantendo o conceito "a única cor é o status" nos dois.
- **Quality floor (a validar em cada fase):** contraste AA em ambos os temas, foco visível, e a
  troca de tema sem "flash". Cada fase é revisada **nos dois temas** antes de fechar.

## 4. Identidade verbal (copy)

- Trocar todas as ocorrências de **"Controle de Certidões Fiscais"** / **"Sistema de Controle de
  Certidões Fiscais"** pelo wordmark **"Zelo"**.
- **Tagline principal:** **"Regularidade sob controle."**
  Cobre prazo *e* débito sem afirmar que o sistema detecta débito (recurso ainda não existe);
  envelhece bem conforme o produto evolui. Uso pontual (login/sobre/rodapé), não em toda tela.
  Alternativas guardadas: *"A saúde fiscal, à vista."* · *"Pendência à vista é débito à vista."* ·
  *"Zelo pela regularidade."*
- **Rodapé:** `Zelo · Assecon Assessoria e Contabilidade`.
- `<title>`: `Zelo — Certidões` (+ nome da página).
- Vozes ativas nos botões/toasts (princípio da skill): "Emitir" → toast "Emitida".

### Wordmark / navbar (wireframe)

```
┌ navbar ─────────────────────────────────────────────┐
│  ☰   Zelo                          [logo Assecon]  ◐ │
│      ^serif ink     wordmark do sistema   marca-mãe  │
└──────────────────────────────────────────────────────┘
```

## 5. Autocrítica (contra os defaults de "design de IA")

A skill alerta para 3 clichês. Checagem:

- ❌ creme + serif alto-contraste + terracota — **não usamos** (fundo é papel/grafite).
- ❌ quase-preto + verde-ácido/vermelhão — **não usamos** (cor só no status, e dessaturada).
- ❌ broadsheet hairline sem raio — **não usamos** (mantemos raio e respiro).
- ✅ Monocromático grafite + status como única cor + superfamília IBM Plex = escolha **derivada do
  sujeito** (contabilidade/documento/prazo), não default.
- **Decisão opinativa #1:** matar o "arco-íris" de cor por *tipo* de certidão (chips federal=azul,
  fgts=ciano, estadual=roxo, municipal=laranja, trabalhista=verde). Cor que codifica *tipo* é
  decoração — o tipo já é dito por ícone + rótulo. Reservar cor só para *status*, que codifica
  verdade (validade). (Mudança maior → faseada, ver Fase 3.)

## 6. Faseamento (passos pequenos, reversíveis)

| Fase | Entrega | Arquivos | Risco |
|------|---------|----------|-------|
| 0 | Tokens CSS + import das fontes; mapear `--bs-primary` → grafite | `base.html`, `style.css` | baixo |
| 1 | Identidade verbal: wordmark/título/rodapé "Zelo" | `base.html`, demais templates (textos) | baixo |
| 2 | Status refinado (dessaturar verde/âmbar/vermelho/cinza + espinha) | `style.css` | baixo |
| 3 | Chips/tipos monocromáticos (cor só no status) | `style.css`, `dashboard.html` | médio |
| 4 | Componentes aos tokens: botões, sombras, raios, tabelas, modais | `style.css` | médio |
| 5 | Polimento: foco visível, `prefers-reduced-motion`, revisão por screenshot | `style.css` | baixo |

## 7. Arquivos afetados (reais)

- `app/templates/base.html` — `<head>` (fontes), wordmark/navbar, `<title>`, rodapé.
- `app/static/css/style.css` — bloco de tokens `:root`, mapeamento Bootstrap, status, componentes.
- `app/templates/*.html` — textos pontuais que citam o nome antigo.
- `app/static/images/` — favicon e wordmark Zelo (preto/branco) novos.
- `docs/context.json` — `metadata.projeto` (referência; arquivo é local/gitignored).
- `README.md` — nome e descrição.

## 8. Fora de escopo (o que NÃO muda)

- Estrutura/layout (navbar + sidebar + conteúdo), rotas, modelos, lógica de negócio e JS de
  comportamento.
- Sem Tailwind. Sem novo *build step* (fontes via CDN/self-host CSS, sem Node).
- Logos Assecon permanecem (Zelo nomeia o sistema, não substitui a marca-mãe).
