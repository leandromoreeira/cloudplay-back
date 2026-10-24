# cloudplay-back

Orquestrador da plataforma. Lambdas atrás de um API Gateway que entregam o
catálogo de games, reservam uma conta AWS de um pool, sobem o lab do game
dentro dela, corrigem as respostas do aluno e depois **apagam a conta inteira**.

Um game nunca roda na conta do orquestrador. Ele roda numa conta descartável
que é reciclada com `aws-nuke` ao final.

## Os três repositórios

| Repo | O quê |
|---|---|
| `cloudplay-back` (este) | API, pool de contas, limpeza, governança (SCP/budget) |
| `cloudplay-front` | SPA React. Tem `CLAUDE.md` próprio |
| `cloudraiser-games` | Catálogo dos games e a config do `aws-nuke`. Tem `CLAUDE.md` próprio — **leia antes de mexer em template de game** |

Receitas operacionais (encerrar sessão na mão, soltar conta em quarentena,
conferir o nuke, emergências de MFA) estão em **`docs/RUNBOOK.md`**.

## Fontes da verdade

| O quê | Onde |
|---|---|
| Conteúdo dos games | DynamoDB `aws-jam-Games` (populado a partir do repo de games) |
| Sessões de jogo | DynamoDB `aws-jam-Game-Sessions` |
| Pool de contas e seu estado | DynamoDB `aws-jam-Game-Accounts` |
| Quem pode jogar o quê | DynamoDB `aws-jam-Game-Access` + grupo Cognito `full-access` |
| Templates, validators, artefatos | S3 `cloudraiser-673000199571` |
| Infra da aplicação | Stack SAM `aws-jam` (`template.yaml`) |
| Governança: OU, SCPs, budget | Stack `cloudplay-org` (`org/organization.yaml`), **deploy manual** |

## O ciclo de vida de uma sessão

```
POST /games/{id}/start
  grava a sessão como STARTING   <- antes de reivindicar qualquer coisa
  reivindica uma conta           <- escrita condicional, atômica
  grava account_id na sessão
  garante a HengHaTeamRole + escreve a GamePolicy do game
  create_stack                   <- template vem do S3
  sessão -> CREATE_IN_PROGRESS

POST /games/{id}/console  ->  sessão de console AWS de 1 hora
POST /games/{id}/tasks/{t}/answer  ->  invoca validate_taskN na conta do game

POST /games/{id}/end   (ou o janitor, após MaxSessionHours)
  sessão -> CLEANUP_IN_PROGRESS
  conta -> CLEANING
  apaga a GamePolicy             <- derruba o console aberto na hora
  dispara DeleteStack            <- sem esperar, só para adiantar
  inicia o build de limpeza (aws-nuke)

EventBridge: build terminou -> cleanup_result
  SUCCEEDED -> conta AVAILABLE,     sessão CLEANUP_COMPLETE
  qualquer outro -> conta QUARANTINED + e-mail, sessão CLEANUP_FAILED
```

## O pool de contas

Estados em `aws-jam-Game-Accounts`: `AVAILABLE` → `IN_USE` → `CLEANING` →
`AVAILABLE`, com `QUARANTINED` como saída de emergência. As transições vivem em
`layers/utils-layer/account_manager.py` e **todas são escritas condicionais** —
é o que impede duas sessões de receberem a mesma conta.

Administração pelo `scripts/manage_accounts.py`: `list`, `add`, `remove`,
`set-status`, `team-role`, `provision`. O `provision` cria uma conta AWS nova,
move para a OU e registra no pool.

Uma conta em `QUARANTINED` sai do pool e **só volta na mão**
(`set-status --status AVAILABLE`). Um e-mail avisa quando isso acontece.

## Duas roles dentro de cada conta de game

| Role | Quem usa | Para quê |
|---|---|---|
| `CloudPlayGameRole` | o orquestrador | subir o lab, corrigir respostas, nukar |
| `HengHaTeamRole` | o aluno | é nela que o aluno entra no console |

A `HengHaTeamRole` carrega duas inline policies:

- **`HengHaGuardrails`** — sempre igual, só `Deny`. Bloqueia ler/mexer na stack
  do lab, nas lambdas `validate_task*` e nos logs delas, criar usuário IAM ou
  access key, e alterar as roles de infraestrutura (inclusive ela mesma).
- **`GamePolicy`** — reescrita a cada sessão a partir do `gameN/iam.json`. Games
  sem `iam.json` recebem um allow-tudo; os guardrails é que seguram.

Os mesmos bloqueios existem como SCP (`PlayerGuardrailsPolicy`), porque uma
policy de role só vale para aquela role — um aluno que consiga criar outra role
sairia dela. O SCP isenta a `CloudPlayGameRole`, senão o orquestrador não
conseguiria trabalhar.

## Limpeza

CodeBuild `aws-jam-account-cleanup`, timeout de 60 min, roda `aws-nuke` v3.68.3.

A config base está em **`cloudraiser-games/nuke/aws-nuke.yaml`** e chega ao S3
pelo sync normal daquele repo. Ela **não tem seção `accounts:`** — o buildspec
injeta uma nomeando só a conta que recebeu, para um ARN errado falhar fechado.

Três camadas antes de qualquer destruição: a config bloqueia a conta
orquestradora, o build injeta só a conta alvo, e o build recusa rodar se a conta
não estiver `CLEANING` no DynamoDB.

O nuke preserva a `HengHaTeamRole` e a `HengHaGuardrails`, mas **não** a
`GamePolicy` — a conta volta ao pool com a role e os guardrails e nada mais.

## Autenticação

Cognito user pool `us-east-1_WoKQuKpZv`, tier Essentials, criação só por admin.

- **MFA obrigatório por app autenticador (TOTP)**. Não é por e-mail: o Cognito
  exige SES para isso, e SES fora do sandbox pede domínio próprio, que não
  temos. Detalhes em `git log` do commit que trocou.
- Sessão de **8 horas** (`SessionHours`) — é a validade do refresh token.
- Recuperação de senha por e-mail verificado, com o remetente padrão do Cognito.
- Aluno perdeu o celular: `admin-set-user-mfa-preference` com
  `Enabled=false` força novo cadastro no próximo login.

## Acesso ao console AWS pelo aluno

`POST /games/{id}/console` confere grant, sessão ativa, stack em
`CREATE_COMPLETE` e que a conta **ainda é daquela sessão**; só então assume a
`HengHaTeamRole` e troca por uma URL de login federado.

**A sessão dura 1 hora e não há como aumentar.** Assumir role a partir de uma
Lambda é *role chaining*, que a AWS limita a uma hora, e o endpoint de federação
recusa o parâmetro `SessionDuration` quando a credencial veio assim. O
contorno é o `Issuer`: ao expirar, o console manda o aluno para `/reconnect` no
front, que pede uma URL nova.

A URL é credencial ao portador — **nunca logar**.

## Ambiente

| | |
|---|---|
| Região | `us-east-1` (tudo) |
| Profile | `personal` |
| Conta orquestradora / management | `673000199571` |
| Organização / root / OU dos games | `o-prvoj7g1n5` / `r-jd38` / `ou-jd38-9o1nbdwa` |
| Contas do pool | `369488682912`, `639793187465`, `708485766895` |
| Bucket de artefatos | `cloudraiser-673000199571` |
| Front (bucket / CloudFront) | `aws-jam-frontendbucket-3iduyp7mgecn` / `d1hjkx6ep6vabw.cloudfront.net` |
| API | `https://n40hrrfx49.execute-api.us-east-1.amazonaws.com/Prod/games/` |

Budget único de 100 USD/mês no consolidado da organização; em 150 USD o AWS
Budgets anexa uma SCP de freeze na OU dos games. Não há budget por conta: o que
importa é o total.

## Testes

```bash
.venv/bin/python -m pytest -q        # ~136 testes, tudo mockado com moto
```

Se o `.venv` não existir: `python3 -m venv .venv && .venv/bin/pip install -r tests/requirements.txt`.

Dois limites do moto que já custaram tempo: ele **não implementa**
`simulate_principal_policy` (para testar policy de verdade, use a AWS), e exige
`location` num source `NO_SOURCE` do CodeBuild, que a AWS real dispensa.

## Deploy

```bash
sam deploy                                    # usa o profile do samconfig.toml

# governança, só quando org/organization.yaml mudar:
aws cloudformation deploy --template-file org/organization.yaml \
  --stack-name cloudplay-org --capabilities CAPABILITY_IAM \
  --parameter-overrides RootId=r-jd38 --profile personal
```

**Quando o contrato da API muda, back e front sobem juntos.**

## Rodar os scripts localmente

Um profile vindo de `aws login` não é lido pelo boto3 avulso — só pelo botocore
embutido no AWS CLI v2. Dá `NoCredentialsError`. Exporte antes:

```bash
eval "$(aws configure export-credentials --profile personal --format env)"
export AWS_DEFAULT_REGION=us-east-1   # boto3 local não lê AWS_REGION
```

## Decisões deliberadas que parecem bugs

Não "conserte" estas sem ler o motivo:

- **A sessão é gravada como `STARTING` antes de a conta ser reivindicada.** É o
  que impede um duplo clique de levar duas contas. Por isso o
  `get_game_session` ignora a checagem de stack enquanto o status é `STARTING`:
  a stack ainda não existe, e o caminho de "stack sumiu" encerraria a sessão.
- **Essa proteção não é atômica**, por escolha. Consultar e gravar são duas
  chamadas, e o GSI é eventualmente consistente. A janela caiu de segundos para
  ~100ms e o botão do front bloqueia o segundo clique. A versão atômica (lock
  com chave determinística) foi escrita e descartada: custava três pontos de
  release e uma regra de lock órfão.
- **`end_game_session` não espera o `DeleteStack`** e manda só `StackName`. Os
  parâmetros "forçados" (`FORCE_DELETE_STACK`, `RetainResources`) **só são
  aceitos em stack já em `DELETE_FAILED`** — mandar antes derruba a chamada.
- **`mark_available` limpa o `session_id`; `mark_cleaning` mantém.** Uma conta
  livre com id de sessão parece ocupada.
- **`cleanup_result` casa a sessão pelo id do build**, não pela conta: uma conta
  já rodou dezenas de sessões.
- **A revogação do console só acontece depois do `mark_cleaning`.** Antes disso
  a conta poderia já ser de outra pessoa.

## Pendências conhecidas

- Versionamento desligado no bucket de artefatos.
- `scripts/upload_game_templates.py` está quebrado: aponta para
  `back/cloudraiser-games`, que não existe.
- `README.md` deste repo é boilerplate do SAM com partes desatualizadas.
- Uma conta pode ficar `IN_USE` sem sessão se a Lambda de start morrer entre
  reivindicar e gravar; o janitor varre sessões e não enxerga esse caso.
