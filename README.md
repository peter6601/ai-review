# AI Review

讓 **Codex 負責審查、Claude 負責修正、人類負責最後決定** 的本機工作流。

它提供兩個主要入口：

| Skill | 適用情境 | 結果 |
|---|---|---|
| `consensus-plan` | 需求、設計或實作文件已寫好 | Codex 唯讀審查；人類依 findings 修改文件 |
| `consensus-review` | 功能、分支或 bug fix 已完成 | Codex 審查、Claude 有限修復，最後交給人類核准 |

> AI 可以互相檢查，但不會替人類通過最終審查。

## 運作方式

### 文件審查

```text
文件 → Codex 唯讀審查 → Findings → 人類修改 → 可再次審查
```

審查過程不會修改原文件，也不會執行測試或寫入程式碼。

### 程式碼審查

```text
目前變更 → 執行測試 → Codex 審查 → Claude 修復 → 重新測試與審查 → 人類核准
```

自動修復最多六輪；遇到高風險變更或無法確認的狀況會暫停，等待人類處理。

## 系統需求

- macOS
- Python 3.9+ 與 `PyYAML`
- Git
- 已登入的 [Codex CLI](https://github.com/openai/codex)
- 已登入的 [Claude Code CLI](https://claude.com/claude-code)（只有程式碼修復需要）

## 安裝

```bash
git clone https://github.com/peter6601/ai-review.git
cd ai-review

python3 -m pip install pyyaml

# 讓 ai-review 指令可直接使用
echo 'export PATH="'"$PWD"'/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc

# 安裝 consensus-plan 與 consensus-review
python3 install_skills.py --apply
```

`PATH` 與 skill 連結都指向這個 clone；安裝後請不要直接移動資料夾。

安裝程式會將 skills 連結至：

- `~/.claude/commands/`
- `~/.codex/skills/`

若目的地已有同名內容，會先備份到 repository 內的 `.skill-backups/`。

## 快速使用

建議直接在 Claude Code 或 Codex 中描述需求：

```text
使用 consensus-plan 審查 docs/spec.md
```

```text
使用 consensus-review 審查目前分支，base 是 main，驗證指令是……
```

Skill 會負責收集必要資訊、建立 run，並在需要人類判斷時停下來。

### CLI 流程

如果要直接操作 CLI，可先用 `ai-review --help` 查看參數。`init` 會輸出 JSON，後續指令使用其中的 `run_id`。

```text
文件：init doc → run → status → 人類修改文件 → re-review
程式碼：init review → approve-review → run → status → 人類 approve-code
```

`approve-review` 核准目前的 patch、base 與驗證範圍；`approve-code` 則是不可自動化的最終核准。`run` 可能執行較久，建議放到背景並以 `status` 查看進度。

## 常見狀態

| 狀態 | 下一步 |
|---|---|
| `AWAITING_USER_INPUT` | 準備 answers JSON，執行 `ai-review answer RUN_ID --answers FILE` |
| `AWAITING_HUMAN_DOC_REVIEW` | 閱讀 findings，並由人類修改文件 |
| `AWAITING_HUMAN_CODE_REVIEW` | 人類閱讀 diff 後執行 `approve-code` |
| `INTERRUPTED` | 排除外部問題後執行 `ai-review resume RUN_ID` |
| `PAUSED` | 依 `status` 顯示的原因處理；不要直接重開 run |

完整的 lens 選擇、iOS preflight、context 擴充與各種暫停處理方式，請參考：

- [`skills/consensus-plan/SKILL.md`](skills/consensus-plan/SKILL.md)
- [`skills/consensus-review/SKILL.md`](skills/consensus-review/SKILL.md)

## 安全原則

- Codex 以唯讀模式審查。
- Claude 的修復次數有上限，每次修復後都必須重新測試與審查。
- 驗證指令不經 shell 執行，並限制為可信任的本機工具。
- 核准會綁定當下的 base 與 patch；程式碼改變後，舊核准會失效。
- `approve-review` 與 `approve-risk` 可選擇使用 `--auto`；最終的 `approve-code` 永遠只能由人類執行。

## 設定與資料位置

- 工作流參數：`config/defaults.yaml`
- Run 記錄：`~/Library/Application Support/ai-review/runs/`
- Canonical workspace：預設為 `~/.ai-review/workspace/`，可用 `AI_REVIEW_WORKSPACE` 指定

## 開發

從 repository 根目錄執行測試：

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_*.py'
```

## License

[MIT](LICENSE)
