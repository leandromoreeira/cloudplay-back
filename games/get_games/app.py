import json
import os
import re
import boto3
from botocore.exceptions import ClientError

# Import from Lambda Layer
import cors
import game_progress
from access_control import get_identity, get_accessible_game_ids

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['GAMES_TABLE'])

PROGRESS_STATUSES = frozenset(
    (game_progress.TODO, game_progress.IN_PROGRESS, game_progress.DONE)
)

def lambda_handler(event, context):
    email, groups = get_identity(event)
    accessible_game_ids = get_accessible_game_ids(email, groups)

    query_params = event.get('queryStringParameters') or {}
    page = int(query_params.get('page', 1))
    page_size = int(query_params.get('pageSize', 12))
    title = query_params.get('title')
    category = query_params.get('category')
    difficulty = query_params.get('difficulty')
    # Ascending, so a game keeps its place: with the catalogue running to
    # game1000, descending moved every row down a slot each time a game
    # was published. The parameter stays for a caller that wants the
    # other way; nothing in the UI offers it any more.
    order = query_params.get('order', 'asc').lower()
    status = query_params.get('status')

    if status and status not in PROGRESS_STATUSES:
        return {
            'statusCode': 400,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': cors.allowed_origin()
            },
            'body': json.dumps({
                'message': f"status must be one of {', '.join(sorted(PROGRESS_STATUSES))}"
            })
        }

    # Difficulty is one of three exact words, so DynamoDB can do it. Title
    # and category cannot be left to it: a FilterExpression has no way to
    # fold case — there is no lower() — and `contains` would make "VPC"
    # and "vpc" different searches. They are matched below instead, which
    # costs nothing: a filter never reduces the read units of a scan, only
    # what comes back over the wire.
    scan_kwargs = {}
    if difficulty:
        scan_kwargs['FilterExpression'] = 'difficulty = :difficulty'
        scan_kwargs['ExpressionAttributeValues'] = {':difficulty': difficulty}

    try:
        response = table.scan(**scan_kwargs)
        items = response.get('Items', [])

        # Case-folded substring, the way someone typing into a search box
        # expects it to behave.
        if title:
            needle = title.strip().lower()
            items = [i for i in items if needle in str(i.get('title', '')).lower()]
        if category:
            needle = category.strip().lower()
            items = [i for i in items if needle in str(i.get('category', '')).lower()]

        # Games are private by default: keep only games this user was granted
        # access to, unless they're in the full-access group (None = no restriction)
        if accessible_game_ids is not None:
            items = [item for item in items if item.get('id') in accessible_game_ids]

        # Sort items by ID with numeric sorting for game IDs
        def sort_key(item):
            game_id = str(item.get('id', ''))
            # Extract numeric part from game ID (e.g., "game123" -> 123)
            match = re.search(r'(\d+)', game_id)
            if match:
                # Return tuple: (prefix, numeric_part) for proper sorting
                prefix = game_id[:match.start()]
                numeric_part = int(match.group(1))
                return (prefix.lower(), numeric_part)
            else:
                # Fallback to string sorting if no number found
                return (game_id.lower(), 0)

        # One query covers every game this player has touched; anything it
        # does not mention has never been started, which is the default.
        statuses = game_progress.statuses_for(email)

        # Filtered here rather than after paging, so the totals describe the
        # list the player is actually looking at and no page comes back
        # short because its rows were dropped afterwards.
        if status:
            items = [
                item for item in items
                if statuses.get(item.get('id'), game_progress.TODO) == status
            ]

        reverse_order = order == 'desc'
        items.sort(key=sort_key, reverse=reverse_order)

        total_items = len(items)
        total_pages = (total_items + page_size - 1) // page_size

        start_index = (page - 1) * page_size
        end_index = start_index + page_size
        paginated_items = items[start_index:end_index]

        for item in paginated_items:
            item['progress'] = {
                'status': statuses.get(item.get('id'), game_progress.TODO)
            }

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': cors.allowed_origin()
            },
            'body': json.dumps({
                'data': paginated_items,
                'currentPage': page,
                'totalPages': total_pages,
                'totalItems': total_items
            })
        }
    except ClientError as e:
        print(e.response['Error']['Message'])
        return {
            'statusCode': 500,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': cors.allowed_origin()
            },
            'body': json.dumps({'error': 'Could not fetch games'})
        }