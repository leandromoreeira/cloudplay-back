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
    # Operational, for the counters an operator reads.
    assert agg.ending(session) == expected


# --- what happened to the player, which is a different question -------------

def test_a_session_that_finished_every_task_reads_as_completed(agg):
    session = a_session(tasks=done(task1=60, task2=60))

    assert agg.outcome(session, task_count=2) == 'completed'


def test_a_session_that_stopped_short_reads_as_unfinished(agg):
    session = a_session(tasks=done(task1=60))

    assert agg.outcome(session, task_count=2) == 'unfinished'


def test_pressing_stop_is_not_the_same_as_finishing(agg):
    # ended_reason says they pressed the button; the tasks say they were
    # halfway. The column is about them, not about the button.
    session = a_session(reason='ended by player', tasks=done(task1=60))

    assert agg.outcome(session, task_count=3) == 'unfinished'


def test_a_failed_cleanup_says_nothing_about_the_player(agg):
    session = a_session(status='CLEANUP_FAILED', tasks=done(task1=60, task2=60))

    # The wipe afterwards broke; they still finished the game.
    assert agg.outcome(session, task_count=2) == 'completed'
    assert agg.ending(session) == 'cleanup_failed'


def test_a_running_session_is_running_whatever_it_has_done(agg):
    session = a_session(end=None, active=True, tasks=done(task1=60, task2=60))

    assert agg.outcome(session, task_count=2) == 'running'


def test_a_game_whose_task_count_is_unknown_is_not_called_completed(agg):
    # Better unfinished than a green tick we cannot justify.
    assert agg.outcome(a_session(tasks=done(task1=60)), task_count=None) == 'unfinished' 


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


# --- drilling into one game -------------------------------------------------

def test_a_game_can_be_asked_about_on_its_own(handler, sessions_table):
    given_sessions(sessions_table,
                   a_session(game_id='game1'), a_session(game_id='game2'))

    body = body_of(handler.lambda_handler(
        a_request(caller=ADMIN, groups='[admin]'), None))
    assert body['summary']['sessions'] == 2

    narrowed = body_of(handler.lambda_handler(
        {**a_request(caller=ADMIN, groups='[admin]'),
         'queryStringParameters': {'game': 'game1'}}, None))

    assert narrowed['game'] == 'game1'
    assert narrowed['summary']['sessions'] == 1
    assert [r['game_id'] for r in narrowed['per_game']] == ['game1']


def test_a_player_drilling_into_a_game_still_sees_only_their_own(handler, sessions_table):
    given_sessions(sessions_table,
                   a_session(user=PLAYER, game_id='game1'),
                   a_session(user=OTHER, game_id='game1'),
                   a_session(user=OTHER, game_id='game1'))

    body = body_of(handler.lambda_handler(
        {**a_request(), 'queryStringParameters': {'game': 'game1'}}, None))

    # Narrowing by game must never widen who is visible. Three sessions of
    # that game exist; one of them is theirs.
    assert body['summary']['sessions'] == 1
    assert {r['user'] for r in body['recent']} == {PLAYER}
    assert 'per_user' not in body


def test_an_admin_can_narrow_to_one_student_in_one_game(handler, sessions_table):
    given_sessions(sessions_table,
                   a_session(user=PLAYER, game_id='game1'),
                   a_session(user=PLAYER, game_id='game2'),
                   a_session(user=OTHER, game_id='game1'))

    body = body_of(handler.lambda_handler(
        {**a_request(caller=ADMIN, groups='[admin]'),
         'queryStringParameters': {'user': PLAYER, 'game': 'game1'}}, None))

    assert body['user'] == PLAYER and body['game'] == 'game1'
    assert body['summary']['sessions'] == 1


def test_completions_are_narrowed_with_the_game(handler, sessions_table, progress_table):
    given_sessions(sessions_table,
                   a_session(user=PLAYER, game_id='game1'),
                   a_session(user=PLAYER, game_id='game2'))
    for game_id in ('game1', 'game2'):
        progress_table.put_item(Item={
            'user_email': PLAYER, 'game_id': game_id,
            'ever_completed_at': '2026-10-25T11:00:00Z',
        })

    body = body_of(handler.lambda_handler(
        {**a_request(), 'queryStringParameters': {'game': 'game1'}}, None))

    # Two games finished overall, one of them this one.
    assert body['summary']['games_completed'] == 1


def test_a_game_nobody_played_comes_back_empty_rather_than_wrong(handler, sessions_table):
    given_sessions(sessions_table, a_session(game_id='game1'))

    body = body_of(handler.lambda_handler(
        {**a_request(), 'queryStringParameters': {'game': 'game999'}}, None))

    assert body['summary']['sessions'] == 0
    assert body['per_game'] == [] and body['recent'] == []


def test_the_recent_list_carries_the_player_facing_outcome(handler, sessions_table, games_table):
    games_table.put_item(Item={
        'id': 'game1', 'title': 'A game',
        'tasks': [{'id': 'task1'}, {'id': 'task2'}],
    })
    given_sessions(
        sessions_table,
        a_session(game_id='game1', start='2026-10-25T09:00:00Z',
                  tasks=done(task1=60, task2=60)),
        a_session(game_id='game1', start='2026-10-25T08:00:00Z',
                  tasks=done(task1=60)),
    )

    rows = body_of(handler.lambda_handler(a_request(), None))['recent']

    assert [r['outcome'] for r in rows] == ['completed', 'unfinished']
    assert [r['tasks_total'] for r in rows] == [2, 2]


# --- how the platform closed it is operator detail --------------------------

def test_an_admin_is_told_how_each_session_was_closed(handler, sessions_table):
    given_sessions(
        sessions_table,
        a_session(start='2026-10-25T09:00:00Z', reason='expired by janitor after 3.2h'),
        a_session(start='2026-10-25T08:00:00Z', status='CLEANUP_FAILED'),
        a_session(start='2026-10-25T07:00:00Z'),
    )

    rows = body_of(handler.lambda_handler(
        a_request(caller=ADMIN, groups='[admin]'), None))['recent']

    assert [r['ending'] for r in rows] == ['expired', 'cleanup_failed', 'ended']


def test_a_player_is_not_told_about_the_plumbing(handler, sessions_table):
    given_sessions(sessions_table, a_session(status='CLEANUP_FAILED'))

    rows = body_of(handler.lambda_handler(a_request(), None))['recent']

    # A failed wipe is not theirs to read, and it is not about them.
    assert 'ending' not in rows[0]
    assert rows[0]['outcome'] in ('completed', 'unfinished')
