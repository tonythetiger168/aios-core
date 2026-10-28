# AIOS Workstation 使用說明

一張 32 GB 顯示卡，用 vLLM 跑 7B/8B 模型，可同時讓幾百個代理即時回應。適合小團隊共用一台機器，或一個人要大量平行處理。定時任務碰到高風險動作會停下來，等你在瀏覽器或手機上按核准。

> 其他產品線與完整收費表見 [EDITIONS.zh-TW.md](EDITIONS.zh-TW.md)（原始碼目錄裡是 `editions/README.zh-TW.md`）。

## 收費

| 使用人數 | 方案 | 費用（新台幣） |
|---|---|---|
| 1–3 人 | Core 或 Team | **免費**（Core 為 MIT 授權，包含所有安全功能；Team 3 人以下也免費） |
| 4 人以上 | Team（自架） | **NT$490／人／月**，年繳，最少 5 人（5 人 = NT$29,400／年） |

- Team 功能（SSO 與角色、Postgres、多台 worker、審計匯出、機密管理、集中政策）**還在開發中**。這個套件目前只含 Core，多人共用時大家用同一個帳號和同一份政策；Team 推出後會另外提供套件與授權檔。
- 模型免費（開源模型，本機執行）。硬體自備，參考價約新台幣 18–25 萬元。

## 硬體需求

- 16 核心 32 執行緒、64–128 GB RAM
- 32 GB VRAM 的 NVIDIA 顯示卡（例如 RTX 5090）
- Linux，或 Windows 加 WSL2（vLLM 需要 Linux 環境）；Python 3.11 以上
- 約 200 個代理同時即時回應；背景批次約 6,000 個（從 24 GB 卡的實測換算，5090 尚未實測）

## 安裝

1. 安裝並啟動 vLLM（在 Linux 或 WSL2 裡）：

   ```bash
   pip install vllm
   vllm serve Qwen/Qwen2.5-7B-Instruct --max-model-len 16384 --gpu-memory-utilization 0.9
   ```

   第一次會下載模型（約 15 GB）。也可以用官方 Docker 映像檔 `vllm/vllm-openai`。

2. 解開 `aios-workstation-<版本>.zip`，進入 `aios-workstation` 資料夾：

   ```bash
   python start.py install           # 安裝 mcp
   python start.py check             # 確認連得上 vLLM（http://localhost:8000），並列出沙箱能力
   ```

3. 建議改用 Docker 沙箱執行代理寫的程式：`docker pull python:3.12-slim`，之後在指令加 `--sandbox docker`（或設定環境變數 `AIOS_SANDBOX=docker`）。

## 開始使用

```bash
python start.py chat                       # 和代理 "main" 對話
python start.py chat --agent lead          # 讓代理一次把子任務分給很多個 worker
python start.py console --open             # 在瀏覽器看審計日誌、任務、記憶（唯讀）
```

**定時任務與審批：** 這條產品線的 `daemon` 預設開啟審批（`--approvals console`）：任務要做政策沒預先允許的高風險動作時，會等人回答，240 秒沒人回答就拒絕。

```bash
python start.py jobs add digest "把 inbox.txt 摘要寫到 reports/digest.txt" --schedule "daily 09:00" --agent reporter
python start.py daemon                     # 終端機 1：執行任務
python start.py console --approve          # 終端機 2：在瀏覽器的 Approvals 頁按 Approve／Deny
```

想用手機核准：用 @BotFather 建一個 Telegram 機器人，把 token 放進 `aios.env` 的 `AIOS_TELEGRAM_TOKEN=`，再執行 `python start.py telegram --chat-id <你的 user id>`。

開機自動執行守護程式：`python start.py service systemd --home .`，它會印出服務設定與安裝指令（已帶上這條產品線的參數），你確認後自己執行。

**一次跑很多任務：**

```bash
python start.py swarm --agent worker --tasks tasks.txt --out results.jsonl --reduce editor
```

Workstation 預設同時 200 個任務、128 個模型呼叫。

## 代理有哪些權限

設定在 `policy.toml`，可以直接改：

| 代理 | 用途 | 可以做什麼 |
|---|---|---|
| `main` | 日常對話 | 讀整個工作區、抓 https 網頁（每次最多 10 次）、記憶；只能寫 `notes/` 和 `out/`，每次寫入前會問你 |
| `reporter` | 定時任務 | 讀工作區，只能寫 `reports/`（預先允許）；其他高風險動作要有人在控制台核准 |
| `lead` | 分派任務 | 只能把子任務交給 `worker` 和 `editor`，每次最多 6,000 個 |
| `worker` | 被分派的任務 | 唯讀 |
| `editor` | 彙整結果 | 什麼工具都沒有，只讀 worker 的回覆 |

多人共用時，每個人用不同的 `--agent` 名稱（例如複製 `main` 區段成 `alice`、`bob`），各自有獨立的歷史與記憶。

## 調整

```bash
python start.py chat --model meta-llama/Llama-3.1-8B-Instruct   # vLLM 要先 serve 同一個模型
python start.py swarm --parallel 64 --model-slots 64 ...        # 降低並行
python start.py chat --embed-model nomic-embed-text --embed-url http://localhost:11434/v1   # 語意搜尋記憶（需要 Ollama）
```

**選模型：** 沒有防護時，qwen2.5:7b 在注入測試中被騙 27 次中 15 次，llama3.1:8b 是 3 次；開啟 AIOS 防護後兩者都是 0 次。中文內容 Qwen 通常比較好，對安全要求高的工作可以考慮 Llama。

## 什麼時候該升級

- 單台要撐上千個代理、要 Docker 沙箱和審計外送 → **Server**
- GPU 一天用不到約 8 小時，或需要超過一台 Server → **Cloud**

## 疑難排解

| 狀況 | 處理 |
|---|---|
| `model server not reachable` | vLLM 還在下載或載入模型，看 vLLM 的輸出；WSL2 裡的服務要在 Windows 連 `localhost:8000` |
| vLLM 顯示記憶體不足 | 降低 `--max-model-len`，或 `--gpu-memory-utilization 0.85` |
| 定時任務一直被拒絕 | 沒有開 `console --approve` 或 Telegram，審批逾時就是拒絕；或把要寫的路徑加進政策的 `auto_paths` |
