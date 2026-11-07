"""早读语音评测 · 单端口 HTTP 服务（本地 / 公网发布通用）。仅用 Python 标准库。"""
from __future__ import annotations

import json
import mimetypes
import os
import random
import re
import secrets
import sys
import traceback
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import evaluate
import media
import report
import store

PORT = int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8787))
BIND_HOST = os.environ.get("BIND_HOST", "0.0.0.0")
MAX_BODY = 30 * 1024 * 1024
TEACHER_PATHS = {"/api/config", "/api/evaluate", "/api/report", "/api/report_batch",
                 "/api/seed", "/api/reset", "/api/comment"}


def _load_teacher_key() -> str:
    """优先环境变量；其次 teacher_key.txt；都没有时自动生成随机口令并落盘。

    公开源码里不保留固定口令：首次启动会把随机口令写入 data/teacher_key.txt
    （已被 .gitignore 排除），并在启动日志里打印，方便教师查看或改为自定义口令。
    """
    env = os.environ.get("TEACHER_KEY")
    if env:
        return env
    for p in (Path(__file__).resolve().parent / "teacher_key.txt",
              store.DATA / "teacher_key.txt"):
        try:
            if p.exists():
                v = p.read_text(encoding="utf-8").strip()
                if v:
                    return v
        except OSError:
            pass
    key = "zd-" + secrets.token_urlsafe(12)
    try:
        p = store.DATA / "teacher_key.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(key, encoding="utf-8")
        print(f"[早读] 未设置教师口令，已自动生成并写入 {p}", flush=True)
        print(f"[早读] 本次教师口令：{key}", flush=True)
    except OSError:
        print(f"[早读] 未设置教师口令，本次临时口令（重启会变）：{key}", flush=True)
    return key


TEACHER_KEY = _load_teacher_key()

MIME = {
    ".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".m4a": "audio/mp4", ".mp3": "audio/mpeg",
    ".wav": "audio/wav",     ".webm": "audio/webm", ".ogg": "audio/ogg",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".pdf": "application/pdf",
    ".zip": "application/zip",
    ".json": "application/json; charset=utf-8",
}


def request_base(headers) -> str:
    """从反向代理头推导公网地址，报告里的音频链接据此生成。"""
    proto = (headers.get("X-Forwarded-Proto") or headers.get("X-Forwarded-Scheme")
             or "").split(",")[0].strip()
    host = (headers.get("X-Forwarded-Host") or headers.get("Host")
            or f"127.0.0.1:{PORT}").split(",")[0].strip()
    if not proto:
        # 无代理头时按主机推断：本机回环走 http，公网域名一律按 https（发布平台即如此）
        local = (host.startswith("127.0.0.1") or host.startswith("localhost")
                 or host.startswith("0.0.0.0") or host.startswith("[::1]"))
        proto = "http" if local else "https"
    return f"{proto}://{host}"


def capabilities() -> dict:
    """告诉前端当前运行环境具备哪些能力，便于明确降级而不是静默失败。"""
    return {
        "pdf": report.pdf_available(),
        "tts": media.tts_available(),
        "wav_convert": media.wav_convert_available(),
    }


def public_config(cfg: dict) -> dict:
    out = json.loads(json.dumps(cfg))
    out["soe"]["secret_key"] = "******" if cfg["soe"].get("secret_key") else ""
    return out


def state_payload() -> dict:
    cfg = store.get_config()
    tids = store.recent_task_ids(7)
    subs = [s for s in store.get_submissions() if s.get("task_id") in tids]
    soe = cfg.get("soe", {})
    live = bool(soe.get("enabled") and soe.get("secret_id") and soe.get("secret_key"))
    return {
        "ok": True,
        "config": public_config(cfg),
        "roster": store.parse_roster(cfg),
        "task_ids": tids,
        "submissions": subs,
        "today": store.today_str(),
        "closed": store.is_closed(cfg.get("cutoff", "23:30")),
        "evaluation_mode": "soe" if live else "demo",
        "demo_audio": media.demo_audio(cfg.get("material", "")),
        "capabilities": capabilities(),
        "reports": [p.name for p in sorted(
            (p for p in store.OUTPUT.iterdir()
             if p.is_file() and p.suffix.lower() in (".pdf", ".html")),
            key=lambda p: p.stat().st_mtime, reverse=True)][:10],
    }


def normalize_nos(cfg: dict, raw) -> list:
    """把前端传来的学号列表归一化到名单写法，去重且保持顺序。"""
    if isinstance(raw, str):
        raw = [raw]
    valid = {r["no"] for r in store.parse_roster(cfg)}
    out = []
    for x in raw or []:
        no = store.match_roster_no(cfg, str(x).strip())
        if no and no in valid and no not in out:
            out.append(no)
    return out


def safe_name(s: str) -> str:
    """文件名安全化：去掉路径分隔符与非法字符，保留中文。"""
    return re.sub(r'[\\/:*?"<>|\r\n\t]+', "", str(s or "")).strip() or "未命名"


def report_stem(cfg: dict, only_nos: list | None = None,
                single_no: str | None = None) -> str:
    """报告文件名主体：单人「张三-20261107读音纠正」，批量/全班同理带日期。"""
    day = datetime.now().strftime("%Y%m%d")
    if single_no:
        names = {r["no"]: r["name"] for r in store.parse_roster(cfg)}
        who = safe_name(names.get(single_no) or f"学号{single_no}")
        return f"{who}-{day}读音纠正"
    if only_nos:
        return f"批量-{day}读音纠正-{len(only_nos)}人"
    return f"全班-{day}读音纠正"


def render_report(cfg: dict, days: int, only_nos: list | None = None,
                  single_no: str | None = None, stem: str | None = None) -> dict:
    """生成一份报告：全班 / 单人 / 多人合并；无 PDF 引擎时降级为 HTML。"""
    tids = store.recent_task_ids(days)
    cfg["_roster_parsed"] = store.parse_roster(cfg)
    subs = store.get_submissions()
    demo = media.demo_audio(cfg.get("material", ""))
    ipa = media.lookup_ipa(cfg.get("material", ""), use_network=False)
    stem = stem or report_stem(cfg, only_nos, single_no)
    html_text = report.build_html(cfg, subs, tids, demo, ipa,
                                  only_nos=only_nos or None)
    pdf = report.render_pdf(html_text, stem + ".pdf")
    if not pdf:
        saved_base = cfg.get("base_url", "")
        cfg["base_url"] = ""
        html_text = report.build_html(cfg, subs, tids, demo, ipa,
                                      only_nos=only_nos or None)
        cfg["base_url"] = saved_base
        name = stem + ".html"
        out = store.OUTPUT / name
        out.write_text(html_text, "utf-8")
        return {"format": "html", "file": name, "url": "/output/" + quote(name),
                "size": out.stat().st_size,
                "note": "当前环境无 PDF 引擎，已生成网页版；浏览器「打印 → 存为 PDF」即可导出。"}
    return {"format": "pdf", "file": pdf.name, "url": "/output/" + quote(pdf.name),
            "size": pdf.stat().st_size}


def submissions_page(query: str) -> dict:
    """提交记录分页：按提交时间倒序，带分数、音频地址与教师批注。"""
    q = parse_qs(query or "")
    try:
        page = max(1, int((q.get("page") or ["1"])[0] or 1))
    except ValueError:
        page = 1
    try:
        size = min(50, max(3, int((q.get("size") or ["8"])[0] or 8)))
    except ValueError:
        size = 8
    kw = ((q.get("q") or [""])[0] or "").strip().lower()

    subs = store.get_submissions()
    if kw:
        subs = [s for s in subs
                if kw in str(s.get("student_no", "")).lower()
                or kw in str(s.get("student_name", "")).lower()
                or kw in str(s.get("task_id", ""))]
    subs.sort(key=lambda s: (str(s.get("submitted_at") or ""), str(s.get("task_id") or "")),
              reverse=True)
    total = len(subs)
    pages = max(1, (total + size - 1) // size)
    page = min(page, pages)
    comments = store.get_comments()

    items = []
    for s in subs[(page - 1) * size: page * size]:
        ev = s.get("eval") or {}
        af = s.get("audio_file") or ""
        items.append({
            "id": s.get("id"), "task_id": s.get("task_id"),
            "student_no": s.get("student_no"), "student_name": s.get("student_name"),
            "submitted_at": s.get("submitted_at"), "source": s.get("source"),
            "score": ev.get("score"), "accuracy": ev.get("accuracy"),
            "fluency": ev.get("fluency"), "integrity": ev.get("integrity"),
            "evaluated": bool(ev) and ev.get("mode") != "error",
            "error": ev.get("error") if ev.get("mode") == "error" else "",
            "sample": bool(ev.get("sample")), "audio_file": af,
            "audio_url": ("/audio/" + quote(af)) if af else "",
            "comment": (comments.get(store.comment_key(s.get("student_no"), s.get("task_id")))
                        or {}).get("text", ""),
        })
    return {"ok": True, "page": page, "size": size, "total": total,
            "pages": pages, "items": items}


def api_seed(days: int) -> int:
    cfg = store.get_config()
    roster = store.parse_roster(cfg)
    tids = store.recent_task_ids(days)
    text = cfg.get("material") or "Hello world."
    voices = [v for v in (media.sample_voice(text, str(i)) for i in range(4)) if v]
    if not voices:
        v = media.demo_audio(text)
        voices = [v] if v else []
    count = 0
    for ti, tid in enumerate(tids):
        for i, stu in enumerate(roster):
            rnd = random.Random(f"{tid}-{stu['no']}")
            if rnd.random() < 0.18:
                continue
            item = {
                "task_id": tid,
                "student_no": stu["no"],
                "student_name": stu["name"],
                "source": "demo",
                "audio_file": voices[(i + ti) % len(voices)] if voices else "",
                "mime_type": "audio/mp4",
                "size_bytes": 12000,
                "duration_sec": 4 + (i % 3),
                "material_text": text,
                "submitted_at": f"{tid}T07:{10 + i:02d}:00",
                "seed": True,
            }
            item["eval"] = evaluate.evaluate_submission(item, cfg)
            store.upsert_submission(item)
            count += 1
    return count


class Handler(BaseHTTPRequestHandler):
    server_version = "ZaoduH5/1.0"

    def log_message(self, fmt, *args):
        msg = fmt % args
        if "/api/" in msg or " 40" in msg or " 50" in msg:
            print("[http] " + msg, flush=True)

    def _send(self, status: int, body: bytes, ctype: str, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _file(self, path: Path, download_name=None):
        if not path.exists() or not path.is_file():
            return self._json({"ok": False, "error": "not found"}, 404)
        ctype = MIME.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        extra = {}
        if download_name:
            ascii_name = "".join(ch if ord(ch) < 128 and ch not in '"\\' else "_"
                                 for ch in download_name)
            extra["Content-Disposition"] = (
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{quote(download_name)}")
        self._send(200, path.read_bytes(), ctype, extra)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n <= 0:
            return {}
        if n > MAX_BODY:
            raise ValueError("请求体过大")
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def _teacher_ok(self) -> bool:
        return (self.headers.get("X-Teacher-Key") or "") == TEACHER_KEY

    def _redirect(self, location: str):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        try:
            # 两个独立入口：/s 纯学生端、/t 纯教师端；sharecode 等查询参数原样保留
            if path in ("/s", "/student"):
                q = urlparse(self.path).query
                return self._redirect("/?" + (q + "&" if q else "") + "mode=stu")
            if path in ("/t", "/teacher"):
                q = urlparse(self.path).query
                return self._redirect("/?" + (q + "&" if q else "") + "mode=tea")
            if path in ("/", "/index.html"):
                return self._file(store.WEB / "index.html")
            if path == "/api/state":
                return self._json(state_payload())
            if path == "/api/submissions":
                if not self._teacher_ok():
                    return self._json({"ok": False, "error": "教师口令不正确"}, 401)
                return self._json({"ok": True, "submissions": store.get_submissions()})
            if path == "/api/submissions_page":
                if not self._teacher_ok():
                    return self._json({"ok": False, "error": "教师口令不正确"}, 401)
                return self._json(submissions_page(urlparse(self.path).query))
            if path.startswith("/audio/"):
                name = Path(path).name
                p = store.AUDIO / name
                if not p.exists():
                    p = store.UPLOADS / name
                return self._file(p)
            if path.startswith("/output/"):
                return self._file(store.OUTPUT / Path(path).name, Path(path).name)
            if path.startswith("/web/"):
                return self._file(store.WEB / Path(path).name)
            return self._json({"ok": False, "error": "not found"}, 404)
        except Exception as ex:
            traceback.print_exc()
            return self._json({"ok": False, "error": str(ex)}, 500)

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        try:
            body = self._body()
            if path in TEACHER_PATHS and not self._teacher_ok():
                return self._json({"ok": False, "error": "教师口令不正确，请在教师端输入口令"}, 401)
            cfg = store.get_config()

            if path == "/api/config":
                soe = body.get("soe")
                if isinstance(soe, dict) and soe.get("secret_key") in ("", "******"):
                    soe = dict(soe)
                    soe.pop("secret_key", None)
                    body["soe"] = soe
                old_material = cfg.get("material")
                cfg = store.save_config(body)
                if cfg.get("material") != old_material:
                    media.demo_audio(cfg.get("material", ""))
                return self._json({"ok": True, "config": public_config(cfg)})

            if path == "/api/submit":
                no = str(body.get("student_no", "")).strip()
                name = str(body.get("student_name", "")).strip()
                audio = body.get("audio") or ""
                if not no or not name:
                    return self._json({"ok": False, "error": "请填写序号和姓名"}, 400)
                if not audio:
                    return self._json({"ok": False, "error": "缺少音频数据"}, 400)
                canonical = store.match_roster_no(cfg, no)
                if canonical is None:
                    nos = "、".join(r["no"] for r in store.parse_roster(cfg))
                    return self._json({"ok": False,
                                       "error": f"序号 {no} 不在教师名单中（当前名单：{nos or '空'}）"}, 400)
                if store.is_closed(cfg.get("cutoff", "23:30")) and not body.get("force"):
                    return self._json({"ok": False, "error": "今日已截卡，请联系老师补交"}, 403)
                fname, size = store.save_audio_b64(audio, body.get("mime") or "audio/mp4")
                item = {
                    "task_id": body.get("task_id") or store.today_str(),
                    "student_no": canonical, "student_name": name,
                    "source": body.get("source") or "upload",
                    "audio_file": fname, "size_bytes": size,
                    "mime_type": body.get("mime") or "audio/mp4",
                    "duration_sec": body.get("duration_sec"),
                    "material_text": cfg.get("material", ""),
                    "submitted_at": store.datetime.now().isoformat(timespec="seconds"),
                    "seed": False,
                }
                saved, replaced = store.upsert_submission(item)
                return self._json({"ok": True, "submission": saved, "replaced": replaced})

            if path == "/api/evaluate":
                tids = set(body.get("task_ids") or store.recent_task_ids(7))
                subs = store.get_submissions()
                n, errors = 0, 0
                for s in subs:
                    if s.get("task_id") not in tids:
                        continue
                    if not body.get("force") and s.get("eval") and s["eval"].get("mode") != "error":
                        continue
                    s["eval"] = evaluate.evaluate_submission(s, cfg)
                    if s["eval"].get("mode") == "error":
                        errors += 1
                    n += 1
                store.write_submissions(subs)
                return self._json({"ok": True, "evaluated": n, "errors": errors})

            if path == "/api/comment":
                raw_no = str(body.get("student_no") or "").strip()
                no = store.match_roster_no(cfg, raw_no) or raw_no
                tid = str(body.get("task_id") or "").strip()
                if not no or not tid:
                    return self._json({"ok": False, "error": "缺少学员或日期"}, 400)
                saved = store.set_comment(no, tid, str(body.get("text") or ""))
                return self._json({"ok": True, "comment": saved})

            if path == "/api/report":
                days = max(1, min(31, int(body.get("days", 7))))
                nos = normalize_nos(cfg, body.get("student_nos") or body.get("student_no"))
                # 公网域名以浏览器上报的为准：反向代理可能只把内网地址写进 Host，
                # 直接用请求头会生成老师手机上打不开的沙箱链接。
                client_base = str(body.get("base_url") or "").strip()
                cfg["base_url"] = (client_base.rstrip("/") if client_base.startswith(("http://", "https://"))
                                   else request_base(self.headers))
                share = str(body.get("sharecode") or "").strip()
                cfg["link_query"] = ("?sharecode=" + quote(share)) if share else ""
                res = render_report(cfg, days, nos or None,
                                    single_no=nos[0] if len(nos) == 1 else None)
                return self._json({"ok": True, **res})

            if path == "/api/report_batch":
                days = max(1, min(31, int(body.get("days", 7))))
                nos = normalize_nos(cfg, body.get("student_nos"))
                if not nos:
                    return self._json({"ok": False, "error": "请先勾选至少一名在册学员"}, 400)
                client_base = str(body.get("base_url") or "").strip()
                cfg["base_url"] = (client_base.rstrip("/") if client_base.startswith(("http://", "https://"))
                                   else request_base(self.headers))
                share = str(body.get("sharecode") or "").strip()
                cfg["link_query"] = ("?sharecode=" + quote(share)) if share else ""

                day = datetime.now().strftime("%Y%m%d")
                names = {r["no"]: r["name"] for r in store.parse_roster(cfg)}
                combined = render_report(cfg, days, nos,
                                         stem=f"批量-{day}读音纠正-{len(nos)}人")
                items, used = [], {}
                for no in nos:
                    who = safe_name(names.get(no) or f"学号{no}")
                    used[who] = used.get(who, 0) + 1
                    one_stem = (f"{who}-{day}读音纠正"
                                + (f"-{used[who]}" if used[who] > 1 else ""))
                    one = render_report(cfg, days, [no], single_no=no, stem=one_stem)
                    items.append({"student_no": no, "student_name": who, **one})
                zname = f"批量-{day}读音纠正-{len(nos)}人.zip"
                zpath = store.OUTPUT / zname
                with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
                    zf.write(store.OUTPUT / combined["file"], combined["file"])
                    for it in items:
                        zf.write(store.OUTPUT / it["file"], it["file"])
                return self._json({
                    "ok": True, "count": len(nos),
                    "students": [{"no": n, "name": names.get(n, "")} for n in nos],
                    "combined": combined, "items": items,
                    "zip": {"file": zname, "url": "/output/" + quote(zname),
                            "size": zpath.stat().st_size}})

            if path == "/api/seed":
                n = api_seed(max(1, min(31, int(body.get("days", 7)))))
                return self._json({"ok": True, "created": n,
                                   "note": "已生成可复现的演示数据，报告会带「演示数据」水印"})

            if path == "/api/reset":
                store.write_submissions([])
                return self._json({"ok": True})

            return self._json({"ok": False, "error": "not found"}, 404)
        except Exception as ex:
            traceback.print_exc()
            return self._json({"ok": False, "error": str(ex)}, 500)


def main():
    cfg = store.get_config()
    print("=" * 58)
    print("  早读语音评测系统 · 单端口服务")
    print("=" * 58)
    print(f"  监听       {BIND_HOST}:{PORT}")
    print(f"  本地打开   http://127.0.0.1:{PORT}/")
    print(f"  班级       {cfg['class_name']}")
    print(f"  评测模式   {'SOE 真实评测' if cfg['soe']['enabled'] and cfg['soe']['secret_id'] else '演示模式（报告带水印）'}")
    print(f"  PDF 引擎   {'可用' if report.pdf_available() else '不可用 → 降级为网页版报告'}")
    print(f"  本机 TTS   {'可用' if media.tts_available() else '不可用 → 用预置示范音/浏览器合成'}")
    print(f"  教师口令   {'已由 TEACHER_KEY 设置' if os.environ.get('TEACHER_KEY') else '来自 teacher_key.txt 或首次启动自动生成'}")
    print(f"  数据目录   {store.DATA}")
    print("  停止服务   Ctrl+C")
    print("=" * 58, flush=True)
    media.demo_audio(cfg.get("material", ""))
    store.purge_expired(int(cfg.get("retention_days", 30)))
    srv = ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
