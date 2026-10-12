"""Draft upload workflow: durable intent, one remote writer, GET reconciliation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4, uuid5

from .content import validate_draft
from .models import WordPressPayloadV1
from .protocol import (
    NAMESPACE,
    ProtocolError,
    atomic_write,
    canonical,
    digest,
    load_json,
    object_hash,
    process_lock,
    read_bundle,
)
from .settings import Settings
from .store import Store
from .wordpress import WordPressClient, wordpress_target


class Port(Protocol):
    def find(self, slug: str) -> dict[str, Any] | None: ...
    def get(self, post_id: str) -> dict[str, Any]: ...
    def create_draft(self, payload: WordPressPayloadV1) -> dict[str, Any]: ...
    def get_media(self, media_id: str) -> dict[str, Any]: ...
    def get_media_bytes(self, url: str) -> bytes: ...
    def replace_draft(self, post_id: str, payload: WordPressPayloadV1) -> dict[str, Any]: ...
    def find_media(self, slug: str) -> dict[str, Any] | None: ...
    def upload_image(self, image: bytes, **kwargs: Any) -> dict[str, Any]: ...
    def set_featured_media(self, post_id: str, media_id: str, **kwargs: Any) -> dict[str, Any]: ...


def remote_payload(post: dict[str, Any]) -> dict[str, Any]:
    value = {key: post[key]["raw"] for key in ("title", "content", "excerpt")}
    value.update(
        slug=post["slug"],
        status=post["status"],
        categories=sorted(post["categories"]),
        tags=sorted(post["tags"]),
    )
    media = post.get("featured_media", 0)
    if media:
        value["featured_media"] = media
    return value


def matches(post: dict[str, Any], payload: dict[str, Any]) -> bool:
    return remote_payload(post) == payload


def remote_hash(post: dict[str, Any]) -> str:
    return object_hash({**remote_payload(post), "modified_gmt": post.get("modified_gmt")})


class Uploader:
    def __init__(self, store: Store, settings: Settings, port: Port | None = None) -> None:
        self.store, self.settings = store, settings
        self.port = port
        self.target = wordpress_target(settings.base_url, settings.post_type, settings.rest_base)
        with store.transaction() as db:
            old = db.execute("SELECT value FROM meta WHERE key='target'").fetchone()
            if old and old[0] != self.target:
                raise ProtocolError("STATE_TARGET_CHANGED")
            db.execute("INSERT OR IGNORE INTO meta VALUES ('target',?)", (self.target,))

    def client(self) -> Port:
        if not self.settings.enabled:
            raise ProtocolError("WORDPRESS_DISABLED")
        if self.port is None:
            self.port = WordPressClient(
                self.settings.base_url,
                self.settings.username,
                self.settings.password,
                post_type=self.settings.post_type,
                rest_base=self.settings.rest_base,
            )
        return self.port

    def import_file(self, path: Path) -> str:
        if path.stat().st_size > 25 * 1024 * 1024:
            raise ProtocolError("BUNDLE_TOO_LARGE")
        data = path.read_bytes()
        meta, files = read_bundle(data, draft=True)
        validate_draft(meta, files, self.settings.review_required)
        package = meta["package_id"]
        bundle_hash = digest(data)
        cache = self.store.directory / "cache" / (bundle_hash[7:] + ".nwdraft")
        atomic_write(cache, data, immutable=True)
        with self.store.transaction() as db:
            old = db.execute(
                "SELECT manifest_hash FROM bundles WHERE package_id=?", (package,)
            ).fetchone()
            if old and old[0] == "":
                db.execute(
                    "UPDATE bundles SET manifest_hash=?,bundle_hash=?,cache_path=?,metadata=?"
                    " WHERE package_id=?",
                    (
                        meta["manifest_hash"],
                        bundle_hash,
                        str(cache),
                        canonical(meta).decode(),
                        package,
                    ),
                )
                conflict = False
            elif old:
                if old[0] != meta["manifest_hash"]:
                    db.execute(
                        "UPDATE deliveries SET state='conflict',error='PACKAGE_ID_CONFLICT'"
                        " WHERE package_id=?",
                        (package,),
                    )
                    # Commit conflict before raising, retaining any previous remote IDs.
                    conflict = True
                else:
                    conflict = False
            else:
                db.execute(
                    "INSERT INTO bundles VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        package,
                        meta["producer_id"],
                        meta["article_id"],
                        meta["revision"],
                        meta["artifact_revision"],
                        meta["manifest_hash"],
                        bundle_hash,
                        str(cache),
                        canonical(meta).decode(),
                    ),
                )
                conflict = False
        if conflict:
            raise ProtocolError("PACKAGE_ID_CONFLICT")
        return package

    def scan(self) -> list[str]:
        errors = []
        if self.settings.transfer_dir:
            ready = self.settings.transfer_dir / "ready"
            try:
                paths = sorted(ready.glob("*.nwdraft"))
            except OSError:
                return ["TRANSFER_UNAVAILABLE"]
            for path in paths:
                try:
                    self.import_file(path)
                except Exception as exc:
                    errors.append(getattr(exc, "code", "BUNDLE_INVALID"))
        return errors

    def payload(self, meta: dict[str, Any]) -> dict[str, Any]:
        _, files = read_bundle(
            Path(
                self.store.db.execute(
                    "SELECT cache_path FROM bundles WHERE package_id=?", (meta["package_id"],)
                ).fetchone()[0]
            ),
            draft=True,
        )
        validate_draft(meta, files, self.settings.review_required)
        try:
            categories = sorted({self.settings.category_map[key] for key in meta["category_keys"]})
            tags = sorted({self.settings.tag_map[key] for key in meta.get("tag_keys", [])})
        except KeyError as exc:
            raise ProtocolError("TAXONOMY_MAPPING_REQUIRED") from exc
        key = object_hash(
            [
                self.target,
                meta["producer_id"],
                meta["article_id"],
                meta["revision"],
                meta["artifact_revision"],
            ]
        )[7:]
        return WordPressPayloadV1(
            title=meta["title"],
            content=files["article.html"].decode(),
            excerpt=meta["excerpt"],
            slug="news-" + key,
            categories=tuple(categories),
            tags=tuple(tags),
        ).model_dump(mode="json")

    def prepare(self, *, recovery: bool = False) -> None:
        if recovery:
            with self.store.transaction() as db:
                db.execute("INSERT OR REPLACE INTO meta VALUES ('recovery_mode','1')")
                db.execute("INSERT OR REPLACE INTO meta VALUES ('instance_id',?)", (str(uuid4()),))
                db.execute(
                    "UPDATE deliveries SET state='uncertain' WHERE state IN ('prepared','creating')"
                )
                db.execute(
                    "UPDATE media_deliveries SET state='uncertain' WHERE state IN ('prepared','creating')"
                )
        recovering = self.store.db.execute(
            "SELECT value FROM meta WHERE key='recovery_mode'"
        ).fetchone()
        new_state_uncertain = recovery or bool(recovering and recovering[0] == "1")
        rows = self.store.db.execute(
            "SELECT * FROM bundles ORDER BY revision DESC, artifact_revision DESC,package_id"
        ).fetchall()
        for bundle in rows:
            meta = load_json(bundle["metadata"].encode())
            old = self.store.db.execute(
                "SELECT state FROM deliveries WHERE target_id=? AND package_id=?",
                (self.target, bundle["package_id"]),
            ).fetchone()
            if old and old[0] not in {"blocked"}:
                continue
            try:
                payload = self.payload(meta)
                error = None
                state = "uncertain" if new_state_uncertain else "prepared"
            except (ValueError, KeyError) as exc:
                payload, error, state = {}, getattr(exc, "code", "PAYLOAD_INVALID"), "blocked"
            key = payload.get("slug", "news-" + object_hash([self.target, meta["package_id"]])[7:])[
                5:
            ]
            with self.store.transaction() as db:
                siblings = db.execute(
                    "SELECT b.*,d.state FROM bundles b JOIN deliveries d USING(package_id)"
                    " WHERE d.target_id=? AND b.producer_id=? AND b.article_id=?"
                    " AND b.package_id<>?",
                    (self.target, meta["producer_id"], meta["article_id"], meta["package_id"]),
                ).fetchall()
                for sibling in siblings:
                    if sibling["state"] in {
                        "creating",
                        "uncertain",
                        "draft_created",
                        "completed",
                        "revision_pending",
                        "conflict",
                    }:
                        state, error = "revision_pending", "EXPLICIT_REPLACEMENT_REQUIRED"
                    elif (sibling["revision"], sibling["artifact_revision"]) > (
                        meta["revision"],
                        meta["artifact_revision"],
                    ):
                        state = "superseded"
                claim = db.execute(
                    "SELECT * FROM source_claims WHERE target_id=? AND source_hash=?",
                    (self.target, meta["source_set_hash"]),
                ).fetchone()
                if claim and (claim["producer_id"], claim["article_id"]) != (
                    meta["producer_id"],
                    meta["article_id"],
                ):
                    state, error = "blocked", "WORDPRESS_SOURCE_SET_DUPLICATE"
                if state in {"prepared", "uncertain"}:
                    db.execute(
                        "INSERT OR IGNORE INTO source_claims VALUES (?,?,?,?,?)",
                        (
                            self.target,
                            meta["source_set_hash"],
                            meta["producer_id"],
                            meta["article_id"],
                            meta["package_id"],
                        ),
                    )
                db.execute(
                    "INSERT INTO deliveries VALUES (?,?,?,?,?,NULL,NULL,NULL,NULL,?)"
                    " ON CONFLICT(target_id,package_id) DO UPDATE SET "
                    "state=excluded.state,payload=excluded.payload,error=excluded.error",
                    (
                        self.target,
                        meta["package_id"],
                        key,
                        canonical(payload).decode(),
                        state,
                        error,
                    ),
                )

    def event(self, db: Any, package: str, state: str, error: str | None = None) -> None:
        seq = db.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE target_id=? AND package_id=?",
            (self.target, package),
        ).fetchone()[0]
        row = db.execute(
            "SELECT * FROM deliveries WHERE target_id=? AND package_id=?", (self.target, package)
        ).fetchone()
        bundle = db.execute(
            "SELECT manifest_hash FROM bundles WHERE package_id=?", (package,)
        ).fetchone()
        instance_id = db.execute("SELECT value FROM meta WHERE key='instance_id'").fetchone()[0]
        event_id = str(
            uuid5(NAMESPACE, canonical([instance_id, self.target, package, seq]).decode())
        )
        payload = {
            "schema_version": "1.0",
            "event_id": event_id,
            "target_id": self.target,
            "package_id": package,
            "manifest_hash": bundle[0],
            "sequence": seq,
            "uploader_instance_id": instance_id,
            "state": state,
            "remote_post_id": row["remote_post_id"],
            "error": error,
            "created_at": datetime.now(UTC).isoformat(),
        }
        db.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,0)",
            (event_id, self.target, package, seq, canonical(payload).decode()),
        )

    def update(
        self,
        package: str,
        state: str,
        *,
        error: str | None = None,
        post: dict[str, Any] | None = None,
    ) -> None:
        with self.store.transaction() as db:
            prior = db.execute(
                "SELECT state,error,remote_hash FROM deliveries WHERE target_id=? AND package_id=?",
                (self.target, package),
            ).fetchone()
            if (
                prior
                and prior["state"] == state
                and prior["error"] == error
                and (post is None or prior["remote_hash"] == remote_hash(post))
            ):
                return
            if post is not None:
                db.execute(
                    "UPDATE deliveries SET state=?,error=?,remote_post_id=?,remote_hash=?,remote_status=?"
                    " WHERE target_id=? AND package_id=?",
                    (
                        state,
                        error,
                        str(post["id"]),
                        remote_hash(post),
                        post["status"],
                        self.target,
                        package,
                    ),
                )
            else:
                db.execute(
                    "UPDATE deliveries SET state=?,error=? WHERE target_id=? AND package_id=?",
                    (state, error, self.target, package),
                )
            self.event(db, package, state, error)

    def process(self, package: str, *, allow_writes: bool = True) -> None:
        row = self.store.db.execute(
            "SELECT * FROM deliveries WHERE target_id=? AND package_id=?", (self.target, package)
        ).fetchone()
        if row["state"] not in {
            "prepared",
            "creating",
            "uncertain",
            "draft_created",
            "completed",
            "replacing",
            "replace_uncertain",
        }:
            return
        payload = load_json(row["payload"].encode())
        client = self.client()
        try:
            post = (
                client.get(row["remote_post_id"])
                if row["remote_post_id"]
                else client.find(payload["slug"])
            )
            if post is not None:
                if (
                    row["state"] in {"draft_created", "completed"}
                    and matches(post, payload)
                    and row["remote_hash"]
                    and remote_hash(post) != row["remote_hash"]
                ):
                    self.update(package, "conflict", error="WORDPRESS_REMOTE_CONFLICT")
                    return
                if row["state"] == "completed" and matches(post, payload):
                    return
                if not matches(post, payload):
                    media = self.store.db.execute(
                        "SELECT remote_media_id FROM media_deliveries WHERE target_id=? AND package_id=?"
                        " AND state IN ('uploaded','attached')",
                        (self.target, package),
                    ).fetchone()
                    desired = (
                        {**payload, "featured_media": int(media[0])} if media and media[0] else None
                    )
                    if desired and matches(post, desired):
                        payload = desired
                        with self.store.transaction() as db:
                            db.execute(
                                "UPDATE deliveries SET payload=? WHERE target_id=? AND package_id=?",
                                (canonical(payload).decode(), self.target, package),
                            )
                    elif (
                        row["state"] in {"replacing", "replace_uncertain"}
                        and post["status"] == "draft"
                        and remote_hash(post) == row["remote_hash"]
                    ):
                        self.update(
                            package, "replace_uncertain", error="EXPLICIT_REPLACE_RETRY_REQUIRED"
                        )
                        return
                    else:
                        self.update(package, "conflict", error="WORDPRESS_REMOTE_CONFLICT")
                        return
                self.update(package, "draft_created", post=post)
                with self.store.transaction() as db:
                    db.execute(
                        "UPDATE deliveries SET state='superseded' WHERE target_id=?"
                        " AND package_id<>? AND remote_post_id=?",
                        (self.target, package, str(post["id"])),
                    )
            elif row["state"] == "prepared" and allow_writes:
                with self.store.transaction() as db:
                    db.execute(
                        "UPDATE deliveries SET state='creating',attempt_id=?"
                        " WHERE target_id=? AND package_id=?",
                        (str(uuid4()), self.target, package),
                    )
                    self.event(db, package, "creating")
                post = client.create_draft(WordPressPayloadV1.model_validate(payload))
                if not matches(post, payload):
                    media = self.store.db.execute(
                        "SELECT remote_media_id FROM media_deliveries WHERE target_id=? AND package_id=?"
                        " AND state IN ('uploaded','attached')",
                        (self.target, package),
                    ).fetchone()
                    desired = (
                        {**payload, "featured_media": int(media[0])} if media and media[0] else None
                    )
                    if desired and matches(post, desired):
                        payload = desired
                        with self.store.transaction() as db:
                            db.execute(
                                "UPDATE deliveries SET payload=? WHERE target_id=? AND package_id=?",
                                (canonical(payload).decode(), self.target, package),
                            )
                    elif (
                        row["state"] in {"replacing", "replace_uncertain"}
                        and post["status"] == "draft"
                        and remote_hash(post) == row["remote_hash"]
                    ):
                        self.update(
                            package, "replace_uncertain", error="EXPLICIT_REPLACE_RETRY_REQUIRED"
                        )
                        return
                    else:
                        self.update(package, "conflict", error="WORDPRESS_REMOTE_CONFLICT")
                        return
                self.update(package, "draft_created", post=post)
                with self.store.transaction() as db:
                    db.execute(
                        "UPDATE deliveries SET state='superseded' WHERE target_id=?"
                        " AND package_id<>? AND remote_post_id=?",
                        (self.target, package, str(post["id"])),
                    )
            else:
                self.update(package, "uncertain", error="WORDPRESS_RECONCILIATION_PENDING")
                return
            self.decorate(package, post, allow_writes=allow_writes)
        except Exception as exc:
            code = getattr(exc, "code", "WORDPRESS_NETWORK_UNCERTAIN")
            current = self.store.db.execute(
                "SELECT state FROM deliveries WHERE target_id=? AND package_id=?",
                (self.target, package),
            ).fetchone()[0]
            if current in {"replacing", "replace_uncertain"}:
                self.update(package, "replace_uncertain", error=code)
            elif current in {"creating", "uncertain"}:
                self.update(package, "uncertain", error=code)
            elif current == "draft_created":
                self.update(package, "draft_created", error=code)
            else:
                self.update(package, current, error=code)

    def decorate(self, package: str, post: dict[str, Any], *, allow_writes: bool = True) -> None:
        bundle = self.store.db.execute(
            "SELECT * FROM bundles WHERE package_id=?", (package,)
        ).fetchone()
        if not bundle["cache_path"]:
            self.update(package, "completed", post=post)
            return
        meta, files = read_bundle(Path(bundle["cache_path"]), draft=True)
        image = meta.get("featured_image")
        if image is None:
            self.update(package, "completed", post=post)
            return
        data = files[image["path"]]
        asset_hash = digest(data)
        slug = "nw-image-" + object_hash([self.target, package, asset_hash])[7:]
        with self.store.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO media_deliveries VALUES (?,?,?,'prepared',NULL,?,NULL)",
                (self.target, package, asset_hash, slug),
            )
        record = self.store.db.execute(
            "SELECT * FROM media_deliveries WHERE target_id=? AND package_id=? AND asset_hash=?",
            (self.target, package, asset_hash),
        ).fetchone()
        client = self.client()
        media = (
            client.get_media(record["remote_media_id"])
            if record["remote_media_id"]
            else client.find_media(slug)
        )
        uploaded_here = media is None
        if media is None:
            if not allow_writes:
                return
            if record["state"] != "prepared":
                raise ProtocolError("WORDPRESS_MEDIA_RECONCILIATION_PENDING")
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE media_deliveries SET state='creating' WHERE target_id=?"
                    " AND package_id=? AND asset_hash=?",
                    (self.target, package, asset_hash),
                )
            media = client.upload_image(
                data,
                filename=Path(image["path"]).name,
                slug=slug,
                title=meta["title"],
                alt_text=image["alt"],
                caption=image["caption"],
                post_id=str(post["id"]),
            )
        details = media.get("media_details", {})
        if (
            media.get("slug") != slug
            or details.get("width") != image["width"]
            or details.get("height") != image["height"]
        ):
            raise ProtocolError("WORDPRESS_MEDIA_CONFLICT")
        # Only adopt media proven by a successful response in this process or a saved ID.
        if not uploaded_here and not record["remote_media_id"]:
            proof = client.get_media_bytes(media["source_url"])
            if digest(proof) != asset_hash:
                raise ProtocolError("WORDPRESS_MEDIA_CONFLICT")
        if record["remote_media_id"] and str(media["id"]) != record["remote_media_id"]:
            raise ProtocolError("WORDPRESS_MEDIA_CONFLICT")
        with self.store.transaction() as db:
            db.execute(
                "UPDATE media_deliveries SET state='uploaded',remote_media_id=?"
                " WHERE target_id=? AND package_id=? AND asset_hash=?",
                (str(media["id"]), self.target, package, asset_hash),
            )
        row = self.store.db.execute(
            "SELECT * FROM deliveries WHERE target_id=? AND package_id=?", (self.target, package)
        ).fetchone()
        expected = load_json(row["payload"].encode())
        desired = {**expected, "featured_media": media["id"]}
        current = client.get(str(post["id"]))
        if matches(current, desired):
            updated = current
        elif matches(current, expected) and remote_hash(current) == row["remote_hash"]:
            if not allow_writes:
                return
            updated = client.set_featured_media(str(post["id"]), str(media["id"]))
            if not matches(updated, desired):
                raise ProtocolError("WORDPRESS_REMOTE_CONFLICT")
        else:
            self.update(package, "conflict", error="WORDPRESS_REMOTE_CONFLICT")
            return
        with self.store.transaction() as db:
            db.execute(
                "UPDATE deliveries SET payload=? WHERE target_id=? AND package_id=?",
                (canonical(desired).decode(), self.target, package),
            )
            db.execute(
                "UPDATE media_deliveries SET state='attached' WHERE target_id=? AND package_id=?",
                (self.target, package),
            )
        self.update(package, "completed", post=updated)

    def receipts(self, *, repair: bool = False) -> None:
        if self.settings.transfer_dir is None:
            return
        for row in self.store.db.execute(
            "SELECT * FROM events" + ("" if repair else " WHERE delivered=0")
        ).fetchall():
            try:
                path = (
                    self.settings.transfer_dir
                    / "receipts"
                    / self.target
                    / (row["event_id"] + ".json")
                )
                if row["delivered"] and path.exists():
                    continue
                atomic_write(path, row["payload"].encode(), immutable=True)
                with self.store.transaction() as db:
                    db.execute("UPDATE events SET delivered=1 WHERE event_id=?", (row["event_id"],))
            except OSError:
                break

    def run_once(self, *, reconcile_only: bool = False) -> list[str]:
        with process_lock(self.store.directory / "worker.lock"):
            errors = self.scan()
            self.prepare()
            rows = self.store.db.execute(
                "SELECT package_id,state FROM deliveries WHERE target_id=?"
                " AND state IN ('prepared','creating','uncertain','draft_created','replacing','replace_uncertain'"
                + (",'completed')" if reconcile_only else ")"),
                (self.target,),
            ).fetchall()
            for row in rows:
                if reconcile_only and row["state"] == "prepared":
                    continue
                self.process(row["package_id"], allow_writes=not reconcile_only)
            self.receipts(repair=reconcile_only)
            return errors

    def resolve_media(
        self,
        package: str,
        *,
        adopt_media_id: str | None = None,
        confirmed_not_created: bool = False,
    ) -> None:
        with process_lock(self.store.directory / "worker.lock"):
            record = self.store.db.execute(
                "SELECT * FROM media_deliveries WHERE target_id=? AND package_id=?",
                (self.target, package),
            ).fetchone()
            if record is None or record["state"] not in {"creating", "uncertain"}:
                raise ProtocolError("MEDIA_RESOLUTION_STATE_INVALID")
            remote_id = None
            if adopt_media_id:
                bundle = self.store.db.execute(
                    "SELECT cache_path FROM bundles WHERE package_id=?", (package,)
                ).fetchone()
                meta, files = read_bundle(Path(bundle[0]), draft=True)
                image = meta["featured_image"]
                media = self.client().get_media(adopt_media_id)
                if (
                    media["slug"] != record["slug"]
                    or media["media_details"].get("width") != image["width"]
                    or media["media_details"].get("height") != image["height"]
                    or digest(self.client().get_media_bytes(media["source_url"]))
                    != digest(files[image["path"]])
                ):
                    raise ProtocolError("WORDPRESS_MEDIA_CONFLICT")
                remote_id = str(media["id"])
            elif confirmed_not_created:
                if self.client().find_media(record["slug"]) is not None:
                    raise ProtocolError("WORDPRESS_MEDIA_ALREADY_EXISTS")
            else:
                raise ProtocolError("RESOLUTION_CONFIRMATION_REQUIRED")
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE media_deliveries SET state=?,remote_media_id=?"
                    " WHERE target_id=? AND package_id=?",
                    ("uploaded" if remote_id else "prepared", remote_id, self.target, package),
                )

    def finish_recovery(self) -> None:
        with process_lock(self.store.directory / "worker.lock"), self.store.transaction() as db:
            unresolved = db.execute(
                "SELECT COUNT(*) FROM deliveries WHERE state IN "
                "('creating','uncertain','replacing','replace_uncertain')"
            ).fetchone()[0]
            media = db.execute(
                "SELECT COUNT(*) FROM media_deliveries WHERE state IN ('creating','uncertain')"
            ).fetchone()[0]
            if unresolved or media:
                raise ProtocolError("RECOVERY_RECONCILIATION_PENDING")
            db.execute("DELETE FROM meta WHERE key='recovery_mode'")

    def resolve(
        self, package: str, *, adopt_post_id: str | None = None, confirmed_not_created: bool = False
    ) -> None:
        with process_lock(self.store.directory / "worker.lock"):
            row = self.store.db.execute(
                "SELECT * FROM deliveries WHERE target_id=? AND package_id=?",
                (self.target, package),
            ).fetchone()
            if row is None or row["state"] not in {"uncertain", "creating"}:
                raise ProtocolError("RESOLUTION_STATE_INVALID")
            payload = load_json(row["payload"].encode())
            if adopt_post_id:
                post = self.client().get(adopt_post_id)
                if not matches(post, payload):
                    raise ProtocolError("WORDPRESS_REMOTE_CONFLICT")
                self.update(package, "draft_created", post=post)
            elif confirmed_not_created:
                if self.client().find(payload["slug"]) is not None:
                    raise ProtocolError("WORDPRESS_ALREADY_EXISTS")
                self.update(package, "prepared", error="OPERATOR_CONFIRMED_NOT_CREATED")
            else:
                raise ProtocolError("RESOLUTION_CONFIRMATION_REQUIRED")

    def replace(self, package: str, expected_remote_hash: str) -> None:
        """Explicit replacement of a known, unedited draft. Preserve CMS ID and slug."""
        with process_lock(self.store.directory / "worker.lock"):
            current = self.store.db.execute(
                "SELECT d.*,b.producer_id,b.article_id FROM deliveries d JOIN bundles b USING(package_id)"
                " WHERE d.target_id=? AND d.package_id=?",
                (self.target, package),
            ).fetchone()
            if current is None or current["state"] not in {"revision_pending", "replace_uncertain"}:
                raise ProtocolError("REPLACEMENT_STATE_INVALID")
            prior = self.store.db.execute(
                "SELECT d.*,b.revision,b.artifact_revision FROM deliveries d JOIN bundles b USING(package_id)"
                " WHERE d.target_id=? AND b.producer_id=? AND b.article_id=?"
                " AND d.remote_post_id IS NOT NULL AND d.package_id<>?"
                " ORDER BY b.revision DESC,b.artifact_revision DESC LIMIT 1",
                (self.target, current["producer_id"], current["article_id"], package),
            ).fetchone()
            if prior is None:
                raise ProtocolError("EXISTING_REMOTE_DRAFT_REQUIRED")
            post = self.client().get(prior["remote_post_id"])
            if post["status"] != "draft" or remote_hash(post) != expected_remote_hash:
                raise ProtocolError("WORDPRESS_REMOTE_CONFLICT")
            if expected_remote_hash != prior["remote_hash"]:
                raise ProtocolError("WORDPRESS_REMOTE_CONFLICT")
            payload = load_json(current["payload"].encode())
            if not payload:
                raise ProtocolError("REPLACEMENT_PAYLOAD_REQUIRED")
            payload["slug"] = post["slug"]
            if post.get("featured_media"):
                payload["featured_media"] = post["featured_media"]
            bundle = self.store.db.execute(
                "SELECT metadata FROM bundles WHERE package_id=?", (package,)
            ).fetchone()
            meta = load_json(bundle[0].encode())
            with self.store.transaction() as db:
                claim = db.execute(
                    "SELECT * FROM source_claims WHERE target_id=? AND source_hash=?",
                    (self.target, meta["source_set_hash"]),
                ).fetchone()
                if claim and (claim["producer_id"], claim["article_id"]) != (
                    meta["producer_id"],
                    meta["article_id"],
                ):
                    raise ProtocolError("WORDPRESS_SOURCE_SET_DUPLICATE")
                db.execute(
                    "INSERT OR IGNORE INTO source_claims VALUES (?,?,?,?,?)",
                    (
                        self.target,
                        meta["source_set_hash"],
                        meta["producer_id"],
                        meta["article_id"],
                        package,
                    ),
                )
                db.execute(
                    "UPDATE deliveries SET state='replacing',payload=?,remote_post_id=?,"
                    " remote_hash=?,attempt_id=? WHERE target_id=? AND package_id=?",
                    (
                        canonical(payload).decode(),
                        prior["remote_post_id"],
                        expected_remote_hash,
                        str(uuid4()),
                        self.target,
                        package,
                    ),
                )
                self.event(db, package, "replacing")
            try:
                updated = self.client().replace_draft(
                    prior["remote_post_id"], WordPressPayloadV1.model_validate(payload)
                )
                if not matches(updated, payload):
                    raise ProtocolError("WORDPRESS_REMOTE_CONFLICT")
                self.update(package, "draft_created", post=updated)
                with self.store.transaction() as db:
                    db.execute(
                        "UPDATE deliveries SET state='superseded' WHERE target_id=? AND package_id=?",
                        (self.target, prior["package_id"]),
                    )
                self.decorate(package, updated)
            except Exception as exc:
                self.update(
                    package,
                    "replace_uncertain",
                    error=getattr(exc, "code", "WORDPRESS_REPLACE_UNCERTAIN"),
                )

    def import_history(self, path: Path) -> int:
        """Import supported-target history before enabling the new sender. No remote writes."""
        data = load_json(path.read_bytes())
        if data.get("schema_version") != "1.0":
            raise ProtocolError("HISTORY_SCHEMA_UNSUPPORTED")
        rows = data.get("deliveries", [])
        count = 0
        with process_lock(self.store.directory / "worker.lock"), self.store.transaction() as db:
            for record in rows:
                if record["target_id"] != self.target:
                    raise ProtocolError("HISTORY_TARGET_MISMATCH")
                identity = {
                    k: record[k]
                    for k in (
                        "producer_id",
                        "article_id",
                        "draft_id",
                        "revision",
                        "artifact_revision",
                    )
                }
                from .protocol import package_identity

                package = package_identity(identity)
                identity["package_id"] = package
                payload = WordPressPayloadV1.model_validate(record["payload"]).model_dump(
                    mode="json"
                )
                if object_hash(record["payload"]) != record["payload_hash"]:
                    raise ProtocolError("HISTORY_PAYLOAD_HASH_MISMATCH")
                if record["state"] in {"published", "trashed", "trashing", "legacy"}:
                    state = "historical_" + record["state"]
                else:
                    state = "uncertain"  # Even successful history is first reconciled.
                identity["history_imported"] = True
                db.execute(
                    "INSERT OR IGNORE INTO bundles VALUES (?,?,?,?,?,'','','',?)",
                    (
                        package,
                        identity["producer_id"],
                        identity["article_id"],
                        identity["revision"],
                        identity["artifact_revision"],
                        canonical(identity).decode(),
                    ),
                )
                cursor = db.execute(
                    "INSERT OR IGNORE INTO deliveries VALUES (?,?,?,?,?,?,?,?,NULL,NULL)",
                    (
                        self.target,
                        package,
                        record["operation_key"],
                        canonical(payload).decode(),
                        state,
                        record.get("remote_post_id"),
                        record.get("remote_hash"),
                        record.get("remote_status"),
                    ),
                )
                count += cursor.rowcount
                source = record.get("source_set_hash")
                if source:
                    old = db.execute(
                        "SELECT * FROM source_claims WHERE target_id=? AND source_hash=?",
                        (self.target, source),
                    ).fetchone()
                    if old and (old["producer_id"], old["article_id"]) != (
                        identity["producer_id"],
                        identity["article_id"],
                    ):
                        raise ProtocolError("HISTORY_SOURCE_SET_CONFLICT")
                    db.execute(
                        "INSERT OR IGNORE INTO source_claims VALUES (?,?,?,?,?)",
                        (
                            self.target,
                            source,
                            identity["producer_id"],
                            identity["article_id"],
                            package,
                        ),
                    )
        return count
