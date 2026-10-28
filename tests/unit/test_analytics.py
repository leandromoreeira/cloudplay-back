"""
The numbers, and who is allowed to see them.

Split in two: the arithmetic, which is a pure function of rows, and the
handler, whose only interesting decision is scope.
"""

import json

import pytest

from tests.conftest import load_handler

PLAYER = 'player@example.com'
OTHER = 'other@example.com'
ADMIN = 'admin@example.com'


@pytest.fixture
def agg():
    import analytics
    return analytics


def a_session(user=PLAYER, game_id='game1', start='2026-10-25T10:00:00Z',
              end='2026-10-25T11:00:00Z', reason='ended by player',
              status='CLEANUP_COMPLETE', tasks=None, active=False):
    session = {
        'session_id': f'{user}-{game_id}-{start}',
        'user_name': user,
        'game_id': game_id,
        'start_time': start,
        'active': active,
        'stack_status': status,
        'completed_tasks': tasks or {},
    }
    if end:
        session['end_time'] = end
    if reason:
        session['ended_reason'] = reason
    return session


def done(**seconds):
    return {
        task: {'completed_at': '2026-10-25T10:30:00Z', 'seconds': value}
        for task, value in seconds.items()
    }


# --- duration and outcome ---------------------------------------------------

def test_a_session_still_running_has_no_duration(agg):
    assert agg.duration_seconds(a_session(end=None, active=True)) is None


def test_a_session_from_before_end_time_existed_has_none(agg):
    # Counting it as zero would drag every median towards nothing.
    assert agg.duration_seconds({'start_time': '2026-10-25T10:00:00Z'}) is None


def test_duration_is_the_gap_between_the_two_stamps(agg):
    assert agg.duration_seconds(a_session()) == 3600


@pytest.mark.parametrize('session, expected', [
    (a_session(), 'ended'),
    (a_session(reason='expired by janitor after 3.2h'), 'expired'),
    (a_session(status='CLEANUP_FAILED'), 'cleanup_failed'),
    (a_session(reason=None), 'unknown'),
    (a_session(end=None, active=True), 'running'),
])
def test_each_ending_is_named(agg, session, expected):
    assert agg.outcome(session) == expected


# --- the headline numbers ---------------------------------------------------

def test_the_summary_counts_what_happened(agg):
    sessions = [
        a_session(game_id='game1', tasks=done(task1=600, task2=300)),
        a_session(game_id='game2', reason='expired by janitor after 3.2h'),
        a_session(game_id='game1'),
    ]

    summary = agg.summarise(sessions, completed_games=1)

    assert summary['sessions'] == 3
    assert summary['games_played'] == 2  # game1 twice is still one game
    assert summary['games_completed'] == 1
    assert summary['tasks_completed'] == 2
    assert summary['ended_by_player'] == 2
    assert summary['expired_by_janitor'] == 1
    assert summary['median_session_seconds'] == 3600


def test_a_running_session_does_not_distort_the_median(agg):
    sessions = [a_session(), a_session(end=None, active=True)]

    assert agg.summarise(sessions, 0)['median_session_seconds'] == 3600


# --- per game ---------------------------------------------------------------

def test_per_game_counts_players_not_sessions_as_completions(agg):
    sessions = [a_session(game_id='game1'), a_session(game_id='game1')]

    rows = agg.per_game(sessions, {'game1': 'A game'}, [('game1', PLAYER)])

    # Played twice, finished once. Replaying must not inflate it.
    assert rows[0]['sessions'] == 2
    assert rows[0]['completions'] == 1
    assert rows[0]['title'] == 'A game'


def test_per_game_is_ordered_by_how_much_it_is_played(agg):
    sessions = [a_session(game_id='game1'), a_session(game_id='game2'),
                a_session(game_id='game2')]

    rows = agg.per_game(sessions, {}, [])

    assert [r['game_id'] for r in rows] == ['game2', 'game1']


# --- per task, the reason this exists ---------------------------------------

def test_per_task_shows_where_the_time_goes(agg):
    sessions = [
        a_session(tasks=done(task1=1800, task2=79)),
        a_session(tasks=done(task1=1900, task2=120)),
    ]

    rows = {r['task_id']: r for r in agg.per_task(sessions, {})}

    # The shape that prompted this: one task eating the session and the
    # next one finishing in a minute.
    assert rows['task1']['median_seconds'] == 1850
    assert rows['task2']['median_seconds'] == 99
    assert rows['task1']['completions'] == 2


def test_a_task_nobody_finished_does_not_appear(agg):
    assert agg.per_task([a_session()], {}) == []


# --- per user ---------------------------------------------------------------

def test_per_user_separates_the_players(agg):
    sessions = [
        a_session(user=PLAYER, game_id='game1', tasks=done(task1=60)),
        a_session(user=PLAYER, game_id='game2'),
        a_session(user=OTHER, game_id='game1'),
    ]

    rows = {r['user']: r for r in agg.per_user(sessions, [('game1', PLAYER)])}

    assert rows[PLAYER]['sessions'] == 2
    assert rows[PLAYER]['games_played'] == 2
    assert rows[PLAYER]['games_completed'] == 1
    assert rows[PLAYER]['tasks_completed'] == 1
    assert rows[OTHER]['games_completed'] == 0


# --- the handler, which is all about scope ----------------------------------

@pytest.fixture
def handler(sessions_table, progress_table, games_table):
    return load_handler('analytics')


def a_request(caller=PLAYER, groups='[]', user=None):
    return {
        'queryStringParameters': {'user': user} if user else None,
        'requestContext': {'authorizer': {'claims': {
            'email': caller, 'cognito:groups': groups,
        }}},
    }


def body_of(response):
    return json.loads(response['body'])


def given_sessions(sessions_table, *sessions):
    for session in sessions:
        sessions_table.put_item(Item=session)


def test_a_request_without_an_identity_is_rejected(handler):
    assert handler.lambda_handler({'requestContext': {}}, None)['statusCode'] == 401


def test_a_player_sees_only_their_own_rows(handler, sessions_table):
    given_sessions(sessions_table, a_session(user=PLAYER), a_session(user=OTHER))

    body = body_of(handler.lambda_handler(a_request(), None))

    assert body['scope'] == 'user'
    assert body['summary']['sessions'] == 1
    assert {r['user'] for r in body['recent']} == {PLAYER}


def test_a_player_cannot_ask_about_someone_else(handler, sessions_table):
    given_sessions(sessions_table, a_session(user=OTHER))

    response = handler.lambda_handler(a_request(user=OTHER), None)

    # The page offers no such control, so reaching here means the URL was
    # edited by hand.
    assert response['statusCode'] == 403


def test_a_player_is_never_told_who_else_exists(handler, sessions_table):
    given_sessions(sessions_table, a_session(user=PLAYER), a_session(user=OTHER))

    assert 'per_user' not in body_of(handler.lambda_handler(a_request(), None))


def test_an_admin_sees_everyone(handler, sessions_table):
    given_sessions(sessions_table, a_session(user=PLAYER), a_session(user=OTHER))

    body = body_of(handler.lambda_handler(a_request(caller=ADMIN, groups='[admin]'), None))

    assert body['scope'] == 'all'
    assert body['summary']['sessions'] == 2
    assert {r['user'] for r in body['per_user']} == {PLAYER, OTHER}


def test_an_admin_can_narrow_to_one_student(handler, sessions_table):
    given_sessions(sessions_table, a_session(user=PLAYER), a_session(user=OTHER))

    body = body_of(handler.lambda_handler(
        a_request(caller=ADMIN, groups='[admin]', user=OTHER), None))

    assert body['scope'] == 'user'
    assert body['user'] == OTHER
    assert body['summary']['sessions'] == 1


def test_completions_come_from_the_progress_table(handler, sessions_table, progress_table):
    given_sessions(sessions_table, a_session(user=PLAYER, game_id='game1'))
    progress_table.put_item(Item={
        'user_email': PLAYER, 'game_id': 'game1',
        'ever_completed_at': '2026-10-25T11:00:00Z',
    })

    body = body_of(handler.lambda_handler(a_request(), None))

    # A game finished in an earlier session is still finished, which the
    # sessions table alone cannot say.
    assert body['summary']['games_completed'] == 1


def test_titles_come_from_the_catalogue(handler, sessions_table, games_table):
    given_sessions(sessions_table, a_session(game_id='game1'))
    games_table.put_item(Item={'id': 'game1', 'title': 'Where are my nodes'})

    body = body_of(handler.lambda_handler(a_request(), None))

    assert body['per_game'][0]['title'] == 'Where are my nodes'
