# Setup Instructions: AWS S3

## Overview

The AWS S3 connector integrates Amazon S3 (and any S3-compatible object store — Cloudflare R2, Wasabi, Backblaze B2, MinIO) with the Shielva platform. Once connected, Shielva can list buckets and objects, read and write object bytes, copy objects server-side, generate time-limited presigned URLs, and manage bucket lifecycle.

Authentication uses an IAM access key (Access Key ID + Secret Access Key). The connector signs every request with AWS Signature V4 via the `aiobotocore` SDK (which delegates SigV4 to `botocore`) — your team never needs to handcraft signatures.

---

## Prerequisites

Before you begin, make sure you have:

- An **AWS account** with permission to create IAM users (or an admin who can issue access keys for you).
- An **IAM user** dedicated to this connector — do not reuse a console-login user.
- An **IAM policy** scoped to the buckets the connector should touch (sample below).
- For non-AWS providers (R2, Wasabi, Backblaze, MinIO): the provider's endpoint URL and a compatible access-key pair issued by them.

### Minimum recommended IAM policy

Attach an inline policy like this to the IAM user — replace `<your-bucket-name>` with the actual bucket(s):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BucketLevelOps",
      "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
      "Resource": ["arn:aws:s3:::<your-bucket-name>"]
    },
    {
      "Sid": "ObjectLevelOps",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:CopyObject"
      ],
      "Resource": ["arn:aws:s3:::<your-bucket-name>/*"]
    },
    {
      "Sid": "AccountLevelDiscovery",
      "Effect": "Allow",
      "Action": ["s3:ListAllMyBuckets"],
      "Resource": "*"
    }
  ]
}
```

If you also need `create_bucket` / `delete_bucket`, add `s3:CreateBucket` and `s3:DeleteBucket` on `arn:aws:s3:::*` (these are account-level actions and cannot be scoped to a single bucket name pattern that does not yet exist).

---

## Step-by-Step Configuration

### Step 1: AWS Access Key ID (`access_key_id`) — **Required**

1. Sign in to the [AWS Console](https://console.aws.amazon.com/iam/).
2. Go to **IAM → Users**, click the user you created for this connector.
3. Open the **Security credentials** tab.
4. Under **Access keys**, click **Create access key**.
5. Select **Application running outside AWS** when asked about use case.
6. Copy the **Access key ID** (starts with `AKIA…`, 20 characters).
7. Paste it into the **AWS Access Key ID** field in Shielva.

> **Tip:** The Access Key ID is not a secret on its own — but treat it as PII because it identifies the IAM principal.

---

### Step 2: AWS Secret Access Key (`secret_access_key`) — **Required**

1. On the same AWS screen as Step 1, copy the **Secret access key** — AWS shows this **only once**. If you lose it, you must create a new key.
2. Paste it into the **AWS Secret Access Key** field in Shielva. The value is stored encrypted at rest.

> **Common mistake:** Rotating the access key in AWS without updating Shielva immediately breaks every operation. Always update Shielva first, then deactivate the old key in IAM.

---

### Step 3: AWS Region (`region`) — **Optional**

- **Default:** `us-east-1`
- Set this to the region that hosts your buckets — e.g. `eu-west-1`, `ap-south-1`, `us-west-2`.
- For S3-compatible providers: use the value they document (often `auto`, `wnam`, `us-east-1`).
- A wrong region typically surfaces as `AuthorizationHeaderMalformed` or `PermanentRedirect`.

---

### Step 4: Endpoint URL Override (`endpoint_url`) — **Optional**

Leave blank to talk to AWS S3.

Provide a value to point the connector at an S3-compatible provider:

| Provider | Example endpoint |
|----------|------------------|
| Cloudflare R2 | `https://<account-id>.r2.cloudflarestorage.com` |
| Wasabi | `https://s3.<region>.wasabisys.com` |
| Backblaze B2 | `https://s3.<region>.backblazeb2.com` |
| MinIO (self-hosted) | `https://minio.example.com:9000` |

The connector still signs with SigV4 — the provider must accept SigV4 (all the providers above do).

---

### Step 5: STS Session Token (`session_token`) — **Optional**

Leave blank for long-lived IAM access keys.

Fill this in when you are using **temporary credentials** from:

- `aws sts get-session-token`
- `aws sts assume-role`
- AWS SSO / IAM Identity Center
- Federated identity (SAML, OIDC)

The session token is the third component required by SigV4 alongside the access key ID and secret. The connector stores it encrypted.

> **Warning:** STS tokens expire (commonly 1h–12h). When they expire, every call returns `ExpiredToken` and the connector status drops to `EXPIRED`. Either configure a longer TTL on your AssumeRole policy or wire your secret-rotation system to update the connector before expiry.

---

### Step 6: Default Bucket (`default_bucket`) — **Optional**

A convenience field — the UI uses it to pre-populate bucket pickers in object-level actions. The connector does NOT validate that the bucket exists or that the credentials can reach it (health-check does that for the whole account, not for one bucket).

Leave blank if your team works across many buckets equally.

---

## Testing the Connection

1. After saving, the connector status badge should show **Pending**.
2. Click **Run Health Check** — a successful check calls `ListBuckets`, which validates credentials and (transitively) connectivity to the endpoint. Status flips to **Connected**.
3. Click **List Buckets** to see the buckets your credentials can read.
4. Try **List Objects** with a bucket name to enumerate keys.
5. Try **Generate Presigned URL** with a known key — the URL should open the object directly in your browser. This validates the region + endpoint configuration without writing any data.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `InvalidAccessKeyId` | Access key ID is wrong or has been deactivated | Re-copy from IAM → Users → Security credentials |
| `SignatureDoesNotMatch` | Secret access key is wrong, or there is clock skew on the host | Re-copy the secret; ensure host NTP is healthy (drift > 5 min breaks SigV4) |
| `AccessDenied` on `list_buckets` | IAM policy lacks `s3:ListAllMyBuckets` | Add the `AccountLevelDiscovery` statement from the sample policy |
| `AccessDenied` on a single bucket | IAM policy does not include that bucket's ARN | Add the bucket ARN to the `BucketLevelOps` / `ObjectLevelOps` statements |
| `NoSuchBucket` on a known-good bucket | Wrong region in `region` field | Match the region the bucket was created in |
| `PermanentRedirect` | Bucket lives in a different region | Update `region` to match the bucket's home region |
| `ExpiredToken` shortly after install | STS session token has expired | Issue a new STS session and update `session_token` |
| `BucketNotEmpty` on `delete_bucket` | The bucket has objects, possibly hidden versioning markers | List + delete all objects (and versions, if versioning is enabled) before retrying |
| `EndpointConnectionError` | `endpoint_url` is unreachable from the connector host | Check DNS, TLS, firewall rules; verify the URL by `curl`-ing it |
| Status flips to **EXPIRED** | Credentials no longer accepted | Rotate access key in IAM, update both fields in Shielva, run health check |
