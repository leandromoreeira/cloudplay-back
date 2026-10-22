import os
import json
import boto3
import uuid
from datetime import datetime
from botocore.exceptions import ClientError

# Import from Lambda Layer
import team_role
from account_manager import get_account_manager
from access_control import get_identity, can_access_game

AWS_REGION = os.environ['AWS_REGION']

def lambda_handler(event, context):
    game_id = event['pathParameters']['gameId']
    user_name, groups = get_identity(event)

    if not user_name:
        return {
            'statusCode': 401,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({'message': 'Invalid or expired token'})
        }

    if not can_access_game(user_name, groups, game_id):
        return {
            'statusCode': 403,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({'message': 'You do not have access to this game'})
        }

    # Initialize account manager
    account_manager = get_account_manager()

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
            return {
                'statusCode': 400,
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*'
                },
                'body': json.dumps({'message': 'You already have this game in progress'})
            }

        # Claim an account for this session. The claim is atomic, so concurrent
        # starts can't be handed the same account.
        session_id = str(uuid.uuid4())
        selected_account = account_manager.claim_account(session_id)
        if not selected_account:
            return {
                'statusCode': 503,  # Service Unavailable
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*'
                },
                'body': json.dumps({
                    'message': 'All game environments are currently busy. Please try again later.',
                    'error_code': 'NO_AVAILABLE_ACCOUNTS'
                })
            }

        print(f"Selected account {selected_account} for game deployment")

    except ClientError as e:
        print(f"Error querying DynamoDB: {e}")
        return {
            "statusCode": 500,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            "body": json.dumps(
                {
                    "error": "Could not retrieve game status.",
                    "details": e.response["Error"]["Message"],
                }
            ),
        }
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return {
            "statusCode": 500,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            "body": json.dumps({"error": "An unexpected error occurred."}),
        }

    stack_name = f'game-session-{session_id}'
    bucket_name = os.environ['S3_BUCKET_NAME']
    template_key = f'{game_id}/{game_id}.yaml'
    template_url = f'https://{bucket_name}.s3.{AWS_REGION}.amazonaws.com/{template_key}'

    s3_client = boto3.client('s3')

    try:
        # The player's role is set up before the lab is, so that nothing can
        # ever hand out a session you cannot log into. Doing it first also
        # means a failure here leaves the account untouched and the existing
        # error path below can simply give it back to the pool.
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
        stack_response = cf_client.create_stack(
            StackName=stack_name,
            TemplateURL=template_url,
            Capabilities=['CAPABILITY_IAM', 'CAPABILITY_NAMED_IAM', 'CAPABILITY_AUTO_EXPAND']
        )

        message = f'Game session {game_id} started successfully in account {selected_account}'

    except ClientError as e:
        # Nothing was deployed, so hand the account straight back to the pool
        account_manager.mark_available(selected_account)

        if e.response['Error']['Code'] == 'NoSuchKey':
            print(f"Error: Template not found at s3://{bucket_name}/{template_key}")
            return {
                "statusCode": 404,
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*'
                },
                "body": json.dumps({"error": f"Game template for gameId {game_id} not found."}),
            }
        print(f"Error creating CloudFormation stack: {e}")
        return {
            "statusCode": 500,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            "body": json.dumps({
                "error": "Could not create CloudFormation stack.",
                "details": e.response["Error"]["Message"],
            }),
        }
    except Exception as e:
        account_manager.mark_available(selected_account)
        print(f"An unexpected error occurred during deployment: {e}")
        return {
            "statusCode": 500,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            "body": json.dumps({"error": "An unexpected error occurred during deployment."}),
        }

    # Save session with account_id
    table.put_item(
        Item={
            'session_id': session_id,
            'user_name': user_name,
            'game_id': game_id,
            'account_id': selected_account,  # Store the account where game is deployed
            'start_time': datetime.utcnow().isoformat() + 'Z',
            'active': True,
            'end_time': None,
            'stack_status': 'CREATE_IN_PROGRESS',
            'stack_outputs': {}  # Initialize empty outputs
        }
    )

    return {
        'statusCode': 200,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        },
        'body': json.dumps({
            'message': message,
            'stackId': stack_response['StackId'],
            'sessionId': session_id,
            'accountId': selected_account
        })
    }