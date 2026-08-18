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







import xml.etree.ElementTree as ET







from datetime import datetime, timedelta, timezone







from qcloud_cos import CosConfig, CosS3Client















# Configuration (GitHub Actions supplies these via secrets)







DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")







COS_SECRET_ID = os.environ.get("COS_SECRET_ID", "")







COS_SECRET_KEY = os.environ.get("COS_SECRET_KEY", "")







HOSTING_BUCKET = os.environ.get("HOSTING_BUCKET", "")







HOSTING_REGION = os.environ.get("HOSTING_REGION", "")















DEDUP_DAYS = 14







TODAY = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")























def log(msg):







    ts = datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M:%S")







    print(f"[{ts}] {msg}", flush=True)























# RSS 源：用于抓取当天真实新闻标题，喂给 DeepSeek 作为创作素材







NEWS_FEEDS = [







    "https://news.google.com/rss?hl=zh-CN&gl=CN&ceid=CN:zh-Hans",







    "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=zh-CN&gl=CN&ceid=CN:zh-Hans",







    "https://feeds.bbci.co.uk/zhongwen/simp/rss.xml",







    "https://feeds.bbci.co.uk/news/business/rss.xml",







    "https://feeds.bbci.co.uk/news/world/rss.xml",







]























def fetch_today_news(max_items=25):







    """Fetch today's real news headlines from RSS feeds (GitHub Actions runs on overseas servers,







    these feeds are reachable). Returns a dedup list of headlines."""







    titles = []







    for url in NEWS_FEEDS:







        try:







            r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})







            if r.status_code != 200:







                continue







            root = ET.fromstring(r.content)







            for item in root.iter("item"):







                title = (item.findtext("title") or "").strip()







                if title and title not in titles:







                    titles.append(title)







        except Exception as e:







            log(f"RSS fetch failed {url}: {e}")







            continue







        if len(titles) >= max_items:







            break







    log(f"Fetched {len(titles)} real news headlines")







    return titles[:max_items]























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























def _clean_display(m):







    """Turn a word's Chinese meaning into a short tap-friendly display.







    e.g. '(在业绩/表现上)胜过; 超越' -> '胜过'; 'n./v. 激增；汹涌' -> '激增'."""







    if not m:







        return ""







    g = str(m).strip()







    # strip a leading (...) annotation like '(在业绩/表现上)'







    mm = re.match(r"^\([^()]*\)\s*(.*)$", g)







    if mm:







        g = mm.group(1).strip()







    # strip a leading part-of-speech tag like 'n./v.' 'v.' 'adj.'







    mm = re.match(r"^(?:[a-z]{1,5}\.?/?)+\s*", g, re.IGNORECASE)







    if mm:







        g = g[mm.end():].strip()







    g = g.split("；")[0].split(";")[0].split("，")[0].split(",")[0].strip()







    return g























def _inflection_pattern(base):







    """Regex matching a word's base + common English inflections.







    Uses explicit ASCII boundaries (not \\b) because CJK chars ARE word chars in Python 3,







    so \\b fails when an English word sits next to Chinese text.







    Multi-word entries (e.g. 'central bank') are matched literally (no inflections)."""







    if " " in base:







        return re.compile(r"(?<![A-Za-z])" + re.escape(base) + r"(?![A-Za-z])", re.IGNORECASE)







    low = base.lower()







    suffixes = [s for s in ("ing", "ed", "es", "s") if not low.endswith(s)]







    pat = r"(?<![A-Za-z])" + re.escape(base)







    if suffixes:







        pat += r"(?:" + "|".join(suffixes) + r")?"







    pat += r"(?![A-Za-z])"







    return re.compile(pat, re.IGNORECASE)























def _find_occurrences(clean, words, use_english_display=False):







    """Return first non-overlapping occurrence of each word in clean prose.







    Matches the English base/inflection OR the Chinese gloss (first sense).







    When use_english_display is True, the visible text of the marker is the English







    base itself (for the English story); otherwise it is the Chinese gloss (for the







    Chinese preview/hook)."""







    cands = []







    for w in words:







        base = (w.get("w") or "").strip()







        if not base:







            continue







        disp = base if use_english_display else (_clean_display(w.get("m")) or base)







        em = _inflection_pattern(base).search(clean)







        if em:







            cands.append((em.start(), em.end(), disp, base))







            continue







        gloss = _clean_display(w.get("m"))







        if len(gloss) >= 2:







            gm = re.search(re.escape(gloss), clean)







            if gm:







                cands.append((gm.start(), gm.end(), disp, base))







    cands.sort()







    chosen, occupied = [], []







    for (s, e, disp, base) in cands:







        if any(not (e <= os or s >= oe) for (os, oe, _, _) in occupied):







            continue







        chosen.append((s, e, disp, base))







        occupied.append((s, e, disp, base))







    return chosen























def _mark_text(text, words, use_english_display=False):







    """Deterministically wrap every today's word that appears in text as [display|英文原形].







    Used by both build_clean_hook (preview) and story-en marking.







    For the Chinese preview/hook the display is the Chinese gloss; for the English story







    the display is the English base itself (so the English article stays English).







    Returns (marked_text, marked_count).







    """







    if not text:







        return text, 0







    clean = re.sub(r"\[([^\]|]+)\|([^\]]+)\]", r"\1", text)







    chosen = _find_occurrences(clean, words, use_english_display=use_english_display)







    result = clean







    for (s, e, disp, base) in sorted(chosen, reverse=True):







        result = result[:s] + f"[{disp}|{base}]" + result[e:]







    return result, len(chosen)























def build_clean_hook(hook, words):







    """Deterministically rebuild the hook so each today's word that appears in the prose is







    wrapped exactly once as a clean [中文|英文原形] marker. Words absent from the prose are







    simply left out (they still appear on their own card) — NO trailing list is ever appended,







    so the hook stays a clean flowing sentence.















    Strategy:







    1. Unwrap any pre-existing [display|base] markers to plain text -> clean prose.







    2. For each word present in the prose, find its FIRST occurrence and wrap as [英文|中文].







    """







    marked, _ = _mark_text(hook, words, use_english_display=True)







    return marked























def build_clean_story_en(story_en, words):







    """Mark all today's words that appear in the English story dispatch with [英文原形|英文原形]







    markers, so users can tap each English word to hear pronunciation while the article stays







    English (the visible token is the English word, not a Chinese gloss). story.en is a longer







    article — we mark every occurrence we can find. Words absent from the text are left unmarked







    (DeepSeek prompt already asks to cover all 8).







    """







    marked, count = _mark_text(story_en, words, use_english_display=True)







    return marked, count























def build_clean_story_cn(story_cn, words):







    """Mark today's words in the Chinese serial (热点串讲) as [中文释义|英文原形] markers so the







    Chinese story reads cleanly in Chinese and each vocab word is tap-to-speak (English). Default







    display is the Chinese gloss; the front-end renders group1 and speaks group2 (English).







    """







    marked, count = _mark_text(story_cn, words, use_english_display=False)







    return marked, count























def build_clean_impact(impact, words):







    """Mark today's words in the impact summary as [英文|中文] (English display default)."""







    marked, _ = _mark_text(impact, words, use_english_display=True)







    return marked























FORBIDDEN_HOOK_PHRASES = [







    "也值得关注", "收进你的词表", "今天的财经职场里", "记住这8个词",







    "串起了今天", "串起今天的", "关键词——", "关键词——", "这些关键词",







    "提醒我们", "以上就是", "涵盖了今天", "总体看", "总体而言",







]























def hook_covers_words(hook, words, min_count=8):







    """True if the hook prose naturally contains at least min_count of today's words







    (by English base/inflection or Chinese gloss). We require ALL 8 today's words to be woven







    into the flowing sentence (no trailing list — that is caught separately by validate_hook),







    so every word shows up in the headline lead-in."""







    if not hook:







        return False







    text = re.sub(r"\[[^\]|]+\|([^\]]+)\]", r"\1", hook)







    covered = 0







    for w in words:







        base = (w.get("w") or "").strip()







        if not base:







            continue







        if _inflection_pattern(base).search(text):







            covered += 1







            continue







        gloss = _clean_display(w.get("m"))







        if len(gloss) >= 2 and gloss in text:







            covered += 1







    return covered >= min_count























def _hook_cover_count(result):







    """How many of today's words appear in the hook prose (used to pick the best candidate)."""







    if not result:







        return -1







    hook = (result.get("preview") or {}).get("hook") or ""







    words = result.get("words") or []







    text = re.sub(r"\[[^\]|]+\|([^\]]+)\]", r"\1", hook)







    c = 0







    for w in words:







        base = (w.get("w") or "").strip()







        if base and _inflection_pattern(base).search(text):







            c += 1







            continue







        gloss = _clean_display(w.get("m"))







        if len(gloss) >= 2 and gloss in text:







            c += 1







    return c























def _strip_forbidden(hook):







    """Fallback sanitizer: when even retries can't pass the quality gate, cut the cliché summary







    (the forbidden canned phrase and everything after it up to the sentence end) so the deployed







    hook at least has no '这些关键词串起今天…' style trailing list."""







    if not hook:







        return hook







    positions = [hook.find(p) for p in FORBIDDEN_HOOK_PHRASES if p in hook]







    worst = max(positions) if positions else -1







    if worst >= 0:







        end = re.search(r"[。！？]", hook[worst:])







        hook = hook[:worst] + (hook[worst + end.end():] if end else "")







    hook = re.sub(r"[：:]\s*$", "", hook)







    hook = re.sub(r"[，,、]{2,}", "，", hook)

    # 兜底：若裁剪后 hook 变成半截句（不以句号/叹号/问号/省略号收尾），
    # 裁掉最后一个终止标点之后的内容，保证上线文案永远是一个完整句。
    plain = re.sub(r"\[[^\]|]+\|([^\]]+)\]", r"\1", hook)
    if plain and not re.search(r'[。！？…][」』”’）)]*\s*$', plain):
        raw_ends = [i for i, ch in enumerate(hook) if ch in "。！？…"]
        if raw_ends:
            hook = hook[: raw_ends[-1] + 1]
        else:
            hook = re.sub(r"[——\-—\s]*$", "", hook)

    return hook.strip()







    return hook.strip()























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























def validate_core(result, min_cn=310, max_cn=400):







    """Quality gate for the core call (words + story + quote)."""







    if not result:







        return False







    words = result.get("words", [])







    if len(words) != 8:







        return False







    # Every word must have a non-empty ASCII base form, a non-empty Chinese meaning, and a valid domain







    for w in words:







        base = (w.get("w") or "").strip()







        if not base:







            return False







        if not re.match(r"^[A-Za-z-]+$", base):







            return False







        if not (w.get("m") or "").strip():







            return False







        if w.get("c") not in ("econ", "work", "news", "politics"):







            return False







    from collections import Counter







    cnt = Counter(w.get("c") for w in words)







    work = cnt.get("work", 0)







    econ = cnt.get("econ", 0)







    politics = cnt.get("politics", 0)







    if work < 2:







        return False







    if econ + work < 5:







        return False







    if politics > 1:







        return False







    story = result.get("story", {})







    cn = (story.get("cn") or "").strip()







    # Length measured on PLAIN text (markers stripped) so build_clean's [中文|英文] wrappers







    # don't inflate the count; target ~350, hard cap 400.







    cn_plain = re.sub(r"\[[^\]|]+\|([^\]]+)\]", r"\1", cn)







    if len(cn_plain) < min_cn or len(cn_plain) > max_cn:







        return False







    return True























def validate_hook(preview, words, min_len=140, max_len=340, min_words=8):







    """Quality gate for the hook: clean flowing sentence, no canned list, covers >= min_words."""







    if not preview:







        return False







    hook = (preview.get("hook") or "").strip()







    # Length is measured on the PLAIN text (markers stripped) — markers are a display-layer







    # decoration added by build_clean_hook and must not count toward the word budget.







    hook_plain = re.sub(r"\[[^\]|]+\|([^\]]+)\]", r"\1", hook)

    # 完整句校验：hook 必须以句号/叹号/问号/省略号等终止标点收尾（允许带闭合引号/括号）。
    # 防止 AI 输出半截句（如 "……今天的头条，正是" 无下文）被当作合格内容上线——
    # 残缺句会让前端文案看起来被截断，必须在这里拦截并触发重试。
    if not re.search(r'[。！？…][」』”’）)]*\s*$', hook_plain):
        return False







    if len(hook_plain) < min_len or len(hook_plain) > max_len:







        return False







    if any(p in hook for p in FORBIDDEN_HOOK_PHRASES):







        return False







    # Detect trailing marker LIST: 3+ [..|..] markers separated ONLY by 、/， (no other words between) —







    # this is a comma-separated word dump like [A|a]、[B|b]、[C|c]







    pure_list = re.search(r'\[[^\]|]+\|[^\]]+\][、，]\s*\[[^\]|]+\|[^\]]+\][、，]\s*\[[^\]|]+\|[^\]]+\]', hook)







    if pure_list:







        return False







    # Also reject if the last 50 chars contain 4+ markers (words dumped at tail)







    tail = hook[-50:] if len(hook) > 50 else hook







    if len(re.findall(r'\[[^\]|]+\|[^\]]+\]', tail)) >= 4:







        return False







    if not hook_covers_words(hook, words, min_count=min_words):







        return False







    return True























def validate_quality(result, min_cn=310, max_cn=400):







    """Final combined quality gate (core + hook)."""







    if not result:







        return False







    if not validate_core(result, min_cn, max_cn):







        return False







    if not validate_hook(result.get("preview"), result.get("words")):







        return False







    return True























def explain_quality(result):







    """Print why a generation failed the quality gate (for debugging)."""







    if not result:







        log("  reason: result is None/empty")







        return







    words = result.get("words", [])







    from collections import Counter







    cnt = Counter(w.get("c") for w in words)







    log(f"  words={len(words)} domain={dict(cnt)} work={cnt.get('work',0)} econ+work={cnt.get('econ',0)+cnt.get('work',0)} politics={cnt.get('politics',0)}")







    cn = (result.get("story") or {}).get("cn") or ""







    log(f"  story.cn len={len(cn)} (want 310-400)")







    hook = (result.get("preview", {}) or {}).get("hook") or ""







    forbidden = [p for p in FORBIDDEN_HOOK_PHRASES if p in hook]







    if forbidden:







        log(f"  hook forbidden phrases: {forbidden}")







    if not hook_covers_words(hook, words, min_count=8):







        log(f"  hook covers fewer than 6 of the 8 words")







    en = (result.get("story") or {}).get("en") or ""







    for name, text in (("hook", hook), ("story.en", en)):







        bases = re.findall(r"\[[^\]|]+\|([^\]]+)\]", text)







        dup = len(bases) - len(set(bases))







        log(f"  {name} markers={len(bases)} unique={len(set(bases))} duplicates={dup}")























def call_deepseek(prompt, max_retries=3, max_tokens=6000):







    url = "https://api.deepseek.com/chat/completions"







    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}







    payload = {







        "model": "deepseek-chat",







        "messages": [







            {"role": "system", "content": "You are an expert IELTS vocabulary tutor and a financial/economy news editor. Always respond with strict JSON only, no markdown, no extra text."},







            {"role": "user", "content": prompt}







        ],







        "temperature": 0.7,







        "max_tokens": max_tokens







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























def _build_context(recent_set, quote_history, news_headlines):







    """Shared context builders for both generation calls."""







    avoid = ", ".join(sorted(recent_set)[:50]) if recent_set else "none"







    quote_history = quote_history or []







    if quote_history:







        recent_quotes = [q.get("en", "") for q in quote_history[-60:]]







        quote_avoid = "\n".join("  - " + q for q in recent_quotes[-15:])







        quote_instruction = f"Your quote.en MUST NOT be similar to any of these recent quotes (avoid same sentence structure, keywords, and meaning):\n{quote_avoid}"







    else:







        quote_instruction = "No prior quotes to avoid; just make it fresh and original."















    news_headlines = news_headlines or []







    if news_headlines:







        news_block = "\n".join("  - " + h for h in news_headlines)







        news_instruction = f"""The following are REAL news headlines from TODAY (fetched live). Use them as your ONLY source of facts for t/story/hook/impact. Pick the most relevant 2-4 for each word. NEVER invent events, names, numbers, or dates that are not in these headlines. If a fact is not in the headlines, do not claim it.\n\nTODAY'S REAL NEWS HEADLINES:\n{news_block}"""







    else:







        news_instruction = "No live news could be fetched today. In that case: DO NOT invent fake specific events (fake company names, fake numbers, fake dated events like '2026年8月希腊'). Instead use generic current-topic references WITHOUT specific fabricated facts (e.g. '全球通胀降温的背景下'), and never write a fake dated news item."







    return avoid, quote_instruction, news_instruction























def generate_content(recent_set, quote_history=None, news_headlines=None):







    """Single call: generate the 8 words + story + quote + preview (hook/impact).







    The hook is written as a clean flowing sentence; markers are inserted deterministically afterwards."""







    avoid, quote_instruction, news_instruction = _build_context(recent_set, quote_history, news_headlines)







    prompt = f"""Today is {TODAY}. Your job: act as both an IELTS vocabulary editor AND a financial news writer.















Generate EXACTLY 8 English words (IELTS 6.5+/CEFR C1, not below CET-6), each must be strongly tied to CURRENT real hot topics as of {TODAY}.















DOMAIN MIX (USER PRIORITY: 财经新闻 + 职场 are the FOCUS):







- PRIMARY — must be 6-8 of the 8 words: econ (财经/商业/市场) and work (职场/就业/办公) TOGETHER at least 5 (suggest econ 3-4, work 2-3). The day's words should orbit "财经、商业、市场、职场、贸易、科技商业、就业".







- ALLOWED but limited: news (经济/商业相关新闻) at most 2.







- RESTRICTED: politics at most 1, and ONLY if it is economy/trade/tariff/central-bank related (e.g. trade talks, sanctions, rate decisions). FORBIDDEN: pure military conflict, geopolitics-for-its-own-sake, social-livelihood, entertainment, sports soft news.







- FORBIDDEN filler: entertainment, sports, pure social-livelihood, pure military-conflict words — do NOT pick them just to fill the 8.















GEOGRAPHY: Prefer real stories from the US, EU, Japan/Korea, and China, especially business/market/workplace angles.















IMPORTANT: Do NOT use these words that were learned recently: {avoid}















IMPORTANT FACTUALITY RULE (highest priority):







{news_instruction}















For EACH word, output ALL of these fields (every field is required). Use the "STYLE GUIDE" below to match the expected depth and length:















  w: word (base form, ASCII only, no spaces — use a single base word)







  ph: /IPA pronunciation/







  m: 8-24 Chinese chars. Concise Chinese meaning with a part-of-speech hint if multiple senses, e.g. "(冲突)升级；逐步扩大". MUST be non-empty.







  c: econ | work | news | politics  (财经/职场主导：econ+work 合计 5-7 个; news 经济商业相关最多 2; politics 经济贸易相关最多 1; 禁止 entertainment/sports/纯社会民生)







  pos: v. / n. / adj. / adv.







  en: 30-70 English chars. Plain-English definition, can include multiple senses joined by semicolons, and the typical context (e.g. "in medical/economic contexts")







  defs: array of 2-3 sense objects, each {{"cn": "Chinese sense, 6-18 chars", "en": "English gloss, 20-50 chars", "pos": "v./n./adj."}}. Cover the word's DISTINCT senses (literal, figurative, common collocation) — do NOT just repeat m. The FIRST object's cn MUST equal or start with m. Example for "escalate": [{{"cn":"(冲突)升级","en":"to increase in intensity, esp. conflict","pos":"v."}},{{"cn":"逐步扩大","en":"to grow or expand step by step","pos":"v."}},{{"cn":"使(武器/冲突)升级","en":"to escalate weapons or a dispute","pos":"v."}}]







  col: 40-90 chars total. 3-4 common collocations separated by " · ", each ideally with Chinese gloss in parentheses, e.g. "cross a threshold 越过临界点 · pain threshold 痛感阈值 · threshold for action 行动门槛"







  ex: 60-130 English chars. ONE natural English sentence that uses the word (inflected ok), and reflects a real current news/economic event or plausible scenario







  exZh: 14-50 Chinese chars. Natural Chinese translation of the example







  ex2: 60-130 English chars. A SECOND natural sentence using the word in a DIFFERENT context than ex.







  ex2Zh: 14-50 Chinese chars. Chinese translation of ex2.







  t: 90-140 Chinese chars, in TWO paragraphs separated by a newline. Paragraph 1 (50-80 chars): tie the word to a SPECIFIC real current event (economy/news/workplace/politics/entertainment/sports), with names, numbers, and the word used in context. Paragraph 2 (40-60 chars): give a workplace/daily-life example sentence, OR contrast with a related word (e.g. antonym, near-synonym, noun/verb form), OR a memorable image/association to lock the word in memory. Both paragraphs required.







  root: 60-100 Chinese chars. Full affix breakdown with: prefix/root/suffix separated by " + ", each component's source language (拉丁/希腊/古英语/法语/阿拉伯语 etc.), at least one cognate word per component, then a short arrow showing etymology-to-modern-meaning evolution. Example format: "前缀 bene-(好, 拉丁, 同源 benefit/benevolent) + 词根 gn(=gen 生, 同源 genus) → 天性是好的 → 温和无害、良性". If the word has no clear affixes, briefly explain its etymology origin in Chinese.















STYLE GUIDE - match this level of detail (these are real examples from this app's history, do not copy, just match the depth):















  t example A (109 chars): "高盛押注今晚"鸽派惊喜"、汇丰预判数据"温和"，一旦成真就是市场最爱的 Goldilocks 行情，几乎所有资产普涨。医院报告里的"良性"也是这个词：benign 良性 ↔ malignant 恶性，成对记。"







  t example B (135 chars): "今日盘面注解：CTA 仍在"大举做空"短端美债、主动型基金低配，一旦数据不配合，这批极度拥挤的空头就得被迫 recalibrate。职场高频：数据一变就 recalibrate your expectations（把预期重新标定），比 change 显得有依据、有分寸。"







  root example (97 chars): "前缀 bene-(好，拉丁 bene，同源 benefit 好处、benevolent 仁慈的) + 词根 gn(=gen 生、天性，同源 genus 属类) → 天性是好的 → 温和无害、良性"















Then output these three objects:















  story: {{







    "en": "English news dispatch, 约 500 字符（480-560，roughly 90-130 words）。This is NOT a rephrase of the hook — it's a tight news article: an opening lede naming the lead event, a middle covering 2-3 real sub-events with specific names, numbers, and institutions, and a one-sentence forward-looking close. Cover ALL 8 of today's words using [display|base] markers, each exactly ONCE, in BASE form (no precipitates/precipitating variants — use the base like precipitate). No bare words. 480-560 characters.",







    "cn": "中文热点综述（『热点串讲』板块主体），独立成篇，严格约 350 字（320-380 字为佳，不足 310 或超 400 不合格）。结构：①导语 1 句约 50 字总起今日财经/职场主线；②主体 3 段，每段 80-110 字，分别展开一条具体主线（如某市场/资产动向、某行业或公司进展、职场/科技趋势），并把今日 8 个词自然写入叙述（中文显示即可，如『激增』『部署』『权衡』『韧性』『颠覆性的』）；③收束 1 句约 40 字收尾。这是中文原创综述，不是英文的翻译，不要逐词对应英文。\n\n重要：正文结尾不要写『总体看/总体而言』之类的总结句——串讲本身就是叙事，自然收束即可；统一的总结由 preview.impact 承担，不要在这里重复。















长度结构参考样例（约 340 字，仅学长度与结构，勿抄内容）：今日市场被三条线索牵动。其一是地缘与能源：某海峡通航新模式公布，国际油价变得『不稳定』，投资者在回报与安全间做『权衡』。其二是企业基本面：某龙头净利下滑，但子业务『激增』成新增长极；某地调高经济预期，尽显『韧性』底色。其三是职场与科技：AI 『颠覆性』浪潮持续，企业加速『部署』工具以『优化』流程，员工被迫升级技能；央行在通胀与增长间『授权』权衡，政策空间受限，三条主线交织，机会藏在分化里。"







  }}







  quote: {{







    "en": "One English inspirational sentence, 6-12 words, about learning/growth/persistence",







    "zh": "Chinese translation"







  }}







  preview: {{







    "hook": "中文『导语』一句话（150-300 字），围绕今日财经/职场主线，自然地把今天这批词里的 6-8 个串进这句话。**必须中英夹杂写**——英文原词直接嵌入中文句子里（如『市场的 volatility 让人紧张』『资金 influx 持续』『票房 surge』『企业加速 deploy AI』『lucrative 岗位 outperform 传统岗』），不要全翻译成中文。以『今天的故事』或『今天的头条』开头，像讲故事一样有起承转合：先说一个具体场景/事件（带数字或名字更好），再展开关联动向，最后给一个贴切的判断或启示。词要散落在句子各处，绝对禁止在句尾用顿号/逗号罗列词汇（如禁止『这些关键词——A、B、C——串起了…』『关键词A、B、C涵盖了今天…』『提醒我们…』这类清单式结尾）。结尾的判断/总结句也必须是中英夹杂、继续把今天这批词嵌进叙事（可复用前句已出现的词，比如把『复苏/部署/胜过』之一写进收尾），绝对禁止出现纯中文、不带任何新词的收尾升华句。不要自己加 [中文|英文] 标记（生成后自动添加）。**关键：两个英文单词之间必须至少有一个中文字或标点/空格隔开，绝对不能紧挨着写在一起（如禁止 automateprocurement，必须是 automate procurement 或 automate 的 procurement）。**",







    "impact": "One Chinese sentence (30-55 chars) that DISTILLS today's real news essence — the actual core takeaway. **必须中英夹杂**——把今天这批词里的若干个英文原词直接嵌入中文句子里（如『市场volatility放大』『资本influx涌动』『行业resurgence复兴』），不要全翻译成中文。FORBIDDEN empty filler: '直击/助力读懂/解读/见证/背后的关键词/走向/聚焦' and any sentence that says 'help you understand'. Must NOT contain '8个词'. 不要自己加标记（生成后自动添加）。**关键：两个英文单词之间必须至少有一个中文字或标点/空格隔开，绝对不能紧挨着写在一起。**"







  }}







  IMPACT STYLE GUIDE (real example from this app's history, do not copy, match the substance):







    impact example: "今天的头条就串成了一句话：'数据定门槛，谈判在降温，承诺被反悔'——覆盖通胀博弈、地缘缓和、科技监管三条主线。"















QUOTE DEDUP RULE:







{quote_instruction}















FINAL FORMAT RULE: preview.hook 必须是通顺的一句话、把今天这批词尽量都自然融入叙事，不要以『总体看/总体而言』这类总结句收尾（统一总结由 preview.impact 承担），禁止句尾清单式罗列；不要写任何 [..|..] 标记（自动添加）。story.cn 必须严格约 350 字（320-380 字为佳，不足 310 或超 400 不合格），且结尾不要写『总体看/总体而言』总结句（总结由 impact 承担）。















Output STRICT JSON only, no markdown, exactly this shape:







{{"words":[8 word objects],"story":{{"en":"...","cn":"..."}},"quote":{{"en":"...","zh":"..."}},"preview":{{"hook":"...","impact":"..."}}}}







"""







    return call_deepseek(prompt, max_tokens=8000)























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























def upload_frontend(client):







    """Upload the static frontend (index.html) from the repo checkout to the hosting bucket."""







    _fe_dir = os.path.dirname(os.path.abspath(__file__))







    _fe_path = os.path.join(_fe_dir, "index.html")







    if os.path.exists(_fe_path):







        try:







            with open(_fe_path, "rb") as _f:







                _fe_bytes = _f.read()







            upload_to_cos(client, _fe_bytes, "index.html")







            log("Frontend index.html uploaded to hosting bucket")







        except Exception as _e:







            log(f"Frontend upload skipped: {_e}")







    else:







        log("index.html not found in repo; skipping frontend upload")







def main():







    log(f"=== Daily Word Generation {TODAY} ===")















    if not all([DEEPSEEK_API_KEY, COS_SECRET_ID, COS_SECRET_KEY, HOSTING_BUCKET, HOSTING_REGION]):







        log("ERROR: Missing required environment variables")







        sys.exit(1)















    client = get_cos_client()











    # Pinned mode: if the repo carries today's words-data.json (user pinned words on purpose),



    # upload it as-is (plus frontend) and skip DeepSeek generation.



    _pin_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "words-data.json")



    if os.path.exists(_pin_path):



        try:



            _pin = json.loads(open(_pin_path, encoding="utf-8").read())



            _pin_today = [w for w in _pin.get("words", []) if w.get("d") == TODAY]



            if _pin.get("updated_on") == TODAY and len(_pin_today) == 8:



                log("Pinned words-data.json for TODAY found; uploading as-is (skip generation)")



                _pb = json.dumps(_pin, ensure_ascii=False, indent=2).encode("utf-8")



                upload_to_cos(client, _pb, "words-data.json")



                upload_frontend(client)



                log("=== DONE (pinned) ===")



                sys.exit(0)



        except Exception as _e:



            log(f"Pinned check skipped: {_e}")















    existing_words, existing_data = get_existing_words(client)
    # Idempotency guard: if today's 8 words already exist, skip (avoid double-run overwrites).
    if existing_data.get("updated_on") == TODAY:
        _today_cnt = len([w for w in existing_words if w.get("d") == TODAY])
        if _today_cnt == 8:
            log(f"Today {TODAY} already has {_today_cnt} words; skipping generation.")
            sys.exit(0)
    log(f"Existing words: {len(existing_words)}")















    recent_set = get_recent_words(existing_words)















    quote_history = get_quote_history(client)















    news_headlines = fetch_today_news()















    result = None







    best = None          # best-effort candidate kept across retries (most words covered)







    best_score = -1







    for attempt in range(8):







        r = generate_content(recent_set, quote_history, news_headlines)







        if validate_quality(r):







            result = r







            log(f"Quality check passed on attempt {attempt+1}")







            break







        else:







            log(f"Quality check FAILED on attempt {attempt+1}, regenerating...")







            explain_quality(r)







            score = _hook_cover_count(r)







            if score > best_score:







                best_score = score







                best = r







    if not result:







        log("WARNING: quality not met after 8 retries; deploying best-effort (forbidden stripped)")







        if best:







            hook = (best.get("preview") or {}).get("hook") or ""







            best["preview"]["hook"] = _strip_forbidden(hook)







            result = best







        else:







            log("ERROR: no generation produced at all")







            sys.exit(1)







    if not result or not result.get("words"):







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















    # Deterministically rebuild hook and story.en so every today's word that appears in the







    # prose is wrapped as a clean [中文|英文原形] marker (tap to hear pronunciation).







    hook = (output["preview"] or {}).get("hook", "")







    if hook:







        output["preview"]["hook"] = build_clean_hook(hook, new_words)







    impact = (output["preview"] or {}).get("impact", "")







    if impact:







        output["preview"]["impact"] = build_clean_impact(impact, new_words)







    story_en = ((output.get("story") or {}).get("en") or "")







    if story_en:







        marked_en, en_count = build_clean_story_en(story_en, new_words)







        output["story"]["en"] = marked_en







        log(f"  story.en markers={en_count}")







    story_cn = ((output.get("story") or {}).get("cn") or "")







    if story_cn:







        marked_cn, cn_count = build_clean_story_cn(story_cn, new_words)







        output["story"]["cn"] = marked_cn







        log(f"  story.cn markers={cn_count}")







    # Ensure each word's t field renders as two paragraphs







    for w in output["words"]:







        if w.get("d") == TODAY and w.get("t"):







            w["t"] = ensure_t_two_paragraphs(w["t"])







    hb = re.findall(r"\[[^\]|]+\|([^\]]+)\]", (output.get("preview") or {}).get("hook", "") or "")







    eb = re.findall(r"\[[^\]|]+\|([^\]]+)\]", (output.get("story") or {}).get("en", "") or "")







    log(f"Post-build hook markers={len(hb)} unique={len(set(hb))}; story.en markers={len(eb)} unique={len(set(eb))}; story.cn len={len((output.get('story') or {}).get('cn') or '')}")







    if len(hb) != 8 or len(set(hb)) != 8:







        log("WARNING: hook marker count is not exactly 8 unique — review build_clean_hook")







    log("Hook rebuilt deterministically; t two-paragraph ensured")















    content_bytes = json.dumps(output, ensure_ascii=False, indent=2).encode("utf-8")







    success = upload_to_cos(client, content_bytes, "words-data.json")















    # Also push the static frontend so UI edits go live via GitHub push



    upload_frontend(client)















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







