import json
import os
import re
import boto3
from botocore.exceptions import ClientError

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['GAMES_TABLE'])

def lambda_handler(event, context):
    query_params = event.get('queryStringParameters') or {}
    page = int(query_params.get('page', 1))
    page_size = int(query_params.get('pageSize', 10))
    title = query_params.get('title')
    category = query_params.get('category')
    difficulty = query_params.get('difficulty')
    order = query_params.get('order', 'desc').lower()  # Default to desc
    exclude_chinese = query_params.get('excludeChinese', 'true').lower() == 'true'  # Default to exclude

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

        # Filter out games that contain Chinese characters
        def contains_chinese(text):
            """Check if text contains Chinese characters"""
            if not text:
                return False
            # Chinese characters Unicode range: U+4E00-U+9FFF
            chinese_pattern = re.compile(r'[\u4e00-\u9fff]+')
            return bool(chinese_pattern.search(str(text)))

        # Filter out games with Chinese content in title or content (if enabled)
        if exclude_chinese:
            filtered_items = []
            for item in items:
                title = item.get('title', '')
                content = item.get('content', '')

                # Skip games that have Chinese in title or content
                if not contains_chinese(title) and not contains_chinese(content):
                    filtered_items.append(item)

            items = filtered_items

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