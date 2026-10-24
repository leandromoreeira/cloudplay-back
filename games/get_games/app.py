import json
import os
import re
import boto3
from botocore.exceptions import ClientError

# Import from Lambda Layer
from access_control import get_identity, get_accessible_game_ids

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['GAMES_TABLE'])

def lambda_handler(event, context):
    email, groups = get_identity(event)
    accessible_game_ids = get_accessible_game_ids(email, groups)

    query_params = event.get('queryStringParameters') or {}
    page = int(query_params.get('page', 1))
    page_size = int(query_params.get('pageSize', 10))
    title = query_params.get('title')
    category = query_params.get('category')
    difficulty = query_params.get('difficulty')
    order = query_params.get('order', 'desc').lower()  # Default to desc

    scan_kwargs = {}
    filter_expression = []
    expression_attribute_values = {}

    if title:
        filter_expression.append('contains(#title, :title)')
        scan_kwargs['ExpressionAttributeNames'] = {'#title': 'title'}
        expression_attribute_values[':title'] = title
    if category:
        filter_expression.append('category = :category')
        expression_attribute_values[':category'] = category
    if difficulty:
        filter_expression.append('difficulty = :difficulty')
        expression_attribute_values[':difficulty'] = difficulty

    if filter_expression:
        scan_kwargs['FilterExpression'] = ' AND '.join(filter_expression)
    if expression_attribute_values:
        scan_kwargs['ExpressionAttributeValues'] = expression_attribute_values

    try:
        response = table.scan(**scan_kwargs)
        items = response.get('Items', [])

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

        reverse_order = order == 'desc'
        items.sort(key=sort_key, reverse=reverse_order)

        total_items = len(items)
        total_pages = (total_items + page_size - 1) // page_size

        start_index = (page - 1) * page_size
        end_index = start_index + page_size
        paginated_items = items[start_index:end_index]

        return {
            'statusCode': 200,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
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
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps({'error': 'Could not fetch games'})
        }