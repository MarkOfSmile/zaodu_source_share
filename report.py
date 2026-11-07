"""报告生成：HTML 版式 + 可选 PDF 渲染；无 PDF 引擎时返回 HTML 报告由浏览器打印。"""
from __future__ import annotations

import html
import os
import shutil
import subprocess
import tempfile
import uuid
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import media
import store
from store import OUTPUT

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium", "/usr/bin/chromium-browser",
]


def find_chrome() -> str | None:
    if os.environ.get("ZAODU_NO_PDF"):  # 模拟无 Chrome 的托管环境 / 强制走 HTML 报告
        return None
    for p in CHROME_CANDIDATES:
        if Path(p).exists():
            return p
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def pdf_available() -> bool:
    return find_chrome() is not None


def e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def _bar(score, color="#0F6E56") -> str:
    try:
        v = max(0, min(100, float(score)))
    except (TypeError, ValueError):
        return ""
    return (f'<div class="bar"><i style="width:{v:.0f}%;background:{color}"></i></div>')


def _score_color(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "#6B6A65"
    if f >= 85:
        return "#0F6E56"
    if f >= 70:
        return "#BA7517"
    return "#A32D2D"


def build_stats(cfg, subs, task_ids):
    roster = [dict(x) for x in cfg.get("_roster_parsed", [])]
    by_student = defaultdict(dict)
    for s in subs:
        if s.get("task_id") in task_ids:
            by_student[s["student_no"]][s["task_id"]] = s
    total_expected = len(roster) * len(task_ids)
    submitted = sum(len(v) for v in by_student.values())
    scored = [s["eval"]["score"] for v in by_student.values() for s in v.values()
              if s.get("eval") and s["eval"].get("score") is not None]
    wrong = Counter()
    for v in by_student.values():
        for s in v.values():
            ev = s.get("eval") or {}
            for w in ev.get("words", []):
                for p in w.get("phonemes", []):
                    if p.get("score", 100) < 60:
                        wrong["/" + str(p.get("symbol") or "?") + "/"] += 1
    word_wrong = Counter()
    for v in by_student.values():
        for s in v.values():
            for c in (s.get("eval") or {}).get("corrections", []):
                if c.get("word"):
                    word_wrong[c["word"]] += 1
    return {
        "roster": roster, "by_student": by_student, "total_expected": total_expected,
        "submitted": submitted,
        "rate": round(submitted / total_expected * 100, 1) if total_expected else 0,
        "avg": round(sum(scored) / len(scored), 1) if scored else None,
        "wrong": wrong.most_common(3), "word_wrong": word_wrong.most_common(3),
        "is_demo": any((s.get("eval") or {}).get("sample") for v in by_student.values()
                       for s in v.values()),
        "has_error": any((s.get("eval") or {}).get("mode") == "error"
                         for v in by_student.values() for s in v.values()),
    }


# 常见音素的中文口型／舌位提示，用于生成可执行的读音批注（非课标原文，教师可自行修订）。
PHONEME_TIPS = {
    "/θ/": "舌尖伸到上下齿之间送气，像「思」但舌头要露出来；别读成「斯」或「夫」。",
    "/ð/": "舌位同 θ，但要出声、喉咙发麻，像很轻的「日」；别读成「兹」或「德」。",
    "/ɜː/": "像「厄——」拖长两拍，嘴放松不圆不扁；别读成短促的「呃」，也别卷舌成「儿」。",
    "/æ/": "下巴明显打开发「哎」，嘴要比「诶」张得大；别读成「诶」。",
    "/v/": "上齿轻咬下唇再出声，像「夫」但带嗓音；别读成「乌」。",
    "/w/": "双唇收圆再滑向后面的元音，像「乌」起头；别读成「夫」。",
    "/r/": "舌尖卷起但不碰上颚，双唇略圆；别读成汉语「日」。",
    "/l/": "舌尖抵住上齿背，词尾也要点一下；别把它吞掉。",
    "/ɪ/": "短促放松的「衣」，别拖长成「衣——」。",
    "/iː/": "「衣——」拉长，嘴角往两边展开，长度要够。",
    "/ʃ/": "舌面抬起、双唇略前突，像「诗」；别读成「西」。",
    "/tʃ/": "「吃」一个音，别拆成「特+诗」。",
    "/ŋ/": "舌根抵住软腭、气从鼻子出，像「嗯」；别在后面补一个「格」。",
    "/ə/": "轻轻带过的「呃」，又短又松，只出现在非重读音节。",
    "/b/": "双唇闭紧再放开出声，像「伯」；别读成「泼」，词尾也别吞。",
    "/d/": "舌尖点一下上齿背再放开出声，像「德」；词尾要轻但清楚。",
    "/k/": "舌根抵住软腭再放开，像「克」；别在后面补「呃」。",
    "/z/": "「兹」的摩擦音要持续、出声；别读成「斯」。",
    "/m/": "双唇闭住、气从鼻子出，像「姆」；词尾要收住。",
    "/i/": "短促的「衣」，词尾别拖成长音。",
    "/t/": "舌尖点一下上齿背再放开，像「特」；词尾别吞。",
    "/p/": "双唇闭紧再放开送气，像「泼」；别读成「伯」。",
    "/s/": "「斯」的摩擦要细长；别读成「诗」。",
    "/f/": "上齿轻触下唇送气，像「夫」；别读成「呼」。",
    "/n/": "舌尖抵上齿背、气从鼻子出，像「恩」。",
}

# 音标 → 中文近似读法，用于让不认音标的老师也能直接照读
PHONEME_SAY = {
    "iː": "衣—", "ɪ": "衣", "e": "诶", "æ": "哎", "ɜː": "厄—", "ə": "呃",
    "ʌ": "阿", "ɑː": "啊—", "ɒ": "奥", "ɔː": "奥—", "ʊ": "乌", "uː": "乌—",
    "eɪ": "诶衣", "aɪ": "爱", "ɔɪ": "奥衣", "aʊ": "奥乌", "əʊ": "欧",
    "ɪə": "衣厄", "eə": "诶厄", "ʊə": "乌厄",
    "p": "泼", "b": "伯", "t": "特", "d": "德", "k": "克", "g": "格", "ɡ": "格",
    "f": "夫", "v": "夫·咬唇", "θ": "思·咬舌", "ð": "日·咬舌", "s": "斯", "z": "兹",
    "ʃ": "诗", "ʒ": "日", "tʃ": "吃", "dʒ": "知", "m": "姆", "n": "恩", "ŋ": "嗯",
    "l": "勒", "r": "若", "j": "衣", "w": "乌", "h": "喝",
    "i": "衣", "u": "乌", "ɑːr": "啊—若", "ɜːr": "厄—若", "ɔːr": "奥—若",
}


def say_word(ipa: str) -> str:
    """把音标转成中文近似读法，如 /ˈɜːli/ → 厄— · 利；查不到的音素直接略过。"""
    if not ipa or "待回填" in ipa:
        return ""
    toks = media.word_phonemes(ipa)
    parts = [PHONEME_SAY[t] for t in toks if t in PHONEME_SAY]
    return " · ".join(parts)


def phoneme_say(sym: str) -> str:
    return PHONEME_SAY.get(str(sym).strip("/"), "")


def phoneme_tip(sym: str) -> str:
    return PHONEME_TIPS.get(
        sym, "对照示范音做最小对立对听辨 → 放慢速度分音节跟读 → 放回原句连读。")


def daily_corrections(own: dict, task_ids: list, ipa_map: dict) -> list:
    """按「该学员的每一次提交」分别生成纠正条目，不跨日期、不跨学员合并。"""
    out = []
    for t in task_ids:
        s = own.get(t)
        if not s:
            out.append({"date": t, "status": "未交", "items": []})
            continue
        ev = s.get("eval") or {}
        if ev.get("mode") == "error":
            out.append({"date": t, "status": "error", "items": [ev.get("error", "")]})
            continue
        words, hits = [], []
        for w in ev.get("words", []):
            word = w.get("word") or ""
            bad = [p for p in w.get("phonemes", []) if p.get("score", 100) < 60]
            if bad:
                hits.append((bad[0].get("score", 0), word))
            elif isinstance(w.get("score"), (int, float)) and w["score"] < 75:
                hits.append((w["score"], word))
            words.append(word)
        hits.sort()
        out.append({"date": t, "status": "ok", "score": ev.get("score"),
                    "accuracy": ev.get("accuracy"), "fluency": ev.get("fluency"),
                    "integrity": ev.get("integrity"),
                    "audio": s.get("audio_file"), "sample": ev.get("sample"),
                    "words": words, "problems": [w for _, w in hits]})
    return out


def student_findings(own: dict, task_ids: list, ipa_map: dict) -> list:
    """该学员每个有问题的词一行：应当怎么读、错在第几个音、差多少、怎么改。"""
    data = {}
    for t in task_ids:
        ev = (own.get(t) or {}).get("eval") or {}
        if ev.get("mode") == "error":
            continue
        for w in ev.get("words", []):
            word = (w.get("word") or "").strip()
            if not word:
                continue
            key = word.lower()
            ipa = next((v for k, v in ipa_map.items() if k.lower() == key and v), "")
            d = data.setdefault(key, {"word": word, "ipa": ipa, "scores": [],
                                      "bad": Counter(), "worst": None,
                                      "pos": {}, "days": set(), "days_bad": set()})
            if not d["ipa"] and ipa:
                d["ipa"] = ipa
            if isinstance(w.get("score"), (int, float)):
                d["scores"].append(w["score"])
            d["days"].add(t)
            toks = media.word_phonemes(d["ipa"]) if d["ipa"] else []
            hit = False
            for p in w.get("phonemes", []):
                sym = str(p.get("symbol") or "?").strip("/")
                sc = p.get("score", 100)
                if sc < 60:
                    hit = True
                    d["bad"][sym] += 1
                    d["worst"] = sc if d["worst"] is None else min(d["worst"], sc)
                    if sym in toks and sym not in d["pos"]:
                        d["pos"][sym] = toks.index(sym) + 1
            if hit:
                d["days_bad"].add(t)

    rows = []
    for d in data.values():
        avg = round(sum(d["scores"]) / len(d["scores"]), 1) if d["scores"] else None
        has_bad = bool(d["bad"])
        if not has_bad and not (avg is not None and avg < 75):
            continue
        sym = None
        if has_bad:
            sym = d["bad"].most_common(1)[0][0]
        if sym:
            pos = d["pos"].get(sym)
            where = f"第 {pos} 个音" if pos else "其中某个音"
            head = (f"{where} <b>{e(sym)}</b> 只有 {d['worst']:.0f} 分"
                    f"（整个词 {avg if avg is not None else '—'} 分，"
                    f"{len(d['days_bad'])}/{len(d['days'])} 次偏低）")
            fix = phoneme_tip("/" + sym + "/")
        else:
            head = f"整个词 {avg} 分，属于整体读得含糊"
            fix = "先按「" + (say_word(d["ipa"]) or "音节") + "」拆开慢读，再连回整句。"
        rows.append({"word": d["word"], "ipa": d["ipa"], "say": say_word(d["ipa"]),
                     "sym": "/" + sym + "/" if sym else "", "avg": avg,
                     "where": head, "fix": fix,
                     "days_bad": len(d["days_bad"]), "days": len(d["days"])})
    rows.sort(key=lambda r: (r["days_bad"], -(r["avg"] or 0)), reverse=True)
    return rows


def teacher_notes(student_no: str, task_ids: list) -> list:
    """取该学员已保存的教师批注，按日期顺序返回 [(日期, 文字)]。"""
    comments = store.get_comments()
    out = []
    for t in task_ids:
        text = (comments.get(store.comment_key(student_no, t)) or {}).get("text", "").strip()
        if text:
            out.append((t, text))
    return out


def student_brief(own: dict, task_ids: list, findings: list) -> str:
    """一段话让老师先看懂这个学生的情况。"""
    n = len(own)
    scores = [own[t]["eval"]["score"] for t in task_ids
              if t in own and (own[t].get("eval") or {}).get("score") is not None]
    if not n:
        return "本期没有提交记录，无法给出读音纠正。"
    bits = [f"本期交了 {n}/{len(task_ids)} 天"]
    if scores:
        bits.append(f"均分 {round(sum(scores)/len(scores), 1)}（最高 {max(scores)}、最低 {min(scores)}）")
    missing = [t[5:] for t in task_ids if t not in own]
    if missing:
        bits.append("缺交 " + "、".join(missing))
    s = "，".join(bits) + "。"
    if findings:
        top = findings[:2]
        names = "、".join(f"{r['word']}（{r['say'] or r['ipa']}）" for r in top)
        s += f"最该改的是 {names}，具体读法见下表。"
    else:
        s += "没有明显的错词，可以开始练连读、重音与语调。"
    return s


CSS = """
@page { size: A4; margin: 14mm 13mm; }
* { box-sizing: border-box; }
body { font-family: "PingFang SC","Songti SC","Arial Unicode MS",sans-serif;
       font-size:10.5pt; color:#2C2C2A; line-height:1.65; margin:0; }
h1 { font-size:20pt; margin:0 0 2mm; font-weight:600; }
h2 { font-size:13pt; margin:9mm 0 3mm; padding-bottom:2mm;
     border-bottom:1px solid #D3D1C7; font-weight:600; }
h3 { font-size:11.5pt; margin:5mm 0 2mm; font-weight:600; }
.meta { color:#6B6A65; font-size:9pt; margin-bottom:5mm; }
.banner { padding:3mm 4mm; border-radius:2mm; font-size:9pt; margin-bottom:6mm; }
.banner.demo { background:#FCEBEB; color:#A32D2D; }
.banner.live { background:#E1F5EE; color:#0F6E56; }
.cards { display:flex; gap:3mm; margin:3mm 0 5mm; }
.card { flex:1; border:1px solid #E5E3DE; border-radius:2mm; padding:3mm; }
.card .n { font-size:17pt; font-weight:600; }
.card .l { font-size:8.5pt; color:#6B6A65; }
table { width:100%; border-collapse:collapse; margin:2mm 0 4mm; }
th,td { border:0.5px solid #DDDBD5; padding:2mm 2.2mm; font-size:9pt;
        text-align:left; vertical-align:top; }
th { background:#F7F7F5; font-weight:600; color:#444441; }
.material { background:#E1F5EE; border-radius:2mm; padding:4mm; margin:2mm 0 4mm; }
.material .txt { font-size:14pt; }
.material .ipa { font-family:Georgia,"Arial Unicode MS",serif; color:#0F6E56; margin-top:1mm; }
.bar { background:#EFEDE8; height:3mm; border-radius:2mm; overflow:hidden; min-width:22mm; }
.bar i { display:block; height:100%; }
.student { border:1px solid #E5E3DE; border-radius:2mm; padding:4mm;
           margin:0 0 5mm; break-inside:auto; }
.newpage { page-break-before:always; }
.student h3 { break-after:avoid; }
.audio-link { display:inline-block; padding:0.6mm 2mm; background:#E6F1FB;
              color:#185FA5; border-radius:1.5mm; font-size:8.5pt; }
.annot { table-layout:fixed; }
.annot td, .annot th { overflow-wrap:anywhere; }
.annot .note { font-size:8.5pt; line-height:1.5; }
.daily { table-layout:fixed; }
.daily td { overflow-wrap:anywhere; }
.fixes { margin:0; padding-left:4mm; }
.fixes li { margin:0.8mm 0; font-size:8.5pt; line-height:1.5; }
.bad { color:#A32D2D; font-weight:600; }
.tip { background:#FAEEDA; border-radius:2mm; padding:3mm; font-size:9pt; }
.one { background:#F1EFE8; border-radius:2mm; padding:2.5mm 3mm; font-size:9.5pt; margin:2mm 0 1mm; }
.say { font-weight:600; color:#0F6E56; }
.words .note { font-size:8.5pt; line-height:1.5; }
.tnote { margin-top:5mm; font-size:9.5pt; }
.lines { border-bottom:0.5px solid #B4B2A9; height:8mm; margin-top:2mm; }
.student .hd { display:flex; justify-content:space-between; align-items:baseline;
               border-bottom:1px solid #EFEDE8; padding-bottom:2mm; margin-bottom:2mm; }
.student .nm { font-size:12pt; font-weight:600; }
.student .sc { font-size:9pt; color:#6B6A65; }
ul { margin:1mm 0 3mm; padding-left:5mm; }
li { margin:1mm 0; font-size:9.5pt; }
a { color:#185FA5; text-decoration:none; }
.tag { display:inline-block; padding:0 2mm; border-radius:1mm; font-size:8pt;
       background:#F1EFE8; color:#5F5E5A; margin-left:2mm; }
.tag.ok { background:#E1F5EE; color:#0F6E56; }
.tag.no { background:#FCEBEB; color:#A32D2D; }
.small { font-size:8.5pt; color:#6B6A65; overflow-wrap:anywhere; word-break:break-all; }
.foot { margin-top:8mm; padding-top:3mm; border-top:1px solid #D3D1C7;
        font-size:8.5pt; color:#6B6A65; }
.break { page-break-before:always; }
"""


def build_html(cfg, subs, task_ids, demo_name=None, ipa_map=None, only_nos=None) -> str:
    st = build_stats(cfg, subs, task_ids)
    if isinstance(only_nos, str):
        only_nos = [only_nos]
    restricted = bool(only_nos)
    base = (cfg.get("base_url") or "").rstrip("/")
    qs = cfg.get("link_query") or ""
    period = f"{task_ids[0]} 至 {task_ids[-1]}" if task_ids else "—"
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    w = cfg.get("weights", {})
    ipa_map = ipa_map or {}
    if restricted:
        order = {no: i for i, no in enumerate(only_nos)}
        st["roster"] = sorted(
            [r for r in st["roster"] if r["no"] in order],
            key=lambda r: order.get(r["no"], 999))
    single = st["roster"][0] if (restricted and len(st["roster"]) == 1) else None
    if single:
        title = f"{single['no']} · {single['name']} 读音批注单"
    elif restricted:
        title = f"{cfg.get('class_name')} · 批量读音批注单（{len(st['roster'])} 人）"
    else:
        title = f"{cfg.get('class_name')} · 早读读音报告"
    parts = [
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>",
        f"<title>{e(title)}</title><style>{CSS}</style></head><body>",
        f"<h1>{e(title)}</h1>",
        f"<div class='meta'>统计区间 {e(period)} ｜ 最近 {len(task_ids)} 天 ｜ 生成于 {now} ｜ 材料形式：{e(cfg.get('mode'))}</div>",
    ]
    if st["is_demo"]:
        parts.append("<div class='banner demo'>演示数据 · 报告中的得分与音素错误为可复现示例，"
                     "不是真实发音评分。配置腾讯云 SOE 密钥后，重新运行评测即自动替换为音素级真实结果。</div>")
    else:
        parts.append("<div class='banner live'>真实评测 · 结果来自腾讯云智聆口语评测（SOE）音素级评测。</div>")
    if st["has_error"]:
        parts.append("<div class='banner demo'>部分提交评测失败，报告中该条已标注失败原因，未以示例分数代替。</div>")

    if restricted and len(st["roster"]) > 1:
        parts.append(f"<h2>本次批量批注名单（{len(st['roster'])} 人）</h2>")
        parts.append("<table><thead><tr><th>序号</th><th>姓名</th><th>提交次数</th>"
                     "<th>均分</th></tr></thead><tbody>")
        for r in st["roster"]:
            own = st["by_student"].get(r["no"], {})
            sc = [own[t]["eval"]["score"] for t in task_ids
                  if t in own and (own[t].get("eval") or {}).get("score") is not None]
            avg = round(sum(sc) / len(sc), 1) if sc else "—"
            parts.append(f"<tr><td>{e(r['no'])}</td><td>{e(r['name'])}</td>"
                         f"<td>{len(own)}/{len(task_ids)}</td><td>{avg}</td></tr>")
        parts.append("</tbody></table>")
        parts.append("<p class='small'>后页起每位学员独立起页，"
                     "含一句话看懂、每天得分与本人录音回放、逐词读音纠正与教师批注栏。</p>")

    if not restricted:
        parts.append("<h2>一 · 班级概览</h2><div class='cards'>")
        parts.append(f"<div class='card'><div class='n'>{len(st['roster'])}</div><div class='l'>在册学员</div></div>")
        parts.append(f"<div class='card'><div class='n'>{st['submitted']}</div><div class='l'>有效提交 / {st['total_expected']} 应交</div></div>")
        parts.append(f"<div class='card'><div class='n'>{st['rate']}%</div><div class='l'>七日提交率</div></div>")
        avg = f"{st['avg']}" if st["avg"] is not None else "—"
        parts.append(f"<div class='card'><div class='n'>{avg}</div><div class='l'>班级平均分</div></div></div>")
        parts.append("<p class='small'>后页起每位学员独立一页，结构固定为三块："
                     "① 一句话看懂（交了几次、均分、最该改哪几个词）；② 每天读得怎么样（含本人读音回放）；"
                     "③ 错在哪 · 怎么读（每个错词给出「应当这样读」「错在第几个音」「照着做的改法」）。"
                     "全班共性数据放在末页附录，仅供备课参考。</p>")

        parts.append("<h3>七日提交明细</h3><table><thead><tr><th>学员</th>")
        for t in task_ids:
            parts.append(f"<th>{t[5:]}</th>")
        parts.append("<th>均分</th></tr></thead><tbody>")
        for r in st["roster"]:
            parts.append(f"<tr><td>{e(r['no'])} {e(r['name'])}</td>")
            scores = []
            for t in task_ids:
                s = st["by_student"].get(r["no"], {}).get(t)
                ev = (s or {}).get("eval") or {}
                sc = ev.get("score")
                if sc is None:
                    parts.append("<td>—</td>")
                else:
                    scores.append(sc)
                    parts.append(f"<td>{sc}</td>")
            m = round(sum(scores) / len(scores), 1) if scores else "—"
            parts.append(f"<td><b>{m}</b></td></tr>")
        parts.append("</tbody></table>")

    parts.append("<h2>" + ("跟读材料与示范读音" if restricted else "二 · 跟读材料与示范读音") + "</h2>")
    parts.append(f"<div class='material'><div class='txt'>{e(cfg.get('material'))}</div>"
                 f"<div class='ipa'>{e(cfg.get('ipa') or '音标待回填')}</div></div>")
    if demo_name:
        parts.append(f"<p>示范读音：<a href='{base}/audio/{e(demo_name)}{qs}'>点击播放标准示范音</a>"
                     f"<span class='small'>（{base}/audio/{e(demo_name)}{e(qs)}）</span></p>")
    else:
        parts.append("<p class='small'>示范音未生成：本机 TTS 不可用，可在教师端页面用浏览器语音合成播放。</p>")
    words = list(ipa_map.keys())
    if words and not restricted:
        parts.append("<h3>逐词音标</h3><table><thead><tr><th>单词</th><th>音标</th><th>单词</th><th>音标</th></tr></thead><tbody>")
        for i in range(0, len(words), 2):
            a = words[i]
            b = words[i + 1] if i + 1 < len(words) else ""
            parts.append(f"<tr><td>{e(a)}</td><td>{e(ipa_map.get(a) or '待回填')}</td>"
                         f"<td>{e(b)}</td><td>{e(ipa_map.get(b) or '待回填') if b else ''}</td></tr>")
        parts.append("</tbody></table>")
        parts.append("<p class='small'>逐词音标为单词的孤立读音，进入句子后可能发生连读、弱读或变音"
                     "（如 the 在元音前读 /ði/），请以整句音标与示范音为准。"
                     "音标来自内置常用词表与可选在线词典，查不到时保留「待回填」，不做猜测；"
                     "生产环境建议由评测接口的「音素转国际音标」能力统一生成。</p>")

    cb = ["<h2>附录 · 班级共性参考（备课用，不替代逐人纠正）</h2>",
          "<p class='small'>以下为全班汇总，仅用于安排集体带读；每位学员本人的逐次纠正见前面各自的批注单。</p>"]
    if st["wrong"]:
        cb.append("<table><thead><tr><th>高频错音</th><th>怎么读</th><th>出现次数</th>"
                  "<th>集体带读建议</th></tr></thead><tbody>")
        for sym, cnt in st["wrong"]:
            cb.append(f"<tr><td><b>{e(sym)}</b></td><td>{e(phoneme_say(sym) or '—')}</td><td>{cnt}</td>"
                      f"<td>下次早读先做 {e(sym)} 的最小对立对听辨（如清浊、长短音），再带读含该音的单词，最后回到整句。</td></tr>")
        cb.append("</tbody></table>")
    else:
        cb.append("<p>本期未统计到音素级错误，建议提高材料难度或增加连读、语调维度的练习。</p>")
    ww = "、".join(f"「{e(x)}」" for x, _ in st["word_wrong"]) or "暂无"
    cb.append(f"<p><b>全班易错词：</b>{ww}。建议明天早读用 3 分钟集中带读这些词，再放回原句连读。</p>")
    cb.append(f"<p class='small'>评分权重：准确度 {w.get('accuracy',0.6)*100:.0f}% ／ 流利度 "
              f"{w.get('fluency',0.2)*100:.0f}% ／ 完整度 {w.get('integrity',0.2)*100:.0f}%。"
              "该权重为建议初值，需用本班样本与教师人工听评校准后固定。</p>")
    class_block = "".join(cb)

    if not restricted:
        parts.append("<div class='break'></div><h2>三 · 每位学员：错在哪、怎么读</h2>")
        parts.append("<p class='small'>每位学员占一页。先看「一句话看懂」了解整体情况，"
                     "再用第 1 节核对每天交没交、得几分，最后在第 2 节逐词看"
                     "「应当这样读 ／ 这次错在第几个音 ／ 怎么改」。"
                     "读音同时给中文近似与音标，不认音标也能照念。</p>")
    if not st["roster"]:
        parts.append("<p>尚未配置学员名单。</p>")
    for ridx, r in enumerate(st["roster"]):
        own = st["by_student"].get(r["no"], {})
        scores = [own[t]["eval"]["score"] for t in task_ids
                  if t in own and own[t].get("eval") and own[t]["eval"].get("score") is not None]
        mean = round(sum(scores) / len(scores), 1) if scores else "—"
        top = max(scores) if scores else "—"
        low = min(scores) if scores else "—"
        findings = student_findings(own, task_ids, ipa_map)
        start_new = (restricted and len(st["roster"]) > 1) or (not restricted and ridx > 0)
        parts.append("<div class='student" + (" newpage" if start_new else "") + "'>")
        parts.append(f"<div class='hd'><span class='nm'>{e(r['no'])} · {e(r['name'])}　读音批注单</span>"
                     f"<span class='sc'>提交 {len(own)}/{len(task_ids)} 天 ｜ 均分 "
                     f"<b style='color:{_score_color(mean)}'>{mean}</b> ｜ 最高 {top} ／ 最低 {low}</span></div>")
        if scores:
            parts.append(_bar(mean, _score_color(mean)))
        parts.append(f"<div class='one'><b>一句话看懂：</b>{e(student_brief(own, task_ids, findings))}</div>")

        parts.append("<h3>1 · 每天读得怎么样</h3>")
        daily = daily_corrections(own, task_ids, ipa_map)
        parts.append("<table class='daily'><colgroup>"
                     "<col style='width:9%'><col style='width:9%'><col style='width:9%'>"
                     "<col style='width:9%'><col style='width:9%'>"
                     "<col style='width:27%'><col style='width:28%'></colgroup>"
                     "<thead><tr><th>日期</th><th>综合</th><th>准确</th><th>流利</th><th>完整</th>"
                     "<th>当天读错的词</th><th>本人读音</th></tr></thead><tbody>")
        for d in daily:
            if d["status"] == "未交":
                parts.append(f"<tr><td>{d['date'][5:]}</td><td colspan='5'>"
                             f"<span class='tag no'>未交</span></td><td>—</td></tr>")
                continue
            if d["status"] == "error":
                parts.append(f"<tr><td>{d['date'][5:]}</td><td colspan='5' class='small'>评测失败："
                             f"{e(d['items'][0] if d['items'] else '')}</td><td>—</td></tr>")
                continue
            af = d.get("audio")
            link = (f"<a class='audio-link' href='{base}/audio/{e(af)}{qs}'>听 {d['date'][5:]}</a>"
                    f"<br><span class='small'>/audio/{e(af)}</span>") if af else "—"
            sample = " <span class='tag'>示例</span>" if d.get("sample") else ""
            probs = d.get("problems") or []
            probs_txt = "、".join(f"<b>{e(x)}</b>" for x in probs[:3]) if probs else "无明显错词"
            parts.append(f"<tr><td>{d['date'][5:]}</td><td><b>{d.get('score','—')}</b>{sample}</td>"
                         f"<td>{d.get('accuracy','—')}</td><td>{d.get('fluency','—')}</td>"
                         f"<td>{d.get('integrity','—')}</td><td>{probs_txt}</td><td>{link}</td></tr>")
        parts.append("</tbody></table>")

        parts.append("<h3>2 · 错在哪 · 怎么读</h3>")
        if findings:
            parts.append("<table class='words'><colgroup>"
                         "<col style='width:15%'><col style='width:19%'>"
                         "<col style='width:30%'><col style='width:36%'></colgroup>"
                         "<thead><tr><th>单词</th><th>应当这样读</th>"
                         "<th>这次错在哪</th><th>怎么改（照着做）</th></tr></thead><tbody>")
            for f in findings[:8]:
                say = f.get("say") or "音标待补"
                ipa = e(f["ipa"]) if f["ipa"] else "音标待回填"
                parts.append(
                    f"<tr><td><b>{e(f['word'])}</b><br><span class='small'>{ipa}</span></td>"
                    f"<td><span class='say'>{e(say)}</span></td>"
                    f"<td>{f['where']}</td>"
                    f"<td class='note'>{e(f['fix'])}</td></tr>")
            parts.append("</tbody></table>")
            parts.append("<p class='small'>「应当这样读」里的中文只是近似的发音提示，"
                         "带「—」的音要拖长；准确音值仍以音标和示范音为准。</p>")
        else:
            parts.append("<div class='tip'>这位学员本期没有明显错词。"
                         "下一步可练连读、重音位置与语调，或换更长的材料。</div>")

        parts.append("<h3>3 · 教师批注</h3>")
        saved = teacher_notes(r["no"], task_ids)
        if saved:
            parts.append("<div class='one' style='background:#EAF3FF'>"
                         + "<br>".join(f"<b>{e(t[5:])}</b>：{e(x)}" for t, x in saved)
                         + "</div>")
        parts.append("<div class='tnote'>教师批注（可手写）：<div class='lines'></div>"
                     "<div class='lines'></div></div>")
        parts.append("</div>")

    if not restricted:
        parts.append("<div class='break'></div>" + class_block)
    parts.append("<div class='foot'>"
                 "说明：本 PDF 中的音频为可点击链接，需保持本地服务运行；报告按学员、按提交逐次给出读音纠正，"
                 "仅用于教师教学参考，不建议作为公开排名依据。学员录音属于个人信息，按配置到期自动清理"
                 f"（当前留存 {e(cfg.get('retention_days'))} 天）。"
                 "示范音由本机语音合成生成，正式使用前请由任课教师核对标准读音与音标。</div>")
    parts.append("</body></html>")
    return "".join(parts)


def render_pdf(html_text: str, out_name: str | None = None) -> Path | None:
    chrome = find_chrome()
    if not chrome:
        return None
    name = out_name or ("zaodu_report_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".pdf")
    out = OUTPUT / name
    tmp = OUTPUT / ("_render_" + uuid.uuid4().hex[:8] + ".html")
    profile = Path(tempfile.gettempdir()) / ("zaodu-chrome-" + uuid.uuid4().hex[:8])
    tmp.write_text(html_text, "utf-8")
    cmd = [chrome, "--headless", "--disable-gpu", "--no-sandbox",
           f"--user-data-dir={profile}", "--no-pdf-header-footer",
           "--virtual-time-budget=3000", f"--print-to-pdf={out}", tmp.as_uri()]
    try:
        subprocess.run(cmd, check=True, timeout=90,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.SubprocessError, OSError):
        return None
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return out if out.exists() and out.stat().st_size > 2000 else None
