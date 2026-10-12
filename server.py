"""Local chunk progress annotation server. No model inference or GPU required."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import mimetypes
import os
import re
import secrets
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pyarrow as pa
import pyarrow.parquet as pq

APP_DIR = Path(__file__).resolve().parent
HORIZON = 20
MANUAL_SET = "human_value_h20"
CAMERAS = [
    ("observation.images.cam_left_high", "固定相机"),
    ("observation.images.cam_left_wrist", "左腕相机"),
    ("observation.images.cam_right_wrist", "右腕相机"),
]


class APIError(Exception):
    def __init__(self, message, status=400):
        self.status = status
        super().__init__(message)


def revision(raw: bytes | None) -> str:
    return hashlib.sha256(raw).hexdigest() if raw is not None else "missing"


def read_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def atomic_write(path: Path, raw: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".annotator-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@dataclass
class Session:
    token: str
    path: Path
    info: dict
    episodes: dict[int, dict]
    source_set: str | None
    opened_at: str
    baseline: dict[int, bytes]
    revisions: dict[int, str]
    cache: dict[int, dict] = field(default_factory=dict)

    def manual_path(self, ep: int) -> Path:
        return self.path / "value_labels" / (self.source_set or MANUAL_SET) / "episodes" / f"episode_{ep:06d}.parquet"

    def data_path(self, ep: int) -> Path:
        size = int(self.info.get("chunks_size", 1000))
        template = self.info.get("data_path", "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet")
        meta = self.episodes[ep]
        return self.path / template.format(episode_chunk=ep // size, episode_index=ep,
                                           chunk_index=meta.get("data/chunk_index", ep // size),
                                           file_index=meta.get("data/file_index", 0))


class Store:
    def __init__(self, root: Path, cache_dir: Path):
        self.root = root.resolve()
        self.cache_dir = cache_dir.resolve()
        self.sessions: dict[str, Session] = {}
        self.lock = threading.RLock()
        self.video_jobs = {}
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="video")

    @staticmethod
    def labelsets(path):
        result = []
        for directory in sorted((path / "value_labels").glob("*")):
            if not directory.is_dir():
                continue
            if not any((directory / "episodes").glob("*.parquet")):
                continue
            manifest = directory / "manifest.json"
            meta = json.loads(manifest.read_text()) if manifest.exists() else {}
            result.append({"name": directory.name, "chunk_horizon": meta.get("chunk_horizon")})
        return result

    def datasets(self):
        paths = [self.root] + sorted(p for p in self.root.iterdir() if p.is_dir())
        result = []
        for path in paths:
            if (path / "meta/info.json").exists() and (path / "data").is_dir():
                try:
                    info = json.loads((path / "meta/info.json").read_text())
                    result.append({"path": str(path), "name": path.name,
                                   "episodes": info.get("total_episodes"), "labelsets": self.labelsets(path)})
                except (OSError, ValueError):
                    continue
        return result

    def open(self, path, source_set=None):
        path = Path(path).expanduser().resolve()
        if not (path / "meta/info.json").is_file() or not (path / "data").is_dir():
            raise APIError("请选择含 meta/info.json 和 data/ 的数据集目录。")
        info = json.loads((path / "meta/info.json").read_text())
        meta_file = path / "meta/episodes.jsonl"
        if meta_file.exists():
            rows = [json.loads(line) for line in meta_file.read_text().splitlines() if line.strip()]
        else:
            rows = []
            for f in sorted((path / "meta/episodes").rglob("*.parquet")):
                pf = pq.ParquetFile(f)
                columns = [name for name in pf.schema_arrow.names if not name.startswith("stats/")]
                rows.extend(pf.read(columns=columns).to_pylist())
        if not rows:
            raise APIError("没有找到 episode 元数据。")
        sets = self.labelsets(path)
        names = [s["name"] for s in sets]
        source_set = source_set or (names[0] if names else None)
        if source_set and source_set not in names:
            raise APIError("所选模型标签不存在。")
        for item in sets:
            if item["name"] == source_set and item["chunk_horizon"] not in (None, HORIZON):
                raise APIError("所选模型标签的 action horizon 不是 20，不能用于此标注器。")
        episodes = {int(r["episode_index"]): r for r in rows}
        # Capture selected label files NOW, rather than on the first episode visit.
        with self.lock:
            baseline = {}
            for f in (path / "value_labels" / (source_set or MANUAL_SET) / "episodes").glob("episode_*.parquet"):
                ep = int(f.stem.rsplit("_", 1)[1])
                if ep in episodes:
                    baseline[ep] = f.read_bytes()
            initial_dir = path / "value_labels" / (source_set or MANUAL_SET) / ".annotation_initial"
            for marker in initial_dir.glob("episode_*.json"):
                ep = int(marker.stem.rsplit("_", 1)[1])
                if ep not in episodes:
                    continue
                saved_initial = json.loads(marker.read_text())
                if saved_initial["original_exists"]:
                    baseline[ep] = marker.with_suffix(".parquet").read_bytes()
                else:
                    baseline.pop(ep, None)
            sess = Session(secrets.token_urlsafe(24), path, info, episodes, source_set,
                           datetime.now(timezone.utc).isoformat(), baseline,
                           {ep: revision(baseline.get(ep)) for ep in episodes})
            self.sessions[sess.token] = sess
        return {"session": sess.token, "dataset_name": path.name, "dataset_path": str(path),
                "source_set": source_set, "label_path": str(path / "value_labels" / (source_set or MANUAL_SET)),
                "episodes": self.episode_list(sess), "chunk_horizon": HORIZON}

    def session(self, token):
        try:
            return self.sessions[token]
        except KeyError:
            raise APIError("会话不存在，请重新打开数据集。", 404)

    def episode_list(self, sess):
        states = self.annotation_states(sess)
        result = []
        for ep, meta in sorted(sess.episodes.items()):
            raw = read_bytes(sess.manual_path(ep))
            annotated = 0
            if raw is not None:
                table = pq.read_table(pa.BufferReader(raw))
                if "human_annotated" in table.column_names:
                    annotated = sum(bool(v) for v in table["human_annotated"].to_pylist())
            modified = states.get(str(ep), {}).get("modified", annotated > 0)
            result.append({"episode_index": ep, "category": meta.get("collection_category", "unknown"),
                           "length": meta.get("length"), "has_labels": raw is not None,
                           "annotated_chunks": annotated, "modified": bool(modified)})
        return result

    @staticmethod
    def annotation_states(sess):
        file = sess.manual_path(0).parent.parent / "annotation_state.json"
        return json.loads(file.read_text()).get("episodes", {}) if file.exists() else {}

    def set_modified(self, sess, ep, modified):
        file = sess.manual_path(ep).parent.parent / "annotation_state.json"
        episodes = self.annotation_states(sess)
        episodes[str(ep)] = {"modified": modified, "updated_at": datetime.now(timezone.utc).isoformat()}
        atomic_write(file, json.dumps({"schema_version": 1, "episodes": episodes}, ensure_ascii=False, indent=2).encode())

    @staticmethod
    def capture_initial(sess, ep):
        """Retain the first annotation baseline across reopening and server restarts."""
        marker = sess.manual_path(ep).parent.parent / ".annotation_initial" / f"episode_{ep:06d}.json"
        if marker.exists():
            return
        raw = sess.baseline.get(ep)
        if raw is not None:
            atomic_write(marker.with_suffix(".parquet"), raw)
        atomic_write(marker, json.dumps({"original_exists": raw is not None, "captured_at": sess.opened_at}).encode())

    def base_episode(self, sess, ep):
        if ep not in sess.episodes:
            raise APIError("Episode 不存在。", 404)
        if ep in sess.cache:
            return sess.cache[ep]
        data = pq.read_table(sess.data_path(ep), columns=["index", "episode_index", "frame_index", "timestamp"])
        if "data/file_index" in sess.episodes[ep]:
            import pyarrow.compute as pc
            data = data.filter(pc.equal(data["episode_index"], ep))
        d = data.to_pydict()
        n = len(data)
        if not n or any(e != ep for e in d["episode_index"]):
            raise APIError("Episode 数据为空或 episode_index 不匹配。")
        stamps = [float(v) for v in d["timestamp"]]
        if any(not math.isfinite(v) for v in stamps) or any(b <= a for a, b in zip(stamps, stamps[1:])):
            raise APIError("时间戳必须有限且严格递增。")
        fps = float(sess.info.get("fps", 30))
        duration = stamps[-1] + 1 / fps
        starts = list(range(0, n, HORIZON))
        boundaries = [stamps[i] for i in starts] + [duration]
        deltas = [None] * len(starts)
        source = "linear"
        length = duration - boundaries[0]
        points = [(t - boundaries[0]) / length for t in boundaries]
        chunks = []
        for k, start in enumerate(starts):
            end = min(start + HORIZON, n)
            chunks.append({"chunk_index": k, "start_step": start, "end_step_exclusive": end,
                           "start_index": int(d["index"][start]), "last_index": int(d["index"][end - 1]),
                           "start_frame_index": int(d["frame_index"][start]),
                           "last_frame_index": int(d["frame_index"][end - 1]),
                           "start_timestamp": boundaries[k], "end_timestamp": boundaries[k + 1],
                           "valid_action_count": end - start, "model_value_delta": deltas[k]})
        base = {"episode_index": ep, "category": sess.episodes[ep].get("collection_category", "unknown"),
                "tasks": sess.episodes[ep].get("tasks", []), "n_steps": n, "fps": fps,
                "duration": duration, "timestamps": stamps, "frame_indices": d["frame_index"],
                "indices": d["index"],
                "chunks": chunks, "boundary_times": boundaries, "reference_points": points,
                "reference_source": source}
        if sess.baseline.get(ep) is not None:
            points, _ = self.points_from_manual(sess.baseline[ep], base)
            base["reference_points"] = points
            base["reference_source"] = "existing"
            for k, chunk in enumerate(chunks):
                chunk["model_value_delta"] = points[k + 1] - points[k]
        sess.cache[ep] = base
        return base

    def points_from_manual(self, raw, base):
        if raw is None:
            return list(base["reference_points"]), [False] * len(base["chunks"])
        table = pq.read_table(pa.BufferReader(raw))
        if "value_delta" not in table.column_names:
            raise APIError("标签文件缺少 value_delta。")
        chunk_level = "chunk_index" in table.column_names
        if chunk_level:
            if len(table) != len(base["chunks"]):
                raise APIError("标签的 chunk 数量与当前 data 不匹配。")
            rows = table.to_pylist()
            for row, chunk in zip(rows, base["chunks"]):
                for key in ["episode_index", "chunk_index", "start_step", "end_step_exclusive", "start_index", "last_index", "start_frame_index", "last_frame_index"]:
                    expected = base["episode_index"] if key == "episode_index" else chunk[key]
                    if row.get(key) != expected:
                        raise APIError("标签的索引与当前 data 不匹配。")
            deltas = table["value_delta"].to_pylist()
            flags = table["human_annotated"].to_pylist() if "human_annotated" in table.column_names else [False] * len(rows)
            start_progress = float(rows[0].get("progress_start", 0))
        else:
            if len(table) != base["n_steps"] or any(
                table[k].to_pylist() != expected
                for k, expected in [("index", base["indices"]), ("episode_index", [base["episode_index"]] * base["n_steps"]), ("frame_index", base["frame_indices"])]
            ):
                raise APIError("标签与 data 的行或索引不匹配。")
            values = table["value_delta"].to_pylist()
            markers = table["human_annotated"].to_pylist() if "human_annotated" in table.column_names else [False] * len(table)
            deltas = [values[c["start_step"]] for c in base["chunks"]]
            flags = [bool(markers[c["start_step"]]) for c in base["chunks"]]
            start_progress = 0.0
        if any(v is None or not math.isfinite(v) for v in deltas):
            raise APIError("Chunk 增量含缺失值或非有限值。")
        points = [start_progress]
        for delta in deltas:
            points.append(points[-1] + float(delta))
        if any(not math.isfinite(v) for v in points):
            raise APIError("进度含非有限值。")
        return points, [bool(v) for v in flags]

    def episode(self, sess, ep):
        with self.lock:
            base = self.base_episode(sess, ep)
            raw = read_bytes(sess.manual_path(ep))
            points, flags = self.points_from_manual(raw, base)
            baseline_points, baseline_flags = self.points_from_manual(sess.baseline.get(ep), base)
            return {**base, "points": points, "annotated": flags,
                    "baseline_points": baseline_points, "baseline_annotated": baseline_flags,
                    "has_labels": raw is not None, "revision": revision(raw),
                    "modified": bool(self.annotation_states(sess).get(str(ep), {}).get("modified", any(flags))),
                    "total_progress": points[-1],
                    "label_file": str(sess.manual_path(ep))}

    def save(self, sess, ep, payload):
        with self.lock:
            base = self.base_episode(sess, ep)
            path = sess.manual_path(ep)
            before = read_bytes(path)
            if payload.get("revision") != revision(before):
                raise APIError("此 episode 已被其他页面保存，请重新载入后再修改。", 409)
            old_points, _ = self.points_from_manual(before, base)
            points = payload.get("points")
            flags = payload.get("annotated")
            if not isinstance(points, list) or len(points) != len(base["chunks"]) + 1:
                raise APIError("进度点数量不正确。")
            if any(type(v) not in (int, float) or not math.isfinite(v) for v in points):
                raise APIError("进度值必须是有限数值。")
            if any(not math.isfinite(points[k + 1] - points[k]) for k in range(len(points) - 1)):
                raise APIError("进度差值超出可保存的数值范围。")
            if not isinstance(flags, list) or len(flags) != len(base["chunks"]) or any(type(f) is not bool for f in flags):
                raise APIError("人工修改标记不正确。")
            if points[0] != old_points[0]:
                raise APIError("Episode 起点保持打开时的进度值，只能修改 chunk 结束进度。")
            # Saving confirms every chunk in this episode, including unchanged values.
            flags = [True] * len(base["chunks"])
            now = datetime.now(timezone.utc).isoformat()
            rows = [{"episode_index": ep, **c, "progress_start": float(points[k]),
                     "progress_end": float(points[k + 1]), "progress_delta": float(points[k + 1] - points[k]),
                     "value_delta": float(points[k + 1] - points[k]),
                     "human_annotated": flags[k]} for k, c in enumerate(base["chunks"])]
            # Explicit schema ensures all-null model columns still have a stable float type.
            int_keys = ["episode_index", "chunk_index", "start_step", "end_step_exclusive", "start_index",
                        "last_index", "start_frame_index", "last_frame_index", "valid_action_count"]
            float_keys = ["start_timestamp", "end_timestamp", "model_value_delta", "progress_start", "progress_end", "progress_delta", "value_delta"]
            if before is not None:
                table = pq.read_table(pa.BufferReader(before))
                chunk_level = "chunk_index" in table.column_names
                values = table["value_delta"].to_pylist()
                markers = table["human_annotated"].to_pylist() if "human_annotated" in table.column_names else [False] * len(table)
                for k, c in enumerate(base["chunks"]):
                    pos = k if chunk_level else c["start_step"]
                    changed = points[k] != old_points[k] or points[k + 1] != old_points[k + 1]
                    if changed:
                        values[pos] = points[k + 1] - points[k]
                    markers[pos] = True
                column = table.schema.field("value_delta")
                converted = pa.array(values, type=column.type)
                if any(not math.isfinite(converted[k if chunk_level else c["start_step"]].as_py())
                       for k, c in enumerate(base["chunks"])):
                    raise APIError("进度增量超出原标签数据类型的数值范围。")
                table = table.set_column(table.schema.get_field_index("value_delta"), column,
                                         converted)
                human_column = pa.array(markers, type=pa.bool_())
                if "human_annotated" in table.column_names:
                    table = table.set_column(table.schema.get_field_index("human_annotated"), "human_annotated", human_column)
                else:
                    table = table.append_column("human_annotated", human_column)
                if chunk_level:
                    for name, vals in [("progress_start", points[:-1]), ("progress_end", points[1:]),
                                       ("progress_delta", [points[k + 1] - points[k] for k in range(len(rows))])]:
                        array = pa.array(vals, type=pa.float64())
                        if name in table.column_names:
                            table = table.set_column(table.schema.get_field_index(name), name, array)
                        else:
                            table = table.append_column(name, array)
            else:
                schema = pa.schema([(k, pa.int64()) for k in int_keys] + [(k, pa.float64()) for k in float_keys] + [("human_annotated", pa.bool_())])
                table = pa.Table.from_pylist(rows, schema=schema)
            metadata = dict(table.schema.metadata or {})
            metadata.update({k.encode(): v.encode() for k, v in {
                "annotation_schema_version": "1", "annotation_chunk_horizon": "20",
                "annotation_updated_at": now, "annotation_session_opened_at": sess.opened_at,
                "annotation_episode_modified": "true",
                "annotation_human_confirmation_scope": "all_episode_chunks",
                "annotation_semantics": "episode save confirms all offset-zero 20-action chunks; value_delta=end-start"}.items()})
            table = table.replace_schema_metadata(metadata)
            out = pa.BufferOutputStream()
            pq.write_table(table, out, compression="zstd")
            self.capture_initial(sess, ep)
            atomic_write(path, out.getvalue().to_pybytes())
            sess.revisions[ep] = revision(path.read_bytes())
            self.set_modified(sess, ep, True)
            self.write_manifest(sess)
            return self.episode(sess, ep)

    def restore(self, sess, ep, payload):
        with self.lock:
            self.base_episode(sess, ep)
            path = sess.manual_path(ep)
            if payload.get("revision") != revision(read_bytes(path)):
                raise APIError("此 episode 已被其他页面保存，请重新载入后再还原。", 409)
            raw = sess.baseline.get(ep)
            self.capture_initial(sess, ep)
            if raw is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, raw)
            sess.revisions[ep] = revision(raw)
            self.set_modified(sess, ep, False)
            self.write_manifest(sess)
            return self.episode(sess, ep)

    def write_manifest(self, sess):
        dest = sess.path / "value_labels" / (sess.source_set or MANUAL_SET) / "manifest.json"
        manifest = json.loads(dest.read_text()) if dest.exists() else {"label_schema_version": 1, "chunk_horizon": HORIZON, "source_dataset": str(sess.path)}
        summaries = []
        human_count = 0
        for f in sorted(dest.parent.joinpath("episodes").glob("episode_*.parquet")):
            t = pq.read_table(f)
            vals = [v for v in t["value_delta"].to_pylist() if v is not None and math.isfinite(v)]
            ep = int(f.stem.rsplit("_", 1)[1])
            summaries.append({"episode_index": ep, "num_labels": len(t),
                              "value_delta_min": min(vals) if vals else None,
                              "value_delta_max": max(vals) if vals else None,
                              "value_delta_mean": sum(vals) / len(vals) if vals else None})
            if "human_annotated" in t.column_names:
                human_count += sum(bool(v) for v in t["human_annotated"].to_pylist())
        manifest.update({"episodes": summaries, "num_labeled_samples": sum(e["num_labels"] for e in summaries),
                         "eligible_episode_count": len(summaries), "eligible_episode_indices": [e["episode_index"] for e in summaries],
                         "annotation_updated_at": datetime.now(timezone.utc).isoformat(),
                         "num_human_annotated_chunks": human_count, "annotation_chunk_horizon": HORIZON})
        if not sess.source_set or sess.source_set == MANUAL_SET:
            manifest.update({"label_type": "human_chunk_progress", "evaluation_filter": "human_annotated == true"})
        atomic_write(dest, json.dumps(manifest, ensure_ascii=False, indent=2).encode())

    def video_source(self, sess, ep, camera):
        self.base_episode(sess, ep)
        if camera not in range(3):
            raise APIError("相机不存在。", 404)
        key = CAMERAS[camera][0]
        if camera == 0 and key not in sess.info.get("features", {}):
            key = "observation.images.cam_high"
        template = sess.info.get("video_path", "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4")
        return sess.path / template.format(episode_chunk=ep // int(sess.info.get("chunks_size", 1000)),
                                           episode_index=ep, video_key=key,
                                           chunk_index=sess.episodes[ep].get(f"videos/{key}/chunk_index", 0),
                                           file_index=sess.episodes[ep].get(f"videos/{key}/file_index", 0))

    def video(self, sess, ep, camera, video_format="mp4"):
        if video_format not in ("mp4", "webm"):
            raise APIError("不支持的视频格式。")
        source = self.video_source(sess, ep, camera)
        if not source.exists():
            return {"state": "error", "message": "此 episode 缺少该相机视频。"}
        stat = source.stat()
        key = CAMERAS[camera][0]
        if camera == 0 and key not in sess.info.get("features", {}):
            key = "observation.images.cam_high"
        start = sess.episodes[ep].get(f"videos/{key}/from_timestamp")
        end = sess.episodes[ep].get(f"videos/{key}/to_timestamp")
        cache_key = hashlib.sha256(f"{source.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:{start}:{end}".encode()).hexdigest()
        cache_key += "-" + video_format
        dest = self.cache_dir / "videos" / f"{cache_key}.{video_format}"
        with self.lock:
            if dest.is_file():
                return {"state": "ready", "path": dest}
            if cache_key not in self.video_jobs:
                self.video_jobs[cache_key] = self.pool.submit(self.transcode, source, dest, start, end)
            job = self.video_jobs[cache_key]
            if job.done():
                try:
                    job.result()
                except Exception as ex:
                    return {"state": "error", "message": str(ex)}
                return {"state": "ready", "path": dest}
        return {"state": "preparing"}

    @staticmethod
    def transcode(source, dest, start=None, end=None):
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp = dest.with_name(dest.stem + ".tmp" + dest.suffix)
        encoding = (["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "6", "-crf", "32", "-b:v", "0"]
                    if dest.suffix == ".webm" else
                    ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-movflags", "+faststart"])
        seek = ["-ss", str(float(start))] if start is not None else []
        length = ["-t", str(float(end) - float(start or 0))] if end is not None else []
        try:
            result = subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", *seek, "-i", str(source),
                                     *length, "-map", "0:v:0", "-an", *encoding,
                                     "-pix_fmt", "yuv420p", "-threads", "2", str(temp)], capture_output=True, text=True, timeout=300)
            if result.returncode != 0 or not temp.is_file():
                raise RuntimeError("视频转换失败：" + result.stderr[-350:])
            os.replace(temp, dest)
        finally:
            temp.unlink(missing_ok=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "ValueAnnotator/1"

    @property
    def store(self):
        return self.server.store

    def do_GET(self):
        self.handle_request()

    def do_HEAD(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def payload(self):
        # Only the local application may mutate dataset files.
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            raise APIError("不允许跨站写入。", 403)
        if "application/json" not in self.headers.get("Content-Type", ""):
            raise APIError("请求必须为 JSON。", 415)
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 1024 * 1024:
            raise APIError("请求大小不正确。")
        try:
            result = json.loads(self.rfile.read(length))
            if not isinstance(result, dict):
                raise APIError("请求 JSON 必须为对象。")
            return result
        except ValueError:
            raise APIError("请求 JSON 格式错误。")

    def handle_request(self):
        try:
            path = unquote(urlparse(self.path).path)
            if self.command in ("GET", "HEAD") and path == "/api/datasets":
                return self.json_response({"datasets": self.store.datasets(), "root": str(self.store.root), "annotation_ui_version": 3})
            if self.command == "GET" and path == "/api/inspect":
                p = parse_qs(urlparse(self.path).query).get("path", [""])[0]
                directory = Path(p).expanduser().resolve()
                if not (directory / "meta/info.json").is_file():
                    raise APIError("未找到该路径的 meta/info.json。")
                return self.json_response({"labelsets": self.store.labelsets(directory)})
            if self.command == "POST" and path == "/api/open":
                p = self.payload()
                return self.json_response(self.store.open(p.get("path", ""), p.get("source_set")))
            match = re.fullmatch(r"/api/session/([\w-]+)/(episodes|episode/\d+(?:/(save|restore|videos|video/[0-2]))?)", path)
            if match:
                token, route, _ = match.groups()
                sess = self.store.session(token)
                if route == "episodes" and self.command == "GET":
                    return self.json_response({"episodes": self.store.episode_list(sess)})
                parts = route.split("/")
                if len(parts) < 2:
                    raise APIError("接口不存在。", 404)
                ep = int(parts[1])
                action = parts[2] if len(parts) > 2 else "get"
                if action == "get" and self.command == "GET":
                    return self.json_response(self.store.episode(sess, ep))
                if action == "save" and self.command == "POST":
                    return self.json_response(self.store.save(sess, ep, self.payload()))
                if action == "restore" and self.command == "POST":
                    return self.json_response(self.store.restore(sess, ep, self.payload()))
                if action == "videos" and self.command == "GET":
                    video_format = parse_qs(urlparse(self.path).query).get("format", ["mp4"])[0]
                    results = []
                    for cam, (_, title) in enumerate(CAMERAS):
                        status = self.store.video(sess, ep, cam, video_format)
                        status.pop("path", None)
                        results.append({"camera": cam, "title": title, **status,
                                        "url": f"/api/session/{token}/episode/{ep}/video/{cam}?format={video_format}"})
                    return self.json_response({"videos": results})
                if action == "video" and self.command in ("GET", "HEAD"):
                    video_format = parse_qs(urlparse(self.path).query).get("format", ["mp4"])[0]
                    status = self.store.video(sess, ep, int(parts[3]), video_format)
                    if status["state"] != "ready":
                        raise APIError(status.get("message", "视频正在准备，请稍后再试。"), 503)
                    return self.file_response(status["path"])
            if self.command in ("GET", "HEAD") and not path.startswith("/api/"):
                file = (APP_DIR / "static" / ("index.html" if path == "/" else path.lstrip("/"))).resolve()
                if not file.is_relative_to(APP_DIR / "static") or not file.is_file():
                    raise APIError("文件不存在。", 404)
                return self.file_response(file)
            raise APIError("接口不存在。", 404)
        except APIError as ex:
            self.json_response({"error": str(ex)}, ex.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as ex:
            import traceback
            traceback.print_exc()
            self.json_response({"error": str(ex)}, 500)

    def file_response(self, file):
        size = file.stat().st_size
        start, end = 0, size - 1
        code = 200
        request_range = self.headers.get("Range")
        if request_range:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", request_range.strip())
            if not match or not any(match.groups()):
                return self.range_error(size)
            first, last = match.groups()
            if first:
                start = int(first)
                end = min(int(last), size - 1) if last else size - 1
            else:
                count = int(last)
                if count <= 0:
                    return self.range_error(size)
                start = max(0, size - count)
            if start >= size or end < start:
                return self.range_error(size)
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", mimetypes.guess_type(file)[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-cache")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with file.open("rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                block = f.read(min(256 * 1024, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)

    def range_error(self, size):
        self.send_response(416)
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", "0")
        self.end_headers()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=Path.cwd(), help="数据集列表的根目录，默认当前工作目录")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8003)
    ap.add_argument("--cache-dir", type=Path, default=APP_DIR / ".cache")
    args = ap.parse_args()
    if not args.root.is_dir():
        ap.error("--root 必须是现有的数据集根目录。")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    server.store = Store(args.root, args.cache_dir)
    print(f"Value 标注器：http://{args.host}:{server.server_port}", flush=True)
    print(f"数据集目录：{args.root.resolve()}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.store.pool.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
