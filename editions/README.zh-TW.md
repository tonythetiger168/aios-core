# AIOS 產品線

**English: [README.md](README.md)**

AIOS 依硬體分成五條產品線：四條在你自己的機器上跑，一條是雲端託管。四條本機產品線用的是同一份程式碼，差別在設定：接哪個模型伺服器、同時跑多少代理、沙箱用哪一種、代理有哪些權限。每條產品線都打包成一個 zip，解開後用 `python start.py` 啟動，不用自己記參數。

| 產品線 | 硬體 | 模型 | 同時代理數（即時／批次） | 收費方案 | 使用說明 |
|---|---|---|---|---|---|
| **Lite** | 任何 4 核心、8 GB RAM，不需要 GPU | 雲端 API（自帶 key） | 約 200（受 API 速率限制） | Core 免費 | [lite/GUIDE.zh-TW.md](lite/GUIDE.zh-TW.md) |
| **Personal** | 8 核 16 執行緒、32 GB RAM、16 GB VRAM（如 RTX 5060 Ti 16GB） | Qwen2.5 3B（Ollama） | 約 64／2,000 | Core 免費 | [personal/GUIDE.zh-TW.md](personal/GUIDE.zh-TW.md) |
| **Workstation** | 16 核 32 執行緒、64–128 GB RAM、32 GB VRAM（如 RTX 5090） | Qwen2.5 7B（vLLM） | 約 200／6,000 | Core 免費；超過 3 人共用用 Team | [workstation/GUIDE.zh-TW.md](workstation/GUIDE.zh-TW.md) |
| **Server** | 32 核 64 執行緒、256 GB RAM、96 GB VRAM（如 RTX PRO 6000），Linux | Qwen2.5 7B（vLLM；可換 Llama 3.1 8B） | 約 150–200／5,000 | Team 或 Enterprise | [server/GUIDE.zh-TW.md](server/GUIDE.zh-TW.md) |
| **Cloud** | 不用買硬體 | 自帶 key，或託管模型 | 超過 5,000 | Cloud | [cloud/GUIDE.zh-TW.md](cloud/GUIDE.zh-TW.md)（**尚未推出**） |

「即時」是有人在等回應的代理（最慢 1% 在 2 秒內回覆），「批次」是背景執行的代理（最慢 1% 在 60 秒內完成）。這些數字是用一張 24 GB 顯示卡跑 Qwen2.5 3B 的實測結果換算的（4,096 個代理同時執行全部成功，超過約 5,500 個開始逾時，見 [docs/stress-test.md](../docs/stress-test.md)）；5090 和 96 GB 卡還沒實測。CPU 不是瓶頸，錢該花在 VRAM。

**怎麼選：** 沒有 GPU 或只是試用選 Lite；一個人、資料不能離開電腦選 Personal；小團隊共用一台選 Workstation；單台要撐上千個代理選 Server。代理數超過一台 Server（約 5,000 個），或 GPU 一天用不到約 8 小時，租雲端比買硬體划算，選 Cloud。

## 收費標準

所有金額為**美金**（新台幣定價尚未決定）。

| 方案 | 價格 | 包含 | 適用產品線 | 狀態 |
|---|---|---|---|---|
| **Core** | **免費**（MIT 授權） | 本 repo 全部功能，包括所有安全功能：權限與範圍、人工審批、污染追蹤、沙箱、雜湊鏈審計日誌、預算 | Lite、Personal、Workstation | 可用 |
| **Team**（自架） | **US$15／人／月**，年繳，最少 5 人；**3 人以下免費** | Core 全部，加上 SSO 與角色、Postgres、多台 worker、審計匯出、機密管理、集中管理的政策與技能 | Workstation（超過 3 人）、Server | 開發中 |
| **Cloud**（託管） | **US$25／人／月**，含每人每月 2,000 次執行（全組共用）；超過的部分每 1,000 次 **US$10** | Team 全部，由我們代管；預設自帶模型 API key（BYOK），我們不轉賣 token | Cloud | 尚未推出 |
| **Enterprise** | 報價，建議 **US$20,000／年起** | Team 或 Cloud，加上客製合約、支援與合規需求 | Server、Cloud | 洽談 |

- **安全功能永遠不收費。** 權限、審批、污染追蹤、沙箱、審計都在免費的 Core 裡，付費方案賣的是多人管理與代管，不是安全。
- **一次執行**＝一個任務從頭到尾跑完。超過 20 次模型呼叫的任務，每多 20 次多算一次；被政策擋下的執行不計費。
- **Team 授權**是離線驗證的簽章授權檔，不回傳任何資料，到期後有 30 天寬限期。Team 模組會以另一個套件發布（FSL 授權，2 年後轉為 Apache/MIT）。
- 硬體不包含在任何方案裡。參考價（新台幣，2026 年 10 月）：Personal 約 4.5–6 萬、Workstation 約 18–25 萬、Server 約 45–55 萬。

## 打包

```bash
python scripts/build-editions.py                  # dist/editions/aios-<產品線>-<版本>.zip，四個本機產品線各一個
python scripts/build-editions.py --only personal  # 只打包一個
```

每個 zip 解開後是 `aios-<產品線>/` 資料夾，裡面有 AIOS 原始碼、`start.py`、這條產品線的 `edition.toml`（模型與並行設定）和 `policy.toml`（代理權限）、`GUIDE.zh-TW.md`（使用說明）、這份總覽，以及 `aios.env.example`。CI 每次都會打包並上傳到 Actions 的 artifacts。

不解壓縮、直接在原始碼目錄裡也能用：`python editions/start.py --edition personal chat`。

## start.py

```bash
python start.py install     # 安裝這條產品線需要的套件（pip）
python start.py check       # 檢查 API key、模型伺服器是否連得上，並執行 aios doctor
python start.py chat        # = aios chat 加上這條產品線的參數與 policy.toml
python start.py daemon      # 定時任務
python start.py swarm --agent worker --tasks tasks.txt --out results.jsonl
python start.py console     # 其他指令（console、jobs、audit、status……）原樣轉給 aios
```

產品線的參數放在前面，你在指令後面加的參數會蓋過它們，例如 `python start.py chat --model qwen2.5:7b`。`aios.env` 裡的 `KEY=value` 會在啟動時載入（環境變數已有的不會被蓋過）。
