"""
Checking an answer.

The handler is a relay: it finds the player's session, looks the task title
up in the catalogue, and invokes validate_taskN inside the game's own
account. These pin what it sends, what it refuses to send, and what it makes
of the answer that comes back.
"""

import io
import json

import pytest
from botocore.exceptions import ClientError

from tests.conftest import ACCOUNT_A, load_handler

PLAYER = 'player@example.com'
GAME_ID = 'game1'
SESSION_ID = 'session-1'
OUTPUTS = {'BucketName': 'lab-bucket', 'VpcId': 'vpc-123'}


class FakeLambda:
    """Stands in for the Lambda client built with the game account's keys."""

    def __init__(self, payload=None, error=None):
        self.payload = payload if payload is not None else {
            'completed': True, 'message': 'Well done', 'progressPercent': 100,
        }
        self.error = error
        self.invocations = []

    def invoke(self, FunctionName, InvocationType, Payload):
        self.invocations.append({
            'function': FunctionName,
            'payload': json.loads(Payload),
        })
        if self.error:
            raise self.error
        return {
            'StatusCode': 200,
            'Payload': io.BytesIO(json.dumps(self.payload).encode()),
        }


@pytest.fixture
def validator():
    return FakeLambda()


@pytest.fixture
def handler(sessions_table, games_table, accounts_table, access_table, progress_table,
            validator, monkeypatch):
    import boto3
    import account_manager

    monkeypatch.setattr(
        account_manager.AccountManager, 'assume_cross_account_role',
        lambda self, account_id: {
            'AccessKeyId': 'AKIAFAKE',
            'SecretAccessKey': 'secret',
            'SessionToken': 'token',
        }
    )

    # Everything but the Lambda client is a genuine moto client; only the
    # cross-account invoke has no moto equivalent worth trusting here.
    real_client = boto3.client

    def client(service_name, *args, **kwargs):
        if service_name == 'lambda':
            return validator
        return real_client(service_name, *args, **kwargs)

    monkeypatch.setattr(boto3, 'client', client)
    return load_handler('submit_task_answer')


def given_session(sessions_table, active=True, outputs=None, account_id=ACCOUNT_A, done=None):
    sessions_table.put_item(Item={
        'session_id': SESSION_ID,
        'user_name': PLAYER,
        'game_id': GAME_ID,
        'account_id': account_id,
        'active': active,
        'start_time': '2026-10-24T10:00:00Z',
        'stack_status': 'CREATE_COMPLETE',
        'stack_outputs': OUTPUTS if outputs is None else outputs,
        'completed_tasks': done or {},
    })


def finished(*task_ids):
    """Tasks already completed in this session, so later ones are unlocked."""
    return {t: {'completed_at': '2026-10-25T10:05:00Z', 'seconds': 300} for t in task_ids}


def given_game(games_table, tasks=None):
    games_table.put_item(Item={
        'id': GAME_ID,
        'title': 'Where are my nodes',
        'content': '<p>...</p>',
        'category': 'Compute',
        'difficulty': 'Hard',
        'tasks': tasks if tasks is not None else [
            {'id': 'task1', 'title': 'Find the node', 'content': '<p>...</p>'},
            {'id': 'task2', 'title': 'Fix the node', 'content': '<p>...</p>'},
        ],
    })


def a_request(claims=None, task_id='task1', answer='my-answer', body=None):
    return {
        'pathParameters': {'gameId': GAME_ID, 'taskId': task_id},
        'body': json.dumps(body if body is not None else {'answer': answer}),
        'requestContext': {'authorizer': {'claims': claims if claims is not None else {
            'email': PLAYER, 'cognito:groups': '[full-access]',
        }}},
    }


def body_of(response):
    return json.loads(response['body'])


# --- what it refuses to do --------------------------------------------------

def test_a_request_without_an_identity_is_rejected(handler, validator):
    response = handler.lambda_handler(
        {'pathParameters': {'gameId': GAME_ID, 'taskId': 'task1'},
         'body': json.dumps({'answer': 'x'}),
         'requestContext': {}}, None
    )

    assert response['statusCode'] == 401
    assert validator.invocations == []


def test_a_game_the_user_cannot_play_cannot_be_answered(
    handler, sessions_table, games_table, validator
):
    given_session(sessions_table)
    given_game(games_table)

    response = handler.lambda_handler(a_request(claims={'email': PLAYER}), None)

    assert response['statusCode'] == 403
    assert validator.invocations == []


def test_a_task_id_with_no_number_in_it_is_refused(handler, validator):
    response = handler.lambda_handler(a_request(task_id='task-one'), None)

    # The number is what names the function to call; without one there is
    # nothing to invoke and guessing would invoke the wrong task.
    assert response['statusCode'] == 400
    assert validator.invocations == []


def test_without_a_running_session_there_is_nothing_to_check(handler, games_table, validator):
    given_game(games_table)

    response = handler.lambda_handler(a_request(), None)

    assert response['statusCode'] == 404
    assert validator.invocations == []


def test_a_closed_session_cannot_be_answered(handler, sessions_table, games_table, validator):
    given_session(sessions_table, active=False)
    given_game(games_table)

    assert handler.lambda_handler(a_request(), None)['statusCode'] == 404
    assert validator.invocations == []


def test_a_session_whose_game_left_the_catalogue_is_reported(handler, sessions_table, validator):
    given_session(sessions_table)

    response = handler.lambda_handler(a_request(), None)

    assert response['statusCode'] == 404
    assert body_of(response)['message'] == 'Game not found'
    assert validator.invocations == []


# --- the relay --------------------------------------------------------------

def test_the_validator_is_called_in_the_game_account_with_the_stack_outputs(
    handler, sessions_table, games_table, validator
):
    given_session(sessions_table, done=finished('task1'))
    given_game(games_table)

    handler.lambda_handler(a_request(task_id='task2', answer='the-answer'), None)

    assert len(validator.invocations) == 1
    call = validator.invocations[0]
    assert call['function'] == 'validate_task2'
    assert call['payload']['userInput'] == 'the-answer'
    assert call['payload']['taskNumber'] == 2
    # The validator's whole view of the lab is this dict; dropping it is how
    # a correct answer starts reading as wrong.
    assert call['payload']['stackOutputParams'] == OUTPUTS


def test_the_task_title_comes_from_the_catalogue(handler, sessions_table, games_table, validator):
    given_session(sessions_table)
    given_game(games_table)

    handler.lambda_handler(a_request(task_id='task1'), None)

    assert validator.invocations[0]['payload']['taskTitle'] == 'Find the node'


def test_an_unlisted_task_falls_back_to_a_generic_title(
    handler, sessions_table, games_table, validator
):
    given_session(sessions_table)
    given_game(games_table, tasks=[{'id': 'task1', 'title': 'Find the node', 'content': ''}])

    handler.lambda_handler(a_request(task_id='task7'), None)

    assert validator.invocations[0]['payload']['taskTitle'] == 'Task 7'


def test_a_passing_answer_comes_back_with_the_validators_verdict(
    handler, sessions_table, games_table
):
    given_session(sessions_table)
    given_game(games_table)

    response = handler.lambda_handler(a_request(), None)

    assert response['statusCode'] == 200
    assert body_of(response)['validation'] == {
        'completed': True,
        'message': 'Well done',
        'progressPercent': 100,
        'metadata': {},
    }


def test_a_failing_answer_is_a_200_with_completed_false(
    handler, sessions_table, games_table, validator
):
    given_session(sessions_table)
    given_game(games_table)
    validator.payload = {'completed': False, 'message': 'Not there yet', 'progressPercent': 40}

    response = handler.lambda_handler(a_request(), None)

    # A wrong answer is a normal outcome of asking, not an error: the page
    # renders the message and the progress bar from this body.
    assert response['statusCode'] == 200
    assert body_of(response)['validation']['completed'] is False
    assert body_of(response)['validation']['progressPercent'] == 40


def test_a_missing_validator_says_which_one_was_missing(
    handler, sessions_table, games_table, validator
):
    given_session(sessions_table, done=finished('task1'))
    given_game(games_table)
    validator.error = ClientError(
        {'Error': {'Code': 'ResourceNotFoundException', 'Message': 'Function not found'}},
        'Invoke'
    )

    response = handler.lambda_handler(a_request(task_id='task2'), None)

    assert response['statusCode'] == 404
    assert 'validate_task2' in body_of(response)['message']


def test_an_unreachable_game_account_is_a_500(
    handler, sessions_table, games_table, monkeypatch
):
    given_session(sessions_table)
    given_game(games_table)

    import account_manager
    monkeypatch.setattr(
        account_manager.AccountManager, 'assume_cross_account_role',
        lambda self, account_id: (_ for _ in ()).throw(RuntimeError('no such role'))
    )

    response = handler.lambda_handler(a_request(), None)

    assert response['statusCode'] == 500


# --- tasks open one at a time -----------------------------------------------

def test_a_task_the_player_has_not_reached_is_refused(
    handler, sessions_table, games_table, validator
):
    given_session(sessions_table)
    given_game(games_table)

    response = handler.lambda_handler(a_request(task_id='task2'), None)

    assert response['statusCode'] == 409
    assert body_of(response)['blockedBy'] == 'task1'
    # The page hides a locked task, so getting here means the gate was gone
    # around. Nothing may reach the validator.
    assert validator.invocations == []


def test_finishing_one_task_opens_the_next(handler, sessions_table, games_table, validator):
    given_session(sessions_table, done=finished('task1'))
    given_game(games_table)

    response = handler.lambda_handler(a_request(task_id='task2'), None)

    assert response['statusCode'] == 200
    assert len(validator.invocations) == 1


def test_the_first_task_is_open_from_the_start(handler, sessions_table, games_table):
    given_session(sessions_table)
    given_game(games_table)

    assert handler.lambda_handler(a_request(task_id='task1'), None)['statusCode'] == 200


# --- finishing a task, and finishing the game -------------------------------

def test_a_completed_task_is_written_onto_the_session(handler, sessions_table, games_table):
    given_session(sessions_table)
    given_game(games_table)

    handler.lambda_handler(a_request(task_id='task1'), None)

    stored = sessions_table.get_item(Key={'session_id': SESSION_ID})['Item']
    assert 'task1' in stored['completed_tasks']
    assert stored['completed_tasks']['task1']['completed_at']


def test_a_wrong_answer_records_nothing(handler, sessions_table, games_table, validator):
    given_session(sessions_table)
    given_game(games_table)
    validator.payload = {'completed': False, 'message': 'Nope', 'progressPercent': 10}

    handler.lambda_handler(a_request(task_id='task1'), None)

    stored = sessions_table.get_item(Key={'session_id': SESSION_ID})['Item']
    assert stored['completed_tasks'] == {}


def test_the_last_task_says_the_game_is_over(handler, sessions_table, games_table, progress_table):
    given_session(sessions_table, done=finished('task1'))
    given_game(games_table)  # two tasks

    response = handler.lambda_handler(a_request(task_id='task2'), None)

    body = body_of(response)
    # This is what opens the "shut the environment down" dialog, so it is
    # the backend's call and not something the page works out for itself.
    assert body['progress']['game_completed'] is True
    assert body['progress']['completed_tasks'] == ['task1', 'task2']
    assert body['progress']['total'] == 2

    progress = progress_table.get_item(
        Key={'user_email': PLAYER, 'game_id': GAME_ID}
    )['Item']
    assert progress['ever_completed_at']


def test_a_task_that_is_not_the_last_does_not_end_the_game(
    handler, sessions_table, games_table, progress_table
):
    given_session(sessions_table)
    given_game(games_table)  # two tasks

    body = body_of(handler.lambda_handler(a_request(task_id='task1'), None))

    assert body['progress']['game_completed'] is False
    assert 'Item' not in progress_table.get_item(
        Key={'user_email': PLAYER, 'game_id': GAME_ID}
    )


# --- not every task wants something typed -----------------------------------

def test_a_task_with_nothing_typed_is_still_validated(
    handler, sessions_table, games_table, validator
):
    given_session(sessions_table)
    given_game(games_table)

    response = handler.lambda_handler(a_request(answer=''), None)

    # Most tasks are checked against the AWS account. Refusing an empty
    # answer only taught players to type a character to reach the button.
    assert response['statusCode'] == 200
    assert validator.invocations[0]['payload']['userInput'] == ''


def test_a_missing_answer_reaches_the_validator_as_a_string(
    handler, sessions_table, games_table, validator
):
    given_session(sessions_table)
    given_game(games_table)

    handler.lambda_handler(a_request(body={}), None)

    # Validators are written expecting a string; null would reach them as
    # None and break ones that call .strip() on it.
    assert validator.invocations[0]['payload']['userInput'] == ''
