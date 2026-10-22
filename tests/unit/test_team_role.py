"""
HengHaTeamRole: the role a player logs into their game account with.

The guarantee under test is that the guardrails are a policy of their own,
separate from whatever the game allows, so the games with no iam.json — which
get allow-everything — are still fenced in.
"""

import json
import re

import pytest

TRUSTED_ACCOUNT = '673000199571'


@pytest.fixture
def iam(aws):
    import boto3
    return boto3.client('iam')


@pytest.fixture
def team_role():
    import team_role
    return team_role


def inline(iam, team_role, policy_name):
    document = iam.get_role_policy(
        RoleName=team_role.ROLE_NAME, PolicyName=policy_name
    )['PolicyDocument']
    # moto hands back whatever was stored; the real API decodes it for you.
    return json.loads(document) if isinstance(document, str) else document


def statement(document, sid):
    for item in document['Statement']:
        if item.get('Sid') == sid:
            return item
    raise AssertionError(f"no statement {sid} in {[s.get('Sid') for s in document['Statement']]}")


# --- room to grow -----------------------------------------------------------

def test_the_guardrails_leave_room_for_the_biggest_game_policy(team_role):
    """
    A role's inline policies share a budget of 10240 characters, whitespace
    not counted, and the guardrails spend part of it in every account. Growing
    them takes room away from what a game is allowed to ask for.

    The largest iam.json in the catalogue is game94's, at 5982 characters.
    """
    limit = 10240
    largest_game_policy = 5982

    guardrails = len(re.sub(r'\s', '', json.dumps(team_role.guardrail_policy())))

    assert guardrails + largest_game_policy <= limit, (
        f"guardrails are {guardrails} chars, which leaves "
        f"{limit - guardrails} for a game that may need {largest_game_policy}"
    )


# --- creating the role ------------------------------------------------------

def test_the_role_is_created_with_its_guardrails(iam, team_role):
    assert team_role.ensure_team_role(iam, TRUSTED_ACCOUNT) is True

    assert iam.list_role_policies(
        RoleName=team_role.ROLE_NAME
    )['PolicyNames'] == [team_role.GUARDRAIL_POLICY_NAME]


def test_the_role_trusts_the_orchestrator(iam, team_role):
    team_role.ensure_team_role(iam, TRUSTED_ACCOUNT)

    role = iam.get_role(RoleName=team_role.ROLE_NAME)['Role']
    trust = role['AssumeRolePolicyDocument']
    trust = json.loads(trust) if isinstance(trust, str) else trust

    assert trust['Statement'][0]['Principal']['AWS'] == f'arn:aws:iam::{TRUSTED_ACCOUNT}:root'


def test_calling_it_again_is_not_an_error(iam, team_role):
    team_role.ensure_team_role(iam, TRUSTED_ACCOUNT)

    # An account provisioned before this existed heals on its next session.
    assert team_role.ensure_team_role(iam, TRUSTED_ACCOUNT) is False


def test_a_second_call_refreshes_the_guardrails(iam, team_role, monkeypatch):
    team_role.ensure_team_role(iam, TRUSTED_ACCOUNT)

    monkeypatch.setattr(team_role, 'guardrail_policy', lambda: {
        'Version': '2012-10-17',
        'Statement': [{'Sid': 'Newer', 'Effect': 'Deny', 'Action': 's3:*', 'Resource': '*'}],
    })
    team_role.ensure_team_role(iam, TRUSTED_ACCOUNT)

    assert statement(inline(iam, team_role, team_role.GUARDRAIL_POLICY_NAME), 'Newer')


# --- what the guardrails actually say ---------------------------------------

@pytest.fixture
def guardrails(iam, team_role):
    team_role.ensure_team_role(iam, TRUSTED_ACCOUNT)
    return inline(iam, team_role, team_role.GUARDRAIL_POLICY_NAME)


def test_the_validation_lambdas_are_off_limits(guardrails):
    denied = statement(guardrails, 'NoValidationFunctions')

    assert denied['Effect'] == 'Deny'
    assert denied['Action'] == 'lambda:*'
    assert denied['Resource'] == 'arn:aws:lambda:*:*:function:validate_task*'


def test_so_are_their_logs(guardrails):
    denied = statement(guardrails, 'NoValidationLogs')

    assert denied['Effect'] == 'Deny'
    assert 'logs:FilterLogEvents' in denied['Action']
    assert 'logs:StartQuery' in denied['Action']
    assert all('validate_task' in r for r in denied['Resource'])


def test_the_lab_stack_cannot_be_changed_or_read(guardrails):
    changed = statement(guardrails, 'NoTouchingTheLabStack')
    read = statement(guardrails, 'NoReadingTheLabStack')

    assert 'cloudformation:DeleteStack' in changed['Action']
    # The template is the answer key.
    assert 'cloudformation:GetTemplate' in read['Action']
    for denied in (changed, read):
        assert denied['Resource'] == 'arn:aws:cloudformation:*:*:stack/game-session-*'


def test_no_users_and_no_access_keys(guardrails):
    denied = statement(guardrails, 'NoIdentityOfTheirOwn')

    assert 'iam:CreateUser' in denied['Action']
    assert 'iam:CreateAccessKey' in denied['Action']
    assert denied['Resource'] == '*'


def test_a_player_cannot_take_their_own_guardrails_off(guardrails, team_role):
    denied = statement(guardrails, 'ProtectTheWayIn')

    # This Deny is evaluated on the request that would remove it.
    assert 'iam:DeleteRolePolicy' in denied['Action']
    assert 'iam:PutRolePolicy' in denied['Action']
    assert f'arn:aws:iam::*:role/{team_role.ROLE_NAME}' in denied['Resource']


def test_the_orchestrators_way_in_is_protected_too(guardrails):
    denied = statement(guardrails, 'ProtectTheWayIn')

    assert 'arn:aws:iam::*:role/CloudPlayGameRole' in denied['Resource']
    assert 'arn:aws:iam::*:role/validation-checker-role' in denied['Resource']


# --- the per-game policy ----------------------------------------------------

def test_a_games_own_policy_is_read_from_s3(aws, iam, team_role, games_bucket):
    import boto3

    document = {
        'Version': '2012-10-17',
        'Statement': [{'Effect': 'Allow', 'Action': 'eks:*', 'Resource': '*'}],
    }
    boto3.client('s3').put_object(
        Bucket=games_bucket, Key='game1/iam.json', Body=json.dumps(document)
    )

    assert team_role.load_game_policy(boto3.client('s3'), games_bucket, 'game1') == document


def test_a_game_with_no_iam_json_gets_the_default(aws, team_role, games_bucket):
    import boto3

    loaded = team_role.load_game_policy(boto3.client('s3'), games_bucket, 'game999')

    assert loaded == team_role.DEFAULT_GAME_POLICY
    assert loaded['Statement'][0]['Action'] == '*'


def test_a_broken_iam_json_is_not_quietly_turned_into_admin(aws, team_role, games_bucket):
    import boto3

    boto3.client('s3').put_object(
        Bucket=games_bucket, Key='game2/iam.json', Body='{ this is not json'
    )

    with pytest.raises(ValueError):
        team_role.load_game_policy(boto3.client('s3'), games_bucket, 'game2')


def test_the_game_policy_lands_next_to_the_guardrails_not_over_them(iam, team_role):
    team_role.ensure_team_role(iam, TRUSTED_ACCOUNT)
    team_role.put_game_policy(iam, team_role.DEFAULT_GAME_POLICY)

    names = sorted(iam.list_role_policies(RoleName=team_role.ROLE_NAME)['PolicyNames'])

    # Allow-everything is only safe because the Deny is still a separate,
    # untouched policy on the same role.
    assert names == sorted([team_role.GUARDRAIL_POLICY_NAME, team_role.GAME_POLICY_NAME])
    assert statement(inline(iam, team_role, team_role.GUARDRAIL_POLICY_NAME),
                     'NoValidationFunctions')


def test_starting_a_new_game_replaces_only_the_game_policy(iam, team_role):
    team_role.ensure_team_role(iam, TRUSTED_ACCOUNT)
    team_role.put_game_policy(iam, {
        'Version': '2012-10-17',
        'Statement': [{'Sid': 'First', 'Effect': 'Allow', 'Action': 's3:*', 'Resource': '*'}],
    })
    team_role.put_game_policy(iam, {
        'Version': '2012-10-17',
        'Statement': [{'Sid': 'Second', 'Effect': 'Allow', 'Action': 'ec2:*', 'Resource': '*'}],
    })

    game = inline(iam, team_role, team_role.GAME_POLICY_NAME)
    assert [s['Sid'] for s in game['Statement']] == ['Second']
