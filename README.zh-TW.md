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
- **沙箱程式執行**（`run_python`），用 `--sandbox`（或 `AIOS_SANDBOX`）選擇兩種後端：
  - `process`（Linux／macOS 預設）：獨立行程、CPU／記憶體／檔案大小限制、無網路（Linux 且支援非特權 user namespace）。
  - `docker`：每次呼叫用一個用完即丟的容器，無網路、**不掛載你電腦上的任何東西**、根目錄唯讀、非特權使用者、移除所有 capabilities，並限制記憶體／CPU／行程數。設計支援 Linux 容器，包括 macOS／Windows 的 Docker Desktop；實際限制取決於 daemon 與平台。目前只有 Ubuntu 有 real-Docker CI 證據。映像檔不會自動下載：請先執行一次 `docker pull python:3.12-slim`。在 Windows 上，`auto` 會在 Docker 可用時自動選用；否則 `run_python` 保持停用。
- **MCP 客戶端**：可接 stdio MCP 伺服器；工具以 `<伺服器>__<工具>` 註冊，且除非你明確授權，agent 不會取得。
- **模型供應商**：預設 Anthropic，也可用 `--provider openai --model <名稱>` 連接任何 OpenAI 相容伺服器（Ollama、LM Studio、vLLM、llama.cpp）。openai 供應商只用標準函式庫；只有使用 Anthropic 時才會載入 `anthropic` 套件。
- **預算**：每次執行可限制模型呼叫數（`--max-steps`）、token（`--max-tokens`）與費用（`--max-cost`，需搭配 `--price-in/--price-out`）。達到上限時會在執行更多工具**之前**停止並說明原因，每次執行的用量都會寫進審計日誌。
- **人工審批**：`write_file`、`run_python` 與所有 MCP 工具執行前都會詢問 `[y/N]`。沒有設定審批者時（例如 `demo`、當函式庫使用）這些工具會被拒絕。`--auto-approve` 可關閉詢問，不安全。
- **不可信內容標記＋污染政策**：`read_file`、`list_files`、`run_python` 與所有 MCP 工具的輸出都會用 `<untrusted>` 標籤包起來（內容裡偽造的結束標籤會被拆除），並在系統提示詞要求模型絕不服從其中的指令。只要這類內容還在上下文裡，這次執行就算「被污染」：`--auto-approve` 不再生效、每次高風險呼叫都必須有人在提示前確認（並顯示警告），`remember` 也需要審批，避免注入的文字悄悄污染記憶。
- **能力範圍（scopes）**：除了工具名稱，還能限制工具只能碰哪些工作區路徑（`paths`）、哪些安全路徑在乾淨執行時免詢問（`auto_paths`）、每次執行最多呼叫幾次（`max_calls`）。參數規則（`args`）可用萬用字元樣式與長度上限，允許或拒絕任何參數的值，MCP 工具因此不再是全有或全無（例如只能抓 `https://docs.python.org/*` 的 fetch 工具）；`auto_args` 則像 `auto_paths` 一樣，讓預先核可的值免詢問。可在程式中設定（`Agent(..., scopes=...)`），或用 TOML：`--policy policy.example.toml`（需要 Python 3.11+）。政策檔打錯字會直接報錯，而不是默默放行。
- **記憶來源標記**：每條記憶都記錄是誰寫的（`user` 你親自寫入、`model` 模型保存、`tainted` 在讀過不可信內容的執行中保存）與時間。被污染的記憶會以「未驗證」呈現給模型；你用 `/remember` 親自設定的記憶，模型無法覆蓋；記憶內容裡的換行無法偽造系統提示詞的額外行。舊版 `aios.db` 會自動升級。
- **定時任務**：`aios jobs ...` 與 `aios daemon` 在無人值守下執行任務，有上限且套用同一套政策（見下）。
- **多代理協作**：政策檔裡的每個代理，都能透過名為 `agent__<名稱>` 的工具接手子任務，授權方式和其他工具一樣（`permissions = ["agent__notes"]`）。接手的代理用**自己的**權限、範圍與記憶工作，每個子任務都從空白開始（不保存歷史），並沿用呼叫者的預算。委派無法用來「洗白」提示注入：呼叫者讀過不可信內容時，接手的代理一開始就被視為被污染（沒有自動核准、`auto_paths` 失效），它的回覆也會被標記為不可信。循環委派會被拒絕，深度最多 2 層。`chat` 與 `daemon` 任務都能使用。
- **技能：摘要固定與掃描**：`aios skills scan|trust|list|untrust`、`chat --skill <資料夾>`（見下）。
- **MCP 伺服器模式**：`python aios.py serve` 把受管控的工具提供給任何 MCP 客戶端（見下）。`python aios.py audit` 可在命令列查看審計日誌。
- **本機 Web 控制台**（`python aios.py console`）：在瀏覽器以唯讀方式查看審計日誌（可依代理、工具、狀態篩選）、被拒絕的呼叫、任務與執行紀錄、各代理的記憶與寫入者，以及已信任的技能。它只聽 127.0.0.1、需要用它印出的一次性連結進入、以唯讀模式開啟資料庫，而且頁面完全不執行 JavaScript，所以攻擊者藏在日誌裡的文字無法在你的瀏覽器中作怪。目前還不能在瀏覽器裡審批。
- **記憶檢索**：`search_memory` 工具與 `/search` 可用關鍵字（BM25，純 Python，支援中文）搜尋記憶。加上 `--embed-model nomic-embed-text`（任何 OpenAI 相容的 `/embeddings` 伺服器，例如 Ollama）後，也能依語意比對，兩種排名會合併。記憶超過 30 條時，提示詞只放與這次任務相關的，其餘讓模型用 `search_memory` 去查。
- **提示注入測試集**：`python tests/test_injection.py --report` 會印出各種防護下哪些攻擊能得逞（見下表）。
- **審計日誌**（SQLite，程式介面只能新增）：每次工具呼叫都會記錄代理、工具、參數、結果與狀態（`ok` / `error` / `denied`），用 `/audit` 查看。
- **測試**：`python -m unittest discover -s tests -v`（只用標準函式庫，不需要 API key）；CI 在 Linux 與 Windows 上執行。
- **CLI**：互動式對話，支援 `/facts`、`/search <words>`、`/remember <key> <value>`、`/forget <key>`、`/audit`、`/reset`、`/quit`。

## 快速開始

需要 Python 3.10 以上與 [Anthropic API key](https://platform.claude.com/settings/keys)。

```bash
pip install anthropic            # 選用：pip install mcp
export ANTHROPIC_API_KEY="sk-ant-..."      # PowerShell：$env:ANTHROPIC_API_KEY="sk-ant-..."

python aios.py chat                         # 互動式 agent，名稱為 "main"
python aios.py chat --agent work            # 每個名稱有獨立的歷史與記憶
python aios.py chat --mcp "demo=python demo_mcp_server.py"   # 接上 MCP 伺服器
python aios.py chat --provider openai --model llama3.1   # 用 Ollama 的本機模型
python aios.py chat --max-tokens 20000 --max-steps 6     # 每次執行的硬性上限
python aios.py chat --policy policy.example.toml --agent notes   # 每個代理的權限與路徑範圍
python aios.py chat --policy policy.example.toml --agent lead    # 把寫作交給 'notes' 的代理
python aios.py chat --sandbox docker          # run_python 改在用完即丟的容器裡執行（需先 `docker pull python:3.12-slim`）
python aios.py chat --skill examples/skills/meeting-notes        # 載入你已固定的技能（見「技能」）
python aios.py chat --embed-model nomic-embed-text               # 同時依語意搜尋記憶（需先 `ollama pull nomic-embed-text`）
python aios.py chat --auto-approve          # 乾淨的執行略過 [y/N] 詢問（不安全）
python aios.py demo                         # 腳本化示範（高風險工具會被拒絕）
```

狀態存放在 `./aios.db` 與 `./workspace/`（兩者都已列入 .gitignore）。

## 定時任務

讓任務按排程執行，不需要你在場：

```bash
python aios.py jobs add digest "把 inbox.txt 摘要寫到 reports/digest.txt" --schedule "daily 09:00" --agent reporter
python aios.py jobs list                  # 另有：enable | disable | run <name> | remove <name> | history [name]
python aios.py daemon --policy policy.example.toml --provider openai --model llama3.1   # 保持執行
python aios.py daemon --once ...          # 執行到期的任務就結束：給 cron 或 Windows 工作排程器用
```

排程格式：`every 30m`、`every 2h`、`every 1d`、`daily 09:00`、`weekly mon 09:00`、`once 2026-12-24 18:30`、`on change inbox/*`、`on webhook`（兩者見下；本地時間，最短每分鐘一次）。用 cron 的話：`* * * * * cd ~/aios && python aios.py daemon --once --provider openai --model llama3.1`。

- **沒有人能批准任何事。** 政策流程和 `chat` 相同，但高風險工具一律拒絕，除非政策用 `auto_paths` 預先允許該路徑。沒有政策區段的任務只能唯讀（`now`、`read_file`、`list_files`、`recall`、`list_facts`、`search_memory`）。
- 讀過檔案的任務會被視為「被污染」，連 `auto_paths` 也不再生效，除非範圍設定了 `auto_when_tainted = true`：這代表你明確接受「任務讀到的文字若含注入指令，可以寫入那一個資料夾，且僅限該資料夾」。
- 每個任務是獨立的代理（`job-<name>`：獨立的歷史與記憶），每次執行都從乾淨的歷史開始。執行一律有上限（預設 5 萬 token、8 步、300 秒），每個任務每天最多執行 200 次（`--max-runs-per-day`），所以打錯成 `every 1m` 也不會悄悄燒光預算。
- 守護程式停止期間錯過的任務，恢復後只會執行**一次**，不會每個錯過的時段各補一次。任務一次執行一個。結果用 `aios jobs history` 查看，每次工具呼叫都在 `aios audit` 裡。

**檔案變動觸發。** `--schedule "on change inbox/*"` 會在符合樣式的檔案（相對於工作區，包含子資料夾）被新增、修改或刪除時執行任務：

```bash
python aios.py jobs add mail "把 inbox/ 裡的新檔案摘要寫到 reports/mail.txt" --schedule "on change inbox/*" --agent reporter
```

- 新增任務時已存在的檔案是基準，不算變動。守護程式每次 `--tick`（或每次 `--once`）檢查一次，變動要在下一次檢查時仍保持不變才會執行，避免讀到還在複製中的檔案。
- 檔名**絕不會**放進任務文字（任何能放檔案的人都能挑檔名）；任務只會說明樣式有變動。任務會自己去讀檔案，所以這次執行和其他執行一樣會被視為「被污染」。
- 任務自己寫進監看資料夾的檔案不會再次觸發它；執行**期間**別人做的變動會併入這次執行。最多只計算 5,000 個符合的檔案，每日執行上限照樣適用。

**Webhook 觸發。** `--schedule "on webhook"` 會在本機其他程式（git hook、腳本、智慧家庭橋接器等）送出請求時執行任務：

```bash
python aios.py jobs add triage "把回報的 bug 記到 notes/bugs.md" --schedule "on webhook" --agent reporter   # 只會印出一次任務的密鑰
python aios.py daemon --webhook-port 8766 ...
curl -X POST http://127.0.0.1:8766/hook/triage -H "Authorization: Bearer <密鑰>" --data-binary @event.json
```

- 接收端只聽 127.0.0.1，接受帶有該任務專屬密鑰的 `POST /hook/<任務>`（只儲存雜湊；`aios jobs secret <任務>` 可產生新的密鑰）。密鑰錯誤和任務不存在都回同樣的 401，無法藉此探測任務名稱。內容上限 64 KB，每個任務最多 20 個請求排隊（超過回 429）。
- 接收端本身不執行任何東西：每個請求會在下一次 tick 變成一次執行，套用該任務的政策、上限與每日次數限制。請求內容會**標記為不可信資料**交給模型，這次執行從一開始就是「被污染」的，所以 `auto_paths` 不再生效（除非設定 `auto_when_tainted`）。空白內容只算是通知，不會污染。執行後內容即刪除。
- 若要接收來自網際網路的 webhook，請在前面自行架設 HTTPS 反向代理或通道，並保管好密鑰；aios 本身不處理 TLS。

## 技能（Skills）

技能是一個資料夾，內含 `skill.toml`（名稱、版本）與 `SKILL.md`（給代理的指令）。aios **絕不執行技能裡的程式碼**，只會讀取 `SKILL.md`。因為這些指令會進入系統提示詞，技能本身就是提示注入的管道，所以要先審查並**固定它的摘要（digest）**：

```bash
python aios.py skills scan examples/skills/meeting-notes     # 風險掃描＋摘要
python aios.py skills trust examples/skills/meeting-notes    # 要輸入技能名稱才會確認
python aios.py chat --skill examples/skills/meeting-notes    # 摘要仍相符才會載入
python aios.py skills list                                   # 你信任的技能
python aios.py skills untrust meeting-notes
```

- 摘要涵蓋**資料夾內的每一個檔案**（路徑與內容；換行符號會先統一）。修改、新增、刪除或改名任何檔案，技能就會變成 `changed` 並被拒絕，直到你重新信任。同名但內容不同的資料夾，不會繼承原本的信任。
- 掃描會標出：提示注入用語（中英文）、「不要告訴使用者」、不經詢問就執行、關閉審批／審計／沙盒、傳送金鑰、`curl | sh`、隱藏的 Unicode、金鑰檔案路徑，以及程式檔裡的網路／行程／混淆跡象。有高風險項目時，除非加上 `--force`，否則不能信任。
- 技能無法授予權限或更改審批規則，它只是文字；工具與範圍由你的政策決定。
- **限制**：固定摘要只能證明內容就是*你審查過的那份*，不能證明作者是誰（目前沒有發布者簽章，留待 P3 的註冊表）。掃描乾淨只是警報，不是保證。你信任的技能仍可能誤導模型：請實際讀過它。

## 當作 MCP 伺服器使用

其他工具（Claude Desktop 與任何 MCP 客戶端）可以改用 aios 受管控的工具，而不是不受管控的工具：

```bash
pip install mcp
python aios.py serve --home ~/aios-data --agent gateway --policy policy.example.toml
python aios.py audit --home ~/aios-data      # 查看客戶端做了什麼
python aios.py console --home ~/aios-data    # 或用瀏覽器看：會印出含存取權杖的本機連結
```

MCP 客戶端設定（Claude Desktop 的 `claude_desktop_config.json` 所用的格式，請使用絕對路徑）：

```json
{
  "mcpServers": {
    "aios": {
      "command": "python",
      "args": ["/path/to/aios.py", "serve", "--home", "/path/to/aios-data"]
    }
  }
}
```

- 客戶端被當成模型看待：**不受信任**。權限、範圍、審計日誌與 `<untrusted>` 標記都和 `chat` 相同。
- 沒有政策檔時只提供唯讀工具（`now`、`read_file`、`list_files`、`recall`、`list_facts`）。
- stdio 上沒有人能回答 `[y/N]`，所以高風險工具（`write_file`、`run_python`、被污染後的 `remember`）一律**拒絕**，除非政策用 `auto_paths` 預先允許某些路徑。只要這次連線讀過檔案，連這些也會被拒絕，直到伺服器重啟。
- `max_calls` 以伺服器連線為單位計算。所有資料都放在 `--home` 底下（`aios.db`、`workspace/`）。
- **狀態**：依據 MCP SDK 的低階伺服器介面撰寫，只用替身測過，還沒有用真實客戶端測過。如果客戶端連不上或列不出工具，請開 issue 並附上 stderr 輸出。

## 架構

```
 你 ──> CLI ──> Kernel ──┬─> Claude API（工具呼叫迴圈，逐 agent 權限）
                         ├─> ToolRegistry ──> 內建工具 / MCP 伺服器
                         ├─> Store（SQLite）：歷史、長期事實
                         └─> Sandbox：子行程 + rlimits + 無網路，或 Docker 容器
```

## 內建工具

| 工具 | 功能 |
|---|---|
| `now` | 目前本地日期與時間 |
| `read_file`／`write_file`／`list_files` | 僅限工作區的檔案存取 |
| `remember`／`recall`／`list_facts` | 每個 agent 各自的長期記憶 |
| `run_python` | 沙箱化 Python（`--sandbox process` 或 `docker`） |

## 安全現況

目前**尚未**做到的事，如實列出：

- **提示注入防禦只做到一部分。** 標記只是請模型守規矩；真正的保護是污染政策加範圍限制，它們能阻止「已被劫持的模型」在沒有人的情況下行動。但它**擋不住**不看提示就按 `y` 的人、只是誤導回答內容（沒有用到工具）的文字，或會說謊的惡意工具。我們的測試集模擬的是最壞情況（模型會服從所有注入的指令）；真實模型表現各異，我們還沒有量測。
- 參數規則是針對頂層參數文字的萬用字元比對，不是解析器：`*` 也會比對到 `/`，所以請寫 `https://host/*`，不要寫 `https://host*`（它會比對到 `https://host.evil.com`）。它檢查的是工具「被要求做什麼」，不是實際做了什麼（例如 fetch 仍可能跟隨重新導向）。`allow` 區分大小寫，`deny` 不區分。目前仍沒有網路層級的對外連線控制。
- 定時任務是最危險的執行方式：無人值守，異常行為不會有人當場發現。請盡量讓任務唯讀，把寫入限制在單一輸出資料夾，並偶爾看看 `aios audit`。`auto_when_tainted` 是刻意拿安全換實用。時間為本地時間（遇到夏令時間切換時，每日任務可能偏移一小時一次）。守護程式只是一個行程：它掛掉，任務就會停擺，直到你重新啟動。
- 記憶檢索只涵蓋已儲存的記憶（不含對話歷史）。向量檢索會把每條記憶的文字送到你設定的嵌入伺服器：私人記憶請用本機的。它只用替身伺服器測過，還沒有用真實的 Ollama 或 OpenAI 端點試過；關鍵字檢索不需要任何設定。
- 技能信任的是**你審查過的內容**，不是作者。掃描只抓得到明顯的手法，抓不到細膩的，而你信任的技能仍可能引導模型。把 `--force` 當成「我讀過每一行」。
- `serve` 沒有互動式審批（stdio 是協議通道）。請用 `auto_paths` 預先允許路徑（或用 `auto_args` 預先允許參數值），或不開放高風險工具。連線的污染狀態會持續到伺服器重啟。
- 審計日誌只負責記錄，並非防竄改：能寫入 `aios.db` 的人仍可修改它。
- 控制台連結在控制台執行期間就等於密碼：拿到它（或它設定的瀏覽器 cookie）的人都能讀你的日誌與記憶。每次啟動都會產生新的連結。它防不了以你同一個使用者身分執行的其他軟體，那些軟體本來就能直接讀 `aios.db`。
- **`process` 沙箱不是完整的安全邊界**：會要求資源限制，並在可用時使用 Linux 網路 namespace，但程式碼仍可讀取該使用者讀得到的檔案，行程數量也沒有上限。要執行不可信程式碼，請用 `--sandbox docker`。容器與 Docker 主機共用核心，核心或 Docker 逃逸漏洞可能突破隔離。Docker Desktop 有自己的 VM，但此後端沒有實作 microVM 隔離。每次呼叫先建立容器，再附加執行；AIOS 不安裝額外 Python 套件。映像檔、Docker CLI、daemon 與選定的 Docker context 必須可信。
- Docker 輸出以固定大小串流讀取，預設最多保留 10,000 個 UTF-8 bytes。逾時或取消會觸發有期限的容器 kill、強制移除與不存在確認；清理失敗會明確報錯。建立容器逾時的結果仍未知，daemon 延遲作業可能稍後才建立容器，必須在該 daemon 檢查指定名稱。參見[驗證範圍](docs/docker-sandbox-validation.md)。
- **Windows**：沒有 Docker 時 `run_python` 停用（只有逾時的隔離並不安全）。請用 `--sandbox docker`，或設定 `AIOS_ALLOW_WEAK_SANDBOX=1` 自行承擔風險啟用較弱的沙箱。
- 請把 MCP 伺服器同時當作不可信的程式碼與不可信的資料來源，並逐 agent 明確授權。
- 預算在每次模型呼叫之後才檢查，所以單次呼叫可能略微超出上限。費用上限需要你自己提供單價（價格會變，因此不內建預設值）。預算是每次執行的上限，不是每日上限。
- 本機模型在工具使用與抵禦提示詞注入上，都比前沿模型弱很多，請保持人工審批開啟。

### 注入測試結果

最壞情況的模型，開啟 `--auto-approve`。`X` = 攻擊得逞。

| 攻擊 | 無防護 | 只有污染政策 | 只有範圍限制 | 兩者並用 |
|---|:-:|:-:|:-:|:-:|
| 任意寫檔 | X | | | |
| 寫入 `config/` | X | | | |
| 寫入 `notes/`（範圍內） | X | | X | |
| 路徑穿越 | | | | |
| 記憶投毒 | X | | X | |
| 覆蓋你設定的記憶 | | | | |
| 執行程式碼 | X | | X | |
| 呼叫外部工具 | X | | X | |

解讀：只有範圍限制時，停留在允許路徑內的惡意呼叫仍會得逞，要靠污染政策才擋得住。路徑穿越在每一欄都被工作區圍欄擋下。

## Roadmap

月份為相對估計值（第 1 個月＝專案起點），之後會調整。

| 階段 | 時間 | 目標 | 完成標準 |
|---|---|---|---|
| **P0 打底** | 第 1 月 | 自動化測試 · 審計日誌 · 高風險工具人工批准 · 成本／步數預算 · 模型供應商抽象層（含本機模型） | 每個動作可追溯，超出預算會中止 |
| **P1 安全核心** | 第 1–2 月 | ✅ 能力範圍（路徑、呼叫上限、參數規則）· ✅ 不可信內容標記＋污染政策 · ✅ 提示注入測試集 · ✅ 記憶來源與時間標記 · ✅ 技能固定與掃描（發布者簽章留待 P3 的註冊表）· ✅ 記憶檢索（BM25 關鍵字，向量為選用）· ✅ 作為 MCP 伺服器運行（`aios serve`；尚未用真實客戶端試過） | 通過自建的提示注入測試集（最壞情況模型已達成）；P1 功能已齊全，真實環境測試仍待進行 |
| **P2 擴展** | 第 3–4 月 | ✅ 多代理委派（`agent__<名稱>`；A2A 網路協定尚未）· ✅ 排程（`aios jobs`／`daemon`）· ✅ 檔案變動與 webhook 觸發· ✅ 本機 Web 控制台（唯讀；瀏覽器審批尚未）· 可插拔沙箱後端（✅ Docker；microVM 尚未） | 一行指令跑多 agent；Windows 也有真隔離 |
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
