# AIOS Lite 使用說明

不需要 GPU。模型在雲端執行，用你自己的 API key（BYOK）。適合先試用 AIOS、筆電或文書機，或代理數量不多的個人。

> 其他產品線與完整收費表見 [EDITIONS.zh-TW.md](EDITIONS.zh-TW.md)（原始碼目錄裡是 `editions/README.zh-TW.md`）。

## 收費

| 項目 | 費用 |
|---|---|
| AIOS | **免費**（Core，MIT 授權），包含所有安全功能 |
| 模型 | 你直接付給模型供應商（例如 Anthropic），AIOS 不經手也不加價 |

模型費用由供應商以美金計價。Lite 預設每次執行最多 5 萬 token、每個代理每 24 小時最多 200 萬 token，避免帳單失控（見「調整」）。

## 硬體需求

- 任何 4 核心 CPU、8 GB RAM，不需要 GPU
- Windows、macOS 或 Linux
- Python 3.11 以上
- 可同時執行約 200 個代理，實際上限是你的 API 帳號的速率限制

## 安裝

1. 解開 `aios-lite-<版本>.zip`，進入 `aios-lite` 資料夾。
2. 安裝套件：

   ```bash
   python start.py install        # 安裝 anthropic 與 mcp
   ```

3. 設定 API key：把 `aios.env.example` 複製成 `aios.env`，填入 `ANTHROPIC_API_KEY=sk-ant-...`（請設成只有你能讀）。也可以直接設成環境變數。
4. 檢查：

   ```bash
   python start.py check          # 確認 API key 已設定，並列出這台機器的沙箱能力
   ```

## 開始使用

```bash
python start.py chat                       # 和代理 "main" 對話
python start.py chat --agent lead          # 讓代理一次把子任務分給很多個 worker
```

對話中可用 `/attach 檔案` 附圖片或 PDF、`/remember 鍵 值` 設定記憶、`/audit` 看審計日誌、`/quit` 離開。工作檔案放在 `workspace/`，狀態存在 `aios.db`。

**定時任務：**

```bash
python start.py jobs add digest "把 inbox.txt 摘要寫到 reports/digest.txt" --schedule "daily 09:00" --agent reporter
python start.py daemon                     # 保持執行；或用 daemon --once 搭配排程器
```

**一次跑很多任務：**

```bash
python start.py swarm --agent worker --tasks tasks.txt --out results.jsonl --reduce editor
```

Lite 預設最多 16 個任務同時執行，避免撞到 API 速率限制。

## 代理有哪些權限

設定在 `policy.toml`，可以直接改：

| 代理 | 用途 | 可以做什麼 |
|---|---|---|
| `main` | 日常對話 | 讀整個工作區、抓 https 網頁（每次最多 10 次）、記憶；只能寫 `notes/` 和 `out/`，每次寫入前會問你 |
| `reporter` | 定時任務 | 讀工作區，只能寫 `reports/`（不用問，因為沒人在場） |
| `lead` | 分派任務 | 只能把子任務交給 `worker` 和 `editor` |
| `worker` | 被分派的任務 | 唯讀 |
| `editor` | 彙整結果 | 什麼工具都沒有，只讀 worker 的回覆 |

`main` 每小時最多 120 次模型呼叫，`reporter` 每小時 60 次，防止失控的迴圈燒掉 API 額度。

## 調整

在指令後面加參數就會蓋過預設值：

```bash
python start.py chat --model <模型名稱>                     # 換模型（預設用 AIOS 內建的 Claude 模型）
python start.py chat --daily-max-tokens 500000               # 每天上限調低
python start.py chat --max-cost 0.5 --price-in 3 --price-out 15   # 每次執行最多 0.5 美金（價格依模型填）
```

**用其他雲端模型：** 任何 OpenAI 相容的服務都可以，把 key 放在 `OPENAI_API_KEY`：

```bash
python start.py chat --provider openai --base-url https://api.example.com/v1 --model <模型名稱>
```

## 資料去哪裡

你的對話和代理讀到的檔案內容會送到模型供應商。記憶、審計日誌、工作區都只在你的電腦上。資料不能離開公司的話，請用 Personal 以上的產品線（模型在本機跑）。

## 什麼時候該升級

- 不想把資料送到雲端，或 API 帳單每月超過一張 16 GB 顯示卡的分期 → **Personal**
- 要同時跑幾百個代理，或多人共用 → **Workstation**

## 疑難排解

| 狀況 | 處理 |
|---|---|
| `missing: $ANTHROPIC_API_KEY` | 檢查 `aios.env` 是否和 `start.py` 放在同一個資料夾 |
| 回覆說已達每日上限 | 已到每日上限，24 小時內的用量往後移就會恢復；或加 `--daily-max-tokens` 調高 |
| API 回報速率限制（HTTP 429） | `swarm` 加 `--model-slots 4` 降低並行 |
