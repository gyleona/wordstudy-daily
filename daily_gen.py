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
import re
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


QUOTE_HISTORY_KEY = "quote-history.jsonl"


def get_quote_history(client):
    """Fetch quote history from COS. Returns list of {date, en, zh} dicts."""
    raw = fetch_remote_json(client, QUOTE_HISTORY_KEY)
    history = []
    if raw:
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                history.append(json.loads(line))
            except Exception:
                continue
    log(f"Quote history: {len(history)} entries")
    return history


def append_quote_history(client, entry):
    """Append today's quote to quote-history.jsonl on COS. Keeps last 60 entries."""
    raw = fetch_remote_json(client, QUOTE_HISTORY_KEY)
    lines = []
    if raw:
        lines = [l for l in raw.splitlines() if l.strip()]
    lines.append(json.dumps(entry, ensure_ascii=False))
    if len(lines) > 60:
        lines = lines[-60:]
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    log("Uploading updated quote-history.jsonl...")
    try:
        client.put_object(Bucket=HOSTING_BUCKET, Key=QUOTE_HISTORY_KEY, Body=payload)
        log("quote-history.jsonl updated")
    except Exception as e:
        log(f"quote-history.jsonl upload failed: {e}")


def ensure_markers(text, words):
    """Fallback: wrap any today's words that appear bare in text with [word|word] markers.
    Handles both the English base form AND the Chinese gloss (first sense of m).
    Protects existing [display|base] markers so we don't double-wrap."""
    if not text:
        return text
    placeholders = {}
    def protect(m):
        ph = f"\x00{len(placeholders)}\x00"
        placeholders[ph] = m.group(0)
        return ph
    text2 = re.sub(r"\[[^\]|]+\|[^\]]+\]", protect, text)
    for w in words:
        base = (w.get("w") or "").strip()
        if not base:
            continue
        # English base form
        pattern = r"\b" + re.escape(base) + r"\b"
        def wrap(m):
            return f"[{m.group(0)}|{base}]"
        text2 = re.sub(pattern, wrap, text2, flags=re.IGNORECASE)
        # Chinese gloss: first sense from m, e.g. "通胀的；引起通胀的" -> "通胀"
        gloss = (w.get("m") or "").strip()
        if gloss:
            first_sense = gloss.split("；")[0].split(";")[0].split("，")[0].split(",")[0].strip()
            if len(first_sense) >= 2:
                gpattern = re.escape(first_sense)
                def gwrap(m):
                    return f"[{m.group(0)}|{base}]"
                text2 = re.sub(gpattern, gwrap, text2)
    for ph, orig in placeholders.items():
        text2 = text2.replace(ph, orig)
    return text2


SECOND_PART_KEYWORDS = ["对比词", "职场中", "职场版", "记忆", "联想", "同根词", "形近词", "反义词", "近义词", "顺口溜", "助记"]


def ensure_t_two_paragraphs(t):
    """Fallback: if the t field is a single paragraph but contains a second-part keyword
    (对比词/职场中/记忆/联想...), insert a newline before it so the card shows two paragraphs
    like the original version."""
    if not t or "\n" in t:
        return t
    for kw in SECOND_PART_KEYWORDS:
        idx = t.find(kw)
        if idx > 10:
            return t[:idx].rstrip() + "\n" + t[idx:].lstrip()
    return t


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


def generate_content(recent_set, quote_history=None):
    avoid = ", ".join(sorted(recent_set)[:50]) if recent_set else "none"
    quote_history = quote_history or []
    # Build quote dedup text
    if quote_history:
        recent_quotes = [q.get("en", "") for q in quote_history[-60:]]
        quote_avoid = "\n".join("  - " + q for q in recent_quotes[-15:])
        quote_instruction = f"Your quote.en MUST NOT be similar to any of these recent quotes (avoid same sentence structure, keywords, and meaning):\n{quote_avoid}"
    else:
        quote_instruction = "No prior quotes to avoid; just make it fresh and original."
    prompt = f"""Today is {TODAY}. Your job: act as both an IELTS vocabulary editor AND a financial news writer.

Generate EXACTLY 8 English words (IELTS 6.5+/CEFR C1, not below CET-6), each must be strongly tied to CURRENT real hot topics as of {TODAY}. PRIMARY sources (5-6 of the 8 words): economy, finance, news, workplace. SECONDARY sources (2-3 of the 8 words): politics, entertainment, sports — whenever today's headlines contain a political/entertainment/sports hot topic that yields a good IELTS word, USE it; only skip secondary if nothing fits. Mix should feel diverse, not monotonous.

IMPORTANT: Do NOT use these words that were learned recently: {avoid}

For EACH word, output ALL of these fields (every field is required). Use the "STYLE GUIDE" below to match the expected depth and length:

  w: word (base form)
  ph: /IPA pronunciation/
  m: 10-30 Chinese chars. Concise multi-sense Chinese meaning, with part-of-speech hint in front if multiple senses, e.g. "(冲突)升级; 逐步扩大"
  c: econ | news | work | politics | entertainment | sports  (aim: 5-6 of 8 words in econ/news/work; 2-3 words from politics/entertainment/sports when current headlines fit)
  pos: v. / n. / adj. / adv.
  en: 30-70 English chars. Plain-English definition, can include multiple senses joined by semicolons, and the typical context (e.g. "in medical/economic contexts")
  col: 40-90 chars total. 3-4 common collocations separated by " · ", each ideally with Chinese gloss in parentheses, e.g. "cross a threshold 越过临界点 · pain threshold 痛感阈值 · threshold for action 行动门槛"
  ex: 60-130 English chars. ONE natural English sentence that uses the word (inflected ok), and reflects a real current news/economic event or plausible scenario
  exZh: 14-50 Chinese chars. Natural Chinese translation of the example
  t: 90-140 Chinese chars, in TWO paragraphs separated by a newline. Paragraph 1 (50-80 chars): tie the word to a SPECIFIC real current event (economy/news/workplace/politics/entertainment/sports), with names, numbers, and the word used in context. Paragraph 2 (40-60 chars): give a workplace/daily-life example sentence, OR contrast with a related word (e.g. antonym, near-synonym, noun/verb form), OR a memorable image/association to lock the word in memory. Both paragraphs required.
  root: 60-100 Chinese chars. Full affix breakdown with: prefix/root/suffix separated by " + ", each component's source language (拉丁/希腊/古英语/法语/阿拉伯语 etc.), at least one cognate word per component, then a short arrow showing etymology-to-modern-meaning evolution. Example format: "前缀 bene-(好, 拉丁, 同源 benefit/benevolent) + 词根 gn(=gen 生, 同源 genus) → 天性是好的 → 温和无害、良性". If the word has no clear affixes, briefly explain its etymology origin in Chinese.

STYLE GUIDE - match this level of detail (these are real examples from this app's history, do not copy, just match the depth):

  t example A (109 chars): "高盛押注今晚"鸽派惊喜"、汇丰预判数据"温和"，一旦成真就是市场最爱的 Goldilocks 行情，几乎所有资产普涨。医院报告里的"良性"也是这个词：benign 良性 ↔ malignant 恶性，成对记。"
  t example B (135 chars): "今日盘面注解：CTA 仍在"大举做空"短端美债、主动型基金低配，一旦数据不配合，这批极度拥挤的空头就得被迫 recalibrate。职场高频：数据一变就 recalibrate your expectations（把预期重新标定），比 change 显得有依据、有分寸。"
  root example (97 chars): "前缀 bene-(好，拉丁 bene，同源 benefit 好处、benevolent 仁慈的) + 词根 gn(=gen 生、天性，同源 genus 属类) → 天性是好的 → 温和无害、良性"

Then output these three objects:

  story: {{
    "en": "English short paragraph (180-240 words) weaving together ALL 8 of today's words into ONE coherent story about today's real economic/news events. HARD RULE: every one of the 8 words MUST appear inside a [display text|base form] marker (e.g. [surged|surge]); bare words without markers are not allowed. Each word appears exactly once. The story must feel like a real news article: start with a hook, have a middle that connects 2-3 real sub-events, end with a forward-looking conclusion. Exactly 8 markers required.",
    "cn": "Chinese translation of the story"
  }}
  quote: {{
    "en": "One English inspirational sentence, 6-12 words, about learning/growth/persistence",
    "zh": "Chinese translation"
  }}
  preview: {{
    "hook": "Chinese hook paragraph (100-180 chars) weaving ALL 8 of today's words into today's hot-news storyline. HARD RULE: every one of the 8 words MUST appear inside a [display|base] marker, e.g. '市场陷入[动荡|turmoil]' — bare English words are not allowed. Each word exactly once, 8 markers minimum. Structure like a news lede with REAL substance: name the actual event, the actual numbers, the actual institutions (e.g. '今晚20:30美国7月CPI揭晓，0.2%就是[门槛|threshold]'). 2-3 real sub-events connected by causal transitions (一边…另一边…顺带…). Must NOT contain phrases like '8个词' or '记住这8个词'. Start with '今天的故事' or '今天的头条'.",
    "impact": "One Chinese sentence (30-55 chars) that DISTILLS today's real news essence — the actual core takeaway, what is actually at stake today. It must name the concrete substance (e.g. '数据定门槛，谈判在降温，承诺被反悔'). FORBIDDEN empty filler: '直击/助力读懂/解读/见证/背后的关键词/走向/聚焦' and any sentence that says the words 'help you understand' the news. The impact must be readable standalone as a news summary even without the words. Must NOT contain '8个词'."
  }}
  IMPACT STYLE GUIDE (real example from this app's history, do not copy, match the substance):
    impact example: "今天的头条就串成了一句话：'数据定门槛，谈判在降温，承诺被反悔'——覆盖通胀博弈、地缘缓和、科技监管三条主线。"

QUOTE DEDUP RULE:
{quote_instruction}

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

    quote_history = get_quote_history(client)

    result = generate_content(recent_set, quote_history)
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

    # Ensure every today's word appears with [display|base] markers in story.en and preview.hook
    story_en = (output["story"] or {}).get("en", "")
    hook = (output["preview"] or {}).get("hook", "")
    if story_en:
        fixed_story = ensure_markers(story_en, new_words)
        output["story"]["en"] = fixed_story
    if hook:
        fixed_hook = ensure_markers(hook, new_words)
        output["preview"]["hook"] = fixed_hook
    # Ensure each word's t field renders as two paragraphs
    for w in output["words"]:
        if w.get("d") == TODAY and w.get("t"):
            w["t"] = ensure_t_two_paragraphs(w["t"])
    log("Markers ensured for story.en and preview.hook; t two-paragraph ensured")

    content_bytes = json.dumps(output, ensure_ascii=False, indent=2).encode("utf-8")
    success = upload_to_cos(client, content_bytes, "words-data.json")

    # Record today's quote for future dedup
    new_quote = result.get("quote", {})
    if new_quote and new_quote.get("en"):
        append_quote_history(client, {"date": TODAY, "en": new_quote["en"], "zh": new_quote.get("zh", "")})

    if success:
        log("=== DONE ===")
        sys.exit(0)
    else:
        log("=== FAILED (upload) ===")
        sys.exit(1)


if __name__ == "__main__":
    main()
