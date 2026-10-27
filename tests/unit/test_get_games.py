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
def handler(games_table, access_table, progress_table):
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


# --- the chip ---------------------------------------------------------------

def test_every_game_carries_a_status(handler, games_table):
    given_games(games_table, a_game('game1'))

    body = listed(handler.lambda_handler(a_request(), None))

    assert body['data'][0]['progress'] == {'status': 'todo'}


def test_a_game_being_played_and_one_already_finished(handler, games_table, progress_table):
    import game_progress

    given_games(games_table, a_game('game1'), a_game('game2'), a_game('game3'))
    game_progress.mark_playing(PLAYER, 'game1', 'session-1')
    game_progress.mark_completed(PLAYER, 'game2')
    game_progress.mark_not_playing(PLAYER, 'game2')

    body = listed(handler.lambda_handler(a_request(order='asc'), None))
    by_id = {game['id']: game['progress']['status'] for game in body['data']}

    assert by_id == {'game1': 'in_progress', 'game2': 'done', 'game3': 'todo'}


def test_replaying_a_finished_game_shows_as_in_progress(handler, games_table, progress_table):
    import game_progress

    given_games(games_table, a_game('game1'))
    game_progress.mark_completed(PLAYER, 'game1')
    game_progress.mark_not_playing(PLAYER, 'game1')
    game_progress.mark_playing(PLAYER, 'game1', 'session-2')

    body = listed(handler.lambda_handler(a_request(), None))

    assert body['data'][0]['progress']['status'] == 'in_progress'


# --- filtering by where the player stands -----------------------------------

@pytest.fixture
def a_played_catalogue(handler, games_table, progress_table):
    """game1 in progress, game2 finished, game3 never touched."""
    import game_progress

    given_games(games_table, a_game('game1'), a_game('game2'), a_game('game3'))
    game_progress.mark_playing(PLAYER, 'game1', 'session-1')
    game_progress.mark_completed(PLAYER, 'game2')
    game_progress.mark_not_playing(PLAYER, 'game2')
    return handler


@pytest.mark.parametrize('status, expected', [
    ('in_progress', ['game1']),
    ('done', ['game2']),
    ('todo', ['game3']),
])
def test_each_status_returns_only_its_own(a_played_catalogue, status, expected):
    response = a_played_catalogue.lambda_handler(a_request(status=status), None)

    assert ids_in(response) == expected


def test_the_totals_describe_the_filtered_list(a_played_catalogue):
    body = listed(a_played_catalogue.lambda_handler(a_request(status='done'), None))

    # Filtering after paging would have left the counts describing a list
    # the player cannot see, and pages arriving short.
    assert body['totalItems'] == 1
    assert body['totalPages'] == 1
    assert len(body['data']) == 1


def test_a_game_never_touched_counts_as_todo(a_played_catalogue):
    # There is no progress row for game3 at all; the absence is the answer.
    assert ids_in(a_played_catalogue.lambda_handler(a_request(status='todo'), None)) == ['game3']


def test_the_status_filter_combines_with_the_others(handler, games_table, progress_table):
    import game_progress

    given_games(
        games_table,
        a_game('game1', difficulty='Hard'),
        a_game('game2', difficulty='Easy'),
    )
    game_progress.mark_completed(PLAYER, 'game1')
    game_progress.mark_completed(PLAYER, 'game2')
    game_progress.mark_not_playing(PLAYER, 'game1')
    game_progress.mark_not_playing(PLAYER, 'game2')

    response = handler.lambda_handler(a_request(status='done', difficulty='Hard'), None)

    assert ids_in(response) == ['game1']


def test_a_status_that_does_not_exist_is_refused(handler, games_table):
    given_games(games_table, a_game('game1'))

    response = handler.lambda_handler(a_request(status='finished'), None)

    # An unknown value would otherwise come back as an empty catalogue and
    # read like "you have played nothing".
    assert response['statusCode'] == 400
