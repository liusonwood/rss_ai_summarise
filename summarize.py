import os
import json
import xml.etree.ElementTree as ET
import urllib.request
import urllib.parse
from datetime import datetime
import subprocess
import re

# Why: Using global config for ease of manual updates without deep code modification.
# RSS_SOURCE can be a local path for testing or a remote URL.
RSS_SOURCE = os.getenv("RSS_SOURCE", "example-rss-feed")
PROCESSED_FILE = "processed.txt"
OUTPUT_FEED = "summary_feed.xml"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
MAX_ITEMS = 10
# Why: Gemini-2.0-Flash provides the best balance of context window and token efficiency for RSS summarization.
AI_MODEL = os.getenv("AI_MODEL", "google/gemini-2.0-flash-001")

def clean_html(raw_html):
    """Strip HTML tags for token efficiency."""
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
    """Fetch and parse RSS items from a local file or URL."""
    try:
        if source.startswith("http"):
            with urllib.request.urlopen(source) as response:
                content = response.read()
        else:
            with open(source, 'rb') as f:
                content = f.read()
        
        root = ET.fromstring(content)
        items = []
        for item in root.findall('.//item'):
            title = item.find('title').text if item.find('title') is not None else "No Title"
            link = item.find('link').text if item.find('link') is not None else ""
            # Fallback for empty link in 9to5mac feed
            if not link:
                guid = item.find('guid')
                if guid is not None:
                    link = guid.text

            content_encoded = item.find('{http://purl.org/rss/1.0/modules/content/}encoded')
            description = item.find('description').text if item.find('description') is not None else ""
            
            body = content_encoded.text if content_encoded is not None else description
            items.append({
                "title": title,
                "link": link,
                "body": clean_html(body)
            })
        return items
    except Exception as e:
        print(f"Error fetching RSS: {e}")
        return []

def get_ai_summary(items):
    """Batch summarize items using OpenRouter."""
    if not OPENROUTER_API_KEY:
        print("Error: OPENROUTER_API_KEY not found in environment.")
        return "AI Summary could not be generated (API Key missing)."

    # Combine items for a single summary to save tokens/calls
    prompt = "Please provide a concise summary (in Chinese) for the following RSS items. Focus on the core message and use bullet points if possible:\n\n"
    for idx, item in enumerate(items, 1):
        prompt += f"{idx}. {item['title']}\n{item['body'][:500]}...\n\n"

    data = {
        "model": AI_MODEL,
        "messages": [
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
        print(f"Error calling OpenRouter: {e}")
        return "AI Summary generation failed due to API error."

def generate_rss_xml(summary_text):
    """Generate a standard RSS XML with the summarized content."""
    now = datetime.now()
    rfc822_date = now.strftime("%a, %d %b %Y %H:%M:%S +0000")
    unique_link = f"https://github.com/liusonwood#{now.strftime('%Y%m%d%H%M%S')}"

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    
    ET.SubElement(channel, "title").text = "RSS AI Daily Summary"
    ET.SubElement(channel, "link").text = "https://github.com/liusonwood/rssaisummarise"
    ET.SubElement(channel, "description").text = "AI-generated summaries of your RSS feeds."
    ET.SubElement(channel, "lastBuildDate").text = rfc822_date

    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = f"Summary - {now.strftime('%Y-%m-%d %H:%M')}"
    ET.SubElement(item, "link").text = unique_link
    ET.SubElement(item, "guid", isPermaLink="false").text = unique_link
    ET.SubElement(item, "pubDate").text = rfc822_date
    ET.SubElement(item, "description").text = summary_text

    tree = ET.ElementTree(rss)
    tree.write(OUTPUT_FEED, encoding='utf-8', xml_declaration=True)
    print(f"Generated {OUTPUT_FEED}")

def git_commit_push():
    """Commit changes to the repository if running in GitHub Actions."""
    if os.getenv("GITHUB_ACTIONS") != "true":
        print("Not running in GitHub Actions, skipping git commit.")
        return

    commands = [
        ["git", "config", "user.name", "github-actions[bot]"],
        ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
        ["git", "add", PROCESSED_FILE, OUTPUT_FEED],
        ["git", "commit", "-m", f"Auto-update: RSS AI Summary {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
        ["git", "push"]
    ]

    for cmd in commands:
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            if "nothing to commit" in e.stdout or "nothing to commit" in e.stderr:
                print("Nothing to commit, skipping.")
                continue
            print(f"Git command failed: {cmd}. Error: {e.stderr or e.stdout}")

def main():
    processed_links = load_processed_links()
    all_items = fetch_rss_items(RSS_SOURCE)
    
    new_items = [item for item in all_items if item['link'] not in processed_links]
    
    if not new_items:
        print("No new items to process.")
        return

    # Limit items to prevent token explosion
    to_process = new_items[:MAX_ITEMS]
    print(f"Processing {len(to_process)} new items...")

    summary = get_ai_summary(to_process)
    generate_rss_xml(summary)
    
    # Update state
    update_storage([item['link'] for item in to_process])
    
    # Persist via Git
    git_commit_push()

if __name__ == "__main__":
    main()
