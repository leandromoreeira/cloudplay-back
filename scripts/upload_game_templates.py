#!/usr/bin/env python3
"""
Script to upload game templates to S3 bucket for CloudFormation deployment.
This script uploads all game{id}.yaml files from cloudraiser-games to the S3 bucket.
"""

import os
import boto3
import glob
from pathlib import Path

def upload_game_templates():
    # Configuration
    bucket_name = "cloudraiser-673000199571"
    base_path = Path(__file__).parent.parent / "cloudraiser-games"

    # Initialize S3 client
    s3_client = boto3.client('s3')

    # Check if bucket exists
    try:
        s3_client.head_bucket(Bucket=bucket_name)
        print(f"✅ Bucket {bucket_name} exists")
    except Exception as e:
        print(f"❌ Error accessing bucket {bucket_name}: {e}")
        return False

    uploaded_count = 0
    failed_count = 0

    # Find all game directories
    for game_dir in base_path.glob("game*"):
        if game_dir.is_dir():
            game_id = game_dir.name

            # Look for YAML template files
            yaml_files = list(game_dir.glob("*.yaml")) + list(game_dir.glob("*.yml"))

            for yaml_file in yaml_files:
                # Upload main template (game{id}.yaml)
                if yaml_file.name == f"{game_id}.yaml":
                    s3_key = f"{game_id}/{yaml_file.name}"

                    try:
                        print(f"📤 Uploading {yaml_file} to s3://{bucket_name}/{s3_key}")

                        s3_client.upload_file(
                            str(yaml_file),
                            bucket_name,
                            s3_key,
                            ExtraArgs={
                                'ContentType': 'application/x-yaml',
                                'ServerSideEncryption': 'AES256'
                            }
                        )

                        print(f"✅ Successfully uploaded {s3_key}")
                        uploaded_count += 1

                    except Exception as e:
                        print(f"❌ Failed to upload {yaml_file}: {e}")
                        failed_count += 1

                # Upload other YAML files (like validation_lambdas.yml)
                else:
                    s3_key = f"{game_id}/{yaml_file.name}"

                    try:
                        s3_client.upload_file(
                            str(yaml_file),
                            bucket_name,
                            s3_key,
                            ExtraArgs={
                                'ContentType': 'application/x-yaml',
                                'ServerSideEncryption': 'AES256'
                            }
                        )
                        print(f"✅ Successfully uploaded additional template {s3_key}")
                        uploaded_count += 1

                    except Exception as e:
                        print(f"❌ Failed to upload {yaml_file}: {e}")
                        failed_count += 1

            # Upload ZIP files (lambda functions)
            zip_files = list(game_dir.glob("*.zip"))
            for zip_file in zip_files:
                s3_key = f"{game_id}/{zip_file.name}"

                try:
                    s3_client.upload_file(
                        str(zip_file),
                        bucket_name,
                        s3_key,
                        ExtraArgs={
                            'ContentType': 'application/zip',
                            'ServerSideEncryption': 'AES256'
                        }
                    )
                    print(f"✅ Successfully uploaded {s3_key}")
                    uploaded_count += 1

                except Exception as e:
                    print(f"❌ Failed to upload {zip_file}: {e}")
                    failed_count += 1

    print(f"\n📊 Upload Summary:")
    print(f"   ✅ Successfully uploaded: {uploaded_count} files")
    print(f"   ❌ Failed uploads: {failed_count} files")

    return failed_count == 0

def list_bucket_contents():
    """List current contents of the bucket for verification"""
    bucket_name = "cloudraiser-673000199571"
    s3_client = boto3.client('s3')

    try:
        response = s3_client.list_objects_v2(Bucket=bucket_name)

        if 'Contents' in response:
            print(f"\n📁 Current bucket contents ({len(response['Contents'])} files):")
            for obj in sorted(response['Contents'], key=lambda x: x['Key']):
                print(f"   📄 {obj['Key']} ({obj['Size']} bytes)")
        else:
            print(f"\n📁 Bucket {bucket_name} is empty")

    except Exception as e:
        print(f"❌ Error listing bucket contents: {e}")

if __name__ == "__main__":
    print("🚀 Starting game templates upload...")

    success = upload_game_templates()

    if success:
        print("\n🎉 All templates uploaded successfully!")
        list_bucket_contents()
    else:
        print("\n⚠️  Some uploads failed. Please check the errors above.")

    print("\n💡 Next steps:")
    print("   1. Redeploy your SAM application: sam deploy")
    print("   2. Test the start_game_session function")