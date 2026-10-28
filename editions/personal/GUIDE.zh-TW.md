# AIOS Personal 使用說明

一張 16 GB 顯示卡，模型用 Ollama 在你的電腦上跑，**資料完全不離開這台機器**，也沒有 API 帳單。適合個人，或資料不能送到雲端的工作。

> 其他產品線與完整收費表見 [EDITIONS.zh-TW.md](EDITIONS.zh-TW.md)（原始碼目錄裡是 `editions/README.zh-TW.md`）。

## 收費

| 項目 | 費用 |
|---|---|
| AIOS | **免費**（Core，MIT 授權），包含所有安全功能 |
| 模型 | 免費（Qwen2.5 3B 開源模型，本機執行） |
| 硬體 | 自備；參考價約新台幣 4.5–6 萬元 |

所有方案金額以新台幣計，Personal 不需要付任何授權費。

## 硬體需求

- 8 核心 16 執行緒、32 GB RAM
- 16 GB VRAM 的 NVIDIA 顯示卡（例如 RTX 5060 Ti 16GB）
- Windows、macOS 或 Linux，Python 3.11 以上
- 約 64 個代理同時在等回應時仍在 2 秒內回覆；背景批次可同時掛約 2,000 個

## 安裝

1. 安裝 [Ollama](https://ollama.com)，下載模型：

   ```bash
   ollama pull qwen2.5:3b            # 對話模型（約 2 GB）
   ollama pull nomic-embed-text      # 記憶語意搜尋用
   ```

   想讓很多代理同時跑得快一點，啟動 Ollama 前設定 `OLLAMA_NUM_PARALLEL=8`。

2. 解開 `aios-personal-<版本>.zip`，進入 `aios-personal` 資料夾：

   ```bash
   python start.py install           # 安裝 mcp（AIOS 本身只用 Python 標準函式庫）
   python start.py check             # 確認連得上 Ollama（http://localhost:11434），並列出沙箱能力
   ```

## 開始使用

```bash
python start.py chat                       # 和代理 "main" 對話
python start.py chat --agent lead          # 讓代理一次把子任務分給很多個 worker
```

對話中可用 `/attach 檔案` 附圖片或 PDF（需要支援視覺的模型）、`/search 關鍵字` 搜尋記憶、`/audit` 看審計日誌、`/quit` 離開。工作檔案放在 `workspace/`，狀態存在 `aios.db`。

**定時任務：**

```bash
python start.py jobs add digest "把 inbox.txt 摘要寫到 reports/digest.txt" --schedule "daily 09:00" --agent reporter
python start.py daemon
```

開機自動執行：`python start.py service windows --home .`（macOS 用 `launchd`、Linux 用 `systemd`），它會印出設定和安裝指令，你確認後自己執行。

**一次跑很多任務：**

```bash
python start.py swarm --agent worker --tasks tasks.txt --out results.jsonl --reduce editor
```

Personal 預設同時 64 個任務、64 個模型呼叫。

## 代理有哪些權限

設定在 `policy.toml`，可以直接改：

| 代理 | 用途 | 可以做什麼 |
|---|---|---|
| `main` | 日常對話 | 讀整個工作區、抓 https 網頁（每次最多 10 次）、記憶；只能寫 `notes/` 和 `out/`，每次寫入前會問你 |
| `reporter` | 定時任務 | 讀工作區，只能寫 `reports/`（不用問，因為沒人在場） |
| `lead` | 分派任務 | 只能把子任務交給 `worker` 和 `editor`，每次最多 2,000 個 |
| `worker` | 被分派的任務 | 唯讀 |
| `editor` | 彙整結果 | 什麼工具都沒有，只讀 worker 的回覆 |

## 調整

```bash
python start.py chat --model qwen2.5:7b               # 換 7B（4-bit 量化，16 GB 放得下，較慢但較聰明）
python start.py swarm --parallel 16 ...               # 顯示卡太熱或太慢時降低並行
```

**換成 vLLM（Linux 或 WSL2）：** 大量代理時吞吐量比 Ollama 高。實測一張 24 GB 卡用 vLLM 跑 Qwen2.5 3B，每秒處理約 60–80 個任務。

```bash
vllm serve Qwen/Qwen2.5-3B-Instruct --max-model-len 16384
python start.py chat --base-url http://localhost:8000/v1 --model Qwen/Qwen2.5-3B-Instruct
```

## 小模型的安全提醒

小模型比較容易被檔案或網頁裡藏的指令騙。AIOS 的防護（不可信內容標記、污染政策、審批）在測試中讓 3B 模型被注入成功的次數從 27 次中約 9 次降到 0 次，所以**不要用 `--auto-approve`**，也不要把 `reporter` 能寫的資料夾開得太大。

## 什麼時候該升級

- 要同時有幾百個代理即時回應，或想用 7B/8B 跑得快 → **Workstation**
- 超過 3 個人共用 → **Workstation** 搭配 Team 方案

## 疑難排解

| 狀況 | 處理 |
|---|---|
| `model server not reachable` | Ollama 沒啟動；執行 `ollama serve` 或打開 Ollama 程式 |
| 搜尋記憶時說連不到 embedding 伺服器 | 執行 `ollama pull nomic-embed-text`；不需要語意搜尋的話，把 `edition.toml` 裡的 `chat = [...]` 改成 `chat = []` |
| 回覆很慢 | `nvidia-smi` 確認模型在 GPU 上；降低 `--parallel` |
