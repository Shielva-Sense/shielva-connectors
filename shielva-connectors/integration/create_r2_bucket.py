"""One-time script to create the Cloudflare R2 bucket for integration plan history.

Run: python create_r2_bucket.py
"""

import os

import boto3
from botocore.exceptions import ClientError

ACCOUNT_ID = os.getenv("INTEGRATION_R2_ACCOUNT_ID", "")
ACCESS_KEY = os.getenv("INTEGRATION_R2_ACCESS_KEY_ID", "")
SECRET_KEY = os.getenv("INTEGRATION_R2_SECRET_ACCESS_KEY", "")
BUCKET = os.getenv("INTEGRATION_R2_BUCKET_NAME", "shielvasense")

if not all([ACCOUNT_ID, ACCESS_KEY, SECRET_KEY]):
    # Try loading from .env
    from dotenv import load_dotenv
    load_dotenv()
    ACCOUNT_ID = os.getenv("INTEGRATION_R2_ACCOUNT_ID", "")
    ACCESS_KEY = os.getenv("INTEGRATION_R2_ACCESS_KEY_ID", "")
    SECRET_KEY = os.getenv("INTEGRATION_R2_SECRET_ACCESS_KEY", "")
    BUCKET = os.getenv("INTEGRATION_R2_BUCKET_NAME", "shielvasense")

client = boto3.client(
    "s3",
    endpoint_url=f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=ACCESS_KEY,
    aws_secret_access_key=SECRET_KEY,
    region_name="auto",
)

try:
    client.create_bucket(Bucket=BUCKET)
    print(f"Bucket '{BUCKET}' created successfully.")
except ClientError as e:
    code = e.response.get("Error", {}).get("Code", "")
    if code == "BucketAlreadyOwnedByYou":
        print(f"Bucket '{BUCKET}' already exists — OK.")
    else:
        print(f"Error creating bucket: {e}")
        raise
