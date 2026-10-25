"""
Progress through a game.

Two halves, tested apart: the status behind the chip, which is derived and
never stored, and the record of one attempt, which lives on the session and
decides what the player is allowed to answer next.
"""

import pytest

from tests.conftest import PROGRESS_TABLE

PLAYER = 'player@example.com'
GAME_ID = 'game1'
SESSION_ID = 'session-1'


@pytest.fixture
def progress():
    import game_progress
    return game_progress


def a_game(*task_ids):
    return {'id': GAME_ID, 'tasks': [{'id': t, 'title': t} for t in task_ids]}


def a_session(sessions_table, start_time='2026-10-25T10:00:00Z', done=None):
    item = {
        'session_id': SESSION_ID,
        'user_name': PLAYER,
        'game_id': GAME_ID,
        'active': True,
        'start_time': start_time,
        'completed_tasks': done if done is not None else {},
    }
    sessions_table.put_item(Item=item)
    return item


def stored(sessions_table):
    return sessions_table.get_item(Key={'session_id': SESSION_ID})['Item']


# --- the status behind the chip ---------------------------------------------

@pytest.mark.parametrize('item, expected', [
    (None, 'todo'),
    ({}, 'todo'),
    ({'playing': False}, 'todo'),
    ({'playing': True}, 'in_progress'),
    ({'ever_completed_at': '2026-10-25T10:00:00Z'}, 'done'),
    ({'playing': False, 'ever_completed_at': '2026-10-25T10:00:00Z'}, 'done'),
])
def test_the_status_is_read_off_two_facts(progress, item, expected):
    assert progress.status_of(item) == expected


def test_replaying_a_finished_game_reads_as_in_progress(progress):
    # Playing beats finished. The player is in it right now, and the board
    # they are looking at is empty again.
    assert progress.status_of({
        'playing': True, 'ever_completed_at': '2026-10-25T10:00:00Z'
    }) == 'in_progress'


def test_started_and_walked_away_from_is_back_to_todo(progress):
    assert progress.status_of({'playing': False, 'last_session_id': 's-9'}) == 'todo'


# --- reading and writing it -------------------------------------------------

def test_a_game_never_touched_has_no_item_and_reads_todo(progress, progress_table):
    assert progress.status_for(PLAYER, GAME_ID) == 'todo'
    assert progress.statuses_for(PLAYER) == {}


def test_starting_then_finishing_then_ending_walks_the_three_states(progress, progress_table):
    progress.mark_playing(PLAYER, GAME_ID, SESSION_ID)
    assert progress.status_for(PLAYER, GAME_ID) == 'in_progress'

    progress.mark_completed(PLAYER, GAME_ID)
    assert progress.status_for(PLAYER, GAME_ID) == 'in_progress'  # still up

    progress.mark_not_playing(PLAYER, GAME_ID)
    assert progress.status_for(PLAYER, GAME_ID) == 'done'


def test_a_second_completion_does_not_move_the_first_one(progress, progress_table):
    progress.mark_completed(PLAYER, GAME_ID)
    first = progress_table.get_item(
        Key={'user_email': PLAYER, 'game_id': GAME_ID}
    )['Item']['ever_completed_at']

    progress.mark_completed(PLAYER, GAME_ID)
    item = progress_table.get_item(Key={'user_email': PLAYER, 'game_id': GAME_ID})['Item']

    # The green chip rests on this date; a replay must not be able to move it.
    assert item['ever_completed_at'] == first
    assert item['last_completed_at'] >= first


def test_the_listing_answers_every_touched_game_at_once(progress, progress_table):
    progress.mark_playing(PLAYER, 'game1', SESSION_ID)
    progress.mark_completed(PLAYER, 'game2')
    progress.mark_not_playing(PLAYER, 'game2')

    assert progress.statuses_for(PLAYER) == {'game1': 'in_progress', 'game2': 'done'}


def test_an_unreadable_table_costs_the_chip_and_nothing_else(progress, progress_table, monkeypatch):
    monkeypatch.setenv('GAME_PROGRESS_TABLE', 'a-table-that-is-not-there')

    assert progress.status_for(PLAYER, GAME_ID) == 'todo'
    assert progress.statuses_for(PLAYER) == {}


# --- the tasks of one attempt -----------------------------------------------

def test_a_finished_task_is_written_with_how_long_it_took(progress, sessions_table):
    session = a_session(sessions_table)

    done = progress.record_task_completion(sessions_table, session, 'task1')

    assert 'task1' in done
    assert done['task1']['seconds'] > 0  # measured from the session start
    assert stored(sessions_table)['completed_tasks']['task1']['completed_at']


def test_the_clock_restarts_at_the_previous_task(progress, sessions_table):
    session = a_session(sessions_table, done={
        'task1': {'completed_at': '2026-10-25T10:05:00Z', 'seconds': 300},
    })

    done = progress.record_task_completion(sessions_table, session, 'task2')

    # Measured from task1 finishing, not from the session starting, so the
    # five minutes already spent on task1 are not charged to task2 as well.
    from datetime import datetime, timezone
    elapsed = (datetime.now(timezone.utc)
               - datetime.fromisoformat('2026-10-25T10:05:00+00:00')).total_seconds()
    assert abs(done['task2']['seconds'] - elapsed) < 5


def test_answering_a_finished_task_again_leaves_its_time_alone(progress, sessions_table):
    original = {'completed_at': '2026-10-25T10:05:00Z', 'seconds': 300}
    session = a_session(sessions_table, done={'task1': original})

    done = progress.record_task_completion(sessions_table, session, 'task1')

    assert done['task1'] == original


def test_a_session_with_no_map_yet_still_records(progress, sessions_table):
    # Sessions that started before this feature have no attribute to set a
    # key on, and DynamoDB refuses the update rather than creating one.
    sessions_table.put_item(Item={
        'session_id': SESSION_ID, 'user_name': PLAYER, 'game_id': GAME_ID,
        'active': True, 'start_time': '2026-10-25T10:00:00Z',
    })
    session = sessions_table.get_item(Key={'session_id': SESSION_ID})['Item']

    progress.record_task_completion(sessions_table, session, 'task1')

    assert 'task1' in stored(sessions_table)['completed_tasks']


def test_the_view_of_the_map_survives_json(progress, sessions_table):
    import json

    session = a_session(sessions_table)
    progress.record_task_completion(sessions_table, session, 'task1')
    reloaded = stored(sessions_table)

    # Straight off DynamoDB the seconds are a Decimal, which json refuses.
    with pytest.raises(TypeError):
        json.dumps(reloaded['completed_tasks'])

    assert json.dumps(progress.completed_view(reloaded))


# --- finishing the game -----------------------------------------------------

def test_the_game_is_finished_only_when_every_task_is(progress):
    game = a_game('task1', 'task2')

    assert progress.all_tasks_done(game, {'task1': {}}) is False
    assert progress.all_tasks_done(game, {'task1': {}, 'task2': {}}) is True


def test_a_game_with_no_tasks_is_never_finished(progress):
    # Nothing to finish, and an empty set is a subset of everything: this
    # would otherwise turn the chip green the moment the game was opened.
    assert progress.all_tasks_done(a_game(), {}) is False


# --- what the player may answer next ----------------------------------------

def test_only_the_first_task_is_open_at_the_start(progress):
    assert progress.unlocked_task_ids(a_game('task1', 'task2', 'task3'), {}) == ['task1']


def test_finishing_one_opens_the_next(progress):
    game = a_game('task1', 'task2', 'task3')

    assert progress.unlocked_task_ids(game, {'task1': {}}) == ['task1', 'task2']


def test_a_finished_game_has_everything_open(progress):
    game = a_game('task1', 'task2')

    assert progress.unlocked_task_ids(game, {'task1': {}, 'task2': {}}) == ['task1', 'task2']


def test_the_refusal_names_the_task_standing_in_the_way(progress):
    game = a_game('task1', 'task2', 'task3')

    assert progress.blocking_task(game, {'task1': {}}, 'task3') == 'task2'
    assert progress.blocking_task(game, {'task1': {}}, 'task2') is None
    assert progress.blocking_task(game, {}, 'task1') is None


def test_a_task_the_game_does_not_have_is_not_blocked_here(progress):
    # The handler already has a "no such task" answer; this must not shadow
    # it with a confusing one about ordering.
    assert progress.blocking_task(a_game('task1'), {}, 'task9') is None
