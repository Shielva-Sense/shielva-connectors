"""One-time script to create the Cloudflare R2 bucket for integration plan history.

Run: python create_r2_bucket.py
"""

import os


def main() -> None:
    # Lazy imports — boto3/botocore are ops-only deps, not part of the service
    # runtime, so importing this module must not require them.
    import boto3
    from botocore.exceptions import ClientError

    account_id = os.getenv("INTEGRATION_R2_ACCOUNT_ID", "")
    access_key = os.getenv("INTEGRATION_R2_ACCESS_KEY_ID", "")
    secret_key = os.getenv("INTEGRATION_R2_SECRET_ACCESS_KEY", "")
    bucket = os.getenv("INTEGRATION_R2_BUCKET_NAME", "shielvasense")

    if not all([account_id, access_key, secret_key]):
        # Try loading from .env
        from dotenv import load_dotenv

        load_dotenv()
        account_id = os.getenv("INTEGRATION_R2_ACCOUNT_ID", "")
        access_key = os.getenv("INTEGRATION_R2_ACCESS_KEY_ID", "")
        secret_key = os.getenv("INTEGRATION_R2_SECRET_ACCESS_KEY", "")
        bucket = os.getenv("INTEGRATION_R2_BUCKET_NAME", "shielvasense")

    client = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )

    try:
        client.create_bucket(Bucket=bucket)
        print(f"Bucket '{bucket}' created successfully.")
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "BucketAlreadyOwnedByYou":
            print(f"Bucket '{bucket}' already exists — OK.")
        else:
            print(f"Error creating bucket: {e}")
            raise


if __name__ == "__main__":
    main()
