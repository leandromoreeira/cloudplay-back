#!/usr/bin/env python3
"""
Script to set up the S3 bucket for game templates with proper permissions.
"""

import boto3
import json
import sys
from botocore.exceptions import ClientError

def create_bucket_if_not_exists(bucket_name, region='us-east-1'):
    """Create S3 bucket if it doesn't exist"""
    s3_client = boto3.client('s3', region_name=region)

    try:
        # Try to check if bucket exists
        s3_client.head_bucket(Bucket=bucket_name)
        print(f"✅ Bucket {bucket_name} already exists")
        return True

    except ClientError as e:
        error_code = int(e.response['Error']['Code'])

        if error_code == 404:
            # Bucket doesn't exist, create it
            print(f"📦 Creating bucket {bucket_name}...")

            try:
                if region == 'us-east-1':
                    # For us-east-1, don't specify location constraint
                    s3_client.create_bucket(Bucket=bucket_name)
                else:
                    s3_client.create_bucket(
                        Bucket=bucket_name,
                        CreateBucketConfiguration={'LocationConstraint': region}
                    )

                print(f"✅ Successfully created bucket {bucket_name}")
                return True

            except ClientError as create_error:
                print(f"❌ Failed to create bucket: {create_error}")
                return False

        elif error_code == 403:
            print(f"❌ Access denied to bucket {bucket_name}. Check your permissions.")
            return False
        else:
            print(f"❌ Error checking bucket: {e}")
            return False

def set_bucket_policy(bucket_name):
    """Set bucket policy to allow CloudFormation access"""
    s3_client = boto3.client('s3')

    # Get current AWS account ID
    sts_client = boto3.client('sts')
    account_id = sts_client.get_caller_identity()['Account']

    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowCloudFormationAccess",
                "Effect": "Allow",
                "Principal": {
                    "Service": "cloudformation.amazonaws.com"
                },
                "Action": [
                    "s3:GetObject",
                    "s3:GetObjectVersion"
                ],
                "Resource": f"arn:aws:s3:::{bucket_name}/*"
            },
            {
                "Sid": "AllowAccountAccess",
                "Effect": "Allow",
                "Principal": {
                    "AWS": f"arn:aws:iam::{account_id}:root"
                },
                "Action": [
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:DeleteObject",
                    "s3:ListBucket"
                ],
                "Resource": [
                    f"arn:aws:s3:::{bucket_name}/*",
                    f"arn:aws:s3:::{bucket_name}"
                ]
            }
        ]
    }

    try:
        s3_client.put_bucket_policy(
            Bucket=bucket_name,
            Policy=json.dumps(policy)
        )
        print(f"✅ Successfully applied bucket policy to {bucket_name}")
        return True

    except ClientError as e:
        print(f"❌ Failed to set bucket policy: {e}")
        return False

def enable_versioning(bucket_name):
    """Enable versioning on the bucket"""
    s3_client = boto3.client('s3')

    try:
        s3_client.put_bucket_versioning(
            Bucket=bucket_name,
            VersioningConfiguration={'Status': 'Enabled'}
        )
        print(f"✅ Enabled versioning on {bucket_name}")
        return True

    except ClientError as e:
        print(f"⚠️  Failed to enable versioning: {e}")
        return False

def main():
    bucket_name = "cloudraiser-673000199571"
    region = "us-east-1"

    print(f"🚀 Setting up S3 bucket {bucket_name}...")

    # Step 1: Create bucket if needed
    if not create_bucket_if_not_exists(bucket_name, region):
        print("❌ Failed to create/access bucket. Exiting.")
        sys.exit(1)

    # Step 2: Set bucket policy
    if not set_bucket_policy(bucket_name):
        print("⚠️  Failed to set bucket policy. CloudFormation may not be able to access templates.")

    # Step 3: Enable versioning (optional but recommended)
    enable_versioning(bucket_name)

    print(f"\n🎉 Bucket setup completed!")
    print(f"📋 Next steps:")
    print(f"   1. Run: python upload_game_templates.py")
    print(f"   2. Deploy SAM: sam deploy")

if __name__ == "__main__":
    main()