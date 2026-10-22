"""
Shared setup for the unit tests.

The handlers are written against the Lambda layer SAM mounts for them, so the
layer goes on sys.path here and each handler is loaded straight from its own
directory — several of them are called app.py, which is exactly why they can't
just be imported by name.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

BACK = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACK / 'layers' / 'utils-layer'))

# moto refuses to start without something that looks like credentials, and
# picking up real ones here would be a lot worse than failing.
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ.setdefault('AWS_ACCESS_KEY_ID', 'testing')
os.environ.setdefault('AWS_SECRET_ACCESS_KEY', 'testing')
os.environ.setdefault('AWS_SECURITY_TOKEN', 'testing')
os.environ.setdefault('AWS_SESSION_TOKEN', 'testing')

ACCOUNTS_TABLE = 'test-Game-Accounts'
SESSIONS_TABLE = 'test-Game-Sessions'
ACCESS_TABLE = 'test-Game-Access'
CLEANUP_PROJECT = 'test-account-cleanup'
GAMES_BUCKET = 'test-cloudraiser-games'
ALERT_EMAIL = 'alerts@example.com'
ACCOUNT_A = '111111111111'
ACCOUNT_B = '222222222222'


def load_handler(name):
    """Load games/<name>/app.py under a module name of its own."""
    path = BACK / 'games' / name / 'app.py'
    spec = importlib.util.spec_from_file_location(f"handler_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def aws():
    """A clean set of mocked AWS services for one test."""
    from moto import mock_aws

    with mock_aws():
        yield


@pytest.fixture
def accounts_table(aws, monkeypatch):
    import boto3

    monkeypatch.setenv('GAME_ACCOUNTS_TABLE', ACCOUNTS_TABLE)
    dynamodb = boto3.resource('dynamodb')
    dynamodb.create_table(
        TableName=ACCOUNTS_TABLE,
        KeySchema=[{'AttributeName': 'account_id', 'KeyType': 'HASH'}],
        AttributeDefinitions=[{'AttributeName': 'account_id', 'AttributeType': 'S'}],
        BillingMode='PAY_PER_REQUEST',
    )
    return dynamodb.Table(ACCOUNTS_TABLE)


@pytest.fixture
def sessions_table(aws, monkeypatch):
    import boto3

    monkeypatch.setenv('GAME_SESSIONS_TABLE', SESSIONS_TABLE)
    dynamodb = boto3.resource('dynamodb')
    dynamodb.create_table(
        TableName=SESSIONS_TABLE,
        KeySchema=[{'AttributeName': 'session_id', 'KeyType': 'HASH'}],
        AttributeDefinitions=[
            {'AttributeName': 'session_id', 'AttributeType': 'S'},
            {'AttributeName': 'user_name', 'AttributeType': 'S'},
            {'AttributeName': 'game_id', 'AttributeType': 'S'},
        ],
        GlobalSecondaryIndexes=[{
            'IndexName': 'user_game_index',
            'KeySchema': [
                {'AttributeName': 'user_name', 'KeyType': 'HASH'},
                {'AttributeName': 'game_id', 'KeyType': 'RANGE'},
            ],
            'Projection': {'ProjectionType': 'ALL'},
        }],
        BillingMode='PAY_PER_REQUEST',
    )
    return dynamodb.Table(SESSIONS_TABLE)


@pytest.fixture
def access_table(aws, monkeypatch):
    import boto3

    monkeypatch.setenv('GAME_ACCESS_TABLE', ACCESS_TABLE)
    dynamodb = boto3.resource('dynamodb')
    dynamodb.create_table(
        TableName=ACCESS_TABLE,
        KeySchema=[
            {'AttributeName': 'user_email', 'KeyType': 'HASH'},
            {'AttributeName': 'game_id', 'KeyType': 'RANGE'},
        ],
        AttributeDefinitions=[
            {'AttributeName': 'user_email', 'AttributeType': 'S'},
            {'AttributeName': 'game_id', 'AttributeType': 'S'},
        ],
        BillingMode='PAY_PER_REQUEST',
    )
    return dynamodb.Table(ACCESS_TABLE)


@pytest.fixture
def games_bucket(aws, monkeypatch):
    """The bucket holding game templates and their iam.json."""
    import boto3

    monkeypatch.setenv('S3_BUCKET_NAME', GAMES_BUCKET)
    boto3.client('s3').create_bucket(Bucket=GAMES_BUCKET)
    return GAMES_BUCKET


@pytest.fixture
def cleanup_project(aws, monkeypatch):
    """A CodeBuild project for the teardown to start, like the real one."""
    import boto3

    monkeypatch.setenv('CLEANUP_PROJECT_NAME', CLEANUP_PROJECT)
    boto3.client('codebuild').create_project(
        name=CLEANUP_PROJECT,
        # Real CodeBuild takes NO_SOURCE without a location; moto insists on
        # one, so this is here for moto's benefit and nothing else.
        source={'type': 'NO_SOURCE', 'location': 'none', 'buildspec': 'version: 0.2'},
        artifacts={'type': 'NO_ARTIFACTS'},
        environment={
            'type': 'LINUX_CONTAINER',
            'image': 'aws/codebuild/amazonlinux2-x86_64-standard:5.0',
            'computeType': 'BUILD_GENERAL1_SMALL',
        },
        serviceRole='arn:aws:iam::123456789012:role/test-cleanup',
    )
    return CLEANUP_PROJECT


@pytest.fixture
def verified_sender(aws, monkeypatch):
    """SES only delivers between verified identities, in moto as in the sandbox."""
    import boto3

    monkeypatch.setenv('ALERT_EMAIL', ALERT_EMAIL)
    boto3.client('ses').verify_email_identity(EmailAddress=ALERT_EMAIL)
    return ALERT_EMAIL


def sent_emails():
    """Everything SES accepted so far in this test."""
    from moto.core import DEFAULT_ACCOUNT_ID
    from moto.ses.models import ses_backends

    region = os.environ['AWS_DEFAULT_REGION']
    return ses_backends[DEFAULT_ACCOUNT_ID][region].sent_messages
