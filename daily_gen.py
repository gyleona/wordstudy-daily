#!/usr/bin/env python3
"""
Daily Word Generation Script
Generates 8 new words + story + quote via DeepSeek API,
avoids duplication with last 30 days, uploads to CloudBase COS (static hosting).
Uses official Tencent COS Python SDK.
"""

import os
import sys
import json
import time
import requests
from datetime import datetime, timedelta, timezone
from qcloud_cos import CosConfig, CosS3Client

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


def get_cos_client():
    config = CosConfig(Region=HOSTING_REGION, SecretId=COS_SECRET_ID, SecretKey=COS_SECRET_KEY)
    return CosS3Client(config)


def fetch_remote_json(client, key):
    log(f"Fetching {key} from bucket {HOSTING_BUCKET}...")
    try:
        resp = client.get_object(Bucket=HOSTING_BUCKET, Key=key)
        body = resp["Body"].get_raw_stream().read().decode("utf-8")
        return body
    except Exception as e:
        log(f"Fetch failed: {e}")
        return None


def get_existing_words(client):
    raw = fetch_remote_json(client, "words-data.json")
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
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=90)
            r.raise_for_status()
            result = r.json()
        except Exception as e:
            log(f"POST failed: {e}")
            time.sleep(2)
            continue
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


def upload_to_cos(client, content_bytes, key):
    log(f"Uploading {key} ({len(content_bytes)} bytes) to bucket {HOSTING_BUCKET}...")
    try:
        resp = client.put_object(Bucket=HOSTING_BUCKET, Key=key, Body=content_bytes)
        log(f"Upload success ETag: {resp.get('ETag')}")
        return True
    except Exception as e:
        log(f"COS upload failed: {e}")
        return False


def main():
    log(f"=== Daily Word Generation {TODAY} ===")

    if not all([DEEPSEEK_API_KEY, COS_SECRET_ID, COS_SECRET_KEY, HOSTING_BUCKET, HOSTING_REGION]):
        log("ERROR: Missing required environment variables")
        sys.exit(1)

    client = get_cos_client()

    existing_words, existing_data = get_existing_words(client)
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
    # Replace today's existing words (avoid duplicates when re-running same day)
    prev_words = [w for w in existing_words if w.get("d") != TODAY]
    output["words"] = prev_words + [{**w, "d": TODAY} for w in new_words]
    output["story"] = result.get("story", output.get("story", {}))
    output["quote"] = result.get("quote", output.get("quote", {}))
    output["preview"] = result.get("preview", output.get("preview", {}))

    content_bytes = json.dumps(output, ensure_ascii=False, indent=2).encode("utf-8")
    success = upload_to_cos(client, content_bytes, "words-data.json")

    if success:
        log("=== DONE ===")
        sys.exit(0)
    else:
        log("=== FAILED (upload) ===")
        sys.exit(1)


if __name__ == "__main__":
    main()
