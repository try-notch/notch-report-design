"""
prompts.py holds copies of text measured in the demo modules (prompt_variants.py, seed_db.py,
llm.py), so the server can run without importing them. These tests rebuild every copy from
its source, exactly as analysis.py and reports.py used to at import time, and fail if one
drifts; and they prove the server really does run without the demo modules.
"""

import subprocess
import sys

import llm
import prompt_variants
import seed_db

from notch_api import analysis, prompts, reports


def test_the_catalog_is_seed_dbs():
    assert prompts.CATEGORY_CATALOG == tuple(seed_db.TAG_CATALOG)
    assert prompts.CATEGORIES == tuple(seed_db.TAGS)
    assert prompts.format_catalog(prompts.CATEGORY_CATALOG) == prompt_variants.format_tag_catalog(
        [{"name": n, "explanation": e} for n, e in seed_db.TAG_CATALOG])


def test_the_label_text_is_the_measured_text():
    notes, project_match = analysis._copied_sections(prompt_variants._SHARED_TAIL)
    assert prompts.LABEL_PREAMBLE == prompt_variants._SHARED_PREAMBLE
    assert prompts.CATEGORY_POLICY_V4 == prompt_variants.V4_FIXED
    assert prompts.IMPACT_AND_RECOGNITION == notes
    assert prompts.PROJECT_MATCH == project_match


def test_the_report_voice_is_the_demo_writers():
    source = llm.SYSTEM_PROMPT
    assert prompts.REPORT_VOICE == source[source.index("VOICE\n"):source.index("\n\nDATES\n")]
    for header in ("VOICE", "HARD CONSTRAINT ON STRENGTHS & GROWTH", "WORK THAT DOESN'T USUALLY GET COUNTED"):
        assert f"{header}\n" in prompts.REPORT_VOICE


def test_the_assembled_v4_prompts_are_what_v1_sent():
    catalog = prompt_variants.format_tag_catalog([{"name": n, "explanation": e} for n, e in seed_db.TAG_CATALOG])
    notes, project_match = analysis._copied_sections(prompt_variants._SHARED_TAIL)
    writing = prompts._WRITING
    assert prompts.LABEL_V4.system == prompt_variants._SHARED_PREAMBLE + writing + notes
    assert prompts.LABEL_V4.fallback_system == (
        prompt_variants._SHARED_PREAMBLE + prompt_variants.V4_FIXED.replace("{tag_catalog}", catalog)
        + writing + prompts._MOOD + notes + project_match)
    assert prompts.REPORT_R1.system.count(prompts.REPORT_VOICE) == 1
    # v1 still sends exactly these.
    assert (analysis.SYSTEM_PROMPT, analysis.FALLBACK_PROMPT) == (prompts.LABEL_V4.system,
                                                                 prompts.LABEL_V4.fallback_system)
    assert (reports.SYSTEM_PROMPT, reports.WRITE_REPORT) == (prompts.REPORT_R1.system, prompts.REPORT_R1.schema)


def test_every_variant_resolves_and_unknown_names_do_not():
    for kind, variants in prompts.VARIANTS.items():
        for name in variants:
            assert prompts.variant(kind, name) is variants[name]
    try:
        prompts.variant("analyze", "no-such-variant")
    except KeyError:
        pass
    else:
        raise AssertionError("an unknown variant resolved")


def test_the_server_runs_without_the_demo_modules():
    """Importing the whole server pulls in none of prompt_variants, seed_db, llm or anthropic."""
    code = ("import sys; import notch_api.app, notch_api.__main__; "
            "loaded = {'prompt_variants', 'seed_db', 'llm', 'anthropic', 'matplotlib', 'reportlab'} & set(sys.modules); "
            "print(sorted(loaded)); sys.exit(1 if loaded else 0)")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
