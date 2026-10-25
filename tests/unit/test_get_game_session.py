"""
Polling a session.

The one behaviour worth pinning here is what happens before CloudFormation
has been asked for anything: the handler treats a missing stack as a
finished teardown, which is right once a stack existed and wrong while one
is still on its way.
"""

import json

import pytest
from botocore.exceptions import ClientError

from tests.conftest import ACCOUNT_A, load_handler

GAME_ID = 'game1'
SESSION_ID = 'session-1'


class MissingStack:
    """
    A game account whose stack does not exist.

    Hand-rolled rather than moto: real CloudFormation answers describe_stacks
    for an unknown stack with ValidationError, which is the exact code this
    handler keys off, and moto answers with something else.
    """

    def __init__(self):
        self.describe_calls = 0

    def describe_stacks(self, StackName):
        self.describe_calls += 1
        raise ClientError(
            {'Error': {'Code': 'ValidationError',
                       'Message': f'Stack with id {StackName} does not exist'}},
            'DescribeStacks'
        )


@pytest.fixture
def cloudformation():
    return MissingStack()


@pytest.fixture
def handler(sessions_table, accounts_table, access_table, cloudformation, monkeypatch):
    import account_manager

    monkeypatch.setattr(
        account_manager.AccountManager, 'create_cross_account_clients',
        lambda self, account_id: cloudformation
    )
    return load_handler('get_game_session')


def a_request():
    return {
        'pathParameters': {'gameId': GAME_ID},
        'requestContext': {'authorizer': {'claims': {
            'email': 'player@example.com',
            'cognito:groups': '[full-access]',
        }}},
    }


def given_session(sessions_table, stack_status):
    sessions_table.put_item(Item={
        'session_id': SESSION_ID,
        'user_name': 'player@example.com',
        'game_id': GAME_ID,
        'account_id': ACCOUNT_A,
        'active': True,
        'start_time': '2026-10-23T22:17:50Z',
        'stack_status': stack_status,
    })


def body_of(response):
    return json.loads(response['body'])


def test_a_starting_session_is_not_mistaken_for_a_finished_one(
    handler, sessions_table, cloudformation
):
    given_session(sessions_table, 'STARTING')

    body = body_of(handler.lambda_handler(a_request(), None))

    # The stack genuinely does not exist yet. Closing the session here would
    # kill a game the player is still waiting on.
    assert body['active_session'] is True
    assert body['stack_status'] == 'STARTING'
    assert sessions_table.get_item(Key={'session_id': SESSION_ID})['Item']['active'] is True
    # Not even asked: there is nothing to ask about.
    assert cloudformation.describe_calls == 0


def test_a_stack_that_really_is_gone_closes_the_session(handler, sessions_table):
    given_session(sessions_table, 'CREATE_COMPLETE')

    body = body_of(handler.lambda_handler(a_request(), None))

    assert body['stack_status'] == 'DELETE_COMPLETE'
    assert sessions_table.get_item(Key={'session_id': SESSION_ID})['Item']['active'] is False


def test_the_response_carries_nothing_the_screen_does_not_need(handler, sessions_table):
    given_session(sessions_table, 'CREATE_COMPLETE')

    body = body_of(handler.lambda_handler(a_request(), None))

    # Which AWS account a game runs in, and the internal session id, are
    # ours to know, and neither was ever rendered.
    assert set(body) == {
        'active_session', 'start_time', 'stack_status', 'stack_outputs',
        'completed_tasks',
    }


def test_no_session_is_reported_plainly(handler):
    body = body_of(handler.lambda_handler(a_request(), None))

    assert body['active_session'] is False


def test_the_board_for_this_attempt_comes_back_with_the_session(handler, sessions_table):
    given_session(sessions_table, 'CREATE_COMPLETE')
    sessions_table.update_item(
        Key={'session_id': SESSION_ID},
        UpdateExpression='SET completed_tasks = :done',
        ExpressionAttributeValues={':done': {
            'task1': {'completed_at': '2026-10-25T10:05:00Z', 'seconds': 300},
        }},
    )

    body = body_of(handler.lambda_handler(a_request(), None))

    # Decimal straight off DynamoDB would have failed json.dumps, so the
    # seconds have to arrive as a plain number.
    assert body['completed_tasks'] == {
        'task1': {'completed_at': '2026-10-25T10:05:00Z', 'seconds': 300}
    }


def test_a_fresh_session_has_an_empty_board(handler, sessions_table):
    given_session(sessions_table, 'CREATE_COMPLETE')

    assert body_of(handler.lambda_handler(a_request(), None))['completed_tasks'] == {}
