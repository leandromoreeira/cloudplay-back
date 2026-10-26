"""
Who is allowed to read this API.

A star meant any page on the internet could call it with a logged-in
player's browser and read the catalogue, their progress and the state of
their AWS account. These pin that every handler answers with the one
configured origin instead.
"""

import json

import pytest

from tests.conftest import ALLOWED_ORIGIN, load_handler

PLAYER = 'player@example.com'


@pytest.fixture
def cors():
    import cors
    return cors


def test_the_origin_comes_from_the_environment(cors, monkeypatch):
    monkeypatch.setenv('ALLOWED_ORIGIN', 'https://somewhere.else')

    # Read when answering, not when the container started: a warm Lambda
    # must not keep serving the value it booted with.
    assert cors.allowed_origin() == 'https://somewhere.else'


def test_a_missing_origin_is_not_quietly_a_star(cors, monkeypatch):
    monkeypatch.delenv('ALLOWED_ORIGIN', raising=False)

    # Failing loudly beats shipping an open API because a variable was
    # forgotten in the template.
    with pytest.raises(KeyError):
        cors.allowed_origin()


# --- the handlers that answer the browser -----------------------------------

def test_the_catalogue_answers_with_the_configured_origin(games_table, access_table, progress_table):
    handler = load_handler('get_games')

    response = handler.lambda_handler({
        'queryStringParameters': None,
        'requestContext': {'authorizer': {'claims': {
            'email': PLAYER, 'cognito:groups': '[full-access]'}}},
    }, None)

    assert response['headers']['Access-Control-Allow-Origin'] == ALLOWED_ORIGIN


def test_even_a_refusal_carries_the_origin(sessions_table, accounts_table, access_table,
                                           cleanup_project, verified_sender, progress_table):
    handler = load_handler('end_game_session')

    # A 401 the browser cannot read is a 401 the front cannot react to.
    response = handler.lambda_handler(
        {'pathParameters': {'gameId': 'game1'}, 'requestContext': {}}, None
    )

    assert response['statusCode'] == 401
    assert response['headers']['Access-Control-Allow-Origin'] == ALLOWED_ORIGIN


def test_no_handler_still_ships_a_star():
    from pathlib import Path

    back = Path(__file__).resolve().parent.parent.parent
    offenders = [
        p.relative_to(back)
        for p in back.glob('games/*/app.py')
        if "'Access-Control-Allow-Origin': '*'" in p.read_text()
    ]

    assert offenders == []
