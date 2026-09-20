#!/usr/bin/env python3
"""
Unit tests for gif_tool.py
Tests multi-tier dynamic search: Tenor -> Giphy -> Graceful Skip.
"""
import unittest
from unittest.mock import patch, MagicMock
from tools.gif_tool import (
    clean_slug_title,
    is_ocr_safe,
    extract_gif_ocr,
    get_contextual_gif,
    is_valid_gif_url,
    detect_franchise,
    check_cooldown,
    get_cooldown_summary,
    get_history_urls,
    load_history,
    log_gif_failure,
    get_recent_failures,
    score_candidate,
    FAILURE_LOG_FILE,
)

class TestGifTool(unittest.TestCase):

    def test_clean_slug_title(self):
        self.assertEqual(
            clean_slug_title("arrested-development-lucille-bluth-lock-the-door-gif-26514757"),
            "Arrested Development Lucille Bluth Lock The"
        )
        self.assertEqual(
            clean_slug_title("ive-made-a-huge-mistake-gif-12345"),
            "I've Made A Huge Mistake"
        )
        self.assertEqual(
            clean_slug_title("dont-look-back-in-anger-999"),
            "Don't Look Back In Anger"
        )
        self.assertEqual(
            clean_slug_title("intergalactic-quality-gif-hd-trending-dance-moves"),
            "Dance Moves"
        )
        self.assertEqual(clean_slug_title(""), "Reaction GIF")
        self.assertEqual(clean_slug_title(None), "Reaction GIF")

    def test_is_ocr_safe(self):
        self.assertTrue(is_ocr_safe("I've made a huge mistake"))
        self.assertTrue(is_ocr_safe(""))
        self.assertTrue(is_ocr_safe("This is fine."))
        # Word boundary protects benign substrings
        self.assertTrue(is_ocr_safe("we met at the cocktail lounge"))
        # Blocked terms rejected
        self.assertFalse(is_ocr_safe("get out you faggot"))
        self.assertFalse(is_ocr_safe("go kill yourself right now"))
        self.assertFalse(is_ocr_safe("white nazi propaganda"))

    def test_extract_gif_ocr_empty(self):
        self.assertEqual(extract_gif_ocr(""), "")
        self.assertEqual(extract_gif_ocr("http://invalid.local/fake.gif"), "")

    @patch("urllib.request.urlopen")
    def test_is_valid_gif_url_redirect_detection(self, mock_urlopen):
        # 1. Direct 200 without redirect -> True
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.geturl.return_value = "https://tenor.com/view/suspect-guilty-19455657"
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp
        self.assertTrue(is_valid_gif_url("https://tenor.com/view/suspect-guilty-19455657"))

        # 2. Redirect with matching slug -> True
        mock_resp.geturl.return_value = "https://tenor.com/view/suspect-guilty-itysl-19455657"
        self.assertTrue(is_valid_gif_url("https://tenor.com/view/suspect-guilty-19455657"))

        # 3. Redirect with zero matching words (hallucinated URL) -> False
        mock_resp.geturl.return_value = "https://tenor.com/view/xdbacom-sfea-gif-20092285"
        self.assertFalse(is_valid_gif_url("https://tenor.com/view/hot-dog-suit-costume-car-crash-i-think-you-should-leave-gif-20092285"))

        # 4. Status non-200 -> False
        mock_resp.status = 404
        self.assertFalse(is_valid_gif_url("https://tenor.com/view/broken-404"))

    def test_detect_franchise(self):
        self.assertEqual(detect_franchise("arrested development lucille bluth"), "arrested_development")
        self.assertEqual(detect_franchise("good-for-her-lucille-bluth-gif"), "arrested_development")
        self.assertEqual(detect_franchise("dead dove do not eat"), "arrested_development")
        self.assertEqual(detect_franchise("gilfoyle server fire"), "silicon_valley")
        self.assertEqual(detect_franchise("richard-hendricks-pied-piper"), "silicon_valley")
        self.assertEqual(detect_franchise("larry david pretty good"), "curb_your_enthusiasm")
        self.assertEqual(detect_franchise("tim robinson hot dog suit"), "i_think_you_should_leave")
        self.assertEqual(detect_franchise("jeff winger community wow"), "community")
        self.assertEqual(detect_franchise("random cat typing"), None)
        self.assertEqual(detect_franchise(""), None)
        self.assertEqual(detect_franchise(None), None)

    @patch("tools.gif_tool.get_runtime_gif_rules", return_value={"enabled": True, "default_cooldown_turns": 2, "franchise_cooldowns": {"arrested_development": 4}, "quarantined_franchises": []})
    def test_cooldown_calculation(self, mock_rules):
        mock_history = [
            {"url": "u1", "franchise": "curb_your_enthusiasm"},
            {"url": "u2", "franchise": "silicon_valley"},
            {"url": "u3", "franchise": "community"},
            {"url": "u4", "franchise": "arrested_development"},
        ]
        # arrested_development is distance 1, default threshold 4 -> True
        is_cd, dist, thresh = check_cooldown("arrested_development", history=mock_history)
        self.assertTrue(is_cd)
        self.assertEqual(dist, 1)
        self.assertEqual(thresh, 4)

        # community is distance 2, default threshold 2 -> True
        is_cd, dist, thresh = check_cooldown("community", history=mock_history)
        self.assertTrue(is_cd)
        self.assertEqual(dist, 2)
        self.assertEqual(thresh, 2)

        # silicon_valley is distance 3, threshold 2 -> False
        is_cd, dist, thresh = check_cooldown("silicon_valley", history=mock_history)
        self.assertFalse(is_cd)
        self.assertEqual(dist, 3)

        # Unused franchise has distance 999 -> False
        is_cd, dist, thresh = check_cooldown("30_rock", history=mock_history)
        self.assertFalse(is_cd)

    @patch("tools.gif_tool.get_runtime_gif_rules", return_value={"enabled": True, "default_cooldown_turns": 2, "franchise_cooldowns": {"arrested_development": 4}, "quarantined_franchises": []})
    def test_history_legacy_strings_support(self, mock_rules):
        legacy = [
            "https://tenor.com/view/already-used-1",
            "https://tenor.com/view/good-for-her-arrested-development-lucille-bluth-gif-11778278665179077372"
        ]
        urls = get_history_urls(legacy)
        self.assertIn("https://tenor.com/view/already-used-1", urls)
        self.assertIn("https://tenor.com/view/good-for-her-arrested-development-lucille-bluth-gif-11778278665179077372", urls)

        # check cooldown works on legacy list
        is_cd, dist, thresh = check_cooldown("arrested_development", history=legacy)
        self.assertTrue(is_cd)
        self.assertEqual(dist, 1)

    @patch("tools.gif_tool.find_canonical_gif", return_value=None)
    @patch("tools.gif_tool.is_valid_gif_url", return_value=True)
    @patch("tools.gif_tool.search_tenor")
    @patch("tools.gif_tool.load_history", return_value=[])
    @patch("tools.gif_tool.record_history")
    def test_tenor_primary_success(self, mock_rec, mock_hist, mock_tenor, mock_valid, mock_canon):
        mock_tenor.return_value = [
            {"title": "Test Tenor", "url": "https://tenor.com/view/test-123", "media_url": None}
        ]
        res = get_contextual_gif("test query", run_ocr=False, allow_dynamic=True)
        self.assertEqual(res["source"], "dynamic_tenor")
        self.assertEqual(res["url"], "https://tenor.com/view/test-123")
        self.assertEqual(res["markdown"], "[GIF](https://tenor.com/view/test-123)")
        mock_rec.assert_called_once_with(
            "https://tenor.com/view/test-123",
            query="test query",
            title="Test Tenor",
            franchise=None
        )

    @patch("tools.gif_tool.find_canonical_gif", return_value=None)
    @patch("tools.gif_tool.is_valid_gif_url", return_value=True)
    @patch("tools.gif_tool.search_tenor")
    @patch("tools.gif_tool.extract_gif_ocr")
    @patch("tools.gif_tool.load_history", return_value=[])
    @patch("tools.gif_tool.record_history")
    def test_tenor_ocr_safety_rejection(self, mock_rec, mock_hist, mock_ocr, mock_tenor, mock_valid, mock_canon):
        # First candidate contains toxic text; second candidate is clean
        mock_tenor.return_value = [
            {"title": "Toxic Candidate", "url": "https://tenor.com/view/toxic-1", "media_url": "https://media.tenor.com/toxic.gif"},
            {"title": "Clean Candidate", "url": "https://tenor.com/view/clean-2", "media_url": "https://media.tenor.com/clean.gif"}
        ]
        mock_ocr.side_effect = ["kill yourself", "I've made a huge mistake"]
        res = get_contextual_gif("test query", run_ocr=True, allow_dynamic=True)
        self.assertEqual(res["source"], "dynamic_tenor")
        self.assertEqual(res["url"], "https://tenor.com/view/clean-2")
        self.assertEqual(res["title"], "Clean Candidate")
        self.assertEqual(res["markdown"], "[GIF](https://tenor.com/view/clean-2)")
        mock_rec.assert_called_once_with(
            "https://tenor.com/view/clean-2",
            query="test query",
            title="Clean Candidate",
            franchise="arrested_development"
        )

    @patch("tools.gif_tool.find_canonical_gif", return_value=None)
    @patch("tools.gif_tool.is_valid_gif_url", return_value=True)
    @patch("tools.gif_tool.search_tenor", return_value=[])
    @patch("tools.gif_tool.search_giphy")
    @patch("tools.gif_tool.load_history", return_value=[])
    @patch("tools.gif_tool.record_history")
    def test_giphy_fallback_success(self, mock_rec, mock_hist, mock_giphy, mock_tenor, mock_valid, mock_canon):
        mock_giphy.return_value = [
            {"title": "Test Giphy", "url": "https://giphy.com/gifs/test-giphy-abc456", "media_url": None}
        ]
        res = get_contextual_gif("test query", run_ocr=False, allow_dynamic=True)
        self.assertEqual(res["source"], "dynamic_giphy")
        self.assertEqual(res["url"], "https://giphy.com/gifs/test-giphy-abc456")
        self.assertEqual(res["markdown"], "[GIF](https://giphy.com/gifs/test-giphy-abc456)")
        mock_rec.assert_called_once_with(
            "https://giphy.com/gifs/test-giphy-abc456",
            query="test query",
            title="Test Giphy",
            franchise=None
        )

    @patch("tools.gif_tool.find_canonical_gif", return_value=None)
    @patch("tools.gif_tool.search_tenor", return_value=[])
    @patch("tools.gif_tool.search_giphy", return_value=[])
    @patch("tools.gif_tool.load_history", return_value=[])
    def test_graceful_skip_when_both_fail(self, mock_hist, mock_giphy, mock_tenor, mock_canon):
        res = get_contextual_gif("unfindable query", allow_dynamic=True)
        self.assertEqual(res["source"], "skip")
        self.assertIsNone(res["url"])
        self.assertIsNone(res["title"])
        self.assertIsNone(res["markdown"])

    @patch("tools.gif_tool.find_canonical_gif", return_value=None)
    @patch("tools.gif_tool.record_history")
    @patch("tools.gif_tool.is_valid_gif_url")
    @patch("tools.gif_tool.search_tenor")
    @patch("tools.gif_tool.load_history")
    def test_history_filtering(self, mock_hist, mock_tenor, mock_valid, mock_rec, mock_canon):
        used_url = "https://tenor.com/view/already-used-1"
        fresh_url = "https://tenor.com/view/fresh-2"
        mock_hist.return_value = [{"url": used_url, "franchise": None}]
        mock_tenor.return_value = [
            {"title": "used", "url": used_url, "media_url": None},
            {"title": "fresh", "url": fresh_url, "media_url": None}
        ]
        mock_valid.return_value = True
        res = get_contextual_gif("query", run_ocr=False, allow_dynamic=True)
        self.assertEqual(res["url"], fresh_url)

    @patch("tools.gif_tool.check_cooldown", return_value=(True, 1, 8))
    def test_explicit_query_cooldown_blocked(self, mock_cd):
        res = get_contextual_gif("arrested development lucille wink")
        self.assertEqual(res["source"], "cooldown_blocked")
        self.assertIn("cooldown", res["error"].lower())
        self.assertEqual(res["franchise"], "arrested_development")

    @patch("tools.gif_tool.find_canonical_gif", return_value=None)
    @patch("tools.gif_tool.record_history")
    @patch("tools.gif_tool.is_valid_gif_url", return_value=True)
    @patch("tools.gif_tool.search_tenor")
    @patch("tools.gif_tool.load_history")
    def test_candidate_filtering_skips_cooled_down_franchise(self, mock_hist, mock_tenor, mock_valid, mock_rec, mock_canon):
        mock_hist.return_value = [
            {"url": "https://tenor.com/view/ad-1", "franchise": "arrested_development", "query": "", "title": ""}
        ]
        mock_tenor.return_value = [
            {"title": "Lucille Bluth Wink", "url": "https://tenor.com/view/lucille-bluth-wink-1", "media_url": None},
            {"title": "Gilfoyle Smug", "url": "https://tenor.com/view/gilfoyle-smug-2", "media_url": None}
        ]
        res = get_contextual_gif("smug stare", run_ocr=False, allow_dynamic=True)
        self.assertEqual(res["url"], "https://tenor.com/view/gilfoyle-smug-2")
        self.assertEqual(res["franchise"], "silicon_valley")

    @patch("tools.gif_tool.find_canonical_gif", return_value=None)
    @patch("tools.gif_tool.record_history")
    @patch("tools.gif_tool.is_valid_gif_url", return_value=True)
    @patch("tools.gif_tool.search_tenor")
    @patch("tools.gif_tool.load_history")
    def test_force_bypasses_cooldown(self, mock_hist, mock_tenor, mock_valid, mock_rec, mock_canon):
        mock_hist.return_value = [
            {"url": "https://tenor.com/view/ad-1", "franchise": "arrested_development", "query": "", "title": ""}
        ]
        mock_tenor.return_value = [
            {"title": "Lucille Bluth Wink", "url": "https://tenor.com/view/lucille-bluth-wink-1", "media_url": None}
        ]
        res = get_contextual_gif("lucille bluth wink", run_ocr=False, force=True, allow_dynamic=True)
        self.assertEqual(res["url"], "https://tenor.com/view/lucille-bluth-wink-1")
        self.assertEqual(res["source"], "dynamic_tenor")

    def test_failure_logging_and_retrieval(self):
        log_gif_failure(
            query="test_cooldown_query",
            reason="cooldown_blocked",
            details="Test details",
            franchise="silicon_valley",
            candidate_url="https://tenor.com/view/test"
        )
        recent = get_recent_failures(limit=5)
        self.assertTrue(len(recent) >= 1)
        latest = recent[0]
        self.assertEqual(latest["query"], "test_cooldown_query")
        self.assertEqual(latest["reason"], "cooldown_blocked")
        self.assertEqual(latest["franchise"], "silicon_valley")

    def test_score_candidate_franchise_integrity(self):
        # Specific franchise in query disqualifies candidates without that franchise
        q = "Succession toast"
        bad_cand = {"title": "Toast Aubrey Omori", "url": "https://tenor.com/view/toast-aubrey-24550137"}
        good_cand = {"title": "Succession Hbo Logan Roy", "url": "https://tenor.com/view/succession-hbo-123"}
        self.assertLess(score_candidate(q, bad_cand), 0)
        self.assertGreaterEqual(score_candidate(q, good_cand), 40)

    def test_score_candidate_token_overlap(self):
        q = "doc rivers disbelief"
        partial_cand = {"title": "In Disbelief", "url": "https://tenor.com/view/in-disbelief-1"}
        full_cand = {"title": "Disbelief Shock Doc Rivers", "url": "https://tenor.com/view/disbelief-shock-doc-rivers-2"}
        self.assertGreater(score_candidate(q, full_cand), score_candidate(q, partial_cand))

    @patch("tools.gif_tool.find_canonical_gif", return_value=None)
    @patch("tools.gif_tool.is_valid_gif_url", return_value=True)
    @patch("tools.gif_tool.search_tenor")
    @patch("tools.gif_tool.load_history", return_value=[])
    @patch("tools.gif_tool.record_history")
    def test_relevance_ranking_picks_highest_scoring_candidate(self, mock_rec, mock_hist, mock_tenor, mock_valid, mock_canon):
        # Index 0 has only generic hit; index 1 has full phrase and entity match
        mock_tenor.return_value = [
            {"title": "Judge Judy", "url": "https://tenor.com/view/judge-judy-1", "media_url": None},
            {"title": "Case Closed Pikachu", "url": "https://tenor.com/view/case-closed-pikachu-2", "media_url": None}
        ]
        res = get_contextual_gif("case closed", run_ocr=False, allow_dynamic=True)
        self.assertEqual(res["url"], "https://tenor.com/view/case-closed-pikachu-2")
        self.assertEqual(res["title"], "Case Closed Pikachu")

    @patch("tools.gif_tool.find_canonical_gif", return_value=None)
    @patch("tools.gif_tool.is_valid_gif_url", return_value=True)
    @patch("tools.gif_tool.search_tenor")
    @patch("tools.gif_tool.search_giphy")
    @patch("tools.gif_tool.load_history", return_value=[])
    @patch("tools.gif_tool.record_history")
    def test_tenor_disqualified_falls_back_to_giphy(self, mock_rec, mock_hist, mock_giphy, mock_tenor, mock_valid, mock_canon):
        # Tenor only has irrelevant bread toast (disqualified for Succession query)
        mock_tenor.return_value = [
            {"title": "Toast Aubrey Omori", "url": "https://tenor.com/view/toast-aubrey-24550137", "media_url": None}
        ]
        # Giphy has actual Succession candidate
        mock_giphy.return_value = [
            {"title": "Successionhbo Tv Television Succession", "url": "https://giphy.com/gifs/successionhbo-123", "media_url": None}
        ]
        res = get_contextual_gif("Succession toast", run_ocr=False, allow_dynamic=True)
        self.assertEqual(res["source"], "dynamic_giphy")
        self.assertEqual(res["url"], "https://giphy.com/gifs/successionhbo-123")
        self.assertEqual(res["franchise"], "succession")

    @patch("tools.gif_tool.is_ocr_safe")
    @patch("tools.gif_tool.extract_gif_ocr")
    @patch("tools.gif_tool.is_valid_gif_url", return_value=True)
    @patch("tools.gif_tool.load_history", return_value=[])
    @patch("tools.gif_tool.record_history")
    def test_canonical_registry_tier_0_match(self, mock_rec, mock_hist, mock_valid, mock_ocr, mock_safe):
        # Query matches canonical entry directly without calling external APIs or OCR safety checks
        res = get_contextual_gif("started blasting", run_ocr=True, force=True, use_llm=False)
        self.assertEqual(res["source"], "canonical_registry")
        self.assertEqual(res["canonical_id"], "iasip_frank_started_blasting")
        self.assertEqual(res["url"], "https://tenor.com/view/danny-devito-guns-always-sunny-i-started-blasting-gif-15815322")
        mock_ocr.assert_not_called()
        mock_safe.assert_not_called()

    @patch("tools.gif_tool.find_canonical_gif", return_value=None)
    @patch("tools.gif_tool.is_valid_gif_url", return_value=True)
    @patch("tools.gif_tool.load_history", return_value=[])
    @patch("tools.gif_tool.check_cooldown", return_value=(True, 1, 4))
    @patch("tools.gif_tool.search_tenor")
    @patch("tools.gif_tool.record_history")
    def test_canonical_cooldown_skips_to_dynamic(self, mock_rec, mock_tenor, mock_cd, mock_hist, mock_valid, mock_canon):
        # When canonical franchise is on cooldown, canonical matcher skips it
        mock_tenor.return_value = [
            {"title": "Fallback Tenor", "url": "https://tenor.com/view/fallback-1", "media_url": None}
        ]
        res = get_contextual_gif("stanley eye roll", run_ocr=False, force=False, allow_dynamic=True)
        # Even though "stanley eye roll" matches office_stanley_eye_roll, cooldown skips it
        self.assertEqual(res["source"], "dynamic_tenor")
        self.assertEqual(res["url"], "https://tenor.com/view/fallback-1")

    @patch("tools.gif_tool.find_canonical_gif", return_value=None)
    @patch("tools.gif_tool.is_valid_gif_url", return_value=True)
    @patch("tools.gif_tool.load_history")
    @patch("tools.gif_tool.search_tenor")
    @patch("tools.gif_tool.record_history")
    def test_canonical_anti_repetition_skips_used_url(self, mock_rec, mock_tenor, mock_hist, mock_valid, mock_canon):
        # If canonical URL is in history, canonical matcher skips it
        canon_url = "https://tenor.com/view/doors-that-open-like-this-silicon-valley-russ-gif-11431521"
        mock_hist.return_value = [
            {"url": canon_url, "franchise": "silicon_valley", "query": "doors", "title": "Billionaire Doors"}
        ]
        mock_tenor.return_value = [
            {"title": "Fallback Car", "url": "https://tenor.com/view/fallback-car", "media_url": None}
        ]
        res = get_contextual_gif("billionaire doors", run_ocr=False, force=True, allow_dynamic=True)
        self.assertEqual(res["source"], "dynamic_tenor")
        self.assertEqual(res["url"], "https://tenor.com/view/fallback-car")

    @patch("tools.gif_tool.is_valid_gif_url", return_value=True)
    @patch("tools.gif_tool.load_history", return_value=[])
    @patch("tools.gif_tool.record_history")
    def test_canonical_franchise_affinity_and_id_match(self, mock_rec, mock_hist, mock_valid):
        # Franchise query snaps to canonical registry candidate
        res = get_contextual_gif("silicon valley dinesh shrug", run_ocr=False, force=True, use_llm=False)
        self.assertEqual(res["source"], "canonical_registry")
        self.assertEqual(res["franchise"], "silicon_valley")

        # Direct ID match resolves to canonical registry
        res2 = get_contextual_gif("30_rock_buscemi_fellow_kids", run_ocr=False, force=True, use_llm=False)
        self.assertEqual(res2["source"], "canonical_registry")
        self.assertEqual(res2["canonical_id"], "30_rock_buscemi_fellow_kids")

    @patch("tools.gif_tool.is_valid_gif_url", return_value=True)
    @patch("tools.gif_tool.load_history", return_value=[])
    @patch("tools.gif_tool.record_history")
    def test_situational_vibe_query_matching(self, mock_rec, mock_hist, mock_valid):
        # 1. Effortless superiority / flawless fix
        r1 = get_contextual_gif("effortless technical superiority solving a problem on the first try", run_ocr=False, force=True, use_llm=False)
        self.assertEqual(r1["source"], "canonical_registry")
        self.assertEqual(r1["canonical_id"], "silicon_valley_gilfoyle_smug")

        # 2. Slapping down terrible ideas
        r2 = get_contextual_gif("slapping down terrible ideas brittle hacks or overcomplicated proposals", run_ocr=False, force=True, use_llm=False)
        self.assertEqual(r2["source"], "canonical_registry")
        self.assertEqual(r2["canonical_id"], "silicon_valley_erlich_slap")

        # 3. Production incident chaos
        r3 = get_contextual_gif("walking into a chaotic production outage with cascading alerts", run_ocr=False, force=True, use_llm=False)
        self.assertEqual(r3["source"], "canonical_registry")
        self.assertEqual(r3["canonical_id"], "community_troy_fire_pizza")

        # 4. External cloud outage while homelab is unaffected
        r4 = get_contextual_gif("watching external cloud outage while local homelab is unaffected", run_ocr=False, force=True, use_llm=False)
        self.assertEqual(r4["source"], "canonical_registry")
        self.assertEqual(r4["canonical_id"], "kermit_sipping_tea")

        # 5. Relief after clean backup restore
        r5 = get_contextual_gif("barely averting disaster and deep relief after clean restore", run_ocr=False, force=True, use_llm=False)
        self.assertEqual(r5["source"], "canonical_registry")
        self.assertEqual(r5["canonical_id"], "training_day_denzel_relief")

        # 6. Shutting down patronizing advice
        r6 = get_contextual_gif("shutting down patronizing advice I know more than you", run_ocr=False, force=True, use_llm=False)
        self.assertEqual(r6["source"], "canonical_registry")
        self.assertEqual(r6["canonical_id"], "parks_rec_ron_know_more")

        # 7. Mic drop after clean fix
        r7 = get_contextual_gif("mic drop signing off after nailing a clean fix", run_ocr=False, force=True, use_llm=False)
        self.assertEqual(r7["source"], "canonical_registry")
        self.assertEqual(r7["canonical_id"], "seinfeld_costanza_high_note")

    @patch("subprocess.run")
    @patch("tools.gif_tool.is_valid_gif_url", return_value=True)
    @patch("tools.gif_tool.load_history", return_value=[])
    @patch("tools.gif_tool.record_history")
    def test_llm_semantic_selection_mocked(self, mock_rec, mock_hist, mock_valid, mock_subproc):
        # Mock LLM returning a specific canonical ID
        mock_proc = MagicMock()
        mock_proc.stdout = "office_jim_welp\n"
        mock_subproc.return_value = mock_proc

        res = get_contextual_gif("mock situation query", run_ocr=False, force=True, use_llm=True)
        self.assertEqual(res["source"], "canonical_registry")
        self.assertEqual(res["canonical_id"], "office_jim_welp")

        # Verify fallback to FTS5 if LLM fails
        mock_proc.stdout = ""
        res_fallback = get_contextual_gif("kermit sipping tea", run_ocr=False, force=True, use_llm=True)
        self.assertEqual(res_fallback["source"], "canonical_registry")
        self.assertEqual(res_fallback["canonical_id"], "kermit_sipping_tea")

    @patch("tools.gif_tool.is_valid_gif_url", return_value=True)
    @patch("tools.gif_tool.load_history", return_value=[])
    @patch("tools.gif_tool.record_history")
    def test_matrix_kung_fu_does_not_hijack_generic_queries(self, mock_rec, mock_hist, mock_valid):
        # Generic status, readiness, confidence, or automation should NOT match Kung Fu
        generic_queries = [
            "online and ready systems nominal",
            "ready to go",
            "confident ironclad locked down fixed",
            "automation patched bug fixed lock secured",
            "quietly confident technical fix everything running silent and smooth",
            "deal locked in strategy ready to roll",
            "back in action online ready",
            "green light or ready to roll",
        ]
        for q in generic_queries:
            res = get_contextual_gif(q, run_ocr=False, force=True, use_llm=False)
            if res.get("canonical_id"):
                self.assertNotEqual(
                    res["canonical_id"],
                    "matrix_neo_kung_fu",
                    f"Query {q!r} incorrectly matched matrix_neo_kung_fu"
                )

    @patch("tools.gif_tool.is_valid_gif_url", return_value=True)
    @patch("tools.gif_tool.load_history", return_value=[])
    @patch("tools.gif_tool.record_history")
    def test_matrix_kung_fu_matches_specific_martial_arts_or_neural_upload(self, mock_rec, mock_hist, mock_valid):
        # Specific martial arts / neural upload queries SHOULD match Kung Fu
        specific_queries = [
            "I know kung fu martial arts download",
            "neural upload sudden skill mastery",
        ]
        for q in specific_queries:
            res = get_contextual_gif(q, run_ocr=False, force=True, use_llm=False)
            self.assertEqual(
                res.get("canonical_id"),
                "matrix_neo_kung_fu",
                f"Query {q!r} should have matched matrix_neo_kung_fu"
            )

    def test_history_window_limit_3(self):
        # Verify default limit=3 filters out the last 3 items, but not older items
        history = [{"url": f"https://tenor.com/view/gif-{i}"} for i in range(10)]
        excluded = get_history_urls(history)
        self.assertEqual(len(excluded), 3)
        self.assertIn("https://tenor.com/view/gif-9", excluded)
        self.assertIn("https://tenor.com/view/gif-7", excluded)
        self.assertNotIn("https://tenor.com/view/gif-6", excluded)
        self.assertNotIn("https://tenor.com/view/gif-0", excluded)


if __name__ == "__main__":
    unittest.main()

