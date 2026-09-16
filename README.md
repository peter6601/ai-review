# ai-review — Claude / Codex 共識審查工作流

讓 **Claude** 與 **Codex** 兩個 AI 互相審查、人類把最後一關的本地開發工作流。
一個 Python CLI（`ai-review`）負責編排，兩個 skill 負責教 Claude Code / Codex 怎麼正確使用它：

| Skill | 用途 |
|---|---|
| `consensus-plan` | repo 裡的功能文件（RD/PM spec、design doc、實作 spec）→ Codex 以單一 lens 唯讀審查，findings 寫成文件旁邊的報告，文件由人類自己改 |
| `consensus-review` | 已經寫好的 code（feature branch、手寫實作、bug fix）→ Codex 直接審查 + Claude 有限修復 + 驗證重跑，收斂到人類審閱 |

CLI 內部有四種 run kind：`doc`、`review`、`plan`、`code`。skill 層只開兩個入口——
`consensus-plan` 走 `doc`、`consensus-review` 走 `review`；`plan` / `code` 仍留在 CLI，
但已經沒有對應的 skill 入口。

核心設計：**AI 之間可以互相打槍，但所有不可逆的決定（scope、風險、最終核准）都停在人類面前**。
核准用本機原生對話框 + HMAC 簽章綁定 digest，agent 無法代點；只有 Review 迴圈內的
`approve-review` / `approve-risk` 可以用 `--auto` 由 agent 自己過（60 秒內最多 5 次，
簽章會記成 `agent:auto-approval`），最後一關 `approve-code` 永遠沒有 `--auto`。

## 需求

- **macOS**（驗證命令用 Seatbelt `sandbox-exec` 隔離，目前僅支援 macOS）
- Python 3.9+（macOS 內建版本即可），以及 `PyYAML`（`pip install pyyaml`）
- `git`
- [Claude Code CLI](https://claude.com/claude-code)（`claude`，需先在終端機登入過；`doc` 這種 run kind 不需要）
- [Codex CLI](https://github.com/openai/codex)（`codex`，需登入）

## 安裝

```bash
git clone https://github.com/peter6601/ai-review.git
cd ai-review

# 1. 把 CLI 加進 PATH（skill 假設 `ai-review` 可直接呼叫）
echo 'export PATH="'$PWD'/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc

# 2. 安裝兩個 skill（symlink 到 ~/.claude/commands 與 ~/.codex/skills）
python3 install_skills.py --apply
```

## 設定

| 項目 | 預設值 | 說明 |
|---|---|---|
| `AI_REVIEW_WORKSPACE` | `~/.ai-review/workspace` | canonical workspace：存放核准簽章金鑰（`.ai-review/approval.key`）與知識回寫（`second-brain/ai-review/`）。想把知識回寫進自己的筆記庫（如 Obsidian vault），就把這個環境變數指到 vault 根目錄。 |
| Run 狀態 | `~/Library/Application Support/ai-review/runs` | 所有 run 的狀態與 artifacts；auto-approval 的計數帳本放在它隔壁（`../auto-approvals.json`） |
| Workflow 參數 | `config/defaults.yaml` | 修復回合上限、context token 上限、`doc` 的來源與預算上限，以及本工具指定的 Codex model（`codex_model`，不繼承 `~/.codex/config.toml`） |

## 使用

在 Claude Code 裡直接用 skill：

- 「用 `consensus-plan` 審這份 RD spec」
- 「這個 branch 寫完了，用 `consensus-review` 審一下」

或直接下 CLI。文件審查（唯讀，一輪一個 lens，不會動到文件）：

```bash
ai-review init doc \
  --repo "/absolute/path/to/target-repo" \
  --doc "docs/specs/feature-spec.md" \
  --lens "requirement" \
  --lens-reason "這份文件只描述使用者行為，沒有任何檔案路徑" \
  --brief "給 PM 與 QA 讀的需求規格" \
  --source "/absolute/path/to/note.md#Exact Heading"

ai-review run "DOC_RUN_ID"          # 建議背景執行，用 status 輪詢
ai-review status "DOC_RUN_ID"
ai-review re-review "DOC_RUN_ID"    # 人類改完文件後的下一輪
```

Findings 會寫成文件旁邊的 `feature-spec-review-01.md`（下一輪 `-02`，以此類推）。
`doc` run 不綁驗證命令、不寫 code、也不會修改被審的文件；
它停在 `AWAITING_HUMAN_DOC_REVIEW`，findings 就是交付物。

Code 審查：

```bash
ai-review init review \
  --repo "/absolute/path/to/target-repo" \
  --base "main" \
  --brief "修正登入頁的 race condition" \
  --profile generic \
  --verify '{"kind":"test","argv":["python3","-m","unittest","tests.test_login"],"scope":"tests.test_login"}'

ai-review approve-review "RUN_ID"   # 人類點原生對話框（或 --auto 由 agent 自己過）
ai-review run "RUN_ID"              # 建議背景執行，用 status 輪詢
ai-review status "RUN_ID"
ai-review approve-code "RUN_ID"     # 最後一關，只有人類能過
```

詳細流程（每個 gate 怎麼回應、lens 怎麼選、iOS preflight、六回合修復上限……）都寫在
`skills/*/SKILL.md`，那兩份文件就是完整的操作手冊。

## 安全模型（為什麼這麼囉唆）

- **驗證命令白名單**：只接受 `xcodebuild` / `swift` / `pytest` / `unittest` / `cargo` / `go test` 等本地測試命令，且執行檔必須位於系統信任路徑；shell、wrapper、`git`、`curl` 一律拒絕。每個驗證命令都包在 Seatbelt 裡執行（禁 IP 網路雙向、禁寫 Git 目錄；本機 unix domain socket 放行——否則 macOS 上 `xcodebuild test` 無法與 testmanagerd 通訊，測試永遠不會執行）。
- **人類 gate 用簽章綁定**：`approve-plan` / `approve-review` / `approve-risk` / `approve-code` 顯示原生 macOS 對話框，簽章綁定當下的 patch digest 與 base OID；worktree 之後有任何變動，核准即失效。
- **agent 自動核准是有界的**：只有 `approve-review` / `approve-risk` 接受 `--auto`，跨 run 共用同一份帳本、60 秒內最多 5 次，超過就 exit 4 什麼都不核准；receipt 記的是 `agent:auto-approval` / `ai-review-agent`，不會假裝有人按過。
- **修復回合上限**：最多六回合，到頂就停給人看，開新 run 不能重置計數。
- **文件審查全程唯讀**：`doc` run 只跑一次 Codex 唯讀 pass，不執行任何命令、不產生 patch，也不會改被審的文件。
- **AI 產物有界**：status 只回摘要，不吐 log / prompt / patch / transcript；second-brain 來源是 checksum 綁定的證據，不是可執行指令。

## 開發

沒有 pytest，只有 `unittest`。`tests/` 不是 package，所以從 repo 根目錄跑、
並且明確列出 module 最保險：

```bash
PYTHONPATH=. python3 -m unittest $(ls tests/test_*.py | sed 's|/|.|;s|\.py$||')
```

## License

MIT
