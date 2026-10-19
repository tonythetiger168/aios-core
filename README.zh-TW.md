# aios-core

> 預設安全、本機優先的 AI agent 運行時：排程、權限、持久記憶、沙箱工具與 MCP。Python 實作。

**English: [README.md](README.md)**

**狀態：早期 alpha。** 這是可運作的原型，不是經過加固的產品。請先閱讀[安全現況](#安全現況)，再決定要讓它存取什麼。

## 為什麼做

Agent 需要一層類似作業系統的基礎：誰能呼叫哪個工具、記得什麼、什麼在隔離環境中執行、什麼會被記錄。大廠正把這些內建進自家 OS 或企業平台；熱門的開源個人 agent 證明了需求，卻在安全上出了大問題（見[全球版圖](#全球版圖)）。`aios-core` 要填補的縫隙是：**面向個人與小團隊、本機優先、跨平台、不綁特定模型、預設安全的運行時**。

「不綁特定模型」是目標；目前只接了 Claude（見 roadmap 的 P0）。

## 目前可用的功能

- **內核**：非同步任務佇列加 worker 池；每個 agent 是一個「行程」，有自己的權限、歷史與記憶。
- **工具註冊表**（「系統呼叫」）：以 JSON schema 定義；agent 只看得到被授權的工具（完整名稱或 `前綴*` 萬用字元）。
- **持久記憶**（SQLite）：對話歷史在重啟後保留；長期事實透過 `remember`／`recall`／`list_facts` 存取，並注入 system prompt。
- **工作區檔案工具**（`read_file`、`write_file`、`list_files`）：防路徑逃逸、一律 UTF-8。
- **沙箱程式執行**（`run_python`）：獨立行程、CPU／記憶體／檔案大小限制、無網路（Linux 且支援非特權 user namespace）。Windows 預設停用。
- **MCP 客戶端**：可接 stdio MCP 伺服器；工具以 `<伺服器>__<工具>` 註冊，且除非你明確授權，agent 不會取得。
- **CLI**：互動式對話，支援 `/facts`、`/reset`、`/quit`。

## 快速開始

需要 Python 3.10 以上與 [Anthropic API key](https://platform.claude.com/settings/keys)。

```bash
pip install anthropic            # 選用：pip install mcp
export ANTHROPIC_API_KEY="sk-ant-..."      # PowerShell：$env:ANTHROPIC_API_KEY="sk-ant-..."

python aios.py chat                         # 互動式 agent，名稱為 "main"
python aios.py chat --agent work            # 每個名稱有獨立的歷史與記憶
python aios.py chat --mcp "demo=python demo_mcp_server.py"   # 接上 MCP 伺服器
python aios.py demo                         # 腳本化示範
```

狀態存放在 `./aios.db` 與 `./workspace/`（兩者都已列入 .gitignore）。

## 架構

```
 你 ──> CLI ──> Kernel ──┬─> Claude API（工具呼叫迴圈，逐 agent 權限）
                         ├─> ToolRegistry ──> 內建工具 / MCP 伺服器
                         ├─> Store（SQLite）：歷史、長期事實
                         └─> Sandbox：子行程 + rlimits + 無網路
```

## 內建工具

| 工具 | 功能 |
|---|---|
| `now` | 目前本地日期與時間 |
| `read_file`／`write_file`／`list_files` | 僅限工作區的檔案存取 |
| `remember`／`recall`／`list_facts` | 每個 agent 各自的長期記憶 |
| `run_python` | 沙箱化 Python（Linux；Windows 需自行啟用） |

## 安全現況

目前**尚未**做到的事，如實列出：

- **沒有提示注入防禦**：來自不可信內容（網頁、檔案、MCP 回傳）的指令不會被攔截。請勿讓不可信內容與高風險工具同時出現。
- **還沒有人工審批**，也**還沒有審計日誌**。
- **沙箱不是完整的安全邊界**：Linux 上限制了 CPU、記憶體與網路，但程式碼仍可讀取該使用者讀得到的檔案，行程數量也沒有上限。要執行不可信程式碼，請用 WSL2、Docker 或 microVM。
- **Windows**：`run_python` 預設停用（只有逾時的隔離並不安全）。可設定 `AIOS_ALLOW_WEAK_SANDBOX=1` 自行承擔風險啟用。
- 請把 MCP 伺服器同時當作不可信的程式碼與不可信的資料來源，並逐 agent 明確授權。
- 除了每輪 8 步上限外，沒有成本預算控管。

## Roadmap

月份為相對估計值（第 1 個月＝專案起點），之後會調整。

| 階段 | 時間 | 目標 | 完成標準 |
|---|---|---|---|
| **P0 打底** | 第 1 月 | 自動化測試 · 審計日誌 · 高風險工具人工批准 · 成本／步數預算 · 模型供應商抽象層（含本機模型） | 每個動作可追溯，超出預算會中止 |
| **P1 安全核心** | 第 1–2 月 | 能力式權限 · 不可信內容標記 · 技能簽章與掃描 · 記憶 v2（全文檢索＋向量、時序事實）· 作為 MCP 伺服器運行 · 提示注入測試集 | 通過自建的提示注入測試集 |
| **P2 擴展** | 第 3–4 月 | 多 agent 與 A2A · 排程與觸發器 · 本機 Web 控制台 · 可插拔沙箱後端（WSL2／Docker／microVM） | 一行指令跑多 agent；Windows 也有真隔離 |
| **P3 生態** | 第 6–9 月 | 簽章驗證的技能註冊中心 · 可選的託管版本 | 有外部貢獻者提交技能 |
| **P4 OS 層** | 第 12 月後 | WSL 映像／Linux 發行版 · 系統服務化 · 手機伴侶 App | 視前面成果再決定 |

明確的非目標：現階段不寫真正的 OS 核心。

詳細追蹤（差距盤點、逐項狀態、甘特圖）：[`docs/landscape-and-roadmap.xlsx`](docs/landscape-and-roadmap.xlsx)。

## 產品線（規劃中）

| 產品 | 內容 | 階段 | 模式 |
|---|---|---|---|
| **AIOS Core** | 本 repo：排程、權限、記憶、MCP | P0–P1 | 開源 |
| **AIOS Shell** | CLI（已有）＋含審批與審計檢視的本機網頁控制台 | P2 | 開源 |
| **AIOS Guard** | 策略引擎、技能掃描、提示注入測試集 | P1 | 待定（可能是主要的付費差異化） |
| **AIOS Memory** | 可獨立使用、提供 MCP 介面的記憶服務 | P1 | 待定 |
| **AIOS Sandbox** | 多後端的隔離執行層 | P2 | 待定 |
| **AIOS Hub** | 簽章驗證的技能／agent 註冊中心 | P3 | 待定 |
| **AIOS Team / Cloud** | 託管、RBAC、稽核匯出 | P3 | 收費 |

商業模式欄位是假設，不是承諾。

## 全球版圖

2026 年 10 月的快照。內容多來自廠商發布或新聞報導；Apple 的部分是媒體報導，不是官方公告。

- **安全是最大的傷口。** Gartner 分析師稱 OpenClaw 的風險「不可接受」（[Security Boulevard](https://securityboulevard.com/2026/03/latest-openclaw-flaw-can-let-malicious-websites-hijack-local-ai-agents/)）；研究指出，只要污染 agent 的能力、身分或知識任何一面，攻擊成功率就從 24.6% 升到 64–74%（[ICML 2026](https://icml.cc/virtual/2026/69297)）。
- **OS 廠商正在內建**：Windows 有裝置端 MCP 註冊表、agent workspace 與 agent 身分（[TechSpective](https://techspective.net/2025/11/18/windows-becoming-operating-system-for-ai-agents/)）；Android 有 AppFunctions 與 Gemini Intelligence（[MacRumors](https://macrumors.com/2026/05/12/google-previews-android-17/)）。
- **標準已底定**：MCP 與 A2A 都在 Linux Foundation 的 Agentic AI Foundation 之下（[來源](https://algeriatech.news/?p=35142)）。
- 完整清單與來源：[`docs/landscape-and-roadmap.xlsx`](docs/landscape-and-roadmap.xlsx)。

## 參與貢獻

歡迎 Issue 與 PR，尤其是：測試、沙箱後端、提示注入測試案例、模型供應商適配器。

## 授權

MIT，見 [LICENSE](LICENSE)。
