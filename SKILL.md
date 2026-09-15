---
name: upload-markdown-to-lexiang
version: "1.3.3"
author: ajaxhe
license: MIT
category: productivity
description: >-
  Upload a Markdown document with local images and formulas to an editable
  Tencent Lexiang page. Performs local preflight, deterministic mixed
  text/image upload, and remote verification. Use when publishing Markdown to
  Lexiang or when another skill needs the shared lexiang-upload CLI.
  将 Markdown 图文文档可靠上传为乐享在线文档，供归档、PDF 翻译和笔记同步 Skill 复用。
tags: markdown, lexiang, upload, knowledge-base, images
disable-model-invocation: false
---

# Upload Markdown to Lexiang

这是 Markdown → 乐享在线文档的唯一公共上传层。抓取、翻译、目录策略、增量同步等业务逻辑应留在上层 Skill。
可移植 Markdown 中的目标专有标注解析与乐享块渲染属于本 Skill，不由调用方上传后修补。

## 硬性规则

1. 上传代码只维护在本 Skill；禁止复制到调用方。
2. 只使用从 <https://lexiangla.com/ai/claw> 获取的个人凭证。
3. 禁止读取 Agent MCP 配置、连接器 OAuth token 或扫描本地代理。
4. 创建或修改页面前完成全部本地预检，写入后必须线上对账。
5. stdout 只输出 JSON/页面 URL，诊断和进度输出到 stderr。

## 定位本 Skill

调用方不得写死某个 Agent 的安装目录。按以下顺序定位：

1. 环境变量 `LEXIANG_UPLOADER_HOME`。
2. 当前上层 Skill 所在 `skills` 根目录的同级 Skill：
   `<skills-root>/upload-markdown-to-lexiang`。
3. 当前 Agent 暴露的已安装 Skill 列表。
4. 当前平台的 Skill 管理器默认安装目录。

用户 ajaxhe 的本地开发默认目录是
`~/.workbuddy/skills/upload-markdown-to-lexiang`，这不是跨用户公共契约。

定位后调用：

```bash
python3 "<skill-root>/scripts/lexiang_upload.py" --version
```

上层 Skill 必须要求 `cli_api == "1"`，不得 import 内部 Python 模块。

## 首次配置个人凭证

页面提供的凭证是 `lxmcp_` 前缀 MCP API Token 和 `company_from`。本 Skill
直接使用 `Authorization: Bearer <lxmcp_token>`，不做 OAuth token 交换。

```bash
python3 "<skill-root>/scripts/lexiang_upload.py" auth login
python3 "<skill-root>/scripts/lexiang_upload.py" auth status --check
```

凭证默认保存在 `~/.config/lexiang-upload/credentials.json`，权限为 `0600`，
不会写进 Skill 或 Git 仓库。

多账号可使用命名 profile，保存到
`~/.config/lexiang-upload/profiles/<profile>.json`：

```bash
python3 "<skill-root>/scripts/lexiang_upload.py" auth login \
  --profile obsidian-sync
python3 "<skill-root>/scripts/lexiang_upload.py" auth status \
  --profile obsidian-sync --check
python3 "<skill-root>/scripts/lexiang_upload.py" upload article.md \
  --profile obsidian-sync --parent-id "<PARENT_ID>" --json
```

也可用 `--credential-file PATH` 指定凭证保存/读取位置。`auth login --file PATH`
表示从导出文件读取凭证内容，不是保存目标。选择优先级为：
`--credential-file`、`--profile`、`LEXIANG_UPLOAD_CREDENTIALS`、
`LEXIANG_UPLOAD_PROFILE`、default；default 始终对应原有 `credentials.json`。

> ⚠️ **上传前核对凭证租户（2026-09-15 坑）**：default `credentials.json` 的
> `company_from` 可能不是目标知识库所在租户（例如被外链分享租户
> `e6c565d6d16811efac17768586f8a025` 覆盖）。租户不匹配时父目录会报
> `code 71 不存在ID为...的kb_entry资源`。遇到 code 71 先检查凭证
> `company_from`；ajaxhe 个人知识库（csig 租户）上传固定使用
> `--profile csig`。

## 上传

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

只做预检：

```bash
python3 "<skill-root>/scripts/lexiang_upload.py" upload article.md \
  --parent-id "<PARENT_ID>" --dry-run --json
```

## 调用方职责

- 调用方只负责确定 `parent_id`、`entry_id` 和归档策略（标题、置顶、来源等）。
- 本 Skill 负责 Markdown 解析、本地图片、公式转换、目标专有标注渲染、页面写入和线上对账。
- 本地图片路径支持 `%20`、未转义空格及 `<images/file name.png>` 形式；所有识别到的
  本地图片都必须进入预检与线上数量对账。
- `> [!stat]`、`> [!definition]` 与 `> [!note]` 连续 blockquote 由本 Skill分别渲染为
  📊/📖/💡 乐享 callout；`note` 可承载文章亮点列表。调用方禁止二次 MCP 修补。
  普通 blockquote 不受影响。
- 非 Markdown 独立附件、视频和音频不属于本 Skill。

## 页面写入不变量（重要）

`execute_plan` 写入前，entry 必须为空（新建页初始 0 block；覆盖已有页时先
`clear_existing_page`）。因此**所有片段（含首段）统一走
`block_convert_content_to_blocks` → `block_create_block_descendant` 逐块写入**，
不再对首段使用 `block_update_page` 的 `replace_content`。

原因（2026-07-16 修复）：`replace_content` 对含多行引用块等结构的首段，会触发
乐享服务端 round-trip 结构校验失败（报
`replacement content is structurally unstable after round trip`），且此时页面
已被 clear 清空 → 留下空壳 entry。改为 create_descendant 后同一内容可稳定写入，
线上对账 verified=true。修改此处务必保持「写入前页面为空」这一前置条件。

## 乐享平台硬限制（预检已覆盖）

| 限制 | 表现 | 本 Skill 的处理 |
|---|---|---|
| 单表列数 ≤ 10 | 超限时服务端报 `column_size: ... less than or equal to 10`（code 51），**此时页面已被 clear 清空**，留下半成品 entry | `check_table_columns` 在预检阶段直接报错，要求调用方按列分组拆表（每张表保留第 0 列作行标签列） |
| 单表行数 ≤ 10 | 平台自动分页 | `split_large_gfm_tables` 主动按 10 行切分并重复表头 |
| 同站外链被改写 | `[标题](https://lexiangla.com/pages/xxx)` 在线上变成 `<mention-page url="/pages/xxx">xxx</mention-page>`：链接文字替换为条目 id、URL 变成相对路径 | 源文链接行不参与长锚点对账（见下） |

调用方遇到「列数超限」时**不要**指望上传层自动拆表：按列拆表会改变文档结构，
分组边界必须由了解表格语义的上层 Skill 决定。

## 线上对账锚点（重要）

`verify_remote` 用「长段落锚点」做线上对账：从源 Markdown 每行提取纯文本
（≥80 字），`normalize_for_compare` 归一后检查是否为远程 clean 文本的子串。

**源文链接行不作为锚点**（2026-09-15 修复）：`prepend_source_link` 注入的
`**原文链接**：[标题](url)` 行，在线上会被乐享规范化成页内引用节点
（`<mention-page url="/pages/<id>">`），链接文字被替换成条目 id、URL 变成相对路径，
因此该行纯文本（含原始 URL）永远无法命中远程文本，会让每次上传都稳定误报
「缺少长段落锚点 1/N」。`_anchors` 用 `_SOURCE_LINK_LINE_RE` 跳过这一行。

**列表项锚点必须去掉行首列表标记**（2026-07-20 修复）：乐享 round-trip 会把
有序列表序号重编号（如 `1. 2. 3.` 全部变成 `1. 1. 1.`），序号本身不是稳定
锚点。`_anchors` 在提取前用 `_LIST_PREFIX_RE` 去掉 `数字.` / `数字)` / `-` /
`*` / `+` 行首标记，使锚点只依赖正文内容。修改此处时勿重新引入对列表序号的
依赖，否则序号 ≥2 的列表项会在远程文本中找不到子串匹配而误判缺失
（报 `缺少长段落锚点`）。

完整参数、JSON 输出和退出码见 [references/cli-contract.md](references/cli-contract.md)。

## 修改与验证

发现公共上传问题时，只修改本仓库并运行：

```bash
python3 -m unittest discover -s tests -v
```

兼容改动升级次版本；破坏 CLI 契约时升级主版本和 `cli_api`。
