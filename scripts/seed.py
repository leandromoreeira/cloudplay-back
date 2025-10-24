import boto3
import json
import os
import re

# Initialize DynamoDB client
dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
table = dynamodb.Table('aws-jam-Games')

# Function to map difficulty level
def get_difficulty(level):
    if level == "1":
        return "Easy"
    elif level == "2":
        return "Medium"
    else:
        return "Hard"

# Function to extract and transform tasks
def transform_tasks(tasks):
    transformed_tasks = []
    for task_item in tasks:
        task_data = task_item.get('task', {})
        transformed_tasks.append({
            'id': task_data.get('id'),
            'title': re.sub('<[^<]+?>', '', task_data.get('title')),
            'content': task_data.get('content')
        })
    return transformed_tasks

# Path to the games directory
games_directory = '../cloudraiser-games'

# Populate the table
with table.batch_writer() as batch:
    for game_dir in os.listdir(games_directory):
        if game_dir.startswith('game'):
            json_path = os.path.join(games_directory, game_dir, f'{game_dir}.json')
            if os.path.exists(json_path):
                with open(json_path, 'r') as f:
                    game_data = json.load(f)

                    # Create the game item
                    game_item = {
                        'id': game_data.get('game'),
                        'title': re.sub('<[^<]+?>', '', game_data.get('title')),
                        'content': game_data.get('content'),
                        'category': game_data.get('category'),
                        'difficulty': get_difficulty(game_data.get('difficulty')),
                        'tasks': transform_tasks(game_data.get('tasks', []))
                    }
                    batch.put_item(Item=game_item)

print("Successfully seeded the Games table from JSON files.")