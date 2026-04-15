import os
import json
import xml.etree.ElementTree as ET
import urllib.request
import urllib.parse
from datetime import datetime
import subprocess
import re

# 现在可以直接使用官方源，不再需要 morss.it 了
RSS_SOURCE = os.getenv("RSS_SOURCE", "https://9to5mac.com/feed/")
PROCESSED_FILE = "processed.txt"
OUTPUT_FEED = "summary_feed.xml"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
MAX_ITEMS = 10
AI_MODEL = os.getenv("AI_MODEL", "google/gemini-2.0-flash-001")

def clean_html(raw_html):
    """Strip HTML tags for token efficiency (用于抓取失败时的兜底过滤)."""
    if not raw_html: return ""
    clean_re = re.compile('<.*?>')
    return re.sub(clean_re, '', raw_html).strip()

def load_processed_links():
    """Load processed links into a set."""
    if not os.path.exists(PROCESSED_FILE):
        return set()
    with open(PROCESSED_FILE, 'r') as f:
        return set(line.strip() for line in f if line.strip())

def update_storage(links):
    """Append new links to processed.txt."""
    with open(PROCESSED_FILE, 'a') as f:
        for link in links:
            f.write(f"{link}\n")

def fetch_rss_items(source):
    """Fetch RSS, parse it, and fetch full text via Jina Reader."""
    try:
        print(f"Fetching RSS from: {source}")
        if source.startswith("http"):
            # 增加 User-Agent 防止被网站基础防护拦截
            req = urllib.request.Request(source, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=15) as response:
                content = response.read()
        else:
            with open(source, 'rb') as f:
                content = f.read()
                
        root = ET.fromstring(content)
        items =[]
        for item in root.findall('.//item'):
            title = item.find('title').text if item.find('title') is not None else "No Title"
            link = item.find('link').text if item.find('link') is not None else ""
            
            # Fallback for empty link in 9to5mac feed
            if not link:
                guid = item.find('guid')
                if guid is not None:
                    link = guid.text
            
            # --- 新增的 Jina 全文抓取逻辑 ---
            print(f"正在抓取全文: {title}")
            try:
                jina_url = f"https://r.jina.ai/{link}"
                jina_req = urllib.request.Request(
                    jina_url, 
                    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
                )
                with urllib.request.urlopen(jina_req, timeout=15) as response:
                    # 获取到的直接是排版干净的 Markdown 文本
                    body = response.read().decode('utf-8')
            except Exception as e:
                print(f"  [!] 抓取全文失败 ({link}): {e}，将使用原生摘要兜底")
                # 抓取失败时的兜底逻辑（使用 RSS 原本的描述）
                content_encoded = item.find('{http://purl.org/rss/1.0/modules/content/}encoded')
                description = item.find('description').text if item.find('description') is not None else ""
                fallback_text = content_encoded.text if content_encoded is not None else description
                body = clean_html(fallback_text)
            # -------------------------------
            
            items.append({"title": title, "link": link, "body": body})
        return items
    except Exception as e:
        print(f"Error fetching RSS: {e}")
        return[]

def get_ai_summary(items):
    """Call OpenRouter API to summarize the text."""
    if not OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY is not set!")
        
    prompt = "Please provide a concise summary (in Chinese) for the following RSS items. Focus on the core message and use bullet points if possible:\n\n"
    for idx, item in enumerate(items, 1):
        # 【重点修改】因为获取到了全文，我们将截断长度从 500 放宽到 4000 字符
        prompt += f"{idx}. {item['title']}\n{item['body'][:4000]}...\n\n"
        
    data = {
        "model": AI_MODEL,
        "messages":[
            {"role": "user", "content": prompt}
        ]
    }
    
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(data).encode('utf-8'),
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/liusonwood/rssaisummarise",
            "X-Title": "RSS AI Summary Agent"
        }
    )
    
    try:
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode('utf-8'))
            return res_data['choices'][0]['message']['content']
    except Exception as e:
        print(f"Error calling AI API: {e}")
        return "Summary generation failed."

def generate_rss_xml(summary_text):
    """Generate a valid RSS XML feed with the AI summary."""
    now = datetime.now()
    rfc822_date = now.strftime("%a, %d %b %Y %H:%M:%S +0000")
    unique_link = f"https://github.com/liusonwood#{now.strftime('%Y%m%d%H%M%S')}"
    
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    
    ET.SubElement(channel, "title").text = "AI RSS Summaries"
    ET.SubElement(channel, "link").text = "https://github.com/liusonwood/rss_ai_summarise"
    ET.SubElement(channel, "description").text = "Automated AI summaries of your favorite feeds."
    ET.SubElement(channel, "lastBuildDate").text = rfc822_date

    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = f"Summary - {now.strftime('%Y-%m-%d %H:%M')}"
    ET.SubElement(item, "link").text = unique_link
    ET.SubElement(item, "guid").text = unique_link
    ET.SubElement(item, "pubDate").text = rfc822_date
    ET.SubElement(item, "description").text = summary_text

    tree = ET.ElementTree(rss)
    tree.write(OUTPUT_FEED, encoding='utf-8', xml_declaration=True)

def git_commit_push():
    """Commit and push the updated files back to GitHub."""
    if os.getenv("GITHUB_ACTIONS") != "true":
        print("Not running in GitHub Actions. Skipping git push.")
        return

    commands = [["git", "config", "user.name", "github-actions[bot]"],["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],["git", "add", PROCESSED_FILE, OUTPUT_FEED],["git", "commit", "-m", f"Auto-update: RSS AI Summary {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
        ["git", "push"]
    ]

    for cmd in commands:
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as e:
            if "nothing to commit" in e.stdout.decode().lower():
                print("No changes to commit.")
            else:
                print(f"Git command failed: {e.stderr.decode()}")

def main():
    print("Starting RSS AI Summarizer...")
    processed_links = load_processed_links()
    all_items = fetch_rss_items(RSS_SOURCE)
    
    # 过滤掉已经处理过的文章
    new_items = [item for item in all_items if item['link'] not in processed_links]
    print(f"Found {len(new_items)} new items out of {len(all_items)} total.")
    
    if not new_items:
        print("No new items to process. Exiting.")
        return
        
    to_process = new_items[:MAX_ITEMS]
    print(f"Processing {len(to_process)} items...")
    
    summary = get_ai_summary(to_process)
    generate_rss_xml(summary)
    
    # 将刚刚处理过的链接加入到已读文件
    update_storage([item['link'] for item in to_process])
    
    print("Summary generated successfully!")
    git_commit_push()

if __name__ == "__main__":
    main()