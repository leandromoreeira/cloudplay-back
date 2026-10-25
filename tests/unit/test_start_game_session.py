"""
Starting a session: claim an account, make the player's role ready, deploy.

The role is set up before the lab on purpose, so a failure there costs
nothing — these are the tests that say so.
"""

import json

import pytest
from botocore.exceptions import ClientError

from tests.conftest import ACCOUNT_A, ACCOUNT_B, load_handler

TOPIC_ARN = 'arn:aws:sns:us-east-1:673000199571:test-stack-events'
GAME_ID = 'game1'


class FakeCloudFormation:
    """Stands in for the game account's CloudFormation; moto would want to fetch the TemplateURL."""

    def __init__(self, sessions_table=None):
        self.stacks = []
        self.sessions_when_called = []
        self._sessions_table = sessions_table

    def create_stack(self, **kwargs):
        self.stacks.append(kwargs)
        # Snapshot of the sessions table at the moment the slow call starts,
        # which is the window a second click used to arrive in.
        if self._sessions_table is not None:
            self.sessions_when_called.append(self._sessions_table.scan()['Items'])
        return {'StackId': f"arn:aws:cloudformation:us-east-1:1:stack/{kwargs['StackName']}/abc"}


@pytest.fixture
def cloudformation(sessions_table):
    return FakeCloudFormation(sessions_table)


@pytest.fixture
def handler(sessions_table, accounts_table, access_table, games_bucket,
            progress_table, cloudformation, monkeypatch):
    import boto3
    import account_manager

    monkeypatch.setenv('AWS_REGION', 'us-east-1')
    monkeypatch.setenv('STACK_EVENTS_TOPIC_ARN', TOPIC_ARN)

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


def test_the_session_exists_before_cloudformation_is_asked(
    handler, accounts_table, cloudformation
):
    """
    The fix for the triple-start, stated directly.

    The session row used to be written only after CloudFormation accepted
    the stack, so for those seconds the "already in progress" query had
    nothing to find.
    """
    handler.lambda_handler(a_request(), None)

    during = cloudformation.sessions_when_called[0]
    assert len(during) == 1
    assert during[0]['stack_status'] == 'STARTING'
    assert during[0]['active'] is True


def test_the_account_is_recorded_before_the_stack_exists(
    handler, accounts_table, cloudformation
):
    # Otherwise a crash here leaves the account IN_USE with nothing pointing
    # at it, and the janitor has no way to find it.
    handler.lambda_handler(a_request(), None)

    assert cloudformation.sessions_when_called[0][0]['account_id'] == ACCOUNT_A


def test_an_empty_pool_leaves_no_session_behind(
    handler, accounts_table, sessions_table, cloudformation
):
    accounts_table.put_item(Item={'account_id': ACCOUNT_A, 'status': 'IN_USE'})

    response = handler.lambda_handler(a_request(), None)

    assert response['statusCode'] == 503
    assert sessions_table.scan()['Items'] == []
    assert cloudformation.stacks == []


def test_a_second_click_does_not_win_a_second_account(
    handler, accounts_table, sessions_table, cloudformation
):
    """
    Three clicks a second apart once took all three accounts in the pool.

    Each request only wrote its session row after CloudFormation accepted
    the stack, so every one of them looked first, saw nothing running, and
    helped itself to an account.
    """
    accounts_table.put_item(Item={'account_id': ACCOUNT_B, 'status': 'AVAILABLE'})

    first = handler.lambda_handler(a_request(), None)
    second = handler.lambda_handler(a_request(), None)

    assert first['statusCode'] == 200
    assert second['statusCode'] == 400
    assert 'already have this game' in json.loads(second['body'])['message']
    assert len(cloudformation.stacks) == 1
    assert accounts_table.get_item(
        Key={'account_id': ACCOUNT_B})['Item']['status'] == 'AVAILABLE'


def test_a_different_game_is_still_allowed(handler, accounts_table, cloudformation):
    accounts_table.put_item(Item={'account_id': ACCOUNT_B, 'status': 'AVAILABLE'})

    handler.lambda_handler(a_request(), None)
    other = handler.lambda_handler(a_request('game99'), None)

    assert other['statusCode'] == 200
    assert len(cloudformation.stacks) == 2


def test_ending_the_session_lets_the_player_start_it_again(
    handler, accounts_table, sessions_table, cloudformation, cleanup_project
):
    import session_teardown
    from account_manager import get_account_manager

    body = json.loads(handler.lambda_handler(a_request(), None)['body'])
    session = sessions_table.get_item(Key={'session_id': body['sessionId']})['Item']
    session_teardown.end_session(sessions_table, session, get_account_manager())

    accounts_table.put_item(Item={'account_id': ACCOUNT_B, 'status': 'AVAILABLE'})
    again = handler.lambda_handler(a_request(), None)

    assert again['statusCode'] == 200


def test_a_start_that_fails_does_not_block_the_next_one(
    handler, accounts_table, cloudformation, monkeypatch
):
    real_put = handler.team_role.put_game_policy
    attempts = []

    def fail_the_first_time(iam_client, document):
        attempts.append(1)
        if len(attempts) == 1:
            raise ClientError(
                {'Error': {'Code': 'AccessDenied', 'Message': 'not allowed'}}, 'PutRolePolicy'
            )
        return real_put(iam_client, document)

    monkeypatch.setattr(handler.team_role, 'put_game_policy', fail_the_first_time)

    assert handler.lambda_handler(a_request(), None)['statusCode'] == 500
    # The lock has to come off with the account, or one bad start would lock
    # the player out of that game until someone noticed.
    assert handler.lambda_handler(a_request(), None)['statusCode'] == 200


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


def test_the_player_is_recorded_as_playing_once_the_lab_is_on_its_way(
    handler, progress_table
):
    handler.lambda_handler(a_request(), None)

    item = progress_table.get_item(
        Key={'user_email': 'player@example.com', 'game_id': GAME_ID}
    )['Item']
    assert item['playing'] is True
    assert item['last_session_id']


def test_a_start_that_never_happened_records_no_progress(
    handler, accounts_table, progress_table
):
    accounts_table.update_item(
        Key={'account_id': ACCOUNT_A},
        UpdateExpression='SET #s = :s',
        ExpressionAttributeNames={'#s': 'status'},
        ExpressionAttributeValues={':s': 'IN_USE'},
    )

    response = handler.lambda_handler(a_request(), None)

    # Nothing was started, so nothing may claim the player is in a game.
    assert response['statusCode'] == 503
    assert 'Item' not in progress_table.get_item(
        Key={'user_email': 'player@example.com', 'game_id': GAME_ID}
    )


def test_the_session_starts_with_an_empty_board(handler, sessions_table):
    handler.lambda_handler(a_request(), None)

    session = sessions_table.scan()['Items'][0]
    # A replay of a finished game must not open showing the previous run.
    assert session['completed_tasks'] == {}


def test_cloudformation_is_told_where_to_report_back(handler, cloudformation):
    handler.lambda_handler(a_request(), None)

    # Without this the session row only learns what the stack is doing when
    # somebody asks, which is the whole point of the topic.
    assert cloudformation.stacks[0]['NotificationARNs'] == [TOPIC_ARN]
