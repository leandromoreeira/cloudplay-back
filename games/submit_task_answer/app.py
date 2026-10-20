import json
import boto3
import os
import re
from botocore.exceptions import ClientError

# Import from Lambda Layer
from account_manager import get_account_manager
from access_control import get_identity, can_access_game

def lambda_handler(event, context):
    game_id = event['pathParameters']['gameId']
    task_id = event['pathParameters']['taskId']
    body = json.loads(event['body'])
    answer = body.get('answer')

    user_name, groups = get_identity(event)
    team_display_name = user_name or 'Unknown Team'  # Use email as team name for now

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

    # Extract task number from task_id (e.g., "task1" -> 1)
    task_number_match = re.search(r'\d+', task_id)
    if not task_number_match:
        return {
            'statusCode': 400,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({'message': 'Invalid task ID format'})
        }

    task_number = int(task_number_match.group())

    try:
        # Get game session and stack outputs
        dynamodb = boto3.resource('dynamodb')
        sessions_table = dynamodb.Table(os.environ['GAME_SESSIONS_TABLE'])
        games_table = dynamodb.Table(os.environ['GAMES_TABLE'])

        # Find active session for this user and game
        session_response = sessions_table.query(
            IndexName='user_game_index',
            KeyConditionExpression='user_name = :user_name and game_id = :game_id',
            ExpressionAttributeValues={
                ':user_name': user_name,
                ':game_id': game_id,
                ':active': True
            },
            FilterExpression='active = :active'
        )

        if not session_response["Items"]:
            return {
                'statusCode': 404,
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*'
                },
                'body': json.dumps({'message': 'No active game session found'})
            }

        session = session_response["Items"][0]
        stack_outputs = session.get('stack_outputs', {})
        account_id = session.get('account_id')  # Get the account where game is deployed

        # Get game details to find task title
        game_response = games_table.get_item(Key={'id': game_id})
        if 'Item' not in game_response:
            return {
                'statusCode': 404,
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*'
                },
                'body': json.dumps({'message': 'Game not found'})
            }

        game = game_response['Item']

        # Find the specific task to get its title
        task_title = f"Task {task_number}"  # Default fallback
        for task in game.get('tasks', []):
            if task.get('id') == task_id:
                task_title = task.get('title', task_title)
                break

        # Prepare payload for validate_task lambda
        validation_payload = {
            "taskTitle": task_title,
            "taskId": task_id,
            "taskNumber": task_number,
            "teamDisplayName": team_display_name,
            "userInput": answer,
            "stackOutputParams": stack_outputs
        }

        validate_function_name = f"validate_task{task_number}"

        # Create Lambda client (cross-account if needed)
        if account_id:
            # Use cross-account Lambda invocation
            print(f"Using cross-account Lambda invocation for account {account_id}")
            account_manager = get_account_manager()

            try:
                # Get cross-account credentials
                credentials = account_manager.assume_cross_account_role(account_id)

                # Create Lambda client with cross-account credentials
                lambda_client = boto3.client(
                    'lambda',
                    aws_access_key_id=credentials['AccessKeyId'],
                    aws_secret_access_key=credentials['SecretAccessKey'],
                    aws_session_token=credentials['SessionToken'],
                    region_name=os.environ.get('AWS_REGION', 'us-east-1')
                )

            except Exception as e:
                print(f"Failed to assume cross-account role for account {account_id}: {e}")
                return {
                    'statusCode': 500,
                    'headers': {
                        'Content-Type': 'application/json',
                        'Access-Control-Allow-Origin': '*'
                    },
                    'body': json.dumps({
                        'message': 'Failed to access game environment for validation',
                        'error': str(e)
                    })
                }
        else:
            # Use local account Lambda client (for backward compatibility)
            print("Using local Lambda client (no account_id found)")
            lambda_client = boto3.client('lambda')

        try:
            print(f"Invoking {validate_function_name} in account {account_id or 'local'}")
            response = lambda_client.invoke(
                FunctionName=validate_function_name,
                InvocationType='RequestResponse',
                Payload=json.dumps(validation_payload)
            )

            # Parse the response from validate_task lambda
            response_payload = json.loads(response['Payload'].read())

            if response.get('StatusCode') == 200:
                # Extract validation results
                completed = response_payload.get('completed', False)
                message = response_payload.get('message', 'Task validation completed')
                progress_percent = response_payload.get('progressPercent', 0)
                metadata = response_payload.get('metadata', {})

                return {
                    'statusCode': 200,
                    'headers': {
                        'Content-Type': 'application/json',
                        'Access-Control-Allow-Origin': '*'
                    },
                    'body': json.dumps({
                        'message': f'Answer for task {task_id} in game {game_id} validated',
                        'submittedAnswer': answer,
                        'validation': {
                            'completed': completed,
                            'message': message,
                            'progressPercent': progress_percent,
                            'metadata': metadata
                        }
                    })
                }
            else:
                return {
                    'statusCode': 500,
                    'headers': {
                        'Content-Type': 'application/json',
                        'Access-Control-Allow-Origin': '*'
                    },
                    'body': json.dumps({
                        'message': 'Validation function returned an error',
                        'details': response_payload
                    })
                }

        except ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                return {
                    'statusCode': 404,
                    'headers': {
                        'Content-Type': 'application/json',
                        'Access-Control-Allow-Origin': '*'
                    },
                    'body': json.dumps({
                        'message': f'Validation function {validate_function_name} not found in account {account_id or "local"}',
                        'submittedAnswer': answer
                    })
                }
            else:
                print(f"Lambda invocation error: {e}")
                return {
                    'statusCode': 500,
                    'headers': {
                        'Content-Type': 'application/json',
                        'Access-Control-Allow-Origin': '*'
                    },
                    'body': json.dumps({
                        'message': 'Failed to invoke validation function',
                        'error': str(e),
                        'submittedAnswer': answer
                    })
                }

    except Exception as e:
        print(f"Error in submit_task_answer: {e}")
        return {
            'statusCode': 500,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({
                'message': 'Internal server error during task validation',
                'error': str(e)
            })
        }