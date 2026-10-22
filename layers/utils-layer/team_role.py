"""
The role a player logs into their game account with.

HengHaTeamRole is the AWS Jam convention and several game templates already
reference it by name (game96, game159, game167, game300), so the name is
fixed. It exists for the whole life of a pool account and carries two inline
policies:

  HengHaGuardrails - always the same, always Deny. What a player must not be
                     able to do, whichever game they are playing.
  GamePolicy       - written at the start of each session from the game's
                     iam.json, and swept with the rest of the account.

An explicit Deny beats any Allow, so the guardrails still hold for the games
that ship no iam.json and get the allow-everything default below.

The guardrails are the role's own policy, so they only bind sessions of this
role. A player who can create a *new* role walks around them, which is why
org/organization.yaml carries the same core denies as an SCP: that one binds
every principal in the account and cannot be escaped from inside.
"""

import json

from botocore.exceptions import ClientError

ROLE_NAME = 'HengHaTeamRole'
GUARDRAIL_POLICY_NAME = 'HengHaGuardrails'
GAME_POLICY_NAME = 'GamePolicy'

# A session runs for at most MaxSessionHours (3) and the janitor sweeps every
# 30 minutes, so four hours covers the longest a login can usefully last.
MAX_SESSION_SECONDS = 4 * 60 * 60

# The lab stack, and everything CloudFormation nests underneath it — the
# validation lambdas live in a nested stack whose name starts the same way.
SESSION_STACK_ARN = 'arn:aws:cloudformation:*:*:stack/game-session-*'
VALIDATION_FUNCTION_ARN = 'arn:aws:lambda:*:*:function:validate_task*'
VALIDATION_LOG_GROUP_ARN = 'arn:aws:logs:*:*:log-group:/aws/lambda/validate_task*'

# Deleting or rewriting any of these is a way out of the account, or a way to
# tamper with how answers are graded.
PROTECTED_ROLES = [
    f'arn:aws:iam::*:role/{ROLE_NAME}',
    'arn:aws:iam::*:role/CloudPlayGameRole',
    'arn:aws:iam::*:role/OrganizationAccountAccessRole',
    'arn:aws:iam::*:role/validation-checker-role',
]

# 16 of the 164 games ship no iam.json. They get this, and the guardrails are
# what stop "everything" from meaning everything.
DEFAULT_GAME_POLICY = {
    'Version': '2012-10-17',
    'Statement': [
        {'Sid': 'NoGamePolicyOnFile', 'Effect': 'Allow', 'Action': '*', 'Resource': '*'}
    ],
}


def guardrail_policy():
    """The denies every player carries, whatever their game allows."""
    return {
        'Version': '2012-10-17',
        'Statement': [
            {
                'Sid': 'NoTouchingTheLabStack',
                'Effect': 'Deny',
                'Action': [
                    'cloudformation:DeleteStack',
                    'cloudformation:UpdateStack',
                    'cloudformation:UpdateTerminationProtection',
                    'cloudformation:CancelUpdateStack',
                    'cloudformation:ContinueUpdateRollback',
                    'cloudformation:RollbackStack',
                    'cloudformation:SetStackPolicy',
                ],
                'Resource': SESSION_STACK_ARN,
            },
            {
                # The template is the answer key.
                'Sid': 'NoReadingTheLabStack',
                'Effect': 'Deny',
                'Action': [
                    'cloudformation:GetTemplate',
                    'cloudformation:GetTemplateSummary',
                    'cloudformation:ListStackResources',
                    'cloudformation:DescribeStackResource',
                    'cloudformation:DescribeStackResources',
                ],
                'Resource': SESSION_STACK_ARN,
            },
            {
                # lambda:GetFunction hands out a download URL for the code,
                # and the code is where the expected answer is written.
                'Sid': 'NoValidationFunctions',
                'Effect': 'Deny',
                'Action': 'lambda:*',
                'Resource': VALIDATION_FUNCTION_ARN,
            },
            {
                'Sid': 'NoValidationLogs',
                'Effect': 'Deny',
                'Action': [
                    'logs:GetLogEvents',
                    'logs:FilterLogEvents',
                    'logs:DescribeLogStreams',
                    'logs:StartLiveTail',
                    'logs:StartQuery',
                ],
                'Resource': [VALIDATION_LOG_GROUP_ARN, f'{VALIDATION_LOG_GROUP_ARN}:*'],
            },
            {
                # Credentials that would outlive the session and the nuke.
                'Sid': 'NoIdentityOfTheirOwn',
                'Effect': 'Deny',
                'Action': [
                    'iam:CreateUser',
                    'iam:CreateAccessKey',
                    'iam:CreateLoginProfile',
                    'iam:UpdateLoginProfile',
                    'iam:CreateServiceSpecificCredential',
                    'iam:UploadSSHPublicKey',
                    'iam:UploadSigningCertificate',
                ],
                'Resource': '*',
            },
            {
                # Including this role: a player cannot drop their own
                # guardrails, because this Deny is evaluated on that request.
                'Sid': 'ProtectTheWayIn',
                'Effect': 'Deny',
                'Action': [
                    'iam:DeleteRole',
                    'iam:UpdateRole',
                    'iam:UpdateAssumeRolePolicy',
                    'iam:PutRolePolicy',
                    'iam:DeleteRolePolicy',
                    'iam:AttachRolePolicy',
                    'iam:DetachRolePolicy',
                    'iam:PutRolePermissionsBoundary',
                    'iam:DeleteRolePermissionsBoundary',
                ],
                'Resource': PROTECTED_ROLES,
            },
            {
                'Sid': 'NoLeavingAndNoBilling',
                'Effect': 'Deny',
                'Action': [
                    'organizations:*',
                    'account:*',
                    'billing:*',
                    'payments:*',
                    'budgets:*',
                    'ce:*',
                    'aws-portal:*',
                ],
                'Resource': '*',
            },
        ],
    }


def _trust_policy(trusted_account):
    """
    Who may assume the role.

    The orchestrator account, for now: the login button will have the backend
    assume this role and hand the player a console session. When that moves to
    Cognito this is the one place that changes.
    """
    return {
        'Version': '2012-10-17',
        'Statement': [
            {
                'Effect': 'Allow',
                'Principal': {'AWS': f'arn:aws:iam::{trusted_account}:root'},
                'Action': 'sts:AssumeRole',
            }
        ],
    }


def _compact(document):
    """IAM caps an inline policy at 10240 characters; don't spend them on whitespace."""
    return json.dumps(document, separators=(',', ':'))


def ensure_team_role(iam_client, trusted_account):
    """
    Make sure the role exists in this account with current guardrails.

    Idempotent, and safe to call on every session start: an account
    provisioned before this existed heals itself the first time it is used.

    Returns:
        bool: True if the role had to be created
    """
    created = False

    try:
        iam_client.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=_compact(_trust_policy(trusted_account)),
            Description='Role players use to work in their game account',
            MaxSessionDuration=MAX_SESSION_SECONDS,
        )
        created = True
    except ClientError as e:
        if e.response['Error']['Code'] != 'EntityAlreadyExists':
            raise
        # The system owns this role, so the trust policy is refreshed rather
        # than left at whatever a previous version of this code wrote.
        iam_client.update_assume_role_policy(
            RoleName=ROLE_NAME,
            PolicyDocument=_compact(_trust_policy(trusted_account)),
        )

    iam_client.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName=GUARDRAIL_POLICY_NAME,
        PolicyDocument=_compact(guardrail_policy()),
    )

    return created


def load_game_policy(s3_client, bucket, game_id):
    """
    The game's own permissions, from s3://<bucket>/<game_id>/iam.json.

    A game with no iam.json gets DEFAULT_GAME_POLICY. A game whose iam.json is
    not valid JSON raises instead: that is a broken game to fix, and quietly
    handing the player administrator access would be the worse answer.
    """
    key = f'{game_id}/iam.json'

    try:
        body = s3_client.get_object(Bucket=bucket, Key=key)['Body'].read()
    except ClientError as e:
        if e.response['Error']['Code'] in ('NoSuchKey', 'NoSuchBucket', '404'):
            print(f"No {key}, falling back to the default game policy")
            return DEFAULT_GAME_POLICY
        raise

    return json.loads(body)


def put_game_policy(iam_client, document):
    """Replace the player's game-specific permissions."""
    iam_client.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName=GAME_POLICY_NAME,
        PolicyDocument=_compact(document),
    )
