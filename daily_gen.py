#!/usr/bin/env python3
"""
Daily Word Generation Script
Generates 8 new words + story + quote via DeepSeek API,
avoids duplication with last 30 days, uploads to CloudBase COS.
"""

import os
import sys
import json
import time
import hmac
import hashlib
import requests
from datetime import datetime, timedelta, timezone

# Configuration
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
COS_SECRET_ID = os.environ.get("COS_SECRET_ID", "")
COS_SECRET_KEY = os.environ.get("COS_SECRET_KEY", "")
HOSTING_BUCKET = os.environ.get("HOSTING_BUCKET", "")
HOSTING_REGION = os.environ.get("HOSTING_REGION", "")

DEDUP_DAYS = 30
TODAY = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def log(msg):
    ts = datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def http_get(url, timeout=15):
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception as e:
        log(f"GET failed: {e}")
        return None


def http_post(url, data, headers=None, timeout=90):
    try:
        r = requests.post(url, json=data, headers=headers or {}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log(f"POST failed: {e}")
        return None


def get_cos_host():
    # Bucket format: 0313-static-wordstudy-d1gh0i7r67e8a64e8-1463809286
    # COS host: <bucket>.cos.<region>.myqcloud.com
    return f"{HOSTING_BUCKET}.cos.{HOSTING_REGION}.myqcloud.com"


def fetch_remote_json(key):
    host = get_cos_host()
    url = f"https://{host}/{key}"
    log(f"Fetching {key} from {host}...")
    return http_get(url)


def get_existing_words():
    raw = fetch_remote_json("words-data.json")
    if not raw:
        return [], {}
    try:
        d = json.loads(raw)
        return d.get("words", []), d
    except Exception as e:
        log(f"Parse JSON failed: {e}")
        return [], {}


def get_recent_words(words, days=DEDUP_DAYS):
    cutoff = (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=days)).strftime("%Y-%m-%d")
    recent = [w.get("w", "").lower() for w in words if w.get("d", "") >= cutoff]
    log(f"Last {days} days: {len(recent)} words to avoid")
    return set(recent)


def call_deepseek(prompt, max_retries=3):
    url = "https://api.deepseek.com/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "You are an expert IELTS vocabulary tutor. Always respond with strict JSON only, no markdown, no extra text."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 4000
    }
    for attempt in range(max_retries):
        log(f"DeepSeek call attempt {attempt+1}...")
        result = http_post(url, payload, headers=headers, timeout=90)
        if result and "choices" in result:
            content = result["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                lines = content.split("\n")
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                content = "\n".join(lines)
            try:
                return json.loads(content)
            except json.JSONDecodeError as e:
                log(f"JSON parse failed: {e}")
                log(f"Content preview: {content[:200]}")
        time.sleep(2)
    return None


def generate_content(recent_set):
    avoid = ", ".join(sorted(recent_set)[:50]) if recent_set else "none"
    prompt = f"""Today is {TODAY}. Generate EXACTLY 8 new English words for IELTS prep (CEFR C1 level, not below CET-6), themed around current hot topics in economy, news, or workplace.

IMPORTANT: Do NOT use these words that were learned recently: {avoid}

Output STRICT JSON (no markdown):
{{
  "words": [
    {{
      "w": "word",
      "ph": "/IPA/",
      "m": "Chinese meaning",
      "c": "econ|news|work",
      "ex": "English example",
      "exZh": "Chinese translation",
      "t": "Memory tip tied to news/hot topic",
      "root": "Affix breakdown (e.g. dis- + rupt + -ion)"
    }}
  ],
  "story": {{
    "en": "English paragraph with [display|original] markers for 2-4 of the words",
    "cn": "Chinese translation"
  }},
  "quote": {{
    "en": "An English inspirational sentence, 6-12 words, about learning/growth/persistence",
    "zh": "Chinese translation"
  }},
  "preview": {{
    "hook": "Hook paragraph (must NOT contain '8 words' or 'these 8 words'. Use today's story instead). Can use [display|original] markers.",
    "impact": "One-sentence impact. Must NOT contain '8 words'."
  }}
}}"""
    return call_deepseek(prompt)


def hmac_sha1(key, msg):
    return hmac.new(key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha1).hexdigest()


def upload_to_cos(content_bytes, key):
    host = get_cos_host()
    url = f"https://{host}/{key}"
    log(f"Uploading {key} ({len(content_bytes)} bytes) to {host}...")

    now = datetime.now(timezone(timedelta(hours=0)))
    timestamp = int(now.timestamp())
    date_str = now.strftime("%Y%m%d")
    short_date = now.strftime("%a, %d %b %Y %H:%M:%S GMT")

    http_method = "put"
    canonical_uri = f"/{key}"
    canonical_querystring = ""
    payload_hash = hashlib.sha1(content_bytes).hexdigest().lower()

    headers_to_sign = {
        "host": host,
        "date": short_date,
        "content-type": "application/json",
        "x-cos-content-sha1": payload_hash
    }

    canonical_headers = "\n".join(f"{k}:{v.strip()}" for k, v in sorted(headers_to_sign.items()))
    signed_headers = ";".join(sorted(headers_to_sign.keys()))

    canonical_request = f"{http_method}\n{canonical_uri}\n{canonical_querystring}\n{canonical_headers}\n\n{signed_headers}\n{payload_hash}"

    algorithm = "sha1"
    hashed_canonical_request = hashlib.sha1(canonical_request.encode("utf-8")).hexdigest().lower()
    credential_scope = f"{date_str}/{HOSTING_REGION}/cos/request"
    string_to_sign = f"{algorithm}\n{timestamp}\n{credential_scope}\n{hashed_canonical_request}"

    secret_date = hmac_sha1(f"TC3{COS_SECRET_KEY}", date_str)
    secret_service = hmac_sha1(secret_date, HOSTING_REGION)
    secret_signing = hmac_sha1(secret_service, "cos/request")
    signature = hmac_sha1(secret_signing, string_to_sign)

    authorization = (
        f"{algorithm} Credential={COS_SECRET_ID}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    full_headers = {
        "Authorization": authorization,
        "Date": short_date,
        "Host": host,
        "Content-Type": "application/json",
        "Content-Length": str(len(content_bytes)),
        "x-cos-content-sha1": payload_hash,
    }

    try:
        r = requests.put(url, data=content_bytes, headers=full_headers, timeout=30)
        if r.status_code in (200, 204):
            log("Upload success")
            return True
        else:
            log(f"COS upload failed: {r.status_code} {r.text[:200]}")
            return False
    except Exception as e:
        log(f"COS upload error: {e}")
        return False


def main():
    log(f"=== Daily Word Generation {TODAY} ===")

    if not all([DEEPSEEK_API_KEY, COS_SECRET_ID, COS_SECRET_KEY, HOSTING_BUCKET, HOSTING_REGION]):
        log("ERROR: Missing required environment variables")
        sys.exit(1)

    existing_words, existing_data = get_existing_words()
    log(f"Existing words: {len(existing_words)}")

    recent_set = get_recent_words(existing_words)

    result = generate_content(recent_set)
    if not result:
        log("ERROR: Failed to generate content")
        sys.exit(1)

    new_words = result.get("words", [])
    log(f"Generated {len(new_words)} new words")
    if len(new_words) != 8:
        log(f"WARNING: Got {len(new_words)} words, expected 8")

    output = dict(existing_data) if existing_data else {"words": [], "preview": {}, "story": {}, "quote": {}}
    output["updated_on"] = TODAY
    output["words"] = existing_words + [{**w, "d": TODAY} for w in new_words]
    output["story"] = result.get("story", output.get("story", {}))
    output["quote"] = result.get("quote", output.get("quote", {}))
    output["preview"] = result.get("preview", output.get("preview", {}))

    content_bytes = json.dumps(output, ensure_ascii=False, indent=2).encode("utf-8")
    success = upload_to_cos(content_bytes, "words-data.json")

    if success:
        log("=== DONE ===")
        sys.exit(0)
    else:
        log("=== FAILED (upload) ===")
        sys.exit(1)


if __name__ == "__main__":
    main()
