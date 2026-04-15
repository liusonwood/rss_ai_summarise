import os
import json
import xml.etree.ElementTree as ET
import urllib.request
import urllib.parse
from datetime import datetime
import subprocess
import re
import trafilatura

# 配置
RSS_SOURCE = os.getenv("RSS_SOURCE", "https://9to5mac.com/feed/")
PROCESSED_FILE = "processed.txt"
OUTPUT_FEED = "summary_feed.xml"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
MAX_ITEMS = 20
AI_MODEL = os.getenv("AI_MODEL", "google/gemini-2.0-flash-001")
MAX_HISTORY_ITEMS = 500  # RSS 文件中保留的历史摘要条目数量

def clean_html(raw_html):
    """清理 HTML 标签"""
    if not raw_html: return ""
    clean_re = re.compile('<.*?>')
    return re.sub(clean_re, '', raw_html).strip()

def load_processed_links():
    """加载已处理过的链接"""
    if not os.path.exists(PROCESSED_FILE):
        return set()
    with open(PROCESSED_FILE, 'r') as f:
        return set(line.strip() for line in f if line.strip())

def update_storage(links):
    """记录已处理的链接"""
    with open(PROCESSED_FILE, 'a') as f:
        for link in links:
            if link:
                f.write(f"{link}\n")

def fetch_rss_items(source, processed_links):
    """优化版：先判断链接，只有新文章才抓取全文"""
    try:
        print(f"正在读取 RSS 源: {source}")
        req = urllib.request.Request(source, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as response:
            content = response.read()
                
        root = ET.fromstring(content)
        items = []
        
        for item in root.findall('.//item'):
            title = item.find('title').text if item.find('title') is not None else "No Title"
            link = item.find('link').text if item.find('link') is not None else ""
            
            if not link:
                guid = item.find('guid')
                if guid is not None: link = guid.text
            
            link = link.strip()

            # --- 关键优化：在这里先做判断 ---
            if link in processed_links:
                # 如果已经处理过，直接跳过抓取全文的步骤
                continue 

            print(f"发现新文章，正在抓取全文: {title}")
            body = ""
            try:
                downloaded = trafilatura.fetch_url(link)
                if downloaded:
                    body = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
            except Exception as e:
                print(f"  [!] 全文提取失败: {e}")

            if not body or len(body) < 100:
                content_encoded = item.find('{http://purl.org/rss/1.0/modules/content/}encoded')
                description = item.find('description').text if item.find('description') is not None else ""
                fallback = content_encoded.text if content_encoded is not None else description
                body = clean_html(fallback)
            
            items.append({"title": title, "link": link, "body": body})
            
            # 为了防止单次任务耗时过长，如果新文章已经达到 MAX_ITEMS，可以提前停止抓取
            if len(items) >= MAX_ITEMS:
                break
                
        return items
    except Exception as e:
        print(f"Error: {e}")
        return []

def get_ai_summary(items):
    """调用 AI 生成摘要"""
    if not OPENROUTER_API_KEY:
        raise ValueError("OPENROUTER_API_KEY is not set!")
        
    prompt = "请为以下 RSS 文章提供中文简报，重点突出核心信息，使用列表形式：\n\n"
    for idx, item in enumerate(items, 1):
        prompt += f"{idx}. {item['title']}\n{item['body'][:4000]}...\n\n"
        
    data = {
        "model": AI_MODEL,
        "messages": [{"role": "user", "content": prompt}]
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
        return "摘要生成失败。"

def generate_rss_xml(summary_text):
    """生成或更新 RSS XML 文件 (增量添加)"""
    now = datetime.now()
    rfc822_date = now.strftime("%a, %d %b %Y %H:%M:%S +0000")
    unique_link = f"https://github.com/liusonwood#{now.strftime('%Y%m%d%H%M%S')}"
    
    channel = None
    rss_root = None

    # 如果文件已存在，尝试读取旧内容
    if os.path.exists(OUTPUT_FEED):
        try:
            tree = ET.parse(OUTPUT_FEED)
            rss_root = tree.getroot()
            channel = rss_root.find("channel")
        except Exception as e:
            print(f"读取旧 RSS 文件失败，将创建新文件: {e}")

    # 如果没有旧文件或读取失败，创建基础结构
    if channel is None:
        rss_root = ET.Element("rss", version="2.0")
        channel = ET.SubElement(rss_root, "channel")
        ET.SubElement(channel, "title").text = "AI RSS 简报"
        ET.SubElement(channel, "link").text = "https://github.com/liusonwood/rss_ai_summarise"
        ET.SubElement(channel, "description").text = "由 AI 自动生成的文章全文摘要"

    # 更新最后构建时间
    last_build = channel.find("lastBuildDate")
    if last_build is None:
        last_build = ET.SubElement(channel, "lastBuildDate")
    last_build.text = rfc822_date

    # 创建新的 item 条目
    new_item = ET.Element("item")
    ET.SubElement(new_item, "title").text = f"AI 简报 - {now.strftime('%Y-%m-%d %H:%M')}"
    ET.SubElement(new_item, "link").text = unique_link
    ET.SubElement(new_item, "guid", isPermaLink="false").text = unique_link
    ET.SubElement(new_item, "pubDate").text = rfc822_date
    ET.SubElement(new_item, "description").text = summary_text

    # 将新条目插入到所有 item 的最前面 (index 0 会插在 title/link 等标签前，所以寻找第一个 item 的位置)
    # 简单处理：直接 insert 到第一个位置
    channel.insert(0, new_item)

    # 限制历史条目数量 (防止 XML 无限变大)
    items = channel.findall("item")
    if len(items) > MAX_HISTORY_ITEMS:
        for old_item in items[MAX_HISTORY_ITEMS:]:
            channel.remove(old_item)

    # 格式化输出 (每行一个标签)
    tree = ET.ElementTree(rss_root)
    if hasattr(ET, 'indent'): # Python 3.9+
        ET.indent(tree, space="  ", level=0)
    
    tree.write(OUTPUT_FEED, encoding='utf-8', xml_declaration=True)
    print(f"已更新 RSS 文件: {OUTPUT_FEED}")

def git_commit_push():
    """推送更改到 GitHub"""
    if os.getenv("GITHUB_ACTIONS") != "true":
        print("非 GitHub Actions 环境，跳过 Git 推送。")
        return

    commands = [
        ["git", "config", "user.name", "github-actions[bot]"],
        ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
        ["git", "add", PROCESSED_FILE, OUTPUT_FEED],
        ["git", "commit", "-m", f"Auto-update: {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
        ["git", "push"]
    ]

    for cmd in commands:
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            print(f"Git 命令跳过或失败: {e.stdout.decode()}")



def main():
    print("开始执行 AI RSS 摘要任务...")
    # 1. 先加载已读列表
    processed_links = load_processed_links()
    
    # 2. 传入已读列表，只抓取真正需要处理的文章
    new_items = fetch_rss_items(RSS_SOURCE, processed_links)
    
    if not new_items:
        print("没有新内容，任务结束。")
        return
        
    # 3. 标记为已读
    update_storage([item['link'] for item in new_items])
    
    # 4. 生成摘要并更新 RSS
    print(f"正在为 {len(new_items)} 篇文章生成摘要...")
    summary = get_ai_summary(new_items)
    generate_rss_xml(summary)
    
    print("任务成功完成！")
    git_commit_push()

if __name__ == "__main__":
    main()