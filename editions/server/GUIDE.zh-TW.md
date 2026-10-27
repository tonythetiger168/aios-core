# AIOS Server 使用說明

一台 Linux 伺服器加一張 96 GB 顯示卡，單台撐 1,000 到 5,000 個代理。代理寫的程式一律在用完即丟的 Docker 容器裡執行，代理群分散到多個 CPU 核心，審計紀錄另外保存一份，就算有人改了資料庫也查得出來。適合部門或公司內部共用。

> 其他產品線與完整收費表見 [EDITIONS.zh-TW.md](EDITIONS.zh-TW.md)（原始碼目錄裡是 `editions/README.zh-TW.md`）。

## 收費

| 方案 | 費用（美金） | 適合 |
|---|---|---|
| Team（自架） | **US$15／人／月**，年繳，最少 5 人；3 人以下免費 | 一般團隊（例如 20 人 = US$3,600／年） |
| Enterprise | **報價**，建議 US$20,000／年起 | 需要客製合約、支援、合規文件 |

- 所有安全功能（權限、審批、污染追蹤、沙箱、審計）都在免費的 Core 裡，不需付費也能用。付費買的是多人管理：SSO 與角色、Postgres、多台 worker、審計匯出、機密管理、集中政策。
- **Team 功能還在開發中。** 這個套件目前只含 Core；Team 推出後會另外提供套件和離線驗證的授權檔（不回傳資料，到期有 30 天寬限期）。
- 模型免費（開源模型，本機執行）。硬體自備，參考價約新台幣 45–55 萬元。

## 硬體需求

- 32 核心 64 執行緒、256 GB RAM
- 96 GB VRAM 的 NVIDIA 顯示卡（例如 RTX PRO 6000）
- Linux（建議 Ubuntu 22.04 以上）、Docker、Python 3.11 以上
- 跑 8B 模型時約 150–200 個代理同時即時回應；背景批次約 5,000 個（從 24 GB 卡的實測換算，96 GB 卡尚未實測）。4,096 個代理時排程器延遲約 1.9 秒，所以這一檔的代理群預設分散到多個 CPU 核心（`--workers 0`）

## 安裝

1. 安裝 Docker 和 NVIDIA Container Toolkit，啟動 vLLM：

   ```bash
   docker run -d --gpus all --name vllm -p 127.0.0.1:8000:8000 --ipc=host \
     -v ~/.cache/huggingface:/root/.cache/huggingface \
     vllm/vllm-openai --model Qwen/Qwen2.5-7B-Instruct --max-model-len 32768 --gpu-memory-utilization 0.9
   ```

   只聽 `127.0.0.1`，不要把模型伺服器開到網路上。

2. 準備程式碼沙箱的映像檔，並鎖定版本：

   ```bash
   docker pull python:3.12-slim
   docker image inspect --format '{{index .RepoDigests 0}}' python:3.12-slim
   # 把印出來的 python@sha256:... 放進 aios.env：AIOS_DOCKER_IMAGE=python@sha256:...
   ```

3. 解開 `aios-server-<版本>.zip` 到例如 `/opt/aios-server`：

   ```bash
   python3 start.py install
   python3 start.py check            # vLLM、Docker 沙箱、審計鏈都要是正常的
   ```

## 開始使用

```bash
python3 start.py chat                       # 互動對話（run_python 在 Docker 容器裡執行）
python3 start.py console --approve          # 只聽 127.0.0.1；遠端請用 ssh -L 8765:127.0.0.1:8765
```

**常駐守護程式：**

```bash
python3 start.py service systemd --home /opt/aios-server   # 印出 systemd 設定與安裝指令，確認後自己執行
```

守護程式預設：

- `--approvals console`：高風險動作等人在控制台或 Telegram 核准
- `--anchor audit-anchors.txt`：每小時把審計鏈的鏈頭寫到這個檔案。請把它放到資料庫以外的地方（另一顆磁碟或定期備份），之後用 `python3 aios.py audit --verify --anchors audit-anchors.txt` 檢查，連整條鏈被重算都抓得到
- 想把每筆審計即時送到日誌系統，加 `--audit-sink https://你的收集端/...`

**大量任務：**

```bash
python3 start.py swarm --agent worker --tasks tasks.txt --out results.jsonl --reduce editor
```

Server 預設同時 512 個任務、192 個模型呼叫，分散到「核心數減一」個行程。

## 代理有哪些權限

設定在 `policy.toml`，可以直接改：

| 代理 | 用途 | 可以做什麼 |
|---|---|---|
| `main` | 日常對話 | 讀整個工作區、抓 https 網頁（每次最多 10 次）、記憶；只能寫 `notes/` 和 `out/`，每次寫入前會問你 |
| `reporter` | 定時任務 | 讀工作區，只能寫 `reports/`（預先允許）；其他高風險動作要有人核准 |
| `lead` | 分派任務 | 只能把子任務交給 `worker` 和 `editor`，每次最多 5,000 個 |
| `worker` | 被分派的任務 | 唯讀 |
| `editor` | 彙整結果 | 什麼工具都沒有，只讀 worker 的回覆 |

部門共用時，在 `policy.toml` 為每個人或每個用途複製一個代理區段，權限只開需要的。外部工具（MCP 伺服器）建議一律加 `--offline-mcp` 或 `--mcp-egress` 限制網路。

## 調整

```bash
python3 start.py chat --model meta-llama/Llama-3.1-8B-Instruct   # vLLM 要先 serve 同一個模型
python3 start.py swarm --workers 8 ...                           # 指定行程數
python3 start.py chat --sandbox microvm                          # 更強的隔離（實驗性，需 KVM，見 docs/microvm-sandbox.md）
```

## 什麼時候該升級

- 代理數超過一台 Server（約 5,000 個），或 GPU 一天用不到約 8 小時 → **Cloud**（租用比買划算）

## 疑難排解

| 狀況 | 處理 |
|---|---|
| `--sandbox docker` 啟動時報錯 | 依錯誤訊息處理；rootless Docker 沒有委派 cgroup 時無法限制資源，AIOS 會拒絕使用（見 docs/docker-sandbox-validation.md） |
| 代理群很多任務逾時 | 超過 GPU 能消化的量了；降低 `--parallel` 或拉長 `--timeout` |
| `aios status` 回報警示 | `python3 aios.py alerts` 查看，處理後 `--ack` |
