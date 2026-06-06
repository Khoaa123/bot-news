import os
import time
import json
import re
import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
import requests
import feedparser
from bs4 import BeautifulSoup
from openai import OpenAI
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status, Query, Header, BackgroundTasks

# Load environment variables for local testing
load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

# Configuration
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
APP_API_KEY = os.getenv("APP_API_KEY")  # Optional secret to protect HTTP endpoint

FEEDS_FILE = "feeds.json"
DEFAULT_FEEDS = {
    "TechCrunch AI": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "VentureBeat AI": "https://venturebeat.com/category/ai/feed/",
    "The Verge AI": "https://www.theverge.com/ai-artificial-intelligence/rss/index.xml"
}

# Time window for news (last 24 hours)
TIME_WINDOW_HOURS = 24

app = FastAPI(
    title="Daily AI News Bot API",
    description="Web server to trigger Daily AI News Bot via HTTP or Telegram Webhook.",
    version="1.2.0"
)

def load_rss_feeds() -> dict:
    """Loads RSS feeds from feeds.json, falling back to default feeds if not found."""
    if os.path.exists(FEEDS_FILE):
        try:
            with open(FEEDS_FILE, "r", encoding="utf-8") as f:
                feeds = json.load(f)
                if isinstance(feeds, dict) and feeds:
                    return feeds
        except Exception as e:
            logging.error(f"Error reading feeds.json: {str(e)}")
            
    # If file doesn't exist or is invalid, write defaults
    try:
        with open(FEEDS_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_FEEDS, f, indent=2, ensure_ascii=False)
        logging.info(f"Initialized default {FEEDS_FILE}")
    except Exception as e:
        logging.error(f"Error writing default feeds.json: {str(e)}")
        
    return DEFAULT_FEEDS

def extract_stars_number(stars_str: str) -> int:
    """Extracts the integer number of stars from strings like '1,826 stars today', '1.8k stars today', '+1.8k today'."""
    if not stars_str:
        return 0
    # Normalize string: remove commas, plus signs
    s = stars_str.lower().replace(",", "").replace("+", "").strip()
    # Find numeric patterns
    match = re.search(r'([\d\.]+)\s*k?', s)
    if match:
        try:
            val = float(match.group(1))
            if 'k' in s:
                return int(val * 1000)
            return int(val)
        except Exception:
            return 0
    return 0

def fetch_recent_news():
    """Fetches articles from RSS feeds published in the last 24 hours or parses GitHub Trending."""
    logging.info("Starting feed scraping...")
    now_utc = datetime.now(timezone.utc)
    cutoff_time = now_utc - timedelta(hours=TIME_WINDOW_HOURS)
    
    rss_sources = load_rss_feeds()
    all_articles = []
    
    for source_name, feed_url in rss_sources.items():
        # Handle GitHub Trending page
        if "github.com/trending" in feed_url:
            try:
                logging.info(f"Scraping GitHub Trending: {source_name} ({feed_url})")
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                }
                response = requests.get(feed_url, headers=headers, timeout=15)
                if response.status_code == 200:
                    soup = BeautifulSoup(response.text, "html.parser")
                    articles = soup.find_all("article", class_="Box-row")
                    
                    # Extract top 8 repositories to stay within character limits
                    feed_articles_count = 0
                    for entry in articles[:8]:
                        h2 = entry.find("h2")
                        if not h2:
                            continue
                        a_tag = h2.find("a")
                        if not a_tag:
                            continue
                        
                        repo_path = a_tag["href"].strip()
                        repo_name = repo_path.lstrip("/")  # Format "owner/repo"
                        link = f"https://github.com{repo_path}"
                        
                        # Repository Description
                        p_tag = entry.find("p")
                        description = p_tag.text.strip() if p_tag else "No description available."
                        
                        # Stars gained (today / this week)
                        stars_gained = ""
                        span_stars = entry.find("span", class_="float-sm-right")
                        if span_stars:
                            stars_gained = span_stars.text.strip()
                        else:
                            # Fallback: search for span containing "stars"
                            for span in entry.find_all("span"):
                                if "stars" in span.text.lower():
                                    stars_gained = span.text.strip()
                                    break
                                    
                        # Total stars
                        total_stars = ""
                        stargazers_link = entry.find("a", href=lambda href: href and href.endswith("/stargazers"))
                        if stargazers_link:
                            total_stars = stargazers_link.text.strip()
                            
                        # Star Velocity Filter (🚀 [SIÊU HOT] if daily >= 1000 stars or weekly >= 5000 stars)
                        stars_num = extract_stars_number(stars_gained)
                        is_super_hot = False
                        if "since=daily" in feed_url and stars_num >= 1000:
                            is_super_hot = True
                        elif "since=weekly" in feed_url and stars_num >= 5000:
                            is_super_hot = True
                            
                        title_prefix = "🚀 [SIÊU HOT] " if is_super_hot else ""
                        title_text = f"{title_prefix}{repo_name} (Total Stars: {total_stars}, +{stars_gained})"
                            
                        all_articles.append({
                            "source": source_name,
                            "title": title_text,
                            "link": link,
                            "summary": description,
                            "published_at": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
                        })
                        feed_articles_count += 1
                        
                    logging.info(f"Extracted {feed_articles_count} trending repositories from {source_name}")
                else:
                    logging.error(f"Failed to fetch GitHub trending page: HTTP {response.status_code}")
            except Exception as e:
                logging.error(f"Error parsing GitHub Trending {source_name}: {str(e)}")
            continue

        # Handle Standard RSS feed
        try:
            logging.info(f"Parsing feed: {source_name} ({feed_url})")
            feed = feedparser.parse(feed_url)
            
            feed_articles_count = 0
            for entry in feed.entries:
                # Parse publishing time
                published_struct = entry.get("published_parsed") or entry.get("updated_parsed")
                if not published_struct:
                    continue
                
                # Convert struct_time to aware datetime in UTC
                published_dt = datetime(*published_struct[:6], tzinfo=timezone.utc)
                
                if published_dt >= cutoff_time:
                    title = entry.get("title", "").strip()
                    link = entry.get("link", "").strip()
                    summary = entry.get("summary", "") or entry.get("description", "")
                    
                    all_articles.append({
                        "source": source_name,
                        "title": title,
                        "link": link,
                        "summary": summary[:300] + "..." if len(summary) > 300 else summary,
                        "published_at": published_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
                    })
                    feed_articles_count += 1
            
            logging.info(f"Found {feed_articles_count} recent articles from {source_name}")
            
        except Exception as e:
            logging.error(f"Error parsing feed {source_name}: {str(e)}")
            
    logging.info(f"Total articles/repositories fetched: {len(all_articles)}")
    return all_articles

def escape_telegram_html(text: str) -> str:
    """Escapes raw HTML special characters in text while preserving valid Telegram tags."""
    placeholders = {}
    
    # 1. Protect <a> tags with either single or double quotes
    a_tags = re.findall(r'<a\s+href=[\"\'][^\'\"]+[\"\']>', text)
    for idx, tag in enumerate(a_tags):
        ph = f"___A_TAG_START_{idx}___"
        # Escape raw '&' in URLs so Telegram parser doesn't crash
        clean_tag = tag.replace("&", "&amp;").replace("&amp;amp;", "&amp;")
        placeholders[ph] = clean_tag
        text = text.replace(tag, ph)
        
    text = text.replace("</a>", "___A_TAG_END___")
    placeholders["___A_TAG_END___"] = "</a>"
    
    # 2. Protect valid Telegram HTML formatting tags
    valid_tags = ["<b>", "</b>", "<i>", "</i>", "<u>", "</u>", "<s>", "</s>", "<code>", "</code>", "<pre>", "</pre>"]
    for tag in valid_tags:
        ph = f"___TAG_{tag.replace('<', '').replace('>', '').replace('/', 'CLOSE_')}___"
        placeholders[ph] = tag
        text = text.replace(tag, ph)
        
    # 3. Unescape any pre-existing HTML entities, then escape all raw characters (like <, >, &)
    text = html.unescape(text)
    text = html.escape(text)
    
    # 4. Restore the protected valid HTML tags
    for ph, tag in placeholders.items():
        text = text.replace(ph, tag)
        
    return text

def summarize_news(articles):
    """Sends the raw articles list to DeepSeek AI to get a structured, translated summary."""
    if not articles:
        return "Hôm nay không có tin tức AI mới nào nổi bật từ các trang theo dõi."
        
    logging.info("Sending news to DeepSeek API for summarization...")
    
    if not DEEPSEEK_API_KEY:
        raise ValueError("Missing DEEPSEEK_API_KEY environment variable.")
        
    # Calculate today's date in Vietnam Time (ICT / UTC+7)
    ict_tz = timezone(timedelta(hours=7))
    current_date_str = datetime.now(ict_tz).strftime("%d/%m/%Y")
        
    # Format articles for the LLM prompt
    formatted_articles = ""
    for idx, art in enumerate(articles, 1):
        formatted_articles += (
            f"[{idx}] Source: {art['source']}\n"
            f"Title: {art['title']}\n"
            f"Link: {art['link']}\n"
            f"Summary: {art['summary']}\n"
            f"-------------------\n"
        )
        
    client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com"
    )
    
    system_prompt = (
        "You are an expert tech news editor. Your job is to write a premium AI & GitHub newsletter in Vietnamese.\n"
        "You must output the summary in HTML format suitable for Telegram sendMessage API.\n"
        "Rules for Telegram HTML formatting:\n"
        "- Use <b>text</b> for bold headers\n"
        "- Use <i>text</i> for italic text\n"
        "- Use <a href=\"URL\">Link Text</a> for hyperlinks\n"
        "- Do NOT use markdown symbols like **, *, or [text](url) in the output.\n"
        "- Do NOT use <br>, <br/>, <ul>, <li>, <ol>, or <div> tags. For line breaks, use plain newlines.\n"
        "- Format the newsletter in a highly premium, clean style with clear section dividers (e.g. ━━━━━━━━━━━━━━━━━━━━━━━━).\n"
        "- Limit the entire response to 3800 characters to fit Telegram limits."
    )
    
    user_prompt = (
        f"Dưới đây là các bài báo về trí tuệ nhân tạo (AI) mới nhất và các GitHub repository đang thịnh hành trong 24 giờ qua.\n"
        f"Hãy tổng hợp và viết một bản tin tổng hợp Tiếng Việt thật cô đọng, chuyên nghiệp và cuốn hút.\n\n"
        f"Yêu cầu nội dung & định dạng (Newsletter Style):\n"
        f"1. TIÊU ĐỀ: Sử dụng định dạng:\n"
        f"<b>📬 BẢN TIN AI & GITHUB TRENDING</b>\n"
        f"<i>Ngày phát hành: {current_date_str}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"2. MỤC 1: 🔥 <b>Tin tức AI nổi bật</b>\n"
        f"(Đánh giá điểm số tác động [Impact: X/10] ngay sau tiêu đề của mỗi tin tức, ví dụ: <b>1. OpenAI ra mắt mô hình mới</b> [Impact: 9.5/10]. Viết tóm tắt 2-3 câu ngắn gọn, súc tích và có link gốc <a href=\"link\">Đọc thêm</a>. Thêm một dòng trống giữa các tin)\n\n"
        f"3. MỤC 2: 🚀 <b>GitHub Trending nổi bật</b>\n"
        f"(Đối với mỗi repo: Viết theo định dạng:\n"
        f"• <b>owner/repo</b> (tổng số star, số star tăng hôm nay) [Impact: X/10]\n"
        f"Nếu tiêu đề repo có gắn nhãn '🚀 [SIÊU HOT]', hãy giữ lại nhãn này, bôi đậm nó và đánh giá điểm số tác động cao. Mô tả ngắn gọn xem repo này làm gì, số lượng star tích lũy và lý do nó nổi bật. Kèm link <a href=\"link\">Chi tiết</a>. Thêm một dòng trống giữa các repo)\n\n"
        f"4. MỤC 3: 💡 <b>Nhận định xu hướng</b>\n"
        f"(Viết một đoạn nhận định ngắn, súc tích và sâu sắc về xu hướng công nghệ nổi bật ngày hôm nay dựa trên các tin tức và repository trên)\n\n"
        f"5. SỬ DỤNG đường phân cách ━━━━━━━━━━━━━━━━━━━━━━━━ giữa các mục lớn.\n"
        f"Danh sách dữ liệu nguồn:\n"
        f"{formatted_articles}"
    )
    
    try:
        response = client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.7
        )
        summary = response.choices[0].message.content
        logging.info(f"DeepSeek response length: {len(summary) if summary else 0}")
        if summary:
            logging.info(f"Raw DeepSeek response snippet: {repr(summary[:300])}")
        else:
            logging.warning("DeepSeek response is empty or None!")
            
        if summary:
            # Sanitize HTML tags for Telegram API compatibility
            summary = summary.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
            summary = summary.replace("<ul>", "").replace("</ul>", "")
            summary = summary.replace("<li>", "• ").replace("</li>", "\n")
            summary = summary.replace("<ol>", "").replace("</ol>", "")
            
            # Escape raw <, >, and & characters to prevent unclosed tag errors
            summary = escape_telegram_html(summary)
            logging.info(f"Sanitized response length: {len(summary)}")
            logging.info(f"Sanitized response snippet: {repr(summary[:300])}")
        
        return summary
    except Exception as e:
        logging.error(f"Error calling DeepSeek API: {str(e)}")
        # Fallback to simple listing if AI fails
        fallback_msg = "<b>Bản tin AI hôm nay (Lỗi tóm tắt AI):</b>\n\n"
        for art in articles[:10]:
            fallback_msg += f"• {art['title']} - <a href='{art['link']}'>{art['source']}</a>\n"
        return fallback_msg

def send_to_telegram_specific_chat(chat_id, message):
    """Sends a message to a specific Telegram chat ID with retries."""
    logging.info(f"Sending message to chat {chat_id}...")
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("Missing TELEGRAM_BOT_TOKEN.")
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(url, json=payload, timeout=30)
            response_data = response.json()
            if response.status_code == 200 and response_data.get("ok"):
                logging.info(f"Message sent to chat {chat_id} successfully on attempt {attempt}!")
                return
            else:
                logging.error(f"Failed to send Telegram message on attempt {attempt}: {response.text}")
        except Exception as e:
            logging.error(f"Error sending message to Telegram on attempt {attempt}: {str(e)}")
            
        if attempt < max_retries:
            time.sleep(2)  # Wait 2 seconds before retrying
            
    logging.error(f"Failed to send Telegram message after {max_retries} attempts.")

def send_to_telegram(message):
    """Fallback sending to the configured owner chat ID."""
    if not TELEGRAM_CHAT_ID:
        raise ValueError("Missing TELEGRAM_CHAT_ID environment variable.")
    send_to_telegram_specific_chat(TELEGRAM_CHAT_ID, message)

def run_bot_flow_for_chat(chat_id):
    """Runs the main pipeline and sends the result to the specified chat ID."""
    start_time = time.time()
    logging.info(f"Triggered bot flow for chat {chat_id}...")
    try:
        articles = fetch_recent_news()
        if not articles:
            logging.info("No new articles found.")
            send_to_telegram_specific_chat(chat_id, "<b>Bản tin AI hôm nay:</b>\nHôm nay không có tin tức AI mới nào nổi bật từ các nguồn theo dõi.")
        else:
            summary = summarize_news(articles)
            send_to_telegram_specific_chat(chat_id, summary)
            
    except Exception as e:
        logging.critical(f"Bot flow execution failed: {str(e)}")
        send_to_telegram_specific_chat(chat_id, f"❌ Có lỗi xảy ra trong quá trình cào tin và tóm tắt: {str(e)}")
        
    logging.info(f"Bot flow completed in {time.time() - start_time:.2f} seconds.")

def run_bot_flow():
    """Fallback daily cron flow for owner."""
    if not TELEGRAM_CHAT_ID:
        raise ValueError("Missing TELEGRAM_CHAT_ID environment variable.")
    run_bot_flow_for_chat(TELEGRAM_CHAT_ID)

@app.api_route("/", methods=["GET", "HEAD"])
def root():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": "Daily AI News Bot API",
        "current_time": datetime.now(timezone.utc).isoformat(),
        "sources_configured": list(load_rss_feeds().keys())
    }

@app.get("/run-bot")
def trigger_bot(
    background_tasks: BackgroundTasks,
    api_key: Optional[str] = Query(None),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
):
    """Endpoint to trigger the news summarization pipeline in the background via HTTP."""
    # Check authorization if APP_API_KEY is configured in env
    if APP_API_KEY:
        provided_key = api_key or x_api_key
        if provided_key != APP_API_KEY:
            logging.warning("Unauthorized access attempt to /run-bot")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Unauthorized: Invalid APP_API_KEY."
            )
            
    logging.info("Received request to trigger bot.")
    background_tasks.add_task(run_bot_flow)
    return {
        "status": "success",
        "message": "Bot process triggered in background."
    }

@app.post("/telegram-webhook")
def telegram_webhook(update: dict, background_tasks: BackgroundTasks):
    """Handles incoming messages from Telegram (Webhook)."""
    logging.info(f"Received webhook update: {update}")
    
    # Extract message details
    message = update.get("message")
    if not message:
        return {"status": "ignored"}
        
    chat = message.get("chat")
    if not chat:
        return {"status": "ignored"}
        
    chat_id = chat.get("id")
    text = message.get("text", "").strip()
    
    # Security check: only allow owner to trigger the bot to prevent API abuse
    if TELEGRAM_CHAT_ID and str(chat_id) != str(TELEGRAM_CHAT_ID):
        logging.warning(f"Unauthorized chat_id {chat_id} attempted to trigger the bot.")
        send_to_telegram_specific_chat(
            chat_id, 
            "Xin lỗi, tôi là bot cá nhân của Khoa. Tôi không được phép phục vụ bạn."
        )
        return {"status": "unauthorized"}
        
    if text == "/start" or text == "/help":
        help_msg = (
            "Chào mừng bạn đến với <b>AI Tech News Bot</b>!\n\n"
            "Các lệnh khả dụng:\n"
            "• <code>/run</code> hoặc <code>/summary</code>: Kích hoạt cào tin tức và tóm tắt gửi về ngay lập tức.\n"
            "• <code>/help</code>: Hiển thị hướng dẫn này."
        )
        send_to_telegram_specific_chat(chat_id, help_msg)
        return {"status": "ok"}
        
    if text in ["/run", "/summary"]:
        # Run bot flow in background and respond immediately to avoid webhook timeouts
        background_tasks.add_task(run_bot_flow_for_chat, chat_id)
        send_to_telegram_specific_chat(chat_id, "🤖 Đang tiến hành cào tin tức và tóm tắt bằng DeepSeek v4... Vui lòng đợi trong giây lát!")
        return {"status": "processing"}
        
    return {"status": "ignored"}

@app.get("/setup-webhook")
def setup_webhook(url: str = Query(..., description="The HTTPS URL of your deployed server (e.g. https://my-bot.onrender.com)")):
    """Helper route to easily register this server's webhook with Telegram."""
    if not url.startswith("https://"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook URL must start with https://"
        )
    
    webhook_url = f"{url.rstrip('/')}/telegram-webhook"
    telegram_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook"
    
    try:
        response = requests.post(telegram_url, json={"url": webhook_url}, timeout=10)
        return {
            "status": "success",
            "telegram_response": response.json()
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to set webhook: {str(e)}"
        )
