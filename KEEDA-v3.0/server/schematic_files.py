"""Server-side store for the team's shared schematic files.

eeschema has no live API, so schematic edits cannot be pushed into another
user's open editor. The workaround is file distribution: the author's saved
`.kicad_sch` is uploaded here, and every other agent writes it into its own
project folder on a timer. The user then reloads the sheet in eeschema.

The store is a plain directory (`<data>/schematics/<project>/`) plus a JSON
manifest, so it survives a server restart and can be inspected by hand.

Concurrency rule: a push carries the sha256 of the version the author started
from (`base_sha256`). If someone else pushed in between, the push is REJECTED
rather than silently overwriting their work.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import threading
import time

MAX_FILE_BYTES = 10 * 1024 * 1024
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()+-]{0,120}\.kicad_(?:sch|pcb|pro)$")
# Project support files: no extension, fixed names.
SUPPORT_NAMES = ("sym-lib-table", "fp-lib-table")
OVERWRITE = "*"          # base_sha256 meaning "last writer wins" (PCB publishing only)


class SchematicFileError(ValueError):
    """A rejected push, with a machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def valid_name(name: object) -> bool:
    """Bare file names only: no directories, no traversal."""
    if not isinstance(name, str) or ".." in name:
        return False
    return name in SUPPORT_NAMES or bool(_NAME_RE.match(name))


def file_kind(name: str) -> str:
    """'sch' | 'pcb' | 'pro' | 'table'. What the file is decides how it is shared:
    schematics are merged, the PCB is published (the live API is its truth),
    project/support files are handed to newcomers and never overwritten."""
    if name.endswith(".kicad_sch"):
        return "sch"
    if name.endswith(".kicad_pcb"):
        return "pcb"
    if name.endswith(".kicad_pro"):
        return "pro"
    return "table"


def looks_complete(name: str, data: bytes) -> bool:
    """A save caught half-way is not a file. .kicad_pro is JSON; the rest are
    S-expressions ending in a closing parenthesis."""
    tail = data.rstrip()[-1:]
    return tail == (b"}" if file_kind(name) == "pro" else b")")


class SchematicFileStore:
    def __init__(self, data_dir: str):
        self.root = os.path.join(data_dir, "schematics")
        self._lock = threading.Lock()

    def _dir(self, project_id: str) -> str:
        safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in project_id)[:64]
        return os.path.join(self.root, safe or "default")

    def _manifest_path(self, project_id: str) -> str:
        return os.path.join(self._dir(project_id), "manifest.json")

    def manifest(self, project_id: str) -> dict[str, dict]:
        """name -> {sha256, size, author, ts, rev}."""
        try:
            with open(self._manifest_path(project_id), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_manifest(self, project_id: str, manifest: dict) -> None:
        path = self._manifest_path(project_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=1)
        os.replace(tmp, path)

    def put(self, project_id: str, name: str, content_b64: str, sha256: str,
            base_sha256: str | None, author: str) -> dict:
        """Store one pushed sheet. Raises SchematicFileError on any problem."""
        if not valid_name(name):
            raise SchematicFileError("bad_name", f"not a valid schematic file name: {name!r}")
        try:
            data = base64.b64decode(content_b64 or "", validate=True)
        except (binascii.Error, ValueError):
            raise SchematicFileError("bad_content", f"{name}: content is not valid base64")
        if not data or len(data) > MAX_FILE_BYTES:
            raise SchematicFileError("bad_size", f"{name}: empty or larger than "
                                     f"{MAX_FILE_BYTES // (1024 * 1024)} MB")
        if sha256_of(data) != sha256:
            raise SchematicFileError("bad_hash", f"{name}: checksum mismatch (corrupted upload)")
        if not looks_complete(name, data):
            raise SchematicFileError("truncated", f"{name}: file looks incomplete "
                                     "(caught mid-save?)")

        with self._lock:
            os.makedirs(self._dir(project_id), exist_ok=True)
            manifest = self.manifest(project_id)
            current = manifest.get(name)
            if current is not None and current["sha256"] == sha256:
                return current                      # identical: nothing to do
            # Publishing a PCB file is last-writer-wins: the live API is the truth
            # for the board, the file is only a starting point for newcomers.
            overwrite = base_sha256 == OVERWRITE and file_kind(name) == "pcb"
            if current is not None and not overwrite and base_sha256 != current["sha256"]:
                raise SchematicFileError(
                    "stale_base", f"{name} was updated by {current.get('author', 'someone')} "
                                  "after you last synced it")
            path = os.path.join(self._dir(project_id), name)
            tmp = path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
            entry = {"sha256": sha256, "size": len(data), "author": author,
                     "ts": time.time(), "rev": (current or {}).get("rev", 0) + 1,
                     "kind": file_kind(name)}
            manifest[name] = entry
            self._write_manifest(project_id, manifest)
            return entry

    def get(self, project_id: str, name: str) -> tuple[bytes, dict] | None:
        if not valid_name(name):
            return None
        entry = self.manifest(project_id).get(name)
        if entry is None:
            return None
        try:
            with open(os.path.join(self._dir(project_id), name), "rb") as fh:
                return fh.read(), entry
        except OSError:
            return None
