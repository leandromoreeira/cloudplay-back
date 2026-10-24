"""
The catalogue listing.

Two things happen here that nothing else in the API does: the per-user access
filter runs after DynamoDB has already answered, and game ids are sorted by
the number inside them rather than as text. Both are easy to undo by accident.
"""

import json

import pytest

from tests.conftest import load_handler

PLAYER = 'player@example.com'


@pytest.fixture
def handler(games_table, access_table):
    # The module grabs its table at import time, so the fixtures above have to
    # have run first.
    return load_handler('get_games')


def given_games(games_table, *games):
    for game in games:
        games_table.put_item(Item=game)


def a_game(game_id, title='Some game', category='Compute', difficulty='Easy'):
    return {
        'id': game_id,
        'title': title,
        'content': '<p>...</p>',
        'category': category,
        'difficulty': difficulty,
        'tasks': [{'id': 'task1', 'title': 'First', 'content': '<p>do it</p>'}],
    }


def a_request(claims=None, **query):
    return {
        'queryStringParameters': {k: str(v) for k, v in query.items()} or None,
        'requestContext': {'authorizer': {'claims': claims if claims is not None else {
            'email': PLAYER, 'cognito:groups': '[full-access]',
        }}},
    }


def listed(response):
    return json.loads(response['body'])


def ids_in(response):
    return [game['id'] for game in listed(response)['data']]


# --- who sees what ----------------------------------------------------------

def test_full_access_sees_every_game(handler, games_table):
    given_games(games_table, a_game('game1'), a_game('game2'))

    assert sorted(ids_in(handler.lambda_handler(a_request(), None))) == ['game1', 'game2']


def test_without_full_access_only_granted_games_come_back(handler, games_table, access_table):
    given_games(games_table, a_game('game1'), a_game('game2'), a_game('game3'))
    access_table.put_item(Item={'user_email': PLAYER, 'game_id': 'game2'})

    response = handler.lambda_handler(a_request(claims={'email': PLAYER}), None)

    assert ids_in(response) == ['game2']


def test_a_user_with_no_grants_sees_an_empty_catalogue(handler, games_table):
    given_games(games_table, a_game('game1'))

    response = handler.lambda_handler(a_request(claims={'email': PLAYER}), None)

    assert ids_in(response) == []


def test_the_totals_count_only_the_games_the_user_may_see(handler, games_table, access_table):
    given_games(games_table, *[a_game(f'game{n}') for n in range(1, 6)])
    access_table.put_item(Item={'user_email': PLAYER, 'game_id': 'game4'})

    body = listed(handler.lambda_handler(a_request(claims={'email': PLAYER}), None))

    # Counting before the filter would tell a player there are pages of games
    # waiting behind a grant they do not have.
    assert body['totalItems'] == 1
    assert body['totalPages'] == 1


# --- filters ----------------------------------------------------------------

def test_the_title_filter_matches_on_a_fragment(handler, games_table):
    given_games(
        games_table,
        a_game('game1', title='Where are my nodes'),
        a_game('game2', title='Broken pipeline'),
    )

    assert ids_in(handler.lambda_handler(a_request(title='nodes'), None)) == ['game1']


def test_category_and_difficulty_narrow_together(handler, games_table):
    given_games(
        games_table,
        a_game('game1', category='Compute', difficulty='Hard'),
        a_game('game2', category='Compute', difficulty='Easy'),
        a_game('game3', category='Storage', difficulty='Hard'),
    )

    response = handler.lambda_handler(a_request(category='Compute', difficulty='Hard'), None)

    assert ids_in(response) == ['game1']


# --- ordering and paging ----------------------------------------------------

def test_game_ids_sort_by_their_number_not_as_text(handler, games_table):
    given_games(games_table, a_game('game1'), a_game('game2'), a_game('game10'))

    # Sorted as text, game10 would sit between game1 and game2 and the
    # catalogue would read as nonsense from the second page onwards.
    assert ids_in(handler.lambda_handler(a_request(order='asc'), None)) == [
        'game1', 'game2', 'game10'
    ]


def test_the_default_order_is_newest_first(handler, games_table):
    given_games(games_table, a_game('game1'), a_game('game2'), a_game('game10'))

    assert ids_in(handler.lambda_handler(a_request(), None)) == ['game10', 'game2', 'game1']


def test_a_page_holds_only_its_own_slice(handler, games_table):
    given_games(games_table, *[a_game(f'game{n}') for n in range(1, 6)])

    body = listed(handler.lambda_handler(a_request(order='asc', page=2, pageSize=2), None))

    assert [game['id'] for game in body['data']] == ['game3', 'game4']
    assert body['currentPage'] == 2
    assert body['totalPages'] == 3
    assert body['totalItems'] == 5


def test_a_page_past_the_end_is_empty_rather_than_an_error(handler, games_table):
    given_games(games_table, a_game('game1'))

    body = listed(handler.lambda_handler(a_request(page=9), None))

    assert body['data'] == []
