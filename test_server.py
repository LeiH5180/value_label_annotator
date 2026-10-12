"""Persistence and alignment checks use disposable datasets, never the real data."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pyarrow as pa
import pyarrow.parquet as pq

from server import APIError, Handler, Store, ThreadingHTTPServer


def make_dataset(root, name="fixture", labels=True, episodes=1):
    ds = root / name
    (ds / "meta").mkdir(parents=True)
    info = {"fps": 30, "chunks_size": 1000, "total_episodes": episodes,
            "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
            "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
            "features": {"observation.images.cam_left_high": {}}}
    (ds / "meta/info.json").write_text(json.dumps(info))
    rows = [{"episode_index": ep, "tasks": ["test"], "length": 43, "collection_category": "dagger"} for ep in range(episodes)]
    (ds / "meta/episodes.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    for ep in range(episodes):
        # Retained actions map to original frames 0, 4, 8, ... rather than 0, 1, 2.
        fields = {"index": list(range(ep * 43, (ep + 1) * 43)), "episode_index": [ep] * 43,
                  "frame_index": [4 * i for i in range(43)], "timestamp": [4 * i / 30 for i in range(43)],
                  "action": [[0.0]] * 43}
        file = ds / "data/chunk-000" / f"episode_{ep:06d}.parquet"
        file.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table(fields), file)
        if labels:
            raw = {k: fields[k] for k in ["index", "episode_index", "frame_index"]}
            raw.update({"valid_action_count": [min(20, 43 - i) for i in range(43)],
                        "is_partial_action_chunk": [i > 23 for i in range(43)],
                        "value_delta": [0.01 * (i + 1) for i in range(43)]})
            file = ds / "value_labels/model/episodes" / f"episode_{ep:06d}.parquet"
            file.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(pa.table(raw).replace_schema_metadata({b"original": b"keep"}), file)
    if labels:
        (ds / "value_labels/model/manifest.json").write_text(json.dumps({"chunk_horizon": 20, "value_checkpoint": "keep"}))
    return ds


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root, self.root / "cache")

    def tearDown(self):
        self.store.pool.shutdown(wait=True)
        self.tmp.cleanup()

    def open(self, ds):
        return self.store.session(self.store.open(str(ds))["session"])

    def test_filtered_actions_alignment_and_partial_chunk(self):
        sess = self.open(make_dataset(self.root))
        e = self.store.episode(sess, 0)
        self.assertEqual(e["frame_indices"][1], 4)
        self.assertAlmostEqual(e["timestamps"][1], 4 / 30)
        self.assertEqual([c["start_step"] for c in e["chunks"]], [0, 20, 40])
        self.assertEqual([c["start_frame_index"] for c in e["chunks"]], [0, 80, 160])
        self.assertEqual([c["valid_action_count"] for c in e["chunks"]], [20, 20, 3])
        self.assertAlmostEqual(e["boundary_times"][1], 80 / 30)
        for value, expected in zip(e["points"], [0, .01, .22, .63]):
            self.assertAlmostEqual(value, expected)

    def test_restore_is_draft_until_explicit_save(self):
        sess = self.open(make_dataset(self.root))
        path = sess.manual_path(0)
        original = path.read_bytes()
        old = pq.read_table(path)
        e = self.store.episode(sess, 0)
        points = list(e["points"]); points[1] = .15
        e = self.store.save(sess, 0, {"revision": e["revision"], "points": points, "annotated": [True, False, False]})
        new = pq.read_table(path)
        self.assertAlmostEqual(new["value_delta"][0].as_py(), .15)
        self.assertAlmostEqual(new["value_delta"][20].as_py(), .07)
        self.assertEqual(new["value_delta"][40].as_py(), old["value_delta"][40].as_py())
        for i in range(43):
            if i not in (0, 20):
                self.assertEqual(new["value_delta"][i].as_py(), old["value_delta"][i].as_py())
        for column in old.column_names:
            if column != "value_delta":
                self.assertTrue(old[column].equals(new[column]))
        self.assertEqual(new.schema.metadata[b"original"], b"keep")
        self.assertEqual(e["annotated"], [True, True, True])
        self.assertAlmostEqual(e["points"][-1], .63)
        manifest = json.loads((path.parent.parent / "manifest.json").read_text())
        self.assertEqual(manifest["value_checkpoint"], "keep")
        self.assertEqual(manifest["num_labeled_samples"], 43)
        saved_bytes = path.read_bytes()
        sidecars = {p: p.read_bytes() for p in path.parent.parent.glob("*.json")}
        restored = self.store.restore(sess, 0, {"revision": e["revision"]})
        self.assertEqual(restored["points"], e["baseline_points"])
        self.assertEqual(restored["revision"], e["revision"])
        self.assertTrue(restored["modified"])
        self.assertEqual(path.read_bytes(), saved_bytes)
        self.assertEqual({p: p.read_bytes() for p in sidecars}, sidecars)
        self.assertEqual(self.store.episode(self.open(sess.path), 0)["points"], e["points"])
        committed = self.store.save(sess, 0, {"revision": restored["revision"], "points": restored["points"], "annotated": restored["annotated"]})
        self.assertEqual(committed["points"], e["baseline_points"])
        self.assertTrue(pq.read_table(path)["value_delta"].equals(old["value_delta"]))

    def test_restore_missing_baseline_keeps_saved_labels_until_save(self):
        ds = make_dataset(self.root, labels=False)
        sess = self.open(ds)
        e = self.store.episode(sess, 0)
        self.assertEqual(e["reference_source"], "linear")
        self.assertEqual(e["points"][0], 0)
        self.assertEqual(e["points"][-1], 1)
        self.assertAlmostEqual(e["points"][1], e["boundary_times"][1] / e["duration"])
        points = list(e["points"]); points[2] = .8
        saved = self.store.save(sess, 0, {"revision": e["revision"], "points": points, "annotated": [False, True, False]})
        table = pq.read_table(sess.manual_path(0))
        self.assertEqual(len(table), 3)
        self.assertEqual(table["valid_action_count"].to_pylist(), [20, 20, 3])
        self.assertEqual(table["human_annotated"].to_pylist(), [True, True, True])
        for row in table.to_pylist():
            self.assertAlmostEqual(row["value_delta"], row["progress_end"] - row["progress_start"])
        reopened = self.store.episode(self.open(ds), 0)
        self.assertEqual(reopened["points"], saved["points"])
        restored = self.store.restore(sess, 0, {"revision": saved["revision"]})
        self.assertTrue(sess.manual_path(0).exists())
        self.assertEqual(restored["points"], e["points"])
        self.assertEqual(self.store.episode(sess, 0)["points"], saved["points"])
        self.assertFalse((sess.manual_path(0).parent.parent / ".annotation_initial").exists())

    def test_snapshot_captured_on_first_episode_visit_and_conflicts(self):
        ds = make_dataset(self.root, episodes=2)
        a, b = self.open(ds), self.open(ds)
        # Opening a dataset must not capture unvisited episode labels.
        path = a.manual_path(1)
        table = pq.read_table(path)
        values = table["value_delta"].to_pylist(); values[0] = .12
        pq.write_table(table.set_column(table.schema.get_field_index("value_delta"), "value_delta", pa.array(values)), path)
        e = self.store.episode(b, 1)
        self.assertAlmostEqual(e["baseline_points"][1], .12)
        points = list(e["points"]); points[2] = .5
        saved = self.store.save(b, 1, {"revision": e["revision"], "points": points, "annotated": [False, True, False]})
        with self.assertRaises(APIError) as caught:
            self.store.save(a, 1, {"revision": e["revision"], "points": points, "annotated": [False, True, False]})
        self.assertEqual(caught.exception.status, 409)
        first_visit = self.store.episode(a, 1)
        self.assertNotEqual(first_visit["points"], first_visit["baseline_points"])
        before = a.manual_path(1).read_bytes()
        restored = self.store.restore(a, 1, {"revision": saved["revision"]})
        self.assertEqual(restored["points"], e["points"])
        self.assertEqual(a.manual_path(1).read_bytes(), before)
        with self.assertRaises(APIError) as caught:
            self.store.restore(a, 1, {"revision": e["revision"]})
        self.assertEqual(caught.exception.status, 409)

    def test_save_without_numeric_changes_marks_modified_and_persists(self):
        ds = make_dataset(self.root)
        sess = self.open(ds)
        original = sess.manual_path(0).read_bytes()
        e = self.store.episode(sess, 0)
        self.assertFalse(e["modified"])
        saved = self.store.save(sess, 0, {"revision": e["revision"], "points": e["points"], "annotated": e["annotated"]})
        self.assertTrue(saved["modified"])
        self.assertEqual(saved["annotated"], [True] * 3)
        table = pq.read_table(sess.manual_path(0))
        self.assertEqual([i for i, flag in enumerate(table["human_annotated"].to_pylist()) if flag], [0, 20, 40])
        self.assertTrue(table["value_delta"].equals(pq.read_table(pa.BufferReader(original))["value_delta"]))
        self.assertEqual(json.loads((sess.manual_path(0).parent.parent / "manifest.json").read_text())["num_human_annotated_chunks"], 3)
        self.assertTrue(self.store.episode_list(sess)[0]["modified"])
        another = Store(self.root, self.root / "cache2")
        try:
            reopened = another.session(another.open(str(ds))["session"])
            current = another.episode(reopened, 0)
            self.assertTrue(current["modified"])
            self.assertEqual(current["annotated"], [True] * 3)
            restored = another.restore(reopened, 0, {"revision": current["revision"]})
            self.assertTrue(restored["modified"])
            self.assertEqual(restored["points"], current["points"])
            self.assertEqual(restored["annotated"], current["annotated"])
            self.assertTrue(self.store.episode_list(sess)[0]["modified"])
            again = another.save(reopened, 0, {"revision": restored["revision"], "points": restored["points"], "annotated": restored["annotated"]})
            self.assertTrue(again["modified"])
            self.assertEqual(again["annotated"], [True] * 3)
        finally:
            another.pool.shutdown(wait=True)

    def test_save_unchanged_chunk_labels_confirms_entire_episode(self):
        ds = make_dataset(self.root, labels=False)
        sess = self.open(ds)
        initial = self.store.episode(sess, 0)
        saved = self.store.save(sess, 0, {"revision": initial["revision"], "points": initial["points"], "annotated": [False] * 3})
        path = sess.manual_path(0)
        table = pq.read_table(path)
        self.assertEqual(saved["points"], initial["points"])
        self.assertEqual(saved["annotated"], [True] * 3)
        self.assertEqual(table["human_annotated"].to_pylist(), [True] * 3)
        self.assertEqual(table.schema.metadata[b"annotation_human_confirmation_scope"], b"all_episode_chunks")
        # Saving an existing chunk file without numeric edits also confirms every row.
        old_deltas = table["value_delta"]
        table = table.set_column(table.schema.get_field_index("human_annotated"), "human_annotated", pa.array([False] * 3))
        pq.write_table(table, path)
        current = self.store.episode(sess, 0)
        again = self.store.save(sess, 0, {"revision": current["revision"], "points": current["points"], "annotated": [False] * 3})
        self.assertEqual(again["annotated"], [True] * 3)
        self.assertTrue(pq.read_table(path)["value_delta"].equals(old_deltas))
        manifest = json.loads((path.parent.parent / "manifest.json").read_text())
        self.assertEqual(manifest["num_human_annotated_chunks"], 3)
        self.assertEqual(manifest["evaluation_filter"], "human_annotated == true")

    def test_initial_values_survive_reopening_after_numeric_edit(self):
        ds = make_dataset(self.root, labels=False)
        sess = self.open(ds)
        initial = self.store.episode(sess, 0)
        points = list(initial["points"]); points[1] += .1; points[2] += .1; points[3] += .1
        saved = self.store.save(sess, 0, {"revision": initial["revision"], "points": points, "annotated": [True] * 3})
        self.assertAlmostEqual(saved["total_progress"], 1.1)
        reopened = self.open(ds)
        current = self.store.episode(reopened, 0)
        self.assertEqual(current["baseline_points"], initial["points"])
        self.assertEqual(current["points"], saved["points"])
        restored = self.store.restore(reopened, 0, {"revision": current["revision"]})
        self.assertTrue(restored["modified"])
        self.assertTrue(reopened.manual_path(0).exists())
        self.assertEqual(restored["points"], initial["points"])
        self.assertEqual(self.store.episode(reopened, 0)["points"], saved["points"])
        # A new server run captures saved labels, ignoring legacy disk baselines.
        marker = reopened.manual_path(0).parent.parent / ".annotation_initial/episode_000000.json"
        marker.parent.mkdir(); marker.write_text('{"original_exists": false}')
        another = Store(self.root, self.root / "restart-cache")
        try:
            new_session = another.session(another.open(str(ds))["session"])
            current = another.episode(new_session, 0)
            self.assertEqual(current["baseline_points"], saved["points"])
            self.assertEqual(another.restore(new_session, 0, {"revision": current["revision"]})["points"], saved["points"])
        finally:
            another.pool.shutdown(wait=True)

    def test_restore_unlabeled_episode_writes_nothing(self):
        sess = self.open(make_dataset(self.root, labels=False))
        initial = self.store.episode(sess, 0)
        before = {p: p.read_bytes() for p in sess.path.rglob("*") if p.is_file()}
        restored = self.store.restore(sess, 0, {"revision": initial["revision"]})
        self.assertEqual(restored["points"], initial["points"])
        self.assertEqual(restored["revision"], "missing")
        self.assertEqual(before, {p: p.read_bytes() for p in sess.path.rglob("*") if p.is_file()})

    def test_bad_values_and_wrong_alignment_do_not_write(self):
        sess = self.open(make_dataset(self.root))
        original = sess.manual_path(0).read_bytes()
        e = self.store.episode(sess, 0)
        for points in [[0, float("nan"), .2, .3], [0, float("inf"), .2, .3], [0, .2], [.1, .2, .3, .4]]:
            with self.assertRaises(APIError):
                self.store.save(sess, 0, {"revision": e["revision"], "points": points, "annotated": [True] * 3})
        self.assertEqual(sess.manual_path(0).read_bytes(), original)
        bad = make_dataset(self.root, name="bad")
        file = bad / "value_labels/model/episodes/episode_000000.parquet"
        t = pq.read_table(file); t = t.set_column(t.schema.get_field_index("frame_index"), "frame_index", pa.array(list(range(43))))
        pq.write_table(t, file)
        with self.assertRaises(APIError):
            self.store.episode(self.open(bad), 0)


class HTTPTests(unittest.TestCase):
    def test_video_range_and_cross_origin_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); ds = make_dataset(root)
            store = Store(root, root / "cache")
            session = store.open(str(ds))
            token = session["session"]
            video = root / "mock.mp4"; video.write_bytes(bytes(range(256)) * 4)
            store.video = lambda *args: {"state": "ready", "path": video}
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler); server.store = store
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            host = f"http://127.0.0.1:{server.server_port}"
            url = host + f"/api/session/{token}/episode/0/video/0"
            try:
                for requested, expected in [("bytes=10-29", bytes(range(10, 30))), ("bytes=-10", bytes(range(246, 256)))]:
                    with urlopen(Request(url, headers={"Range": requested})) as response:
                        self.assertEqual(response.status, 206); self.assertEqual(response.read(), expected)
                with self.assertRaises(HTTPError) as caught:
                    urlopen(Request(url, headers={"Range": "bytes=4096-"}))
                self.assertEqual(caught.exception.code, 416)
                request = Request(host + "/api/open", data=json.dumps({"path": str(ds)}).encode(),
                                  headers={"Content-Type": "application/json", "Origin": "https://foreign.example"})
                with self.assertRaises(HTTPError) as caught:
                    urlopen(request)
                self.assertEqual(caught.exception.code, 403)
            finally:
                server.shutdown(); server.server_close(); store.pool.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
