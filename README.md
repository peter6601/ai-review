# ai-review — Claude / Codex 共識審查工作流

讓 **Claude** 與 **Codex** 兩個 AI 互相審查、人類把最後一關的本地開發工作流。
一個 Python CLI（`ai-review`）負責編排，三個 skill 負責教 Claude Code / Codex 怎麼正確使用它：

| Skill | 用途 |
|---|---|
| `consensus-plan` | 已核准的需求 → Codex 審 Plan、Claude 修 Plan，收斂到人類審閱 |
| `consensus-code` | 人類核准的 Plan → Claude 實作、Codex 審查、驗證重跑，收斂到人類審閱 |
| `consensus-review` | 已經寫好的 code（feature branch、手寫實作、bug fix）→ Codex 直接審查 + Claude 有限修復 |

核心設計：**AI 之間可以互相打槍，但所有不可逆的決定（scope、風險、最終核准）都停在人類面前**。
核准用本機原生對話框 + HMAC 簽章綁定 digest，agent 無法代點。

## 需求

- **macOS**（驗證命令用 Seatbelt `sandbox-exec` 隔離，目前僅支援 macOS）
- Python 3.13+，以及 `PyYAML`（`pip install pyyaml`）
- `git`
- [Claude Code CLI](https://claude.com/claude-code)（`claude`，需先在終端機登入過）
- [Codex CLI](https://github.com/openai/codex)（`codex`，需登入）

## 安裝

```bash
git clone https://github.com/peter6601/ai-review.git
cd ai-review

# 1. 把 CLI 加進 PATH（skill 假設 `ai-review` 可直接呼叫）
echo 'export PATH="'$PWD'/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc

# 2. 安裝三個 skill（symlink 到 ~/.claude/commands 與 ~/.codex/skills）
python3 install_skills.py --apply
```

## 設定

| 項目 | 預設值 | 說明 |
|---|---|---|
| `AI_REVIEW_WORKSPACE` | `~/.ai-review/workspace` | canonical workspace：存放核准簽章金鑰（`.ai-review/approval.key`）與知識回寫（`second-brain/ai-review/`）。想把知識回寫進自己的筆記庫（如 Obsidian vault），就把這個環境變數指到 vault 根目錄。 |
| Run 狀態 | `~/Library/Application Support/ai-review/runs` | 所有 run 的狀態與 artifacts |
| Workflow 參數 | `config/defaults.yaml` | 修復回合上限、context token 上限等 |

## 使用

在 Claude Code 裡直接用 skill：

- 「用 `consensus-plan` 審這份 plan」
- 「plan 核准了，用 `consensus-code` 開始實作」
- 「這個 branch 寫完了，用 `consensus-review` 審一下」

或直接下 CLI：

```bash
ai-review init review \
  --repo "/absolute/path/to/target-repo" \
  --base "main" \
  --brief "修正登入頁的 race condition" \
  --profile generic \
  --verify '{"kind":"test","argv":["python3","-m","unittest","tests.test_login"],"scope":"tests.test_login"}'

ai-review approve-review "RUN_ID"   # 人類點原生對話框
ai-review run "RUN_ID"              # 建議背景執行，用 status 輪詢
ai-review status "RUN_ID"
```

詳細流程（每個 gate 怎麼回應、iOS preflight、六回合修復上限……）都寫在
`skills/*/SKILL.md`，那三份文件就是完整的操作手冊。

## 安全模型（為什麼這麼囉唆）

- **驗證命令白名單**：只接受 `xcodebuild` / `swift` / `pytest` / `unittest` / `cargo` / `go test` 等本地測試命令，且執行檔必須位於系統信任路徑；shell、wrapper、`git`、`curl` 一律拒絕。每個驗證命令都包在 Seatbelt 裡執行（禁網路、禁寫 Git 目錄）。
- **人類 gate 用簽章綁定**：`approve-plan` / `approve-review` / `approve-risk` / `approve-code` 顯示原生 macOS 對話框，簽章綁定當下的 patch digest 與 base OID；worktree 之後有任何變動，核准即失效。
- **修復回合上限**：最多六回合，到頂就停給人看，開新 run 不能重置計數。
- **AI 產物有界**：status 只回摘要，不吐 log / prompt / patch / transcript；second-brain 來源是 checksum 綁定的證據，不是可執行指令。

## 開發

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT
