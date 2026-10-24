# Runbook

Receitas operacionais que já precisaram ser montadas na pressa. Tudo aqui roda
com `--profile personal` em `us-east-1`.

Scripts em Python precisam das credenciais exportadas antes — um profile de
`aws login` não é lido pelo boto3 avulso:

```bash
eval "$(aws configure export-credentials --profile personal --format env)"
export AWS_DEFAULT_REGION=us-east-1
```

## Ver o estado do pool

```bash
python scripts/manage_accounts.py list
```

`IN_USE` sem sessão ativa correspondente é anomalia. Cruze com:

```bash
aws dynamodb scan --table-name aws-jam-Game-Sessions --profile personal \
  --filter-expression "active = :t" \
  --expression-attribute-values '{":t":{"BOOL":true}}' \
  --query 'Items[].{s:session_id.S,g:game_id.S,a:account_id.S}' --output table
```

## Encerrar uma sessão na mão

Use o caminho de produção, não escreva no DynamoDB direto — assim a conta passa
por revogação de console, `DeleteStack` e build de limpeza.

```bash
GAME_ACCOUNTS_TABLE=aws-jam-Game-Accounts \
GAME_SESSIONS_TABLE=aws-jam-Game-Sessions \
CLEANUP_PROJECT_NAME=aws-jam-account-cleanup \
AWS_REGION=us-east-1 ALERT_EMAIL=leandro.lx3@gmail.com \
.venv/bin/python - <<'PY'
import sys, os, boto3
sys.path.insert(0, 'layers/utils-layer')
import session_teardown
from account_manager import get_account_manager

SID = 'COLE-O-SESSION-ID-AQUI'
t = boto3.resource('dynamodb', region_name='us-east-1').Table(os.environ['GAME_SESSIONS_TABLE'])
s = t.get_item(Key={'session_id': SID})['Item']
print(session_teardown.end_session(t, s, get_account_manager(), reason='encerrada manualmente'))
PY
```

## Devolver uma conta em quarentena

Só depois de olhar o que o build deixou para trás.

```bash
aws codebuild batch-get-builds --ids <build-id> --profile personal \
  --query 'builds[0].{s:buildStatus,log:logs.deepLink}'
python scripts/manage_accounts.py set-status --account-id <conta> --status AVAILABLE
```

## Conferir se o nuke limpou mesmo

```bash
.venv/bin/python - <<'PY'
import boto3
ACCT = 'COLE-A-CONTA'
c = boto3.client('sts').assume_role(
    RoleArn=f'arn:aws:iam::{ACCT}:role/CloudPlayGameRole', RoleSessionName='check')['Credentials']
kw = dict(aws_access_key_id=c['AccessKeyId'], aws_secret_access_key=c['SecretAccessKey'],
          aws_session_token=c['SessionToken'], region_name='us-east-1')
iam, cf, ec2 = (boto3.client(s, **kw) for s in ('iam', 'cloudformation', 'ec2'))
print('HengHaTeamRole :', iam.list_role_policies(RoleName='HengHaTeamRole')['PolicyNames'])
print('stacks         :', len(cf.list_stacks(StackStatusFilter=['CREATE_COMPLETE','DELETE_FAILED'])['StackSummaries']))
print('EC2 vivas      :', sum(len(r['Instances']) for r in ec2.describe_instances()['Reservations']))
PY
```

Esperado: `['HengHaGuardrails']`, zero stacks, zero instâncias. A `GamePolicy`
**não** deve estar lá — ela é varrida junto com a sessão.

## Nova conta no pool

```bash
python scripts/manage_accounts.py provision \
  --email leandro.lx3+cloudplay-0N@gmail.com \
  --ou-id ou-jd38-9o1nbdwa --orchestrator-account 673000199571
```

Depois confirme que a `HengHaTeamRole` ficou de pé:
`python scripts/manage_accounts.py team-role --account-id <conta>`.

## Propagar mudança de guardrail para as contas existentes

```bash
for a in 369488682912 639793187465 708485766895; do
  python scripts/manage_accounts.py team-role --account-id $a
done
```

## Ver se o janitor rodou

```bash
aws logs filter-log-events --profile personal \
  --log-group-name /aws/lambda/$(aws lambda list-functions --profile personal \
    --query "Functions[?starts_with(FunctionName,'aws-jam-SessionJanitor')].FunctionName|[0]" --output text) \
  --start-time $(python3 -c "import time;print(int((time.time()-86400)*1000))") \
  --query 'events[].message' --output text | grep -i janitor
```

Varre de 30 em 30 minutos. "checked N, expired 0" com N > 0 é normal — só expira
acima de `MaxSessionHours`.

## Emergências

**Trancado fora pelo MFA:**

```bash
aws cognito-idp set-user-pool-mfa-config --user-pool-id us-east-1_WoKQuKpZv \
  --mfa-configuration OFF --profile personal
```

**Aluno perdeu o celular:**

```bash
aws cognito-idp admin-set-user-mfa-preference --user-pool-id us-east-1_WoKQuKpZv \
  --username <email> --software-token-mfa-settings Enabled=false,PreferredMfa=false \
  --profile personal
```

**Reverter a policy do bucket de artefatos:** a versão vigente está descrita no
`CLAUDE.md` do `cloudraiser-games`. Para ver a atual:

```bash
aws s3api get-bucket-policy --bucket cloudraiser-673000199571 --profile personal \
  --query Policy --output text | python3 -m json.tool
```

**Gasto fugindo:** em 150 USD no consolidado o AWS Budgets anexa a SCP de freeze
na OU sozinho. Para soltar antes do fim do ciclo, desanexe
`CloudPlayGames-freeze` de `ou-jd38-9o1nbdwa`.
