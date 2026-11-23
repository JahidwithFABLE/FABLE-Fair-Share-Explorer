"""Durable snapshot storage on GitHub's app-data branch.

Only Admin publish/settings actions write. Raw workbooks are content-addressed
and are committed unchanged with their snapshot and catalogue in one ref update.
"""
from __future__ import annotations

import base64
from collections import OrderedDict
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from threading import RLock, local
from time import monotonic, sleep
from urllib.parse import quote

import requests

from publication import checked_public_views


BRANCH = "app-data"
PREFIX = "fair-share/v1"
INDEX = PREFIX + "/index.json"
MAX_UPLOAD_MIB = 95


class GitHubStorageError(RuntimeError):
    def __init__(self, status, action):
        self.status = status
        super().__init__(f"GitHub storage {action} failed (HTTP {status}).")


class Base64BlobBody:
    """Length-known JSON body without building a full Base64 string in memory."""
    prefix = b'{"encoding":"base64","content":"'
    suffix = b'"}'
    chunk_size = 768 * 1024

    def __init__(self, data):
        self.data = data

    def __len__(self):
        return len(self.prefix) + 4 * ((len(self.data) + 2) // 3) + len(self.suffix)

    def __iter__(self):
        yield self.prefix
        data = memoryview(self.data)
        for start in range(0, len(data), self.chunk_size):
            yield base64.b64encode(data[start:start + self.chunk_size])
        yield self.suffix


class GitHubAPI:
    def __init__(self, repository, token, *, base="https://api.github.com"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repository):
            raise ValueError("FABLE_GITHUB_REPOSITORY must be in OWNER/REPO format.")
        if not token:
            raise ValueError("FABLE_GITHUB_TOKEN is required for GitHub storage.")
        self.base = base.rstrip("/") + "/repos/" + repository
        self._token = token
        self._threads = local()

    def _session(self):
        session = getattr(self._threads, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({
                "Authorization": "Bearer " + self._token,
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "FABLE-Fair-Share-Explorer",
            })
            self._threads.session = session
        return session

    def call(self, method, path, *, payload=None, params=None, body=None,
             raw=False, missing_ok=False):
        headers = {"Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        action = "read" if method == "GET" else "write"
        try:
            response = self._session().request(
                method, self.base + path, json=payload, params=params, data=body,
                headers=headers, timeout=(8, 180 if body is not None else 20),
                allow_redirects=False,
            )
        except requests.RequestException:
            raise RuntimeError(f"Could not reach GitHub storage during {action}. Try again.") from None
        with response:
            if response.status_code == 404 and missing_ok:
                return None
            if not response.ok or response.status_code >= 300:
                raise GitHubStorageError(response.status_code, action)
            return response.content if raw else response.json()

    def ref(self):
        data = self.call("GET", f"/git/ref/heads/{BRANCH}", missing_ok=True)
        if data is None:
            return None
        if data.get("ref") != f"refs/heads/{BRANCH}" or data["object"]["type"] != "commit":
            raise ValueError("Unexpected GitHub data-branch reference.")
        return data["object"]["sha"]

    def read(self, path, head, *, missing_ok=False):
        return self.call("GET", "/contents/" + quote(path, safe="/"),
                         params={"ref": head}, raw=True, missing_ok=missing_ok)

    def blob(self, data):
        return self.call("POST", "/git/blobs", body=Base64BlobBody(data))["sha"]

    def commit(self, parent, entries, message):
        tree_body = {"tree": entries}
        if parent is not None:
            tree_body["base_tree"] = self.call("GET", f"/git/commits/{parent}")["tree"]["sha"]
        tree = self.call("POST", "/git/trees", payload=tree_body)["sha"]
        commit = self.call("POST", "/git/commits", payload={
            "message": message, "tree": tree, "parents": [parent] if parent else [],
        })["sha"]
        if parent:
            self.call("PATCH", f"/git/refs/heads/{BRANCH}", payload={"sha": commit, "force": False})
        else:
            self.call("POST", "/git/refs", payload={"ref": f"refs/heads/{BRANCH}", "sha": commit})
        return commit


def _json(data):
    return json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _entry(path, *, content=None, sha=None):
    return {"path": path, "mode": "100644", "type": "blob",
            **({"content": content} if sha is None else {"sha": sha})}


def _run_path(run_id):
    return f"{PREFIX}/runs/{run_id}.json"


def _source_path(digest):
    return f"{PREFIX}/sources/{digest}.xlsx"


def _empty_index():
    return {"version": 1, "events": {}, "runs": {}, "sources": {}}


class GitHubRepository:
    max_upload_mib = MAX_UPLOAD_MIB
    github_archive = True

    def __init__(self, repository, token, *, api=None, ttl=8.0):
        self.api = api if api is not None else GitHubAPI(repository, token)
        self._ttl = ttl
        self._read_lock = RLock()
        self._write_lock = RLock()
        self._cache = None
        self._results = OrderedDict()
        # Reject a bad token or repo early, even before the data branch exists.
        self.public_git_archive = not self.api.call("GET", "").get("private", False)

    def _read_index(self, head):
        if head is None:
            return _empty_index()
        raw = self.api.read(INDEX, head, missing_ok=True)
        if raw is None:
            return _empty_index()
        data = json.loads(raw)
        if (not isinstance(data, dict) or data.get("version") != 1
                or any(not isinstance(data.get(k), dict) for k in ("events", "runs", "sources"))):
            raise ValueError("Unsupported Fair Share GitHub catalogue. Existing data was not changed.")
        return data

    def _current(self):
        with self._read_lock:
            if self._cache is not None and monotonic() < self._cache[0]:
                return self._cache[1:]
            head = self.api.ref()
            index = self._read_index(head)
            self._cache = monotonic() + self._ttl, head, index
            return head, index

    def _change(self, update, message):
        # Re-read on writes, and retry if another server instance moved the
        # branch head. A non-fast-forward update never overwrites another run.
        with self._write_lock:
            for attempt in range(4):
                head = self.api.ref()
                index = self._read_index(head)
                entries, result, changed = update(index)
                if not changed:
                    with self._read_lock:
                        self._cache = monotonic() + self._ttl, head, index
                    return result
                entries.append(_entry(INDEX, content=_json(index)))
                try:
                    new_head = self.api.commit(head, entries, message)
                except GitHubStorageError as exc:
                    if exc.status not in (409, 422) or self.api.ref() == head:
                        raise
                    sleep(.2 * (attempt + 1))
                    continue
                with self._read_lock:
                    self._cache = monotonic() + self._ttl, new_head, index
                return result
        raise RuntimeError("GitHub app-data changed repeatedly. Retry the save.")

    def titles(self):
        _, index = self._current()
        return dict(index["events"])

    def title(self, event, default):
        return self.titles().get(event, default)

    def catalogue(self, private=False):
        _, index = self._current()
        rows = [{**row, "title": index["events"][row["event_id"]]}
                for row in index["runs"].values() if private or row["published"]]
        rows.sort(key=lambda row: row["id"])
        return sorted(rows, key=lambda row: row["published_at"], reverse=True)

    def _get(self, head, index, run_id, private=False):
        row = index["runs"].get(run_id)
        if row is None or (not private and not row["published"]):
            return None
        key = (head, row["id"])
        with self._read_lock:
            raw = self._results.get(key)
        if raw is None:
            raw = self.api.read(_run_path(row["id"]), head)
            with self._read_lock:
                self._results[key] = raw
                self._results.move_to_end(key)
                if len(self._results) > 32:
                    self._results.popitem(last=False)
        result = json.loads(raw)
        if "visibility" in row:
            result["public_views"] = row["visibility"]
        return result

    def get(self, run_id, private=False):
        head, index = self._current()
        return self._get(head, index, run_id, private)

    def get_pair(self, a_id, b_id):
        head, index = self._current()
        return self._get(head, index, a_id), self._get(head, index, b_id)

    def source_info(self, run_id):
        """Source filename and usage count without downloading the large Excel blob."""
        _, index = self._current()
        row = index["runs"].get(run_id)
        if row is None:
            return None
        digest = row["report_hash"]
        return {"filename": index["sources"].get(digest, "(missing source)"),
                "used_by": sum(other["report_hash"] == digest
                               for other in index["runs"].values()),
                "digest": digest}

    def source(self, run_id):
        head, index = self._current()
        row = index["runs"].get(run_id)
        if row is None:
            return None
        digest = row["report_hash"]
        data = self.api.read(_source_path(digest), head)
        if sha256(data).hexdigest() != digest:
            raise ValueError("Archived workbook checksum does not match the catalogue.")
        return index["sources"][digest], data

    def publish(self, title, result, filename, workbook, visibility=None):
        title = title.strip()
        if not title or len(title) > 110:
            raise ValueError("Enter a Scenathon name of 1–110 characters.")
        if not workbook or len(workbook) > MAX_UPLOAD_MIB * 1024**2:
            raise ValueError(f"GitHub storage accepts Excel reports up to {MAX_UPLOAD_MIB} MiB.")
        digest = sha256(workbook).hexdigest()
        if digest != result["report_hash"]:
            raise ValueError("The workbook changed after validation. Run the preview again.")
        identity = [result[k] for k in ("event", "pathway", "after_trade", "snapshot",
                                        "reference_fingerprint", "report_hash")]
        run_id = sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:24]
        filename = filename.replace("\\","/").split("/")[-1][:180] or "scenathon.xlsx"
        if visibility is not None:
            visibility = checked_public_views(visibility)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        payload = {k:v for k,v in result.items() if k != "public_views"}
        blob_sha = None

        def update(index):
            nonlocal blob_sha
            entries = []
            event, pathway, after_trade, snapshot, reference, _ = identity
            old = index["runs"].get(run_id)
            created = old is None
            changed = created or index["events"].get(event) != title
            if digest not in index["sources"]:
                if blob_sha is None:
                    blob_sha = self.api.blob(workbook)
                entries.append(_entry(_source_path(digest), sha=blob_sha))
                index["sources"][digest] = filename
                changed = True
            index["events"][event] = title
            if created:
                entries.append(_entry(_run_path(run_id), content=_json(payload)))
                row = {"id": run_id, "event_id": event, "pathway": pathway,
                       "after_trade": int(after_trade), "snapshot": snapshot,
                       "reference": reference, "report_hash": digest,
                       "published_at": now, "published": 1}
                index["runs"][run_id] = row
            else:
                row = old
                if not row["published"]:
                    row["published"] = 1
                    row["published_at"] = now
                    changed = True
            if visibility is not None and row.get("visibility") != visibility:
                row["visibility"] = visibility
                changed = True
            return entries, (run_id, created), changed

        return self._change(update, "Publish Fair Share snapshot " + run_id)

    def set_public_views(self, run_id, visibility):
        visibility = checked_public_views(visibility)

        def update(index):
            row = index["runs"].get(run_id)
            if row is None:
                raise ValueError("Snapshot no longer exists.")
            changed = row.get("visibility") != visibility
            row["visibility"] = visibility
            return [], None, changed

        self._change(update, "Update Fair Share snapshot views")

    def hide(self, run_id):
        def update(index):
            row = index["runs"].get(run_id)
            if row is None or not row["published"]:
                return [], None, False
            row["published"] = 0
            return [], None, True

        self._change(update, "Hide Fair Share snapshot from app")

    def restore(self, run_id):
        """Make an existing unpublished snapshot visible again without re-uploading."""
        def update(index):
            row = index["runs"].get(run_id)
            if row is None:
                raise ValueError("Snapshot no longer exists.")
            if row["published"]:
                return [], None, False
            row["published"] = 1
            # Preserve its original timestamp: restoring an old export does
            # not make its scientific data the newest submitted version.
            return [], None, True

        self._change(update, "Republish existing Fair Share snapshot " + run_id)

    def delete(self, run_id):
        """Delete one stored result, and its workbook only when no runs refer to it.

        The deletion affects the current Git tree, not historical Git commits.
        """
        def update(index):
            row = index["runs"].get(run_id)
            if row is None:
                raise ValueError("Snapshot no longer exists.")
            digest = row["report_hash"]
            del index["runs"][run_id]
            entries = [{"path": _run_path(run_id), "mode": "100644",
                        "type": "blob", "sha": None}]
            # Before and After trade can share one archived workbook. Never
            # delete its source while any other published/unpublished run needs it.
            if not any(other["report_hash"] == digest for other in index["runs"].values()):
                if digest in index["sources"]:
                    del index["sources"][digest]
                    entries.append({"path": _source_path(digest), "mode": "100644",
                                    "type": "blob", "sha": None})
            return entries, None, True

        self._change(update, "Delete Fair Share snapshot " + run_id)
