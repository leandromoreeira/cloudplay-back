"""
The player's end button.

The handler itself is thin — it finds the caller's active session and hands
it to session_teardown — so these cover the finding and the answering, and
leave the teardown's own behaviour to test_session_teardown.
"""

import json

import pytest

from tests.conftest import ACCOUNT_A, load_handler

PLAYER = 'player@example.com'
GAME_ID = 'game1'
SESSION_ID = 'session-1'


@pytest.fixture
def handler(sessions_table, accounts_table, access_table, cleanup_project, verified_sender):
    return load_handler('end_game_session')


def given_session(sessions_table, accounts_table, active=True, account_id=ACCOUNT_A,
                  user_name=PLAYER, game_id=GAME_ID, session_id=SESSION_ID):
    item = {
        'session_id': session_id,
        'user_name': user_name,
        'game_id': game_id,
        'active': active,
        'start_time': '2026-10-24T10:00:00Z',
        'stack_status': 'CREATE_COMPLETE',
    }
    if account_id:
        item['account_id'] = account_id
        accounts_table.put_item(Item={'account_id': account_id, 'status': 'IN_USE'})
    sessions_table.put_item(Item=item)
    return item


def a_request(claims=None, game_id=GAME_ID):
    return {
        'pathParameters': {'gameId': game_id},
        'requestContext': {'authorizer': {'claims': claims if claims is not None else {
            'email': PLAYER, 'cognito:groups': '[full-access]',
        }}},
    }


def body_of(response):
    return json.loads(response['body'])


def stored(sessions_table, session_id=SESSION_ID):
    return sessions_table.get_item(Key={'session_id': session_id})['Item']


def account(accounts_table, account_id=ACCOUNT_A):
    return accounts_table.get_item(Key={'account_id': account_id})['Item']


# --- getting past the door --------------------------------------------------

def test_a_request_without_an_identity_is_rejected(handler):
    response = handler.lambda_handler(
        {'pathParameters': {'gameId': GAME_ID}, 'requestContext': {}}, None
    )

    assert response['statusCode'] == 401


def test_a_game_the_user_cannot_play_cannot_be_ended(handler, sessions_table, accounts_table):
    given_session(sessions_table, accounts_table)

    response = handler.lambda_handler(a_request(claims={'email': PLAYER}), None)

    assert response['statusCode'] == 403
    # Still running: a 403 must not have torn anything down on the way out.
    assert stored(sessions_table)['active'] is True


def test_with_nothing_running_there_is_nothing_to_end(handler):
    response = handler.lambda_handler(a_request(), None)

    assert response['statusCode'] == 404


def test_an_already_closed_session_is_not_found_again(handler, sessions_table, accounts_table):
    given_session(sessions_table, accounts_table, active=False)

    assert handler.lambda_handler(a_request(), None)['statusCode'] == 404


def test_another_players_session_is_not_visible(handler, sessions_table, accounts_table):
    given_session(sessions_table, accounts_table, user_name='someone.else@example.com')

    assert handler.lambda_handler(a_request(), None)['statusCode'] == 404


# --- the real thing ---------------------------------------------------------

def test_ending_closes_the_session_and_sends_the_account_to_be_wiped(
    handler, sessions_table, accounts_table
):
    given_session(sessions_table, accounts_table)

    response = handler.lambda_handler(a_request(), None)

    assert response['statusCode'] == 200
    assert body_of(response)['sessionId'] == SESSION_ID

    closed = stored(sessions_table)
    assert closed['active'] is False
    assert closed['stack_status'] == 'CLEANUP_IN_PROGRESS'
    assert closed['ended_reason'] == 'ended by player'
    assert closed['end_time']
    assert closed['cleanup_build_id']

    assert account(accounts_table)['status'] == 'CLEANING'


def test_a_session_that_never_got_an_account_still_closes(handler, sessions_table, accounts_table):
    given_session(sessions_table, accounts_table, account_id=None)

    response = handler.lambda_handler(a_request(), None)

    assert response['statusCode'] == 200
    assert stored(sessions_table)['active'] is False


def test_the_response_says_nothing_about_the_account(handler, sessions_table, accounts_table):
    given_session(sessions_table, accounts_table)

    body = body_of(handler.lambda_handler(a_request(), None))

    assert set(body) == {'message', 'sessionId'}


def test_a_cleanup_that_cannot_start_is_reported_and_quarantines_the_account(
    handler, sessions_table, accounts_table, monkeypatch
):
    given_session(sessions_table, accounts_table)
    monkeypatch.setenv('CLEANUP_PROJECT_NAME', 'a-project-that-does-not-exist')

    response = handler.lambda_handler(a_request(), None)

    # The session is over either way; what must not happen is the dirty
    # account drifting back into the pool as if nothing went wrong.
    assert response['statusCode'] == 500
    assert stored(sessions_table)['active'] is False
    assert account(accounts_table)['status'] == 'QUARANTINED'
