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
| Progresso do aluno por game | DynamoDB `aws-jam-Game-Progress` |
| Quem pode jogar o quê | DynamoDB `aws-jam-Game-Access` + grupo Cognito `full-access` |
| Quem administra | grupo Cognito `admin` |
| Templates, validators, artefatos | S3 `cloudraiser-673000199571`, publicados pelo `tools/sync_s3.sh` do repo de games — **não há script de upload aqui** |
| Infra da aplicação | Stack SAM `aws-jam` (`template.yaml`) |
| Governança: OU, SCPs, budget | Stack `cloudplay-org` (`org/organization.yaml`), **deploy manual** |

## O ciclo de vida de uma sessão

```
POST /games/{id}/start
  grava a sessão como STARTING   <- antes de reivindicar qualquer coisa
  reivindica uma conta           <- escrita condicional, atômica
  grava account_id na sessão
  garante a HengHaTeamRole + escreve a GamePolicy do game
  create_stack                   <- template vem do S3, com NotificationARNs
  sessão -> CREATE_IN_PROGRESS

CloudFormation -> tópico SNS -> stack_events
  grava stack_status na sessão sozinho, sem ninguém perguntar
  em CREATE_COMPLETE, busca os outputs (única chamada cross-account)

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

## Progresso do aluno

Dois registros, porque refazer um game concluído não pode apagar o fato de
tê-lo concluído:

| Onde | O quê | Vida |
|---|---|---|
| `completed_tasks` no item de **sessão** | tasks concluídas nesta tentativa, com `completed_at` e `seconds` | morre com a sessão |
| `aws-jam-Game-Progress` | `playing` e `ever_completed_at`, um item por (aluno, game) | permanente |

O status do chip **não é gravado** — sai de duas perguntas, em
`layers/utils-layer/game_progress.py`:

```
playing            -> in_progress     (vence done: ele está jogando agora)
ever_completed_at  -> done
resto              -> todo
```

Começou e abandonou sem concluir volta para `todo`. `ever_completed_at` é
escrito com `if_not_exists` e nunca se move.

As tasks **abrem uma de cada vez**: libera até a primeira não concluída da
sessão atual. O front esconde a travada (se desse para ler, daria para fazer
fora de ordem e submeter em ordem) e o `submit_task_answer` recusa com 409
nomeando a que falta. Sem exceção para `full-access`.

`seconds` é medido desde a última coisa concluída — início da sessão ou task
anterior. Pressupõe trabalho em ordem, o que o travamento agora garante, mas
continua errado para uma sessão largada aberta de um dia para o outro.

As três escritas de progresso são **best-effort**: nenhuma delas pode
derrubar um start que deu certo, uma resposta correta ou a limpeza de uma
conta. Um `playing` preso é cosmético e some no próximo start.

## O status do stack chega sozinho

O `create_stack` recebe o tópico `aws-jam-stack-events` em `NotificationARNs`,
e a Lambda `stack_events` grava cada transição na sessão. Antes disso, a única
coisa que descobria que um stack tinha ficado pronto era a página do aluno
perguntando — cada poll custava um `AssumeRole` + `DescribeStacks` na conta do
game, e a sessão só estava correta até onde alguém tinha perguntado.

Três coisas que não são óbvias:

- **A policy do tópico libera por `aws:PrincipalOrgID` + `ArnLike` na
  `CloudPlayGameRole`**, não por service principal. O `CreateStack` valida que
  *quem chama* pode publicar e recusa com 403 se não puder — então uma policy
  errada quebra o start de forma barulhenta, em vez de perder eventos em
  silêncio. Verificado na prática.
- **Não dá para filtrar na subscrição.** O CloudFormation não manda
  `MessageAttributes` e o corpo é texto, não JSON — nenhum dos dois escopos de
  filtro do SNS tem o que casar. Por isso a restrição vive na policy do
  tópico: quem não é a role do orquestrador não publica. Principals da própria
  conta orquestradora passam de qualquer forma (dentro da conta dona, a policy
  de IAM concede sozinha); é inevitável e inofensivo.
- **O formato é texto, `chave='valor'` por linha**, não JSON.
- **O SNS não garante ordem.** A escrita é condicional em
  `stack_status_at < :ts`, senão um `CREATE_IN_PROGRESS` atrasado sobrescreve
  o `CREATE_COMPLETE` que veio antes dele e o aluno volta a ver spinner sobre
  um lab pronto.

O `get_game_session` virou **leitura pura do DynamoDB** — não fala com o
CloudFormation em hipótese alguma. O `active` da sessão é do
`session_teardown`; o status do stack é do `stack_events`. Se um dia voltar
uma chamada cross-account para lá, é regressão: há teste pegando.

Eventos de **nested stack** (o `validation_lambdas.yml` da maioria dos games)
também têm `ResourceType = AWS::CloudFormation::Stack`. São distinguidos por
`LogicalResourceId != StackName`.

## O pool de contas

Estados em `aws-jam-Game-Accounts`: `AVAILABLE` → `IN_USE` → `CLEANING` →
`AVAILABLE`, com `QUARANTINED` como saída de emergência. As transições vivem em
`layers/utils-layer/account_manager.py` e **todas são escritas condicionais** —
é o que impede duas sessões de receberem a mesma conta.

Administração pelo `scripts/manage_accounts.py`: `list`, `add`, `remove`,
`set-status`, `team-role`, `provision`. O `provision` cria uma conta AWS nova,
move para a OU e registra no pool.

Uma limpeza que falha **não quarentena na hora**. O `cleanup_result` grava
`cleanup_attempts` + `cleanup_failed_at` e deixa a conta em `CLEANING` (que já
a mantém fora do pool); o **janitor** redispara o build depois de
`RetryCleanupAfterMinutes`. Só após `MaxCleanupAttempts` falhas é que
quarentena e manda e-mail.

Por que o retry é no janitor e não dentro do build: a causa quase sempre é a
AWS ainda não ter soltado algo — **ENI de Lambda em VPC leva dezenas de
minutos** e segura o security group, a subnet e a VPC atrás dela. Esperar
dentro do CodeBuild é pagar container por minuto para dormir; foi assim que
três builds de ~40 min esvaziaram o pool em 28-30/10/2026. O janitor já acorda
de 30 em 30 min e custa o mesmo achando ou não algo. Por isso o
`--max-wait-retries` do nuke caiu de 5 para **2**: desistir rápido ficou
barato.

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

## CORS

Uma origem só, nunca `*`: por padrão a distribuição CloudFront desta própria
stack. O parâmetro `AllowedOrigin` existe para o caso de apontar um dev server
local para a API publicada (`sam deploy --parameter-overrides
AllowedOrigin=http://localhost:8080`); vazio significa "a minha CloudFront".

São **três** lugares, e esquecer o terceiro foi o bug:

1. `Cors` no `AWS::Serverless::Api` — gera o `OPTIONS` de preflight.
2. Cada handler, via `cors.allowed_origin()` da layer.
3. **`GatewayResponses`** — as respostas que o API Gateway dá sem chegar a
   handler nenhum. A que importa é `UNAUTHORIZED`: o 401 do authorizer
   Cognito saía sem header, o browser escondia a resposta, o `fetch` do front
   rejeitava como erro de rede, e o refresh-and-retry que existe exatamente
   para esse caso nunca via um status para reagir.

Um teste varre `games/*/app.py` atrás de `'*'` para a estrela não voltar.

## Autenticação

Cognito user pool `us-east-1_WoKQuKpZv`, tier Essentials, criação só por admin.

- **MFA obrigatório por app autenticador (TOTP)**. Não é por e-mail: o Cognito
  exige SES para isso, e SES fora do sandbox pede domínio próprio, que não
  temos. Detalhes em `git log` do commit que trocou.
- Sessão de **8 horas** (`SessionHours`) — é a validade do refresh token.
- Recuperação de senha por e-mail verificado, com o remetente padrão do Cognito.
- Aluno perdeu o celular: `admin-set-user-mfa-preference` com
  `Enabled=false` força novo cadastro no próximo login.

## Analytics

`GET /analytics` — sem parâmetro devolve os dados de quem chama; admin recebe
os de todos. `?user=<email>` é só para admin (um aluno pedindo outro leva
403) e `?game=<id>` recorta por game; os dois combinam.

**O recorte por game nunca alarga quem aparece.** Ele é aplicado depois de o
escopo estar resolvido, então um aluno abrindo a tela de um game vê as sessões
**dele** naquele game, não as da turma. Tem teste.

Tudo é derivado na leitura, de linhas que outro código já grava: a sessão sabe
quando começou, quando acabou, por que acabou e quais tasks fecharam dentro
dela, com quantos segundos cada uma. As conclusões de game vêm da
`Game-Progress`, não das sessões — um game fechado semana passada continua
fechado, e refazê-lo não conta duas vezes.

Duas ausências deliberadas: **tentativas** (nada registra resposta errada, então
"quantas vezes errou" não é respondível e não é chutado) e **histórico antigo**
— as 320 sessões de teste anteriores a out/2026 foram apagadas em 27/10/2026,
então as séries começam ali.

O `per_task` é o que justifica a tela: mediana de tempo por task. O primeiro
dado real já mostrou um game com task1 em 30min e task2 em 1min19s.

## Os dois grupos do Cognito

São **independentes**, e isso é deliberado:

| Grupo | Dá |
|---|---|
| `full-access` | ver e jogar todos os games, sem grant por game |
| `admin` | a área `/admin` e a gestão de usuários — **nada** no catálogo |

Quem administra e também precisa ver tudo entra **nos dois**. A UI de criação
lembra disso, mas o backend não infere um do outro.

O `admin` é verificado no backend em toda rota `/admin/*` (`is_admin` em
`access_control.py`). Esconder o botão no front não é controle.

Duas travas que existem para não haver tranca sem chave: **ninguém se
desabilita** e **ninguém tira o próprio `admin`**. O último admin fazendo
qualquer uma das duas deixaria a CLI como único caminho de volta.

O primeiro admin entra na mão — o CloudFormation cria o grupo, não a
associação:

```bash
aws cognito-idp admin-add-user-to-group --user-pool-id us-east-1_WoKQuKpZv \
  --username leandro.lx3@gmail.com --group-name admin --profile personal
```

`GET /games` aceita `status=todo|in_progress|done`. O filtro é aplicado
**antes da paginação**, junto do filtro de acesso, senão os totais descrevem
uma lista que o aluno não vê e páginas chegam curtas. Valor desconhecido é
400: devolver vazio pareceria "você não jogou nada".

`GET /admin/games` existe porque o `GET /games` normal é filtrado pelo acesso
de quem chama: um admin fora do `full-access` veria um catálogo incompleto
justamente na tela de conceder acesso. Devolve só `id`, `title`, `category` e
`difficulty` — a tela desenha uma linha por game, e mandar o HTML de 160 games
não serviria para nada.

**Apagar usuário não existe de propósito**: desabilitar cobre o caso real e
preserva o vínculo com as sessões históricas. Reset de senha também não — a
recuperação por e-mail verificado é self-service desde a troca para TOTP.

Uma Lambda (`admin_users`) atende as sete rotas de `/admin`, em vez de seis
funções. É exceção ao padrão do repositório, por serem operações raras,
coesas e com a mesma policy.

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

O bucket de artefatos **não é gerenciado por código** — nem aqui nem no repo de
games. Existe desde 2019 e sua configuração é mantida à mão:

- versionamento ligado, com lifecycle mantendo **3 versões** de cada arquivo (a
  atual mais duas) e apagando delete markers órfãos. Com versionamento, um
  `s3 sync --delete` cria marker em vez de apagar — a mesma regra limpa isso;
- a bucket policy libera leitura para a organização, mantém anônimo só o que o
  aluno e o runtime precisam baixar, e nega não-TLS. **O contrato dela está
  documentado no `CLAUDE.md` do repo de games** — leia antes de mexer.

`put_bucket_policy` substitui a policy inteira. Nunca rode um script que a
escreva sem ler o que ele escreve: foi por isso que o `setup_s3_bucket.py`
daqui foi apagado.

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
- **`submit_task_answer` checa `FunctionError` antes do `StatusCode`.** O
  `Invoke` devolve 200 mesmo quando a função lança; o payload vira stack
  trace, `completed` lê `False` e a mensagem cai no default. Sem essa
  checagem o aluno recebe "Not yet complete" sobre um game quebrado e fica
  tentando. Agora é 502 dizendo que o problema não é a resposta dele.
- **A revogação do console só acontece depois do `mark_cleaning`.** Antes disso
  a conta poderia já ser de outra pessoa.
- **`mark_playing` é a última coisa do `start_game_session`.** Antes do
  `create_stack` daria para o chip dizer que o aluno está num game que não
  subiu.
- **`all_tasks_done` é falso para um game sem tasks.** Conjunto vazio é
  subconjunto de tudo; sem a guarda o chip ficaria verde ao abrir.

## Pendências conhecidas

- `README.md` deste repo é boilerplate do SAM com partes desatualizadas.
- Uma conta pode ficar `IN_USE` sem sessão se a Lambda de start morrer entre
  reivindicar e gravar; o janitor varre sessões e não enxerga esse caso.
