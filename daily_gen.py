#!/usr/bin/env python3
"""
Daily Word Generation Script (full-version prompt)
Generates 8 words + story + quote + preview via DeepSeek API with FULL fields:
w, ph, m, c, d, ex, exZh, t, root, pos, en, col
- t (memory tip) must tie to real current news/economy events
- story/preview must weave most of the 8 words into one coherent news narrative
- avoids duplication with last 30 days
- uploads to CloudBase COS (static hosting) with no-cache header
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
            {"role": "system", "content": "You are an expert IELTS vocabulary tutor and a financial/economy news editor. Always respond with strict JSON only, no markdown, no extra text."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 5000
    }
    for attempt in range(max_retries):
        log(f"DeepSeek call attempt {attempt+1}...")
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=120)
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
                log(f"Content preview: {content[:300]}")
        time.sleep(2)
    return None


def generate_content(recent_set):
    avoid = ", ".join(sorted(recent_set)[:50]) if recent_set else "none"
    prompt = f"""Today is {TODAY}. Your job: act as both an IELTS vocabulary editor AND a financial news writer.

Generate EXACTLY 8 English words (IELTS 6.5+/CEFR C1, not below CET-6), each must be strongly tied to CURRENT real hot topics in economy, finance, news, or workplace as of {TODAY}.

IMPORTANT: Do NOT use these words that were learned recently: {avoid}

For EACH word, output ALL of these fields (every field is required):
{{
  "w": "word (base form)",
  "ph": "/IPA pronunciation/",
  "m": "concise Chinese meaning",
  "c": "econ | news | work",
  "pos": "part of speech (e.g. v. / n. / adj.)",
  "en": "English definition in plain English, 5-12 words",
  "col": "2-3 common collocations/patterns separated by center dot, e.g. escalate tensions/conflict · rapidly escalating · an escalation of prices",
  "ex": "English example sentence (1 sentence, natural, uses the word, ideally reflecting a real current event)",
  "exZh": "Chinese translation of the example",
  "t": "Memory tip in Chinese. MUST reference a REAL specific current news/economy event (e.g. '美国7月CPI低于预期后，市场开始 temper 对美联储降息的押注'), not a generic sentence. 15-40 Chinese characters.",
  "root": "Affix breakdown in Chinese (e.g. 前缀 e-(向外) + 词根 scal(梯子,拉丁 scala) + 后缀 -ate(动词) → 本义 爬上去 → 升级). If no clear affix, briefly explain origin in Chinese."
}}

Then output these three objects:
{{
  "story": {{
    "en": "English short paragraph (200 words max) weaving together MOST of today's 8 words (5-8 of them) into ONE coherent story about today's real economic/news events. Use [display text|base form] markers for the words (display can be inflected, base form is the dictionary form). Each word appears at most once.",
    "cn": "Chinese translation of the story"
  }},
  "quote": {{
    "en": "One English inspirational sentence, 6-12 words, about learning/growth/persistence",
    "zh": "Chinese translation"
  }},
  "preview": {{
    "hook": "Chinese hook paragraph (60-120 chars) that weaves MOST of today's 8 words (5-8 of them) into today's hot-news storyline, using [display|base] markers. Must NOT contain phrases like '8个词' or '记住这8个词'. Start with '今天的故事' or '今天的头条'.",
    "impact": "One Chinese sentence (20-40 chars) summarizing how today's words connect to the news mainline. Must NOT contain '8个词'."
  }}
}}

Output STRICT JSON only, no markdown, exactly this shape:
{{"words":[8 word objects],"story":{{"en":"...","cn":"..."}},"quote":{{"en":"...","zh":"..."}},"preview":{{"hook":"...","impact":"..."}}}}
"""
    return call_deepseek(prompt)


def upload_to_cos(client, content_bytes, key):
    log(f"Uploading {key} ({len(content_bytes)} bytes) to bucket {HOSTING_BUCKET}...")
    try:
        # CacheControl no-cache so CDN always fetches fresh data
        resp = client.put_object(
            Bucket=HOSTING_BUCKET,
            Key=key,
            Body=content_bytes,
            CacheControl="no-cache, max-age=0"
        )
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
