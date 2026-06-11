import csv
import io
import json
import os
from datetime import datetime, timedelta, timezone

import boto3


REGION = os.getenv("AWS_REGION", "cn-northwest-1")
DAYS_THRESHOLD = int(os.getenv("DAYS_THRESHOLD", "180"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
DELETE_EMPTY_REPOSITORIES = os.getenv("DELETE_EMPTY_REPOSITORIES", "false").lower() == "true"
REPORT_BUCKET = os.getenv("REPORT_BUCKET", "")
SNS_TOPIC_ARN = os.getenv("SNS_TOPIC_ARN", "")
DELETE_ALLOWLIST = {
    item.strip()
    for item in os.getenv("DELETE_ALLOWLIST", "").replace("\n", ",").split(",")
    if item.strip()
}

ecr = boto3.client("ecr", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
sns = boto3.client("sns", region_name=REGION)


def image_is_stale(detail, cutoff):
    last_pull = detail.get("lastRecordedPullTime")
    pushed_at = detail.get("imagePushedAt")
    reference_time = last_pull or pushed_at
    return reference_time is not None and reference_time < cutoff


def collect_candidates(cutoff):
    candidates = []
    kept = 0
    repo_paginator = ecr.get_paginator("describe_repositories")
    for repo_page in repo_paginator.paginate():
        for repo in repo_page.get("repositories", []):
            repo_name = repo["repositoryName"]
            image_paginator = ecr.get_paginator("describe_images")
            for image_page in image_paginator.paginate(repositoryName=repo_name):
                for detail in image_page.get("imageDetails", []):
                    if image_is_stale(detail, cutoff):
                        candidates.append(
                            {
                                "repositoryName": repo_name,
                                "imageDigest": detail["imageDigest"],
                                "imageTags": detail.get("imageTags", []),
                                "imagePushedAt": detail.get("imagePushedAt").isoformat()
                                if detail.get("imagePushedAt")
                                else "",
                                "lastRecordedPullTime": detail.get("lastRecordedPullTime").isoformat()
                                if detail.get("lastRecordedPullTime")
                                else "",
                                "imageSizeInBytes": detail.get("imageSizeInBytes", 0),
                            }
                        )
                    else:
                        kept += 1
    return candidates, kept


def allowlist_matches(item):
    if not DELETE_ALLOWLIST:
        return False
    repo = item["repositoryName"]
    digest = item["imageDigest"]
    if repo in DELETE_ALLOWLIST or f"{repo}@{digest}" in DELETE_ALLOWLIST:
        return True
    return any(f"{repo}:{tag}" in DELETE_ALLOWLIST for tag in item.get("imageTags", []))


def filter_delete_candidates(candidates):
    return [item for item in candidates if allowlist_matches(item)]


def delete_candidates(candidates):
    deleted = []
    by_repo = {}
    for item in candidates:
        by_repo.setdefault(item["repositoryName"], []).append(item)

    for repo_name, items in by_repo.items():
        for start in range(0, len(items), 100):
            batch = items[start : start + 100]
            response = ecr.batch_delete_image(
                repositoryName=repo_name,
                imageIds=[{"imageDigest": item["imageDigest"]} for item in batch],
            )
            deleted.extend(response.get("imageIds", []))
    return deleted


def delete_empty_repositories():
    deleted = []
    repo_paginator = ecr.get_paginator("describe_repositories")
    for repo_page in repo_paginator.paginate():
        for repo in repo_page.get("repositories", []):
            repo_name = repo["repositoryName"]
            images = ecr.list_images(repositoryName=repo_name, maxResults=1).get("imageIds", [])
            if not images:
                ecr.delete_repository(repositoryName=repo_name, force=True)
                deleted.append(repo_name)
    return deleted


def csv_report(rows):
    output = io.StringIO()
    fieldnames = [
        "repositoryName",
        "imageDigest",
        "imageTags",
        "imagePushedAt",
        "lastRecordedPullTime",
        "imageSizeInBytes",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        row = dict(row)
        row["imageTags"] = ",".join(row.get("imageTags", []))
        writer.writerow(row)
    return output.getvalue()


def upload_report(summary, candidates):
    if not REPORT_BUCKET:
        return {}
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"reports/{timestamp}"
    json_key = f"{prefix}_report.json"
    csv_key = f"{prefix}_candidates.csv"
    s3.put_object(
        Bucket=REPORT_BUCKET,
        Key=json_key,
        Body=json.dumps({"summary": summary, "candidates": candidates}, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    s3.put_object(
        Bucket=REPORT_BUCKET,
        Key=csv_key,
        Body=csv_report(candidates).encode("utf-8-sig"),
        ContentType="text/csv",
    )
    return {"json": f"s3://{REPORT_BUCKET}/{json_key}", "csv": f"s3://{REPORT_BUCKET}/{csv_key}"}


def publish(summary):
    if SNS_TOPIC_ARN:
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject="container-mirror cleanup report",
            Message=json.dumps(summary, indent=2),
        )


def handler(event, context):
    cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS_THRESHOLD)
    candidates, kept = collect_candidates(cutoff)
    delete_candidates_list = filter_delete_candidates(candidates)
    deleted = [] if DRY_RUN else delete_candidates(delete_candidates_list)
    deleted_repositories = [] if (DRY_RUN or not DELETE_EMPTY_REPOSITORIES or DELETE_ALLOWLIST) else delete_empty_repositories()

    summary = {
        "dryRun": DRY_RUN,
        "daysThreshold": DAYS_THRESHOLD,
        "cutoff": cutoff.isoformat(),
        "candidateImages": len(candidates),
        "deleteAllowlistEntries": len(DELETE_ALLOWLIST),
        "deleteCandidateImages": len(delete_candidates_list),
        "keptImages": kept,
        "deletedImages": len(deleted),
        "deletedRepositories": len(deleted_repositories),
    }
    summary["reports"] = upload_report(summary, candidates)
    publish(summary)
    return summary
