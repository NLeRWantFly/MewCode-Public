"""Content-addressed image assets for multimodal pipelines."""

from __future__ import annotations

import base64
import hashlib
import re
import uuid
from pathlib import Path
from typing import Any

from mewcode.pipeline.models import AssetRef

ASSETS_DIR = ".mewcode/pipelines/assets"
MAX_ASSET_BYTES = 10 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EXTENSIONS = {
    "image/png": ".png", "image/jpeg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp",
}


class AssetError(ValueError):
    pass


class AssetStore:
    def __init__(self, work_dir: str | Path) -> None:
        self.root = Path(work_dir) / ASSETS_DIR

    @staticmethod
    def _digest(asset: str) -> str:
        digest = asset.removeprefix("sha256:").lower()
        if not _SHA256_RE.fullmatch(digest):
            raise AssetError("invalid sha256 asset reference")
        return digest

    def put(self, data: bytes, mime: str, name: str = "") -> AssetRef:
        mime = mime.partition(";")[0].strip().lower()
        extension = _EXTENSIONS.get(mime)
        if extension is None:
            raise AssetError(f"unsupported image type: {mime or 'missing'}")
        if not data:
            raise AssetError("image is empty")
        if len(data) > MAX_ASSET_BYTES:
            raise AssetError("image exceeds 10 MiB limit")
        digest = hashlib.sha256(data).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{digest}{extension}"
        if not path.exists():
            temporary = self.root / f".{digest}.{uuid.uuid4().hex}.tmp"
            temporary.write_bytes(data)
            try:
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return AssetRef(asset=f"sha256:{digest}", mime=mime, name=Path(name).name)

    def path_for(self, asset: str | AssetRef | dict[str, Any]) -> Path:
        if isinstance(asset, AssetRef):
            value = asset.asset
        elif isinstance(asset, dict):
            value = str(asset.get("asset", ""))
        else:
            value = str(asset)
        digest = self._digest(value)
        matches = list(self.root.glob(f"{digest}.*")) if self.root.exists() else []
        if len(matches) != 1:
            raise AssetError("asset not found")
        path = matches[0].resolve()
        if self.root.resolve() not in path.parents:
            raise AssetError("asset path escapes asset store")
        return path

    def content_block(self, asset: AssetRef | dict[str, Any]) -> dict[str, Any]:
        ref = asset if isinstance(asset, AssetRef) else AssetRef.model_validate(asset)
        path = self.path_for(ref)
        return {
            "type": "image", "mime_type": ref.mime,
            "data": base64.b64encode(path.read_bytes()).decode("ascii"),
            "name": ref.name or path.name, "asset": ref.asset,
        }

    def by_digest(self, digest: str) -> Path:
        return self.path_for(f"sha256:{digest}")
