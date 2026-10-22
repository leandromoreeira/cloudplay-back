"""
Starting a session: claim an account, make the player's role ready, deploy.

The role is set up before the lab on purpose, so a failure there costs
nothing — these are the tests that say so.
"""

import json

import pytest
from botocore.exceptions import ClientError

from tests.conftest import ACCOUNT_A, load_handler

GAME_ID = 'game1'


class FakeCloudFormation:
    """Stands in for the game account's CloudFormation; moto would want to fetch the TemplateURL."""

    def __init__(self):
        self.stacks = []

    def create_stack(self, **kwargs):
        self.stacks.append(kwargs)
        return {'StackId': f"arn:aws:cloudformation:us-east-1:1:stack/{kwargs['StackName']}/abc"}


@pytest.fixture
def cloudformation():
    return FakeCloudFormation()


@pytest.fixture
def handler(sessions_table, accounts_table, access_table, games_bucket,
            cloudformation, monkeypatch):
    import boto3
    import account_manager

    monkeypatch.setenv('AWS_REGION', 'us-east-1')

    # Cross-account hops are STS's job, not this handler's: point both at the
    # mocked account so the test is about what the handler does with them.
    monkeypatch.setattr(
        account_manager.AccountManager, 'create_cross_account_client',
        lambda self, account_id, service_name: boto3.client(service_name)
    )
    monkeypatch.setattr(
        account_manager.AccountManager, 'create_cross_account_clients',
        lambda self, account_id: cloudformation
    )

    accounts_table.put_item(Item={'account_id': ACCOUNT_A, 'status': 'AVAILABLE'})
    return load_handler('start_game_session')


def a_request(game_id=GAME_ID):
    return {
        'pathParameters': {'gameId': game_id},
        'requestContext': {
            'authorizer': {'claims': {
                'email': 'player@example.com',
                'cognito:groups': '[full-access]',
            }},
        },
    }


def put_iam_json(bucket, game_id, document):
    import boto3
    boto3.client('s3').put_object(
        Bucket=bucket, Key=f'{game_id}/iam.json', Body=json.dumps(document)
    )


def role_policy(name):
    import boto3
    import team_role
    document = boto3.client('iam').get_role_policy(
        RoleName=team_role.ROLE_NAME, PolicyName=name
    )['PolicyDocument']
    return json.loads(document) if isinstance(document, str) else document


def test_the_player_role_is_ready_before_the_lab_is(handler, cloudformation, accounts_table):
    import boto3
    import team_role

    response = handler.lambda_handler(a_request(), None)

    assert response['statusCode'] == 200
    assert len(cloudformation.stacks) == 1
    names = boto3.client('iam').list_role_policies(RoleName=team_role.ROLE_NAME)['PolicyNames']
    assert sorted(names) == sorted([team_role.GUARDRAIL_POLICY_NAME, team_role.GAME_POLICY_NAME])
    assert accounts_table.get_item(
        Key={'account_id': ACCOUNT_A})['Item']['status'] == 'IN_USE'


def test_the_games_own_iam_json_becomes_the_game_policy(handler, games_bucket):
    import team_role

    document = {
        'Version': '2012-10-17',
        'Statement': [{'Sid': 'OnlyEks', 'Effect': 'Allow', 'Action': 'eks:*', 'Resource': '*'}],
    }
    put_iam_json(games_bucket, GAME_ID, document)

    handler.lambda_handler(a_request(), None)

    assert role_policy(team_role.GAME_POLICY_NAME) == document


def test_a_game_without_iam_json_still_starts(handler, games_bucket):
    import team_role

    response = handler.lambda_handler(a_request('game-with-nothing'), None)

    assert response['statusCode'] == 200
    assert role_policy(team_role.GAME_POLICY_NAME) == team_role.DEFAULT_GAME_POLICY


def test_the_session_records_the_account_it_landed_in(handler, sessions_table):
    body = json.loads(handler.lambda_handler(a_request(), None)['body'])

    session = sessions_table.get_item(Key={'session_id': body['sessionId']})['Item']
    assert session['account_id'] == ACCOUNT_A
    assert session['active'] is True
    assert session['game_id'] == GAME_ID


def test_a_role_that_cannot_be_set_up_costs_nothing(
    handler, accounts_table, sessions_table, cloudformation, monkeypatch
):
    def refuse(*args, **kwargs):
        raise ClientError(
            {'Error': {'Code': 'AccessDenied', 'Message': 'not allowed'}}, 'PutRolePolicy'
        )

    monkeypatch.setattr(handler.team_role, 'put_game_policy', refuse)

    response = handler.lambda_handler(a_request(), None)

    assert response['statusCode'] == 500
    # Nothing was deployed, so the account goes straight back.
    assert accounts_table.get_item(
        Key={'account_id': ACCOUNT_A})['Item']['status'] == 'AVAILABLE'
    assert cloudformation.stacks == []
    assert sessions_table.scan()['Items'] == []


def test_a_broken_iam_json_does_not_start_a_session(
    handler, games_bucket, accounts_table, cloudformation
):
    import boto3

    boto3.client('s3').put_object(
        Bucket=games_bucket, Key=f'{GAME_ID}/iam.json', Body='{ not json'
    )

    response = handler.lambda_handler(a_request(), None)

    # Better a game that visibly fails to start than a player quietly handed
    # the allow-everything default.
    assert response['statusCode'] == 500
    assert cloudformation.stacks == []
    assert accounts_table.get_item(
        Key={'account_id': ACCOUNT_A})['Item']['status'] == 'AVAILABLE'
