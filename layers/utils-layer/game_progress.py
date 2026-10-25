"""
How far a player has got in a game.

Two records, because replaying a finished game must not erase having
finished it:

    the session item   the tasks completed in *this* attempt, and how long
                       each one took. It dies with the session, so a replay
                       starts from an empty board.
    Game-Progress      whether this player has ever finished the game, and
                       whether they are playing it right now. One item per
                       (player, game); this is what the chip reads.

Nothing here stores the status itself. Three states is few enough to answer
from those two facts every time, which is one less thing that can drift out
of step with reality.
"""

import os
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

TODO = 'todo'
IN_PROGRESS = 'in_progress'
DONE = 'done'

TABLE_ENV = 'GAME_PROGRESS_TABLE'


def _now():
    """The same shape as every other timestamp in this stack."""
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _parse(stamp):
    return datetime.fromisoformat(stamp.replace('Z', '+00:00'))


def _table():
    return boto3.resource('dynamodb').Table(os.environ[TABLE_ENV])


# --- the status behind the chip ---------------------------------------------

def status_of(item):
    """
    One player's standing in one game, from their Game-Progress item.

    Playing beats finished: reopening a game you already completed shows it
    as in progress, because that is what is happening. Everything else —
    never touched, or started and walked away from without finishing — is
    todo. Partial work that is not running right now is not a state of its
    own; the player sees it again as soon as they start.
    """
    if not item:
        return TODO
    if item.get('playing'):
        return IN_PROGRESS
    if item.get('ever_completed_at'):
        return DONE
    return TODO


def status_for(user_email, game_id):
    """The status of a single game, for the game page."""
    if not user_email:
        return TODO

    try:
        item = _table().get_item(
            Key={'user_email': user_email, 'game_id': game_id}
        ).get('Item')
    except ClientError as e:
        # A missing chip is better than a page that will not load.
        print(f"Could not read progress for {game_id}: {e}")
        return TODO

    return status_of(item)


def statuses_for(user_email):
    """
    Every game this player has touched, as {game_id: status}.

    One query answers the whole catalogue listing: games with no item are
    todo, which is the default the caller applies for anything missing here.
    """
    if not user_email:
        return {}

    try:
        items = _table().query(
            KeyConditionExpression=Key('user_email').eq(user_email)
        ).get('Items', [])
    except ClientError as e:
        print(f"Could not read progress for {user_email}: {e}")
        return {}

    return {item['game_id']: status_of(item) for item in items}


# --- writing it down --------------------------------------------------------

def mark_playing(user_email, game_id, session_id):
    """Called once a session is really up, so an abandoned start writes nothing."""
    _table().update_item(
        Key={'user_email': user_email, 'game_id': game_id},
        UpdateExpression=(
            'SET playing = :yes, last_session_id = :session, last_started_at = :now'
        ),
        ExpressionAttributeValues={
            ':yes': True,
            ':session': session_id,
            ':now': _now(),
        },
    )


def mark_not_playing(user_email, game_id):
    """The session is over, however it ended."""
    _table().update_item(
        Key={'user_email': user_email, 'game_id': game_id},
        UpdateExpression='SET playing = :no',
        ExpressionAttributeValues={':no': False},
    )


def mark_completed(user_email, game_id):
    """
    The player just finished every task.

    ever_completed_at is written once and never moved: it is the fact the
    green chip rests on, and a replay must not be able to unset it.
    """
    now = _now()
    _table().update_item(
        Key={'user_email': user_email, 'game_id': game_id},
        UpdateExpression=(
            'SET ever_completed_at = if_not_exists(ever_completed_at, :now), '
            'last_completed_at = :now'
        ),
        ExpressionAttributeValues={':now': now},
    )


# --- the tasks of one attempt, on the session item --------------------------

def task_ids(game):
    """The game's tasks in the order they are meant to be played."""
    return [task['id'] for task in game.get('tasks', []) if task.get('id')]


def completed_tasks(session):
    """The map as stored: {task_id: {completed_at, seconds}}."""
    return session.get('completed_tasks') or {}


def completed_view(session):
    """
    The same map, safe to hand to json.dumps.

    DynamoDB hands numbers back as Decimal, which json refuses, and this is
    the only place in the API that returns a stored number.
    """
    return {
        task_id: {
            'completed_at': entry.get('completed_at'),
            'seconds': int(entry.get('seconds', 0)),
        }
        for task_id, entry in completed_tasks(session).items()
    }


def _latest_milestone(session, done):
    """
    The last thing the player finished: a task, or the start of the session.
    """
    stamps = []
    for stamp in [session.get('start_time')] + [e.get('completed_at') for e in done.values()]:
        if not stamp:
            continue
        try:
            stamps.append(_parse(stamp))
        except ValueError:
            continue

    return max(stamps) if stamps else None


def record_task_completion(sessions_table, session, task_id):
    """
    Write a finished task onto the session, with how long it took.

    The clock runs from the last thing the player finished — the session
    start, or the previous task. That assumes the tasks are worked in the
    order they are listed, which is now enforced, but it still reads wrong
    for a task answered twice or a session left idle overnight.

    Returns the map including this task, without re-reading it.
    """
    done = dict(completed_tasks(session))
    if task_id in done:
        # Answering a finished task again must not restart its clock.
        return done

    finished_at = _now()
    since = _latest_milestone(session, done)
    seconds = int((_parse(finished_at) - since).total_seconds()) if since else 0

    entry = {'completed_at': finished_at, 'seconds': max(0, seconds)}
    done[task_id] = entry

    try:
        sessions_table.update_item(
            Key={'session_id': session['session_id']},
            UpdateExpression='SET completed_tasks.#task = :entry',
            ExpressionAttributeNames={'#task': task_id},
            ExpressionAttributeValues={':entry': entry},
        )
    except ClientError as e:
        if e.response['Error']['Code'] != 'ValidationException':
            raise
        # A session started before this existed has no map to set a key on.
        sessions_table.update_item(
            Key={'session_id': session['session_id']},
            UpdateExpression='SET completed_tasks = :map',
            ExpressionAttributeValues={':map': done},
        )

    return done


def all_tasks_done(game, done):
    """
    Whether this attempt finished the game.

    A game with no tasks is never finished: there is nothing to finish, and
    only a real submission is allowed to turn the chip green.
    """
    ids = task_ids(game)
    return bool(ids) and all(task_id in done for task_id in ids)


# --- which task the player is allowed to answer -----------------------------

def unlocked_task_ids(game, done):
    """
    Every task up to and including the first unfinished one.

    Tasks open one at a time so a player cannot skip the work and come back
    to it: the game is a sequence, and reading task 3 before solving task 2
    gives away task 2.
    """
    unlocked = []
    for task_id in task_ids(game):
        unlocked.append(task_id)
        if task_id not in done:
            break

    return unlocked


def blocking_task(game, done, task_id):
    """
    The task that has to be finished before this one, or None if it is open.

    Returns the first unfinished task ahead of it, which is the one worth
    naming in the refusal.
    """
    ids = task_ids(game)
    if task_id not in ids:
        # Not one of the game's tasks at all. The handler's own "no such
        # task" answer is the right one; naming an ordering problem here
        # would only send the player looking for the wrong thing.
        return None

    for candidate in ids:
        if candidate == task_id:
            return None
        if candidate not in done:
            return candidate

    return None
