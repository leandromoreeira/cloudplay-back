"""
The endpoint behind the "open the AWS console" button.

Cognito has already said who the caller is by the time this runs. What it
adds is the part Cognito cannot know: whether this person still holds that
account right now.
"""

import json
from datetime import datetime, timezone

import pytest

from tests.conftest import ACCOUNT_A, ACCOUNT_B, load_handler

GAME_ID = 'game1'
SESSION_ID = 'session-1'
EXPIRES = datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def handler(sessions_table, accounts_table, access_table, monkeypatch):
    monkeypatch.setenv('FRONTEND_URL', 'https://play.example.com')
    module = load_handler('console_access')

    # The federation endpoint is not ours to call from a test; what matters
    # here is who gets as far as calling it.
    monkeypatch.setattr(
        module.console_access, 'player_credentials',
        lambda account_id, email, sts_client=None: {
            'AccessKeyId': 'A', 'SecretAccessKey': 'S',
            'SessionToken': 'T', 'Expiration': EXPIRES,
        }
    )
    monkeypatch.setattr(
        module.console_access, 'signin_url',
        lambda credentials, issuer, destination=None: f'https://signin.example/?issuer={issuer}'
    )
    return module


def a_request(game_id=GAME_ID, email='player@example.com', groups='[full-access]'):
    claims = {}
    if email:
        claims['email'] = email
    if groups:
        claims['cognito:groups'] = groups
    return {
        'pathParameters': {'gameId': game_id},
        'requestContext': {'authorizer': {'claims': claims}},
    }


def given_session(sessions_table, accounts_table, stack_status='CREATE_COMPLETE',
                  account_id=ACCOUNT_A, account_status='IN_USE', holder=SESSION_ID,
                  active=True):
    sessions_table.put_item(Item={
        'session_id': SESSION_ID,
        'user_name': 'player@example.com',
        'game_id': GAME_ID,
        'account_id': account_id,
        'active': active,
        'start_time': '2026-10-21T10:00:00Z',
        'stack_status': stack_status,
    })
    accounts_table.put_item(Item={
        'account_id': account_id,
        'status': account_status,
        'session_id': holder,
    })


def body_of(response):
    return json.loads(response['body'])


def test_a_caller_without_claims_is_turned_away(handler):
    assert handler.lambda_handler(a_request(email=None), None)['statusCode'] == 401


def test_a_game_the_player_has_no_grant_for_is_refused(handler, sessions_table, accounts_table):
    given_session(sessions_table, accounts_table)

    response = handler.lambda_handler(a_request(groups=None), None)

    assert response['statusCode'] == 403


def test_no_session_means_nothing_to_sign_in_to(handler):
    assert handler.lambda_handler(a_request(), None)['statusCode'] == 404


def test_a_lab_that_is_still_coming_up_is_not_ready(handler, sessions_table, accounts_table):
    given_session(sessions_table, accounts_table, stack_status='CREATE_IN_PROGRESS')

    response = handler.lambda_handler(a_request(), None)

    # The button only appears on CREATE_COMPLETE, but the client does not
    # get to be the one deciding that.
    assert response['statusCode'] == 409
    assert body_of(response)['stackStatus'] == 'CREATE_IN_PROGRESS'


def test_an_account_the_pool_has_moved_on_from_is_refused(handler, sessions_table, accounts_table):
    # The janitor expired the session and the account went back to the pool,
    # but the session row is still sitting there.
    given_session(sessions_table, accounts_table, account_status='AVAILABLE', holder=None)

    assert handler.lambda_handler(a_request(), None)['statusCode'] == 409


def test_an_account_now_held_by_someone_else_is_refused(handler, sessions_table, accounts_table):
    given_session(sessions_table, accounts_table, holder='a-different-session')

    assert handler.lambda_handler(a_request(), None)['statusCode'] == 409


def test_a_session_whose_account_vanished_is_refused(handler, sessions_table, accounts_table):
    given_session(sessions_table, accounts_table, account_id=ACCOUNT_A)
    accounts_table.delete_item(Key={'account_id': ACCOUNT_A})

    assert handler.lambda_handler(a_request(), None)['statusCode'] == 409


def test_a_player_holding_their_account_gets_a_url(handler, sessions_table, accounts_table):
    given_session(sessions_table, accounts_table)

    response = handler.lambda_handler(a_request(), None)
    body = body_of(response)

    assert response['statusCode'] == 200
    assert body['accountId'] == ACCOUNT_A
    assert body['expiresAt'] == EXPIRES.isoformat()
    assert body['consoleUrl'].startswith('https://signin.example/')


def test_the_console_is_told_where_to_send_them_when_the_hour_is_up(
    handler, sessions_table, accounts_table
):
    given_session(sessions_table, accounts_table)

    url = body_of(handler.lambda_handler(a_request(), None))['consoleUrl']

    # Carrying the game, because a player can have more than one running.
    assert 'https://play.example.com/reconnect?game=game1' in url
