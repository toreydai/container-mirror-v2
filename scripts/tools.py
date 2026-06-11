#!/usr/bin/env python3
import csv
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path


TARGET_REGISTRY = os.getenv("ECR_REGISTRY", "")
ALLOWED_PREFIXES = (
    "dockerhub/",
    "gcr/",
    "registryk8sio/",
    "quay/",
    "ecrpublic/",
    "ghcr/",
    "elastic/",
    "amazonecr/",
)
WINDOWS_DENY = ("windowsservercore", "nanoserver")

PREFIX_REVERSE = (
    ("dockerhub/library/", ""),
    ("dockerhub/", ""),
    ("gcr/google_containers/", "k8s.gcr.io/"),
    ("gcr/", "gcr.io/"),
    ("registryk8sio/", "registry.k8s.io/"),
    ("quay/", "quay.io/"),
    ("ecrpublic/", "public.ecr.aws/"),
    ("ghcr/", "ghcr.io/"),
    ("elastic/", "docker.elastic.co/"),
    ("amazonecr/", "602401143452.dkr.ecr.us-west-2.amazonaws.com/"),
)

SOURCE_PREFIXES = (
    ("docker.io/library/", "dockerhub/library/"),
    ("docker.io/", "dockerhub/"),
    ("k8s.gcr.io/", "gcr/google_containers/"),
    ("registry.k8s.io/", "registryk8sio/"),
    ("gcr.io/google-containers/", "gcr/google_containers/"),
    ("gcr.io/", "gcr/"),
    ("asia.gcr.io/", "gcr/"),
    ("eu.gcr.io/", "gcr/"),
    ("us.gcr.io/", "gcr/"),
    ("quay.io/", "quay/"),
    ("public.ecr.aws/docker/library/", "dockerhub/library/"),
    ("public.ecr.aws/", "ecrpublic/"),
    ("ghcr.io/", "ghcr/"),
    ("docker.elastic.co/", "elastic/"),
    ("602401143452.dkr.ecr.us-west-2.amazonaws.com/", "amazonecr/"),
)


def load_yaml(path):
    try:
        import yaml
    except ImportError:
        print("PyYAML is required: pip install pyyaml", file=sys.stderr)
        sys.exit(2)
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def validate_config(path):
    data = load_yaml(path)
    errors = []
    if not isinstance(data, dict):
        errors.append("config must be a YAML object")
    elif data.get("version") != 1:
        errors.append("version must be 1")

    sync = data.get("sync") if isinstance(data, dict) else None
    if not isinstance(sync, list) or not sync:
        errors.append("sync must be a non-empty list")
        sync = []

    seen_targets = set()
    for idx, item in enumerate(sync):
        label = f"sync[{idx}]"
        if not isinstance(item, dict):
            errors.append(f"{label} must be an object")
            continue
        source = item.get("source", "")
        target = item.get("target", "")
        sync_type = item.get("type")
        if sync_type not in ("image", "repository"):
            errors.append(f"{label}.type must be image or repository")
        if ":" not in source and sync_type == "image":
            errors.append(f"{label}.source image entries must include an explicit tag")
        if not target.startswith(TARGET_REGISTRY + "/"):
            errors.append(f"{label}.target must start with {TARGET_REGISTRY}/")
        else:
            path_part = target[len(TARGET_REGISTRY) + 1 :]
            if not path_part.startswith(ALLOWED_PREFIXES):
                errors.append(f"{label}.target prefix is not allowed: {path_part}")
            expected_prefix = expected_target_prefix(source)
            if expected_prefix and not path_part.startswith(expected_prefix):
                errors.append(
                    f"{label}.target prefix {path_part.split('/')[0]}/ does not match source; expected {expected_prefix}"
                )
        if target in seen_targets:
            errors.append(f"{label}.target duplicates another sync entry: {target}")
        seen_targets.add(target)

        tags = item.get("tags") or {}
        deny = tags.get("deny") or []
        if sync_type == "repository":
            deny_text = "\n".join(str(rule).lower() for rule in deny)
            for pattern in WINDOWS_DENY:
                if pattern not in deny_text:
                    errors.append(f"{label}.tags.deny should include {pattern}")
        if re.search(r"\s", source):
            errors.append(f"{label}.source contains whitespace")
        if re.search(r"\s", target):
            errors.append(f"{label}.target contains whitespace")

    if errors:
        for error in errors:
            print(f"[FAIL] {error}", file=sys.stderr)
        return 1
    print(f"[OK] {path} passed container-mirror validation")
    return 0


def expected_target_prefix(source):
    normalized = normalize_source(source)
    for source_prefix, target_prefix in SOURCE_PREFIXES:
        if normalized.startswith(source_prefix):
            return target_prefix
    if "/" not in normalized:
        return "dockerhub/library/"
    first = normalized.split("/")[0]
    if "." not in first and ":" not in first:
        return "dockerhub/"
    return None


def normalize_source(source):
    if "@" in source:
        return source.split("@", 1)[0]
    if ":" in source.rsplit("/", 1)[-1]:
        return source.rsplit(":", 1)[0]
    return source


def reverse_image(repository_name, tag):
    image = f"{repository_name}:{tag}"
    for prefix, replacement in PREFIX_REVERSE:
        if image.startswith(prefix):
            return replacement + image[len(prefix) :]
    return image


def list_ecr_images(ecr):
    for repo_page in ecr.get_paginator("describe_repositories").paginate():
        for repo in repo_page.get("repositories", []):
            name = repo["repositoryName"]
            for image_page in ecr.get_paginator("describe_images").paginate(repositoryName=name):
                for detail in image_page.get("imageDetails", []):
                    for tag in detail.get("imageTags", []) or []:
                        yield {
                            "source": reverse_image(name, tag),
                            "target": f"{TARGET_REGISTRY}/{name}:{tag}",
                            "repository": name,
                            "tag": tag,
                            "digest": detail.get("imageDigest", ""),
                            "pushedAt": detail.get("imagePushedAt").isoformat() if detail.get("imagePushedAt") else "",
                            "lastRecordedPullTime": detail.get("lastRecordedPullTime").isoformat()
                            if detail.get("lastRecordedPullTime")
                            else "",
                            "sizeBytes": detail.get("imageSizeInBytes", 0),
                        }


def write_catalog(records, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    payload = {"generatedAt": datetime.now(timezone.utc).isoformat(), "images": records}
    with open(os.path.join(output_dir, "mirrored-images.txt"), "w", encoding="utf-8") as handle:
        for item in records:
            handle.write(item["source"] + "\n")
    with open(os.path.join(output_dir, "mirrored-images.json"), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    with open(os.path.join(output_dir, "mirrored-images.csv"), "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0].keys()) if records else ["source"])
        writer.writeheader()
        writer.writerows(records)


def upload_catalog(output_dir, bucket, prefix):
    import boto3

    s3 = boto3.client("s3", region_name=os.getenv("AWS_REGION", "cn-northwest-1"))
    uploaded = []
    for name, content_type in (
        ("mirrored-images.txt", "text/plain; charset=utf-8"),
        ("mirrored-images.json", "application/json"),
        ("mirrored-images.csv", "text/csv; charset=utf-8"),
    ):
        key = f"{prefix.rstrip('/')}/{name}" if prefix else name
        s3.upload_file(
            os.path.join(output_dir, name),
            bucket,
            key,
            ExtraArgs={"ContentType": content_type},
        )
        uploaded.append(f"s3://{bucket}/{key}")
    return uploaded


def generate_catalog():
    import boto3

    region = os.getenv("AWS_REGION", "cn-northwest-1")
    output_dir = os.getenv("CATALOG_OUTPUT_DIR", "catalog")
    catalog_bucket = os.getenv("CATALOG_BUCKET", "")
    catalog_prefix = os.getenv("CATALOG_PREFIX", "catalog")
    records = sorted(list_ecr_images(boto3.client("ecr", region_name=region)), key=lambda item: item["source"])
    write_catalog(records, output_dir)
    uploaded = upload_catalog(output_dir, catalog_bucket, catalog_prefix) if catalog_bucket else []
    print(json.dumps({"generated": len(records), "outputDir": output_dir, "uploaded": uploaded}))
    return 0


def has_tag(image_path):
    """Return True if the last path segment contains an explicit tag."""
    return ":" in image_path.rsplit("/", 1)[-1]


def append_latest(source):
    """Append :latest to a source string that has no tag."""
    return source if has_tag(source) else source + ":latest"


def source_to_sync_entry(source):
    """Convert a source image string to a regsync sync entry dict.

    Always produces type:image. If no tag is given, defaults to :latest.
    """
    source = source.strip()
    for src_prefix, ecr_prefix in SOURCE_PREFIXES:
        if source.startswith(src_prefix):
            source = append_latest(source)
            path = source[len(src_prefix):]
            target = f"{TARGET_REGISTRY}/{ecr_prefix}{path}"
            return {"source": source, "target": target, "type": "image"}
    # bare name (e.g. nginx or nginx:1.27) → dockerhub/library/
    if "/" not in source.split(":")[0]:
        source = append_latest(source)
        target = f"{TARGET_REGISTRY}/dockerhub/library/{source}"
        return {"source": f"docker.io/library/{source}", "target": target, "type": "image"}
    return None


def validate_requests(requests_file):
    """Warn about entries in required-images.txt that are missing an explicit tag."""
    warnings = []
    with open(requests_file, "r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            image = line.split()[0]
            if (not has_tag(image) or image.endswith(":latest")) and not any(image.startswith(e) for e in LATEST_EXCEPTIONS):
                warnings.append(f"  line {lineno}: {image!r} — 建议使用明确版本 tag（如 :1.27），:latest 每次都会重新同步")
    if warnings:
        print(f"[WARN] {requests_file} 中有 {len(warnings)} 条建议：")
        for w in warnings:
            print(w)
    else:
        print(f"[OK] {requests_file} 所有条目均有明确 tag")
    return 0


LATEST_EXCEPTIONS = (
    "gcr.io/distroless/",   # distroless 官方只发布 :latest
)


ECR_PUBLIC_LIBRARY_REPLACEABLE = "public.ecr.aws/docker/library/"


def lint_weekly(requests_file):
    """Check required-images-weekly.txt for docker.io/library/* entries.

    docker.io/library/* images are mirrored on public.ecr.aws/docker/library/ with no
    rate limit. Using the ECR Public source keeps all library pulls out of the DockerHub
    200-pull/6h quota. Non-library docker.io images (e.g. docker.io/grafana/*) are not
    mirrored on ECR Public and must stay on DockerHub — those are allowed.
    """
    errors = []
    with open(requests_file, "r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            image = line.split()[0]
            if image.startswith("docker.io/library/") or (
                "/" not in image.split(":")[0] and not image.startswith("docker.io/")
            ):
                bare = image.removeprefix("docker.io/library/")
                suggestion = f"{ECR_PUBLIC_LIBRARY_REPLACEABLE}{bare}"
                errors.append(
                    f"  line {lineno}: {image!r} — 请改用 {suggestion!r}（ECR Public 无 rate limit）"
                )
    if errors:
        print(f"[FAIL] {requests_file} 中有 {len(errors)} 条 docker.io/library 条目，应改为 ECR Public 源：")
        for e in errors:
            print(e)
        return 1
    print(f"[OK] {requests_file} 无 docker.io/library 条目")
    return 0


def merge_requests(requests_file, base_config, output_file):
    """Merge required-images.txt (or a regsync YAML config) into base_config and write combined config."""
    try:
        import yaml
    except ImportError:
        print("PyYAML is required: pip install pyyaml", file=sys.stderr)
        sys.exit(2)

    config = load_yaml(base_config)
    existing_sources = {e["source"] for e in config.get("sync", [])}
    added = []

    # Detect if requests_file is a full regsync YAML config (has a 'sync' key)
    # rather than a plain text image list.
    requests_data = load_yaml(requests_file) if requests_file.endswith((".yml", ".yaml")) else None
    if requests_data and isinstance(requests_data, dict) and "sync" in requests_data:
        for entry in requests_data.get("sync", []):
            src = entry.get("source")
            if src and src not in existing_sources:
                config.setdefault("sync", []).append(entry)
                existing_sources.add(src)
                added.append(src)
    else:
        with open(requests_file, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                entry = source_to_sync_entry(line)
                if entry is None:
                    print(f"[WARN] cannot map source: {line}", file=sys.stderr)
                    continue
                if entry["source"] not in existing_sources:
                    config.setdefault("sync", []).append(entry)
                    existing_sources.add(entry["source"])
                    added.append(entry["source"])

    with open(output_file, "w", encoding="utf-8") as fh:
        yaml.dump(config, fh, allow_unicode=True, default_flow_style=False, sort_keys=False)

    print(f"[OK] merged {len(added)} new entries from {requests_file} → {output_file}")
    if added:
        for src in added:
            print(f"  + {src}")
    return 0


TAG_DENY_PATTERN = re.compile(
    r"alpha|beta|windowsservercore|nanoserver|perl|otel|slim|rc\d|\.sig$"
)

# 只保留语义版本 tag：1.27 / 1.27.0 / 1.27-alpine / v1.4.2 等
TAG_ALLOW_PATTERN = re.compile(
    r"^v?\d+\.\d+(\.\d+)?(-alpine[\d.]*)?$"
)


def _http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "container-mirror/2.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _fetch_dockerhub_tags(repo, page_size=100):
    """Return list of (tag, last_updated, digest) from Docker Hub for library/{repo} or {repo}."""
    if "/" not in repo:
        api_repo = f"library/{repo}"
    else:
        api_repo = repo.removeprefix("docker.io/")
    url = f"https://registry.hub.docker.com/v2/repositories/{api_repo}/tags?page_size={page_size}&ordering=last_updated"
    try:
        data = _http_get(url)
        return [
            (t["name"], t.get("last_updated", ""), t.get("digest") or "")
            for t in data.get("results", [])
        ]
    except Exception as e:
        print(f"  [WARN] Docker Hub API error for {repo}: {e}", file=sys.stderr)
        return []


def _fetch_quay_tags(image, limit=100, max_pages=3):
    """Return tag list via quay.io v1 API sorted newest-first by last_modified."""
    tags = []
    page = 1
    for _ in range(max_pages):
        url = (
            f"https://quay.io/api/v1/repository/{image}/tag/"
            f"?onlyActiveTags=true&limit={limit}&page={page}"
            f"&sort_by=last_modified&sort_asc=false"
        )
        try:
            data = _http_get(url)
            batch = data.get("tags") or []
            tags.extend((t["name"], t.get("last_modified", "")) for t in batch)
            if not data.get("has_additional"):
                break
            page += 1
        except Exception as e:
            print(f"  [WARN] quay.io API error for {image}: {e}", file=sys.stderr)
            break
    return tags


def _fetch_oci_tags(registry, image, max_pages=5):
    """Return tag list via OCI Distribution API with pagination."""
    tags = []
    url = f"https://{registry}/v2/{image}/tags/list?n=200"
    for _ in range(max_pages):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "container-mirror/2.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                tags.extend((t, "") for t in data.get("tags") or [])
                # follow Link header for next page
                link = resp.headers.get("Link", "")
                m = re.search(r'<([^>]+)>;\s*rel="next"', link)
                if not m:
                    break
                next_path = m.group(1)
                url = f"https://{registry}{next_path}" if next_path.startswith("/") else next_path
        except Exception as e:
            print(f"  [WARN] OCI API error for {registry}/{image}: {e}", file=sys.stderr)
            break
    return tags


def _version_tuple(tag):
    """Extract (major, minor, patch) numeric tuple from a tag string."""
    m = re.match(r"v?(\d+)\.(\d+)(?:\.(\d+))?", tag)
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def _minor_key(tag):
    t = _version_tuple(tag)
    return (t[0], t[1])


def _pick_best_tags(all_tags, max_minor=2):
    """
    Filter to stable semantic versions, keep only the latest patch per minor version,
    return at most max_minor distinct minors (newest first) plus their alpine variants.
    """
    plain_map = {}   # minor_key → (best_tag, version_tuple)
    alpine_map = {}  # minor_key → (best_tag, version_tuple)

    for tag, *_ in all_tags:
        if TAG_DENY_PATTERN.search(tag):
            continue
        if not TAG_ALLOW_PATTERN.match(tag):
            continue
        vt = _version_tuple(tag)
        mk = (vt[0], vt[1])
        if "alpine" in tag:
            if mk not in alpine_map or vt > alpine_map[mk][1]:
                alpine_map[mk] = (tag, vt)
        else:
            if mk not in plain_map or vt > plain_map[mk][1]:
                plain_map[mk] = (tag, vt)

    # pick newest max_minor minor versions
    top_minors = sorted(plain_map.keys(), reverse=True)[:max_minor]

    result = []
    for mk in top_minors:
        result.append(plain_map[mk][0])
        if mk in alpine_map:
            result.append(alpine_map[mk][0])
    return result


def _ecr_get_digest(ecr_registry, ecr_repo, tag):
    """Return the imageDigest already in ECR, or None if not present / unavailable."""
    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        return None
    m = re.search(r"ecr\.([\w-]+)\.amazonaws", ecr_registry)
    if not m:
        return None
    region = m.group(1)
    ak, sk = os.getenv("CHINA_AK"), os.getenv("CHINA_SK")
    kwargs = {"region_name": region}
    if ak and sk:
        kwargs["aws_access_key_id"] = ak
        kwargs["aws_secret_access_key"] = sk
    try:
        client = boto3.client("ecr", **kwargs)
        resp = client.describe_images(
            repositoryName=ecr_repo,
            imageIds=[{"imageTag": tag}],
        )
        details = resp.get("imageDetails", [])
        return details[0].get("imageDigest") if details else None
    except ClientError as e:
        if e.response["Error"]["Code"] in ("ImageNotFoundException", "RepositoryNotFoundException"):
            return None
        print(f"  [WARN] ECR describe-images {ecr_repo}:{tag}: {e}", file=sys.stderr)
    except Exception as e:
        print(f"  [WARN] ECR check error {ecr_repo}:{tag}: {e}", file=sys.stderr)
    return None


def _resolve_tags(source):
    """Resolve latest stable tags for a given image source."""
    source = source.strip()
    if source.startswith("docker.io/") or "/" not in source:
        bare = source.removeprefix("docker.io/").removeprefix("library/")
        all_tags = _fetch_dockerhub_tags(bare)
    elif source.startswith("registry.k8s.io/"):
        image = source.removeprefix("registry.k8s.io/")
        all_tags = _fetch_oci_tags("registry.k8s.io", image)
    elif source.startswith("quay.io/"):
        image = source.removeprefix("quay.io/")
        all_tags = _fetch_quay_tags(image)
    elif source.startswith("ghcr.io/"):
        image = source.removeprefix("ghcr.io/")
        all_tags = _fetch_oci_tags("ghcr.io", image)
    elif source.startswith("gcr.io/"):
        image = source.removeprefix("gcr.io/")
        all_tags = _fetch_oci_tags("gcr.io", image)
    elif source.startswith("public.ecr.aws/"):
        image = source.removeprefix("public.ecr.aws/")
        all_tags = _fetch_oci_tags("public.ecr.aws", image)
    else:
        print(f"  [WARN] unsupported registry for weekly config: {source}", file=sys.stderr)
        return []
    return _pick_best_tags(all_tags)


def _resolve_tags_with_digest(source):
    """Like _resolve_tags but returns [(tag, hub_digest)] pairs.

    hub_digest comes from the hub.docker.com API (not rate-limited) for docker.io
    sources; None for all other registries.
    """
    source = source.strip()
    if source.startswith("docker.io/") or "/" not in source:
        bare = source.removeprefix("docker.io/").removeprefix("library/")
        raw = _fetch_dockerhub_tags(bare)
        digest_map = {t[0]: t[2] for t in raw}
        return [(tag, digest_map.get(tag, "")) for tag in _pick_best_tags(raw)]
    return [(tag, None) for tag in _resolve_tags(source)]


def build_weekly_config(requests_file, output_file):
    """Read required-images-weekly.txt, resolve latest tags, write regsync config."""
    try:
        import yaml
    except ImportError:
        print("PyYAML is required: pip install pyyaml", file=sys.stderr)
        sys.exit(2)

    base = {
        "version": 1,
        "creds": [
            {"registry": "docker.io",
             "user": '{{env "DOCKERHUB_USER"}}',
             "pass": '{{env "DOCKERHUB_TOKEN"}}'},
            {"registry": TARGET_REGISTRY,
             "user": "AWS",
             "pass": '{{env "ECR_PASSWORD"}}'},
        ],
        "defaults": {"parallel": 8, "ratelimit": {"min": 10, "retry": "5m"}},
        "sync": [],
    }

    with open(requests_file, "r", encoding="utf-8") as fh:
        images = [l.strip() for l in fh if l.strip() and not l.startswith("#")]

    total = 0
    skipped = 0
    for image in images:
        print(f"[INFO] resolving tags for {image} ...")
        tag_pairs = _resolve_tags_with_digest(image)
        if not tag_pairs:
            print(f"  [WARN] no stable tags found for {image}", file=sys.stderr)
            continue
        for tag, hub_digest in tag_pairs:
            entry = source_to_sync_entry(f"{image}:{tag}")
            if not entry:
                continue
            if hub_digest:
                # derive ECR repo and tag from target URI
                without_reg = entry["target"][len(TARGET_REGISTRY) + 1:]
                ecr_repo, ecr_tag = without_reg.rsplit(":", 1) if ":" in without_reg else (without_reg, tag)
                ecr_digest = _ecr_get_digest(TARGET_REGISTRY, ecr_repo, ecr_tag)
                if ecr_digest and ecr_digest == hub_digest:
                    print(f"  [SKIP] {entry['source']} digest unchanged ({hub_digest[:19]}…)")
                    skipped += 1
                    continue
            base["sync"].append(entry)
            total += 1
            print(f"  + {entry['source']}")

    with open(output_file, "w", encoding="utf-8") as fh:
        yaml.dump(base, fh, allow_unicode=True, default_flow_style=False, sort_keys=False)

    print(f"[OK] {output_file} 已生成，共 {total} 个条目（跳过 {skipped} 个 digest 未变更）")
    return 0


REGISTRY_LABELS = {
    "docker.io":          "Docker Hub",
    "registry.k8s.io":    "registry.k8s.io",
    "public.ecr.aws":     "public.ecr.aws",
    "quay.io":            "Quay",
    "ghcr.io":            "GHCR",
    "gcr.io":             "GCR",
    "docker.elastic.co":  "Elastic",
}

BATCH_KEY_MAP = [
    ("docker.io/", "dockerhub"),
    ("registry.k8s.io/", "registryk8sio"),
    ("gcr.io/", "gcr"),
    ("quay.io/", "quay"),
    ("ghcr.io/", "ghcr"),
    ("public.ecr.aws/", "ecrpublic"),
    ("docker.elastic.co/", "elastic"),
    ("602401143452.", "amazonecr"),
]


def split_config(merged_config, output_dir):
    """Split merged regsync config into per-registry sub-configs.

    Writes one YAML file per registry under output_dir and prints
    MATRIX=<json> on stdout for GHA to capture.
    """
    try:
        import yaml
    except ImportError:
        print("PyYAML is required: pip install pyyaml", file=sys.stderr)
        sys.exit(2)

    data = load_yaml(merged_config)
    base = {k: v for k, v in data.items() if k != "sync"}

    batches = {}
    for entry in data.get("sync", []):
        source = entry.get("source", "")
        batch = "other"
        for prefix, key in BATCH_KEY_MAP:
            if source.startswith(prefix):
                batch = key
                break
        else:
            if "." not in source.split("/")[0]:
                batch = "dockerhub"
        batches.setdefault(batch, []).append(entry)

    os.makedirs(output_dir, exist_ok=True)
    created = []
    for batch, entries in sorted(batches.items()):
        cfg = dict(base)
        # DockerHub rate limit (200 pulls/6h): serial pulls exhaust quota slower
        if batch == "dockerhub":
            cfg["defaults"] = dict(cfg.get("defaults") or {})
            cfg["defaults"]["parallel"] = 2
        # GHCR requires authentication even for public images
        if batch == "ghcr":
            creds = list(cfg.get("creds") or [])
            creds.append({
                "registry": "ghcr.io",
                "user": '{{env "GHCR_USER"}}',
                "pass": '{{env "GHCR_TOKEN"}}',
            })
            cfg["creds"] = creds
        cfg["sync"] = entries
        out_path = os.path.join(output_dir, f"{batch}.yml")
        with open(out_path, "w", encoding="utf-8") as fh:
            yaml.dump(cfg, fh, allow_unicode=True, default_flow_style=False, sort_keys=False)
        print(f"[OK] {out_path}: {len(entries)} entries")
        created.append(batch)

    matrix = json.dumps({"include": [{"registry": r} for r in created]})
    print(f"MATRIX={matrix}")
    return 0


def generate_sync_list(config_file, output_file):
    """Generate a human-readable sync-list.txt from regsync.yml."""
    data = load_yaml(config_file)
    entries = [e["source"] for e in data.get("sync", [])]

    groups = {}
    for src in entries:
        registry = src.split("/")[0] if "." in src.split("/")[0] else "docker.io"
        label = REGISTRY_LABELS.get(registry, registry)
        groups.setdefault(label, []).append(src)

    lines = [
        "# 每周定时同步镜像清单",
        "# 由 scripts/tools.py list 自动生成，请勿手动编辑",
        f"# 最后更新：{datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        f"# 共 {len(entries)} 个镜像",
        "",
    ]
    for label, sources in groups.items():
        lines.append(f"## {label}")
        lines.extend(sources)
        lines.append("")

    with open(output_file, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    print(f"[OK] {output_file} 已生成，共 {len(entries)} 个镜像")
    return 0


def main(argv):
    command = argv[1] if len(argv) > 1 else "validate"
    if command == "validate":
        return validate_config(Path(argv[2] if len(argv) > 2 else "regsync.yml"))
    if command == "catalog":
        return generate_catalog()
    if command == "merge":
        requests_file = argv[2] if len(argv) > 2 else "required-images.txt"
        base_config = argv[3] if len(argv) > 3 else "regsync.yml"
        output_file = argv[4] if len(argv) > 4 else "/tmp/regsync-merged.yml"
        return merge_requests(requests_file, base_config, output_file)
    if command == "check-requests":
        requests_file = argv[2] if len(argv) > 2 else "required-images.txt"
        return validate_requests(requests_file)
    if command == "lint-weekly":
        requests_file = argv[2] if len(argv) > 2 else "required-images-weekly.txt"
        return lint_weekly(requests_file)
    if command == "build-weekly-config":
        requests_file = argv[2] if len(argv) > 2 else "required-images-weekly.txt"
        output_file = argv[3] if len(argv) > 3 else "/tmp/regsync-weekly.yml"
        return build_weekly_config(requests_file, output_file)
    if command == "list":
        config_file = argv[2] if len(argv) > 2 else "regsync.yml"
        output_file = argv[3] if len(argv) > 3 else "sync-list.txt"
        return generate_sync_list(config_file, output_file)
    if command == "split":
        merged_config = argv[2] if len(argv) > 2 else "/tmp/regsync-merged.yml"
        output_dir = argv[3] if len(argv) > 3 else "/tmp/split"
        return split_config(merged_config, output_dir)
    print("Usage: tools.py validate [regsync.yml] | list [regsync.yml] [output] | split [merged.yml] [output-dir] | merge [...] | check-requests [...] | lint-weekly [...] | build-weekly-config [...] | catalog", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
