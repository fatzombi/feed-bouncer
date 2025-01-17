import feedparser
import yaml
import json
import os
import smtplib
import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import requests
from dotenv import load_dotenv
import openai
import argparse
import html2text
from bs4 import BeautifulSoup
import backoff
from requests.exceptions import RequestException
import time

# Load environment variables
load_dotenv()

class RSSAnalyzer:
    def __init__(self, config_path: str = "config.yaml", state_path: str = "state.json", process_all=False):
        self.config_path = config_path
        self.state_path = state_path
        self.config = self._load_config()
        self.state_file = Path(state_path)
        self.state = self._load_state()
        self.html_converter = html2text.HTML2Text()
        self.html_converter.ignore_images = True
        self.html_converter.body_width = 0  # Don't wrap text
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })
        self.process_all = process_all
        self.openai_client = openai.OpenAI()

    def _load_config(self) -> dict:
        with open(self.config_path, "r") as f:
            return yaml.safe_load(f)
    
    def _load_state(self) -> dict:
        if os.path.exists(self.state_path):
            with open(self.state_path, "r") as f:
                return json.load(f)
        return {"feeds": {}}
    
    def _save_state(self):
        with open(self.state_path, "w") as f:
            json.dump(self.state, f, indent=2)

    def _parse_date(self, date_str: str) -> Optional[datetime.datetime]:
        """Parse date string from feed. Returns None if parsing fails."""
        if not date_str:
            return None
            
        formats = [
            "%a, %d %b %Y %H:%M:%S %z",    # RFC 822
            "%Y-%m-%dT%H:%M:%S%z",         # ISO 8601
            "%Y-%m-%dT%H:%M:%SZ",          # ISO 8601 UTC
            "%Y-%m-%dT%H:%M:%S.%fZ",       # ISO 8601 with milliseconds
            "%Y-%m-%dT%H:%M:%S.%f%z",      # ISO 8601 with milliseconds and timezone
            "%Y-%m-%d %H:%M:%S%z",         # Common format
            "%a, %d %b %Y %H:%M:%S %Z",    # RFC 822 with timezone name
        ]
        
        # First try feedparser's built-in parser
        try:
            parsed = feedparser._parse_date(date_str)
            if parsed:
                return datetime.datetime.fromtimestamp(time.mktime(parsed)).replace(tzinfo=datetime.timezone.utc)
        except:
            pass
            
        # Try our list of formats
        for fmt in formats:
            try:
                dt = datetime.datetime.strptime(date_str, fmt)
                # Add UTC timezone if not present
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=datetime.timezone.utc)
                return dt
            except (ValueError, TypeError):
                continue
                
        print(f"Warning: Could not parse date: {date_str}")
        return None

    def _estimate_reading_time(self, text: str) -> int:
        """Estimate reading time in minutes based on average reading speed of 250 words per minute."""
        words = len(text.split())
        return max(1, round(words / 250))

    def _fetch_article_content(self, url: str) -> str:
        """Fetch article content from URL and convert to markdown."""
        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            
            # Parse with BeautifulSoup to get the main content
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Remove script and style elements
            for script in soup(["script", "style"]):
                script.decompose()
            
            # Try to find the main article content
            article = soup.find('article') or soup.find(class_='post-content') or soup.find(class_='entry-content')
            
            if article:
                content = str(article)
            else:
                # Fallback to body if we can't find a specific article container
                content = str(soup.body)
            
            # Convert to markdown
            markdown = self.html_converter.handle(content)
            return markdown.strip()
        except Exception as e:
            print(f"Error fetching article content from {url}: {str(e)}")
            return ""

    def get_new_articles(self):
        """Get new articles from all configured feeds."""
        new_articles = []
        
        for feed_url in self.config["rss_feeds"]:
            # Initialize state for new feeds
            if feed_url not in self.state["feeds"]:
                self.state["feeds"][feed_url] = {
                    "last_run": None
                }
            
            feed = feedparser.parse(feed_url)
            feed_state = self.state["feeds"][feed_url]
            last_run = None if feed_state["last_run"] == "null" or self.process_all else self._parse_date(feed_state["last_run"])
            
            print(f"\nProcessing feed: {feed_url}")
            print(f"Last run: {'Never' if last_run is None else last_run}")
            
            for entry in feed.entries:
                # Try multiple date fields
                pub_date = None
                for date_field in ['published', 'pubDate', 'updated', 'created']:
                    if pub_date := self._parse_date(entry.get(date_field, "")):
                        break
                
                if not pub_date:
                    print(f"Warning: No valid date found for article: {entry.title}")
                    continue
                
                #print(f"Article: {entry.title}")
                #print(f"Published: {pub_date}")
                #print(f"Date fields: {[f'{k}: {v}' for k, v in entry.items() if 'date' in k.lower() or 'time' in k.lower()]}")
                
                # Skip if we've already processed this article
                if last_run is not None and pub_date <= last_run:
                    continue
                                
                # Fetch and convert the full article content
                print(f"Fetching content for: {entry.title}")
                content = self._fetch_article_content(entry.link)
                
                # Combine title and content for reading time estimate
                full_text = f"{entry.title}\n\n{content}"
                reading_time = self._estimate_reading_time(full_text)
                
                new_articles.append({
                    "title": entry.title,
                    "link": entry.link,
                    "content": content,
                    "published": entry.get("published", ""),
                    "feed_url": feed_url,
                    "reading_time": reading_time
                })
            
            # Update last run time for this feed
            feed_state["last_run"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        
        # Save state after processing all feeds
        self._save_state()
        
        print(f"\nFound {len(new_articles)} new articles to process")
        return new_articles

    def analyze_article(self, article: Dict) -> Tuple[bool, str]:
        """Analyze an article using the configured LLM provider."""
        if self.config["llm"]["provider"] == "openai":
            return self._analyze_with_openai(article)
        else:
            return self._analyze_with_ollama(article)

    @backoff.on_exception(backoff.expo, 
                         (openai.RateLimitError, openai.APIError),
                         max_tries=8)
    def _analyze_with_openai(self, article: Dict) -> Tuple[bool, str]:
        """Analyze article with OpenAI, with exponential backoff on rate limits."""
        prompt = self._create_analysis_prompt(article)
        
        print(f"\nAnalyzing article: {article['title']}")
        
        response = self.openai_client.chat.completions.create(
            model=self.config["llm"]["model"],
            messages=[
                {"role": "system", "content": "You are an article recommendation assistant."},
                {"role": "user", "content": prompt}
            ]
        )
        
        result = response.choices[0].message.content
        #print("-" * 10)
        #print(f"\nLLM Response:\n{result}\n")
        
        # Initialize variables
        aspects = "No relevant aspects found"
        decision = False
        reason = "No explanation provided"
        current_section = None
        aspects_content = []
        reason_content = []
        
        #print("-" * 10)
        # Process the response line by line
        for line in result.split('\n'):
            line = line.strip()
            if not line:
                continue
                
            if line.startswith('RELEVANT ASPECTS:'):
                current_section = 'aspects'
                initial_content = line.replace('RELEVANT ASPECTS:', '').strip()
                if initial_content:
                    aspects_content.append(initial_content)
            elif line.startswith('DECISION:'):
                current_section = 'decision'
                decision_text = line.replace('DECISION:', '').strip().lower()
                decision = decision_text == 'yes'
            elif line.startswith('REASON:'):
                current_section = 'reason'
                initial_content = line.replace('REASON:', '').strip()
                if initial_content:
                    reason_content.append(initial_content)
            elif current_section == 'aspects':
                aspects_content.append(line)
            elif current_section == 'reason':
                reason_content.append(line)
        
        # Join the collected content
        if aspects_content:
            aspects = ' '.join(aspects_content)
        if reason_content:
            reason = ' '.join(reason_content)
        
        print(f"Decision: {'Should read' if decision else 'Skip'}")
        print(f"Reason: {reason}\n")
        print("-" * 80)
        
        return decision, (aspects, reason)

    def _analyze_with_ollama(self, article: Dict) -> Tuple[bool, str]:
        prompt = self._create_analysis_prompt(article)
        
        response = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": self.config["llm"]["model"],
                "prompt": prompt,
                "stream": False
            }
        )
        
        result = response.json()["response"]
        should_read = result.lower().startswith("yes")
        return should_read, result.split("\n")[1]  # Return decision and explanation

    def _create_analysis_prompt(self, article: Dict) -> str:
        personas = "\n\n".join([
            f"As a {p['role']} ({p['priority']} priority):\n{p['context']}"
            for p in self.config["personas"]
        ])
                
        avoid = "\n".join([f"- {item}" for item in self.config["avoid"]])
        
        return f"""Given the following context about me and my interests, analyze this article and determine if I should read it.

MY CONTEXT:
{personas}

CONTENT TO AVOID:
{avoid}

ARTICLE:
Title: {article["title"]}
URL: {article["link"]}
Content:
{article["content"]}

Respond in this format:
RELEVANT ASPECTS: [List the specific aspects of my personas/interests this relates to]
DECISION: [Yes/No]
REASON: [Brief explanation focusing on the value to my specific personas/interests and why the decision was made]"""

    @backoff.on_exception(backoff.expo, 
                         RequestException,
                         max_tries=5)
    def _post_to_raindrop(self, payload: dict, headers: dict) -> None:
        """Post to Raindrop.io with exponential backoff on failures."""
        response = requests.post(
            "https://api.raindrop.io/rest/v1/raindrops",
            headers=headers,
            json=payload
        )
        response.raise_for_status()
        return response

    def save_to_raindrop(self, recommendations: List[Tuple[Dict, bool, str]]):
        """Save recommended articles to Raindrop.io in batches"""
        if not self.config.get("raindrop", {}).get("enabled", False):
            return

        # Pull RAINDROP_TOKEN from environment
        if "RAINDROP_TOKEN" in os.environ:
            token = os.environ["RAINDROP_TOKEN"]

        if not token:
            print("Raindrop.io token not configured, skipping")
            return

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {token}'
        }

        read_articles = [(a, r) for a, should_read, r in recommendations if should_read]
        
        # Prepare all raindrops
        raindrops = []
        for article, (aspects, reason) in read_articles:
            raindrops.append({
                "link": article["link"],
                "title": article["title"],
                "excerpt": reason,
                "collection": {
                    "$id": self.config["raindrop"]["collection_id"]
                }
            })
        
        # Process in batches of 100
        batch_size = 100
        for i in range(0, len(raindrops), batch_size):
            batch = raindrops[i:i + batch_size]
            payload = {"items": batch}
            
            try:
                self._post_to_raindrop(payload, headers)
                print(f"Saved batch of {len(batch)} articles to Raindrop.io")
                
                # Print individual article titles for visibility
                for article in batch:
                    print(f"  - {article['title']}")
            except Exception as e:
                print(f"Error saving batch to Raindrop.io: {str(e)}")

    def send_email(self, recommendations: List[Tuple[Dict, bool, str]]):
        read_articles = [(a, r) for a, should_read, r in recommendations if should_read]
        skip_articles = [(a, r) for a, should_read, r in recommendations if not should_read]
        
        email_body = self._create_email_body(read_articles, skip_articles)
        
        msg = MIMEMultipart()
        msg["From"] = self.config["email"]["from_address"]
        msg["To"] = self.config["email"]["to_address"]
        msg["Subject"] = f"RSS Feed Digest - {datetime.datetime.now().strftime('%Y-%m-%d')}"
        
        msg.attach(MIMEText(email_body, "html"))
        
        smtp_class = smtplib.SMTP_SSL if self.config["email"]["smtp_port"] == 465 else smtplib.SMTP
        with smtp_class(self.config["email"]["smtp_server"], self.config["email"]["smtp_port"]) as server:
            if self.config["email"]["smtp_port"] != 465:
                server.starttls()
            username = os.environ["EMAIL_USERNAME"]
            password = os.environ["EMAIL_PASSWORD"]

            # If username or password are not set, return an error and output the msg to console
            if not username or not password:
                print("Email username or password not configured, skipping email")
                print(email_body)
                return

            server.login(
                username,
                password
            )
            server.send_message(msg)

    def _create_email_body(self, read_articles: List[Tuple[Dict, str]], skip_articles: List[Tuple[Dict, str]]) -> str:
        total_time_saved = sum(article["reading_time"] for article, _ in skip_articles)
        
        html = """
        <html>
        <body style="font-family: Arial, sans-serif;">
        <h2>Articles to Read ({count})</h2>
        <ul>
        {read_list}
        </ul>
        
        <h2>Articles to Skip ({skip_count})</h2>
        <p style="color: green;"><strong>Time saved by skipping: approximately {time_saved} minutes</strong></p>
        <ul>
        {skip_list}
        </ul>
        </body>
        </html>
        """
        
        def format_article(article, analysis):
            aspects, reason = analysis
            return f'''
            <li>
                <h3><a href="{article["link"]}">{article["title"]}</a></h3>
                <p><strong>Estimated reading time:</strong> {article["reading_time"]} minutes</p>
                <p><strong>Relevant to:</strong> {aspects}</p>
                <p><strong>Reason:</strong> {reason}</p>
            </li>'''
        
        read_list = "\n".join(
            format_article(article, reason)
            for article, reason in read_articles
        )
        
        skip_list = "\n".join(
            format_article(article, reason)
            for article, reason in skip_articles
        )
        
        return html.format(
            count=len(read_articles),
            skip_count=len(skip_articles),
            read_list=read_list,
            skip_list=skip_list,
            time_saved=total_time_saved
        )

    def process_articles(self):
        """Main method to process articles and send notifications."""
        articles = self.get_new_articles()

        recommendations = []
        for article in articles:
            should_read, reason = self.analyze_article(article)
            recommendations.append((article, should_read, reason))
        
        # Save state after processing
        self._save_state()
        
        # Send email if configured
        if self.config.get("email", {}).get("enabled", False):
            self.send_email(recommendations)
        
        # Save to Raindrop if configured
        if self.config.get("raindrop", {}).get("enabled", False):
            self.save_to_raindrop(recommendations)

def main():
    parser = argparse.ArgumentParser(description='RSS Feed Analyzer')
    parser.add_argument('--all', action='store_true', help='Process all articles regardless of last run time')
    args = parser.parse_args()
    
    analyzer = RSSAnalyzer(process_all=args.all)
    analyzer.process_articles()

if __name__ == "__main__":
    main()
