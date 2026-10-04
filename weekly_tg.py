"""ruanyf/weekly -> 繁體 Telegraph 全文頁 + Telegram 頻道索引訊息。

流程：偵測 docs/ 內大於 state.last_issue 的新期數 -> 抓 Markdown -> 轉 HTML ->
轉成 Telegraph nodes（文字節點簡轉繁）-> 建 Telegraph 頁 -> 發頻道索引訊息 -> 更新 state.json。
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from markdown_it import MarkdownIt
from opencc import OpenCC

REPO = "ruanyf/weekly"
STATE_PATH = Path(__file__).with_name("state.json")
TELEGRAPH_API = "https://api.telegra.ph"
TELEGRAPH_MAX_BYTES = 64 * 1024
AUTHOR_NAME = "阮一峰"
AUTHOR_URL = "https://www.ruanyifeng.com/blog/"
KEEPALIVE_DAYS = 25  # 無新期時，每隔這麼久 commit 一次，避免排程被 60 天無活動停用
MAX_PER_RUN = 3  # 一次最多補發幾期，避免積壓時洗版
SKIP_IN_INDEX = {"封面圖", "往年回顧"}  # 索引訊息不列的章節（轉繁體後的標題）
DROPPABLE_SECTION = "往年回顧"  # 超過 Telegraph 64KB 時最先捨棄的章節

_cc = OpenCC("s2twp")


def to_tw(text: str) -> str:
    return _cc.convert(text)


# ---------- HTTP ----------

def _request(url: str, *, data: bytes | None = None, headers: dict | None = None, label: str) -> bytes:
    """所有對外請求的唯一出口。錯誤訊息只帶 label，避免 token 隨 URL 進公開 log。"""
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            body = json.loads(e.read())
            detail = f" ({body.get('description') or body.get('error') or body.get('message')})"
        except Exception:
            pass
        raise RuntimeError(f"{label}: HTTP {e.code}{detail}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"{label}: {e.reason}") from None


def with_flood_retry(fn, attempts: int = 4):
    """碰到 Telegraph FLOOD_WAIT_n 或 Telegram 'retry after n' 就等待後重試。"""
    for i in range(attempts):
        try:
            return fn()
        except RuntimeError as e:
            m = re.search(r"FLOOD_WAIT_(\d+)|retry after (\d+)", str(e))
            if not m or i == attempts - 1:
                raise
            wait = int(m.group(1) or m.group(2)) + 1
            print(f"rate limited, sleep {wait}s")
            time.sleep(wait)


def parse_range(spec: str) -> tuple[int, int]:
    m = re.fullmatch(r"(\d+)-(\d+)", spec)
    if not m or int(m.group(1)) > int(m.group(2)):
        raise SystemExit(f"--backfill 格式應為「起-訖」（例如 380-412），收到：{spec}")
    return int(m.group(1)), int(m.group(2))


def github_get(path: str, raw: bool = False) -> bytes:
    headers = {
        "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
        "User-Agent": "ruanyf-weekly-tg",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return _request(f"https://api.github.com/{path}", headers=headers, label=f"github {path}")


def list_issue_numbers() -> list[int]:
    entries = json.loads(github_get(f"repos/{REPO}/contents/docs"))
    nums = []
    for entry in entries:
        m = re.fullmatch(r"issue-(\d+)\.md", entry["name"])
        if m:
            nums.append(int(m.group(1)))
    return sorted(nums)


def fetch_issue_markdown(n: int) -> str:
    return github_get(f"repos/{REPO}/contents/docs/issue-{n}.md", raw=True).decode("utf-8")


# ---------- Markdown -> Telegraph nodes ----------

TAG_MAP = {"h2": "h3", "h3": "h4", "h4": "h4", "h5": "h4", "h6": "h4", "b": "strong", "i": "em", "del": "s"}
ALLOWED = {"a", "blockquote", "br", "code", "em", "figure", "h3", "h4", "hr", "img",
           "li", "ol", "p", "pre", "s", "strong", "u", "ul"}
VOID = {"img", "br", "hr"}
BLOCK_CONTAINERS = {"ul", "ol", "blockquote", "root"}


def _node(tag: str, attrs: dict | None = None, children: list | None = None) -> dict:
    node: dict = {"tag": tag}
    if attrs:
        node["attrs"] = attrs
    if children:
        node["children"] = children
    return node


class _NodeBuilder(HTMLParser):
    """把 markdown-it 產出的 HTML 轉成 Telegraph nodes；h1 另外收成頁面標題。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root: dict = _node("root", children=[])
        self.stack: list[dict] = [self.root]
        self.title_parts: list[str] | None = None
        self.title = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "h1":
            self.title_parts = []
            return
        tag = TAG_MAP.get(tag, tag)
        if tag not in ALLOWED:
            tag = "p"
        keep = {}
        if tag == "a" and attrs.get("href"):
            keep["href"] = attrs["href"]
        if tag == "img" and attrs.get("src"):
            keep["src"] = attrs["src"]
        node = _node(tag, keep)
        if self.title_parts is not None:
            return
        self.stack[-1].setdefault("children", []).append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "h1":
            self.title = "".join(self.title_parts or []).strip()
            self.title_parts = None
            return
        if tag in VOID or self.title_parts is not None:
            return
        if len(self.stack) > 1:
            self.stack.pop()

    def handle_data(self, data):
        if self.title_parts is not None:
            self.title_parts.append(data)
            return
        if data.strip() == "" and self.stack[-1].get("tag") in BLOCK_CONTAINERS:
            return  # 區塊容器之間的換行，不是內容
        self.stack[-1].setdefault("children", []).append(data)


def _convert_text(nodes: list) -> list:
    out = []
    for n in nodes:
        if isinstance(n, str):
            out.append(to_tw(n))
        else:
            if "children" in n:
                n["children"] = _convert_text(n["children"])
            out.append(n)
    return out


def _images_to_figures(nodes: list) -> list:
    """獨佔一段的圖片 <p><img></p> 改成 Telegraph 的 <figure><img></figure>。"""
    out = []
    for n in nodes:
        if isinstance(n, dict):
            kids = n.get("children", [])
            if n["tag"] == "p" and len(kids) == 1 and isinstance(kids[0], dict) and kids[0]["tag"] == "img":
                n = _node("figure", children=kids)
            elif kids:
                n["children"] = _images_to_figures(kids)
        out.append(n)
    return out


def _plain(node) -> str:
    if isinstance(node, str):
        return node
    return "".join(_plain(c) for c in node.get("children", []))


def drop_section(nodes: list, title: str) -> list:
    """捨棄標題為 title 的 h3 章節（到下一個 h3 為止）。"""
    out, skipping = [], False
    for n in nodes:
        if isinstance(n, dict) and n["tag"] == "h3":
            skipping = _plain(n).strip() == title
        if not skipping:
            out.append(n)
    return out


class Issue:
    def __init__(self, number: int, title: str, sections: list[str], nodes: list):
        self.number = number
        self.title = title
        self.sections = sections
        self.nodes = nodes


def parse_issue(number: int, markdown: str) -> Issue:
    rendered = MarkdownIt("commonmark", {"html": False}).render(markdown)  # 原始 HTML 當文字，不放行標籤
    builder = _NodeBuilder()
    builder.feed(rendered)
    builder.close()
    nodes = _images_to_figures(builder.root.get("children", []))
    nodes = _convert_text(nodes)
    title = to_tw(builder.title) or f"科技愛好者週刊（第 {number} 期）"
    sections = [_plain(n).strip() for n in nodes if isinstance(n, dict) and n["tag"] == "h3"]
    return Issue(number, title, sections, nodes)


def github_url(number: int) -> str:
    return f"https://github.com/{REPO}/blob/master/docs/issue-{number}.md"


def with_attribution(issue: Issue) -> list:
    """頁首、頁尾的署名與授權聲明（原文為 CC BY-NC-ND 3.0，須署名、非商用）。"""
    src = github_url(issue.number)
    head = _node("p", children=[_node("em", children=[
        "繁體轉換版・作者 ", _node("a", {"href": AUTHOR_URL}, ["阮一峰"]),
        "・原文（簡體）：", _node("a", {"href": src}, [f"科技愛好者週刊第 {issue.number} 期"]),
    ])])
    foot = _node("p", children=[_node("em", children=[
        "本文轉載自阮一峰《科技愛好者週刊》第 ", str(issue.number), " 期，作者：阮一峰，原文：",
        _node("a", {"href": src}, [src]),
        "。版權聲明：自由轉載－非商用－非衍生－保持署名（創意共享 3.0 許可證）。本頁由程式自動簡轉繁，非官方版本。",
    ])])
    return [head, *issue.nodes, _node("hr"), foot]


def serialize_content(issue: Issue) -> str:
    """序列化成 Telegraph content；ensure_ascii=False 避免中文被轉成 \\uXXXX 撐破 64KB 上限。"""
    nodes = with_attribution(issue)
    content = json.dumps(nodes, ensure_ascii=False, separators=(",", ":"))
    if len(content.encode("utf-8")) > TELEGRAPH_MAX_BYTES:
        trimmed = Issue(issue.number, issue.title, issue.sections, drop_section(issue.nodes, DROPPABLE_SECTION))
        content = json.dumps(with_attribution(trimmed), ensure_ascii=False, separators=(",", ":"))
    size = len(content.encode("utf-8"))
    if size > TELEGRAPH_MAX_BYTES:
        raise RuntimeError(f"issue {issue.number}: content {size} bytes 超過 Telegraph 上限 {TELEGRAPH_MAX_BYTES}")
    return content


# ---------- Telegraph ----------

def telegraph_call(method: str, **params: str) -> dict:
    body = urllib.parse.urlencode(params).encode()
    raw = _request(f"{TELEGRAPH_API}/{method}", data=body,
                   headers={"Content-Type": "application/x-www-form-urlencoded"}, label=f"telegraph {method}")
    resp = json.loads(raw)
    if not resp.get("ok"):
        raise RuntimeError(f"telegraph {method}: {resp.get('error')}")
    return resp["result"]


def telegraph_create_account() -> str:
    result = telegraph_call("createAccount", short_name="weekly-tw", author_name=AUTHOR_NAME, author_url=AUTHOR_URL)
    return result["access_token"]


def telegraph_create_page(token: str, issue: Issue) -> str:
    result = telegraph_call(
        "createPage", access_token=token, title=issue.title[:256], author_name=AUTHOR_NAME,
        author_url=AUTHOR_URL, content=serialize_content(issue), return_content="false",
    )
    return result["url"]


# ---------- Telegram ----------

def build_index_text(issue: Issue) -> str:
    lines = [f"<b>{html.escape(issue.title)}</b>", "", "本期內容："]
    lines += [f"・{html.escape(s)}" for s in issue.sections if s not in SKIP_IN_INDEX]
    return "\n".join(lines)


def telegram_send_index(bot_token: str, chat_id: str, issue: Issue, page_url: str) -> None:
    params = {
        "chat_id": chat_id,
        "text": build_index_text(issue),
        "parse_mode": "HTML",
        "reply_markup": json.dumps({"inline_keyboard": [[{"text": "閱讀全文", "url": page_url}]]}, ensure_ascii=False),
        "link_preview_options": json.dumps({"url": page_url, "prefer_large_media": False}),
    }
    raw = _request(f"https://api.telegram.org/bot{bot_token}/sendMessage", data=urllib.parse.urlencode(params).encode(),
                   headers={"Content-Type": "application/x-www-form-urlencoded"}, label="telegram sendMessage")
    if not json.loads(raw).get("ok"):
        raise RuntimeError("telegram sendMessage: not ok")


# ---------- State ----------

def load_state() -> dict:
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def pick_pending(available: list[int], last_issue: int, forced: int | None) -> list[int]:
    """依序處理所有大於 last_issue 的期數（不是只取最大值），積壓過多時只補最新幾期。"""
    if forced is not None:
        return [forced]
    return [n for n in available if n > last_issue][-MAX_PER_RUN:]


def keepalive_due(state: dict, today: dt.date) -> bool:
    last = state.get("last_checked")
    if not last:
        return True
    return (today - dt.date.fromisoformat(last)).days >= KEEPALIVE_DAYS


# ---------- CLI ----------

def _require_env(*names: str) -> dict:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise SystemExit(f"缺少環境變數：{', '.join(missing)}")
    return {n: os.environ[n] for n in names}


def process_issue(n: int, args: argparse.Namespace, state: dict) -> None:
    issue = parse_issue(n, fetch_issue_markdown(n))
    size = len(serialize_content(issue).encode("utf-8"))
    print(f"issue {n}: {issue.title} | sections={len(issue.sections)} | content={size}B")
    if args.dry_run:
        print(build_index_text(issue))
        return

    if args.telegraph_only:
        token = os.environ.get("TELEGRAPH_TOKEN") or telegraph_create_account()
        if args.token_out and not os.environ.get("TELEGRAPH_TOKEN"):
            fd = os.open(args.token_out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(token)
        print(f"telegraph: {with_flood_retry(lambda: telegraph_create_page(token, issue))}")
        return

    env = _require_env("TG_BOT_TOKEN", "TG_CHAT_ID", "TELEGRAPH_TOKEN")
    page_url = with_flood_retry(lambda: telegraph_create_page(env["TELEGRAPH_TOKEN"], issue))
    print(f"telegraph: {page_url}")
    with_flood_retry(lambda: telegram_send_index(env["TG_BOT_TOKEN"], env["TG_CHAT_ID"], issue, page_url))
    print(f"issue {n}: sent")
    if n > state["last_issue"]:  # 先寫 state 再處理下一期，部分成功也不會重發
        state["last_issue"] = n
    state["last_sent"] = dt.date.today().isoformat()
    save_state(state)


def backfill(args: argparse.Namespace, state: dict) -> int:
    """依序補發 [起, 訖] 期。進度記在 state['backfilled']，中途失敗重跑不會重複發；單期失敗不中斷其他期。"""
    lo, hi = parse_range(args.backfill)
    available = set(list_issue_numbers())
    real_send = not (args.dry_run or args.telegraph_only)
    failed: list[int] = []
    for n in range(lo, hi + 1):
        if n in state.get("backfilled", []):
            print(f"issue {n}: 已補發過，略過")
            continue
        if n not in available:
            print(f"issue {n}: repo 內不存在，略過")
            continue
        try:
            process_issue(n, args, state)
        except RuntimeError as e:
            print(f"issue {n}: FAILED {e}")
            failed.append(n)
            continue
        if real_send:
            state.setdefault("backfilled", []).append(n)
            save_state(state)
            time.sleep(args.delay)
    if failed:
        print(f"補發失敗的期數：{failed}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--issue", type=int, help="強制處理指定期數（忽略 state 偵測）")
    p.add_argument("--dry-run", action="store_true", help="只抓取與轉換，不呼叫 Telegraph/Telegram，不改 state")
    p.add_argument("--telegraph-only", action="store_true", help="只建 Telegraph 頁，不發 Telegram，不改 state")
    p.add_argument("--token-out", help="--telegraph-only 且無 TELEGRAPH_TOKEN 時，把臨時帳號 token 寫入此檔（0600）")
    p.add_argument("--bootstrap-telegraph", action="store_true", help="建立 Telegraph 帳號並把 token 印到 stdout（用於管線給 gh secret set）")
    p.add_argument("--backfill", help="補發歷史期數，格式「起-訖」（含兩端），依期數由小到大逐期發送")
    p.add_argument("--delay", type=float, default=4.0, help="補發時每期之間的間隔秒數（避開頻道每分鐘 20 則上限）")
    args = p.parse_args(argv)

    if args.bootstrap_telegraph:
        sys.stdout.write(telegraph_create_account())
        return 0

    state = load_state()
    if args.backfill:
        return backfill(args, state)
    pending = pick_pending(list_issue_numbers(), state["last_issue"], args.issue)
    today = dt.date.today()
    if not pending:
        print(f"沒有新期數（last_issue={state['last_issue']}）")
        if not (args.dry_run or args.telegraph_only) and keepalive_due(state, today):
            state["last_checked"] = today.isoformat()
            save_state(state)
            print("keepalive: 更新 last_checked")
        return 0
    for n in pending:
        process_issue(n, args, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
