import datetime as dt
import json

import pytest

import weekly_tg as w

SAMPLE = """# 科技爱好者周刊（第 999 期）：测试

这里是前言，**[通知] 假期休息**。

## 封面图

![](https://example.com/a.webp)

图片说明[链接](https://example.com/x)。

## 工具

> - 第一点
> - 第二点

1. 软件
2. 内存

## 往年回顾

[旧文](https://example.com/old)
"""


def test_title_and_sections_converted_to_traditional():
    issue = w.parse_issue(999, SAMPLE)
    assert issue.title == "科技愛好者週刊（第 999 期）：測試"
    assert issue.sections == ["封面圖", "工具", "往年回顧"]


def test_h2_maps_to_h3_and_h1_removed():
    issue = w.parse_issue(999, SAMPLE)
    tags = [n["tag"] for n in issue.nodes]
    assert "h1" not in tags and "h2" not in tags
    assert tags.count("h3") == 3


def test_standalone_image_becomes_figure():
    issue = w.parse_issue(999, SAMPLE)
    figures = [n for n in issue.nodes if n["tag"] == "figure"]
    assert figures == [{"tag": "figure", "children": [{"tag": "img", "attrs": {"src": "https://example.com/a.webp"}}]}]


def test_urls_not_converted_and_text_is_tw_terms():
    issue = w.parse_issue(999, SAMPLE)
    dumped = json.dumps(issue.nodes, ensure_ascii=False)
    assert "https://example.com/x" in dumped
    assert "軟體" in dumped and "記憶體" in dumped


def test_blockquote_list_is_kept():
    issue = w.parse_issue(999, SAMPLE)
    quote = next(n for n in issue.nodes if n["tag"] == "blockquote")
    assert quote["children"][0]["tag"] == "ul"
    assert len(quote["children"][0]["children"]) == 2


def test_only_allowed_tags():
    def walk(nodes):
        for n in nodes:
            if isinstance(n, dict):
                assert n["tag"] in w.ALLOWED | {"figure"}
                walk(n.get("children", []))

    walk(w.with_attribution(w.parse_issue(999, SAMPLE)))


def test_content_uses_raw_utf8_not_escapes():
    content = w.serialize_content(w.parse_issue(999, SAMPLE))
    assert "\\u" not in content and "假期休息" in content  # h1 是頁面標題欄，不在 content 內


def test_oversize_drops_past_reviews_then_fails_if_still_too_big():
    big = "## 工具\n\n" + "\n\n".join("段落" * 50 for _ in range(400)) + "\n"
    issue = w.parse_issue(1, "# t\n\n" + big)
    with pytest.raises(RuntimeError, match="超過"):
        w.serialize_content(issue)
    with_old = w.parse_issue(2, "# t\n\n## 工具\n\n短\n\n## 往年回顧\n\n" + "舊" * 70000 + "\n")
    content = w.serialize_content(with_old)
    assert "舊舊舊" not in content


def test_attribution_present():
    content = w.serialize_content(w.parse_issue(999, SAMPLE))
    assert "阮一峰" in content and "非衍生" in content
    assert "github.com/ruanyf/weekly/blob/master/docs/issue-999.md" in content


def test_index_text_escapes_and_skips_sections():
    issue = w.parse_issue(5, "# <b>&測试\n\n## 封面图\n\n## 工具\n\n## 往年回顾\n")
    text = w.build_index_text(issue)
    assert "&lt;b&gt;&amp;測試" in text
    assert "・工具" in text and "封面圖" not in text and "往年回顧" not in text


def test_pick_pending_iterates_all_new_in_order():
    assert w.pick_pending([410, 411, 412, 413], 411, None) == [412, 413]
    assert w.pick_pending([413], 413, None) == []
    assert w.pick_pending([1, 2, 3, 4, 5, 6], 1, None) == [4, 5, 6]  # 積壓只補最新 3 期
    assert w.pick_pending([413], 413, 400) == [400]


def test_keepalive_due():
    today = dt.date(2026, 10, 4)
    assert w.keepalive_due({}, today)
    assert not w.keepalive_due({"last_checked": "2026-09-20"}, today)
    assert w.keepalive_due({"last_checked": "2026-09-01"}, today)


def test_request_errors_do_not_leak_url(monkeypatch):
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(w.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError) as exc:
        w._request("https://api.telegram.org/botSECRET/sendMessage", label="telegram sendMessage")
    assert "SECRET" not in str(exc.value)
