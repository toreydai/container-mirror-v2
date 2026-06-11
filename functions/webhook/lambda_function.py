import base64
import json
import os


ECR = os.getenv("ECR_REGISTRY", "048912060910.dkr.ecr.cn-northwest-1.amazonaws.com.cn")
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "")
PREFIXES = {
    "direct.to/": "",
    "k8s.gcr.io/": "gcr/google_containers/",
    "registry.k8s.io/": "registryk8sio/",
    "gcr.io/google-containers/": "gcr/google_containers/",
    "gcr.io/": "gcr/",
    "asia.gcr.io/": "gcr/",
    "eu.gcr.io/": "gcr/",
    "us.gcr.io/": "gcr/",
    "quay.io/": "quay/",
    "public.ecr.aws/": "ecrpublic/",
    "ghcr.io/": "ghcr/",
    "docker.elastic.co/": "elastic/",
    "602401143452.dkr.ecr.us-west-2.amazonaws.com/": "amazonecr/",
    "docker.io/": "dockerhub/",
}


def is_authorized(event):
    if not WEBHOOK_TOKEN:
        return True
    headers = event.get("headers") or {}
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    query = event.get("queryStringParameters") or {}
    return auth == f"Bearer {WEBHOOK_TOKEN}" or query.get("token") == WEBHOOK_TOKEN


def rewrite_image(image):
    for source, target in PREFIXES.items():
        if image.startswith(source):
            return (f"{ECR}/{target}" if target else "") + image[len(source) :]
    if "/" not in image:
        return f"{ECR}/dockerhub/library/{image}"
    first = image.split("/")[0]
    return f"{ECR}/dockerhub/{image}" if "." not in first and ":" not in first else image


def patches_for(containers, path):
    return [
        {"op": "replace", "path": f"{path}/{idx}/image", "value": rewritten}
        for idx, container in enumerate(containers or [])
        if (image := container.get("image")) and (rewritten := rewrite_image(image)) != image
    ]


def handler(event, context):
    if not is_authorized(event):
        return {"statusCode": 401, "headers": {"Content-Type": "application/json"}, "body": json.dumps({"error": "unauthorized"})}

    body = json.loads(event.get("body") or "{}")
    request = body.get("request", {})
    spec = request.get("object", {}).get("spec", {})
    patches = patches_for(spec.get("initContainers"), "/spec/initContainers")
    patches += patches_for(spec.get("containers"), "/spec/containers")

    response = {
        "apiVersion": body.get("apiVersion", "admission.k8s.io/v1"),
        "kind": "AdmissionReview",
        "response": {
            "uid": request.get("uid"),
            "allowed": True,
            "patchType": "JSONPatch",
            "patch": base64.b64encode(json.dumps(patches).encode("utf-8")).decode("utf-8"),
        },
    }
    return {"statusCode": 200, "headers": {"Content-Type": "application/json"}, "body": json.dumps(response)}
