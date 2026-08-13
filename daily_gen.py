#!/usr/bin/env python3
"""
每日单词生成脚本（GitHub Actions 用）
1. 调 DeepSeek API 生成 8 个雅思单词 + story + quote + preview
2. 上传 words-data.json 到 CloudBase 静态托管（COS）
3. 维护去重历史（quote-history.jsonl 存在 COS 上）
"""

import os, json, time, hashlib, re, sys
from datetime import datetime, timezone, timedelta
import requests

# ── 环境变量 ──────────────────────────────────────────────
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
COS_SECRET_ID = os.environ.get("COS_SECRET_ID", "")
COS_SECRET_KEY = os.environ.get("COS_SECRET_KEY", "")
BUCKET = os.environ.get("HOSTING_BUCKET", "")       # 0313-static-wordstudy-xxx
REGION = os.environ.get("HOSTING_REGION", "ap-shanghai")

# ── 常量 ───────────────────────────────────────────────────
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
COS_HOST = f"{BUCKET}.cos.{REGION}.myqcloud.com"
DATA_KEY = "words-data.json"          # 静态托管上的数据文件
HISTORY_KEY = "quote-history.jsonl"   # quote 去重历史

BJ_TZ = timezone(timedelta(hours=8))
TODAY = datetime.now(BJ_TZ).strftime("%Y-%m-%d")


def log(msg):
    print(f"[{TODAY}] {msg}", flush=True)


# ── 1. 获取现有数据（用于去重 & 追加）──────────────────────
def fetch_cos_data():
    """从 COS 获取现有的 words-data.json，失败返回空 dict"""
    # 简单 GET 请求（公开读的静态托管桶不需要签名）
    url = f"https://{COS_HOST}/{DATA_KEY}"
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log(f"获取现有数据失败（将新建）: {e}")
    return {"words": [], "updated_on": TODAY}


def fetch_quote_history():
    """从 COS 获取 quote 去重历史"""
    url = f"https://{COS_HOST}/{HISTORY_KEY}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            lines = [l for l in r.text.strip().split("\n") if l.strip()]
            return lines[-60:]  # 只保留最近 60 条去重
    except:
        pass
    return []


# ── 2. 构建最近单词列表（给 DeepSeek 做去重参考）───────────
def build_recent_words(existing_data):
    """提取最近 30 天的单词列表"""
    words = existing_data.get("words", [])
    recent = []
    seen = set()
    # 按日期倒序，取最近的不重复词
    for w in reversed(words):
        wd = w.get("w", "")
        if wd and wd not in seen:
            recent.append(wd)
            seen.add(wd)
        if len(recent) >= 50:
            break
    return recent[:30]  # 给 DeepSeek 最近 30 个做参考


def build_quote_history_text(history_lines):
    """把 quote 历史变成文本供 DeepSeek 参考"""
    entries = []
    for line in history_lines[-40:]:
        try:
            entry = json.loads(line)
            entries.append(entry.get("en", ""))
        except:
            pass
    return "\n".join(entries) if entries else "(无历史)"


# ── 3. 调 DeepSeek 生成 ───────────────────────────────────
def call_deepseek(prompt, max_tokens=4000):
    """调用 DeepSeek API"""
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    body = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "你是一个专业的英语词汇教学助手。输出必须严格符合要求的 JSON 格式，不要输出任何其他文字。"},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": max_tokens,
        "temperature": 0.85
    }
    r = requests.post(DEEPSEEK_URL, headers=headers, json=body, timeout=120)
    r.raise_for_status()
    data = r.json()
    content = data["choices"][0]["message"]["content"]
    # 提取 JSON（可能被 markdown 包裹）
    match = re.search(r'\{[\s\S]*\}', content)
    if match:
        return json.loads(match.group())
    raise ValueError(f"DeepSeek 返回的不是有效 JSON: {content[:200]}")


def generate_daily_words(recent_words, quote_history_text):
    """构建 prompt 并调用 DeepSeek 生成当日数据"""
    recent_str = ", ".join(recent_words) if recent_words else "(无历史)"

    prompt = f"""请为「每日雅思单词」应用生成今天({TODAY})的数据。要求：

## 1. 单词（固定 8 个，恰好 8 个）
生成 8 个实用英语单词，难度雅思 6.5+ (CEFR C1)，涉及经济/新闻/职场。
- 近期已学过的词不要重复：{recent_str}
- 若候选不足，优先选更早学过的词，务必凑满恰好 8 个。

每个单词格式：
{{"w":"单词","ph":"/音标/","m":"中文词义","c":"主题(econ/news/work)","d":"{TODAY}","ex":"英文例句(含该词变形)","exZh":"中文翻译","t":"贴合热点的记忆小提示(一句话)","root":"词根词缀拆解"}}

## 2. 今日热点串讲 story
{{"en":"英文短文(200字内)，用[显示文本|原形]标记2-4个当天单词","cn":"中文翻译"}}

## 3. 每日励志语 quote
- 主题：学习/成长/坚持，雅思水平，6-12 词
- 不要和以下历史重复（句式和用词都要不同）：
{quote_history_text}

## 4. preview 预览对象
{{"hook":"一小段话把当天单词串进热点语境(不要出现'这8个词''记住这8个词'等说法，用'今天的故事''今天的头条')","impact":"一句话总结与新闻主线的关联"}}

## 输出格式（严格 JSON，不要其他文字）：
{{"words":[...8个单词对象...],"story":{{...}},"quote":{{"en":"...","zh":"..."}},"preview":{{...}}}}"""

    log("正在调用 DeepSeek 生成...")
    result = call_deepseek(prompt)
    log(f"DeepSeek 返回成功，words 数量: {len(result.get('words', []))}")
    return result


# ── 4. 合并数据 ───────────────────────────────────────────
def merge_data(existing_data, new_data):
    """把新生成的数据合并到现有数据中"""
    existing_data["updated_on"] = TODAY

    # 追加新单词到 words 数组
    new_words = new_data.get("words", [])
    existing_data.setdefault("words", []).extend(new_words)

    # 更新 story / quote / preview
    if new_data.get("story"):
        existing_data["story"] = new_data["story"]
    if new_data.get("quote"):
        existing_data["quote"] = new_data["quote"]
    if new_data.get("preview"):
        existing_data["preview"] = new_data["preview"]

    return existing_data


# ── 5. 上传到 COS（CloudBase 静态托管）───────────────────
def upload_to_cos(key, data_str, content_type="application/json"):
    """通过 COS REST API 上传文件（简单 PUT）"""
    url = f"https://{COS_HOST}/{key}"

    # 计算 Authorization（HMAC-SHA1 签名）
    now = int(time.time())
    # 简化签名（实际生产应使用 SDK 或完整签名逻辑）
    # 这里用预签名 URL 方式或直接 SDK 更安全
    # 但 GitHub Actions 环境没有 cos-sdk，我们用 requests 直接签
    import hmac, hashlib

    # COS 签名 v5 太复杂，改用临时密钥方式或简化方案
    # 实际上对于公开写桶，我们可以用更简单的方式
    # 这里改用腾讯云 STS 临时凭证方式太复杂
    # 改用最简方案：直接用 SecretId/SecretKey 做 HMAC-SHA1 签名

    http_method = "put"
    path = f"/{key}"
    sign_time = f"{now};{now + 3600}"
    key_time = f"{now - 86400 * 180};{now + 86400 * 180}"

    # SignKey
    sign_key = hmac.new(
        ("TC3" + COS_SECRET_KEY).encode(),
        key_time.encode(),
        hashlib.sha256
    ).hexdigest()

    # HttpString
    http_string = f"{http_method}\n{path}\n\n\ncontent-type={content_type}\n"
    string_to_sign = f"sha256\n{sign_time}\n{hashlib.sha256(http_string.encode()).hexdigest()}"

    # Signature
    signature = hmac.new(
        sign_key.encode(),
        string_to_sign.encode(),
        hashlib.sha256
    ).hexdigest()

    authorization = (
        f"q-sign-algorithm=sha1&q-ak={COS_SECRET_ID}&q-sign-time={sign_time}"
        f"&q-key-time={key_time}&q-header-list=&q-url-param-list="
        f"&q-signature={signature}"
    )

    headers = {
        "Authorization": authorization,
        "Content-Type": content_type,
        "Host": COS_HOST,
        "x-cos-security-token": "",
    }

    r = requests.put(url, headers=headers, data=data_str.encode(), timeout=30)
    r.raise_for_status()
    log(f"上传 {key} 成功 ({len(data_str)} bytes)")


def upload_simple(key, data_str, content_type="application/json"):
    """备用上传方法：如果 COS 签名失败，尝试用更简单的方式"""
    url = f"https://{COS_HOST}/{key}"
    # 尝试匿名 PUT（某些配置允许）
    try:
        r = requests.put(url, data=data_str.encode(), headers={
            "Content-Type": content_type
        }, timeout=30)
        if r.status_code in (200, 204):
            log(f"上传 {key} 成功")
            return True
    except:
        pass
    return False


# ── 主流程 ─────────────────────────────────────────────────
def main():
    log("=" * 50)
    log("开始每日单词生成")

    # 校验环境变量
    missing = [k for k in ["DEEPSEEK_API_KEY", "COS_SECRET_ID", "COS_SECRET_KEY", "BUCKET"] if not globals().get(k.replace("BUCKET", "BUCKET").replace("DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"))]
    # 简化校验
    if not all([DEEPSEEK_API_KEY, COS_SECRET_ID, COS_SECRET_KEY, BUCKET]):
        log("❌ 缺少必要的环境变量！")
        sys.exit(1)

    # 1. 获取现有数据
    existing = fetch_cos_data()
    recent_words = build_recent_words(existing)
    log(f"现有单词总数: {len(existing.get('words', []))}, 近期不重复: {len(recent_words)}")

    # 2. 获取 quote 历史
    history = fetch_quote_history()
    quote_hist_text = build_quote_history_text(history)

    # 3. 调 DeepSeek 生成
    try:
        new_data = generate_daily_words(recent_words, quote_hist_text)
    except Exception as e:
        log(f"❌ DeepSeek 生成失败: {e}")
        sys.exit(1)

    # 校验单词数量
    words = new_data.get("words", [])
    if len(words) != 8:
        log(f"⚠️ 单词数量不是 8（实际 {len(words)}），但继续保存")

    # 4. 合并数据
    merged = merge_data(existing, new_data)
    data_json = json.dumps(merged, ensure_ascii=False, indent=2)

    # 5. 上传 words-data.json
    try:
        upload_to_cos(DATA_KEY, data_json)
    except Exception as e:
        log(f"COS 签名上传失败: {e}，尝试备用方式...")
        if not upload_simple(DATA_KEY, data_json):
            log("❌ 所有上传方式都失败了！")
            sys.exit(1)

    # 6. 追加 quote 历史
    quote = new_data.get("quote", {})
    if quote.get("en"):
        history_entry = json.dumps({"date": TODAY, "en": quote["en"], "zh": quote.get("zh", "")}, ensure_ascii=False)
        history.append(history_entry)
        history_text = "\n".join(history[-80:])  # 保留最近 80 条
        try:
            upload_to_cos(HISTORY_KEY, history_text, "text/plain; charset=utf-8")
        except Exception as e:
            log(f"⚠️ 上传 quote 历史失败（不影响主流程）: {e}")

    # 7. 完成
    log(f"✅ 完成！今日 {len(words)} 个词已上线")
    log(f"   访问: https://wordstudy-d1gh0i7r67e8a64e8-1463809286.tcloudbaseapp.com")
    print(json.dumps({"ok": True, "date": TODAY, "words": len(words)}))


if __name__ == "__main__":
    main()   sys.exit(1)


# 1. Retrieve existing data
existing = fetch_cos_data()
recent_words = build_recent_words(existing)
log(f"当前存在的单词总数：{len(existing.get('words', []))}，近期不重复出现的单词数：{len(recent_words)}")


# 2. Retrieving the quote history
history = fetch_quote_history()
quote_hist_text = build_quote_history_text(history)


# 3. Using DeepSeek to generate results
try:
new_data = generate_daily_words(recent_words, quote_hist_text)
Except for exceptions like the Exception class, as e:
log(f"❌ DeepSeek generation failed: {e}")
sys.exit(1)


# Verifying the number of words
words = new_data.get("words", [])
if len(words) != 8:
log(f"⚠️ The number of words is not 8 (actually {len(words)}), but we will continue saving the data")


# 4. Combining data
merged = merge_data(existing, new_data)
data_json = json.dumps(merged, ensure_ascii=False, indent=2)


# 5. Uploading words-data.json file
try:
upload_to_cos(DATA_KEY, data_json)
Except for exceptions like the Exception class, e:
log(f)“COS 签名上传失败: {e”，尝试备用方法……”)
if not upload_simple(DATA_KEY, data_json):
log("❌ All upload methods have failed!")
sys.exit(1)


# 6. Additional quote history
quote = new_data.get("quote", {})
if quote.get("en"):
history_entry = json.dumps({"date": TODAY, "en": quote["en"], "zh": quote.get("zh", "")}, ensure_ascii=False)
history.append(history_entry)
history_text = "\n".join(history[-80:])  # Keep the last 80 items in the history list
try:
upload_to_cos(HISTORY_KEY, history_text, "text/plain; charset=utf-8")
Except Exception as e:
log(f"⚠️ Uploading the quote history failed (does not affect the main process): {e}")


# 7. Completed
log(f"✅ Completed! Today, {len(words)} words have been launched.)")
log(f"Access: https://wordstudy-d1gh0i7r67e8a64e8-1463809286.tcloudbaseapp.com")
print(json.dumps({“ok”: True, “date”: TODAY, “words”: len(words)}))




if __name__ == "__main__":
main()
