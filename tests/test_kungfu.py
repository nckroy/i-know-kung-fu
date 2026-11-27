# Copyright 2026 Nicole Roy
# SPDX-License-Identifier: Apache-2.0
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from kungfu import bundle, frontmatter, select, topics  # noqa: E402
from kungfu.cli import main
from kungfu.config import Config
from kungfu.extract import Plan, run
from kungfu.importers import neutralize
from kungfu import risk  # noqa: E402
from kungfu.importers import llm_wiki as wiki_imp
from kungfu.importers import obsidian as obs_imp
from kungfu.model import Learning
from kungfu.sanitize import Redactor, residual_secrets



ACK = risk.acknowledge("import", flag=True, say=lambda m: None)  # as with --accept-risk


class FakeClaude:
    """Stands in for anthropic.Anthropic(): answers lesson and examination requests with valid JSON.

    Lessons: one per note, titled from the note, with a statement that includes the note's first line
    (so tests can check what was sent and that the answer is re-scrubbed). Examination: "new", except
    lessons whose title mentions OpenTofu, which get "not_applicable"."""

    def __init__(self, refuse_first: bool = False):
        import json as _json
        from types import SimpleNamespace as NS

        self.calls = []
        fake = self

        class Messages:
            def create(self, **kw):
                fake.calls.append(kw)
                if refuse_first and len(fake.calls) == 1:
                    return NS(stop_reason="refusal", content=[])
                user = kw["messages"][0]["content"]
                if "<lesson" in user:
                    n = user.count("<lesson ")
                    titles = re.findall(r"<lesson n=\"\d+\">\n# (.*)", user)
                    verdicts = [{"lesson": i + 1,
                                 "verdict": "not_applicable" if "OpenTofu" in titles[i] else "new",
                                 "explanation": "Your notes don't cover this.", "adaptation": "",
                                 "related_notes": [1] if "<note n=\"1\"" in user else []} for i in range(n)]
                    body = {"verdicts": verdicts}
                else:
                    notes = re.findall(r'<note id="(\d+)"[^>]*>\n<title>(.*?)</title>\n(.*?)\n', user)
                    body = {"lessons": [{
                        "title": f"Lesson: {t}", "statement": f"From the note: {first}",
                        "why": "Because it keeps things predictable.", "how_to_apply": ["Do the thing."],
                        "applies_when": "Always.", "not_when": "Never.", "topics": ["Git Hygiene"],
                        "confidence": "medium", "based_on": [int(i)]} for i, t, first in notes],
                        "skipped": []}
                return NS(stop_reason="end_turn", content=[NS(type="text", text=_json.dumps(body))])

        self.beta = NS(messages=Messages())
        self.models = NS(retrieve=lambda model: NS(id=model))


def with_fake_claude(fake=None):
    """Patch the CLI so export/import/init use a fake Claude and skip the live preflight."""
    from unittest import mock

    fake = fake or FakeClaude()
    return fake, [mock.patch("kungfu.claude.client", return_value=fake),
                  mock.patch("kungfu.cli._claude_ready", return_value=True)]


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class Fixture(unittest.TestCase):
    """A fake home: Claude memories, a repo with CLAUDE.md and a skill, an Obsidian vault, an LLM wiki."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        os.environ["KUNGFU_CONFIG"] = str(self.home / "none.toml")
        claude = self.home / ".claude"
        proj = claude / "projects" / "-x-git-widgetfactory"
        write(proj / "s.jsonl", json.dumps({"cwd": str(self.home / "git" / "widgetfactory")}) + "\n")
        mem = proj / "memory"
        write(mem / "MEMORY.md", "- index\n")
        write(mem / "pass-aws-region-flag.md", """---
name: pass-aws-region-flag
description: Every aws CLI command must pass --region explicitly
metadata:
  type: feedback
---
Always pass `--region eu-west-3` on every aws command in widgetfactory. Jane Smith hit "no region specified"
when the default was missing. Ping jane@example.org if it breaks; host db1.corp is the primary.
""")
        write(mem / "user-role.md", "---\nname: user-role\ndescription: who she is\ntype: user\n---\nShe is a senior director who prefers terse answers and bullet lists.\n")
        write(mem / "todo-review-backups.md", "---\nname: todo-review-backups\ntype: feedback\n---\nReview the database backups next week for the retention policy.\n")
        write(mem / "deploy-token.md", "---\nname: deploy-token\ntype: reference\n---\nThe deploy uses token ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8" + " stored in CI secrets for the release job.\n")
        write(claude / "CLAUDE.md", "# Global\n\n## Use OpenTofu, not Terraform\n\nFor infrastructure as code use tofu. Pin the tool version in the repo.\n")
        repo = self.home / "git" / "widgetfactory"
        (repo / ".git").mkdir(parents=True)
        write(repo / "CLAUDE.md", "# Widgetfactory\n\n## Testing\n\nRun `pytest -q` before every push; CI runs the same five gates and a green local run is the bar.\n\n## Tiny\n\nok\n")
        write(repo / ".claude/skills/release/SKILL.md", "---\nname: release-checklist\ndescription: Steps to cut a release\n---\nTag the commit, push the tag, then watch the release workflow until the artifacts are published.\n")
        write(repo / "CLAUDE.local.md", "## My setup\n\nMy personal sandbox account is in the 1Password vault called Personal; never commit it.\n")
        self.vault = self.home / "Vault"
        (self.vault / ".obsidian").mkdir(parents=True)
        write(self.vault / "People/Jane Smith.md", "Jane runs the platform team.\n")
        write(self.vault / "Topics/Git hygiene.md", "---\ntags: [git, kungfu]\n---\nPrune merged branches after every merge, then switch back to the base branch. Ask [[Jane Smith]] first.\n")
        write(self.vault / "Topics/Untagged.md", "This note has no share tag so it should never be offered for export by default.\n")
        write(self.vault / "Daily/2026-11-01.md", "#kungfu Had a 1:1 about a promotion and salary bands today; keep this between us.\n")
        write(self.vault / "Topics/Secret plans.md", "---\nshare: false\ntags: [kungfu]\n---\nThis one is explicitly marked share false so it must be held even though tagged.\n")
        self.wiki = self.home / "wiki"
        write(self.wiki / "index.md", "# Index\n")
        write(self.wiki / "log.md", "# Log\n")
        write(self.wiki / "raw/article.md", "Someone else's article text that must never be exported.\n")
        write(self.wiki / "wiki/concepts/Retrieval augmented generation.md", "---\ntype: concept\ntags: [llm]\n---\nRAG retrieves chunks at query time; a compiled wiki accumulates synthesis instead and stays current.\n")
        write(self.wiki / "wiki/people/Andrej.md", "---\ntype: person\n---\nResearcher who described the LLM wiki pattern in a gist about compounding knowledge.\n")

    def tearDown(self):
        self._tmp.cleanup()

    def config(self) -> Config:
        return Config(claude_dir=self.home / ".claude", repo_roots=[self.home / "git"],
                      redact_terms={"Acme": "the organization"}, allow_domains=["github.com"])

    def scan(self):
        return run(self.config(), Plan(vaults=[self.vault], wikis=[self.wiki]))


class TestExtraction(Fixture):
    def test_sources_and_holds(self):
        result = self.scan()
        by_title = {i.title: i for i in result.items}
        mem = by_title["Pass aws region flag"]
        self.assertIsNone(mem.held)
        self.assertIn("[project]", mem.body)  # repo name redacted
        self.assertIn("[person]", mem.body)  # People/ note title redacted
        self.assertIn("[email]", mem.body)
        self.assertIn("[internal-host]", mem.body)
        self.assertIn("aws", mem.topics)
        self.assertEqual(by_title["User role"].held, "memory about the person (type: user)")
        self.assertIn("to-do", by_title["Todo review backups"].held)
        token = by_title["Deploy token"]
        self.assertIn("secret", token.held)
        self.assertNotIn("ghp_", token.body)
        self.assertIn("Use OpenTofu, not Terraform", by_title)
        self.assertIn("infrastructure-as-code", by_title["Use OpenTofu, not Terraform"].topics)
        self.assertIn("Testing", by_title)
        self.assertNotIn("Tiny", by_title)  # too short
        self.assertEqual(by_title["Release checklist"].kind, "skill")
        self.assertIn("CLAUDE.local.md", by_title["My setup"].held)
        git = by_title["Git hygiene"]
        self.assertIsNone(git.held)
        self.assertIn("[person]", git.body)
        self.assertEqual(git.topics[0], "git")
        self.assertNotIn("Untagged", by_title)
        self.assertNotIn("2026-11-01", by_title)  # Daily/ is denied even when tagged
        self.assertIn("share: false", by_title["Secret plans"].held)
        self.assertIsNone(by_title["Retrieval augmented generation"].held)
        andrej = next(i for i in result.items if i.origin.endswith("Andrej.md"))
        self.assertIn("person", andrej.held)
        self.assertEqual(andrej.title, "[person]")  # the page's own name is redacted too
        self.assertFalse(any("article text" in i.body for i in result.items))

    def test_include_folder(self):
        cfg = self.config()
        cfg.include_folders = ["Topics"]
        titles = {i.title for i in run(cfg, Plan(claude=False, repos=False, vaults=[self.vault])).items}
        self.assertIn("Untagged", titles)


class TestSanitizer(unittest.TestCase):
    def test_secrets(self):
        r = Redactor()
        for s in ["AKIA" + "ABCDEFGHIJKLMNOP", "sk-ant-" + "api03-abcdefghijklmnopqrstuvwxyz", "password = hunter2hunter2",
                  "https://bob:s3cr3tpw@example.com/x", "xoxb-" + "1234567890-abcdefghij",
                  "-----BEGIN RSA PRIVATE" + " KEY-----\nabc\n-----END RSA PRIVATE KEY-----"]:
            text, secrets, _ = r.clean_text(s)
            self.assertTrue(secrets, s)
            self.assertIn("REDACTED", text, s)
        # Hashes, digests and prose are not secrets.
        for s in ["sha256:" + "a" * 64, "the password manager holds it", "commit 3f9c2a1b4d5e6f708192a3b4c5d6e7f801234567"]:
            _, secrets, _ = r.clean_text(s)
            self.assertFalse(secrets, s)

    def test_personal(self):
        r = Redactor(terms={"Casey Example": "the user", "Casey": "the user"}, people={"Jane Smith"},
                     allow_domains=["github.com"])
        text, _, flags = r.clean_text(
            "Casey Example asked Jane Smith; see /Users/casey/x and 10.0.0.5, https://wiki.internal/a and https://github.com/x "
            "and https://unknown.example.net/p, call 555-123-4567")
        self.assertNotIn("Casey", text)
        self.assertIn("the user asked [person]", text)
        self.assertIn("~/x", text)
        self.assertIn("[ip]", text)
        self.assertIn("[internal-url]", text)
        self.assertIn("https://github.com/x", text)
        self.assertIn("url:unknown.example.net", flags)
        self.assertIn("[phone]", text)

    def test_sensitive_subject_held(self):
        it = Redactor().apply(Learning(title="Notes", body="Discussed her salary and the promotion timeline.", source_type="obsidian"))
        self.assertIn("sensitive", it.held)


class TestFrontmatter(unittest.TestCase):
    def test_roundtrip_and_nested(self):
        fm, body = frontmatter.split("---\nname: x\ntags: [a, \"b c\"]\nmetadata:\n  type: feedback\n  n: 2\nlist:\n  - one\n  - two\n---\nBody\n")
        self.assertEqual(fm["tags"], ["a", "b c"])
        self.assertEqual(fm["metadata"]["type"], "feedback")
        self.assertEqual(fm["list"], ["one", "two"])
        self.assertEqual(body, "Body\n")
        out = frontmatter.dump({"title": 'He said "hi": ok', "topics": ["a"], "flag": True})
        again, _ = frontmatter.split(out + "x")
        self.assertEqual(again["title"], 'He said "hi": ok')
        self.assertEqual(again["topics"], ["a"])
        self.assertIs(again["flag"], True)


class TestSelection(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(select.parse_selection("1,3-4", 5), {0, 2, 3})
        self.assertEqual(select.parse_selection("all", 3), {0, 1, 2})
        self.assertEqual(select.parse_selection("none", 3), set())
        self.assertIsNone(select.parse_selection("9", 3))
        self.assertIsNone(select.parse_selection("x", 3))

    def test_pick_topics_prompt(self):
        items = [Learning(title=f"t{i}", body="b", source_type="x", topics=[t]) for i, t in enumerate(["git", "git", "aws"])]
        answers = iter(["?1", "bogus", "2"])
        out = io.StringIO()
        chosen = select.pick_topics(topics.counts(items), "export", input_fn=lambda _: next(answers), out=out)
        self.assertEqual(chosen, ["aws"])
        self.assertIn("git", out.getvalue())

    def test_review_defaults(self):
        a = Learning(title="a", body="b", source_type="x")
        h = Learning(title="h", body="b", source_type="x", held="nope")
        c = Learning(title="c", body="b", source_type="x")
        answers = iter(["", "", "n"])
        kept = select.review([a, h, c], "Export", input_fn=lambda _: next(answers), out=io.StringIO())
        self.assertEqual([k.title for k in kept], ["a"])


class TestBundleAndImport(Fixture):
    def make_bundle(self) -> Path:
        result = self.scan()
        out = self.home / "out.kungfu"
        bundle.write(out, result.offered, title="Test Bundle", description="d")
        return out

    def test_bundle_has_no_local_details(self):
        out = self.make_bundle()
        with zipfile.ZipFile(out) as zf:
            blob = b"".join(zf.read(n) for n in zf.namelist()).decode()
        for leak in (str(self.home), "widgetfactory", "Jane", "jane@example.org", "ghp_", "salary", "origin", "flags"):
            self.assertNotIn(leak, blob, leak)
        b = bundle.read(out)
        self.assertEqual(b.title, "Test Bundle")
        self.assertTrue(all("id-mismatch" not in i.flags for i in b.items))

    def test_tampered_and_unsafe_bundles_rejected(self):
        out = self.make_bundle()
        bad = self.home / "bad.kungfu"
        with zipfile.ZipFile(out) as src, zipfile.ZipFile(bad, "w") as dst:
            for n in src.namelist():
                data = src.read(n)
                if n.startswith("learnings/"):
                    data += b"tampered"
                dst.writestr(n, data)
        with self.assertRaises(bundle.BundleError):
            bundle.read(bad)
        evil = self.home / "evil.kungfu"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("manifest.json", json.dumps({"format": "i-know-kung-fu", "learnings": []}))
            zf.writestr("../../escape.md", "x")
        with self.assertRaises(bundle.BundleError):
            bundle.read(evil)

    def test_obsidian_import_and_dedupe(self):
        b = bundle.read(self.make_bundle())
        target = self.home / "Recipient"
        (target / ".obsidian").mkdir(parents=True)
        r = obs_imp.load(b, b.items, target, ack=ACK)
        self.assertEqual(len(r.written), len(b.items))
        note = r.written[0].read_text()
        self.assertIn("trust: \"external\"", note)
        self.assertIn("[!info] Imported learning", note)
        index = target / "Kung Fu" / "Test Bundle" / "Test Bundle index.md"
        self.assertTrue(index.exists())
        self.assertIn("[[Kung Fu/Test Bundle/", index.read_text())
        again = obs_imp.load(b, b.items, target, ack=ACK)
        self.assertEqual(len(again.written), 0)
        self.assertEqual(len(again.skipped), len(b.items))

    def test_wiki_import(self):
        b = bundle.read(self.make_bundle())
        target = self.home / "theirwiki"
        write(target / "index.md", "# Index\n\n## Concepts\n\n- [[x]]\n")
        write(target / "log.md", "# Log\n")
        (target / "wiki").mkdir()
        r = wiki_imp.load(b, b.items, target, ack=ACK)
        self.assertTrue(all("raw/kungfu/test-bundle" in str(p) for p in r.written))
        self.assertIn("ingest | kung-fu bundle: Test Bundle", (target / "log.md").read_text())
        self.assertTrue(any("Ingest the new lessons" in n for n in r.notes))
        self.assertTrue((target / "raw/kungfu/test-bundle/_EXAMINATION.md").exists())
        r2 = wiki_imp.load(b, b.items, target, direct=True, ack=ACK)
        self.assertTrue(any("/wiki/kungfu/" in str(p) for p in r2.written))
        self.assertIn("## Imported with I Know Kung-Fu", (target / "wiki" / "index.md").read_text()
                      if (target / "wiki" / "index.md").exists() else (target / "index.md").read_text())

    def test_neutralize(self):
        body = "```dataviewjs\ndv.el('x')\n```\n<script>alert(1)</script> `$= dv.x` <% tp.file %> [a](javascript:alert(1))"
        out = neutralize(body)
        self.assertNotIn("```dataviewjs", out)
        self.assertNotIn("<script", out)
        self.assertNotIn("<%", out)
        self.assertNotIn("javascript:", out)
        self.assertNotIn("`$=", out)

    def test_cli_end_to_end(self):
        import contextlib

        os.environ["KUNGFU_CONFIG"] = str(self.home / "cfg.toml")
        write(self.home / "cfg.toml",
              f'[sources]\nclaude_dir = "{self.home / ".claude"}"\nrepo_roots = ["{self.home / "git"}"]\n'
              f'obsidian_vaults = ["{self.vault}"]\n')
        out = self.home / "cli.kungfu"
        fake, patches = with_fake_claude()
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            self.assertEqual(main(["export", "--accept-risk", "--topics", "git,aws,infrastructure-as-code", "-o", str(out),
                                   "--title", "CLI", "-y"]), 0)
            sent = "\n".join(c["messages"][0]["content"] for c in fake.calls)
            self.assertNotIn("Jane", sent)  # only scrubbed text reaches Claude
            self.assertNotIn("ghp_", sent)  # held-back learnings (a secret) are never sent
            b = bundle.read(out)
            self.assertTrue(b.lessons)
            self.assertTrue(all(i.kind == "lesson" and i.meta.get("statement") for i in b.items))
            self.assertTrue(all("**Lesson:**" in i.body and "## Why" in i.body for i in b.items))
            self.assertEqual(b.items[0].topics, ["git-hygiene"])
            target = self.home / "R"
            (target / ".obsidian").mkdir(parents=True)
            write(target / "Notes/Git notes.md", "Prune merged branches and keep git history linear.\n")
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                self.assertEqual(main(["import", "--accept-risk", str(out), "--into", str(target), "--all-topics", "-y"]), 0)
            self.assertIn("Left out 1 lesson(s) that don't apply to you:", printed.getvalue())
            self.assertIn("OpenTofu", printed.getvalue())
            written = list((target / "Kung Fu" / "CLI").glob("*.md"))
            notes = [p.read_text() for p in written if "index" not in p.name]
            self.assertTrue(notes)
            self.assertTrue(all("## Examined on import" in n and "New to your notes" in n for n in notes))
            # The OpenTofu lesson was examined as not applicable, so -y left it out.
            self.assertTrue(any("OpenTofu" in i.title for i in b.items))
            self.assertFalse(any("OpenTofu" in p.name for p in written))
            self.assertEqual(main(["import", "--accept-risk", str(out), "--into", str(target), "--topics", "nonexistent"]), 2)
        # Bundles of raw learnings (older kungfu) are refused.
        raw = self.home / "raw.kungfu"
        bundle.write(raw, self.scan().offered, title="Raw")
        with contextlib.ExitStack() as stack:
            for p in with_fake_claude()[1]:
                stack.enter_context(p)
            self.assertEqual(main(["import", "--accept-risk", str(raw), "--into", str(self.home / "R"), "--all-topics", "-y"]), 1)

    def test_residual_secret_gate(self):
        self.assertTrue(residual_secrets("token AKIA" + "ABCDEFGHIJKLMNOP"))
        self.assertFalse(residual_secrets("[REDACTED:aws-access-key]"))


class TestLessons(unittest.TestCase):
    def test_synthesize_groups_maps_sources_and_reports_refusals(self):
        from kungfu import lessons

        notes = [Learning(title=f"Note {i}", body=f"Rule {i}: do it.", source_type="claude-memory",
                          topics=["git"] if i < 4 else ["aws"]) for i in range(6)]
        fake = FakeClaude(refuse_first=True)
        written, skipped = lessons.synthesize(notes, "claude-opus-5-5", client=fake, out=io.StringIO())
        self.assertEqual(fake.calls[0]["model"], "claude-opus-5-5")
        self.assertEqual(fake.calls[0]["fallbacks"], "default")  # server-side fallback on refusals
        self.assertIn("json_schema", json.dumps(fake.calls[0]["output_config"]))
        # git (4 notes) is one group; aws (2) is pooled. The first request was refused.
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(len(skipped), 4)
        self.assertTrue(all("declined" in why for _, why in skipped))
        self.assertEqual([le.title for le in written], ["Lesson: Note 4", "Lesson: Note 5"])
        le = written[0]
        self.assertEqual(le.kind, "lesson")
        self.assertEqual(le.meta["confidence"], "medium")
        self.assertEqual(le.meta["evidence"], "1 note(s): 1 claude-memory")
        self.assertEqual(le.context, "Note 4")  # local only: shown in review, never exported

    def test_lesson_round_trips_through_a_bundle(self):
        import tempfile as tf

        from kungfu import lessons

        le = lessons.make("Pass the region", "Always pass the AWS region explicitly.", "Defaults vary by shell.",
                          ["Add --region to every call."], "Scripts and agents.", "Interactive shells you control.",
                          ["aws"], "high", [Learning(title="x", body="y", source_type="claude-md")])
        with tf.TemporaryDirectory() as d:
            path = Path(d) / "l.kungfu"
            bundle.write(path, [le], title="L", content="lessons")
            b = bundle.read(path)
        self.assertTrue(b.lessons)
        got = b.items[0]
        self.assertEqual(got.meta["statement"], "Always pass the AWS region explicitly.")
        self.assertEqual(got.meta["not_when"], "Interactive shells you control.")
        self.assertIn("## When it doesn't", got.body)
        self.assertNotIn("x", got.meta["evidence"].split(":")[0])  # evidence is counts, not titles

    def test_examine_sets_verdicts_and_related_notes(self):
        import tempfile as tf

        from kungfu import examine, lessons

        with tf.TemporaryDirectory() as d:
            root = Path(d)
            write(root / "Infra/Terraform setup.md", "We use terraform for infrastructure and pin provider versions.\n")
            write(root / "Cooking.md", "Bread needs time.\n")
            le1 = lessons.make("Use OpenTofu, not Terraform", "Prefer OpenTofu for infrastructure as code.",
                               "Licence.", ["Pin versions."], "", "", ["infrastructure-as-code"], "high", [])
            le2 = lessons.make("Prune merged branches", "Delete local branches once merged.", "Tidy.", [], "", "",
                               ["git"], "medium", [])
            notes = examine.index(root)
            rel = examine.related(le1, notes)
            self.assertEqual([n.title for n, _ in rel], ["Terraform setup"])
            examine.examine([le1, le2], notes, "claude-opus-5-5", root, client=FakeClaude(), out=io.StringIO())
        self.assertEqual(le1.meta["verdict"], "not_applicable")
        self.assertEqual(le1.meta["related"], ["Infra/Terraform setup.md"])
        self.assertEqual(le2.meta["verdict"], "new")
        sec = examine.section(le1, lambda r: f"[[{r[:-3]}]]")
        self.assertIn("Probably doesn't apply to you", sec)
        self.assertIn("[[Infra/Terraform setup]]", sec)


class TestSensitivity(unittest.TestCase):
    def test_scores_rank_risk(self):
        from kungfu import sensitivity

        r = Redactor(people={"Jane Smith"})
        clean = r.apply(Learning(title="Prune merged branches", body="After a merge, delete the local branch.",
                                 source_type="claude-md"))
        risky = r.apply(Learning(title="Notes", source_type="obsidian",
                                 body="Jane Smith (jane@acme.org) said the salary review is confidential; key AKIA" + "ABCDEFGHIJKLMNOP"))
        self.assertLess(sensitivity.score(clean), 2)
        self.assertGreater(sensitivity.score(risky), 8)
        self.assertLessEqual(sensitivity.score(risky), 10)
        self.assertGreater(sensitivity.topic_score([clean, risky]), sensitivity.score(clean))

    def test_bar_and_gradient(self):
        from kungfu import sensitivity

        self.assertEqual(sensitivity.bar(0), "·" * 10)
        self.assertEqual(sensitivity.bar(10), "█" * 10)
        self.assertEqual(sensitivity.bar(4.5), "████▌·····")
        r, g, b = sensitivity.gradient(0)
        self.assertGreater(g, r)  # green
        r, g, b = sensitivity.gradient(0.5)
        self.assertGreater(r, 200)
        self.assertGreater(g, 200)  # yellow
        r, g, b = sensitivity.gradient(1)
        self.assertGreater(r, g)  # red
        colored = sensitivity.bar(10, mode="truecolor")
        self.assertIn("\x1b[38;2;", colored)
        self.assertEqual(colored.count("█"), 10)
        self.assertIn("\x1b[38;5;", sensitivity.bar(5, mode="256"))

    def test_picker_shows_gauges(self):
        items = [Learning(title="a", body="b", source_type="claude-md", topics=["git"])]
        out = io.StringIO()
        select.pick_topics(topics.counts(items), "export", input_fn=lambda _: "1", out=out, show_sensitivity=True)
        self.assertIn("sensitivity", out.getvalue())
        self.assertIn("·", out.getvalue())


class TestDiscoverAndInit(Fixture):
    def setUp(self):
        super().setUp()
        import subprocess

        self.repo = self.home / "git" / "widgetfactory"
        env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
        self.git_env = env

        def git(*a, **kw):
            subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True, env={**env, **kw})

        git("init", "-q")
        git("remote", "add", "origin", "git@github.com:acmecorp/widgetfactory.git")
        git("remote", "add", "upstream", "git@github.com:someoneelse/widgetfactory.git")
        git("add", "-A")
        for name, email in (("Pat Tester", "pat@acme-widgets.com"), ("Bob Builder", "bob@acme-widgets.com"),
                            ("root", "root@localhost"), ("Operations", "ops@acme-widgets.com")):
            git("-c", f"user.name={name}", "-c", f"user.email={email}", "-c", "commit.gpgsign=false",
                "commit", "-q", "--allow-empty", "-m", "x")
        write(self.repo / "AGENTS.md",
              "## Working with Bob\n\nBob Builder reviews every change to the Hyperwidget service; see "
              "https://wiki.acme-widgets.com/x and ask Margaret Hamilton about TPSR approvals. TPSR TPSR TPSR.\n\n"
              "## Hyperwidget deploys\n\nHyperwidget deploys go out on Tuesdays; Hyperwidget rollbacks need two reviewers.\n")

    def findings(self):
        from unittest import mock

        from kungfu.discover import discover

        cfg = self.config()
        with mock.patch("kungfu.config.user_identities", return_value={"pat tester", "pat@acme-widgets.com"}):
            return discover(cfg, Plan(vaults=[self.vault], wikis=[self.wiki]))

    def test_discover_finds_people_orgs_projects(self):
        f = self.findings()
        terms = {cat: {c.term for c in cs} for cat, cs in f.candidates.items()}
        self.assertIn("Bob Builder", terms["person"])  # git author mentioned in text
        self.assertIn("Margaret Hamilton", terms["person"])  # given name + surname in text
        self.assertNotIn("Pat Tester", terms.get("person", set()))  # that's you
        self.assertFalse({"root", "Operations"} & terms.get("person", set()))  # accounts, not people
        self.assertIn("Jane Smith", terms["person"])  # People folder, mentioned in a memory
        self.assertGreaterEqual(f.unmentioned_auto.get("organization", 0), 1)  # acmecorp: auto, never mentioned
        self.assertNotIn("someoneelse", {t.lower() for ts in terms.values() for t in ts})  # upstream remote
        self.assertIn("widgetfactory", terms["project"])
        self.assertIn("Hyperwidget", terms["other"])  # capitalized, never lowercase: could be anything
        self.assertFalse(next(c for c in f.candidates["other"] if c.term == "Hyperwidget").suggested)
        self.assertIn("acme-widgets.com", terms["domain"])
        self.assertIn("TPSR", terms["acronym"])
        acr = next(c for c in f.candidates["acronym"] if c.term == "TPSR")
        self.assertFalse(acr.suggested)  # acronyms start unticked

    def test_curate_commands(self):
        choices = [select.Choice(x, selected=True) for x in ("a", "b", "c")]
        answers = iter(["-2", "+Zed, Yan", "?", "+2", "none", "1,3", ""])
        picked = select.curate("t", choices, add=lambda s: [select.Choice(x.strip()) for x in s.split(",")],
                               input_fn=lambda _: next(answers), out=io.StringIO())
        self.assertEqual([c.label for c in picked], ["a", "c"])
        answers = iter(["+Zed", "all", ""])
        picked = select.curate("t", [select.Choice("a", selected=False)],
                               add=lambda s: [select.Choice(s.strip())], input_fn=lambda _: next(answers),
                               out=io.StringIO())
        self.assertEqual([c.label for c in picked], ["a", "Zed"])

    def test_init_accept_all_writes_config_used_by_scan(self):
        import tomllib
        from unittest import mock

        cfg_path = self.home / "init.toml"
        os.environ["KUNGFU_CONFIG"] = str(cfg_path)
        write(cfg_path, f'[sources]\nclaude_dir = "{self.home / ".claude"}"\nrepo_roots = ["{self.home / "git"}"]\n'
                        f'obsidian_vaults = ["{self.vault}"]\n')
        from kungfu import locate as locate_mod

        real_locate = locate_mod.locate
        with mock.patch("kungfu.config.user_identities", return_value={"pat tester"}), \
             mock.patch("kungfu.config.known_vaults", return_value=[]), \
             mock.patch("kungfu.cli._claude_ready", return_value=True), \
             mock.patch("kungfu.locate.locate", lambda **kw: real_locate(roots=[self.home])), \
             mock.patch("kungfu.config.auto_redact_terms", return_value={}):
            self.assertEqual(main(["init", "--accept-risk", "--force", "--accept-all"]), 0)
            data = tomllib.loads(cfg_path.read_text())
            terms = data["redact"]["terms"]
            self.assertEqual(terms["Bob Builder"], "[person]")
            self.assertEqual(terms["Hyperwidget"], "[name]")
            self.assertEqual(terms["TPSR"], "[acronym]")  # accept-all takes even unticked suggestions
            self.assertEqual(data["redact"]["cased_terms"].get("Smith"), "[person]")  # an ordinary word too
            self.assertNotIn("Smith", terms)
            self.assertEqual(data["topics"]["exclude"], [])
            self.assertEqual(data["sources"]["llm_wikis"], [str(self.wiki)])  # found and accepted
            from kungfu import config as config_mod

            cfg = config_mod.load()
            text = "\n".join(i.body for i in run(cfg, Plan(vaults=[self.vault])).items)
            self.assertNotIn("Bob Builder", text)
            self.assertNotIn("Hyperwidget", text)

    def test_init_interactive_curation(self):
        import tomllib
        from unittest import mock

        cfg_path = self.home / "init2.toml"
        os.environ["KUNGFU_CONFIG"] = str(cfg_path)
        answers = iter([
            "I accept the risk",  # the experimental-software acknowledgement
            "",  # repo folders: the search found ~/git, ticked
            "+" + str(self.home / "nope"),  # knowledge bases: a path that doesn't exist is refused
            "",  # vault and wiki were found and start ticked
            "",  # former names? No (the default)
            "",  # vault folders: keep none ticked
            "+Dr Evil",  # people: add one
            "",  # people: accept
            "",  # organizations
            "",  # projects
            "none",  # domains: untick all
            "",
            "",  # acronyms (unticked by default)
            "",  # other capitalized names (unticked by default)
            "+fedops: saml, metadata",  # topics: add a custom topic
            "-1",  # untick the first topic
            "",
        ])
        from kungfu import locate as locate_mod

        real_locate = locate_mod.locate
        with mock.patch("kungfu.cli.interactive", return_value=True), \
             mock.patch("kungfu.config.known_vaults", return_value=[]), \
             mock.patch("kungfu.cli._claude_ready", return_value=True), \
             mock.patch("kungfu.locate.locate", lambda **kw: real_locate(roots=[self.home])), \
             mock.patch("builtins.input", lambda _="": next(answers)), \
             mock.patch("kungfu.config.user_identities", return_value={"pat tester"}), \
             mock.patch("kungfu.config.auto_redact_terms", return_value={}), \
             mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(main(["init"]), 0)
        data = tomllib.loads(cfg_path.read_text())
        terms = data["redact"]["terms"]
        self.assertEqual(terms["Dr Evil"], "[person]")
        self.assertIn("Bob Builder", terms)
        self.assertNotIn("acme-widgets.com", terms)
        self.assertNotIn("TPSR", terms)
        self.assertEqual(data["topics"]["custom"]["fedops"], ["saml", "metadata"])
        self.assertEqual(len(data["topics"]["exclude"]), 1)
        self.assertEqual(data["obsidian"]["include_folders"], [])
        self.assertEqual(data["sources"]["obsidian_vaults"], [str(self.vault)])
        self.assertEqual(data["sources"]["llm_wikis"], [str(self.wiki)])
        self.assertEqual([config_path_expand(r) for r in data["sources"]["repo_roots"]], [str(self.home / "git")])


class TestRain(unittest.TestCase):
    def test_field_renders_fading_green_glyphs(self):
        import random

        from kungfu import rain

        f = rain.Field(40, 6, rng=random.Random(1))
        for _ in range(30):
            f.step()
        lines = f.render("truecolor")
        self.assertEqual(len(lines), 6)
        joined = "".join(lines)
        self.assertIn("\x1b[38;2;", joined)
        self.assertTrue(any(ch in joined for ch in rain.KATAKANA + rain.DIGITS))
        # Colors are green-dominant and span bright to dim.
        import re

        rgbs = {tuple(map(int, m)) for m in re.findall(r"38;2;(\d+);(\d+);(\d+)m", joined)}
        self.assertTrue(all(g >= r and g >= b for r, g, b in rgbs))
        self.assertGreater(len({g for _, g, _ in rgbs}), 3)
        before = [row[:] for row in f.chars]
        f.step()
        self.assertNotEqual(before, f.chars)  # characters keep transforming
        self.assertIn("\x1b[38;5;", "".join(f.render("256")))

    def test_head_is_brightest(self):
        from kungfu import rain

        self.assertEqual(rain.color_for(1.0, "truecolor"), "\x1b[38;2;225;255;225m")
        self.assertEqual(rain.color_for(0.14, "truecolor"), "\x1b[38;2;0;60;15m")

    def test_silent_when_not_a_terminal(self):
        from kungfu import rain

        buf = io.StringIO()
        with rain.activity("work", stream=buf):
            pass
        self.assertEqual(buf.getvalue(), "")

    def test_forced_draws_then_erases(self):
        import time

        from kungfu import rain

        buf = io.StringIO()
        with rain.Rain("Scanning", stream=buf, delay=0, force=True) as r:
            r.status("halfway")
            time.sleep(0.25)
        out = buf.getvalue()
        self.assertIn("\x1b[?25l", out)  # cursor hidden while drawing
        self.assertIn("halfway", out)
        self.assertTrue(out.endswith("\r\x1b[J\x1b[?25h"))  # band cleared, cursor restored



class TestHelp(unittest.TestCase):
    def test_no_arguments_and_help_flag_show_help(self):
        import contextlib
        import subprocess

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main([]), 0)
        self.assertIn("usage: kungfu", out.getvalue())
        self.assertIn("typical use:", out.getvalue())
        launcher = Path(__file__).resolve().parent.parent / "kungfu"
        for argv in ([], ["--help"]):
            r = subprocess.run([sys.executable, str(launcher), *argv], capture_output=True, text=True,
                               env={**os.environ, "KUNGFU_NO_VENV": "1"})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("usage: kungfu", r.stdout)
            self.assertIn("import", r.stdout)



class TestPaths(Fixture):
    def test_clean_path_and_misfiled_vault(self):
        from kungfu.config import clean_path

        self.assertEqual(clean_path("/a/Mobile\\ Documents/x"), "/a/Mobile Documents/x")
        self.assertEqual(clean_path('"/a/My Vault"'), "/a/My Vault")
        # A vault listed as an LLM wiki is read as a vault, with a hint; an empty vault explains why.
        cfg = self.config()
        result = run(cfg, Plan(claude=False, repos=False, wikis=[self.vault]))
        self.assertTrue(any(i.title == "Git hygiene" for i in result.items))
        self.assertTrue(any("is an Obsidian vault, not an LLM wiki" in h for h in result.hints))
        empty = self.home / "Empty"
        (empty / ".obsidian").mkdir(parents=True)
        write(empty / "People/Ann.md", "x")
        write(empty / "Notes/plain.md", "untagged note body that is long enough to count as a note")
        result = run(cfg, Plan(claude=False, repos=False, vaults=[empty]))
        self.assertTrue(any("none are opted in" in h and "1 untagged" in h for h in result.hints))

    def test_nested_vaults_are_not_read_twice(self):
        from kungfu.sources import obsidian

        nested = self.vault / "Inner"
        (nested / ".obsidian").mkdir(parents=True)
        write(nested / "x.md", "#kungfu a note in the nested vault that is long enough")
        self.assertFalse(any("Inner" in str(p) for p in obsidian.notes(self.vault)))



def config_path_expand(p: str) -> str:
    return str(Path(p).expanduser())


class TestLocate(Fixture):
    def test_finds_repo_folders_vaults_and_wikis(self):
        from kungfu.locate import locate

        # A repo folder with several repos, a stray repo among many ordinary folders, junk to skip.
        for name in ("alpha", "beta", "gamma"):
            (self.home / "Work" / "acme-work-git" / name / ".git").mkdir(parents=True)
        docs = self.home / "Docs"
        for name in ("taxes", "recipes", "cars", "music", "photos"):
            (docs / name).mkdir(parents=True)
        (docs / "oneoff" / ".git").mkdir(parents=True)
        (self.home / "proj" / "node_modules" / "dep" / ".git").mkdir(parents=True)
        (self.home / "Thing.app" / "Contents" / ".git").mkdir(parents=True)
        found = locate(roots=[self.home])
        groups = found.repo_folders(home=self.home)
        self.assertIn(self.home / "Work" / "acme-work-git", groups)
        self.assertEqual(len(groups[self.home / "Work" / "acme-work-git"]), 3)
        self.assertIn(docs / "oneoff", groups)  # stands for itself, so Docs isn't read wholesale
        self.assertNotIn(docs, groups)
        self.assertIn(self.home / "git", groups)
        self.assertFalse(any("node_modules" in str(r) or ".app" in str(r) for r in found.repos))
        self.assertEqual(found.vaults, [self.vault])
        self.assertEqual(found.wikis, [self.wiki])



class TestChecklist(unittest.TestCase):
    def run_keys(self, choices, keys, **kw):
        from kungfu import checklist

        out = io.StringIO()
        picked = checklist.run("Pick", choices, keys=iter(keys), out=out, color=False, **kw)
        return picked, out.getvalue()

    def test_arrows_space_enter(self):
        from kungfu.checklist import Choice

        choices = [Choice(x, selected=False) for x in ("a", "b", "c", "d")]
        picked, screen = self.run_keys(choices, ["down", "space", "down", "down", "space", "up", "enter"])
        self.assertEqual([c.label for c in picked], ["b", "d"])
        self.assertIn("❯ [ ] a", screen.split("\x1b[J")[1])  # first frame: cursor on a, nothing ticked
        self.assertIn("2 ticked: b, d", screen)  # summary left behind

    def test_wraps_all_none_and_vi_keys(self):
        from kungfu.checklist import Choice

        choices = [Choice(x, selected=True) for x in ("a", "b", "c")]
        picked, _ = self.run_keys(choices, ["up", "space", "all", "all", "j", "k", "enter"])
        # up wraps to c, untick c; "all" ticks all (not all were ticked); second "all" unticks all.
        self.assertEqual(picked, [])

    def test_cancel_and_add(self):
        from kungfu import select
        from kungfu.checklist import Choice

        with self.assertRaises(select.Aborted):
            self.run_keys([Choice("a")], ["down", "quit"])
        with self.assertRaises(select.Aborted):
            self.run_keys([Choice("a")], ["esc"])
        picked, screen = self.run_keys(
            [Choice("a", selected=False)], ["add", "enter"],
            add=lambda text: [Choice(t.strip()) for t in text.split(",")], ask=lambda prompt: "Zed, a")
        # "Zed" is added and ticked; "a" already existed, so it's ticked rather than duplicated.
        self.assertEqual(sorted(c.label for c in picked), ["Zed", "a"])
        self.assertIn("(added)", screen)

    def test_details_and_scrolling(self):
        from kungfu.checklist import Choice

        choices = [Choice(f"item{i}", selected=False, more=[f"detail of {i}"]) for i in range(60)]
        picked, screen = self.run_keys(choices, ["right", "end", "space", "enter"])
        self.assertIn("detail of 0", screen)
        self.assertIn("more", screen)  # "↓ N more" / "↑ N more" scroll indicators
        self.assertEqual([c.label for c in picked], ["item59"])

    def test_falls_back_without_terminal(self):
        from kungfu import checklist

        self.assertFalse(checklist.available(stdin=io.StringIO(), stdout=io.StringIO()))



class TestChooserAndReview(unittest.TestCase):
    def test_chooser(self):
        from kungfu import checklist, select

        out = io.StringIO()
        self.assertEqual(checklist.choose("Go?", ["Yes", "No"], keys=iter(["right", "enter"]), out=out, color=False), 1)
        self.assertIn("Go?  No", out.getvalue())
        self.assertEqual(checklist.choose("Go?", ["Yes", "No"], default=1, keys=iter(["char:y"]),
                                          out=io.StringIO(), color=False), 0)
        with self.assertRaises(select.Aborted):
            checklist.choose("Go?", ["Yes", "No"], keys=iter(["esc"]), out=io.StringIO(), color=False)

    def test_review_checklist_reads_and_edits(self):
        from unittest import mock

        from kungfu import checklist, select

        a = Learning(title="Alpha", body="line one\nline two", source_type="claude-md", topics=["git"])
        h = Learning(title="Held", body="secret stuff", source_type="obsidian", topics=["git"], held="sensitive")
        keys = iter(["right", "edit", "down", "enter"])

        def fake_edit(it, rescrub):
            it.title = "Alpha edited"
            return rescrub(it)

        real_run = checklist.run
        with mock.patch.object(select, "edit_in_editor", fake_edit), \
             mock.patch.object(select.checklist, "run",
                               lambda *a_, **kw: real_run(*a_, keys=keys, out=io.StringIO(), color=False, **kw)):
            kept = select._review_checklist([a, h], "Export", Redactor().apply, local=True)
        self.assertEqual([k.title for k in kept], ["Alpha edited"])  # held one stays unticked

    def test_add_callback_output_becomes_status(self):
        from kungfu import checklist
        from kungfu.checklist import Choice

        def add(text):
            print(f"Not found: {text}")
            return []

        out = io.StringIO()
        checklist.run("Pick", [Choice("a")], add=add, ask=lambda p: "/nope", keys=iter(["add", "enter"]),
                      out=out, color=False)
        self.assertIn("Not found: /nope", out.getvalue())



class TestFormerNames(Fixture):
    def former(self):
        from kungfu.former_names import FormerNames

        f = FormerNames(path=self.home / "former-names.json")
        f.add("Jordan Example")
        f.add("old.handle@oldmail.example")
        f.add("Will Mason")
        return f

    def test_matching_and_storage(self):
        f = self.former()
        cases = {
            "Jordan Example wrote this.": "the user wrote this.",
            "Jordan's notes": "the user's notes",
            "commits by jexample and jordan.example": "commits by the user and the user",
            "mail old.handle@oldmail.example today": "mail the user today",
            "It will work.": "It will work.",  # ordinary word, lowercase: untouched
            "Ask Will about it": "Ask the user about it",  # capitalized: a name
            "an example": "an example",
        }
        for text, want in cases.items():
            self.assertEqual(f.scrub(text)[0], want, text)
        path = f.save()
        raw = path.read_text()
        for leak in ("Jordan", "jordan", "Example", "jexample", "oldmail", "Will", "Mason"):
            self.assertNotIn(leak, raw)
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        from kungfu.former_names import FormerNames

        again = FormerNames.load(path)
        self.assertEqual(again.scrub("Jordan Example")[0], "the user")
        self.assertEqual(again.entries, 3)

    def test_learning_is_scrubbed_held_and_scored_high(self):
        from kungfu import sensitivity

        it = Redactor(former=self.former()).apply(Learning(
            title="Signing", source_type="claude-memory",
            body="Old commits are signed as Jordan Example (jexample); keep the GPG key."))
        self.assertNotIn("Jordan", it.body)
        self.assertNotIn("jexample", it.body)
        self.assertIn("former-name", it.flags)
        self.assertIn("former name", it.held)
        self.assertGreaterEqual(sensitivity.score(it), 6)

    def test_context_detection_without_a_name(self):
        from kungfu.sanitize import special_categories

        for text in ("my deadname is on the old key", "she transitioned in 2019", "Started HRT",
                     "diagnosed with ADHD", "criminal record check", "immigration status"):
            self.assertTrue(special_categories(text), text)
        for text in ("the state transition table", "CSS transition on hover", "transitioning from REST to gRPC",
                     "disabled the feature flag", "diagnose the bug", "a race condition", "the old name still works",
                     "the release came out Tuesday", "use they/them pronouns for unknown people", "legal counsel"):
            self.assertFalse(special_categories(text), text)
        it = Redactor().apply(Learning(title="Keys", body="The old key still has my deadname on it.",
                                       source_type="claude-memory"))
        self.assertIn("gender history", it.held)

    def test_discover_and_repo_ownership_treat_former_names_as_you(self):
        import subprocess
        from unittest import mock

        from kungfu.discover import discover
        from kungfu.sources import instructions

        repo = self.home / "git" / "oldproject"
        env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
        subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Jordan Example", "-c", "user.email=jexample@old.example",
                        "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "x"], check=True, env=env)
        write(repo / "CLAUDE.md", "## Notes\n\nJordan Example set this repo up; run the tests before pushing anything.\n")
        f = self.former()
        instructions._AUTHORS.clear()
        self.assertIn(repo, instructions.my_repos([repo], {"nobody"}, former=f))
        cfg = self.config()
        cfg.former = f
        with mock.patch("kungfu.config.user_identities", return_value={"nobody"}), \
             mock.patch("kungfu.former_names.current", return_value=f):
            found = discover(cfg, Plan(vaults=[], wikis=[]))
        listed = {c.term.lower() for cs in found.candidates.values() for c in cs}
        self.assertFalse({"jordan example", "jexample"} & listed)

    def test_former_names_never_reach_claude(self):
        import contextlib

        f = self.former()
        f.save()
        os.environ["KUNGFU_CONFIG"] = str(self.home / "config.toml")
        write(self.home / "config.toml", f'[sources]\nclaude_dir = "{self.home / ".claude"}"\n'
                                         f'repo_roots = ["{self.home / "git"}"]\n')
        write(self.home / ".claude/projects/-x-git-widgetfactory/memory/old-key.md",
              "---\nname: old-key\ntype: feedback\n---\nCommits by Jordan Example are signed with the old key; "
              "rotate keys yearly and keep the git history clean.\n")
        fake, patches = with_fake_claude()
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            main(["export", "--accept-risk", "--all-topics", "-o", str(self.home / "x.kungfu"), "-y"])
        sent = "\n".join(c["messages"][0]["content"] for c in fake.calls)
        self.assertNotIn("Jordan", sent)
        self.assertNotIn("old key", sent)  # the learning that named a former name is held back, not sent

    def test_init_step_saves_hashed_names(self):
        from unittest import mock

        from kungfu import cli, former_names

        os.environ["KUNGFU_CONFIG"] = str(self.home / "config.toml")
        entries = iter(["Jordan Example", ""])
        with mock.patch("kungfu.select.choose", return_value=1), \
             mock.patch("sys.stdin.isatty", return_value=True), \
             mock.patch("builtins.input", lambda prompt="": next(entries)), \
             mock.patch("sys.stdout", io.StringIO()):
            cli._former_names_step()
        saved = former_names.FormerNames.load()
        self.assertEqual(saved.entries, 1)
        self.assertTrue(saved.matches("jexample"))
        self.assertNotIn("Jordan", former_names.default_path().read_text())



class TestMeanings(unittest.TestCase):
    def test_every_list_says_what_a_tick_does(self):
        from kungfu import checklist, select
        from kungfu.checklist import Choice

        out = io.StringIO()
        picked = checklist.run("People: which should be scrubbed from exports?",
                               [Choice("Grace Hopper", selected=True), Choice("Ada Lovelace", selected=False)],
                               keys=iter(["space", "enter"]), out=out, color=False, meaning=select.scrub("[person]"))
        frames = out.getvalue().split("\x1b[J")
        first, after_toggle, summary = frames[1], frames[2], frames[-1]
        self.assertIn("[x] = scrubbed: replaced with [person] in everything exported", first)
        self.assertIn("[ ] = left as written (never scrubbed)", first)
        self.assertIn("[x] Grace Hopper  → [person]", first)
        self.assertIn("[ ] Ada Lovelace  left as written", first)
        self.assertIn("1 scrubbed · 1 left as written", first)
        self.assertIn("[ ] Grace Hopper  left as written", after_toggle)  # the tag flips with the tick
        self.assertIn("0 scrubbed · 2 left as written", summary)
        self.assertEqual(picked, [])

    def test_typed_fallback_shows_the_same_meaning(self):
        from kungfu import select
        from kungfu.checklist import Choice

        out = io.StringIO()
        select.curate("Acronyms: which should be scrubbed from exports?", [Choice("TPSR", selected=False)],
                      input_fn=lambda _: "", out=out, meaning=select.scrub("[acronym]"))
        text = out.getvalue()
        self.assertIn("[x] = scrubbed: replaced with [acronym]", text)
        self.assertIn("[ ]   1. TPSR  left as written", text)

    def test_all_meanings_are_distinct_and_complete(self):
        from kungfu import select

        for name in ("READ_REPOS", "READ_KB", "VAULT_FOLDER", "OFFER_TOPIC", "EXPORT_TOPIC", "IMPORT_TOPIC",
                     "EXPORT_ITEM", "IMPORT_ITEM"):
            m = getattr(select, name)
            self.assertTrue(all([m.on, m.off, m.on_tag, m.off_tag, m.on_count, m.off_count]), name)
            self.assertNotEqual(m.on_tag, m.off_tag, name)



class TestNamesAreNotProjects(Fixture):
    def test_first_and_last_names_go_to_people(self):
        from unittest import mock

        from kungfu.discover import discover

        write(self.home / ".claude/projects/-x-git-widgetfactory/memory/review-flow.md",
              "---\nname: review-flow\ntype: feedback\n---\n"
              "Ask Jane before merging; Smith owns the release checklist. Anjali said the Zorblat deploy is "
              "flaky, so Bethany reruns it. Thanks Nkechi for the fix. Zorblat Zorblat Zorblat. "
              "The DataForge sync and DataForge reports run on Monday and Monday again on Monday.\n")
        with mock.patch("kungfu.config.user_identities", return_value={"nobody"}):
            f = discover(self.config(), Plan(vaults=[self.vault], wikis=[self.wiki]))
        people = {c.term for c in f.candidates.get("person", [])}
        projects = {c.term for c in f.candidates.get("project", [])}
        other = {c.term for c in f.candidates.get("other", [])}
        self.assertTrue({"Jane", "Smith"} <= people)  # parts of a People-folder name, used alone
        self.assertTrue({"Anjali", "Bethany"} <= people)  # common given names (bundled list)
        self.assertIn("Nkechi", people)  # used like a name ("Thanks Nkechi")
        self.assertFalse({"Jane", "Smith", "Anjali", "Bethany", "Nkechi"} & (projects | other))
        self.assertIn("DataForge", projects)
        self.assertFalse(next(c for c in f.candidates["project"] if c.term == "DataForge").suggested)
        self.assertIn("Zorblat", other)  # can't tell what it is: offered, unticked
        self.assertNotIn("Monday", projects | other)
        smith = next(c for c in f.candidates["person"] if c.term == "Smith")
        self.assertTrue(smith.cased)  # also an ordinary word: scrub only when capitalized

    def test_cased_terms_only_match_capitalized(self):
        r = Redactor(cased_terms={"Smith": "[person]", "Young": "[person]", "Rank": "[person]"})
        text, _, _ = r.clean_text("Ask Smith; the smith forged a young blade. Ask Young.")
        self.assertEqual(text, "Ask [person]; the smith forged a young blade. Ask [person].")
        # At the start of a sentence or list item, before a lowercase word, it's read as the word.
        for kept in ("- Rank unmigrated repos by activity.", "1. Rank them.", "- **Rank** them by date",
                     "Done. Rank them later."):
            self.assertEqual(r.clean_text(kept)[0], kept, kept)
        for scrubbed in ("Rank's draft is due.", "See (Rank et al.)", "Thanks, Rank."):
            self.assertIn("[person]", r.clean_text(scrubbed)[0], scrubbed)



class TestIdentifiers(unittest.TestCase):
    POSITIVE = {
        "Her SSN is 123-45-6789 for the form.": "Her SSN is [ssn] for the form.",
        "employee 123-45-6789 started": "employee [ssn] started",
        "ITIN 912-78-1234": "ITIN [tax-id]",
        "Card 4111 1111 1111 1111 exp 09/27 cvv 123": "Card [card] exp [expiry] cvv [cvv]",
        "card 4012888888881881 on file": "card [card] on file",
        "IBAN GB82 WEST 1234 5698 7654 32": "IBAN [iban]",
        "Routing 021000021 account number 1234567890123": "Routing [routing-number] account number [account-number]",
        "Driver's license D1234567 (CA), expires 2028.": "Driver's license [license-number] (CA), expires 2028.",
        "DL# S530-4628-1234-56": "DL# [license-number]",
        "Passport no. 563821947 issued 2019.": "Passport no. [passport-number] issued 2019.",
        "EIN 12-3456789": "EIN [tax-id]",
        "member ID ABC123456": "member ID [id-number]",
        "DOB: 03/14/1985": "DOB: [birthdate]",
        "born March 14, 1985 in Ohio": "born [birthdate] in Ohio",
        "birthday is 1985-03-14": "birthday is [birthdate]",
        "born in 1985": "born in [birth year]",
        "1985-03-14 (DOB)": "[birthdate] (DOB)",
        # UK
        "NI number AB 12 34 56 C": "NI number [ni-number]",
        "his NINO is AB123456C": "his NINO is [ni-number]",
        "licence MORGA753116SM9IJ": "licence [license-number]",
        "NHS number 943 476 5919": "NHS number [nhs-number]",
        "UTR 12345 67890": "UTR [tax-id]",
        # Canada
        "SIN 046 454 286": "SIN [sin]",
        "her number is 130-692-544 on the T4": "her number is [sin] on the T4",
        "OHIP 1234-567-890-AB": "OHIP [health-card]",
        # organizational, not personal
        "aws account 123456789012": "aws account [aws-account-id]",
        "arn:aws:iam::123456789012:role/x": "arn:aws:iam::[aws-account-id]:role/x",
    }
    NEGATIVE = [
        "Dev torn down 2026-11-10; release 03/14/2025.", "port 8080 and build 1732662000000", "commit 3f9c2a1",
        "version 1.2.3.4", "call 555-123-4567", "the 2019-2021 period", "Pass --region eu-west-3",
        "uuid 123e4567-e89b-12d3-a456-426614174000", "ticket JIRA-12345", "id 000-12-3456 (never issued)",
        "build 4111111111111112 (fails Luhn)", "born to run", "account for 1234 items", "NHS number 943 476 5918",
        "his NINO is QQ123456C (Q is never issued)", "routing 123456789 (bad checksum)", "see RFC 8259",
    ]

    def test_positive_and_negative(self):
        from kungfu.identifiers import scrub

        for text, want in self.POSITIVE.items():
            self.assertEqual(scrub(text)[0], want, text)
        for text in self.NEGATIVE:
            out, personal, org = scrub(text)
            self.assertEqual((out, personal, org), (text, [], []), text)

    def test_learning_is_held_scored_and_blocked(self):
        from kungfu import sensitivity
        from kungfu.sanitize import residual_secrets

        it = Redactor().apply(Learning(title="Onboarding", source_type="obsidian",
                                       body="New hire paperwork: SSN 123-45-6789, DOB 03/14/1985."))
        self.assertNotIn("6789", it.body)
        self.assertNotIn("1985", it.body)
        self.assertIn("personal identifier", it.held)
        self.assertIn("id:ssn", it.flags)
        self.assertIn("id:birthdate", it.flags)
        self.assertGreaterEqual(sensitivity.score(it), 6)
        # Typed back in after review: the pack gate catches it.
        self.assertTrue(residual_secrets("oops 123-45-6789"))
        self.assertTrue(residual_secrets("card 4111 1111 1111 1111"))
        self.assertFalse(residual_secrets("released 2026-11-10"))

    def test_aws_account_is_scrubbed_but_not_held(self):
        it = Redactor().apply(Learning(title="Deploys", source_type="claude-md",
                                       body="Deploy to aws account 123456789012 with the ci role, then verify."))
        self.assertIn("[aws-account-id]", it.body)
        self.assertIsNone(it.held)



class TestSecretsContactsLocations(unittest.TestCase):
    SECRETS = [
        "the password is hunter2", "pw: Tr0ub4dor&3", "wifi password: BlueHorse77", "login admin / admin123",
        "PIN 4821 for the door", "shared secret: s3cr3t-radius-key", "RADIUS secret testing123",
        "passphrase: correct horse battery staple", "DB_PASSWORD=Sup3rS3cret!", "export API_TOKEN=abcd1234efgh5678",
        'client_secret: "9f8e7d6c5b4a"', "Authorization: Basic dXNlcjpwYXNzd29yZA==",
        "x-api-key: 3a7bd3e2360a3d29eea436fcfb7e44c735d117c4", "apiToken: 'f00dfeedbeef1234'", "dbPassword: Hunter22!",
        "aws_secret_access_key = wJalrX" + "UtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "glpat-" + "xxxxxxxxxxxxxxxxxxxx",
        "SG." + "ngeVfQFYQlKU0ufo8x5d1A.TwL2iGABf9DHoTf-09kqeF8tAmbihYzrnopKc-1s5cr", "hvs." + "CAESIJ1dbZ3Hx1234567890abcdefghij",
        "dop_v1_" + "1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
        "https://hooks.slack.com/services/" + "T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX",
        "https://api.example.com/v1?token=abc123def456ghi789", "recovery codes: 8f3k-29dk, 1kd9-xk2m",
        "password = letmein", "password: 123456", "credentials: root:Toor!234",
    ]
    NOT_SECRETS = [
        "use a password manager", "the password reset flow", "password: required", "password: ${{ secrets.DB_PASSWORD }}",
        "password = os.environ['PW']", "password_file=/run/secrets/db", "password_min_length: 12", "max_tokens: 16000",
        "token bucket rate limiter", "the secret sauce", "store it in Secrets Manager", "token_count: 4000",
        "Pin the tool version in .tool-versions", "pin 1.9.0", "Pass --region eu-west-3", "the tests should pass",
        "login with SSO", "auth: oauth2", "api_key_header: X-API-Key", "token: <your token here>", "secret: !vault |",
        "the token is valid for an hour", "credentials: see 1Password", "session_timeout: 3600", "secret_name: prod/db",
        "API_KEY=", "Bearer tokens expire", "compass: north", "bypass: true", "apiTokenTtl: 3600", "passwordHint: blue",
        # Phrasings once mistaken for secrets:
        "SSH goes through the 1Password agent.", "error: 1Password: failed to fill whole buffer",
        "more fully than the 3/14 pass: **mostly the vendors", "e.g. credential-compromised/revoked",
        "OpenID for Verifiable Credential Issuance / Verifiable Presentations",
    ]
    PHONES_EMAILS = [
        "call 555-123-4567", "+1 (555) 123-4567", "+44 20 7946 0958", "07700 900123", "+61 2 9374 4000",
        "+33 1 23 45 67 89", "(020) 7946 0958", "tel:+15551234567", "mobile: 030 1234567", "phone 0412 345 678",
        "+15551234567", "jane.doe@example.org", "mailto:jane@example.org", "jane at example dot com",
        "jane[at]example[dot]com", "jane (at) example.org",
    ]
    NOT_PHONES_EMAILS = [
        "2026-11-27T10:00:00+00:00", "diff +5 -3", "+1 to that", "localhost:8080", "version 1.9.0", "look at example.com",
        "deployed at api.example.com", "port 05432", "+15% faster", "call the API at 10:30", "phone home feature",
        "build +20261127.1", "+3.5 points", "monthly call (2026-07-14)", "Peer signal (2026-08-01):",
    ]
    LOCATIONS = {
        "lives at 1234 Elm Street, Springfield, IL 62704": "lives at [address]",
        "PO Box 123": "[address]", "postcode SW1A 1AA": "postcode [postcode]", "zip 62704-1234": "zip [postcode]",
        "postal code K1A 0B1": "postal code [postcode]", "GPS 40.7128, -74.0060 (home)": "GPS [location] (home)",
        "40.7128° N, 74.0060° W": "[location]", "MAC 00:1A:2B:3C:4D:5E": "MAC [mac-address]",
        "IMEI 490154203237518": "IMEI [device-id]", "VIN 1HGCM82633A004352": "VIN [vin]", "plate ABC-1234": "plate [plate]",
        "serial number C02XG2JHJG5H": "serial number [device-id]",
    }
    NOT_LOCATIONS = [
        "2 Factor Auth", "3 Main Points", "p = 0.0412, 0.0381", "latency 120.5, 98.2 ms", "Apple M1 8GB", "an M2 16GB laptop",
        "12:34:56", "fe80::1", "2001:db8::1", "de:ad:be:ef", "ABCDEFGHJKLMNPRST", "build 2026 Street View",
        "1.2345, 2.3456 ratio", "00:00:00:00:00:00", "boiler plate 2", "the 3 Way handshake",
    ]

    def test_secrets(self):
        r = Redactor()
        for t in self.SECRETS:
            self.assertNotEqual(r.clean_text(t)[0], t, t)
        for t in self.NOT_SECRETS:
            self.assertEqual(r.clean_text(t)[0], t, t)
        self.assertEqual(r.clean_text("credentials: root:Toor!234")[0], "credentials: root:[REDACTED:secret]")

    def test_phones_and_emails(self):
        r = Redactor()
        for t in self.PHONES_EMAILS:
            out = r.clean_text(t)[0]
            self.assertTrue("[phone]" in out or "[email]" in out, t)
        for t in self.NOT_PHONES_EMAILS:
            self.assertEqual(r.clean_text(t)[0], t, t)

    def test_locations_and_devices(self):
        from kungfu.identifiers import scrub

        for t, want in self.LOCATIONS.items():
            self.assertEqual(scrub(t)[0], want, t)
        for t in self.NOT_LOCATIONS:
            self.assertEqual(scrub(t)[0], t, t)
        it = Redactor().apply(Learning(title="x", body="Ship it to 1234 Elm Street, Springfield, IL 62704.",
                                       source_type="obsidian"))
        self.assertIn("personal identifier (address)", it.held)
        it = Redactor().apply(Learning(title="x", body="The switch port for MAC 00:1A:2B:3C:4D:5E is 12.",
                                       source_type="claude-md"))
        self.assertIsNone(it.held)  # a device address is replaced but doesn't hold a learning back



class TestKeysAndRadius(unittest.TestCase):
    SECRETS = [
        "-----BEGIN OPENSSH PRIVATE" + " KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmU\n-----END OPENSSH PRIVATE KEY-----",
        "-----BEGIN PGP PRIVATE" + " KEY BLOCK-----\n\nlQOYBF0123456789abcdef\n=abcd\n-----END PGP PRIVATE KEY BLOCK-----",
        "PuTTY-User-Key-File-3: ssh-ed25519\nEncryption: none\nPrivate-Lines: 1\nAAAAIGq8T0Ab\nPrivate-MAC: 5c1e3d2b",
        "-----BEGIN PGP MESSAGE-----\n\nhQEMA1234567890ABCDEF\n-----END PGP MESSAGE-----",
        "ssh-keygen -t ed25519 -N 'my key passphrase' -f ~/.ssh/id_ed25519", "sshpass -p 'S3cretPw' ssh admin@host",
        "gpg --batch --passphrase 'Gpg-Pass-2024' --decrypt secrets.gpg",
        "RADIUS secret testing123", "secret = testing123", "client nro { host x; type tls; secret mysharedsecret }",
        "    secret  radsecSecret9", "radius-server key 7 0822455D0A16", "radius server ISE\n address ipv4 10.1.1.1\n key MyR4diusKey",
        "tacacs-server key S3cr3tTac", 'authentication-order radius; secret "$9$abc123DEF";',
        "auth_server_shared_secret=hostapdSecret1", "wpa_passphrase=WifiPass2024", 'psk="HomeWifi1234"',
        "PGPASSWORD=pgpass123 psql -h db", "mysql -u root -pSup3rSecret mydb", "username admin password 7 0822455D0A16",
        "openssl rsa -in k.pem -passin pass:Hunter22", "curl -u admin:Adm1nPw https://x",
        "root:$6$saltsalt$abcdefghijklmnopqrstuvwxyz0123456789./ABCD:19000:0:99999:7:::",
    ]
    PUBLIC = {
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl": "ssh-ed25519 [ssh-public-key]",
        "fingerprint SHA256:47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU": "fingerprint [key-fingerprint]",
        "git config user.signingkey 3AA5C34371567BD2": "git config user.signingkey [key-fingerprint]",
        "key fingerprint 7C8E 4A2F 9B1D 3E6C 0A5B  8F2D 1E9C 4B7A 6D3F 2C1E": "key fingerprint [key-fingerprint]",
        "43:51:43:a1:b5:fc:8b:b7:0a:3a:a9:b1:0f:66:73:a8 (MD5)": "[key-fingerprint] (MD5)",
        "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nmQINBF01\n-----END PGP PUBLIC KEY BLOCK-----": "[pgp-public-key]",
    }
    LEFT_ALONE = [
        "ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519", "docker login --password-stdin",
        "gh auth login --token $GITHUB_TOKEN", "configure the radius server for eduroam", "key /etc/ssl/private/key.pem",
        "secret: ${{ secrets.X }}", "commit 3f9c2a1b4d5e6f708192a3b4c5d6e7f801234567", "the secret key concept",
        "use radsec instead of udp", "   password   %{env:RADIUS_SECRET}", "set the psk length to 63", "mysql -u root -p mydb",
        "the signing key rotates yearly", "secret sauce", "Pass the --passphrase flag via stdin", "fingerprint the device",
    ]

    def test_private_keys_and_shared_secrets(self):
        r = Redactor()
        for t in self.SECRETS:
            out, secrets, _ = r.clean_text(t)
            self.assertNotEqual(out, t, t)
            self.assertTrue(secrets, t)
        for t in self.LEFT_ALONE:
            self.assertEqual(r.clean_text(t)[0], t, t)

    def test_public_keys_are_replaced_but_not_called_secrets(self):
        r = Redactor()
        for t, want in self.PUBLIC.items():
            out, secrets, flags = r.clean_text(t)
            self.assertEqual(out, want, t)
            self.assertEqual(secrets, [], t)
        it = r.apply(Learning(title="x", source_type="claude-md",
                              body="-----BEGIN PGP PUBLIC KEY BLOCK-----\nmQINBF01\n-----END PGP PUBLIC KEY BLOCK-----"))
        self.assertIn("personal identifier", it.held)  # it carries names and email addresses



class TestTagsAndLinks(Fixture):
    def test_tags_naming_scrubbed_terms_do_not_become_topics(self):
        write(self.vault / "Topics/Merger.md", "---\ntags: [kungfu, acme-merger, jane-smith-review, git]\n---\n"
                                                "Always snapshot before a cutover; restore drills catch broken backups early.\n")
        cfg = self.config()  # scrubs "Acme"; Jane Smith is in the People folder
        item = next(i for i in run(cfg, Plan(claude=False, repos=False, vaults=[self.vault])).items
                    if "cutover" in i.body)
        self.assertNotIn("acme-merger", item.topics)
        self.assertNotIn("jane-smith-review", item.topics)
        self.assertIn("git", item.topics)

    def test_links_and_embeds(self):
        out = Redactor().clean_text("See ![[Applicants 2026/Jane resume.pdf]], ![diagram](./img/a.png), "
                                    "[the spec](../Private/spec.md), [docs](https://example.com/x) and "
                                    "[[Projects/Runbook|the runbook]] and [[Org Chart]].")[0]
        self.assertEqual(out, "See [attachment], [attachment], the spec, [docs](https://example.com/x) and "
                              "the runbook and Org Chart.")



@contextlib.contextmanager
def fake_blocklist():
    """Swap the real blocked-word list for one of made-up words, so no test needs a real one."""
    from kungfu import wordlist

    def clear():
        wordlist._table.cache_clear()
        wordlist._word_hits.cache_clear()

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "blocked_words.txt"
        path.write_text("# made-up words\n", encoding="utf-8")
        with mock.patch.object(wordlist, "PATH", path):
            clear()
            try:
                wordlist.add("profanity", "prefix", ["zorp", "gl*b"])
                wordlist.add("profanity", "exact", ["blim", "frob's", "flub off"])
                wordlist.add("slur", "exact", ["quazzle", "quazzles"])
                yield path
            finally:
                clear()


class TestContentChecks(unittest.TestCase):
    HELD = {
        "profanity": "This deploy script is a zorpshow, honestly.",
        "profanity-censored": "What the gl#b happened to the build?",
        "slur": "He called them a quazzle in the meeting.",
        "judgment": "[person] is completely clueless about SAML.",
        "judgment-org": "the vendor is useless at support tickets",
        "judgment-team": "what an incompetent team",
        "quote-colleague": 'Bob wrote in Slack: "we should cancel the contract before renewal"',
        "quote-italic": 'Per [person]: *"I will be out until Friday, please hold the release."*',
        "venting": "The user was really frustrated that the build broke again.",
        "gossip": "Rumor is the CTO is about to quit.",
        "gossip-person": "[person] is interviewing elsewhere.",
        "marking": "TLP:AMBER — share only with the team.",
        "security": "Our prod IdP is still unpatched for CVE-2024-12345.",
        "security-finding": "Finding R4 from the pen test: the admin UI is exposed to the internet on our staging host.",
        "money": "We pay $48,000 a year for the license.",
        "deal": "We're negotiating the contract renewal with the organization.",
        "unreleased": "This is an unannounced feature.",
        "security-question": "Her mother's maiden name is on the form.",
        "whereabouts": "I'm out of the office until December 3.",
        "family": "My daughter starts school Monday.",
        "others": "[person]'s divorce is affecting the schedule.",
        "copyright": "Copyright 2024 Acme. All rights reserved.",
        "language": "Toujours vérifier la région avant de lancer la commande. La configuration par défaut ne suffit pas "
                    "dans les scripts automatisés, il faut toujours préciser la région explicitement.",
    }
    NOT_HELD = [
        "This approach is terrible for large repos; use streaming instead.", "It's useless without auth.",
        "Latency is awful under load.", "The process is leaving zombie processes behind.",
        "Walk the tree and visit their children and their parents.", "We retry lock acquisition three times.",
        "The API contract is versioned.", "Use quoted strings in YAML.", "Packets traveling to the server are dropped.",
        "Unpatched systems are a common risk; patch promptly.", "TLP:GREEN guidance can be shared.",
        "The connection pool is exhausted under load.", "Don't use security questions for account recovery.",
        "They are idempotent, so rerun them.", "Always pass --region eu-west-3 explicitly.",
        "The disaster recovery plan is tested yearly.", "1Password SSH signing fails while the user is away.",
        "the user is away from the keyboard, so ask and retry",
    ]

    def test_held_categories(self):
        from kungfu.content import review

        with fake_blocklist():
            for name, text in self.HELD.items():
                self.assertTrue(review(text), name)
            for text in self.NOT_HELD:
                self.assertEqual(review(text), [], text)
        for text in self.NOT_HELD:  # and no false alarms from the real list
            self.assertEqual(review(text), [], text)

    def test_blocked_word_matching(self):
        from kungfu import wordlist

        with fake_blocklist() as path:
            self.assertNotIn("zorp", path.read_text(encoding="utf-8"))
            for text in ("zorp", "ZORPING it", "a zorped build", "blim!", "(Blim)", "frob’s fault", "just flub off",
                         "gl*b", "GL##B", "gl@bbing", "**gl*b**", "ｚｏｒｐ"):
                self.assertEqual(wordlist.categories(text), {"profanity"}, text)
            self.assertEqual(wordlist.categories("He called them quazzles."), {"slur"})
            self.assertEqual(wordlist.categories("zorp quazzle"), {"profanity", "slur"})
            for clean in ("zor", "unzorp", "_zorp", "blimp", "blims", "frob", "flub", "off flub", "flub-off it",
                          "quazz", "g*lb", "**bold** and #include", "glb"):
                self.assertEqual(wordlist.categories(clean), set(), clean)

    def test_wordlist_add(self):
        from kungfu import wordlist

        with fake_blocklist() as path:
            before = path.read_text(encoding="utf-8")
            self.assertEqual(wordlist.add("profanity", "prefix", ["Zorp", "zorp", "", "zonk"]), 1)  # only zonk is new
            self.assertNotIn("zonk", path.read_text(encoding="utf-8"))
            self.assertTrue(path.read_text(encoding="utf-8").startswith(before))
            self.assertEqual(wordlist.categories("so zonky"), {"profanity"})
            for kind, bad in (("prefix", "two words"), ("prefix", "zo"), ("exact", "three whole words")):
                with self.assertRaises(ValueError):
                    wordlist.add("profanity", kind, [bad])

    def test_real_list_holds_only_hashes(self):
        from collections import Counter

        from kungfu import wordlist

        lines = [l for l in wordlist.PATH.read_text(encoding="utf-8").splitlines() if not l.startswith("#")]
        for line in lines:
            self.assertRegex(line, r"^(?:profanity|slur) (?:exact|prefix) [0-9a-f]{32}$")
        self.assertEqual(len({l.split()[2] for l in lines}), len(lines))
        # Guards against the list being truncated; update these when you add words.
        self.assertEqual(Counter(tuple(l.split()[:2]) for l in lines),
                         {("profanity", "prefix"): 15, ("profanity", "exact"): 12, ("slur", "exact"): 45})

    def test_no_blocked_words_in_repo(self):
        from kungfu import wordlist

        root = Path(__file__).resolve().parent.parent
        files = [p for p in root.rglob("*") if p.is_file() and not {".git", "__pycache__"} & set(p.parts)
                 and p.suffix not in {".pyc", ".png", ".jpg"}]
        self.assertTrue(files)
        for p in files:
            try:
                text = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            self.assertEqual(wordlist.categories(text), set(), p.relative_to(root))

    @unittest.skipUnless(os.environ.get("KUNGFU_BLOCKED_WORDS_FIXTURE"), "set KUNGFU_BLOCKED_WORDS_FIXTURE to check "
                         "the real list against real words")
    def test_real_list_against_private_fixture(self):
        """The fixture lives outside the repo. Each line is "<profanity|slur|clean> <text>", for example
        "profanity <a sentence with a swear word>"; blank lines and lines starting with # are skipped."""
        from kungfu import wordlist

        lines = Path(os.environ["KUNGFU_BLOCKED_WORDS_FIXTURE"]).expanduser().read_text(encoding="utf-8").splitlines()
        cases = [l.split(None, 1) for l in lines if l.strip() and not l.startswith("#")]
        self.assertTrue(cases)
        for n, (want, text) in enumerate(cases, 1):
            # The message names only the line number, so a failure doesn't print the word.
            self.assertEqual(wordlist.categories(text), set() if want == "clean" else {want}, f"fixture case {n}")

    def test_own_quotes_in_ai_notes_are_not_held(self):
        from kungfu.content import review

        own = 'Always merge to main. the user, 2026-11-19: *"Have it do the Slack searches in a subagent."*'
        self.assertEqual(review(own, ai_notes=True), [])
        self.assertTrue(review(own, ai_notes=False))  # the same text in a vault note is someone's words
        self.assertTrue(review('Note: [person] said: "the vendor is clueless and we should switch"', ai_notes=True))

    def test_learning_is_held_with_reason_and_kept_local(self):
        from kungfu import sensitivity

        it = Redactor().apply(Learning(title="Vendor", source_type="obsidian",
                                       body="We pay $48,000 a year and the vendor is clueless."))
        self.assertIn("held back:", it.held)
        self.assertIn("business details", it.held)
        self.assertIn("negative judgment", it.held)
        self.assertGreaterEqual(sensitivity.score(it), 5)

    def test_internal_links_and_more_secrets(self):
        r = Redactor()
        cases = {
            "https://acme.slack.com/archives/C024BE91L/p1700000000": "[slack-link]",
            "https://docs.google.com/document/d/1AbCdEf/edit": "[google-doc-link]",
            "https://acme.zoom.us/j/91234567890?pwd=abc": "[meeting-link]",
            "https://acme.atlassian.net/browse/OPS-123": "[jira-link]",
            "s3://acme-prod-backups/db/2026/": "s3://[bucket]/db/2026/",
            "Meeting ID: 912 3456 7890": "Meeting ID: [meeting-id]",
        }
        for text, want in cases.items():
            self.assertEqual(r.clean_text(text)[0], want, text)
        for text in ("$ANSIBLE_VAULT;1.1;AES256\n6162636465666768\n", "idp.authn.LDAP.bindDNCredential = myLdapPass",
                     "keytool -importkeystore -srcstorepass changeit2", '{"auth":"dXNlcjpwYXNzd29yZDEyMw=="}',
                     "client-key-data: LS0tLS1CRUdJTiBSU0EgUFJJVkFURSBLRVktLS0tLQo=",
                     "product key: 6YTKB-7PR2Q-RN8HK-3WHFT-4XQ2M", "license key ABCD-1234-EFGH-5678",
                     "123456-234567-345678-456789-567890-678901-789012-890123", "recovery key: AB12-CD34-EF56-GH78-IJ90-KL12",
                     "5HueCGU8rMjxEXxiPuD5BDku4MkFqeZyd4dZ1jvhTVqvbTLvyTJ", "idp.sealer.storePassword = Sealer2024!"):
            out, secrets, _ = r.clean_text(text)
            self.assertTrue(secrets, text)
        for text in ("en.wikipedia.org/wiki/SAML", "https://github.com/org/repo/wiki/Home",
                     "https://slack.com/help/articles/123", "uuid 123e4567-e89b-12d3-a456-426614174000",
                     "keytool -list -keystore ks.jks", "the license key is in 1Password", "AWS-REGION-NAME"):
            self.assertEqual(r.clean_text(text)[0], text, text)



class TestNotPeople(Fixture):
    def test_contractions_acronyms_broadcasts_and_domains(self):
        from unittest import mock

        from kungfu.discover import discover

        write(self.vault / "People/Sean O'Brien.md", "Platform lead.\n")
        write(self.home / ".claude/projects/-x-git-widgetfactory/memory/relay.md",
              "---\nname: relay\ntype: feedback\n---\n"
              "From the relay: *\"I've asked Marcus to rerun it.\"* Per IETF the policy changed. @channel please "
              "review; ping @zquarg too. Any @acme-widgets.edu address works. O'Brien owns the rollout and "
              "O'Brien signs off. Related: Grace Pellworth and Frank Tamblin (external). Will Docker work?\n")
        with mock.patch("kungfu.config.user_identities", return_value={"nobody"}):
            f = discover(self.config(), Plan(vaults=[self.vault], wikis=[self.wiki]))
        people = {c.term for c in f.candidates.get("person", [])}
        for wrong in ("I've", "Ietf", "IETF", "@channel", "@acme-widgets.edu"):
            self.assertNotIn(wrong, people)
        self.assertIn("@zquarg", people)
        self.assertIn("O'Brien", people)  # spelled as written, not "O'brien"
        self.assertTrue({"Grace Pellworth", "Frank Tamblin"} <= people)  # first names that are also words
        self.assertNotIn("Will Docker", people)
        self.assertIn("acme-widgets.edu", {c.term for c in f.candidates.get("domain", [])})



class TestInlineTags(unittest.TestCase):
    def test_link_anchors_are_not_tags(self):
        from kungfu.sources.obsidian import tags_of

        body = ("#kungfu notes\n- [Overview](#overview)\n- see page#section and &#123;\n"
                "Tagged #saml and #identity/federation here.\n```\n#not-a-tag in code\n```\n")
        self.assertEqual(tags_of({}, body), ["kungfu", "saml", "identity/federation"])



class TestStaleAndNoisyTerms(Fixture):
    def test_init_unticks_word_like_people_from_an_old_config(self):
        import tomllib
        from unittest import mock

        cfg_path = self.home / "stale.toml"
        os.environ["KUNGFU_CONFIG"] = str(cfg_path)
        write(cfg_path, f'[sources]\nclaude_dir = "{self.home / ".claude"}"\nrepo_roots = ["{self.home / "git"}"]\n'
                        '[redact.terms]\n"Metadata" = "[person]"\n"Org" = "[person]"\n"Zebulon Quarg" = "[person]"\n'
                        '[redact.cased_terms]\n"Smith" = "[person]"\n')
        answers = iter(["", "", "", "", "", "", "", "", "", "", "", ""])
        with mock.patch("kungfu.cli.interactive", return_value=True), \
             mock.patch("kungfu.cli._claude_ready", return_value=True), \
             mock.patch("kungfu.config.known_vaults", return_value=[]), \
             mock.patch("kungfu.locate.locate", lambda **kw: __import__("kungfu.locate").locate.Found()), \
             mock.patch("builtins.input", lambda _="": next(answers)), \
             mock.patch("kungfu.config.user_identities", return_value={"nobody"}), \
             mock.patch("kungfu.config.auto_redact_terms", return_value={}), \
             mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(main(["init", "--accept-risk", "--force"]), 0)
        data = tomllib.loads(cfg_path.read_text())
        self.assertNotIn("Metadata", data["redact"]["terms"])
        self.assertNotIn("Org", data["redact"]["terms"])
        self.assertIn("Zebulon Quarg", data["redact"]["terms"])  # a real-looking name stays
        self.assertIn("Smith", data["redact"]["cased_terms"])  # capitalized-only entries are kept

    def test_export_warns_about_noisy_terms(self):
        from contextlib import redirect_stdout

        from kungfu.cli import _warn_noisy_terms
        from collections import Counter

        r = Redactor(terms={"Metadata": "[person]", "Zebulon Quarg": "[person]"})
        hits = r.term_hits("SAML metadata, metadata aggregates, metadata files. Ask Zebulon Quarg.")
        out = io.StringIO()
        with redirect_stdout(out):
            _warn_noisy_terms(Counter(hits))
        self.assertIn("“Metadata” → [person]  ×3", out.getvalue())
        self.assertIn("looks like an ordinary word", out.getvalue())
        self.assertNotIn("Zebulon", out.getvalue())



class TestSensitivityHeader(unittest.TestCase):
    def test_header_sits_above_the_bars(self):
        from kungfu import checklist, select, sensitivity, topics

        items = [Learning(title=f"t{i}", body="x", source_type="claude-memory", topics=[t])
                 for i, t in enumerate(["git", "git", "documentation", "ci-cd"])]
        out = io.StringIO()
        orig = checklist.run

        def run(*a, **kw):
            return orig(*a, **{**kw, "keys": iter(["enter"]), "out": out, "color": False})

        from unittest import mock

        with mock.patch.object(select.checklist, "run", run):
            select._pick_topics_checklist(topics.counts(items), "export", {}, True, False)
        frame = out.getvalue().split("\x1b[J")[1].split("\x1b[")[0].splitlines()
        head = next(l for l in frame if "sensitivity ▾" in l)
        rows = [l for l in frame if "items" in l or " item " in l]
        col = head.index("sensitivity")
        self.assertTrue(rows)
        for row in rows:
            self.assertIn(row[col], "█▏▎▍▌▋▊▉·", row)  # every bar starts right under the heading
        self.assertTrue(sensitivity.HEADER.startswith("sensitivity ▾"))



class TestAutomaticProjectTerms(Fixture):
    def test_common_words_in_repo_names_are_not_scrubbed(self):
        from unittest import mock

        from kungfu.extract import project_terms

        for name in ("metadata-to-yaml", "docker", "javascript", "json-schema-2.0-flattened", "acme-zephyrine",
                     "baseline-reports"):
            (self.home / "git" / name / ".git").mkdir(parents=True)
        with mock.patch("kungfu.sources.instructions.repos_to_read",
                        lambda roots, only: sorted((self.home / "git").iterdir())):
            terms = project_terms(self.config(), Plan(claude=False))
        for word in ("metadata", "docker", "javascript", "flattened", "schema", "json", "baseline"):
            self.assertNotIn(word, {t.lower() for t in terms}, word)
        self.assertIn("zephyrine", terms)  # a distinctive part of a repo name is still a name
        self.assertIn("metadata-to-yaml", terms)  # and so is the whole hyphenated name



class TestExamineContext(unittest.TestCase):
    def test_stemming_hyphens_and_the_overview_reach_claude(self):
        import tempfile as tf

        from kungfu import examine, lessons

        with tf.TemporaryDirectory() as d:
            root = Path(d)
            write(root / "wiki/tools/dev-machines.md", "Everyone develops on Linux laptops. No cloud-synced folders for code.\n")
            write(root / "wiki/concepts/release-notes.md", "Release notes are a short list of highlights.\n")
            write(root / "wiki/index.md", "# Index\n- [[tools/dev-machines]]\n")
            le = lessons.make("Keep build output out of cloud-synced folders", "Use .nosync names for git and build dirs.",
                              "Sync conflicts corrupt them.", [], "", "", ["icloud"], "medium", [])
            le2 = lessons.make("Expect iCloud duplicate folders in synced repos", "Ignore 'name 2' copies.", "", [], "", "",
                               ["icloud"], "low", [])
            notes = examine.index(root)
            self.assertNotIn("index", {n.title for n in notes})  # index pages aren't notes
            self.assertEqual([n.title for n, _ in examine.related(le, notes)], ["dev-machines"])
            fake = FakeClaude()
            examine.examine([le, le2], notes, "claude-opus-5-5", root, client=fake, out=io.StringIO(), batch_size=1)
        second = fake.calls[1]["messages"][0]["content"]
        self.assertIn("<recipient_overview>", second)
        self.assertIn("No cloud-synced folders for code", second)  # the page reaches Claude even without a match



class TestTopicFoldingAndTemplates(unittest.TestCase):
    def test_topics_fold_onto_the_vocabulary(self):
        from kungfu.lessons import fold_topic

        vocab = ["agent-workflow", "ci-cd", "git", "aws", "diagram"]
        self.assertEqual(fold_topic("Agent Workflows", vocab), "agent-workflow")
        self.assertEqual(fold_topic("ci", vocab), "ci-cd")
        self.assertEqual(fold_topic("diagrams", vocab), "diagram")
        self.assertEqual(fold_topic("Mermaid", vocab), "mermaid")  # new topics are allowed

    def test_template_urls_are_not_internal_hosts(self):
        r = Redactor()
        for t in ("Query https://<your-federation-mdq>/entities/{urlencoded-entityID}", "see https://{host}/api"):
            self.assertEqual(r.clean_text(t)[0], t)
        self.assertEqual(r.clean_text("http://db1/admin")[0], "[internal-url]")



class TestBundleVersions(Fixture):
    def _rewrite_manifest(self, path: Path, out: Path, change) -> Path:
        with zipfile.ZipFile(path) as src, zipfile.ZipFile(out, "w") as dst:
            for n in src.namelist():
                data = src.read(n)
                if n == "manifest.json":
                    m = json.loads(data)
                    change(m)
                    data = json.dumps(m).encode()
                dst.writestr(n, data)
        return out

    def test_only_the_same_version_is_imported(self):
        import contextlib

        from kungfu import __version__, lessons

        le = lessons.make("Pass the region", "Always pass --region.", "Defaults vary.", [], "", "", ["aws"], "high", [])
        good = self.home / "good.kungfu"
        bundle.write(good, [le], title="V", content="lessons")
        self.assertEqual(json.loads(zipfile.ZipFile(good).read("manifest.json"))["kungfu_version"], __version__)
        self.assertEqual(bundle.read(good).version, __version__)
        other = self._rewrite_manifest(good, self.home / "other.kungfu", lambda m: m.update(kungfu_version="0.1.9"))
        unversioned = self._rewrite_manifest(good, self.home / "old.kungfu",
                                             lambda m: (m.pop("kungfu_version"), m.update(generator="i-know-kung-fu 0.2.0")))
        anonymous = self._rewrite_manifest(good, self.home / "anon.kungfu",
                                           lambda m: (m.pop("kungfu_version"), m.pop("generator")))
        for path, mentions in ((other, "kungfu 0.1.9"), (unversioned, "made by kungfu 0.2.0"),
                               (anonymous, "didn't record its version")):
            with self.assertRaises(bundle.VersionMismatch) as cm:
                bundle.read(path)
            self.assertIn(mentions, str(cm.exception))
            self.assertIn(f"this is kungfu {__version__}", str(cm.exception))
            self.assertEqual(len(bundle.read(path, strict=False).items), 1)  # inspect can still read it
        target = self.home / "V"
        (target / ".obsidian").mkdir(parents=True)
        err = io.StringIO()
        with contextlib.ExitStack() as stack:
            for p in with_fake_claude()[1]:
                stack.enter_context(p)
            stack.enter_context(contextlib.redirect_stderr(err))
            self.assertEqual(main(["import", "--accept-risk", str(other), "--into", str(target), "--all-topics", "-y"]), 1)
            self.assertEqual(main(["import", "--accept-risk", str(good), "--into", str(target), "--all-topics", "-y"]), 0)
        self.assertIn("made by kungfu 0.1.9", err.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["inspect", str(other)]), 0)
        self.assertIn("can't be imported here", out.getvalue())



class TestPeopleFolderTemplates(Fixture):
    def test_a_people_note_titled_with_a_word_is_not_a_name(self):
        write(self.vault / "People/person.md", "Template for people notes.\n")
        write(self.vault / "Topics/Review.md", "---\ntags: [kungfu]\n---\nAsk a person, not an AI, to review the "
                                               "release before it ships; Jane Smith agrees.\n")
        item = next(i for i in run(self.config(), Plan(claude=False, repos=False, vaults=[self.vault])).items
                    if "release" in i.body)
        self.assertIn("Ask a person, not an AI", item.body)
        self.assertNotIn("Jane Smith", item.body)



class TestRiskAcknowledgement(unittest.TestCase):
    def _run(self, argv, tty: bool, answer: str | None = None):
        from kungfu import cli

        called = []
        errs = io.StringIO()
        with mock.patch.object(cli, "interactive", return_value=tty), \
             mock.patch.object(cli, "cmd_export", side_effect=lambda a: called.append(a) or 0), \
             mock.patch("builtins.input", side_effect=[answer] if answer is not None else EOFError), \
             contextlib.redirect_stderr(errs):
            # the parser binds fn at build time, so patch the bound function too
            real_parser = cli.parser

            def parser():
                ap = real_parser()
                ap._subparsers._group_actions[0].choices["export"].set_defaults(fn=cli.cmd_export)
                return ap

            with mock.patch.object(cli, "parser", parser):
                code = cli.main(argv)
        return code, bool(called), errs.getvalue()

    def test_refused_without_acknowledgement_when_not_interactive(self):
        code, ran, out = self._run(["export", "-y"], tty=False)
        self.assertEqual((code, ran), (1, False))
        self.assertIn("EXPERIMENTAL", out)
        self.assertIn("--accept-risk", out)

    def test_yes_is_not_an_acknowledgement(self):
        code, ran, _ = self._run(["export", "--yes"], tty=False)
        self.assertFalse(ran)

    def test_the_flag_acknowledges_one_run(self):
        code, ran, out = self._run(["export", "-y", "--accept-risk"], tty=False)
        self.assertEqual((code, ran), (0, True))
        self.assertIn("own risk", out)

    def test_typing_the_phrase_acknowledges(self):
        for answer in ("I accept the risk", "  i ACCEPT the risk. "):
            code, ran, _ = self._run(["export"], tty=True, answer=answer)
            self.assertEqual((code, ran), (0, True), answer)

    def test_anything_else_stops(self):
        for answer in ("", "y", "yes", "accept", "I accept"):
            code, ran, out = self._run(["export"], tty=True, answer=answer)
            self.assertEqual((code, ran), (1, False), answer)
            self.assertIn("nothing was done", out)

    def test_read_only_commands_need_no_acknowledgement(self):
        from kungfu import cli

        self.assertEqual(set(risk.WHAT), {"init", "export", "import"})



class TestWritersNeedAcknowledgement(Fixture):
    def _bundle(self):
        from kungfu import lessons

        le = lessons.make("Pin the region", "Pass --region every time.", "Defaults vary.", ["Add --region."],
                          "CI", "", ["aws"], "high", [])
        out = self.home / "b.kungfu"
        bundle.write(out, [le], title="Risky", content="lessons")
        return bundle.read(out)

    def test_writers_refuse_without_an_acknowledgement(self):
        b = self._bundle()
        vault, wiki = self.home / "V", self.home / "W"
        (vault / ".obsidian").mkdir(parents=True)
        (wiki / "wiki").mkdir(parents=True)
        for load, target in ((obs_imp.load, vault), (wiki_imp.load, wiki)):
            before = sorted(p for p in target.rglob("*"))
            with self.assertRaises(risk.NotAcknowledged) as caught:
                load(b, b.items, target)
            self.assertIn("EXPERIMENTAL", str(caught.exception))
            self.assertEqual(sorted(p for p in target.rglob("*")), before)  # nothing written

    def test_an_export_or_init_acknowledgement_does_not_cover_an_import(self):
        b = self._bundle()
        vault = self.home / "V2"
        (vault / ".obsidian").mkdir(parents=True)
        other = risk.acknowledge("export", flag=True, say=lambda m: None)
        with self.assertRaises(risk.NotAcknowledged):
            obs_imp.load(b, b.items, vault, ack=other)

    def test_an_acknowledgement_cannot_be_made_by_hand(self):
        with self.assertRaises(TypeError):
            risk.Acknowledgement("import", "typed")

    def test_a_dry_run_writes_nothing_and_needs_none(self):
        b = self._bundle()
        vault = self.home / "V3"
        (vault / ".obsidian").mkdir(parents=True)
        obs_imp.load(b, b.items, vault, dry_run=True)
        self.assertEqual([p for p in vault.rglob("*.md")], [])

    def test_acknowledged_writes_go_ahead(self):
        b = self._bundle()
        vault = self.home / "V4"
        (vault / ".obsidian").mkdir(parents=True)
        r = obs_imp.load(b, b.items, vault, ack=ACK)
        self.assertEqual(len(r.written), 1)


if __name__ == "__main__":
    unittest.main()
