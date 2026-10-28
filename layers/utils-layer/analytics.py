"""
Turning the sessions table into numbers worth looking at.

Everything here is derived at read time from rows other code already
writes: a session knows when it started, when it ended, why it ended and
which tasks were finished inside it, with how long each took.

Two things are deliberately absent. Attempts — nothing records a wrong
answer, so "how many tries before it worked" cannot be answered and is not
guessed at. And anything older than the sessions table holds: the history
was cleared of pre-October-2026 test runs, so trends start there.
"""

from datetime import datetime
from statistics import median

PLAYER_ENDED = 'ended by player'
JANITOR = 'expired by janitor'

# What a session's stack_status says about how it finished.
CLEANUP_FAILED = 'CLEANUP_FAILED'


def _parse(stamp):
    try:
        return datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        return None


def duration_seconds(session):
    """
    How long the session lasted, or None when it cannot be known.

    A session still running has no end_time, and one written before the
    teardown existed may have neither — both are left out of the medians
    rather than counted as zero.
    """
    started, ended = _parse(session.get('start_time')), _parse(session.get('end_time'))
    if not started or not ended:
        return None

    seconds = int((ended - started).total_seconds())
    return seconds if seconds >= 0 else None


def outcome(session):
    """Why this session is over, in the words the screen uses."""
    if session.get('active'):
        return 'running'

    reason = session.get('ended_reason') or ''
    if session.get('stack_status') == CLEANUP_FAILED:
        return 'cleanup_failed'
    if reason.startswith(JANITOR):
        return 'expired'
    if reason == PLAYER_ENDED:
        return 'ended'

    # Sessions from before ended_reason existed, and anything unaccounted
    # for. Naming it rather than folding it into "ended" keeps the counts
    # honest about what is actually known.
    return 'unknown'


def completed_tasks(session):
    return session.get('completed_tasks') or {}


def _median(values):
    return int(median(values)) if values else None


def summarise(sessions, completed_games):
    """
    The headline numbers.

    completed_games is the count of (player, game) pairs ever finished,
    which comes from the progress table rather than from here: a game
    finished in an earlier session is still finished.
    """
    durations = [d for d in (duration_seconds(s) for s in sessions) if d is not None]
    outcomes = [outcome(s) for s in sessions]

    return {
        'sessions': len(sessions),
        'games_played': len({s.get('game_id') for s in sessions if s.get('game_id')}),
        'games_completed': completed_games,
        'tasks_completed': sum(len(completed_tasks(s)) for s in sessions),
        'total_seconds': sum(durations),
        'median_session_seconds': _median(durations),
        'ended_by_player': outcomes.count('ended'),
        'expired_by_janitor': outcomes.count('expired'),
        'cleanup_failed': outcomes.count('cleanup_failed'),
    }


def per_game(sessions, titles, finished_pairs):
    """
    One row per game, ordered by how much it has been played.

    completions counts the players who ever finished it, not the sessions
    that finished it, so replaying a game does not inflate it.
    """
    rows = {}
    for session in sessions:
        game_id = session.get('game_id')
        if not game_id:
            continue

        row = rows.setdefault(game_id, {
            'game_id': game_id,
            'title': titles.get(game_id, game_id),
            'sessions': 0,
            'completions': 0,
            'tasks_completed': 0,
            '_durations': [],
        })
        row['sessions'] += 1
        row['tasks_completed'] += len(completed_tasks(session))

        seconds = duration_seconds(session)
        if seconds is not None:
            row['_durations'].append(seconds)

    for game_id, player in finished_pairs:
        if game_id in rows:
            rows[game_id]['completions'] += 1

    out = []
    for row in rows.values():
        durations = row.pop('_durations')
        row['median_seconds'] = _median(durations)
        out.append(row)

    out.sort(key=lambda r: (-r['sessions'], r['game_id']))
    return out


def per_task(sessions, titles):
    """
    How long each task takes, across everyone who finished it.

    This is the view the catalogue cannot give: a task whose median dwarfs
    its neighbours is either where the difficulty really is, or where the
    wording is unclear.
    """
    rows = {}
    for session in sessions:
        game_id = session.get('game_id')
        for task_id, entry in completed_tasks(session).items():
            key = (game_id, task_id)
            row = rows.setdefault(key, {
                'game_id': game_id,
                'title': titles.get(game_id, game_id),
                'task_id': task_id,
                'completions': 0,
                '_seconds': [],
            })
            row['completions'] += 1
            try:
                row['_seconds'].append(int(entry.get('seconds', 0)))
            except (TypeError, ValueError):
                pass

    out = []
    for row in rows.values():
        row['median_seconds'] = _median(row.pop('_seconds'))
        out.append(row)

    out.sort(key=lambda r: (r['game_id'], r['task_id']))
    return out


def per_user(sessions, finished_pairs):
    """One row per player. Only ever sent to an administrator."""
    rows = {}
    for session in sessions:
        email = session.get('user_name')
        if not email:
            continue

        row = rows.setdefault(email, {
            'user': email,
            'sessions': 0,
            'games_played': set(),
            'games_completed': 0,
            'tasks_completed': 0,
            'total_seconds': 0,
        })
        row['sessions'] += 1
        row['games_played'].add(session.get('game_id'))
        row['tasks_completed'] += len(completed_tasks(session))

        seconds = duration_seconds(session)
        if seconds is not None:
            row['total_seconds'] += seconds

    for game_id, player in finished_pairs:
        if player in rows:
            rows[player]['games_completed'] += 1

    out = []
    for row in rows.values():
        row['games_played'] = len(row['games_played'] - {None})
        out.append(row)

    out.sort(key=lambda r: (-r['sessions'], r['user']))
    return out


def recent(sessions, titles, limit=25):
    """The latest sessions, newest first, as the screen lists them."""
    ordered = sorted(
        sessions, key=lambda s: s.get('start_time') or '', reverse=True
    )[:limit]

    return [{
        'game_id': s.get('game_id'),
        'title': titles.get(s.get('game_id'), s.get('game_id')),
        'user': s.get('user_name'),
        'start_time': s.get('start_time'),
        'duration_seconds': duration_seconds(s),
        'outcome': outcome(s),
        'tasks_completed': len(completed_tasks(s)),
    } for s in ordered]
