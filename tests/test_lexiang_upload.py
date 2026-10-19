from argparse import Namespace
from contextlib import redirect_stdout
import io
from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import sys

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lexiang_upload_core import (  # noqa: E402
    PreflightError,
    VerificationError,
    build_plan,
    convert_math,
    demote_caption_headings,
    parse_portable_callouts,
    split_large_gfm_tables,
    verify_remote,
)
from lexiang_upload import (  # noqa: E402
    AuthError,
    DEFAULT_CREDENTIALS,
    PROFILE_CREDENTIALS_DIR,
    CredentialSelector,
    MCPClient,
    MCPError,
    PersonalCredential,
    credentials_path,
    execute_plan,
    latex_to_unicode,
    load_credential,
    materialize_formula_fallback,
    prepend_source_link,
    resolve_credential_selector,
    resolve_source,
    save_credential,
    upload,
    verify_page,
)


class WAFErrorTests(unittest.TestCase):
    """403-WAF 命中时给出针对性行动指引，非 WAF 的 403 走原逻辑。"""

    @staticmethod
    def _http_error(code: int, body: str) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            url="https://mcp.lexiang-app.com/mcp",
            code=code,
            msg="Forbidden",
            hdrs=None,
            fp=io.BytesIO(body.encode("utf-8")),
        )

    def test_waf_403_raises_actionable_message(self) -> None:
        waf_html = "<!DOCTYPE html><html><title>WAF拦截页面</title></html>"
        cred = PersonalCredential("lxmcp_test_token", "csig")
        with patch("lexiang_upload.MCPClient._initialize", lambda self: None):
            client = MCPClient(cred)
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = self._http_error(403, waf_html)
            with self.assertRaises(MCPError) as ctx:
                client._post({"jsonrpc": "2.0", "method": "tools/call"})
        msg = str(ctx.exception)
        self.assertIn("WAF", msg)
        self.assertIn("403", msg)
        self.assertIn("代码片段", msg)
        self.assertIn("os.system", msg)
        # 不应把原始 HTML 倾倒给用户
        self.assertNotIn("<!DOCTYPE", msg)
        self.assertNotIn("WAF拦截页面", msg)

    def test_non_waf_403_falls_back_to_generic_message(self) -> None:
        body = '{"error":"forbidden resource"}'
        cred = PersonalCredential("lxmcp_test_token", "csig")
        with patch("lexiang_upload.MCPClient._initialize", lambda self: None):
            client = MCPClient(cred)
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = self._http_error(403, body)
            with self.assertRaises(MCPError) as ctx:
                client._post({"jsonrpc": "2.0", "method": "tools/call"})
        msg = str(ctx.exception)
        self.assertIn("403", msg)
        # 非 WAF 不应套用 WAF 指引话术
        self.assertNotIn("代码片段", msg)


class CredentialTests(unittest.TestCase):
    def test_default_profile_preserves_legacy_path(self) -> None:
        selector = resolve_credential_selector(environ={})
        self.assertEqual(selector.profile, "default")
        self.assertEqual(selector.path, DEFAULT_CREDENTIALS)
        self.assertEqual(credentials_path(selector), DEFAULT_CREDENTIALS)

    def test_named_profile_uses_profiles_directory(self) -> None:
        selector = resolve_credential_selector("obsidian-sync", environ={})
        self.assertEqual(selector.profile, "obsidian-sync")
        self.assertEqual(
            selector.path,
            PROFILE_CREDENTIALS_DIR / "obsidian-sync.json",
        )

    def test_explicit_default_profile_uses_legacy_path(self) -> None:
        selector = resolve_credential_selector("default", environ={})
        self.assertEqual(selector.profile, "default")
        self.assertEqual(selector.path, DEFAULT_CREDENTIALS)

    def test_explicit_file_precedes_explicit_profile_and_environment(self) -> None:
        selector = resolve_credential_selector(
            "obsidian-sync",
            "~/chosen.json",
            {
                "LEXIANG_UPLOAD_CREDENTIALS": "/tmp/env-file.json",
                "LEXIANG_UPLOAD_PROFILE": "env-profile",
            },
        )
        self.assertIsNone(selector.profile)
        self.assertEqual(selector.path, Path("~/chosen.json").expanduser())

    def test_environment_file_precedes_environment_profile(self) -> None:
        selector = resolve_credential_selector(
            environ={
                "LEXIANG_UPLOAD_CREDENTIALS": "/tmp/env-file.json",
                "LEXIANG_UPLOAD_PROFILE": "env-profile",
            }
        )
        self.assertIsNone(selector.profile)
        self.assertEqual(selector.path, Path("/tmp/env-file.json"))

    def test_environment_profile_is_used_without_file_override(self) -> None:
        selector = resolve_credential_selector(
            environ={"LEXIANG_UPLOAD_PROFILE": "environment-profile"}
        )
        self.assertEqual(selector.profile, "environment-profile")
        self.assertEqual(
            selector.path,
            PROFILE_CREDENTIALS_DIR / "environment-profile.json",
        )

    def test_explicit_profile_precedes_environment_file(self) -> None:
        selector = resolve_credential_selector(
            "explicit",
            environ={"LEXIANG_UPLOAD_CREDENTIALS": "/tmp/env-file.json"},
        )
        self.assertEqual(selector.profile, "explicit")
        self.assertEqual(selector.path, PROFILE_CREDENTIALS_DIR / "explicit.json")

    def test_rejects_unsafe_profiles(self) -> None:
        for profile in ("../escape", "nested/profile", "white space", ""):
            with self.subTest(profile=profile), self.assertRaises(AuthError):
                resolve_credential_selector(profile, environ={})

    def test_personal_credential_accepts_page_field_names(self) -> None:
        credential = PersonalCredential.from_dict(
            {
                "access_token": "lxmcp_test_token",
                "mcp_company_from": "company",
            }
        )
        credential.validate_shape()
        self.assertEqual(credential.mcp_token, "lxmcp_test_token")

    def test_personal_credential_accepts_nested_export(self) -> None:
        credential = PersonalCredential.from_dict(
            {
                "mcp": {
                    "mcp_token": "lxmcp_test_token",
                    "company_from": "company",
                }
            }
        )
        credential.validate_shape()
        self.assertEqual(credential.mcp_token, "lxmcp_test_token")

    def test_personal_credential_parses_copied_install_command(self) -> None:
        credential = PersonalCredential.from_text(
            'url="https://mcp.lexiang-app.com/mcp?company_from=csig" '
            'Authorization="Bearer lxmcp_test_token"'
        )
        credential.validate_shape()
        self.assertEqual(credential.company_from, "csig")
        self.assertEqual(credential.mcp_token, "lxmcp_test_token")

    def test_credential_is_saved_outside_skill_with_private_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            with patch.dict(os.environ, {"LEXIANG_UPLOAD_CREDENTIALS": str(path)}):
                save_credential(PersonalCredential("lxmcp_test_token", "company"))
                loaded = load_credential()
            self.assertEqual(loaded.company_from, "company")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                set(json.loads(path.read_text())),
                {"mcp_token", "company_from"},
            )

    def test_rejects_non_personal_mcp_token(self) -> None:
        with self.assertRaises(AuthError):
            PersonalCredential("not-an-lxmcp-token", "company").validate_shape()


class SourceMetadataTests(unittest.TestCase):
    def test_meta_source_fields_precede_meta_title(self) -> None:
        source_url, source_title = resolve_source(
            "",
            "",
            {
                "source_url": "https://example.com/source",
                "source_title": "Source title",
                "title": "Page title",
            },
            True,
            credential_loader=lambda: self.fail("credential should not be loaded"),
        )
        self.assertEqual(source_url, "https://example.com/source")
        self.assertEqual(source_title, "Source title")

    def test_explicit_source_fields_have_highest_priority(self) -> None:
        source_url, source_title = resolve_source(
            "https://explicit.example",
            "Explicit",
            {
                "source_url": "https://meta.example",
                "source_title": "Meta",
                "title": "Page",
            },
            True,
            credential_loader=lambda: self.fail("credential should not be loaded"),
        )
        self.assertEqual(source_url, "https://explicit.example")
        self.assertEqual(source_title, "Explicit")

    def test_entry_id_falls_back_to_lexiang_page_url(self) -> None:
        source_url, source_title = resolve_source(
            "",
            "",
            {"entry_id": "entry-123", "title": "Archived page"},
            True,
            credential_loader=lambda: PersonalCredential("lxmcp_test", "tenant"),
        )
        self.assertEqual(
            source_url,
            "https://lexiangla.com/pages/entry-123?company_from=tenant",
        )
        self.assertEqual(source_title, "Archived page")

    def test_dry_run_reports_local_callouts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            markdown = root / "article.md"
            markdown.write_text(
                "> [!stat] **73%**\n> English statistic.\n> 中文统计。",
                encoding="utf-8",
            )
            output = io.StringIO()
            args = Namespace(
                work_dir=None,
                md_name=None,
                md_path=str(markdown),
                meta_file=None,
                source_url="",
                source_title="",
                source_from_meta=False,
                formula_mode="unicode",
                name="",
                name_suffix="",
                parent_id="parent",
                parent_from_meta=False,
                entry_id="",
                dry_run=True,
                pin=False,
                json=True,
            )
            with redirect_stdout(output):
                upload(args)
            summary = json.loads(output.getvalue())
            self.assertEqual(summary["local_callouts"], 1)
            self.assertNotIn("remote_callouts", summary)
            self.assertEqual(summary["credential_profile"], "default")
            self.assertEqual(summary["credential_file"], str(DEFAULT_CREDENTIALS))

    def test_dry_run_does_not_load_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            markdown = Path(directory) / "article.md"
            markdown.write_text("# Title\n\nBody", encoding="utf-8")
            args = self._upload_args(markdown, dry_run=True)
            with patch(
                "lexiang_upload.load_credential",
                side_effect=AssertionError("must not read credentials"),
            ):
                with redirect_stdout(io.StringIO()):
                    upload(args)

    def test_upload_uses_selected_credential(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            markdown = root / "article.md"
            markdown.write_text("# Title\n\nBody", encoding="utf-8")
            credential_file = root / "selected.json"
            credential_file.write_text(
                json.dumps(
                    {
                        "mcp_token": "lxmcp_selected_token",
                        "company_from": "selected-company",
                    }
                ),
                encoding="utf-8",
            )
            args = self._upload_args(
                markdown,
                credential_file=str(credential_file),
            )
            output = io.StringIO()
            clients = []

            class SelectedClient:
                def __init__(self, credential):
                    clients.append(credential)

            with (
                patch("lexiang_upload.MCPClient", SelectedClient),
                patch("lexiang_upload.create_page", return_value="entry"),
                patch("lexiang_upload.execute_plan"),
                patch("lexiang_upload.verify_page", return_value=(0, 0)),
                redirect_stdout(output),
            ):
                upload(args)

            summary = json.loads(output.getvalue())
            self.assertEqual(clients[0].mcp_token, "lxmcp_selected_token")
            self.assertEqual(summary["credential_file"], str(credential_file))
            self.assertIsNone(summary["credential_profile"])
            self.assertEqual(summary["company_from"], "selected-company")

    @staticmethod
    def _upload_args(markdown: Path, **overrides) -> Namespace:
        values = {
            "work_dir": None,
            "md_name": None,
            "md_path": str(markdown),
            "meta_file": None,
            "source_url": "",
            "source_title": "",
            "source_from_meta": False,
            "formula_mode": "unicode",
            "name": "",
            "name_suffix": "",
            "parent_id": "parent",
            "parent_from_meta": False,
            "entry_id": "",
            "dry_run": False,
            "pin": False,
            "json": True,
            "profile": None,
            "credential_file": None,
        }
        values.update(overrides)
        return Namespace(**values)

    def test_existing_source_link_is_not_prepended_twice(self) -> None:
        url = "https://example.com/source"
        markdown = f"# Title\n\n> 原文链接: {url}\n\nBody"
        self.assertEqual(prepend_source_link(markdown, url, "Source"), markdown)


class AnchorTests(unittest.TestCase):
    """长锚点提取：源文链接行不参与对账。"""

    SOURCE_URL = "https://lexiangla.com/pages/4efc27856f2c45928d46fbac2fd11531"
    SOURCE_TITLE = "Tencent WorkBuddy Bench"
    # 锚点阈值是 80 个字符，正文段落必须够长才会成为锚点
    BODY = "正文锚点校验段落，用于确认长锚点提取规则。" * 5

    def _anchors_for(self, markdown: str) -> tuple[str, ...]:
        with tempfile.TemporaryDirectory() as workdir:
            return build_plan(markdown, Path(workdir)).anchors

    def test_source_link_line_is_not_used_as_anchor(self) -> None:
        # 乐享把该行规范化成 <mention-page> 节点：链接文字变成页面 id、URL 变成相对路径，
        # 因此含 URL 的锚点永远匹配不上，必须排除。
        markdown = prepend_source_link(self.BODY, self.SOURCE_URL, self.SOURCE_TITLE)
        anchors = self._anchors_for(markdown)
        self.assertTrue(anchors)
        self.assertTrue(all("原文链接" not in anchor for anchor in anchors))
        self.assertTrue(all("lexiangla.com" not in anchor for anchor in anchors))

    def test_body_lines_still_produce_anchors(self) -> None:
        markdown = prepend_source_link(self.BODY, self.SOURCE_URL, self.SOURCE_TITLE)
        self.assertIn(self.BODY[:100], self._anchors_for(markdown))

    def test_fenced_code_block_lines_are_not_used_as_anchors(self) -> None:
        """围栏代码块（含 mermaid）内容不参与锚点。

        代码块内的 HTML 标签（如 mermaid 换行 <br/>）在源文侧被 _plain_text
        整体删除、在远端 clean 文本中保留字面 "br" 字母（normalize_for_compare
        不剥标签），子串匹配必然失败。2026-09-22 修复：曾导致含时序图的文档
        6 个锚点稳定误报缺失 1 个。
        """
        mermaid = (
            "```mermaid\n"
            "sequenceDiagram\n"
            "    L->>C: premissionFileDecryptionRest<br/>body=密文流 · "
            "data~counSize · data~unitNames=采集账号\n"
            "```\n"
        )
        markdown = mermaid + self.BODY
        anchors = self._anchors_for(markdown)
        self.assertIn(self.BODY[:100], anchors)
        self.assertFalse(
            any("premissionFileDecryptionRest" in anchor for anchor in anchors),
            "代码块内的长行不应成为锚点",
        )

    def test_bare_url_linkified_remotely_is_not_a_missing_anchor(self) -> None:
        """源文裸 URL 在线上被渲染成 [url](url) 时，锚点不应误报缺失。

        乐享 clean 文本会把裸 URL 链接化为 [url](url)（URL 因此出现两次），
        而锚点里 URL 只出现一次，归一化后子串匹配必然失败 —— 表现为每次上传都
        VERIFY_ERROR「缺少长段落锚点」，且因未写入 manifest 而被反复重传。
        2026-09-24 修复：normalize_for_compare 先解开 markdown 链接/图片语法。
        """
        url = "https://knot.woa.com/skills/detail/72839"
        paragraph = (
            'WorkBuddy 支持通过"技能（Skill）"扩展能力，司内有个技能市场 knot，'
            f"访问 knot：{url}，一键安装。这段文字足够长，以便被选作长段落锚点，不依赖网址本身的长度。"
        )
        with tempfile.TemporaryDirectory() as workdir:
            plan = build_plan(paragraph, Path(workdir))
        self.assertTrue(plan.anchors, "长段落应产生锚点")
        remote_clean = paragraph.replace(url, f"[{url}]({url})")
        # 不应抛 VerificationError
        verify_remote(plan, remote_clean, remote_clean, 0, 0)

    def test_missing_link_target_paragraph_still_fails_verification(self) -> None:
        """反向保护：归一化放宽后，真正缺失的段落仍必须报错（不能静默通过）。"""
        paragraph = (
            "这是一段刻意写得很长的正文段落，用于确认链接语法归一化并没有把缺失检测一并放宽。"
            "锚点匹配依然要求线上正文里真的存在对应内容，而不是只要线上有任意文字就算通过。"
            "这里继续补充若干句子，确保段落长度稳定超过锚点门槛，避免测试因为长度不足而失效。"
        )
        with tempfile.TemporaryDirectory() as workdir:
            plan = build_plan(paragraph, Path(workdir))
        self.assertTrue(plan.anchors, "长段落应产生锚点")
        with self.assertRaises(VerificationError):
            verify_remote(plan, "完全不相干的线上内容", "完全不相干的线上内容", 0, 0)

    def test_markdown_link_in_anchor_matches_remote_display_text_only(self) -> None:
        """源文是 [文字](url)、线上只保留显示文字时，也应命中。"""
        paragraph = (
            "参考 [配置指南](https://example.com/docs) 的第三小节，即可完成全部配置流程；"
            "如果中途遇到权限相关的报错，请回到第一步重新确认凭证是否已经授权完成。"
            "这段文字同样是刻意写长的，用来确保它稳定落在长段落锚点的选取范围之内。"
        )
        with tempfile.TemporaryDirectory() as workdir:
            plan = build_plan(paragraph, Path(workdir))
        self.assertTrue(plan.anchors, "长段落应产生锚点")
        remote_clean = paragraph.replace("[配置指南](https://example.com/docs)", "配置指南")
        verify_remote(plan, remote_clean, remote_clean, 0, 0)

    def test_https_urls_with_underscores_do_not_leave_query_residue(self) -> None:
        """带下划线的 https 链接必须整段从锚点消失。

        乐享 clean 文本会保留 [文字](https://...)，归一化时先删下划线再识别整段 URL。
        锚点若先把 _ 换成空格，URL 被拆开，查询串会残留，对账稳定误报。
        """
        profile = "https://every.to/@naveen_6804?utm_cta_source=p_how_codex_happened_naveen_naidu_1"
        paragraph = (
            "In Codex, GPT-5 was fast, and it seemed smart. One of our most technical engineers, "
            f"[Naveen Naidu]({profile}), had used it to track down a mysterious freeze for days."
        )
        with tempfile.TemporaryDirectory() as workdir:
            plan = build_plan(paragraph, Path(workdir))
        self.assertTrue(plan.anchors, "长段落应产生锚点")
        for anchor in plan.anchors:
            self.assertNotIn("utm", anchor)
            self.assertNotIn("every.to", anchor)
            self.assertNotIn("6804", anchor)
        verify_remote(plan, paragraph, paragraph, 0, 0)

    def test_relative_cite_href_survives_to_match_remote(self) -> None:
        """双括号相对脚注的 href 会留在乐享 clean 文本里，锚点不能先把它删掉。"""
        cite = (
            "/p/how-codex-happened/interviews/tibo/"
            "?cite=research-start-december"
            "&utm_cta_source=p_how_codex_happened_building_the_builders_cite_research_start_december"
            "#final-20261015-0012"
        )
        paragraph = (
            "“I got involved with coding agents around December 2024,”"
            f"[[5]]({cite}) he told me. We were sitting in the OpenAI cafeteria with a slice of pizza."
        )
        remote_clean = paragraph.replace("[[5]]", r"[\[5\]]")
        with tempfile.TemporaryDirectory() as workdir:
            plan = build_plan(paragraph, Path(workdir))
        self.assertTrue(plan.anchors, "长段落应产生锚点")
        self.assertTrue(any("/p/" in anchor for anchor in plan.anchors))
        verify_remote(plan, remote_clean, remote_clean, 0, 0)

    def test_single_bracket_relative_link_drops_href(self) -> None:
        """单括号相对链接在线上会被解开成显示文字，锚点里不能留下查询串。"""
        href = (
            "/p/how-codex-happened/interviews/alex/"
            "?cite=awkward-revisit&utm_cta_source=p_how_codex_happened_cite_awkward"
            "#final-20261015-0003"
        )
        paragraph = (
            "There were good reasons to stay in the editor after the model launched, "
            "and usage grew more than 10-fold by last October. "
            f"[This made what I had come to say a little awkward.]({href}) "
            "Every had begun to build a reputation for testing new AI models on real work."
        )
        with tempfile.TemporaryDirectory() as workdir:
            plan = build_plan(paragraph, Path(workdir))
        self.assertTrue(plan.anchors)
        for anchor in plan.anchors:
            self.assertNotIn("/p/", anchor)
            self.assertNotIn("utm", anchor)
        verify_remote(plan, paragraph, paragraph, 0, 0)

    def test_https_url_strip_does_not_eat_the_next_link_label(self) -> None:
        """URL 字符集不能包含括号，否则会把紧挨着的下一个链接文字吞进 URL。"""
        url = "https://stratechery.com/2025/an-interview-with-openai-ceo-sam-altman-about-devday-and-the-ai-buildout/"
        paragraph = (
            "“在某种意义上，OpenAI 是在押注一连串创业公司，只是它们都在内部，”"
            f"Altman 在[2025 年接受]({url})[Ben Thompson 的采访]({url})时说。"
            "公司内部这种并行探究，有时让人觉得混乱而浪费，给探索留空间和搞成一团乱只有一条细线。"
        )
        with tempfile.TemporaryDirectory() as workdir:
            plan = build_plan(paragraph, Path(workdir))
        self.assertTrue(any("Ben Thompson" in anchor for anchor in plan.anchors))
        verify_remote(plan, paragraph, paragraph, 0, 0)


class TableColumnLimitTests(unittest.TestCase):
    """乐享单表列数上限 10 列，必须在预检阶段拦截。"""

    def _plan_for(self, markdown: str):
        with tempfile.TemporaryDirectory() as workdir:
            return build_plan(markdown, Path(workdir))

    def test_table_over_ten_columns_is_rejected(self) -> None:
        header = "|" + "|".join(f"c{i}" for i in range(13)) + "|"
        separator = "|" + "---|" * 13
        row = "|" + "|".join("x" for _ in range(13)) + "|"
        with self.assertRaises(PreflightError) as caught:
            self._plan_for("\n".join([header, separator, row]))
        self.assertIn("超过乐享单表上限", str(caught.exception))

    def test_table_at_ten_columns_is_accepted(self) -> None:
        header = "|" + "|".join(f"c{i}" for i in range(10)) + "|"
        separator = "|" + "---|" * 10
        row = "|" + "|".join("x" for _ in range(10)) + "|"
        plan = self._plan_for("\n".join([header, separator, row]))
        self.assertTrue(plan.segments)


class MathConversionTests(unittest.TestCase):
    def test_converts_inline_and_display_math(self) -> None:
        converted, formulas = convert_math("A $K_V/\\rho$ value.\n\n$$\\Phi=n\\cdot d$$")
        self.assertIn("$`K_V/\\rho`$", converted)
        self.assertEqual([formula.display for formula in formulas], [False, True])

    def test_does_not_touch_code(self) -> None:
        source = "Use `$HOME` and:\n```sh\necho '$x$'\n```\nThen $x^2$."
        converted, formulas = convert_math(source)
        self.assertIn("echo '$x$'", converted)
        self.assertEqual(len(formulas), 1)

    def test_does_not_treat_currency_as_math(self) -> None:
        source = "CapEx is $7.6tn, rising from $494.5bn to $1.13tn."
        converted, formulas = convert_math(source)
        self.assertEqual(converted, source)
        self.assertEqual(formulas, ())

    def test_unicode_formula_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            converted, count = materialize_formula_fallback(
                "# Formula\n\nSpecific stiffness is $K_V/\\rho$.",
                Path(directory),
                render_images=False,
            )
            self.assertEqual(count, 1)
            self.assertIn("Kᵥ/ρ", converted)

    def test_latex_to_unicode(self) -> None:
        self.assertEqual(latex_to_unicode(r"B_4C"), "B₄C")
        self.assertEqual(latex_to_unicode(r"C_{11} > 1,000"), "C₁₁ > 1,000")


class PlanTests(unittest.TestCase):
    def test_requires_all_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PreflightError):
                build_plan("# Title\n\n![](images/missing.png)", Path(directory))

    def test_supports_arbitrary_relative_image_paths_and_references(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "assets").mkdir()
            image = root / "assets" / "figure one.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n")
            plan = build_plan(
                "# Title\n\nText\n\n![](assets/figure%20one.png)\n\n"
                "More text.\n\n![](assets/figure%20one.png)",
                root,
            )
            self.assertEqual(len(plan.image_paths), 2)
            self.assertEqual([segment.kind for segment in plan.segments], ["text", "image", "text", "image"])

    def test_supports_unescaped_and_angle_bracket_image_paths_with_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "images").mkdir()
            image = root / "images" / "Pasted image 20260711234206.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n")
            plan = build_plan(
                "# Title\n\n"
                "![first](images/Pasted image 20260711234206.png)\n\n"
                "Text\n\n"
                "![second](<images/Pasted image 20260711234206.png>)",
                root,
            )
            self.assertEqual(plan.image_paths, (image.resolve(), image.resolve()))
            self.assertEqual(
                [segment.kind for segment in plan.segments],
                ["text", "image", "text", "image"],
            )

    def test_image_path_with_spaces_and_title_is_preflighted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "images").mkdir()
            image = root / "images" / "figure one.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n")
            plan = build_plan(
                '# Title\n\n![alt](images/figure one.png "Figure title")',
                root,
            )
            self.assertEqual(plan.image_paths, (image.resolve(),))

    def test_missing_unescaped_image_path_with_spaces_fails_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PreflightError):
                build_plan(
                    "# Title\n\n![](images/Pasted image missing.png)",
                    Path(directory),
                )

    def test_rejects_image_path_outside_document_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PreflightError):
                build_plan("# Title\n\n![](../secret.png)", Path(directory))

    def test_document_starting_with_image_uploads_without_replace_content(self) -> None:
        """图片开头的文档：不再使用 replace_content 占位，直接按序 image→append。

        前置约定：execute_plan 调用前 entry 已为空，因此无需首段 replace。
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "cover.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n")
            plan = build_plan("![](cover.png)\n\n# Title\n\nBody", root)
            events = []
            with (
                patch("lexiang_upload.replace_page_text", side_effect=lambda *_: events.append("replace")),
                patch("lexiang_upload.upload_image", side_effect=lambda *_: events.append("image")),
                patch("lexiang_upload.append_page_text", side_effect=lambda *_: events.append("append")),
            ):
                execute_plan(object(), "entry", plan)
            self.assertEqual(events, ["image", "append"])
            self.assertNotIn("replace", events)

    def test_text_first_document_uses_create_descendant_not_replace(self) -> None:
        """文本开头（含引用块）的文档：首段也走 append(create_descendant)，绝不 replace_content。

        回归 2026-07-16 WAIC PR 稿故障：含多行引用块的首段经 replace_content
        触发服务端 round-trip 结构校验失败。
        """
        plan = build_plan(
            "# 标题\n\n> **草稿版本**：v0.4\n> **用途**：对外宣发\n\n正文一段。",
            Path("."),
        )
        events = []
        with (
            patch("lexiang_upload.replace_page_text", side_effect=lambda *_: events.append("replace")),
            patch("lexiang_upload.upload_image", side_effect=lambda *_: events.append("image")),
            patch("lexiang_upload.append_page_text", side_effect=lambda *_: events.append("append")),
        ):
            execute_plan(object(), "entry", plan)
        self.assertNotIn("replace", events)
        self.assertTrue(all(e == "append" for e in events))
        self.assertGreaterEqual(events.count("append"), 1)

    def test_large_table_is_split(self) -> None:
        table = "| Name |\n|---|\n" + "\n".join(f"| row-{index} |" for index in range(20))
        converted = split_large_gfm_tables(table, max_rows=10)
        self.assertEqual(converted.count("| Name |"), 3)

    def test_figure_caption_is_not_heading(self) -> None:
        converted = demote_caption_headings("# Results\n\n## FIG. 1. Result chart.")
        self.assertIn("# Results", converted)
        self.assertNotIn("## FIG.", converted)

    def test_recognizes_stat_and_definition_callouts(self) -> None:
        markdown = (
            "> [!stat] **73%**\n"
            "> English statistic.\n"
            "> 中文统计。\n\n"
            "> [!definition] **Specific stiffness**\n"
            "> English definition.\n"
            "> 中文定义。"
        )
        callouts, _ = parse_portable_callouts(markdown)
        self.assertEqual([callout.kind for callout in callouts], ["stat", "definition"])
        self.assertEqual([callout.icon for callout in callouts], ["📊", "📖"])
        plan = build_plan(markdown, Path("."))
        self.assertEqual([segment.kind for segment in plan.segments], ["callout", "callout"])

    def test_recognizes_note_callout_with_list_body(self) -> None:
        markdown = (
            "> [!note] **文章亮点**\n"
            "> - **亮点一：**组织管理类比\n"
            "> - **亮点二：**Evals 是新的 OKRs\n"
            "> - **亮点三：**Palantir 销售的是转型"
        )
        callouts, _ = parse_portable_callouts(markdown)
        self.assertEqual(len(callouts), 1)
        self.assertEqual(callouts[0].kind, "note")
        self.assertEqual(callouts[0].icon, "💡")
        self.assertIn("- **亮点二：**", callouts[0].markdown)

    def test_does_not_misrecognize_regular_blockquote(self) -> None:
        markdown = "> A regular quotation.\n> It remains ordinary Markdown."
        callouts, _ = parse_portable_callouts(markdown)
        self.assertEqual(callouts, ())
        plan = build_plan(markdown, Path("."))
        self.assertEqual([segment.kind for segment in plan.segments], ["text"])

    def test_callout_plan_creates_native_descendant(self) -> None:
        plan = build_plan(
            "> [!stat] **73%**\n> English statistic.\n> 中文统计。",
            Path("."),
        )
        client = FakeClient(plan)
        execute_plan(client, "entry", plan)
        created = [
            arguments
            for tool, arguments in client.json_calls
            if tool == "block_create_block_descendant"
        ]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["descendant"][0]["block_type"], "callout")
        self.assertEqual(created[0]["descendant"][0]["callout"]["icon"], "📊")
        self.assertEqual(verify_page(client, "entry", plan), (0, 1))


class VerificationTests(unittest.TestCase):
    def test_remote_verification_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = build_plan(
                "# Mechanical Properties / 机械性能\n\n"
                "An important caveat applies to layered materials and this paragraph "
                "contains enough detail to be used as a stable content anchor for testing.",
                Path(directory),
            )
            verify_remote(plan, plan.markdown, plan.markdown, 0)

    def test_remote_verification_detects_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = build_plan(
                "# Mechanical Properties\n\n"
                "This is a deliberately long source paragraph whose disappearance "
                "must be detected rather than silently accepted after upload.",
                Path(directory),
            )
            with self.assertRaises(VerificationError):
                verify_remote(plan, "Mechanical Properties", "", 0)

    def test_remote_verification_detects_callout_count_loss(self) -> None:
        plan = build_plan(
            "> [!definition] **Term**\n> English definition.\n> 中文定义。",
            Path("."),
        )
        with self.assertRaises(VerificationError):
            verify_remote(plan, plan.markdown, plan.markdown, 0, 0)

    def test_page_verification_retries_eventually_consistent_reads(self) -> None:
        plan = build_plan(
            "# Title\n\n"
            "This deliberately long paragraph becomes visible after the first "
            "eventually consistent page read and must pass on retry.",
            Path("."),
        )
        client = FlakyVerificationClient(plan)
        with patch("lexiang_upload.time.sleep"):
            self.assertEqual(verify_page(client, "entry", plan, attempts=2), (0, 0))
        self.assertEqual(client.clean_fetches, 2)

    def test_anchor_survives_ordered_list_renumbering(self) -> None:
        """乐享 round-trip 会把有序列表序号重编号（1.2.3.→1.1.1.），
        锚点去掉行首列表标记后应仍能匹配，否则序号≥2 的列表项会误判缺失。"""
        body = (
            "1. **快速出原型 Demo**：基于知识库中的产品设计规范和历史设计稿，"
            "让 WorkBuddy 快速生成前端原型，大幅缩短从想法到成品的交付周期。\n"
            "2. **生成 PRD 初稿**：告诉 AI 需求背景和目标，它自动从知识库拉取"
            "相关的用户调研和竞品数据，生成结构化的需求文档初稿而非从零写起。\n"
        )
        plan = build_plan(body, Path("."))
        # 远程 clean 文本：乐享把 2. 重编号为 1.
        remote = (
            "1. **快速出原型 Demo**：基于知识库中的产品设计规范和历史设计稿，"
            "让 WorkBuddy 快速生成前端原型，大幅缩短从想法到成品的交付周期。\n"
            "1. **生成 PRD 初稿**：告诉 AI 需求背景和目标，它自动从知识库拉取"
            "相关的用户调研和竞品数据，生成结构化的需求文档初稿而非从零写起。\n"
        )
        # 不应抛 VerificationError
        verify_remote(plan, remote, remote, 0)

    def test_anchor_strips_unordered_list_marker(self) -> None:
        """无序列表项锚点去掉 `- ` 前缀后应能匹配远程文本。"""
        body = (
            "- **增加自动化校验逻辑**：在 Agent 工作流中插入检查点 Checkpoint，"
            "让它跑完一步先自验结果正确性再进入下一步，这样 Agent 能跑得更久更稳。\n"
        )
        plan = build_plan(body, Path("."))
        remote = (
            "- **增加自动化校验逻辑**：在 Agent 工作流中插入检查点 Checkpoint，"
            "让它跑完一步先自验结果正确性再进入下一步，这样 Agent 能跑得更久更稳。\n"
        )
        verify_remote(plan, remote, remote, 0)


class FakeClient:
    def __init__(self, plan) -> None:
        self.plan = plan
        self.json_calls = []

    def json(self, tool, arguments):
        self.json_calls.append((tool, arguments))
        if tool == "block_convert_content_to_blocks":
            return {
                "data": {
                    "descendant": [
                        {
                            "block_id": "paragraph",
                            "block_type": "p",
                            "text": {"elements": []},
                        }
                    ],
                    "children": ["paragraph"],
                }
            }
        if tool == "block_list_block_children":
            return {"data": {"blocks": [{"block_type": "callout"}]}}
        return {"data": {}}

    def text(self, tool, arguments):
        if tool == "block_update_page":
            return "succeeded"
        if tool == "block_fetch_page":
            return self.plan.markdown
        return ""


class FlakyVerificationClient(FakeClient):
    def __init__(self, plan) -> None:
        super().__init__(plan)
        self.clean_fetches = 0

    def text(self, tool, arguments):
        if tool == "block_fetch_page" and arguments.get("render_mode") == "clean":
            self.clean_fetches += 1
            return "" if self.clean_fetches == 1 else self.plan.markdown
        return super().text(tool, arguments)

    def json(self, tool, arguments):
        if tool == "block_list_block_children":
            return {"data": {"blocks": []}}
        return super().json(tool, arguments)


if __name__ == "__main__":
    unittest.main()
