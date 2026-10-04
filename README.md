# ruanyf-weekly-tg

把阮一峰《[科技爱好者周刊](https://github.com/ruanyf/weekly)》每期自動轉成**繁體（台灣用語）**，發到 Telegram 頻道。

```
GitHub Actions（週五 21:17、週六 09:23，台北時間）
  → 偵測 docs/ 內大於 state.json.last_issue 的新期數
  → 抓 Markdown → OpenCC s2twp 簡轉繁（只轉文字節點，網址不動）
  → 建 Telegraph 全文頁（圖片、連結 1:1）
  → 頻道發索引訊息（標題 + 章節清單 + 「閱讀全文」按鈕）
  → 成功才把 last_issue 寫回 state.json
```

沒有新期（例如假期休息）就靜默結束；超過 25 天沒動作時，會更新 `last_checked` 並 commit，
避免公開 repo 因 60 天無活動被 GitHub 自動停用排程。

## 設定

Repo Secrets：

| 名稱 | 說明 |
|---|---|
| `TG_BOT_TOKEN` | BotFather 給的 bot token |
| `TG_CHAT_ID` | 頻道，如 `@channel_name` 或 `-100…` |
| `TELEGRAPH_TOKEN` | 以 `python weekly_tg.py --bootstrap-telegraph \| gh secret set TELEGRAPH_TOKEN` 建立 |

Bot 需為頻道管理員並有發佈訊息權限。

## 本機開發

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest
.venv/bin/python weekly_tg.py --dry-run --issue 413        # 只抓取與轉換
.venv/bin/python weekly_tg.py --telegraph-only --issue 413 # 只建 Telegraph 頁
```

## 費用

標準 GitHub-hosted runner 在公開 repo 免費；Telegram Bot API、Telegraph 免費；全程不使用 LLM。
workflow 內有 `timeout-minutes: 5`，且**不可**改用 larger runner（即使公開 repo 也會計費）。

## 授權與署名

原作者阮一峰，原文授權為 **CC BY-NC-ND 3.0**（自由轉載、非商用、非衍生、保持署名）。
本專案**不含**任何原文內容（執行時才抓取）；產出的頁面會在頁首、頁尾標註作者與原文連結，並註明「由程式自動簡轉繁」。
簡轉繁是否構成「衍生」屬灰色地帶，使用者自行評估風險；請勿用於商業用途。程式碼以 MIT 授權。
