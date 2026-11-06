# AIOS 與傳統作業系統機制對照

自我檢核，2026-11-06（AIOS 0.2.0）。近期研究認為，已上線的 LLM 代理本身*就是*一個作業系統，而且預設擁有 ring-0 權限：它用使用者的全部權限行事，而寫在提示詞裡的規則無法當作參照監視器（〈Ring-0 by Default: Agents as Operating Systems〉，arXiv 2610.33753，檢查了 116 個教科書機制，發現現今的程式碼代理缺了 65 個）。本頁依同樣的傳統子系統逐項說明 AIOS **用程式碼強制**了什麼、只做到一部分的是什麼、還缺什麼。分組方式是我們自己的，不是論文的。英文版見 [os-mechanisms.md](os-mechanisms.md)。

✅ 程式碼強制 · ⚠️ 部分 · ❌ 缺少

| 子系統 | 作業系統機制 | AIOS 的做法 | |
|---|---|---|---|
| 行程 | 行程抽象、各自的位址空間 | 每個代理有自己的權限、歷史、記憶與預算；幫手每次從空白開始 | ✅ |
| | 以較低權限 fork/exec | 委派（`agent__<名稱>`）；`narrow`／`delegate` 上限只會沿委派鏈縮小 | ✅ |
| 排程 | 執行佇列、worker 池 | 內核佇列＋worker；代理群有背壓 | ✅ |
| | 記憶體壓力下的准入控制 | Docker／microVM 沙箱只有在上限放得進可用記憶體時才啟動（`aios_admit`） | ✅ |
| | 優先順序、搶占 | 沒有：模型呼叫依先來後到 | ❌ |
| 記憶體（上下文） | 有上限的工作集 | 歷史上限、壓縮成摘要、舊圖片縮減 | ✅ |
| | 需要時再載入 | `search_memory`；`recall_on` 在指定工具呼叫後附上相關記憶 | ⚠️（關鍵字比對，沒有學習式預取） |
| | 載入內容的完整性 | 每條記憶都有來源（`user`／`model`／`tainted`）；未驗證的記憶會污染執行 | ✅ |
| 保護 | 不受信程式之外的參照監視器 | 權限、範圍、參數規則與審批都由內核檢查，從不交給模型 | ✅ |
| | 預設最小權限 | 未授權的工具看不到；MCP 工具需要審批或範圍 | ✅ |
| | 保護使用者的其他檔案 | 檔案工具只限工作區；`run_python` 在沙箱裡 | ✅ |
| | | MCP 伺服器以你的權限執行，除非加上 `--confine-mcp`／`--offline-mcp`／`--mcp-egress` | ⚠️ |
| 資訊流 | 污染追蹤 | 讀過不可信內容的執行失去所有自動核准 | ✅ |
| | 逐值標籤 | 參數來源追蹤：參數重複了不可信文字的高風險呼叫必須由人核准 | ⚠️（只抓逐字複製；改寫過的值抓不到） |
| | 解除分級 | 由人在審批提示時決定 | ✅ |
| 身分 | 使用者與驗證 | A2A bearer token、控制台只綁 localhost、Telegram 允許名單 | ⚠️ |
| | 多使用者帳號、RBAC | 單一使用者 | ❌（規劃在 Team／Cloud） |
| 行程間通訊 | 行程之間的訊息 | 委派、代理群、A2A（1.0 與 0.3）、MCP 客戶端與伺服器（2026-07-28 與舊版）、webhook | ✅ |
| 裝置／驅動程式 | 與內核隔離的驅動程式 | MCP 伺服器是 stdio 子行程，可限制檔案、斷網或限制對外連線 | ✅ |
| 網路 | 防火牆、對外過濾 | `fetch_url` 擋私有位址、不跟隨重新導向；`--mcp-egress` 允許名單代理並寫入審計 | ✅ |
| 隔離 | 沙箱 | process（rlimit、Landlock、seccomp、netns）、macOS sandbox-exec、Windows AppContainer、Docker、Firecracker microVM | ✅ |
| 資源 | 配額與計量 | 每次與每日的 token／成本預算、滑動視窗速率限制、呼叫上限、rlimit | ✅ |
| | 失控偵測 | 迴圈保護（`--max-repeats`） | ✅ |
| 審計 | 防竄改日誌 | 雜湊鏈審計、外部錨點、轉送、看門狗警示 | ✅ |
| | 匯出給外部審查 | `aios export`（OpenAI 格式聊天 JSON，來自歷史或審計日誌） | ✅ |
| 服務 | init、監督、健康檢查 | daemon、安裝成系統服務、心跳、`aios doctor`／`status` | ✅ |
| 軟體安裝 | 簽章套件、固定版本 | 技能以摘要固定、掃描、OpenSSH 發布者簽章、靜態註冊中心（可選簽章） | ✅ |
| 復原 | 交易、還原 | 每次 `write_file` 都有檢查點，`aios undo` | ⚠️（MCP 的副作用無法還原） |
| 時間 | 計時器、cron | 排程、檔案變動與 webhook 觸發 | ✅ |

## 缺口代表什麼

- **沒有優先順序或搶占。** 長時間的幫手無法為了緊急請求暫停；模型呼叫依到達順序等候。個人電腦上可以接受，共用伺服器上就有影響。
- **看來源，不看語意。** 〈When the Agent Becomes the Kernel〉（arXiv 2609.23700）說明了為什麼「資料從哪裡來」的檢查可以是確定的，而「這段文字是什麼意思」的檢查永遠會有漏網之魚。AIOS 依此分工：污染與參數來源是確定性檢查，凡是需要判斷語意的（這真的是使用者要的嗎？）都交給人。模型改寫注入的文字就能繞過逐字比對，所以被污染的執行預設仍要人核准每個高風險呼叫。
- **MCP 伺服器除非受限，否則是被信任的程式。** MCP 2026-07-28 的安全最佳實務只*建議*把啟動的伺服器放進沙箱；AIOS 可以逐一設定，但不強制。
- **單一使用者。** 身分是每個呼叫者一個 token，不是有角色的帳號。

## 參考資料

- Ring-0 by Default: Agents as Operating Systems (AaOS)，arXiv 2610.33753，2026-10-24。
- When the Agent Becomes the Kernel: A Systematization of Security on the Path to AI-Native Operating Systems，arXiv 2609.23700，2026-09-20。
- Rethinking Access Control for AI Agents: Recent Advances and Open Challenges，arXiv 2611.06764，2026-11-04。
- TenonAgent，arXiv 2611.00051；ToolSched，arXiv 2611.05269；MemSidecar，arXiv 2611.00779。
- Model Context Protocol 2026-07-28 規格與其安全最佳實務。
