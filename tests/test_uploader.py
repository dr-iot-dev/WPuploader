"""Independent file import, concurrency and failure/reconciliation behavior."""

import copy
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image

from wpuploader.models import WordPressPayloadV1
from wpuploader.protocol import (
    ProtocolError,
    atomic_write,
    bundle_bytes,
    digest,
    object_hash,
    package_identity,
    process_lock,
    source_hash,
)
from wpuploader.service import Uploader, remote_hash
from wpuploader.settings import Settings
from wpuploader.store import Store
from wpuploader.wordpress import WordPressClient


class CMS:
    def __init__(self):
        self.posts, self.media, self.assets = {}, {}, {}
        self.creates = self.uploads = self.replacements = self.attachments = 0
        self.lose_create = self.lose_media = self.lose_attachment = self.lose_replacement = False

    def find(self, slug):
        found = [post for post in self.posts.values() if post["slug"] == slug]
        if len(found) > 1:
            raise ProtocolError("WORDPRESS_RECONCILE_CONFLICT")
        return copy.deepcopy(found[0]) if found else None

    def get(self, post_id):
        return copy.deepcopy(self.posts[str(post_id)])

    def create_draft(self, payload):
        self.creates += 1
        post = self.post(payload.model_dump(mode="json"), self.creates)
        self.posts[str(post["id"])] = post
        if self.lose_create:
            self.lose_create = False
            raise TimeoutError()
        return copy.deepcopy(post)

    def post(self, value, post_id):
        return {
            "id": post_id,
            "type": "news_weave",
            "link": f"https://cms.example/{post_id}",
            "status": "draft",
            "slug": value["slug"],
            "modified_gmt": f"version-{post_id}",
            "title": {"raw": value["title"]},
            "content": {"raw": value["content"]},
            "excerpt": {"raw": value["excerpt"]},
            "categories": value["categories"],
            "tags": value["tags"],
            "featured_media": value.get("featured_media", 0),
        }

    def find_media(self, slug):
        matches = [media for media in self.media.values() if media["slug"] == slug]
        return copy.deepcopy(matches[0]) if matches else None

    def get_media(self, media_id):
        return copy.deepcopy(self.media[str(media_id)])

    def get_media_bytes(self, url):
        return self.assets[url]

    def upload_image(self, data, **kwargs):
        self.uploads += 1
        with Image.open(BytesIO(data)) as image:
            size = image.size
        url = f"https://cms.example/media/{self.uploads}.jpg"
        media = {
            "id": self.uploads,
            "slug": kwargs["slug"],
            "mime_type": "image/jpeg",
            "status": "inherit",
            "source_url": url,
            "media_details": {"width": size[0], "height": size[1]},
        }
        self.media[str(self.uploads)], self.assets[url] = media, data
        if self.lose_media:
            self.lose_media = False
            raise TimeoutError()
        return copy.deepcopy(media)

    def set_featured_media(self, post_id, media_id):
        self.attachments += 1
        post = self.posts[str(post_id)]
        post["featured_media"] = int(media_id)
        post["modified_gmt"] = f"attachment-{self.attachments}"
        if self.lose_attachment:
            self.lose_attachment = False
            raise TimeoutError()
        return copy.deepcopy(post)

    def replace_draft(self, post_id, payload):
        self.replacements += 1
        self.posts[post_id]
        new = self.post(payload.model_dump(mode="json"), int(post_id))
        new["modified_gmt"] = f"replacement-{self.replacements}"
        self.posts[post_id] = new
        if self.lose_replacement:
            self.lose_replacement = False
            raise TimeoutError()
        return copy.deepcopy(new)


def draft(path, *, producer=None, article=None, revision=1, image=False, title="Original"):
    meta = {
        "producer_id": producer or str(uuid4()),
        "article_id": article or str(uuid4()),
        "draft_id": str(uuid4()),
        "revision": revision,
        "artifact_revision": 1,
        "created_at": "2026-10-12T00:00:00Z",
        "producer_version": "1.0.0",
        "title": title,
        "excerpt": "Summary",
        "category_keys": ["iot_platform"],
        "tag_keys": [],
        "source_urls": [f"https://vendor.example/{i}" for i in range(3)],
        "source_normalization_version": "source-set-v1",
        "featured_image": None,
        "image_skip_reason": "disabled",
    }
    files = {"article.md": b"Own article", "article.html": ("<p>" + title + "</p>").encode()}
    meta["eligibility"] = {
        "verification": "pass",
        "mode": "production",
        "review_required": False,
        "review": None,
        "html_hash": digest(files["article.html"]),
        "markdown_hash": digest(files["article.md"]),
    }
    if image:
        stream = BytesIO()
        Image.new("RGB", (12, 8), "blue").save(stream, format="JPEG")
        files["assets/featured.jpg"] = stream.getvalue()
        meta["featured_image"] = {
            "path": "assets/featured.jpg",
            "mime": "image/jpeg",
            "width": 12,
            "height": 8,
            "alt": title,
            "caption": "Conceptual illustration",
            "model": "fixture",
        }
        meta["image_skip_reason"] = None
    meta["source_set_hash"] = source_hash(meta["source_urls"])
    meta["package_id"] = package_identity(meta)
    atomic_write(path, bundle_bytes(meta, files, draft=True))
    return meta


def uploader(tmp_path, cms=None, **settings):
    config = Settings(
        state_dir=tmp_path / "state",
        enabled=True,
        base_url="https://cms.example",
        category_map={"iot_platform": 3},
        **settings,
    )
    return Uploader(Store(config.state_dir, initialize=True), config, cms or CMS())


def test_same_file_and_repeated_runs_create_only_one_post(tmp_path):
    client = CMS()
    app = uploader(tmp_path, client)
    draft(tmp_path / "draft.nwdraft")
    for _ in range(3):
        app.import_file(tmp_path / "draft.nwdraft")
        app.run_once()
    assert client.creates == 1 and app.store.status()[0]["state"] == "completed"


def test_lost_create_response_reconciles_without_reposting(tmp_path):
    client = CMS()
    client.lose_create = True
    app = uploader(tmp_path, client)
    draft(tmp_path / "draft.nwdraft")
    app.import_file(tmp_path / "draft.nwdraft")
    app.run_once()
    assert app.store.status()[0]["state"] == "uncertain"
    app.run_once()
    assert client.creates == 1 and app.store.status()[0]["state"] == "completed"


def test_commit_send_gap_never_blindly_replays(tmp_path):
    client = CMS()
    app = uploader(tmp_path, client)
    meta = draft(tmp_path / "draft.nwdraft")
    app.import_file(tmp_path / "draft.nwdraft")
    app.prepare()
    with app.store.transaction() as db:
        db.execute("UPDATE deliveries SET state='creating'")
    app.run_once()
    assert client.creates == 0 and app.store.status()[0]["state"] == "uncertain"
    app.resolve(meta["package_id"], confirmed_not_created=True)
    app.run_once()
    assert client.creates == 1


def test_unknown_operation_does_not_block_unrelated_article(tmp_path):
    app = uploader(tmp_path)
    draft(tmp_path / "first.nwdraft")
    first = app.import_file(tmp_path / "first.nwdraft")
    app.prepare()
    with app.store.transaction() as db:
        db.execute("UPDATE deliveries SET state='creating' WHERE package_id=?", (first,))
    draft(tmp_path / "second.nwdraft")
    # Distinct source set, not another article from the same sources.
    from wpuploader.protocol import read_bundle

    manifest, files = read_bundle(tmp_path / "second.nwdraft", draft=True)
    manifest["source_urls"] = [f"https://other.example/{i}" for i in range(3)]
    manifest["source_set_hash"] = source_hash(manifest["source_urls"])
    atomic_write(tmp_path / "second.nwdraft", bundle_bytes(manifest, files, draft=True))
    second = app.import_file(tmp_path / "second.nwdraft")
    app.run_once()
    states = {row["package_id"]: row["state"] for row in app.store.status()}
    assert states[first] == "uncertain" and states[second] == "completed"


def test_bad_mapping_is_blocked_locally_without_remote_write(tmp_path):
    app = uploader(tmp_path)
    draft(tmp_path / "draft.nwdraft")
    app.import_file(tmp_path / "draft.nwdraft")
    object.__setattr__(app.settings, "category_map", {})
    app.run_once()
    assert app.client().creates == 0 and app.store.status()[0]["state"] == "blocked"


def test_source_set_duplicate_is_not_posted(tmp_path):
    app = uploader(tmp_path)
    draft(tmp_path / "one.nwdraft")
    draft(tmp_path / "two.nwdraft")
    app.import_file(tmp_path / "one.nwdraft")
    app.run_once()
    app.import_file(tmp_path / "two.nwdraft")
    app.run_once()
    assert app.client().creates == 1
    assert any(row["error"] == "WORDPRESS_SOURCE_SET_DUPLICATE" for row in app.store.status())


def test_old_revision_cannot_roll_back_new_pending_revision(tmp_path):
    app = uploader(tmp_path)
    meta = draft(tmp_path / "new.nwdraft", revision=2)
    draft(
        tmp_path / "old.nwdraft",
        producer=meta["producer_id"],
        article=meta["article_id"],
        revision=1,
    )
    newer = app.import_file(tmp_path / "new.nwdraft")
    older = app.import_file(tmp_path / "old.nwdraft")
    app.run_once()
    states = {row["package_id"]: row["state"] for row in app.store.status()}
    assert states[newer] == "completed" and states[older] == "superseded"
    assert app.client().creates == 1


def test_conflicting_logical_package_does_not_replace_remote_post(tmp_path):
    app = uploader(tmp_path)
    meta = draft(tmp_path / "first.nwdraft")
    app.import_file(tmp_path / "first.nwdraft")
    app.run_once()
    draft(
        tmp_path / "changed.nwdraft",
        producer=meta["producer_id"],
        article=meta["article_id"],
        title="changed",
    )
    with pytest.raises(ProtocolError, match="PACKAGE_ID_CONFLICT"):
        app.import_file(tmp_path / "changed.nwdraft")
    assert app.client().creates == 1


@pytest.mark.parametrize("which", ["media", "attachment"])
def test_image_acknowledgement_loss_recovers_without_duplicate(tmp_path, which):
    client = CMS()
    setattr(client, "lose_" + which, True)
    app = uploader(tmp_path, client)
    draft(tmp_path / "draft.nwdraft", image=True)
    app.import_file(tmp_path / "draft.nwdraft")
    app.run_once()
    app.run_once()
    assert client.creates == 1 and client.uploads == 1
    assert app.store.status()[0]["state"] == "completed"


def test_manual_edit_is_not_overwritten_or_recreated(tmp_path):
    client = CMS()
    client.lose_create = True
    app = uploader(tmp_path, client)
    draft(tmp_path / "draft.nwdraft")
    app.import_file(tmp_path / "draft.nwdraft")
    app.run_once()
    client.posts["1"]["content"]["raw"] = "human edit"
    app.run_once()
    assert client.posts["1"]["content"]["raw"] == "human edit"
    assert client.creates == 1 and app.store.status()[0]["state"] == "conflict"


def test_explicit_replace_preserves_post_id_and_slug(tmp_path):
    app = uploader(tmp_path)
    meta = draft(tmp_path / "first.nwdraft")
    app.import_file(tmp_path / "first.nwdraft")
    app.run_once()
    old = app.store.status()[0]
    original_slug = app.client().posts["1"]["slug"]
    new = draft(
        tmp_path / "next.nwdraft",
        producer=meta["producer_id"],
        article=meta["article_id"],
        revision=2,
        title="Revised",
    )
    app.import_file(tmp_path / "next.nwdraft")
    app.run_once()
    assert app.client().creates == 1
    app.replace(new["package_id"], old["remote_hash"])
    post = app.client().posts["1"]
    assert post["slug"] == original_slug and post["title"]["raw"] == "Revised"
    assert app.client().creates == 1 and app.client().replacements == 1


def test_unchanged_result_notifications_are_republished_after_disconnect(tmp_path):
    transfer = tmp_path / "transfer"
    app = uploader(tmp_path, transfer_dir=transfer)
    draft(tmp_path / "draft.nwdraft")
    app.import_file(tmp_path / "draft.nwdraft")
    app.run_once()
    receipts = list((transfer / "receipts").rglob("*.json"))
    assert receipts
    assert all("password" not in path.read_text() for path in receipts)


def test_missing_database_requires_explicit_initialization(tmp_path):
    with pytest.raises(ProtocolError, match="STATE_INIT_REQUIRED"):
        Store(tmp_path / "missing")


def test_worker_lock_blocks_parallel_remote_sends(tmp_path):
    app = uploader(tmp_path)
    draft(tmp_path / "draft.nwdraft")
    app.import_file(tmp_path / "draft.nwdraft")
    with process_lock(app.store.directory / "worker.lock"):
        with pytest.raises(ProtocolError, match="WORKER_BUSY"):
            app.run_once()
    assert app.client().creates == 0


def test_known_history_is_adopted_without_new_post(tmp_path):
    app = uploader(tmp_path)
    client = app.client()
    payload = WordPressPayloadV1(
        title="Saved", content="<p>Saved</p>", excerpt="", slug="news-" + "0" * 64, categories=(3,)
    )
    post = client.create_draft(payload)
    producer, article = str(uuid4()), str(uuid4())
    record = {
        "target_id": app.target,
        "producer_id": producer,
        "article_id": article,
        "draft_id": str(uuid4()),
        "revision": 1,
        "artifact_revision": 1,
        "payload": payload.model_dump(mode="json"),
        "payload_hash": object_hash(payload.model_dump(mode="json")),
        "operation_key": "0" * 64,
        "remote_post_id": str(post["id"]),
        "remote_hash": remote_hash(post),
        "remote_status": "draft",
        "state": "drafted",
        "source_set_hash": source_hash(["https://vendor.example/saved"]),
    }
    import json

    path = tmp_path / "history.json"
    path.write_text(json.dumps({"schema_version": "1.0", "deliveries": [record]}))
    assert app.import_history(path) == 1
    assert app.import_history(path) == 0
    app.run_once(reconcile_only=True)
    assert client.creates == 1 and app.store.status()[0]["state"] == "completed"


def test_adapter_has_no_publish_or_trash_operations():
    assert not hasattr(WordPressClient, "publish")
    assert not hasattr(WordPressClient, "trash")


def test_old_post_type_is_not_supported(tmp_path):
    with pytest.raises(ProtocolError, match="WORDPRESS_POST_TYPE_UNSUPPORTED"):
        Settings(state_dir=tmp_path, post_type="nc_news", rest_base="nc-news")


def test_bad_shared_file_does_not_block_valid_file_and_partial_files_are_ignored(tmp_path):
    transfer = tmp_path / "transfer"
    ready = transfer / "ready"
    ready.mkdir(parents=True)
    (ready / "a-broken.nwdraft").write_bytes(b"corrupt")
    (ready / "half-written.part").write_bytes(b"partial")
    draft(ready / "z-valid.nwdraft")
    cms = CMS()
    app = uploader(tmp_path, cms, transfer_dir=transfer)
    assert len(app.run_once()) == 1
    assert cms.creates == 1
    assert app.store.status()[0]["state"] == "completed"


def test_read_only_reconcile_detects_human_publication_without_writes(tmp_path):
    cms = CMS()
    app = uploader(tmp_path, cms)
    draft(tmp_path / "draft.nwdraft")
    app.import_file(tmp_path / "draft.nwdraft")
    app.run_once()
    cms.posts["1"]["status"] = "publish"
    app.run_once(reconcile_only=True)
    assert cms.creates == 1 and cms.replacements == cms.uploads == cms.attachments == 0
    assert app.store.status()[0]["state"] == "conflict"


def test_unknown_operations_do_not_grow_events_when_state_is_unchanged(tmp_path):
    app = uploader(tmp_path)
    draft(tmp_path / "draft.nwdraft")
    app.import_file(tmp_path / "draft.nwdraft")
    app.prepare()
    app.store.db.execute("UPDATE deliveries SET state='creating'")
    app.run_once()
    events = app.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    app.run_once()
    assert app.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events


def test_recovery_keeps_late_arriving_files_get_only_until_explicit_resolution(tmp_path):
    cms = CMS()
    app = uploader(tmp_path, cms)
    app.prepare(recovery=True)
    meta = draft(tmp_path / "late.nwdraft")
    app.import_file(tmp_path / "late.nwdraft")
    app.run_once()
    assert cms.creates == 0 and app.store.status()[0]["state"] == "uncertain"
    with pytest.raises(ProtocolError, match="RECOVERY_RECONCILIATION_PENDING"):
        app.finish_recovery()
    app.resolve(meta["package_id"], confirmed_not_created=True)
    app.run_once()
    assert cms.creates == 1 and app.store.status()[0]["state"] == "completed"
    app.finish_recovery()


def test_media_send_gap_requires_explicit_not_created_resolution(tmp_path):
    cms = CMS()
    app = uploader(tmp_path, cms)
    meta = draft(tmp_path / "image.nwdraft", image=True)
    app.import_file(tmp_path / "image.nwdraft")
    app.prepare()

    def interrupted_upload(*args, **kwargs):
        raise TimeoutError()

    real_upload = cms.upload_image
    cms.upload_image = interrupted_upload
    app.run_once()
    cms.upload_image = real_upload
    app.run_once()
    assert cms.creates == 1 and cms.uploads == 0
    app.resolve_media(meta["package_id"], confirmed_not_created=True)
    app.run_once()
    assert cms.creates == 1 and cms.uploads == 1 and cms.attachments == 1
