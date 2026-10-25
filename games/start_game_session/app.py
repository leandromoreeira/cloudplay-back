import os
import json
import boto3
import uuid
from datetime import datetime
from botocore.exceptions import ClientError

# Import from Lambda Layer
import game_progress
import team_role
from account_manager import get_account_manager
from access_control import get_identity, can_access_game

AWS_REGION = os.environ['AWS_REGION']

# The session exists from the first moment, before an account is claimed and
# long before CloudFormation has been asked for anything. That is the whole
# point: the "already in progress" check above can only see a row that is
# already there, and waiting for the stack left a window wide enough for a
# second click to walk through and take another account.
STATUS_STARTING = 'STARTING'


def _response(status_code, body):
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        },
        'body': json.dumps(body)
    }


def _abandon(table, session_id, account_manager=None, account_id=None):
    """Undo a start that never got off the ground."""
    if account_id:
        account_manager.mark_available(account_id)

    try:
        table.delete_item(Key={'session_id': session_id})
    except ClientError as e:
        print(f"Could not drop the abandoned session {session_id}: {e}")


def lambda_handler(event, context):
    game_id = event['pathParameters']['gameId']
    user_name, groups = get_identity(event)

    if not user_name:
        return _response(401, {'message': 'Invalid or expired token'})

    if not can_access_game(user_name, groups, game_id):
        return _response(403, {'message': 'You do not have access to this game'})

    # Initialize account manager
    account_manager = get_account_manager()
    session_id = str(uuid.uuid4())

    try:
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(os.environ['GAME_SESSIONS_TABLE'])

        # Check if user already has this game in progress
        response = table.query(
            IndexName='user_game_index',
            KeyConditionExpression='user_name = :user_name and game_id = :game_id',
            ExpressionAttributeValues={
                ':user_name': user_name,
                ':game_id': game_id,
                ':active': True
            },
            FilterExpression='active = :active'
        )

        if response['Items']:
            return _response(400, {'message': 'You already have this game in progress'})

        table.put_item(
            Item={
                'session_id': session_id,
                'user_name': user_name,
                'game_id': game_id,
                'start_time': datetime.utcnow().isoformat() + 'Z',
                'active': True,
                'end_time': None,
                'stack_status': STATUS_STARTING,
                'stack_outputs': {},
                # The tasks finished in this attempt, filled in as they are.
                # A replay of a finished game starts from an empty board.
                'completed_tasks': {}
            }
        )
    except ClientError as e:
        print(f"Error starting the session: {e}")
        return _response(500, {
            'error': 'Could not start the game session.',
            'details': e.response['Error']['Message'],
        })

    try:
        # Claim an account for this session. The claim is atomic, so concurrent
        # starts can't be handed the same account.
        selected_account = account_manager.claim_account(session_id)
        if not selected_account:
            _abandon(table, session_id)
            return _response(503, {
                'message': 'All game environments are currently busy. Please try again later.',
                'error_code': 'NO_AVAILABLE_ACCOUNTS'
            })

        # Recorded straight away, so that a crash from here on leaves a session
        # the janitor can find the account through.
        table.update_item(
            Key={'session_id': session_id},
            UpdateExpression='SET account_id = :account_id',
            ExpressionAttributeValues={':account_id': selected_account}
        )
        print(f"Selected account {selected_account} for game deployment")
    except ClientError as e:
        print(f"Error claiming an account: {e}")
        _abandon(table, session_id)
        return _response(500, {
            'error': 'Could not reserve an environment for this game.',
            'details': e.response['Error']['Message'],
        })

    stack_name = f'game-session-{session_id}'
    bucket_name = os.environ['S3_BUCKET_NAME']
    template_key = f'{game_id}/{game_id}.yaml'
    template_url = f'https://{bucket_name}.s3.{AWS_REGION}.amazonaws.com/{template_key}'

    s3_client = boto3.client('s3')

    try:
        # The player's role is set up before the lab is, so that nothing can
        # ever hand out a session you cannot log into.
        iam_client = account_manager.create_cross_account_client(selected_account, 'iam')
        team_role.ensure_team_role(iam_client, account_manager.orchestrator_account_id())
        team_role.put_game_policy(
            iam_client,
            team_role.load_game_policy(s3_client, bucket_name, game_id)
        )
        print(f"{team_role.ROLE_NAME} ready in {selected_account} for {game_id}")

        # Create cross-account clients for the selected account
        cf_client = account_manager.create_cross_account_clients(selected_account)

        print(f"Creating CloudFormation stack {stack_name} in account {selected_account} using template URL {template_url}")

        # Deploy to the selected cross-account using cross-account CloudFormation client
        # The topic is what makes the session row keep up with the stack on
        # its own. CloudFormation checks this caller may publish to it and
        # refuses the whole call otherwise, so a broken topic policy shows
        # up here rather than as sessions that never leave "building".
        cf_client.create_stack(
            StackName=stack_name,
            TemplateURL=template_url,
            NotificationARNs=[os.environ['STACK_EVENTS_TOPIC_ARN']],
            Capabilities=['CAPABILITY_IAM', 'CAPABILITY_NAMED_IAM', 'CAPABILITY_AUTO_EXPAND']
        )

        message = f'Game session {game_id} started successfully in account {selected_account}'

    except ClientError as e:
        # Nothing was deployed, so hand the account straight back to the pool
        # and take the session with it.
        _abandon(table, session_id, account_manager, selected_account)

        if e.response['Error']['Code'] == 'NoSuchKey':
            print(f"Error: Template not found at s3://{bucket_name}/{template_key}")
            return _response(404, {'error': f'Game template for gameId {game_id} not found.'})

        print(f"Error creating CloudFormation stack: {e}")
        return _response(500, {
            'error': 'Could not create CloudFormation stack.',
            'details': e.response['Error']['Message'],
        })
    except Exception as e:
        _abandon(table, session_id, account_manager, selected_account)
        print(f"An unexpected error occurred during deployment: {e}")
        return _response(500, {'error': 'An unexpected error occurred during deployment.'})

    table.update_item(
        Key={'session_id': session_id},
        UpdateExpression='SET stack_status = :status',
        ExpressionAttributeValues={':status': 'CREATE_IN_PROGRESS'}
    )

    # Last, so that a start which fell over earlier leaves no trace saying
    # the player is in a game they are not in. Best effort: the lab is up,
    # and a wrong chip is not worth failing that.
    try:
        game_progress.mark_playing(user_name, game_id, session_id)
    except Exception as e:
        print(f"Could not record {user_name} as playing {game_id}: {e}")

    return _response(200, {
        'message': message,
        'sessionId': session_id
    })
