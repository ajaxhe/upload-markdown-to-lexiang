# upload-markdown-to-lexiang

> 将 **Markdown 图文文档（含本地图片与公式）可靠上传为腾讯乐享在线文档** 的唯一公共上传层。
> 供 `fetch-archive-to-lexiang`、`trans-doc-to-md`、`sync-obsidian-to-lexiang` 等 Skill 复用。
> 抓取、翻译、目录策略、增量同步等业务逻辑应留在上层 Skill；目标专有标注解析与乐享块渲染属于本 Skill。

| 项 | 值 |
|---|---|
| **Skill 名** | `upload-markdown-to-lexiang` |
| **版本** | `1.3.1` |
| **cli_api** | `1` |
| **作者** | ajaxhe |
| **许可证** | MIT |
| **分类** | productivity |

---

## ✨ 特性

- 标准 Markdown + 相对本地图片上传（路径支持 `%20`、未转义空格及 `<images/file name.png>`）。
- `> [!stat]` / `> [!definition]` / `> [!note]` 可移植标注 → 乐享原生 callout（📊 / 📖 / 💡）。
- 创建**或**覆盖 page。
- 公式模式：`unicode`（默认）/ `image` / `native`（实验）/ `hybrid`（unicode 兼容别名）。
- 上传前完整本地预检。
- 上传后标题、正文锚点、图片数、公式表示、callout 数**线上对账**。
- 稳定 JSON 输出与明确退出码。
- 仅使用乐享个人凭证，支持多 profile。

---

## 📌 硬性规则

1. 上传代码只维护在本 Skill；禁止复制到调用方。
2. 只使用从 <https://lexiangla.com/ai/claw> 获取的个人凭证。
3. 禁止读取 Agent MCP 配置、连接器 OAuth token 或扫描本地代理。
4. 创建或修改页面前完成全部本地预检，写入后必须线上对账。
5. stdout 只输出 JSON / 页面 URL，诊断和进度输出到 stderr。

---

## 📁 安装位置

调用方**不得写死**某个 Agent 的安装目录。建议安装到调用方 Skill 所在的同一个 `skills` 根目录：

```text
<skills-root>/
├── upload-markdown-to-lexiang/
├── fetch-archive-to-lexiang/
└── sync-obsidian-to-lexiang/
```

定位顺序（上层 Skill 使用）：

1. 环境变量 `LEXIANG_UPLOADER_HOME`
2. 当前上层 Skill 所在 `skills` 根目录的同级 Skill：`<skills-root>/upload-markdown-to-lexiang`
3. 当前 Agent 暴露的已安装 Skill 列表
4. 当前平台的 Skill 管理器默认安装目录

> 作者本机默认开发目录是 `~/.workbuddy/skills/upload-markdown-to-lexiang`，但**这不是跨用户公共契约**。

定位后调用方必须检查 CLI 契约：

```bash
python3 "<skill-root>/scripts/lexiang_upload.py" --version
# 要求返回 {"name":"upload-markdown-to-lexiang","version":"1.3.1","cli_api":"1"}
```

上层 Skill 必须要求 `cli_api == "1"`，**不得 import 内部 Python 模块**。

---

## 🔑 首次配置个人凭证

访问 <https://lexiangla.com/ai/claw> 获取个人凭证（含 `lxmcp_` 前缀 MCP API Token 与 `company_from`）。本 Skill 直接使用 `Authorization: Bearer <lxmcp_token>`，**不做 OAuth token 交换**。

```bash
python3 "<skill-root>/scripts/lexiang_upload.py" auth login
python3 "<skill-root>/scripts/lexiang_upload.py" auth status --check
```

凭证默认保存在 `~/.config/lexiang-upload/credentials.json`（权限 `0600`），**不会写进 Skill 或 Git 仓库**。

**多账号**可使用命名 profile，保存到 `~/.config/lexiang-upload/profiles/<profile>.json`：

```bash
python3 "<skill-root>/scripts/lexiang_upload.py" auth login --profile obsidian-sync
python3 "<skill-root>/scripts/lexiang_upload.py" auth status --profile obsidian-sync --check
python3 "<skill-root>/scripts/lexiang_upload.py" upload article.md \
  --profile obsidian-sync --parent-id "<PARENT_ID>" --json
```

也可用 `--credential-file PATH` 指定凭证读取位置。`auth login --file PATH` 表示从导出文件读取凭证内容（不是保存目标）。选择优先级：

```text
--credential-file > --profile > LEXIANG_UPLOAD_CREDENTIALS > LEXIANG_UPLOAD_PROFILE > default
```

（default 始终对应原 `credentials.json`）

---

## 🚀 上传

创建新页面：

```bash
python3 "<skill-root>/scripts/lexiang_upload.py" upload article.md \
  --parent-id "<PARENT_ID>" --name "标题" --json
```

覆盖已有页面：

```bash
python3 "<skill-root>/scripts/lexiang_upload.py" upload article.md \
  --entry-id "<ENTRY_ID>" --json
```

只做预检（不读取凭证、不写线上）：

```bash
python3 "<skill-root>/scripts/lexiang_upload.py" upload article.md \
  --parent-id "<PARENT_ID>" --dry-run --json
```

### 常用参数

| 参数 | 说明 |
|---|---|
| `upload <md>` | Markdown 文件 |
| `--work-dir DIR --md FILE` | 兼容工作包形式 |
| `--parent-id ID` | 新建 page 的父目录 |
| `--entry-id ID` | 覆盖已有 page |
| `--name NAME` | 页面名称；默认依次取 meta title、首个 H1、文件名 |
| `--name-suffix TEXT` | 显式标题后缀（默认空） |
| `--pin` | 新建后置顶（默认关闭） |
| `--source-url URL` / `--source-title TITLE` | 显式插入原文链接（优先级最高） |
| `--meta-file FILE` | 读取调用方元信息，但**不会隐式**使用其中的 parent/source |
| `--parent-from-meta` | 显式启用 meta `parent_id` |
| `--source-from-meta` | 显式启用 meta source |
| `--formula-mode unicode\|image\|native\|hybrid` | 默认 `unicode` |
| `--dry-run` | 仅预检 |
| `--json` | 成功时输出稳定 JSON |
| `--profile NAME` / `--credential-file PATH` | 凭证选择 |

> `--source-from-meta`：URL 依次取显式 `--source-url` → `meta.source_url`；仍无 URL 且有 `meta.entry_id` 时生成乐享页面 URL。标题依次取显式 `--source-title` → `meta.source_title` → `meta.title`。

---

## 🧩 可移植富文档标注

上传器在本地 preflight 识别以下**连续 blockquote**，并直接规划、创建乐享原生 callout：

```markdown
> [!stat] **73%**
> English statistic.
> 中文统计。

> [!definition] **Specific stiffness**
> English definition.
> 中文定义。

> [!note] **文章亮点**
> - **亮点一：**组织管理类比
> - **亮点二：**Evals 是新的 OKRs
```

| 标注 | 乐享 callout |
|---|---|
| `stat` | 📊 |
| `definition` | 📖 |
| `note` | 💡（可承载文章亮点列表） |

普通 Markdown blockquote 保持原转换行为。**调用方不得在上传后通过 MCP 二次替换这些标注。**

---

## 📤 调用方职责

- 调用方只负责确定 `parent_id`、`entry_id` 和归档策略（标题、置顶、来源等）。
- 本 Skill 负责 Markdown 解析、本地图片、公式转换、目标专有标注渲染、页面写入和线上对账。
- 所有识别到的本地图片都必须进入预检与线上数量对账。
- 非 Markdown 独立附件、视频和音频**不属于**本 Skill。

---

## ✅ 成功输出（JSON）

```json
{
  "ok": true,
  "action": "created",
  "entry_id": "...",
  "page_url": "...",
  "title": "...",
  "local_images": 8,
  "remote_images": 8,
  "local_callouts": 2,
  "remote_callouts": 2,
  "verified": true,
  "credential_profile": "default",
  "credential_file": "~/.config/lexiang-upload/credentials.json",
  "company_from": "...",
  "cli_api": "1",
  "version": "1.3.1"
}
```

- `--dry-run` JSON 含 `local_callouts`，不含尚未产生的 `remote_callouts`；不读取凭证但安全返回 `credential_profile` / `credential_file`。
- 任何 JSON **都不会输出 token**。

### 退出码

| 码 | 含义 |
|---|---|
| `0` | 成功 |
| `2` | 本地预检失败 |
| `3` | 个人凭证缺失、失效或被撤销 |
| `4` | 网络或乐享写入失败 |
| `5` | 线上内容对账失败 |

---

## 🧪 修改与验证

发现公共上传问题时，**只修改本仓库**并运行：

```bash
python3 -m unittest discover -s tests -v
```

兼容改动升级**次版本**；破坏 CLI 契约时升级**主版本**和 `cli_api`。

---

## 📄 License

MIT
