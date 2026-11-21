#!/usr/bin/env python3
"""Query authorized GPT Image models and generate or edit images."""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "gpt-image-2.6-flare"
EXPECTED_BASE_URL = "https://noxgate.net/v1"
CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
CONFIG_PATH = CODEX_HOME / "config.toml"
VERSION_PATTERN = re.compile(r"gpt-image-(?P<version>\d+(?:\.\d+)*)(?:-[a-z0-9-]+)?$", re.IGNORECASE)


class ImageRequestError(Exception):
    """An expected configuration, model, request, or response error."""


def normalize_base_url(value: str) -> str:
    return value.rstrip("/")


def load_provider(path: Path = CONFIG_PATH) -> tuple[str, str]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        provider_name = data["model_provider"]
        provider = data["model_providers"][provider_name]
        base_url = normalize_base_url(provider["base_url"])
        token = provider.get("experimental_bearer_token")
        if not token and provider.get("env_key"):
            token = os.environ.get(provider["env_key"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ImageRequestError("无法读取当前 Codex provider 配置") from exc
    parsed = urllib.parse.urlsplit(base_url)
    if (base_url != EXPECTED_BASE_URL or parsed.scheme != "https" or parsed.hostname != "noxgate.net"
            or parsed.query or parsed.fragment):
        raise ImageRequestError(f"当前 Codex provider 的 Base URL 必须是 {EXPECTED_BASE_URL}")
    if not isinstance(token, str) or not token.strip():
        raise ImageRequestError("当前 Codex provider 没有可用的 API Key")
    return base_url, token.strip()


def request_bytes(url: str, token: str, *, data: bytes | None = None,
                  content_type: str = "application/json", timeout: int = 300) -> bytes:
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read(2000).decode("utf-8", errors="replace").replace(token, "[已隐藏]")
        raise ImageRequestError(f"HTTP {exc.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ImageRequestError(f"请求失败：{exc}") from None


def request_json(url: str, token: str, *, data: bytes | None = None,
                content_type: str = "application/json") -> dict[str, Any]:
    raw = request_bytes(url, token, data=data, content_type=content_type)
    try:
        result = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImageRequestError("网关返回的不是有效 JSON") from exc
    if not isinstance(result, dict):
        raise ImageRequestError("网关返回了非预期的 JSON 结构")
    return result


def number(value: object) -> Decimal | None:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def price_lines(config: object) -> list[dict[str, str]]:
    if not isinstance(config, dict) or not isinstance(config.get("rules"), list):
        return []
    lines = []
    for rule in config["rules"]:
        if not isinstance(rule, dict) or not isinstance(rule.get("meter"), str):
            continue
        price = number(rule.get("price"))
        if price is None:
            continue
        meter = rule["meter"].strip().lower()
        unit = {"image_count": "张", "request": "次请求"}.get(meter, meter)
        lines.append({"amount": format((price / 1_000_000).normalize(), "f"), "unit": unit, "meter": meter})
    return lines


def model_version(model_id: str) -> tuple[int, ...]:
    match = VERSION_PATTERN.fullmatch(model_id)
    return tuple(int(part) for part in match["version"].split(".")) if match else ()


def authorized_models(base_url: str, token: str, operation: str) -> tuple[list[dict[str, Any]], bool]:
    response = request_json(f"{base_url}/models", token)
    entries = response.get("data")
    if not isinstance(entries, list):
        raise ImageRequestError("网关未返回有效的模型权限列表")
    authorized = {
        item["id"]: item for item in entries
        if isinstance(item, dict) and isinstance(item.get("id"), str)
        and item["id"].lower().startswith("gpt-image-")
    }
    catalog_base = base_url.removesuffix("/v1") + "/api/public"
    try:
        public = request_json(f"{catalog_base}/models", token).get("data")
    except ImageRequestError:
        public = None
    catalog = {
        item["model_id"]: item for item in public
        if isinstance(item, dict) and isinstance(item.get("model_id"), str)
    } if isinstance(public, list) else {}
    models = []
    for model_id, entry in sorted(authorized.items()):
        metadata = catalog.get(model_id, {})
        capability = entry.get("capability_type") or metadata.get("capability_type")
        if capability and capability != "image_generation":
            continue
        protocols = metadata.get("supported_client_protocols")
        supports_openai_images = isinstance(protocols, list) and "openai_images" in protocols
        if operation == "edit" and (capability != "image_generation" or not supports_openai_images):
            continue
        rates = price_lines(metadata.get("price_config"))
        models.append({
            "id": model_id,
            "name": metadata.get("name") or entry.get("name") or model_id,
            "capability": capability,
            "operation_support": "confirmed" if capability == "image_generation" and (
                operation != "edit" or supports_openai_images
            ) else "unverified",
            "rates": rates,
            "pricing_status": "available" if rates else "unavailable",
        })
    return models, isinstance(public, list)


def listing(operation: str, count: int = 1) -> dict[str, Any]:
    if operation not in {"generate", "edit"}:
        raise ImageRequestError("operation 必须是 generate 或 edit")
    if count < 1:
        raise ImageRequestError("数量必须大于零")
    base_url, token = load_provider()
    models, catalog_available = authorized_models(base_url, token, operation)
    currency = None
    catalog_base = base_url.removesuffix("/v1") + "/api/public"
    try:
        config = request_json(f"{catalog_base}/platform-config", token).get("data")
        value = config.get("payment_currency") if isinstance(config, dict) else None
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z]{3}", value):
            currency = value.upper()
    except ImageRequestError:
        pass
    for model in models:
        if currency and model["operation_support"] == "confirmed" and len(model["rates"]) == 1:
            rate = model["rates"][0]
            if rate["meter"] in {"image_count", "request"}:
                model["estimated_base_cost"] = format(Decimal(rate["amount"]) * count, "f")
    latest_version = max((model_version(model["id"]) for model in models), default=())
    latest = [model["id"] for model in models if model_version(model["id"]) == latest_version]
    return {
        "operation": operation,
        "currency": currency,
        "catalog_available": catalog_available,
        "default_model": DEFAULT_MODEL,
        "latest_model_ids": latest,
        "models": models,
    }


def show_models(operation: str, count: int) -> int:
    print(json.dumps(listing(operation, count), ensure_ascii=False, indent=2))
    return 0


def multipart(fields: dict[str, str], images: list[Path], mask: Path | None) -> tuple[bytes, str]:
    boundary = f"codex-{uuid.uuid4().hex}"
    marker = f"--{boundary}".encode()
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend((marker, f'Content-Disposition: form-data; name="{name}"'.encode(), b"", value.encode()))
    for path in images:
        if not path.is_file():
            raise ImageRequestError(f"图片文件不存在：{path}")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        chunks.extend((marker, f'Content-Disposition: form-data; name="image[]"; filename="{path.name}"'.encode(),
                       f"Content-Type: {mime}".encode(), b"", path.read_bytes()))
    if mask is not None:
        if not mask.is_file():
            raise ImageRequestError(f"遮罩文件不存在：{mask}")
        mime = mimetypes.guess_type(mask.name)[0] or "application/octet-stream"
        chunks.extend((marker, f'Content-Disposition: form-data; name="mask"; filename="{mask.name}"'.encode(),
                       f"Content-Type: {mime}".encode(), b"", mask.read_bytes()))
    chunks.extend((f"--{boundary}--".encode(), b""))
    return b"\r\n".join(chunks), f"multipart/form-data; boundary={boundary}"


def image_items(result: dict[str, Any]) -> list[dict[str, Any]]:
    data = result.get("data")
    if isinstance(data, list) and data and all(isinstance(item, dict) for item in data):
        return data
    if result.get("b64_json") or result.get("url"):
        return [result]
    raise ImageRequestError("网关响应中没有图片数据")


def url_origin(url: str) -> tuple[str, str, int | None]:
    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port


class ImageRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, trusted_origin: tuple[str, str, int | None]):
        super().__init__()
        self.trusted_origin = trusted_origin

    def redirect_request(self, request: urllib.request.Request, file_pointer: Any, code: int,
                         message: str, headers: Any, new_url: str) -> urllib.request.Request | None:
        if urllib.parse.urlsplit(new_url).scheme.lower() != "https":
            return None
        redirected = super().redirect_request(request, file_pointer, code, message, headers, new_url)
        if redirected is not None and url_origin(new_url) != self.trusted_origin:
            redirected.remove_header("Authorization")
        return redirected


def image_bytes(item: dict[str, Any], base_url: str, token: str) -> bytes:
    encoded = item.get("b64_json")
    if isinstance(encoded, str):
        try:
            return base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ImageRequestError("网关返回了无效的图片 base64 数据") from exc
    url = item.get("url") or item.get("image_url")
    if not isinstance(url, str):
        raise ImageRequestError("图片响应缺少 b64_json 或 url")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ImageRequestError("图片下载地址必须是有效的 HTTPS URL")
    same_origin = url_origin(url) == url_origin(base_url)
    headers = {"Authorization": f"Bearer {token}"} if same_origin else {}
    try:
        opener = urllib.request.build_opener(ImageRedirectHandler(url_origin(base_url)))
        with opener.open(urllib.request.Request(url, headers=headers), timeout=300) as response:
            return response.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        raise ImageRequestError(f"图片下载失败：{exc}") from None


def validate_image(content: bytes, image_format: str) -> None:
    valid = {
        "png": content.startswith(b"\x89PNG\r\n\x1a\n"),
        "jpeg": content.startswith(b"\xff\xd8\xff"),
        "webp": content.startswith(b"RIFF") and content[8:12] == b"WEBP",
    }
    if not valid[image_format]:
        raise ImageRequestError(f"网关响应不是有效的 {image_format.upper()} 图片")


def save_result(result: dict[str, Any], base_url: str, token: str, output: Path, image_format: str) -> list[Path]:
    extension = {"png": ".png", "jpeg": ".jpg", "webp": ".webp"}[image_format]
    items = image_items(result)
    paths = []
    for index, item in enumerate(items, 1):
        path = output if len(items) == 1 else output.with_name(f"{output.stem}_{index:02}{extension}")
        content = image_bytes(item, base_url, token)
        validate_image(content, image_format)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        paths.append(path.resolve())
    return paths


def wait_for_result(base_url: str, token: str, task_id: str, timeout: int = 900) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = request_json(f"{base_url}/images/generations/{urllib.parse.quote(task_id, safe='')}", token)
        if result.get("status") in {"succeeded", "completed"}:
            return result
        if result.get("status") in {"failed", "cancelled", "canceled"}:
            raise ImageRequestError(f"图片任务结束状态为 {result['status']}")
        time.sleep(3)
    raise ImageRequestError("等待图片任务超时")


def submit(args: argparse.Namespace) -> int:
    if args.command == "models":
        return show_models(args.operation, args.count)
    base_url, token = load_provider()
    candidates, _ = authorized_models(base_url, token, args.command)
    candidate_ids = {model["id"] for model in candidates}
    if args.use_default:
        if args.model:
            raise ImageRequestError("--model 和 --use-default 不能同时使用")
        model = DEFAULT_MODEL
    else:
        model = args.model
    if not model:
        print(json.dumps(listing(args.command, args.count), ensure_ascii=False, indent=2))
        print("请先选择模型，再使用 --model 提交生成请求", file=sys.stderr)
        return 2
    if model not in candidate_ids:
        raise ImageRequestError(f"模型 {model} 未获当前 API Key 授权，或不支持本次操作")
    if not args.prompt.strip():
        raise ImageRequestError("提示词不能为空")
    fields = {"model": model, "prompt": args.prompt, "size": args.size, "quality": args.quality,
              "output_format": args.output_format}
    if args.command == "generate":
        fields.update(background=args.background, n=args.count, response_format="b64_json", stream=False)
        body = json.dumps(fields, ensure_ascii=False).encode()
        content_type, endpoint = "application/json", "images/generations"
    else:
        fields.update(n=str(args.count), response_format="b64_json", stream="false")
        body, content_type = multipart(fields, args.image, args.mask)
        endpoint = "images/edits"
    result = request_json(f"{base_url}/{endpoint}", token, data=body, content_type=content_type)
    if result.get("status") in {"failed", "cancelled", "canceled"}:
        raise ImageRequestError(f"图片任务结束状态为 {result['status']}")
    has_image = isinstance(result.get("data"), list) or result.get("b64_json") or result.get("url")
    if not has_image:
        task_id = result.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise ImageRequestError("异步响应缺少任务 ID")
        result = wait_for_result(base_url, token, task_id)
    for path in save_result(result, base_url, token, args.output.expanduser(), args.output_format):
        print(path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    models = subparsers.add_parser("models")
    models.add_argument("--operation", choices=("generate", "edit"), default="generate")
    models.add_argument("--count", type=int, default=1)
    for name in ("generate", "edit"):
        command = subparsers.add_parser(name)
        command.add_argument("--prompt", required=True)
        command.add_argument("--output", type=Path, required=True)
        command.add_argument("--model", help="已选择的 GPT Image 模型 ID")
        command.add_argument("--use-default", action="store_true", help="用户明确选择默认模型时使用")
        command.add_argument("--size", default="1024x1024")
        command.add_argument("--quality", choices=("low", "medium", "high", "auto"), default="medium")
        command.add_argument("--output-format", choices=("png", "jpeg", "webp"), default="png")
        command.add_argument("--count", type=int, choices=range(1, 5), default=1)
        if name == "generate":
            command.add_argument("--background", choices=("auto", "transparent", "opaque"), default="auto")
        else:
            command.add_argument("--image", type=Path, action="append", required=True)
            command.add_argument("--mask", type=Path)
    return parser


def main() -> int:
    try:
        return submit(build_parser().parse_args())
    except (ImageRequestError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
