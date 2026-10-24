"""
Fetching one game.

The behaviour worth holding onto is that "does not exist" and "not yours"
are the same answer, so nobody can map the catalogue by trying ids.
"""

import json

import pytest

from tests.conftest import load_handler

PLAYER = 'player@example.com'
GAME_ID = 'game1'


@pytest.fixture
def handler(games_table, access_table):
    return load_handler('get_game_by_id')


def given_game(games_table, game_id=GAME_ID):
    games_table.put_item(Item={
        'id': game_id,
        'title': 'Where are my nodes',
        'content': '<p>...</p>',
        'category': 'Compute',
        'difficulty': 'Hard',
        'tasks': [{'id': 'task1', 'title': 'First', 'content': '<p>do it</p>'}],
    })


def a_request(claims, game_id=GAME_ID):
    return {
        'pathParameters': {'gameId': game_id},
        'requestContext': {'authorizer': {'claims': claims}},
    }


def full_access():
    return {'email': PLAYER, 'cognito:groups': '[full-access]'}


def body_of(response):
    return json.loads(response['body'])


def test_full_access_gets_the_whole_game(handler, games_table):
    given_game(games_table)

    response = handler.lambda_handler(a_request(full_access()), None)

    assert response['statusCode'] == 200
    assert body_of(response)['id'] == GAME_ID
    assert body_of(response)['tasks'][0]['id'] == 'task1'


def test_a_granted_game_opens(handler, games_table, access_table):
    given_game(games_table)
    access_table.put_item(Item={'user_email': PLAYER, 'game_id': GAME_ID})

    response = handler.lambda_handler(a_request({'email': PLAYER}), None)

    assert response['statusCode'] == 200


def test_a_game_that_exists_but_is_not_granted_reads_as_missing(handler, games_table):
    given_game(games_table)

    response = handler.lambda_handler(a_request({'email': PLAYER}), None)

    assert response['statusCode'] == 404
    # Byte for byte what an unknown id returns, below: the difference between
    # "no such game" and "not for you" is exactly what a prober is after.
    assert body_of(response) == {'error': 'Game not found'}


def test_an_unknown_id_is_the_same_404(handler):
    response = handler.lambda_handler(a_request(full_access(), game_id='game999'), None)

    assert response['statusCode'] == 404
    assert body_of(response) == {'error': 'Game not found'}


def test_a_request_without_claims_gets_nothing(handler, games_table):
    given_game(games_table)

    response = handler.lambda_handler(
        {'pathParameters': {'gameId': GAME_ID}, 'requestContext': {}}, None
    )

    assert response['statusCode'] == 404
