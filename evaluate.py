"""评测适配层：腾讯云智聆口语评测 SOE（真实）/ 演示模式（明确标注，绝不伪装评分）。

真实模式使用官方文档接口：
  Host    soe.tencentcloudapi.com
  Action  TransmitOralProcessWithInit
  Version 2018-07-24
  音频    16kHz / 16bit / 单声道 wav，VoiceFileType=2，非流式一次性传输
  鉴权    TC3-HMAC-SHA256
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import random
import re
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

import media

SOE_HOST = "soe.tencentcloudapi.com"
SOE_ENDPOINT = "https://" + SOE_HOST + "/"
SOE_ACTION = "TransmitOralProcessWithInit"
SOE_VERSION = "2018-07-24"


def _hmac256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _tc3_authorization(secret_id: str, secret_key: str, payload: str, ts: int) -> str:
    date = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
    service = "soe"
    content_type = "application/json; charset=utf-8"
    canonical_headers = f"content-type:{content_type}\nhost:{SOE_HOST}\n"
    signed_headers = "content-type;host"
    hashed_payload = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    canonical_request = "\n".join([
        "POST", "/", "", canonical_headers, signed_headers, hashed_payload,
    ])
    scope = f"{date}/{service}/tc3_request"
    string_to_sign = "\n".join([
        "TC3-HMAC-SHA256", str(ts), scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    k = _hmac256(("TC3" + secret_key).encode("utf-8"), date)
    k = _hmac256(k, service)
    k = _hmac256(k, "tc3_request")
    signature = hmac.new(k, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return (f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}")


def _as_score(v, default=None):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return round(f * 100 if 0 < f <= 1 else f, 1)


def _parse_soe(resp: dict, ref_text: str) -> dict:
    r = resp.get("Response", {})
    if "Error" in r:
        e = r["Error"]
        raise RuntimeError(f"SOE {e.get('Code', '')}: {e.get('Message', '')}")
    accuracy = _as_score(r.get("PronAccuracy"), 0)
    fluency = _as_score(r.get("PronFluency"), 0)
    integrity = _as_score(r.get("PronCompletion"), 0)
    words = []
    for w in r.get("Words") or r.get("WordInfoSet") or []:
        word = w.get("Word") or w.get("WordText") or ""
        wscore = _as_score(w.get("PronAccuracy") or w.get("Score"), 0)
        phonemes = []
        for p in w.get("Phonemes") or w.get("PhonemeInfoSet") or []:
            phonemes.append({
                "symbol": p.get("Phoneme") or p.get("IPA") or "",
                "score": _as_score(p.get("PronAccuracy") or p.get("Score"), 0),
            })
        words.append({"word": word, "score": wscore, "phonemes": phonemes})
    return {
        "accuracy": accuracy, "fluency": fluency, "integrity": integrity,
        "suggested": _as_score(r.get("SuggestedScore")),
        "words": words, "raw": r,
        "score": round(accuracy * 0.6 + fluency * 0.2 + integrity * 0.2, 1),
        "ref_text": ref_text,
    }


def call_soe(wav_bytes: bytes, ref_text: str, cfg: dict) -> dict:
    soe = cfg.get("soe", {})
    sid, skey = soe.get("secret_id", ""), soe.get("secret_key", "")
    if not sid or not skey:
        raise RuntimeError("未配置 SOE SecretId / SecretKey")
    payload = json.dumps({
        "SeqId": 1, "IsEnd": 1,
        "VoiceFileType": 2, "VoiceEncodeType": 1,
        "UserVoiceData": base64.b64encode(wav_bytes).decode("ascii"),
        "SessionId": str(uuid.uuid4()),
        "RefText": ref_text,
        "ServerType": int(cfg.get("server_type", 0)),
        "WorkMode": 0,
        "EvalMode": int(cfg.get("eval_mode", 1)),
        "TextMode": 0,
        "ScoreCoeff": 1.0,
    }, ensure_ascii=False, separators=(",", ":"))
    ts = int(datetime.now(timezone.utc).timestamp())
    headers = {
        "Authorization": _tc3_authorization(sid, skey, payload, ts),
        "Content-Type": "application/json; charset=utf-8",
        "Host": SOE_HOST,
        "X-TC-Action": SOE_ACTION,
        "X-TC-Version": SOE_VERSION,
        "X-TC-Timestamp": str(ts),
    }
    req = urllib.request.Request(SOE_ENDPOINT, data=payload.encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"SOE HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}")
    return _parse_soe(body, ref_text)


def _demo_result(sub: dict, cfg: dict) -> dict:
    """演示数据：可复现的示例结果，所有字段带 sample 标记，报告页显著水印。"""
    seed = int(hashlib.sha1(
        (sub["student_no"] + sub["task_id"] + cfg.get("material", "")).encode("utf-8")
    ).hexdigest()[:8], 16)
    rnd = random.Random(seed)
    words = []
    ipa_map = media.lookup_ipa(cfg.get("material", ""), use_network=False)
    focus = [s.strip("/") for s in (cfg.get("phoneme_focus") or ["/θ/", "/ð/"])]
    for w in media.split_words(cfg.get("material", "")):
        score = max(35, min(99, int(rnd.gauss(82, 11))))
        phonemes = []
        if score < 82:
            # 错音只从该词音标里真实存在的音素中挑选，避免给 bird 安上 /θ/ 这类张冠李戴的批注
            ipa = ipa_map.get(w) or media.BUILTIN_IPA.get(w.lower(), "")
            cand = media.word_phonemes(ipa)
            picked = next((s for s in focus if s in cand), None)
            if not picked:
                picked = rnd.choice(cand) if cand else rnd.choice(focus)
            phonemes.append({"symbol": picked,
                             "score": round(max(5, score - rnd.randint(12, 30)), 1)})
        words.append({"word": w, "score": score, "phonemes": phonemes})
    accuracy = max(40, min(98, int(rnd.gauss(83, 8))))
    fluency = max(40, min(99, int(rnd.gauss(86, 7))))
    integrity = max(55, min(100, int(rnd.gauss(92, 6))))
    return {
        "mode": "demo", "sample": True,
        "accuracy": accuracy, "fluency": fluency, "integrity": integrity,
        "score": round(accuracy * 0.6 + fluency * 0.2 + integrity * 0.2, 1),
        "words": words,
        "note": "演示数据，非真实发音评分。配置 SOE 密钥后自动替换为音素级真实评测。",
    }


def build_corrections(result: dict, cfg: dict) -> list:
    out = []
    for w in result.get("words", []):
        low = [p for p in w.get("phonemes", []) if p.get("score", 100) < 60]
        if low:
            syms = "、".join("/" + str(p.get("symbol", "?")) + "/" for p in low)
            out.append({
                "word": w.get("word", ""),
                "score": w.get("score"),
                "text": f"「{w.get('word', '')}」的 {syms} 发音偏弱（{w.get('score')} 分），建议对照示范音放慢速度跟读，注意口型与舌位后再连读整句。",
            })
        elif w.get("score", 100) < 70:
            out.append({
                "word": w.get("word", ""),
                "score": w.get("score"),
                "text": f"「{w.get('word', '')}」整体准确度 {w.get('score')} 分，建议先分音节朗读再连回句子。",
            })
    if not out:
        out.append({"word": "", "score": result.get("score"),
                    "text": "本次未发现明显音素错误，可增加语速与连读、语调的自然度练习。"})
    return out


def evaluate_submission(sub: dict, cfg: dict) -> dict:
    """评测一条提交。真实模式失败不静默降级为示例，避免把假分数当真结果。"""
    soe = cfg.get("soe", {})
    if soe.get("enabled") and soe.get("secret_id") and soe.get("secret_key"):
        wav = media.to_soe_wav(sub.get("audio_file", ""))
        if wav is None:
            result = {"mode": "error", "sample": False,
                      "error": "音频无法转换为 SOE 要求的 16k/16bit/单声道 wav"}
        else:
            try:
                result = call_soe(wav, sub.get("material_text") or cfg.get("material", ""), cfg)
                result["mode"] = "soe"
                result["sample"] = False
            except Exception as e:
                result = {"mode": "error", "sample": False, "error": str(e)}
    else:
        result = _demo_result(sub, cfg)
    result["corrections"] = build_corrections(result, cfg)
    result["evaluated_at"] = datetime.now().isoformat(timespec="seconds")
    return result
