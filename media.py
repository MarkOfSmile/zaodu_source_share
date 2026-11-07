"""示范音生成、SOE 音频转码、IPA 音标查询。零第三方依赖。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from store import AUDIO, IPA_CACHE_FILE, UPLOADS, _read_json, _write_json

# 内置小词表：只放可确定的常用词，查不到一律留空待回填，绝不猜测音标。
BUILTIN_IPA = {
    "the": "/ðə/", "early": "/ˈɜːli/", "bird": "/bɜːd/",
    "catches": "/ˈkætʃɪz/", "worm": "/wɜːm/", "a": "/ə/",
    "an": "/ən/", "apple": "/ˈæpl/", "book": "/bʊk/", "good": "/ɡʊd/",
    "morning": "/ˈmɔːnɪŋ/", "teacher": "/ˈtiːtʃə/", "think": "/θɪŋk/",
    "this": "/ðɪs/", "three": "/θriː/", "read": "/riːd/",
}


def _run(cmd: list, timeout: int = 40) -> None:
    subprocess.run(cmd, check=True, timeout=timeout,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def tts_available() -> bool:
    """本机语音合成能力（macOS say + afconvert）。云端不可用时使用预置示范音。"""
    if os.environ.get("ZAODU_NO_TTS"):  # 模拟无本机 TTS 的托管环境
        return False
    return shutil.which("say") is not None and shutil.which("afconvert") is not None


def wav_convert_available() -> bool:
    """SOE 所需的 16k/16bit/mono wav 转码能力。"""
    return shutil.which("afconvert") is not None or shutil.which("ffmpeg") is not None


def demo_audio(text: str) -> str | None:
    """生成示范读音；已有预置文件优先，云端无 TTS 时直接返回 None 由前端降级。"""
    text = (text or "").strip()
    if not text:
        return None
    key = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    out = AUDIO / f"demo-{key}.m4a"
    if out.exists() and out.stat().st_size > 0:
        return out.name
    if not tts_available():
        return None
    aiff = Path(tempfile.gettempdir()) / f"zaodu-{key}.aiff"
    try:
        _run(["say", "-v", "Samantha", "-o", str(aiff), text])
        _run(["afconvert", "-f", "m4af", "-d", "aac", str(aiff), str(out)])
        return out.name if out.exists() and out.stat().st_size > 0 else None
    except (subprocess.SubprocessError, OSError):
        return None
    finally:
        try:
            aiff.unlink()
        except OSError:
            pass


def sample_voice(text: str, tag: str) -> str | None:
    """演示数据用：合成一条明显不同的示例录音，仅用于跑通链路。"""
    key = hashlib.sha1((tag + text).encode("utf-8")).hexdigest()[:12]
    out = AUDIO / f"sample-{key}.m4a"
    if out.exists():
        return out.name
    if not tts_available():
        return None
    aiff = Path(tempfile.gettempdir()) / f"zaodu-s-{key}.aiff"
    try:
        voice = "Alex" if tag.endswith(("1", "3", "5", "7", "9")) else "Samantha"
        rate = 130 if tag.endswith(("2", "4", "6")) else 165
        _run(["say", "-v", voice, "-r", str(rate), "-o", str(aiff), text])
        _run(["afconvert", "-f", "m4af", "-d", "aac", str(aiff), str(out)])
        return out.name if out.exists() else None
    except (subprocess.SubprocessError, OSError):
        return None
    finally:
        try:
            aiff.unlink()
        except OSError:
            pass


def to_soe_wav(filename: str) -> bytes | None:
    """转成 SOE 要求的 16kHz / 16bit / 单声道 wav。"""
    src = UPLOADS / filename
    if not src.exists():
        src = AUDIO / filename
    if not src.exists():
        return None
    dst = Path(tempfile.gettempdir()) / ("soe-" + hashlib.sha1(
        filename.encode()).hexdigest()[:10] + ".wav")
    if shutil.which("afconvert"):
        cmd = ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
               str(src), str(dst)]
    elif shutil.which("ffmpeg"):
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
               "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(dst)]
    else:
        return None
    try:
        _run(cmd)
        return dst.read_bytes()
    except (subprocess.SubprocessError, OSError):
        return None
    finally:
        try:
            dst.unlink()
        except OSError:
            pass


_PHONEME_LIST = [
    "tʃ", "dʒ", "ʃ", "ʒ", "θ", "ð", "ŋ",
    "iː", "ɜː", "ɔː", "ɑː", "uː", "ɪə", "eə", "ʊə", "əʊ", "aʊ", "ɔɪ", "eɪ", "aɪ",
    "æ", "ʌ", "ɒ", "ʊ", "ɪ", "ə", "e",
    "p", "b", "t", "d", "k", "ɡ", "f", "v", "s", "z", "h", "m", "n", "l", "r", "w", "j",
    "i", "u", "ɔ", "ɑ",
]
_PH_RE = re.compile("|".join(re.escape(p) for p in sorted(
    set(_PHONEME_LIST), key=len, reverse=True)))


def word_phonemes(ipa_text: str) -> list:
    """从单词音标里抽出实际存在的音素，供演示批注使用（避免给词安上不包含的音）。"""
    found, seen = [], set()
    for p in _PH_RE.findall(ipa_text or ""):
        if p not in seen:
            seen.add(p)
            found.append(p)
    return found


def split_words(text: str) -> list:
    return re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text or "")


def lookup_ipa(text: str, use_network: bool = True) -> dict:
    """逐词音标：内置表 → 可选在线词典 → 空值待回填。"""
    cache = _read_json(IPA_CACHE_FILE, {})
    result = {}
    words = split_words(text)
    for w in words:
        low = w.lower()
        if low in BUILTIN_IPA:
            result[w] = BUILTIN_IPA[low]
        elif low in cache:
            result[w] = cache[low] or ""
        else:
            result[w] = ""
    missing = [w for w in words if not result.get(w)]
    if use_network and missing:
        changed = False
        for w in missing[:12]:
            low = w.lower()
            try:
                url = "https://api.dictionaryapi.dev/api/v2/entries/en/" + urllib.parse.quote(low)
                with urllib.request.urlopen(url, timeout=2.5) as r:
                    data = json.loads(r.read().decode("utf-8"))
                found = ""
                for entry in data if isinstance(data, list) else []:
                    for ph in entry.get("phonetics", []):
                        t = (ph.get("text") or "").strip()
                        if t:
                            found = t if t.startswith("/") else "/" + t.strip("/") + "/"
                            break
                    if found:
                        break
                cache[low] = found
                result[w] = found
                changed = True
            except Exception:
                cache.setdefault(low, "")
                changed = True
        if changed:
            _write_json(IPA_CACHE_FILE, cache)
    return result
