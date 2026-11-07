"""本地 JSON 存储层：配置、学员名单、提交记录、音频文件。零第三方依赖。"""
from __future__ import annotations

import base64
import json
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
UPLOADS = DATA / "uploads"
AUDIO = DATA / "audio"
OUTPUT = ROOT / "output"
WEB = ROOT / "web"
for _p in (DATA, UPLOADS, AUDIO, OUTPUT, WEB):
    _p.mkdir(parents=True, exist_ok=True)

CONFIG_FILE = DATA / "config.json"
SUBS_FILE = DATA / "submissions.json"
IPA_CACHE_FILE = DATA / "ipa_cache.json"
COMMENTS_FILE = DATA / "comments.json"
_LOCK = threading.RLock()

DEFAULT_CONFIG = {
    "class_name": "99 班早读群",
    "cutoff": "23:30",
    "material": "The early bird catches the worm.",
    "ipa": "/ði ˈɜːli bɜːd ˈkætʃɪz ðə wɜːm/",
    "mode": "句子",
    "language": "en",
    "eval_mode": 1,
    "server_type": 0,
    "retention_days": 30,
    "roster": ["01 张三", "02 李四", "03 王五", "04 赵六"],
    "weights": {"accuracy": 0.6, "fluency": 0.2, "integrity": 0.2},
    "phoneme_focus": ["/θ/", "/ð/", "/ɜː/", "/æ/"],
    "soe": {"enabled": False, "secret_id": "", "secret_key": ""},
    "base_url": "",  # 空则报告用相对链接；服务端生成报告时按请求头注入当前公网地址
}


def _read_json(path: Path, default):
    with _LOCK:
        if not path.exists():
            return json.loads(json.dumps(default))
        try:
            return json.loads(path.read_text("utf-8"))
        except (ValueError, OSError):
            return json.loads(json.dumps(default))


def _write_json(path: Path, obj) -> None:
    with _LOCK:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(path)


def get_config() -> dict:
    cfg = _read_json(CONFIG_FILE, DEFAULT_CONFIG)
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    for k, v in DEFAULT_CONFIG["weights"].items():
        cfg["weights"].setdefault(k, v)
    for k, v in DEFAULT_CONFIG["soe"].items():
        cfg["soe"].setdefault(k, v)
    return cfg


def save_config(patch: dict) -> dict:
    cfg = get_config()
    for k, v in patch.items():
        if k not in DEFAULT_CONFIG:
            continue
        if k in ("weights", "soe") and isinstance(v, dict):
            cfg[k] = {**cfg[k], **{kk: vv for kk, vv in v.items() if kk in cfg[k]}}
        else:
            cfg[k] = v
    _write_json(CONFIG_FILE, cfg)
    return cfg


def get_submissions() -> list:
    return _read_json(SUBS_FILE, [])


def write_submissions(subs: list) -> None:
    _write_json(SUBS_FILE, subs)


def parse_roster(cfg: dict) -> list:
    out = []
    for line in cfg.get("roster", []):
        parts = str(line).strip().split()
        if parts:
            out.append({"no": parts[0], "name": " ".join(parts[1:])})
    return out


_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def normalize_no(value) -> str:
    """序号归一化：去空格、全角数字转半角。"""
    return str(value or "").strip().translate(_FULLWIDTH_DIGITS)


def match_roster_no(cfg: dict, value) -> str | None:
    """把学员输入的序号匹配到名单里的规范写法，兼容 1 / 01 / ００１。

    返回名单中的原始序号（如 '01'），匹配不到返回 None。统一返回规范值很关键：
    否则 '1' 与 '01' 会被当成两个学员，破坏去重键与七日统计。
    """
    target = normalize_no(value)
    if not target:
        return None
    roster = parse_roster(cfg)
    for r in roster:
        if normalize_no(r["no"]) == target:
            return r["no"]
    if target.isdigit():
        for r in roster:
            cand = normalize_no(r["no"])
            if cand.isdigit() and int(cand) == int(target):
                return r["no"]
    return None


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def is_closed(cutoff: str) -> bool:
    try:
        h, m = (int(x) for x in cutoff.split(":"))
    except (ValueError, AttributeError):
        h, m = 23, 30
    now = datetime.now()
    return now.hour * 60 + now.minute > h * 60 + m


def recent_task_ids(days: int = 7) -> list:
    base = datetime.now().date()
    return [(base - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]


EXT_BY_MIME = {
    "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "audio/webm": ".webm", "audio/ogg": ".ogg", "video/mp4": ".mp4",
    "video/webm": ".webm", "video/quicktime": ".mov",
}


def save_audio_b64(b64: str, mime: str) -> tuple:
    raw = base64.b64decode(b64.split(",")[-1], validate=False)
    ext = EXT_BY_MIME.get((mime or "").split(";")[0].strip().lower(), ".bin")
    name = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6] + ext
    (UPLOADS / name).write_bytes(raw)
    return name, len(raw)


def upsert_submission(item: dict) -> tuple:
    """去重键 (task_id, student_no)；重复提交覆盖，历史版本留痕。"""
    subs = get_submissions()
    idx = next(
        (i for i, s in enumerate(subs)
         if s["task_id"] == item["task_id"] and s["student_no"] == item["student_no"]),
        -1,
    )
    replaced = idx >= 0
    if replaced:
        old = subs[idx]
        item["id"] = old["id"]
        item["history"] = old.get("history", []) + [{
            "submitted_at": old.get("submitted_at"),
            "audio_file": old.get("audio_file"),
            "source": old.get("source"),
        }]
        subs[idx] = item
    else:
        item["id"] = uuid.uuid4().hex[:12]
        item["history"] = []
        subs.append(item)
    write_submissions(subs)
    return item, replaced


def update_submission(sub_id: str, patch: dict) -> bool:
    subs = get_submissions()
    for s in subs:
        if s["id"] == sub_id:
            s.update(patch)
            write_submissions(subs)
            return True
    return False


def purge_expired(retention_days: int) -> int:
    """按留存策略清理过期上传音频；返回删除文件数。"""
    if retention_days <= 0:
        return 0
    cutoff_ts = datetime.now() - timedelta(days=retention_days)
    removed = 0
    for p in UPLOADS.iterdir():
        if not p.is_file():
            continue
        try:
            if datetime.fromtimestamp(p.stat().st_mtime) < cutoff_ts:
                p.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def comment_key(student_no: str, task_id: str) -> str:
    return f"{student_no}|{task_id}"


def get_comments() -> dict:
    return _read_json(COMMENTS_FILE, {})


def get_comment(student_no: str, task_id: str) -> str:
    return (get_comments().get(comment_key(student_no, task_id)) or {}).get("text", "")


def set_comment(student_no: str, task_id: str, text: str) -> dict:
    """教师批注：按 (学员, 任务日) 保存；传空字符串表示删除该条批注。"""
    data = get_comments()
    key = comment_key(student_no, task_id)
    text = (text or "").strip()
    if text:
        data[key] = {"student_no": student_no, "task_id": task_id, "text": text,
                     "updated_at": datetime.now().isoformat(timespec="seconds")}
    else:
        data.pop(key, None)
    _write_json(COMMENTS_FILE, data)
    return data.get(key, {})
